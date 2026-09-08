"""Движки языковых моделей и маршрутизация между ними.

LM Studio, Ollama и OpenRouter получают собственные сроки первого токена.
При включённом LM Studio оба маршрута используют загруженную локальную модель:
обычный запрос — LM_STUDIO_MODEL, coding/terminal/research —
LM_STUDIO_CODE_MODEL. Ollama и OpenRouter остаются резервом на случай, когда
LM Studio не запущен или не ответил вовремя.
"""

import json
import queue
import re
import subprocess
import shutil
from pathlib import Path
from urllib.parse import urlparse
import threading
import time

from openai import OpenAI

import jarvis_state as _state
from jarvis_settings import activity as _settings_activity
from jarvis_log import jarvis_logger

__all__ = [
    "OPENROUTER_MODEL", "OPENROUTER_FREE_MODEL", "OPENROUTER_AGENT_MODEL",
    "OPENROUTER_API_KEY", "LLM_ENGINE", "OLLAMA_URL", "OLLAMA_MODEL",
    "LM_STUDIO_URL", "LM_STUDIO_MODEL", "LM_STUDIO_CODE_MODEL",
    "LLM_DEADLINE", "LLM_DEADLINE_CLOUD", "LLM_DEADLINE_LM_STUDIO", "LLM_GEN_BUDGET",
    "get_openrouter_client", "get_lmstudio_client", "warmup_ollama", "warmup_lmstudio",
    "_ollama_probe", "_ollama_available", "_ollama_deltas", "_cloud_deltas",
    "_lmstudio_deltas", "LLMUnavailable",
    "_pump_engine", "_classify_complexity", "_llm_deltas",
    "_ROUTE_CODE", "_ROUTE_TERMINAL", "_ROUTE_RESEARCH",
]

import os

OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
OPENROUTER_FREE_MODEL = os.getenv("OPENROUTER_FREE_MODEL", "openrouter/free")
OPENROUTER_AGENT_MODEL = os.getenv("OPENROUTER_AGENT_MODEL", OPENROUTER_MODEL)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

LLM_ENGINE = os.getenv("JARVIS_LLM", "local").lower()
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
LM_STUDIO_URL = os.getenv("LM_STUDIO_URL", "http://127.0.0.1:1234/v1").rstrip("/")
LM_STUDIO_MODEL = os.getenv("LM_STUDIO_MODEL", "mistralai/ministral-3-3b")
LM_STUDIO_CODE_MODEL = os.getenv("LM_STUDIO_CODE_MODEL", LM_STUDIO_MODEL)
LLM_DEADLINE = float(os.getenv("JARVIS_LLM_DEADLINE", "40.0"))
LLM_DEADLINE_CLOUD = float(os.getenv("JARVIS_LLM_DEADLINE_CLOUD", "9.0"))
LLM_DEADLINE_LM_STUDIO = float(os.getenv("JARVIS_LLM_DEADLINE_LM_STUDIO", "30.0"))
LLM_GEN_BUDGET = float(os.getenv("JARVIS_LLM_GEN_BUDGET", "24.0"))
LM_STUDIO_AUTOLOAD = os.getenv("LM_STUDIO_AUTOLOAD", "off").lower() == "on"
LM_STUDIO_GPU = os.getenv("LM_STUDIO_GPU", "0.7")
LM_STUDIO_CONTEXT = int(os.getenv("LM_STUDIO_CONTEXT", "8192"))


class LLMUnavailable(RuntimeError):
    """Public, secret-free reasons for every attempted backend."""
    def __init__(self, failures):
        self.failures = tuple(failures)
        super().__init__("Модели не дали ответа. " + "; ".join(failures))


def _failure_reason(name, error):
    label = {"lmstudio": "LM Studio", "local": "Ollama", "cloud": "OpenRouter", "free": "облачный резерв"}.get(name, name)
    value = str(error).lower()
    if "no models loaded" in value:
        reason = "модель не загружена; проверьте автозагрузку и идентификатор"
    elif "model" in value and any(v in value for v in ("not found", "does not exist", "invalid model")):
        reason = "указанная модель не найдена"
    elif "пустой" in value:
        reason = "пустой ответ"
    elif isinstance(error, TimeoutError) or any(v in value for v in ("timeout", "timed out", "deadline", "первого токена", "первый токен")):
        reason = "истёк срок ожидания ответа"
    elif any(v in value for v in ("connection", "connect", "refused")):
        reason = "сервер недоступен"
    else:
        reason = "ошибка модели или соединения"
    return f"{label}: {reason}"


_openrouter_client = None
_lmstudio_client = None

def get_openrouter_client():
    """Singleton OpenAI client (avoids re-creating on every request)."""
    global _openrouter_client
    if _openrouter_client is None:
        _openrouter_client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_API_KEY,
            max_retries=0,  # The router owns retries and their latency budget.
            default_headers={
                "HTTP-Referer": "https://local-jarvis",
                "X-Title": "Jarvis Voice Assistant",
            }
        )
    return _openrouter_client


def get_lmstudio_client():
    """Singleton for LM Studio's OpenAI-compatible local server."""
    global _lmstudio_client
    if _lmstudio_client is None:
        _lmstudio_client = OpenAI(
            base_url=LM_STUDIO_URL,
            # LM Studio's local server accepts a placeholder token by default.
            api_key=os.getenv("LM_STUDIO_API_KEY", "lm-studio"),
            max_retries=0,
        )
    return _lmstudio_client


_ollama_ok = None
_ollama_lock = threading.Lock()


def _ollama_probe(timeout: float = 1.0) -> bool:
    """True if the Ollama server answers and has our model pulled."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=timeout) as r:
            names = [m.get("name", "") for m in json.load(r).get("models", [])]
    except Exception:
        return False
    if OLLAMA_MODEL not in names:
        print(f"[LLM] Ollama работает, но модель '{OLLAMA_MODEL}' не загружена "
              f"(есть: {', '.join(names) or 'ничего'}). Выполните: ollama pull {OLLAMA_MODEL}")
        return False
    return True


def _ollama_available(cancel_event=None, deadline_at=None) -> bool:
    """Probe Ollama once, starting the server if it isn't running yet.

    Ollama's tray app isn't guaranteed to be up after a reboot, and silently
    dropping to the cloud is what made answers take 13s.
    """
    global _ollama_ok
    cancel = _state.PipelineCancellation(cancel_event)
    if cancel.is_set():
        return False
    if _ollama_ok is True:
        return _ollama_ok
    while not cancel.is_set():
        remaining = float("inf") if deadline_at is None else deadline_at - time.perf_counter()
        if remaining <= 0:
            return False
        if _ollama_lock.acquire(timeout=min(0.02, remaining)):
            break
    else:
        return False
    try:
        if cancel.is_set():
            return False
        if _ollama_ok is True:
            return _ollama_ok
        available = _ollama_start_locked(cancel, deadline_at)
        # A timed-out startup must not cache a permanent negative result.
        if available:
            _ollama_ok = True
        return available
    finally:
        _ollama_lock.release()


def _ollama_start_locked(cancel_event=None, deadline_at=None) -> bool:
    cancel = _state.PipelineCancellation(cancel_event)

    def remaining():
        return 1.0 if deadline_at is None else max(0.0, deadline_at - time.perf_counter())

    def probe():
        left = remaining()
        return not cancel.is_set() and left > 0 and _ollama_probe(timeout=min(1.0, left))

    if probe():
        return True
    if cancel.is_set() or remaining() <= 0:
        return False
    try:
        print("[LLM] Ollama не отвечает — запускаю сервер...")
        subprocess.Popen(["ollama", "serve"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for _ in range(20):
            if cancel.wait(min(0.5, remaining())) or remaining() <= 0:
                return False
            if probe():
                print("[LLM] Ollama запущена.")
                return True
    except FileNotFoundError:
        print("[LLM] Ollama не установлена — работаю через облако (медленнее).")
    except Exception as e:
        print(f"[LLM] Не удалось запустить Ollama: {e}")
    return False


@_settings_activity
def warmup_ollama():
    """Load the local model into VRAM and pin it there.

    A cold Ollama call costs ~7.5s (weights load); once resident it is ~0.45s.
    keep_alive=24h stops it from being evicted between commands.
    """
    if LLM_ENGINE != "local" or not _ollama_available():
        return
    try:
        import urllib.request
        body = json.dumps({
            "model": OLLAMA_MODEL,
            "messages": [{"role": "user", "content": "ping"}],
            "stream": False,
            "keep_alive": "24h",
            "options": {"num_predict": 1},
        }).encode()
        req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=120) as response:
            response.read()
        print(f"[LLM] Local model '{OLLAMA_MODEL}' warm ({time.perf_counter()-t0:.1f}s), pinned in VRAM.")
    except Exception as e:
        print(f"[LLM] Ollama warmup failed: {e}")


@_settings_activity
def warmup_lmstudio():
    """Ask LM Studio for one token so the selected normal model is resident."""
    if LLM_ENGINE != "lmstudio":
        return
    try:
        # Explicit opt-in, local server only. Never silently choose another model.
        if LM_STUDIO_AUTOLOAD:
            if urlparse(LM_STUDIO_URL).hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("Автозагрузка LM Studio разрешена только для loopback")
            binary = shutil.which("lms") or str(Path.home() / ".lmstudio" / "bin" / "lms.exe")
            try:
                get_lmstudio_client().chat.completions.create(model=LM_STUDIO_MODEL,
                    messages=[{"role": "user", "content": "ping"}], max_tokens=1, timeout=20)
                return
            except Exception as exc:
                # A timeout does not mean the model is absent. Loading again may
                # allocate a second instance and exhaust the laptop's VRAM.
                missing = str(exc).lower()
                if not any(reason in missing for reason in (
                        "no models loaded", "model is not loaded", "model not loaded",
                        "model_not_found", "model not found")):
                    raise
                completed = subprocess.run([binary, "load", LM_STUDIO_MODEL,
                    "--identifier", LM_STUDIO_MODEL, "--context-length", str(LM_STUDIO_CONTEXT),
                    "--gpu", LM_STUDIO_GPU, "--yes"], capture_output=True, timeout=120,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if completed.returncode:
                    raise RuntimeError("LM Studio не загрузил настроенную модель; проверьте библиотеку моделей")
        t0 = time.perf_counter()
        get_lmstudio_client().chat.completions.create(
            model=LM_STUDIO_MODEL,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            timeout=120,
        )
        print(f"[LLM] LM Studio model '{LM_STUDIO_MODEL}' warm "
              f"({time.perf_counter() - t0:.1f}s).")
    except Exception as e:
        import jarvis_dashboard as dashboard
        dashboard.service("llm", engine="lmstudio", model=LM_STUDIO_MODEL, status="error",
                          detail=_failure_reason("lmstudio", e))
        print(f"[LLM] LM Studio warmup failed: {e}")


def _ollama_deltas(messages: list, max_tokens: int = 150, timeout: float = None,
                   cancel_event=None):
    """Yield token deltas from the local model. Raises on transport failure."""
    import urllib.request
    body = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": True,
        "keep_alive": "24h",
        "options": {"temperature": getattr(messages, 'temperature', 0.3), "num_predict": max_tokens},
    }).encode()
    req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    produced = False
    cancel = _state.PipelineCancellation(cancel_event)
    if cancel.is_set():
        return
    with urllib.request.urlopen(req, timeout=timeout or (LLM_DEADLINE + LLM_GEN_BUDGET)) as r:
        for line in r:
            if cancel.is_set():
                return
            if not line.strip():
                continue
            obj = json.loads(line)
            err = obj.get("error")
            if err:
                jarvis_logger.error(f"[LLM:ollama] error в теле ответа: {err!r}")
                raise RuntimeError(f"ollama error: {err}")
            piece = obj.get("message", {}).get("content", "") or ""
            if piece:
                produced = True
            yield piece
    if not produced:
        jarvis_logger.warning("[LLM:ollama] стрим завершился без контента (0 токенов)")


def _cloud_deltas(messages: list, max_tokens: int = 150, timeout: float = None,
                  model: str = None, cancel_event=None):
    """Yield token deltas from OpenRouter. Raises on transport failure."""
    cancel = _state.PipelineCancellation(cancel_event)
    if cancel.is_set():
        return
    stream = get_openrouter_client().chat.completions.create(
        model=model or OPENROUTER_MODEL,
        messages=messages,
        temperature=getattr(messages, 'temperature', 0.3),
        max_tokens=max_tokens,
        timeout=timeout or (LLM_DEADLINE_CLOUD + LLM_GEN_BUDGET),
        stream=True,
        extra_body={"provider": {"sort": "latency"}},
    )
    try:
        for chunk in stream:
            if cancel.is_set():
                return
            if not getattr(chunk, "choices", None):
                continue
            yield chunk.choices[0].delta.content or ""
    finally:
        stream.close()


def _lmstudio_deltas(messages: list, max_tokens: int = 150, timeout: float = None,
                      model: str = None, cancel_event=None):
    """Yield streamed deltas from the model currently loaded in LM Studio."""
    cancel = _state.PipelineCancellation(cancel_event)
    if cancel.is_set():
        return
    stream = get_lmstudio_client().chat.completions.create(
        model=model or LM_STUDIO_MODEL,
        messages=messages,
        temperature=getattr(messages, 'temperature', 0.3),
        max_tokens=max_tokens,
        timeout=timeout or (LLM_DEADLINE_LM_STUDIO + LLM_GEN_BUDGET),
        stream=True,
    )
    try:
        for chunk in stream:
            if cancel.is_set():
                return
            if not getattr(chunk, "choices", None):
                continue
            yield chunk.choices[0].delta.content or ""
    finally:
        stream.close()


def _pump_engine(engine, messages: list, cancel_event=None) -> queue.Queue:
    """Run `engine` on a worker thread, pushing ("delta"|"end"|"error", payload).

    The engine generators block inside a socket read, so a deadline checked in a
    plain `for delta in engine(...)` loop can only fire once a delta arrives —
    i.e. never, in the one case the deadline exists for: a server that accepted
    the request and then went quiet. Pumping through a queue makes the wait
    interruptible by q.get(timeout=...).

    Cancelling stops queue writes and closes the iterator in its owner thread.
    A blocked arbitrary next()/native call cannot be killed: it must return or
    hit its transport timeout before that cleanup can run. No unbounded drain.
    """
    q: queue.Queue = queue.Queue(maxsize=16)
    q.cancel_event = _state.PipelineCancellation(cancel_event)
    q.done = threading.Event()

    def put(kind, payload):
        while not q.cancel_event.is_set():
            try:
                q.put((kind, payload), timeout=0.02)
                return True
            except queue.Full:
                continue
        return False

    @_settings_activity
    def _worker():
        iterator = None
        error = None
        try:
            if not q.cancel_event.is_set():
                iterator = iter(engine(messages))
                while not q.cancel_event.is_set():
                    try:
                        delta = next(iterator)
                    except StopIteration:
                        break
                    if not put("delta", delta):
                        break
        except BaseException as exc:
            error = exc
        finally:
            try:
                close = getattr(iterator, "close", None)
                if close:
                    close()
            except BaseException as exc:
                error = error or exc
            finally:
                put("error" if error is not None else "end", error)
                q.done.set()

    q.worker = threading.Thread(target=_worker, name="jarvis-llm", daemon=True)
    q.worker.start()
    return q


_ROUTE_CODE = re.compile(r'(?<!\w)('
    r'код|кодинг|запрограммир|программу|программир|функци|скрипт|алгоритм|'
    r'python|питон|джаваскрипт|javascript|java|c\+\+|regex|регуляр|'
    r'напиши класс|отлад|дебаг|баг|ошибк\w* в коде|стек ?трейс|'
    r'компилир|рефактор|sql|запрос к базе|парсер|парсинг'
    r')', re.I | re.U)
_ROUTE_TERMINAL = re.compile(r'(?<!\w)('
    r'терминал|консол|командную строку|powershell|power shell|\bcmd\b|bash|'
    r'выполни команду|запусти команду|в терминале|через терминал|'
    r'pip install|winget|choco|прогони скрипт|выполни в|набери команду'
    r')', re.I | re.U)
_ROUTE_RESEARCH = re.compile(r'(?<!\w)('
    r'найди в интернете|поищи в|загугли|research|ресерч|исследуй|изучи|'
    r'проанализируй|сравни|разбер\w+ подробно|подробно объясни|'
    r'составь список|собери информацию|напиши статью|напиши текст|'
    r'напиши эссе|сочини|пошагов'
    r')', re.I | re.U)


def _classify_complexity(user_text: str) -> tuple[str, list]:
    """('cloud'|'local', reasons). Complex requests select the code route."""
    t = (user_text or "").lower()
    reasons = []
    if _ROUTE_CODE.search(t):      reasons.append("код")
    if _ROUTE_TERMINAL.search(t):  reasons.append("терминал")
    if _ROUTE_RESEARCH.search(t):  reasons.append("ресерч")
    if len(t.split()) >= 18:       reasons.append("длинный")
    return ("cloud", reasons) if reasons else ("local", [])


def _llm_deltas(messages: list, prefer: str = "local", cancel_event=None):
    """Token deltas from the chosen engine, under a first-token deadline.

    `prefer` picks which engine leads: "local" (simple queries — fast qwen) or
    "cloud" (complex code/terminal/research — stronger DeepSeek). The other engine
    stays as a fallback. Each engine carries its own first-token deadline and token
    budget. A local deadline must allow cold weight loading, not only warm TTFT.
    Records full request TTFT, including availability and failed attempts.
    Only a selected local attempt probes Ollama, inside its first-token budget.
    Closing this generator cancels its worker; it never clears global interrupt.
    """

    import jarvis_dashboard as dashboard
    request_started = time.perf_counter()
    _state.last_llm_ttft_ms = 0.0
    cancel = _state.PipelineCancellation(cancel_event)
    local_spec = (_ollama_deltas, LLM_DEADLINE, 150)
    cloud_tokens = 800 if prefer == "cloud" else 150
    cloud_spec = (_cloud_deltas, LLM_DEADLINE_CLOUD, cloud_tokens)
    lmstudio_model = LM_STUDIO_CODE_MODEL if prefer == "cloud" else LM_STUDIO_MODEL
    lmstudio_spec = (lambda m, **kwargs: _lmstudio_deltas(
        m, model=lmstudio_model, **kwargs), LLM_DEADLINE_LM_STUDIO, cloud_tokens)
    free_spec = (lambda m, **kwargs: _cloud_deltas(
        m, model=OPENROUTER_FREE_MODEL, **kwargs),
        LLM_DEADLINE_CLOUD, cloud_tokens)

    have_cloud = bool(OPENROUTER_API_KEY)

    order = []
    if LLM_ENGINE == "lmstudio":
        order.append(("lmstudio", *lmstudio_spec))
        order.append(("local", *local_spec))
        if have_cloud: order.append(("cloud", *cloud_spec))
        if have_cloud and OPENROUTER_FREE_MODEL != OPENROUTER_MODEL:
            order.append(("free", *free_spec))
    elif LLM_ENGINE == "cloud" or prefer == "cloud":
        if have_cloud: order.append(("cloud", *cloud_spec))
        if have_cloud and OPENROUTER_FREE_MODEL != OPENROUTER_MODEL:
            order.append(("free", *free_spec))
        order.append(("local", *local_spec))
    else:
        order.append(("local", *local_spec))
        if have_cloud: order.append(("cloud", *cloud_spec))
        if have_cloud and OPENROUTER_FREE_MODEL != OPENROUTER_MODEL:
            order.append(("free", *free_spec))
    jarvis_logger.info(f"[LLM] маршрут: prefer={prefer} порядок={[o[0] for o in order]}")

    last_err = None
    failures = []
    for name, engine, deadline, max_tokens in order:
        if cancel.is_set():
            return
        model = (LM_STUDIO_CODE_MODEL if name == "lmstudio" and prefer == "cloud" else
                 LM_STUDIO_MODEL if name == "lmstudio" else
                 OLLAMA_MODEL if name == "local" else
                 OPENROUTER_FREE_MODEL if name == "free" else OPENROUTER_MODEL)
        dashboard.service("llm", engine=name, model=model, status="working")
        t0 = time.perf_counter()
        first_deadline = t0 + deadline
        got_first = False
        attempt_cancel = _state.PipelineCancellation(cancel)

        # Bind each attempt's values: a late worker must never use the next
        # engine's name, deadline, or cancellation token through a loop closure.
        def run_engine(m, e=engine, mt=max_tokens, local=name == "local",
                       until=first_deadline, token=attempt_cancel):
            if token.is_set():
                return
            if local and not _ollama_available(cancel_event=token, deadline_at=until):
                if token.is_set():
                    return
                raise RuntimeError("Ollama недоступна в пределах deadline")
            remaining = until - time.perf_counter()
            if token.is_set():
                return
            if remaining <= 0:
                raise TimeoutError("Срок первого токена истёк до начала генерации")
            yield from e(m, max_tokens=mt, timeout=remaining, cancel_event=token)

        q = _pump_engine(run_engine, messages, cancel_event=attempt_cancel)
        try:
            while not cancel.is_set():
                budget = deadline if not got_first else (deadline + LLM_GEN_BUDGET)
                left = budget - (time.perf_counter() - t0)
                if left <= 0:
                    if got_first:
                        return  # Generation budget, never splice in another model.
                    raise TimeoutError(f"{name}: нет первого токена за {deadline}s")
                try:
                    kind, payload = q.get(timeout=min(0.02, left))
                except queue.Empty:
                    continue

                if cancel.is_set():
                    return
                if time.perf_counter() - t0 >= budget:
                    if got_first:
                        return
                    raise TimeoutError(f"{name}: первый токен пришёл после deadline")

                if kind == "error":
                    raise payload
                if kind == "end":
                    break
                if not payload:
                    continue
                if not got_first:
                    dashboard.service("llm", engine=name, model=model, status="ready")
                    _state.last_llm_ttft_ms = (time.perf_counter() - request_started) * 1000.0
                    got_first = True
                    print(f"[LLM] {name}/{model} first token {_state.last_llm_ttft_ms:.0f}ms")
                    jarvis_logger.info(f"[LLM] {name}/{model} первый токен {_state.last_llm_ttft_ms:.0f} мс")
                yield payload

            if cancel.is_set():
                return

            if got_first:
                return
            last_err = RuntimeError(f"{name}: пустой ответ")
            failures.append(_failure_reason(name, last_err))
            dashboard.service("llm", engine=name, model=model, status="error", detail="Пустой ответ")
            _state.llm_empty_failovers += 1
            print(f"[LLM] {name} вернул пустой ответ — пробую следующий движок.")
            jarvis_logger.warning(f"[LLM] {name}/{model} пустой ответ → откат "
                                  f"(всего пустых за сессию: {_state.llm_empty_failovers})")

        except Exception as e:
            reason = _failure_reason(name, e)
            failures.append(reason)
            dashboard.service("llm", engine=name, model=model, status="error", detail=reason)
            last_err = e
            if got_first:
                print(f"[LLM] {name} прервался после начала ответа: {e}")
                jarvis_logger.error(f"[LLM] {name}/{model} оборвался после начала ответа: {e}")
                raise
            print(f"[LLM] {name} не уложился/упал ({e}) — пробую следующий движок.")
            jarvis_logger.warning(f"[LLM] {name}/{model} не уложился/упал ({e}) → следующий движок")
        finally:
            attempt_cancel.set()
            q.cancel_event.set()
            # Do not delay fallback on a native read. Cooperative workers stop
            # on their next poll; native transports have explicit read timeouts.
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
    raise LLMUnavailable(failures or ["Нет доступных движков"])


# спрашиваем модель: сначала локальную, потом облако

"""Движки языковых моделей и маршрутизация между ними.

Простое уходит локальной модели через Ollama, сложное — облачной через
OpenRouter. Ключевое здесь не выбор модели, а бюджет времени: у каждого движка
свой срок на первый токен, и если он не уложился, запрос перехватывает
следующий. Поэтому Джарвис отвечает быстро даже когда локальная модель встала,
а облако тормозит.

Порядок движков задаёт _llm_deltas: при prefer="local" ведёт Ollama, при
prefer="cloud" — облако, третьим идёт бесплатная резервная модель.
"""

import json
import queue
import re
import subprocess
import threading
import time

from openai import OpenAI

import jarvis_state as _state
from jarvis_log import jarvis_logger

__all__ = [
    "OPENROUTER_MODEL", "OPENROUTER_FREE_MODEL", "OPENROUTER_AGENT_MODEL",
    "OPENROUTER_API_KEY", "LLM_ENGINE", "OLLAMA_URL", "OLLAMA_MODEL",
    "LLM_DEADLINE", "LLM_DEADLINE_CLOUD", "LLM_GEN_BUDGET",
    "get_openrouter_client", "warmup_ollama",
    "_ollama_probe", "_ollama_available", "_ollama_deltas", "_cloud_deltas",
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
LLM_DEADLINE = float(os.getenv("JARVIS_LLM_DEADLINE", "1.5"))
LLM_DEADLINE_CLOUD = float(os.getenv("JARVIS_LLM_DEADLINE_CLOUD", "9.0"))
LLM_GEN_BUDGET = float(os.getenv("JARVIS_LLM_GEN_BUDGET", "6.0"))


_openrouter_client = None

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


def _ollama_deltas(messages: list, max_tokens: int = 150, timeout: float = None,
                   cancel_event=None):
    """Yield token deltas from the local model. Raises on transport failure."""
    import urllib.request
    body = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": True,
        "keep_alive": "24h",
        "options": {"temperature": 0.3, "num_predict": max_tokens},
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
        temperature=0.3,
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
    """('cloud'|'local', reasons). Complex → cloud DeepSeek, simple → local qwen."""
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
    budget so a deliberate cloud route isn't killed by the 1.5s local contract.
    Records full request TTFT, including availability and failed attempts.
    Only a selected local attempt probes Ollama, inside its first-token budget.
    Closing this generator cancels its worker; it never clears global interrupt.
    """

    request_started = time.perf_counter()
    _state.last_llm_ttft_ms = 0.0
    cancel = _state.PipelineCancellation(cancel_event)
    local_spec = (_ollama_deltas, LLM_DEADLINE, 150)
    cloud_tokens = 800 if prefer == "cloud" else 150
    cloud_spec = (_cloud_deltas, LLM_DEADLINE_CLOUD, cloud_tokens)
    free_spec = (lambda m, **kwargs: _cloud_deltas(
        m, model=OPENROUTER_FREE_MODEL, **kwargs),
        LLM_DEADLINE_CLOUD, cloud_tokens)

    have_cloud = bool(OPENROUTER_API_KEY)

    order = []
    if LLM_ENGINE == "cloud" or prefer == "cloud":
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
    for name, engine, deadline, max_tokens in order:
        if cancel.is_set():
            return
        model = (OLLAMA_MODEL if name == "local" else
                 OPENROUTER_FREE_MODEL if name == "free" else OPENROUTER_MODEL)
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
            _state.llm_empty_failovers += 1
            print(f"[LLM] {name} вернул пустой ответ — пробую следующий движок.")
            jarvis_logger.warning(f"[LLM] {name}/{model} пустой ответ → откат "
                                  f"(всего пустых за сессию: {_state.llm_empty_failovers})")

        except Exception as e:
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
    raise last_err or RuntimeError("Все LLM-движки недоступны")


# спрашиваем модель: сначала локальную, потом облако

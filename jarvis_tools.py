"""Действия, которые Джарвис умеет выполнять.

Всё, что диспетчер тегов вызывает как готовую операцию: погода, дата, поиск в
интернете, запуск программ, команды в терминале, выполнение кода, скриншот,
громкость, яркость, музыка. Сам диспетчер живёт в ядре — он решает, что
вызвать, а здесь лежит как именно это делается.

Выполнение кода и команд проходит через is_code_safe. Фильтр намеренно узкий:
он не даёт снести систему, репозиторий или корень диска, но не мешает обычной
работе — лучше пропустить мелкую операцию, чем заблокировать нужный скрипт.
"""

import datetime
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

import psutil
import pyperclip
import requests as http_requests

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

try:
    from PIL import ImageGrab
except ImportError:
    ImageGrab = None

import jarvis_platform as _plat
import jarvis_state as _state
from jarvis_apps import extract_open_app_request, resolve_app, resolve_web_target
from jarvis_config import JARVIS_DIR, _pythonw_exe
from jarvis_log import jarvis_logger
from jarvis_safety import _protected_roots, is_code_safe

__all__ = [
    "get_weather", "get_datetime_reply", "extract_web_search_query",
    "get_system_stats", "take_screenshot", "lock_pc", "set_brightness",
    "set_volume", "get_volume", "nudge_volume", "media_control",
    "search_web", "type_text",
    "execute_system_command", "run_shell_command", "play_yandex_music",
    "is_code_safe", "execute_python_code", "_protected_roots",
]


def get_weather(city: str = "Moscow") -> str:
    """Get weather using wttr.in (free, no API key)."""
    try:
        url = f"https://wttr.in/{urllib.parse.quote(city)}?format=3&lang=ru"
        resp = http_requests.get(url, timeout=5)
        if resp.status_code == 200:
            return f"Погода в {city}: {resp.text.strip()}"
        return "Не удалось получить погоду."
    except Exception as e:
        return f"Ошибка погоды: {e}"


_RU_WEEKDAYS = (
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
)
_RU_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
_RU_MONTHS_NOMINATIVE = (
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)


def get_datetime_reply(text: str, now=None) -> str | None:
    """Answer date/time questions locally without asking an LLM."""
    t = re.sub(r'\s+', ' ', (text or '').strip().lower()).strip(' .,!?:;')
    if not t:
        return None
    now = now or datetime.datetime.now()
    weekday = _RU_WEEKDAYS[now.weekday()]
    full_date = f"{now.day} {_RU_MONTHS[now.month - 1]} {now.year} года"

    if re.fullmatch(r'(?:скажи\s+)?(?:который\s+час|сколько\s+времени|время|time)', t):
        return f"Сейчас {now:%H:%M}, сэр."
    if re.fullmatch(r'(?:какой\s+)?(?:сегодня\s+)?день\s+недели', t):
        return f"Сегодня {weekday}, сэр."
    if re.fullmatch(r'(?:какое|какой)\s+(?:сегодня\s+)?число', t):
        return f"Сегодня {now.day} число, сэр."
    if re.fullmatch(r'(?:какая\s+)?(?:сегодняшняя\s+|сегодня\s+)?дата', t):
        return f"Сегодня {full_date}, сэр."
    if re.fullmatch(r'(?:какой\s+)?(?:сейчас\s+)?месяц', t):
        return f"Сейчас {_RU_MONTHS_NOMINATIVE[now.month - 1]}, сэр."
    if re.fullmatch(r'(?:какой\s+)?(?:сейчас\s+)?год', t):
        return f"Сейчас {now.year} год, сэр."
    if re.fullmatch(r'(?:что\s+)?(?:сегодня|за\s+день\s+сегодня|какой\s+сегодня\s+день)', t):
        return f"Сегодня {weekday}, {full_date}. Сейчас {now:%H:%M}, сэр."
    return None


def extract_web_search_query(text: str) -> str | None:
    """Extract an explicit internet search request, or return None."""
    t = re.sub(r'\s+', ' ', (text or '').strip()).strip(' .,!?:;')
    patterns = (
        r'^(?:пожалуйста\s+)?(?:погугли|загугли)\s+(.+)$',
        r'^(?:пожалуйста\s+)?(?:найди|поищи)\s+(?:информацию\s+)?(?:в|по)\s+'
        r'(?:интернете|сети|гугле)\s+(?:информацию\s+)?(?:про|о|об)?\s*(.+)$',
        r'^(?:что|какая\s+информация)\s+(?:есть|известно)\s+(?:в|по)\s+(?:интернете|сети)\s+(?:про|о|об)\s+(.+)$',
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, t, flags=re.IGNORECASE | re.UNICODE)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return None

def get_system_stats() -> str:
    """Get CPU, RAM, disk stats."""
    try:
        cpu = psutil.cpu_percent(interval=0.5)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage('C:\\')
        ram_used = ram.used // (1024**3)
        ram_total = ram.total // (1024**3)
        disk_free = disk.free // (1024**3)
        return (
            f"Процессор: {cpu:.0f}%, "
            f"ОЗУ: {ram_used} из {ram_total} ГБ, "
            f"Диск C: свободно {disk_free} ГБ."
        )
    except Exception as e:
        return f"Ошибка мониторинга: {e}"

def take_screenshot() -> str:
    """Take a screenshot and save to Screenshots folder."""
    try:
        screenshots_dir = Path.home() / "Pictures" / "Jarvis Screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filepath = screenshots_dir / f"screenshot_{ts}.png"

        try:
            import pyautogui as _pag
            _pag.screenshot(str(filepath))
        except Exception:
            if ImageGrab:
                img = ImageGrab.grab()
                img.save(str(filepath))
            else:
                return "Скриншот недоступен: установите pyautogui или Pillow."

        return f"Скриншот сохранён: {filepath.name}"
    except Exception as e:
        return f"Ошибка скриншота: {e}"


def lock_pc() -> str:
    """Lock the Windows workstation."""
    return _plat.lock_workstation()[1]

def set_brightness(level: int) -> str:
    """Set screen brightness (0-100)."""
    return _plat.set_brightness(level)[1]




# похоже ли услышанное на эхо того, что Джарвис только что сказал сам

def set_volume(level: int) -> bool:
    """Set system volume level (0-100)."""
    ok, note = _plat.set_master_volume(level)
    if ok:
        print(f"Volume set to {max(0, min(100, level))}%")
    else:
        print(f"Error setting volume: {note}")
    return ok

def get_volume() -> int:
    """Return current system volume as 0-100 (or -1 on error)."""
    return _plat.get_master_volume()


def nudge_volume(delta: int) -> int:
    """Change volume by delta (percent). Returns the new level (or -1)."""
    cur = get_volume()
    if cur < 0:
        return -1
    new = max(0, min(100, cur + delta))
    return new if set_volume(new) else -1


def media_control(action: str) -> bool:
    """Control media via keyboard emulation."""
    ok, note = _plat.press_media_key(action)
    if not ok:
        print(f"Media control unavailable: {note}")
    return ok

def search_web(query: str) -> str:
    """Search the web and return a short, speakable summary."""
    print(f"Ищу в интернете: {query}")
    try:
        results = []
        last_error = None
        for backend in ("duckduckgo", "startpage"):
            try:
                with DDGS(timeout=3) as ddgs:
                    results = list(ddgs.text(
                        query, max_results=2, region="ru-ru",
                        safesearch="off", backend=backend))
                if results:
                    break
            except Exception as error:
                last_error = error
        if not results and last_error:
            raise last_error
        summaries = []
        seen = set()
        for item in results:
            title = re.sub(r'\s+', ' ', str(item.get("title") or "")).strip()
            body = re.sub(r'\s+', ' ', str(item.get("body") or "")).strip()
            if not body or body.lower() in seen:
                continue
            seen.add(body.lower())
            piece = f"{title}: {body}" if title else body
            summaries.append(piece[:260].rstrip())
            if len(summaries) == 2:
                break
        if summaries:
            answer = " Вот ещё: ".join(summaries)
            return f"Вот что нашёл в интернете, сэр. {answer}"[:620].rstrip()
        return "В интернете по этому запросу ничего не нашлось, сэр."
    except Exception as e:
        print(f"Search error: {e}")
        jarvis_logger.warning(f"[WEB:SEARCH] {query!r}: {e}")
        try:
            os.startfile("https://www.google.com/search?q=" + urllib.parse.quote(query))
            return "Поиск временно не ответил, поэтому я открыл результаты Google, сэр."
        except Exception:
            return "Не удалось связаться с поиском, сэр."

def type_text(text: str) -> bool:
    """Type text into the active window using the clipboard to support Russian."""
    print(f"Печатаю текст: {text}")
    original_clipboard = None
    try:
        original_clipboard = pyperclip.paste()
        pyperclip.copy(text)
        time.sleep(0.1)
        ok, note = _plat.paste_from_clipboard()
        time.sleep(0.1)
        if not ok:
            print(f"Ghost Writer unavailable: {note}")
        return ok
    except Exception as e:
        print(f"Ghost Writer error: {e}")
        return False
    finally:
        if original_clipboard is not None:
            try:
                pyperclip.copy(original_clipboard)
            except Exception as error:
                jarvis_logger.warning("[TYPE] clipboard restore failed: %s", error)

def execute_system_command(cmd: str) -> bool:
    """Open a known target or resolve any installed Windows application."""
    cmd = cmd.lower().strip()

    web_target = resolve_web_target(cmd)
    if web_target:
        try:
            os.startfile(web_target)
            jarvis_logger.info(f"[WEB] {cmd!r} → {web_target}")
            return True
        except Exception as e:
            jarvis_logger.warning(f"[WEB] launch failed {web_target!r}: {e}")
            return False
    
    app_paths = {
        "browser": "http://google.com",
        "claude": "https://claude.ai",
        "telegram": os.path.expandvars(r"%APPDATA%\Telegram Desktop\Telegram.exe"),
        "discord": os.path.expandvars(r"%LOCALAPPDATA%\Discord\Update.exe"),
        "vscode": os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
        "obsidian": os.path.expandvars(r"%LOCALAPPDATA%\Programs\Obsidian\Obsidian.exe"),
        "calc": "calc.exe",
        "notepad": "notepad.exe"
    }
    
    if cmd in app_paths:
        path = app_paths[cmd]
        try:
            if cmd == "discord":
                subprocess.Popen([path, "--processStart", "Discord.exe"])
            elif path.startswith("http"):
                os.startfile(path)
            else:
                subprocess.Popen(path)
            return True
        except Exception as e:
            print(f"[execute_system_command] Error opening {cmd}: {e}")
            try:
                os.startfile(path)
                return True
            except Exception as e2:
                print(f"[execute_system_command] Fallback also failed: {e2}")
                return False
    else:
        resolved = resolve_app(cmd)
        if resolved:
            try:
                os.startfile(resolved["target"])
                jarvis_logger.info(f"[APPS] {cmd!r} → {resolved['name']!r} "
                                   f"score={resolved['score']:.2f}")
                return True
            except Exception as e:
                jarvis_logger.warning(f"[APPS] launch failed {resolved['target']!r}: {e}")
        try:
            _is_url = (
                cmd.startswith(("http://", "https://")) or (
                    " " not in cmd and
                    not cmd.startswith(("/", "\\")) and
                    re.match(r'^[a-z0-9][-a-z0-9]*(\.[a-z]{2,})+(/\S*)?$', cmd)
                )
            )
            if _is_url:
                target = cmd if cmd.startswith("http") else "https://" + cmd
                os.startfile(target)
                jarvis_logger.info(f"[WEB] URL opened: {target}")
                return True

            expanded = os.path.expandvars(cmd)
            if Path(expanded).exists():
                os.startfile(expanded)
                jarvis_logger.info(f"[APPS] explicit path opened: {expanded!r}")
                return True

            executable = shutil.which(cmd)
            if executable is None and " " not in cmd and not cmd.endswith(".exe"):
                executable = shutil.which(cmd + ".exe")
            if executable:
                os.startfile(executable)
                jarvis_logger.info(f"[APPS] PATH executable opened: {executable!r}")
                return True
        except Exception as e:
            print(f"[execute_system_command] could not launch {cmd!r}: {e}")
            return False
        jarvis_logger.warning(f"[APPS] target not found: {cmd!r}")
        return False


# выполняем команду в PowerShell
def run_shell_command(cmd: str) -> str:
    """Execute a command in PowerShell and return a short spoken summary.

    Full stdout/stderr goes to the log; only a truncated head is spoken so a
    500-line directory listing doesn't get read aloud. Runs with no visible
    console window. Timeout 60s so a hung command can't wedge the assistant.
    """
    cmd = (cmd or "").strip()
    if not cmd:
        return "Пустая команда, сэр."

    _open_m = re.match(r'^open\s+(.+)$', cmd, re.IGNORECASE)
    if _open_m:
        _target = _open_m.group(1).strip().strip('"\'')
        if execute_system_command(_target):
            jarvis_logger.info(f"[CMD→OPEN] перехвачен 'open': {_target!r}")
            return "Открываю, сэр."
        jarvis_logger.warning(f"[CMD→OPEN] цель не найдена: {_target!r}")
        return f"Не нашёл, что открыть по запросу {_target}, сэр."

    placeholder = re.sub(r'[\s<>\[\]{}]+', ' ', cmd.lower()).strip(' .,:;')
    if placeholder in {"команда", "ваша команда", "powershell команда", "cmd команда"}:
        jarvis_logger.warning(f"[CMD] отклонён шаблон вместо реальной команды: {cmd!r}")
        return "Не получил конкретную команду для терминала, сэр."
    ok, reason = is_code_safe(cmd)
    if not ok:
        print(f"[ANTI-WIPE] blocked cmd: {reason}")
        jarvis_logger.warning(f"[ANTI-WIPE] заблокирована команда: {reason} :: {cmd[:120]!r}")
        return "Не могу трогать систему или удалять проекты, сэр."
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + cmd],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        jarvis_logger.info(f"[CMD] {cmd!r} rc={proc.returncode} "
                           f"out={out[:800]!r} err={err[:400]!r}")
        if proc.returncode == 0:
            if not out:
                return "Готово, сэр."
            return "Готово, сэр. " + (out if len(out) <= 300 else out[:300] + "…")
        first_err = (err or out).splitlines()[0].strip() if (err or out) else ""
        if len(first_err) > 90:
            first_err = first_err[:90] + "…"
        return ("Команда завершилась с ошибкой, сэр." +
                (f" {first_err}" if first_err else ""))
    except subprocess.TimeoutExpired:
        jarvis_logger.error(f"[CMD] {cmd!r} timeout 60s")
        return "Команда выполнялась слишком долго, сэр, я её прервал."
    except Exception as e:
        jarvis_logger.error(f"[CMD] {cmd!r} exception: {e}")
        return f"Не удалось выполнить команду, сэр. {e}"


def play_yandex_music(query: str, auto_play: bool = True):
    query = query.strip()
    if not query:
        url = "https://music.yandex.ru/"
    elif query.lower() in ["волна", "мою волну", "музыку", "моя волна"]:
        url = "https://music.yandex.ru/radio"
    else:
        safe_query = urllib.parse.quote(query)
        url = f"https://music.yandex.ru/search?text={safe_query}"

    print(f"Открываю музыку: {url}")
    try:
        os.startfile(url)
        jarvis_logger.info(f"[MUSIC] opened {url}")
    except Exception as e:
        jarvis_logger.error(f"[MUSIC] open failed: {e}")
        return "Не удалось открыть Яндекс Музыку, сэр."

    if auto_play:
        def _auto_play():
            try:
                time.sleep(6)
                _plat.press_media_key("playpause")
                jarvis_logger.info("[MUSIC] sent global play/pause media key")
            except Exception as e:
                jarvis_logger.warning(f"[MUSIC] autoplay unavailable: {e}")
        threading.Thread(target=_auto_play, daemon=True).start()
        return "Открываю музыку, сэр. Если трек не запустится, нажмите воспроизведение вручную."
    return "Открываю Яндекс Музыку, сэр."

def _stop_python_process(proc) -> None:
    """Stop only this command's process tree; never target unrelated Python."""
    try:
        children = psutil.Process(proc.pid).children(recursive=True)
    except psutil.Error:
        children = []
    for child in reversed(children):
        try:
            child.kill()
        except psutil.Error:
            pass
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=3)


def execute_python_code(code: str, timeout: float = 60, cancel_event=None) -> str:
    """Return the actual outcome of bounded, cancellable user code execution.

    A child process prevents a failed/infinite script from wedging the assistant.
    It is NOT a sandbox: ordinary user code retains the existing anti-wipe policy.
    """
    ok, reason = is_code_safe(code)
    if not ok:
        print(f"[ANTI-WIPE] blocked python: {reason}")
        jarvis_logger.warning(f"[ANTI-WIPE] заблокирован Python: {reason} :: {(code or '')[:120]!r}")
        return "Не могу трогать систему или удалять проекты, сэр."

    cancel = cancel_event if cancel_event is not None else _state.interrupt_event
    if cancel.is_set():
        return "Выполнение прервано, сэр."
    prelude = (
        "import os, subprocess, time, sys\n"
        "try:\n    import pyautogui\n"
        "except Exception:\n    pyautogui = None\n"
        "exec(compile(sys.stdin.read(), '<jarvis-command>', 'exec'))\n"
    )
    proc = None
    try:
        # A pipe write could block before the child finishes importing modules,
        # bypassing our timeout for code larger than the pipe buffer.
        with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as output:
            source.write((code or "").encode("utf-8"))
            source.seek(0)
            proc = subprocess.Popen(
                [sys.executable, "-X", "utf8", "-c", prelude],
                cwd=JARVIS_DIR, stdin=source, stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            deadline = time.monotonic() + timeout
            while proc.poll() is None:
                if cancel.is_set():
                    _stop_python_process(proc)
                    return "Выполнение прервано, сэр."
                if time.monotonic() >= deadline:
                    _stop_python_process(proc)
                    return "Python выполнялся слишком долго, сэр; процесс остановлен."
                cancel.wait(min(0.05, max(0, deadline - time.monotonic())))
            # Read a bounded tail, including the final exception message.
            size = output.seek(0, os.SEEK_END)
            output.seek(max(0, size - 4096))
            tail = output.read(4096).decode("utf-8", errors="replace").strip()
            if proc.returncode:
                detail = tail.splitlines()[-1] if tail else f"код {proc.returncode}"
                return f"Ошибка при выполнении Python: {detail[:300]}"
            return "Команда выполнена, сэр." + (f" {tail[:300]}" if tail else "")
    except Exception as e:
        jarvis_logger.error(f"[EXECUTE_PYTHON] ошибка: {e}")
        return f"Ошибка при выполнении: {e}"
    finally:
        if proc is not None:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
            if proc.poll() is None:
                _stop_python_process(proc)

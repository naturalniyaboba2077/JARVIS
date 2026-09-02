"""Окно Jarvis и полоски-визуализация голоса.

Две вещи, которых на домашнем сервере не будет вовсе: нативное окно на
pywebview и оверлей — отдельный процесс, рисующий по краю экрана полоски под
громкость речи. Поэтому они и вынесены отдельно от ядра.

Всё здесь молча ничего не делает, когда окна нет: ui_call при _ui_window=None
просто выходит, а оверлей не запускается при JARVIS_OVERLAY=off. Ядру не нужно
знать, есть ли перед ним экран.

Мост между окном и ядром (класс JarvisApi) живёт в jarvis.py: он дёргает почти
все подсистемы, и держать его здесь означало бы круговые импорты.
"""

import ctypes
import json
import os
import subprocess
import threading

from jarvis_config import JARVIS_DIR, _pythonw_exe
from jarvis_log import jarvis_logger

__all__ = [
    "UI_ENABLED", "UI_HTML", "OVERLAY_ENABLED",
    "ui_call", "ui_state", "ui_sub", "ui_msg", "ui_lat", "ui_clear_lat",
    "start_overlay", "stop_overlay",
    "_overlay_send", "_main_window_minimized",
    "_find_jarvis_hwnd", "_set_native_window_state",
]


UI_ENABLED = os.getenv("JARVIS_UI", "on").lower() == "on"
UI_HTML = str((JARVIS_DIR / "ui" / "index.html").resolve())
_ui_window = None
_ui_last_state = None


def _find_jarvis_hwnd():
    """Find the native pywebview HWND by its exact title."""
    if os.name != "nt":
        return None
    matches = []
    user32 = ctypes.windll.user32
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value == "J.A.R.V.I.S.":
            matches.append(hwnd)
        return True

    user32.EnumWindows(enum_proc(_visit), 0)
    return matches[0] if matches else None


def _set_native_window_state(action: str) -> bool:
    """Maximize/restore/minimize without pywebview's fragile frameless path."""
    commands = {"maximize": 3, "minimize": 6, "restore": 9}
    hwnd = _find_jarvis_hwnd()
    if hwnd is None or action not in commands:
        jarvis_logger.warning(f"[UI] HWND не найден для {action}")
        return False
    try:
        ctypes.windll.user32.ShowWindowAsync(hwnd, commands[action])
        jarvis_logger.info(f"[UI] native {action} hwnd={int(hwnd)}")
        return True
    except Exception as e:
        jarvis_logger.error(f"[UI] native {action} failed: {e}")
        return False


def ui_call(js: str):
    w = _ui_window
    if w is None:
        return
    try:
        w.evaluate_js(js)
    except Exception:
        pass


def ui_state(s: str):
    """Push a state (idle/listening/thinking/speaking) to the UI orb."""
    global _ui_last_state
    if s == _ui_last_state:
        return
    _ui_last_state = s
    ui_call(f"window.jvSetState && jvSetState({json.dumps(s)})")


def ui_sub(text: str):
    """Set just the small line under the orb (the phase caption)."""
    ui_call(f"window.jvSetSub && jvSetSub({json.dumps(text, ensure_ascii=False)})")


def ui_msg(who: str, text: str):
    if not text:
        return
    ui_call(f"window.jvAddMsg && jvAddMsg({json.dumps(who)},{json.dumps(text, ensure_ascii=False)})")


def ui_lat(stage: str, seconds: float):
    ui_call(f"window.jvLatency && jvLatency({json.dumps(stage)},{json.dumps(f'{seconds:.2f}с', ensure_ascii=False)})")


def ui_clear_lat():
    ui_call("window.jvClearLat && jvClearLat()")


OVERLAY_ENABLED = os.getenv("JARVIS_OVERLAY", "on").lower() == "on"
_overlay_proc = None
_overlay_lock = threading.Lock()


def start_overlay():
    """Launch the overlay process. It idles invisibly until we send it amplitude."""
    global _overlay_proc
    if not OVERLAY_ENABLED or _overlay_proc is not None:
        return
    script = JARVIS_DIR / "overlay.py"
    if not script.exists():
        return
    try:
        _overlay_proc = subprocess.Popen(
            [_pythonw_exe(), str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        print("[Overlay] Визуализатор голоса запущен.")
    except Exception as e:
        print(f"[Overlay] Не удалось запустить: {e}")
        _overlay_proc = None


def _overlay_send(**msg):
    """Send one JSON line to the overlay; drop it if the process is gone."""
    global _overlay_proc
    p = _overlay_proc
    if p is None or p.poll() is not None or p.stdin is None:
        return
    try:
        with _overlay_lock:
            p.stdin.write((json.dumps(msg) + "\n").encode())
            p.stdin.flush()
    except Exception:
        _overlay_proc = None


def stop_overlay():
    global _overlay_proc
    _overlay_send(quit=True)
    p = _overlay_proc
    _overlay_proc = None
    if p is not None:
        try:
            p.wait(timeout=2)
        except Exception:
            p.kill()


def _main_window_minimized() -> bool:
    """True when the J.A.R.V.I.S. window is minimised (or hidden behind nothing).

    The overlay only makes sense when the window isn't on screen; when it is,
    the orb already shows Jarvis speaking.
    """
    if _ui_window is None:
        return True
    try:
        hwnd = ctypes.windll.user32.FindWindowW(None, "J.A.R.V.I.S.")
        if not hwnd:
            return False
        return bool(ctypes.windll.user32.IsIconic(hwnd))
    except Exception:
        return False

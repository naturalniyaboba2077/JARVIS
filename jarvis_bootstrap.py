"""Show the desktop before importing audio, model clients and integrations.

Only the explicit JarvisApi bridge is exposed. No commands are queued/replayed
while starting. Native window events, not a guessed delay, release the loader.
"""

import importlib
import os
import threading
import time


class StartupApi:
    def __init__(self, started_at=None, loader=None):
        self._started_at = time.perf_counter() if started_at is None else started_at
        self._loader = loader or (lambda: importlib.import_module("jarvis"))
        self._lock = threading.RLock()
        self._window = None
        self._core = None
        self._api = None
        self._worker = None
        self._closed = False
        self._error = False
        self._marks = {}
        self._message = "Открываю интерфейс"
        self._log_label = "BOOT"

    def _mark(self, stage):
        from jarvis_log import jarvis_logger
        with self._lock:
            if stage in self._marks:
                return
            seconds = time.perf_counter() - self._started_at
            self._marks[stage] = round(seconds, 3)
        jarvis_logger.info("[%s] pid=%s %s=%.3fs", self._log_label, os.getpid(), stage, seconds)

    def _shown(self):
        self._mark("window_shown")

    def _loaded(self):
        self._mark("interface_loaded")
        with self._lock:
            if self._closed or self._worker is not None:
                return
            self._message = "Подключаю голос и инструменты · можно набрать команду"
            self._worker = threading.Thread(target=self._run_backend,
                                            name="jarvis-startup", daemon=True)
            self._worker.start()

    def _run_backend(self):
        try:
            with self._lock:
                if self._closed:
                    return
            core = self._loader()
            self._mark("core_imported")
            with self._lock:
                if self._closed:
                    return
                self._core = core
                core._ui._ui_window = self._window
                core._startup_mark = self._mark
                self._api = core.JarvisApi()
                self._message = "Настраиваю микрофон · можно набрать команду"
            # run_assistant checks the stop event as well: closing during this
            # handoff must not start another listener or resurrect the app.
            core.run_assistant()
            with self._lock:
                if not self._closed:
                    self._error = True
                    self._message = "Помощник остановлен. Перезапустите приложение."
        except Exception:
            from jarvis_log import jarvis_logger
            jarvis_logger.exception("[BOOT] backend initialization failed")
            with self._lock:
                self._error = True
                self._message = "Не удалось запустить помощника. Подробности в logs."
                if self._core is not None:
                    self._core._state.assistant_ready = False
                    self._core._stop_event.set()
                    self._core._state.interrupt_event.set()

    def _closed_event(self):
        with self._lock:
            self._closed = True
            if self._core is not None:
                self._core._stop_event.set()
                self._core._state.interrupt_event.set()

    def _window_event(self, name):
        def handler(*args):
            from jarvis_log import jarvis_logger
            jarvis_logger.info("[UI] event=%s args=%r", name, args)
            if name == "closed":
                self._closed_event()
        return handler

    def _invoke(self, method, *args):
        with self._lock:
            if (self._closed or self._error or self._api is None
                    or not self._core._state.assistant_ready):
                raise RuntimeError("Джарвис ещё не готов. Команда не выполнена; дождитесь запуска.")
            target = self._api
        return getattr(target, method)(*args)

    def runtime_status(self):
        with self._lock:
            if self._api is not None:
                result = self._api.runtime_status()
            else:
                result = {"ready": False, "state": "idle", "pending": None,
                          "timers": [], "activities": [], "services": {},
                          "configured": {"llm": "запуск"},
                          "microphone": {"enabled": True, "ready": False, "error": ""}}
            result["ready"] = bool(result["ready"] and not self._closed and not self._error)
            result["startup"] = {"message": self._message, "error": self._error,
                                 "elapsed_seconds": round(time.perf_counter() - self._started_at, 1),
                                 "timings": dict(self._marks)}
            return result

    def audio_levels(self):
        with self._lock:
            if self._api is not None and not self._closed:
                return self._api.audio_levels()
        return {"input": 0, "output": 0, "output_available": False}

    # Explicit signatures are required by pywebview's bridge introspection.
    # Do not expose the core, loader, window, or a generic RPC dispatcher.
    def send_command(self, text, project_request_id=None):
        if project_request_id is None:
            return self._invoke("send_command", text)
        return self._invoke("send_command", text, project_request_id)

    def set_microphone_enabled(self, enabled):
        return self._invoke("set_microphone_enabled", enabled)

    def listen_once(self):
        return self._invoke("listen_once")

    def test_voice(self):
        return self._invoke("test_voice")

    def stop(self):
        return self._invoke("stop")

    def confirm_send(self, kind, request_id, approved):
        return self._invoke("confirm_send", kind, request_id, approved)

    def confirm_project(self, request_id, choice_id, approved):
        return self._invoke("confirm_project", request_id, choice_id, approved)

    def cancel_timer(self, timer_id):
        return self._invoke("cancel_timer", timer_id)

    def preview_file(self, card_id):
        return self._invoke("preview_file", card_id)

    def preview_change(self, card_id):
        return self._invoke("preview_change", card_id)

    def undo_change(self, card_id):
        return self._invoke("undo_change", card_id)

    def set_compact_mode(self, enabled):
        return self._invoke("set_compact_mode", enabled)

    def get_settings(self):
        return self._invoke("get_settings")

    def save_settings(self, settings):
        return self._invoke("save_settings", settings)

    def diagnostics(self):
        return self._invoke("diagnostics")

    def telegram_status(self):
        return self._invoke("telegram_status")

    def telegram_send_code(self):
        return self._invoke("telegram_send_code")

    def telegram_sign_in(self, code="", password=""):
        return self._invoke("telegram_sign_in", code, password)

    def list_microphones(self):
        return self._invoke("list_microphones")

    def minimize(self):
        if self._api is not None:
            return self._api.minimize()
        if self._window is not None:
            self._window.minimize()
        return True

    def maximize(self):
        if self._api is not None:
            return self._api.maximize()
        if self._window is not None:
            self._window.maximize()
        return True

    def restore(self):
        if self._api is not None:
            return self._api.restore()
        if self._window is not None:
            self._window.restore()
        return True

    def close(self):
        self._closed_event()
        if self._api is not None:
            return self._api.close()
        if self._window is not None:
            self._window.destroy()
        return True


def open_window(webview, bridge):
    from jarvis_ui import UI_HTML
    window = webview.create_window(
        "J.A.R.V.I.S.", url=UI_HTML, js_api=bridge,
        width=1040, height=740, min_size=(420, 300),
        background_color="#101316", frameless=True, easy_drag=False)
    bridge._window = window
    window.events.shown += bridge._shown
    window.events.loaded += bridge._loaded
    window.events.closed += bridge._window_event("closed")
    for name in ("closing", "minimized", "maximized", "restored"):
        event = getattr(window.events, name)
        event += bridge._window_event(name)
    try:
        # start(func) would run the heavy imports BEFORE native window creation.
        webview.start()
    finally:
        bridge._closed_event()


def main(started_at=None):
    from jarvis_ui import UI_ENABLED, UI_HTML
    from pathlib import Path
    if not UI_ENABLED or not Path(UI_HTML).is_file():
        importlib.import_module("jarvis").run_assistant()
        return
    try:
        import webview
    except ImportError:
        importlib.import_module("jarvis").run_assistant()
        return
    open_window(webview, StartupApi(started_at))

"""Live configuration at safe operation boundaries, without reloading modules.

Disk persistence and runtime activation are separate, versioned operations.
No timers, commands, confirmations, history or interrupt generations are reset.
Only the command-loop owner applies changes (including PortAudio reconnects).
"""

from contextlib import contextmanager
from functools import wraps
import inspect
import os
from pathlib import Path
import sys
import threading
import time

import jarvis_config as config


class ApplyError(RuntimeError):
    """A public, secret-free explanation of an activation failure."""


class Settings:
    def __init__(self):
        self._condition = threading.Condition(threading.RLock())
        self._save_lock = threading.Lock()
        self._active = 0
        self._applying = False
        self._pending = {}
        self._revision = 0
        self._applied_revision = 0
        self._state = "idle"
        self._message = "Настройки применяются без перезапуска."
        self._overridden = []
        self.enabled = False  # Only a running assistant uses the active env snapshot.

    @contextmanager
    def operation(self, *, drop_during_apply=False):
        with self._condition:
            if drop_during_apply and self._applying:
                admitted = False
            else:
                self._condition.wait_for(lambda: not self._applying)
                self._active += 1
                admitted = True
        try:
            yield admitted
        finally:
            if admitted:
                with self._condition:
                    self._active -= 1
                    self._condition.notify_all()

    def snapshot(self):
        with self._condition:
            return {"state": self._state, "message": self._message,
                    "revision": self._revision, "applied_revision": self._applied_revision,
                    "overridden_keys": list(self._overridden)}

    def save(self, updates):
        # Serialize save+publication so an older API worker cannot win last.
        with self._save_lock, config._CONFIG_LOCK:
            ok, message = config._write_config_file(updates)
            if not ok:
                return {"ok": False, "message": message}
            saved = config._read_config_file()
            keys = set(updates) & config.WRITABLE_SETTING_KEYS
            keys -= {k for k in config.SECRET_SETTING_KEYS if not updates.get(k)}
            overridden = sorted(keys & config.ENV_OVERRIDES.keys())
            values = {k: saved[k] for k in keys - set(overridden) if k in saved}
            with self._condition:
                self._revision += 1
                self._pending.update(values)
                self._overridden = overridden
                if self._pending or self._applying:
                    self._state = "pending"
                    self._message = "Сохранено. Применю после текущего ответа или загрузки движка."
                else:
                    self._state = "applied"
                    self._applied_revision = self._revision
                    self._message = "Настройки применены."
                if overridden:
                    self._message += " Заданы окружением и не изменены: " + ", ".join(overridden) + "."
                return {"ok": True, **self.snapshot()}

    def try_apply(self, apply, pending_confirmation=None):
        with self._condition:
            if not self._pending or self._applying or self._state == "error" or self._active:
                return False
            if pending_confirmation and any(k.startswith("TELEGRAM_") for k in self._pending):
                confirmation = pending_confirmation()
                if confirmation and confirmation.get("kind") == "telegram":
                    self._message = "Сохранено. Сначала подтвердите или отмените ожидающее сообщение Telegram."
                    return False
            values, revision = dict(self._pending), self._revision
            self._pending.clear()
            self._applying = True
            self._state = "applying"
            self._message = ("Переподключаю микрофон · дожидаюсь окончания текущей записи…"
                             if values.keys() & {"JARVIS_MIC_INDEX", "JARVIS_PHRASE_TIME_LIMIT"}
                             else "Применяю настройки…")
        try:
            apply(values)
        except Exception as error:
            from jarvis_log import jarvis_logger
            # Do not log values, credentials or arbitrary exception messages.
            jarvis_logger.error("[SETTINGS] revision=%s failed type=%s", revision, type(error).__name__)
            with self._condition:
                self._pending = {**values, **self._pending}
                self._state = "pending" if self._revision > revision else "error"
                detail = str(error) if isinstance(error, ApplyError) else "Не удалось обновить подсистемы."
                self._message = ("Сохранено, но не применено. " + detail
                                 + " Исправьте настройки и нажмите «Применить» повторно.")
            return False
        else:
            from jarvis_log import jarvis_logger
            jarvis_logger.info("[SETTINGS] revision=%s applied keys=%s", revision, ",".join(sorted(values)))
            with self._condition:
                self._applied_revision = revision
                self._state = "pending" if self._pending else "applied"
                self._message = ("Сохранено. Ожидает применения следующая правка." if self._pending else
                                 "Настройки применены без перезапуска. Новые модели загружаются при обращении.")
                if values.keys() & {"LM_STUDIO_GPU", "LM_STUDIO_CONTEXT"}:
                    self._message += " GPU и контекст LM Studio используются при следующей загрузке модели; уже загруженная модель не выгружается."
                if self._overridden:
                    self._message += " Заданы окружением: " + ", ".join(self._overridden) + "."
            return True
        finally:
            with self._condition:
                self._applying = False
                self._condition.notify_all()


settings = Settings()


def activity(func=None, *, drop_during_apply=False):
    """Keep resources stable until the real worker/iterator finishes, not timeout."""
    def decorate(fn):
        if inspect.isgeneratorfunction(fn):
            @wraps(fn)
            def generator(*args, **kwargs):
                with settings.operation(drop_during_apply=drop_during_apply) as admitted:
                    if admitted:
                        yield from fn(*args, **kwargs)
            return generator
        @wraps(fn)
        def wrapper(*args, **kwargs):
            with settings.operation(drop_during_apply=drop_during_apply) as admitted:
                if admitted:
                    return fn(*args, **kwargs)
        return wrapper
    return decorate(func) if func is not None else decorate


# Explicit owner mapping: don't reload modules or overwrite user/runtime state.
TEXT_BINDINGS = {
    "jarvis_llm": {
        "JARVIS_LLM": "LLM_ENGINE", "OLLAMA_MODEL": "OLLAMA_MODEL",
        "LM_STUDIO_URL": "LM_STUDIO_URL", "LM_STUDIO_MODEL": "LM_STUDIO_MODEL",
        "LM_STUDIO_CODE_MODEL": "LM_STUDIO_CODE_MODEL", "OPENROUTER_MODEL": "OPENROUTER_MODEL",
        "OPENROUTER_FREE_MODEL": "OPENROUTER_FREE_MODEL", "OPENROUTER_AGENT_MODEL": "OPENROUTER_AGENT_MODEL",
        "OPENROUTER_API_KEY": "OPENROUTER_API_KEY", "LM_STUDIO_GPU": "LM_STUDIO_GPU"},
    "jarvis_tts": {k: k for k in ("TTS_ENGINE", "PIPER_VOICE", "EDGE_VOICE", "EDGE_RATE", "EDGE_PITCH", "XTTS_LANGUAGE")},
    "jarvis_stt": {"STT_ENGINE": "STT_ENGINE", "WHISPER_MODEL": "WHISPER_MODEL_SIZE"},
    "jarvis_config": {"JARVIS_FOLLOWUP_MODE": "FOLLOWUP_MODE"},
}
TEXT_BINDINGS["jarvis_tts"]["JARVIS_VOICE_STYLE"] = "VOICE_STYLE"
FLOAT_BINDINGS = {
    "jarvis_llm": {"JARVIS_LLM_DEADLINE": "LLM_DEADLINE", "JARVIS_LLM_DEADLINE_CLOUD": "LLM_DEADLINE_CLOUD",
                   "JARVIS_LLM_DEADLINE_LM_STUDIO": "LLM_DEADLINE_LM_STUDIO", "JARVIS_LLM_GEN_BUDGET": "LLM_GEN_BUDGET"},
    "jarvis_tts": {k: k for k in ("PIPER_LENGTH_SCALE", "PIPER_NOISE_SCALE", "PIPER_NOISE_W_SCALE", "XTTS_SPEED")},
    "jarvis_config": {"JARVIS_PAUSE_THRESHOLD": "PAUSE_THRESHOLD", "JARVIS_WAKE_COMMAND_WINDOW": "WAKE_COMMAND_WINDOW",
                      "JARVIS_PHRASE_TIME_LIMIT": "PHRASE_TIME_LIMIT", "JARVIS_FOLLOWUP_WINDOW": "FOLLOWUP_WINDOW",
                      "JARVIS_SPEAK_COOLDOWN": "SPEAK_COOLDOWN"},
}


def apply_runtime(core, updates, reconfigure_audio=None):
    """Called by the main loop with zero active operations; never by an API worker."""
    import jarvis_llm as llm
    import jarvis_tts as tts
    import jarvis_stt as stt
    import jarvis_ui as ui
    values = {k: str(v) for k, v in updates.items()
              if k not in config.ENV_OVERRIDES and os.getenv(k) != str(v)}
    if not values:
        return
    assignments = []
    for groups, convert in ((TEXT_BINDINGS, str), (FLOAT_BINDINGS, float)):
        for module_name, fields in groups.items():
            module = sys.modules[module_name]
            for key, attr in fields.items():
                if key not in values:
                    continue
                value = convert(values[key])
                if key == "LM_STUDIO_URL":
                    value = value.rstrip("/")
                if key == "JARVIS_PAUSE_THRESHOLD" and os.getenv("JARVIS_FAST_VAD", "off").lower() in {"on", "1", "true", "yes"}:
                    value = min(value, 1.35)
                assignments.append((module, attr, value))
                if hasattr(core, attr):
                    assignments.append((core, attr, value))
                if module is config and hasattr(tts, attr):
                    assignments.append((tts, attr, value))
    if "LM_STUDIO_CONTEXT" in values:
        assignments.append((llm, "LM_STUDIO_CONTEXT", int(values["LM_STUDIO_CONTEXT"])))
    if "LM_STUDIO_AUTOLOAD" in values:
        assignments.append((llm, "LM_STUDIO_AUTOLOAD", values["LM_STUDIO_AUTOLOAD"] == "on"))
    if "SESSION_MEMORY" in values:
        assignments.append((core, "SESSION_MEMORY", values["SESSION_MEMORY"] == "on"))
    if "JARVIS_OVERLAY" in values:
        for module in (ui, tts, core):
            assignments.append((module, "OVERLAY_ENABLED", values["JARVIS_OVERLAY"] == "on"))
    if "PIPER_VOICE" in values:
        path = Path(os.getenv("PIPER_MODEL", str(config.JARVIS_DIR / "piper_models" / f"ru_RU-{values['PIPER_VOICE']}-medium.onnx")))
        for module in (tts, core):
            assignments.append((module, "PIPER_MODEL_PATH", path))

    # The only fallible hardware step runs BEFORE environment/global publication.
    audio_keys = {"JARVIS_MIC_INDEX", "JARVIS_PHRASE_TIME_LIMIT", "JARVIS_PAUSE_THRESHOLD"}
    if values.keys() & audio_keys:
        if reconfigure_audio is None:
            raise ApplyError("Управление микрофоном ещё не готово.")
        reconfigure_audio(values)

    for module, attr, value in assignments:
        setattr(module, attr, value)
    os.environ.update(values)
    if "OPENROUTER_API_KEY" in values:
        _close_client(llm, "_openrouter_client")
    if "LM_STUDIO_URL" in values:
        _close_client(llm, "_lmstudio_client")
    if "OLLAMA_MODEL" in values:
        llm._ollama_ok = None
    voice_keys = set(TEXT_BINDINGS["jarvis_tts"]) | set(FLOAT_BINDINGS["jarvis_tts"])
    if values.keys() & voice_keys:
        tts._TTS_INSTANT_CACHE.clear()
        try:
            tts._index_existing_tts_cache()
        except OSError:
            pass  # A missing/unreadable optional cache must not prevent applying.
        if values.keys() & {"PIPER_VOICE", "TTS_ENGINE"}:
            tts._piper_voice, tts._piper_tried = None, False
        if "TTS_ENGINE" in values and tts._effective_tts_engine() != "xtts":
            tts.tts = tts._xtts_conditioning = tts._xtts_reference_key = None
        # Cache/model work is optional and lazy; applying settings does no synthesis.
        core._dashboard.service("tts", engine=tts._effective_tts_engine(), status="unknown", detail="Новые настройки; голос ещё не проверен")
    if values.keys() & {"STT_ENGINE", "WHISPER_MODEL"}:
        stt._whisper_model, stt._whisper_tried = None, False
        core._dashboard.service("stt", engine=stt.STT_ENGINE, model=stt.WHISPER_MODEL_SIZE, status="unknown", detail="Новые настройки; распознавание ещё не проверено")
    if values.keys() & (set(TEXT_BINDINGS["jarvis_llm"]) | set(FLOAT_BINDINGS["jarvis_llm"])):
        core._dashboard.service("llm", engine=llm.LLM_ENGINE,
            model=llm.LM_STUDIO_MODEL if llm.LLM_ENGINE == "lmstudio" else llm.OLLAMA_MODEL if llm.LLM_ENGINE == "local" else llm.OPENROUTER_MODEL,
            status="unknown", detail="Новые настройки; модель ещё не проверена")
    if values.keys() & {"JARVIS_FOLLOWUP_MODE", "JARVIS_FOLLOWUP_WINDOW"}:
        # Do not create/extend a window from an unrelated timer's speech time.
        core._state.wake_active_until = (min(core._state.wake_active_until, time.time() + config.FOLLOWUP_WINDOW)
                                         if config.FOLLOWUP_MODE != "off" else 0.0)
    if "JARVIS_OVERLAY" in values:
        try:
            if ui.OVERLAY_ENABLED:
                ui.start_overlay()
            else:
                ui.stop_overlay()
        except Exception:
            core._dashboard.service("overlay", engine="overlay", status="error", detail="Не удалось обновить индикатор голоса")


def _close_client(module, name):
    client = getattr(module, name)
    setattr(module, name, None)
    if client is not None:
        try:
            client.close()
        except Exception:
            pass

"""Конфигурация и пути Jarvis.

Здесь читается и пишется jarvis_config.json, определяются пути проекта и
версия. Модуль импортируется ядром первым: настройки из файла попадают в
окружение до того, как остальные модули начнут их читать через os.getenv.

Раньше это было не так — TTS_ENGINE читался на 65-й строке jarvis.py, а
_load_config() вызывался на 241-й, поэтому движок синтеза из конфига молча
игнорировался, если не был задан переменной окружения.

Реальные переменные окружения по-прежнему выигрывают у файла: так можно
переопределить любую настройку на один запуск.
"""

import datetime
import json
import os
import re
import sys
import tempfile
import threading
from pathlib import Path

__all__ = [
    "JARVIS_DIR", "CONFIG_PATH", "APP_VERSION", "UI_SETTING_KEYS",
    "_read_config_file", "_write_config_file", "_load_config",
    "_redirect_output_when_windowed", "_pythonw_exe",
    "PAUSE_THRESHOLD", "SPEAK_COOLDOWN", "BARGE_IN_MAX_AUDIO",
    "WAKE_COMMAND_WINDOW", "PHRASE_TIME_LIMIT", "FOLLOWUP_WINDOW", "FOLLOWUP_MODE",
]


JARVIS_DIR = Path(__file__).parent
CONFIG_PATH = JARVIS_DIR / "jarvis_config.json"
APP_VERSION = "1.2.0"
_CONFIG_LOCK = threading.RLock()

UI_SETTING_KEYS = {
    "JARVIS_LLM", "OLLAMA_MODEL", "LM_STUDIO_URL", "LM_STUDIO_MODEL", "LM_STUDIO_CODE_MODEL",
    "OPENROUTER_MODEL", "OPENROUTER_FREE_MODEL",
    "OPENROUTER_AGENT_MODEL", "JARVIS_PROJECT_ROOTS", "SESSION_MEMORY", "STT_ENGINE",
    "WHISPER_MODEL", "TTS_ENGINE", "PIPER_VOICE", "EDGE_VOICE", "EDGE_RATE", "EDGE_PITCH", "JARVIS_VOICE_STYLE",
    "PIPER_LENGTH_SCALE", "PIPER_NOISE_SCALE", "PIPER_NOISE_W_SCALE",
    "JARVIS_LLM_DEADLINE", "JARVIS_LLM_DEADLINE_CLOUD", "JARVIS_LLM_DEADLINE_LM_STUDIO",
    "JARVIS_LLM_GEN_BUDGET",
    "LM_STUDIO_AUTOLOAD", "LM_STUDIO_GPU", "LM_STUDIO_CONTEXT", "XTTS_SPEED", "XTTS_LANGUAGE",
    "JARVIS_PAUSE_THRESHOLD", "JARVIS_WAKE_COMMAND_WINDOW",
    "JARVIS_PHRASE_TIME_LIMIT", "JARVIS_FOLLOWUP_WINDOW",
    "JARVIS_SPEAK_COOLDOWN",
    "JARVIS_FOLLOWUP_MODE", "JARVIS_MIC_INDEX", "JARVIS_OVERLAY",
    "TELEGRAM_API_ID", "TELEGRAM_PHONE",
}

SECRET_SETTING_KEYS = {"OPENROUTER_API_KEY", "TELEGRAM_API_HASH", "TELEGRAM_REPORT_BOT_TOKEN"}
WRITABLE_SETTING_KEYS = UI_SETTING_KEYS | SECRET_SETTING_KEYS | {"TELEGRAM_REPORT_CHAT_ID"}
# Capture genuine process overrides BEFORE copying the JSON into os.environ.
ENV_OVERRIDES = {key: os.environ[key] for key in WRITABLE_SETTING_KEYS if os.getenv(key)}


def _read_config_snapshot() -> tuple[dict, bytes | None]:
    """Read a valid object and its original bytes; only a missing file is empty."""
    try:
        original = CONFIG_PATH.read_bytes()
    except FileNotFoundError:
        return {}, None
    data = json.loads(original)
    if not isinstance(data, dict):
        raise ValueError("Конфигурация должна быть JSON-объектом.")
    return data, original


def _read_config_file() -> dict:
    with _CONFIG_LOCK:
        try:
            return _read_config_snapshot()[0]
        except Exception as e:
            print(f"[Config] Не удалось прочитать {CONFIG_PATH.name}: {e}")
            return {}


def _atomic_write_config_bytes(path: Path, content: bytes) -> None:
    """Replace on the same filesystem, keeping the destination intact on failure."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=f".{path.name}.",
                suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_config_file(updates: dict) -> tuple[bool, str]:
    """Persist validated settings; the runtime coordinator applies UI updates."""
    # Hold the lock across read/merge/backup/replace, not just the final write.
    with _CONFIG_LOCK:
        return _write_config_file_locked(updates)


def _write_config_file_locked(updates: dict) -> tuple[bool, str]:
    if not isinstance(updates, dict):
        return False, "Некорректные настройки."
    try:
        cfg, original = _read_config_snapshot()
    except Exception as e:
        return False, f"Не удалось прочитать конфигурацию; настройки не изменены: {e}"
    allowed_values = {
        "JARVIS_LLM": {"local", "cloud", "lmstudio"}, "STT_ENGINE": {"whisper", "google"},
        "TTS_ENGINE": {"auto", "piper", "edge", "xtts"},
        "JARVIS_VOICE_STYLE": {"lively", "calm", "neutral"},
        "JARVIS_OVERLAY": {"on", "off"},
        "JARVIS_FOLLOWUP_MODE": {"smart", "strict", "normal", "off"},
        "SESSION_MEMORY": {"on", "off"},
        "LM_STUDIO_AUTOLOAD": {"on", "off"}, "XTTS_LANGUAGE": {"ru", "en"},
    }
    numeric = {
        "JARVIS_LLM_DEADLINE": (0.2, 60.0),
        "JARVIS_LLM_DEADLINE_CLOUD": (1.0, 30.0),
        "JARVIS_LLM_DEADLINE_LM_STUDIO": (1.0, 60.0),
        "JARVIS_LLM_GEN_BUDGET": (1.0, 60.0),
        "JARVIS_PAUSE_THRESHOLD": (1.0, 6.0),
        "JARVIS_WAKE_COMMAND_WINDOW": (3.0, 30.0),
        "JARVIS_PHRASE_TIME_LIMIT": (10.0, 120.0),
        "JARVIS_FOLLOWUP_WINDOW": (0.0, 60.0),
        "JARVIS_SPEAK_COOLDOWN": (0.3, 5.0),
        "PIPER_LENGTH_SCALE": (0.7, 1.5),
        "PIPER_NOISE_SCALE": (0.1, 1.5),
        "PIPER_NOISE_W_SCALE": (0.1, 1.5),
        "XTTS_SPEED": (0.85, 1.15), "LM_STUDIO_GPU": (0.0, 1.0), "LM_STUDIO_CONTEXT": (2048, 32768),
    }
    for key, value in updates.items():
        if key in {"OPENROUTER_API_KEY", "TELEGRAM_API_HASH", "TELEGRAM_REPORT_BOT_TOKEN"}:
            if value:
                cfg[key] = str(value).strip()
            continue
        if key == "TELEGRAM_REPORT_CHAT_ID":
            cfg[key] = str(value).strip()
            continue
        if key not in UI_SETTING_KEYS:
            continue
        value = str(value).strip()
        if key in allowed_values and value.lower() not in allowed_values[key]:
            return False, f"Недопустимое значение {key}."
        if key in allowed_values:
            value = value.lower()
        if key in numeric:
            try:
                number = float(value)
            except ValueError:
                return False, f"{key} должен быть числом."
            lo, hi = numeric[key]
            if not lo <= number <= hi:
                return False, f"{key}: допустимо от {lo} до {hi}."
            if key == "LM_STUDIO_CONTEXT":
                if not number.is_integer():
                    return False, "Контекст LM Studio должен быть целым числом."
                value = str(int(number))
        if key in {"EDGE_RATE", "EDGE_PITCH"}:
            unit = "%" if key == "EDGE_RATE" else "Hz"
            match = re.fullmatch(r"([+-]\d+)" + unit, value)
            if not match or not -30 <= int(match[1]) <= 30:
                return False, f"{key}: укажите от -30{unit} до +30{unit}, включая знак."
        if key == "JARVIS_MIC_INDEX" and value:
            try:
                if int(value) < 0:
                    raise ValueError
            except ValueError:
                return False, "Индекс микрофона должен быть неотрицательным целым числом."
        if key == "TELEGRAM_API_ID" and value:
            try:
                if int(value) <= 0:
                    raise ValueError
            except ValueError:
                return False, "Telegram API ID должен быть положительным целым числом."
        if key == "TELEGRAM_PHONE" and value and not re.fullmatch(r'\+?[0-9]{7,15}', value):
            return False, "Телефон Telegram укажите в международном формате, например +79991234567."
        cfg[key] = value
    try:
        content = json.dumps(cfg, ensure_ascii=False, indent=2).encode("utf-8")
        if original is not None:
            backup = CONFIG_PATH.with_name(CONFIG_PATH.name + ".bak")
            _atomic_write_config_bytes(backup, original)
        _atomic_write_config_bytes(CONFIG_PATH, content)
        return True, "Настройки сохранены."
    except Exception as e:
        return False, f"Не удалось сохранить настройки: {e}"


def _redirect_output_when_windowed():
    """Under pythonw.exe there is no console and sys.stdout is None, which makes
    every print() in this file raise. Send output to logs/console.log instead —
    that's also where you look when the app misbehaves with no console to watch."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_dir = JARVIS_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    f = open(log_dir / "console.log", "a", encoding="utf-8", buffering=1)
    f.write(f"\n{'='*60}\n{datetime.datetime.now():%Y-%m-%d %H:%M:%S} — запуск\n")
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f


_redirect_output_when_windowed()


def _load_config():
    """Load settings from jarvis_config.json into the environment.

    Keeps the API key out of a .bat launcher so Jarvis can start from a plain
    shortcut. Real environment variables always win, so you can still override
    any setting per-run. This file holds secrets — it is gitignored.
    """
    cfg = _read_config_file()
    for k, v in cfg.items():
        if v is not None and not os.getenv(k):
            os.environ[k] = str(v)


_load_config()


def _pythonw_exe() -> str:
    """Path to pythonw.exe — starts child processes without a console window."""
    exe = Path(sys.executable)
    noconsole = exe.with_name("pythonw.exe")
    return str(noconsole if noconsole.exists() else exe)


# ── Тайминги речи ───────────────────────────────────────────────────────────
# Нужны и ядру, и синтезу: синтез после реплики взводит окно продолжения,
# а фильтр посторонней речи по этому же окну решает, ждать ли команду.

PAUSE_THRESHOLD = float(os.getenv("JARVIS_PAUSE_THRESHOLD", "2.6"))
# Experimental faster endpointing (not full streaming STT). On → shorter pause.
if os.getenv("JARVIS_FAST_VAD", "off").lower() in {"1", "on", "true", "yes"}:
    PAUSE_THRESHOLD = min(PAUSE_THRESHOLD, 1.35)

SPEAK_COOLDOWN = float(os.getenv("JARVIS_SPEAK_COOLDOWN", "1.5"))

# Пока Джарвис говорит, микрофон слышит в основном его самого. Но на своё имя он
# обязан отзываться даже посреди собственной фразы, поэтому короткие реплики в это
# время всё-таки распознаются и проверяются на обращение. Длинные — это его же
# голос из колонок, их отбрасываем не тратя GPU.
BARGE_IN_MAX_AUDIO = float(os.getenv("JARVIS_BARGE_IN_MAX_AUDIO", "6.0"))

WAKE_COMMAND_WINDOW = float(os.getenv("JARVIS_WAKE_COMMAND_WINDOW", "10.0"))

PHRASE_TIME_LIMIT = float(os.getenv("JARVIS_PHRASE_TIME_LIMIT", "45.0"))

FOLLOWUP_WINDOW = float(os.getenv("JARVIS_FOLLOWUP_WINDOW", "60.0"))
FOLLOWUP_MODE = os.getenv("JARVIS_FOLLOWUP_MODE", "smart").lower()
if FOLLOWUP_MODE not in {"smart", "strict", "normal", "off"}:
    FOLLOWUP_MODE = "smart"

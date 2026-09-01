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
import sys
from pathlib import Path

__all__ = [
    "JARVIS_DIR", "CONFIG_PATH", "APP_VERSION", "UI_SETTING_KEYS",
    "_read_config_file", "_write_config_file", "_load_config",
    "_redirect_output_when_windowed", "_pythonw_exe",
]


JARVIS_DIR = Path(__file__).parent
CONFIG_PATH = JARVIS_DIR / "jarvis_config.json"
APP_VERSION = "1.2.0"

UI_SETTING_KEYS = {
    "JARVIS_LLM", "OLLAMA_MODEL", "OPENROUTER_MODEL", "OPENROUTER_FREE_MODEL",
    "OPENROUTER_AGENT_MODEL", "JARVIS_PROJECT_ROOTS", "SESSION_MEMORY", "STT_ENGINE",
    "WHISPER_MODEL", "TTS_ENGINE", "PIPER_VOICE", "EDGE_VOICE",
    "PIPER_LENGTH_SCALE", "PIPER_NOISE_SCALE", "PIPER_NOISE_W_SCALE",
    "JARVIS_LLM_DEADLINE", "JARVIS_LLM_DEADLINE_CLOUD", "JARVIS_LLM_GEN_BUDGET",
    "JARVIS_PAUSE_THRESHOLD", "JARVIS_WAKE_COMMAND_WINDOW",
    "JARVIS_PHRASE_TIME_LIMIT", "JARVIS_FOLLOWUP_WINDOW",
    "JARVIS_SPEAK_COOLDOWN",
    "JARVIS_FOLLOWUP_MODE", "JARVIS_MIC_INDEX", "JARVIS_OVERLAY",
    "TELEGRAM_API_ID", "TELEGRAM_PHONE",
}


def _read_config_file() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[Config] Не удалось прочитать {CONFIG_PATH.name}: {e}")
        return {}


def _write_config_file(updates: dict) -> tuple[bool, str]:
    """Persist validated UI settings. Most engine settings apply on restart."""
    if not isinstance(updates, dict):
        return False, "Некорректные настройки."
    cfg = _read_config_file()
    allowed_values = {
        "JARVIS_LLM": {"local", "cloud"}, "STT_ENGINE": {"whisper", "google"},
        "TTS_ENGINE": {"auto", "piper", "edge", "xtts"},
        "JARVIS_OVERLAY": {"on", "off"},
        "JARVIS_FOLLOWUP_MODE": {"strict", "normal", "off"},
        "SESSION_MEMORY": {"on", "off"},
    }
    numeric = {
        "JARVIS_LLM_DEADLINE": (0.2, 15.0),
        "JARVIS_LLM_DEADLINE_CLOUD": (1.0, 30.0),
        "JARVIS_LLM_GEN_BUDGET": (1.0, 60.0),
        "JARVIS_PAUSE_THRESHOLD": (1.0, 6.0),
        "JARVIS_WAKE_COMMAND_WINDOW": (3.0, 30.0),
        "JARVIS_PHRASE_TIME_LIMIT": (10.0, 120.0),
        "JARVIS_FOLLOWUP_WINDOW": (0.0, 60.0),
        "JARVIS_SPEAK_COOLDOWN": (0.3, 5.0),
        "PIPER_LENGTH_SCALE": (0.7, 1.5),
        "PIPER_NOISE_SCALE": (0.1, 1.5),
        "PIPER_NOISE_W_SCALE": (0.1, 1.5),
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
        if key in numeric:
            try:
                number = float(value)
            except ValueError:
                return False, f"{key} должен быть числом."
            lo, hi = numeric[key]
            if not lo <= number <= hi:
                return False, f"{key}: допустимо от {lo} до {hi}."
        if key == "JARVIS_MIC_INDEX" and value:
            try:
                int(value)
            except ValueError:
                return False, "Индекс микрофона должен быть целым числом."
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
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True, "Настройки сохранены. Перезапустите Джарвис для применения."
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

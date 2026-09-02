# Джарвис — голосовой помощник для Windows: слушает, отвечает голосом и выполняет команды

import os
import subprocess
import time
import urllib.parse
import threading
import queue
import re
import sys
import traceback
import asyncio
import json
import logging
import math
import ctypes
import shutil
from pathlib import Path
from difflib import SequenceMatcher
import datetime
import requests as http_requests

# Конфиг подключается первым: до него ни один os.getenv не должен выполниться.
from jarvis_config import *  # noqa: F401,F403

import speech_recognition as sr
from openai import OpenAI
import pygame
try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS
import pyperclip
import psutil


try:
    from PIL import ImageGrab
except ImportError:
    ImageGrab = None

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import jarvis_features as _feat
import jarvis_platform as _plat
import project_agent as _project_agent

try:
    import edge_tts
except ImportError:
    edge_tts = None

try:
    from telethon import TelegramClient
    from telethon.errors import (
        PasswordHashInvalidError, PhoneCodeExpiredError, PhoneCodeInvalidError,
        SessionPasswordNeededError,
    )
    from telethon.utils import get_display_name as telegram_display_name
except ImportError:
    TelegramClient = None
    PasswordHashInvalidError = PhoneCodeExpiredError = PhoneCodeInvalidError = SessionPasswordNeededError = Exception
    telegram_display_name = None

TTS_ENGINE = os.getenv("TTS_ENGINE", "auto").lower()

tts = None
XTTS_DEVICE = None

def _load_xtts_if_needed():
    global tts, XTTS_DEVICE
    if tts is not None:
        return
    print("Loading XTTS-v2 (this will be slow on first use)...")
    try:
        import torch
        import torchaudio
        import soundfile as sf

        _original_load = torch.load
        def _patched_load(*args, **kwargs):
            kwargs['weights_only'] = False
            return _original_load(*args, **kwargs)
        torch.load = _patched_load

        def _patched_audio_load(filepath, **kwargs):
            data, samplerate = sf.read(filepath, dtype='float32')
            data = data.T if len(data.shape) > 1 else data.reshape(1, -1)
            return torch.tensor(data), samplerate
        torchaudio.load = _patched_audio_load

        from TTS.api import TTS
        XTTS_DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"XTTS device: {XTTS_DEVICE} (RTX 5070 CUDA preferred)")
        tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(XTTS_DEVICE)
    except Exception as e:
        print(f"XTTS load error: {e}")
        tts = None

command_queue = queue.Queue()
pending_telegram_send = None
pending_email_send = None

conversation_history = []
MAX_HISTORY = 4

# Persist the short-term dialogue across restarts. Off by default: the session
# file is personal data, so it is opt-in via config/env.
SESSION_MEMORY = os.getenv("SESSION_MEMORY", "off").strip().lower() in ("on", "1", "true", "yes")

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
_last_llm_ttft_ms = 0.0
_llm_empty_failovers = 0


_is_speaking = False
_speaking_cooldown_until = 0.0
_recognizer = None
_interrupt_event = threading.Event()
_threshold_before_speech = None


# начали говорить — запоминаем порог микрофона, чтобы наш голос его не сбил
def _mark_speaking():
    """Enter the speaking state and remember the mic threshold from before it."""
    global _is_speaking, _threshold_before_speech
    if not _is_speaking and _recognizer is not None:
        _threshold_before_speech = _recognizer.energy_threshold
    _is_speaking = True
    # Звук пошёл — синтез уже позади, подпись под шаром должна это показывать.
    ui_sub("говорю…")


# закончили говорить — ненадолго глушим микрофон, чтобы не слышать себя
def _set_done_speaking():
    """Mark TTS as finished, start the mic cooldown, and open the follow-up window."""
    global _is_speaking, _speaking_cooldown_until, _wake_active_until
    global _threshold_before_speech
    _is_speaking = False
    _speaking_cooldown_until = time.time() + SPEAK_COOLDOWN
    _wake_active_until = (time.time() + FOLLOWUP_WINDOW) if FOLLOWUP_MODE != "off" else 0.0
    # Пока Джарвис говорил, его собственный голос задирал порог микрофона через
    # dynamic_energy_threshold. Возвращаем то значение, что было до речи, иначе
    # он глохнет к пользователю после каждого своего ответа.
    if _recognizer is not None and _threshold_before_speech is not None:
        _recognizer.energy_threshold = min(_threshold_before_speech, 1500)
        jarvis_logger.debug(
            f"[SPEAK] порог микрофона восстановлен: {_recognizer.energy_threshold:.0f}")
    _threshold_before_speech = None
    ui_state("idle")
    ui_sub("")   # вернуть подпись по умолчанию, даже если состояние не менялось
    jarvis_logger.debug(f"[SPEAK] закончил → cooldown {SPEAK_COOLDOWN:.1f} с, "
                        f"окно продолжения {FOLLOWUP_WINDOW:.0f} с")


LOGS_DIR = JARVIS_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
log_filename = LOGS_DIR / f"jarvis_{datetime.datetime.now().strftime('%Y-%m-%d')}.log"
_log_fh = logging.FileHandler(str(log_filename), encoding="utf-8", mode="a")
_log_fh.setLevel(logging.DEBUG)
_log_fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s"))
jarvis_logger = logging.getLogger("jarvis")
jarvis_logger.setLevel(logging.DEBUG)
if not jarvis_logger.handlers:
    jarvis_logger.addHandler(_log_fh)
jarvis_logger.propagate = False

def log_interaction(role: str, text: str):
    """Log user commands and Jarvis replies to daily log file."""
    jarvis_logger.info(f"[{role.upper()}] {text}")

MEMORY_FILE = JARVIS_DIR / "jarvis_memory.json"

def load_memory() -> dict:
    """Load persistent personal memory from JSON."""
    if MEMORY_FILE.exists():
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_memory(memory: dict):
    """Save persistent memory to JSON."""
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(memory, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Memory save error: {e}")

def remember(key: str, value: str) -> str:
    """Store a fact in long-term memory."""
    mem = load_memory()
    mem[key] = value
    save_memory(mem)
    return f"Запомнил: {key} = {value}"

def recall(key: str = None) -> str:
    """Recall fact(s) from long-term memory."""
    mem = load_memory()
    if not mem:
        return "Долгосрочная память пуста."
    if key:
        val = mem.get(key)
        return f"{key}: {val}" if val else f"Не помню ничего о '{key}'."
    items = "; ".join(f"{k}: {v}" for k, v in list(mem.items())[:10])
    return f"Вот что я помню: {items}."

TODO_FILE = JARVIS_DIR / "jarvis_todo.json"

def load_todo() -> list:
    if TODO_FILE.exists():
        try:
            with open(TODO_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_todo(items: list):
    try:
        with open(TODO_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Todo save error: {e}")

def todo_add(text: str) -> str:
    items = load_todo()
    items.append({"task": text, "done": False, "added": datetime.datetime.now().isoformat()})
    save_todo(items)
    return f"Добавил в список: {text}"

def todo_list() -> str:
    items = load_todo()
    pending = [i for i in items if not i["done"]]
    if not pending:
        return "Список дел пуст, сэр."
    tasks = "; ".join(f"{n+1}. {i['task']}" for n, i in enumerate(pending[:7]))
    return f"Ваш список дел: {tasks}."

def todo_done(n: int) -> str:
    items = load_todo()
    pending = [i for i in items if not i["done"]]
    if 1 <= n <= len(pending):
        pending[n-1]["done"] = True
        save_todo(items)
        return f"Готово: {pending[n-1]['task']}"
    return "Такого пункта нет в списке."

_active_timers: list = []

def set_timer(seconds: int, label: str = "", speak_fn=None):
    """Fire a voice alarm after `seconds` seconds."""
    def _fire():
        time.sleep(seconds)
        msg = f"Время вышло, сэр. {label}" if label else "Таймер сработал, сэр."
        print(f"[TIMER] {msg}")
        if speak_fn:
            speak_fn(msg)
    t = threading.Thread(target=_fire, daemon=True)
    t.start()
    _active_timers.append(t)

def parse_timer_duration(text: str) -> int | None:
    """Parse '10 минут', '30 секунд', '1 час' etc. Returns seconds or None."""
    text = text.lower()
    if re.search(r'(?<!\w)полчаса(?!\w)', text):
        return 1800

    number_words = {
        "шестьдесят": 60, "пятьдесят": 50, "сорок": 40, "тридцать": 30,
        "двадцать": 20, "девятнадцать": 19, "восемнадцать": 18,
        "семнадцать": 17, "шестнадцать": 16, "пятнадцать": 15,
        "четырнадцать": 14, "тринадцать": 13, "двенадцать": 12,
        "одиннадцать": 11, "десять": 10, "девять": 9, "восемь": 8,
        "семь": 7, "шесть": 6, "пять": 5, "четыре": 4, "три": 3,
        "два": 2, "две": 2, "один": 1, "одну": 1, "одна": 1,
    }
    units = {
        "девять": 9, "восемь": 8, "семь": 7, "шесть": 6,
        "пять": 5, "четыре": 4, "три": 3, "два": 2, "две": 2,
        "один": 1, "одну": 1, "одна": 1,
    }
    for tens_word, tens_value in (("двадцать", 20), ("тридцать", 30),
                                  ("сорок", 40), ("пятьдесят", 50)):
        for unit_word, unit_value in units.items():
            text = re.sub(rf'(?<!\w){tens_word}\s+{unit_word}(?!\w)',
                          str(tens_value + unit_value), text)
    for word, value in number_words.items():
        text = re.sub(rf'(?<!\w){word}(?!\w)', str(value), text)

    total = 0
    m = re.search(r'(\d+)\s*(?:час(?:а|ов)?|ч\b)', text)
    if m: total += int(m.group(1)) * 3600
    m = re.search(r'(\d+)\s*(?:минут(?:у|ы)?|мин\b)', text)
    if m: total += int(m.group(1)) * 60
    m = re.search(r'(\d+)\s*(?:секунд(?:у|ы)?|сек\b)', text)
    if m: total += int(m.group(1))
    return total if total > 0 else None

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


def handle_local_productivity_command(text: str, speak_fn=None) -> str | None:
    """Execute common productivity commands without an LLM round-trip.

    Returns the reply to speak, or None when the text is not an unambiguous
    local command. Patterns are deliberately verb/shape-qualified so ordinary
    conversation mentioning weather, memory, or tasks is not hijacked.
    """
    t = re.sub(r'\s+', ' ', (text or '').strip().lower()).strip(' .,!?:;')
    if not t:
        return None

    datetime_reply = get_datetime_reply(t)
    if datetime_reply:
        return datetime_reply

    web_query = extract_web_search_query(t)
    if web_query:
        return search_web(web_query)

    weather = re.fullmatch(
        r'(?:(?:скажи|покажи)\s+)?(?:какая\s+)?(?:сейчас\s+)?'
        r'(?:погода|прогноз погоды)(?:\s+(?:в|для)\s+(.+?))?'
        r'(?:\s+(?:сейчас|сегодня))?', t)
    if weather:
        city = (weather.group(1) or "Москва").strip()
        city = {"москве": "Москва", "питере": "Санкт-Петербург",
                "петербурге": "Санкт-Петербург"}.get(city, city)
        return get_weather(city)

    if re.match(r'^(?:(?:поставь|запусти|установи)\s+)?таймер\b', t):
        seconds = parse_timer_duration(t)
        if not seconds:
            return "Не понял длительность таймера, сэр."
        label_match = re.search(r'\bдля\s+(.+)$', t)
        label = label_match.group(1).strip() if label_match else ""
        set_timer(seconds, label, speak_fn=speak_fn or speak)
        mins, secs = divmod(seconds, 60)
        hours, mins = divmod(mins, 60)
        parts = []
        if hours: parts.append(f"{hours} ч")
        if mins: parts.append(f"{mins} мин")
        if secs: parts.append(f"{secs} сек")
        return f"Таймер на {' '.join(parts)} запущен, сэр."

    remember_match = re.match(r'^(?:запомни|сохрани в память)\s+(.+)$', t)
    if remember_match:
        fact = remember_match.group(1).strip()
        key = f"заметка {datetime.datetime.now():%Y-%m-%d %H:%M:%S}"
        remember(key, fact)
        return "Запомнил, сэр."
    if re.fullmatch(r'(?:что ты помнишь|покажи память|что у тебя в памяти|вспомни обо мне)', t):
        return recall()

    done_match = re.fullmatch(
        r'(?:(?:отметь|закрой)\s+)?(?:задачу|пункт)\s+(\d+)\s+'
        r'(?:выполненной|выполненным|готово)', t)
    if not done_match:
        done_match = re.fullmatch(r'(?:выполнил|завершил)\s+(?:задачу|пункт)\s+(\d+)', t)
    if done_match:
        return todo_done(int(done_match.group(1)))

    add_match = re.match(
        r'^(?:добавь|запиши)\s+(?:задачу|в список дел)\s*:?[ ]*(.+)$', t)
    if not add_match:
        add_match = re.match(r'^задача\s*:?[ ]*(.+)$', t)
    if add_match:
        task = add_match.group(1).strip()
        return todo_add(task) if task else "Не услышал текст задачи, сэр."

    if re.fullmatch(r'(?:покажи|прочитай)?\s*(?:список дел|мои задачи|что в списке)', t):
        return todo_list()

    return None


def handle_local_feature_command(text: str, last_reply: str = "", speak_fn=None) -> str | None:
    """Windows/clipboard/reminders/files/OCR/mail/session — local, no LLM."""
    agent_match = re.match(
        r'^(?:поработай|работай|исправь|доработай)\s+(?:над|в)\s+проект(?:е|ом)?\s+'
        r'([^:]+?)\s*:\s*(.+)$', (text or "").strip(), re.IGNORECASE | re.DOTALL)
    if agent_match:
        if not OPENROUTER_API_KEY:
            return "Для проектного агента нужен ключ OpenRouter, сэр."
        project, task = agent_match.group(1).strip(), agent_match.group(2).strip()
        try:
            return _project_agent.run_project_agent(
                get_openrouter_client(), OPENROUTER_AGENT_MODEL, project, task)
        except Exception as exc:
            jarvis_logger.exception("[PROJECT_AGENT] failed")
            return f"Проектный агент завершился с ошибкой: {type(exc).__name__}: {exc}"
    result = _feat.handle_feature_command(text, last_reply=last_reply or "")
    if result == "__FOCUS_MODE__":
        set_volume(0)
        set_timer(25 * 60, "фокус", speak_fn=speak_fn or speak)
        return "Режим фокуса: звук выключен, таймер 25 минут, сэр."
    return result

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


WAKE_CANON = ("джарвис", "jarvis")
WAKE_VARIANT_RE = re.compile(
    r"^(?:дж|ж|ч|щ|д|ш|х|з|тр)[аоуяею]р?[вб][еиыія][сзц]ь?$"
    r"|^(?:ярвис|арвис|ярвись)$"
    r"|^(?:jarvis|travis|djarvis|jarvis|harvey)$",
    re.UNICODE,
)
WAKE_FUZZY_THRESHOLD = 0.72

# Whisper коверкает имя по-разному, но почти всегда сохраняет характерное начало:
# «Джарез», «Джаммитс», «Джанес», «Жарвес» — всё это реальные пропущенные обращения
# из логов, они не дотягивали до 0.72 и Джарвис молчал. А обычные слова, которые
# случайно похожи целиком («держись», «договаривались», «дарим», «жарим»,
# «ужаристы»), начинаются иначе. Поэтому порог опускаем только для токенов с таким
# началом — общий порог 0.72 при этом не трогаем, иначе полезет фоновая речь.
WAKE_ONSET_RE = re.compile(r"^(?:джа|жарв)", re.UNICODE)
WAKE_ONSET_THRESHOLD = 0.60
WAKE_ONSET_MIN_LEN = 5

WAKE_BLOCKLIST = frozenset({"дарвин", "давись"})

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

FOLLOWUP_WINDOW = float(os.getenv("JARVIS_FOLLOWUP_WINDOW", "15.0"))
FOLLOWUP_MODE = os.getenv("JARVIS_FOLLOWUP_MODE", "strict").lower()
if FOLLOWUP_MODE not in {"strict", "normal", "off"}:
    FOLLOWUP_MODE = "strict"

_last_spoken_text = ""

_BACKCHANNEL = frozenset({
    "ага", "угу", "ну", "хм", "хмм", "мм", "ммм", "эм", "э", "а", "ой",
    "мгм", "да-да", "ага-ага", "тс", "ш", "вот", "это", "так",
})

_WHISPER_GHOST_RE = re.compile(
    r"(продолжение следует|субтитр|спасибо за просмотр|редактор субтитров|"
    r"корректор|dimatorzok|подписывайтесь на канал|игорь негода)",
    re.IGNORECASE | re.UNICODE,
)


# похоже ли услышанное на эхо того, что Джарвис только что сказал сам
def _is_echo_of_last_spoken(t: str) -> bool:
    """True if the heard text looks like Jarvis's own last reply coming back."""
    if not _last_spoken_text or not t:
        return False
    heard = re.sub(r'\W+', ' ', t, flags=re.UNICODE).strip()
    spoken = re.sub(r'\W+', ' ', _last_spoken_text.lower(), flags=re.UNICODE).strip()
    if not heard or not spoken:
        return False
    ratio = SequenceMatcher(None, heard, spoken).ratio()
    heard_words = set(heard.split())
    spoken_words = set(spoken.split())
    overlap = len(heard_words & spoken_words) / max(1, len(heard_words))
    return ratio >= 0.58 or (len(heard_words) >= 3 and overlap >= 0.72)


def _is_stray_speech(text: str) -> bool:
    """True if this looks like speech NOT meant for Jarvis (or STT noise).

    Only used inside the follow-up window, where there's no wake word to rely on.
    "Is this addressed to me?" is fundamentally undecidable without one, so this
    filters only the unambiguous cases: STT hallucinations and bare interjections.
    Anything substantive is treated as a command.
    """
    t = text.strip().strip(".,!?…").lower()
    if not t:
        return True
    if ((pending_telegram_send is not None or pending_email_send is not None)
            and (t in _TELEGRAM_CONFIRM_YES or t in _TELEGRAM_CONFIRM_NO)):
        return False
    if _WHISPER_GHOST_RE.search(t):
        return True
    if t in _BACKCHANNEL:
        return True
    if len(t) <= 2:
        return True
    if _is_echo_of_last_spoken(t):
        return True
    if FOLLOWUP_MODE == "strict":
        if not re.search(
            r'\b(открой|запусти|включи|выключи|покажи|скажи|расскажи|объясни|'
            r'найди|сделай|поставь|добавь|запомни|напомни|напиши|проверь|прочитай|'
            r'какой|какая|какие|который|как|что|когда|где|почему|сколько|повтори|стоп|'
            r'громче|тише|ярче|темнее|пауза|следующий|предыдущий)\b', t):
            return True
    return False

INTENT_PATTERNS = [
    (re.compile(r'\b(открой|запусти|включи)\b.{0,20}\b(браузер|хром|chrome|интернет|гугл|google)\b', re.IGNORECASE | re.UNICODE), 'OPEN:browser'),
    (re.compile(r'\b(открой|запусти).{0,20}(клод|claude)\b', re.IGNORECASE | re.UNICODE), 'OPEN:claude'),
    (re.compile(r'\b(открой|запусти).{0,20}(телеграм|телега|telegram)\b', re.IGNORECASE | re.UNICODE), 'OPEN:telegram'),
    (re.compile(r'\b(открой|запусти).{0,20}(дискорд|discord)\b', re.IGNORECASE | re.UNICODE), 'OPEN:discord'),
    (re.compile(r'\b(открой|запусти).{0,20}(vs code|vscode|код|code)\b', re.IGNORECASE | re.UNICODE), 'OPEN:vscode'),
    (re.compile(r'\b(открой|запусти).{0,20}(обсидиан|obsidian|заметки)\b', re.IGNORECASE | re.UNICODE), 'OPEN:obsidian'),
    (re.compile(r'\b(открой|запусти).{0,20}(блокнот|notepad|записную)\b', re.IGNORECASE | re.UNICODE), 'OPEN:notepad'),
    (re.compile(r'\b(открой|запусти).{0,20}(калькулятор|calc)\b', re.IGNORECASE | re.UNICODE), 'OPEN:calc'),
    (re.compile(r'\b(включи|поставь|запусти|открой).{0,30}(музыку|яндекс.музык|yandex.music)\b', re.IGNORECASE | re.UNICODE), 'MUSIC:OPEN'),
    (re.compile(r'\b(включи|поставь|запусти).{0,20}(волну|мою волну)\b', re.IGNORECASE | re.UNICODE), 'MUSIC:PLAY:мою волну'),
]

def _has_word(text: str, words) -> bool:
    """True if any of `words` appears in `text` as a WHOLE word.

    Plain `w in text` (what this used to be) matched inside other words and
    hijacked commands: "включи плейлист" hit "плей" → play/pause, "выключи
    дисплей" likewise, "открой instagram" hit "ram" → system stats, and
    "что такое blockchain" hit "lock" → locked the PC.

    \b is Unicode-aware for str patterns in Python 3, so this works for Cyrillic;
    lookarounds are used instead so multi-word triggers ("который час") also work.
    """
    for w in words:
        if re.search(r'(?<!\w)' + re.escape(w) + r'(?!\w)', text, re.UNICODE):
            return True
    return False


def detect_intent_from_text(text: str) -> str | None:
    """Fallback intent detection when LLM didn't output a tag.
    Returns a tag string like '[OPEN:browser]' or None."""
    text_lower = text.lower()
    for pattern, tag in INTENT_PATTERNS:
        if pattern.search(text_lower):
            return f"[{tag}]"
    return None


def _is_hypothetical_action_question(text: str) -> bool:
    """Do not execute tools when the user is only asking about capability."""
    t = re.sub(r'\s+', ' ', (text or '').strip().lower())
    if not t:
        return False
    if re.search(
        r'\bесли\b.{0,120}\b(?:скажу|попрошу|дам команду|захочу)\b'
        r'.{0,120}\b(?:сможешь|сумеешь|получится|будешь уметь)\b', t):
        return True
    return bool(re.search(
        r'^(?:скажи|расскажи|ответь)[, ]+.*\b(?:можешь ли|сможешь ли|умеешь ли)\b', t))


def detect_telegram_intent_from_text(text: str) -> str | None:
    """Deterministic routing for common Telegram commands, without the LLM."""
    if _is_hypothetical_action_question(text):
        return None
    t = re.sub(r'\s+', ' ', (text or '').strip().lower()).strip(' .,!?:;')
    if not t or not re.search(r'\bтелеграм\w*\b', t, re.UNICODE):
        return None

    if re.fullmatch(r'(?:покажи|перечисли|назови)?\s*(?:мои\s+)?(?:чаты|диалоги)\s+(?:в\s+)?телеграм\w*', t):
        return "[TG:CHATS]"

    export = re.match(
        r'^(?:выгрузи|экспортируй|сохрани)\s+(?:из\s+телеграм\w*\s+)?'
        r'(?:диалог|чат|переписк\w*)(?:\s+с)?\s+(.+)$', t)
    if export:
        tail = export.group(1).strip()
        count_match = re.search(r'\b(?:последн\w*\s+)?(\d{1,4})\s+сообщен\w*\b', tail)
        count = int(count_match.group(1)) if count_match else 200
        if count_match:
            tail = (tail[:count_match.start()] + tail[count_match.end():]).strip(' ,')
        return f"[TG:EXPORT:{tail}:{count}]" if tail else None

    read = re.match(
        r'^(?:прочитай|покажи)\s+(?:последн\w*\s+)?(?:(\d{1,2})\s+)?'
        r'сообщен\w*\s+(?:из|в)\s+(?:телеграм\w*\s+)?(?:чате?\s+)?(?:с\s+)?(.+)$', t)
    if read:
        return f"[TG:READ:{read.group(2).strip()}:{int(read.group(1) or 10)}]"

    search = re.match(
        r'^(?:найди|поищи)\s+(?:в\s+)?телеграм\w*\s+(?:в\s+)?(?:чате?\s+)?'
        r'(.+?)\s+(?:сообщен\w*|текст|слова?)\s+(.+)$', t)
    if search:
        return f"[TG:SEARCH:{search.group(1).strip()}:{search.group(2).strip()}]"

    send = re.match(
        r'^отправь\s+(?:в\s+телеграм\w*\s+)?(?:в\s+чат\s+)?'
        r'(.+?)\s+(?:сообщение|текст)\s+(.+)$', t)
    if send:
        return f"[TG:SEND:{send.group(1).strip()}:{send.group(2).strip()}]"

    lookup = extract_lookup_request(t)
    if lookup:
        kind, value = lookup
        if kind == "tg":
            return f"[LOOKUP:TG:{value}]"
        if kind == "phone":
            return f"[LOOKUP:PHONE:{value}]"
    return None


_TTS_INSTANT_CACHE: dict[str, str] = {}
_TTS_CACHE_DIR = JARVIS_DIR / "tts_cache"

INSTANT_PHRASES = [
    "Слушаю, сэр.",
    "Слушаю.",
    "Выполняю, сэр.",
    "Готово, сэр.",
    "Открываю, сэр.",
    "Включаю, сэр.",
    "Открываю браузер, сэр.",
    "Открываю Яндекс Музыку, сэр.",
    "Блокирую, сэр.",
    "Системы на связи, сэр.",
    "Одну секунду, сэр.",
    "Есть, сэр.",
    "Секунду, обрабатываю, сэр.",
    "Тише, сэр.",
    "Громче, сэр.",
    "Звук выключен, сэр.",
    "Следующий, сэр.",
    "Предыдущий, сэр.",
    "Всегда пожалуйста, сэр.",
    "Здравствуйте, сэр.",
]


def _cache_ext() -> str:
    return "mp3" if _effective_tts_engine() == "edge" else "wav"


def prewarm_tts_cache():
    """Pre-generate audio for common phrases into on-disk cache (once).

    Runs at startup. Files persist between runs, so after the first ever launch
    these phrases are instant even on a cold start.
    """
    effective = _effective_tts_engine()
    if not ((effective == "piper" and _piper_available())
            or (effective == "edge" and edge_tts is not None)):
        return
    try:
        _TTS_CACHE_DIR.mkdir(exist_ok=True)
    except Exception:
        return
    engine = effective
    # В ключ входит не только движок, но и конкретный голос: иначе после смены
    # PIPER_VOICE готовые фразы продолжали бы играть старым голосом, а остальной
    # ответ — новым. Это тот самый баг «два голоса в одном ответе».
    voice_id = (f"{PIPER_MODEL_PATH.stem}:{PIPER_LENGTH_SCALE}:"
                f"{PIPER_NOISE_SCALE}:{PIPER_NOISE_W_SCALE}"
                if engine == "piper" else EDGE_VOICE)
    for phrase in INSTANT_PHRASES:
        import hashlib
        h = hashlib.md5(f"{engine}:{voice_id}:{phrase}".encode("utf-8")).hexdigest()[:12]
        existing = _TTS_CACHE_DIR / f"{h}.{_cache_ext()}"
        if existing.exists():
            _TTS_INSTANT_CACHE[phrase] = str(existing)
            continue
        for stale in _TTS_CACHE_DIR.glob(f"{h}.*"):
            try:
                stale.unlink()
                print(f"[TTS cache] удалён файл от другого движка: {stale.name}")
                jarvis_logger.warning(f"[TTS cache] удалён файл от другого движка: {stale.name}")
            except Exception:
                pass
        data, suffix = tts_to_bytes(phrase)
        if data:
            fpath = _TTS_CACHE_DIR / f"{h}{suffix}"
            try:
                fpath.write_bytes(data)
                _TTS_INSTANT_CACHE[phrase] = str(fpath)
            except Exception:
                continue


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


def _wav_envelope(data: bytes, fps: int = 60):
    """Per-frame loudness (0..1) of a WAV, for driving the overlay bars.

    Returns None for anything we can't read (e.g. the mp3 fallback path).
    """
    try:
        import io, wave
        import numpy as np
        with wave.open(io.BytesIO(data)) as w:
            if w.getsampwidth() != 2:
                return None
            rate, ch = w.getframerate(), w.getnchannels()
            samples = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if ch > 1:
            samples = samples.reshape(-1, ch).mean(axis=1)
        samples = samples.astype(np.float32) / 32768.0
        hop = max(1, rate // fps)
        n = len(samples) // hop
        if n < 1:
            return None
        frames = samples[:n * hop].reshape(n, hop)
        rms = np.sqrt((frames ** 2).mean(axis=1))
        env = rms ** 0.55
        peak = env.max()
        if peak <= 1e-6:
            return None
        return (env / peak).clip(0, 1).tolist()
    except Exception:
        return None


def _playback_pump(env, fps: int = 60) -> bool:
    """Block until playback ends, feeding the overlay real amplitude as it goes.

    Returns False if playback was interrupted (barge-in), True if it finished.
    """
    show = OVERLAY_ENABLED and _main_window_minimized()
    if show:
        _overlay_send(show=True, amp=0.0)
    clock = pygame.time.Clock()
    try:
        while pygame.mixer.music.get_busy():
            if _interrupt_event.is_set():
                pygame.mixer.music.stop()
                return False
            if show:
                if env:
                    pos = pygame.mixer.music.get_pos()
                    i = int(pos / 1000.0 * fps) if pos >= 0 else 0
                    amp = env[i] if 0 <= i < len(env) else 0.0
                else:
                    amp = 0.45 + 0.25 * math.sin(time.perf_counter() * 9.0)
                _overlay_send(amp=amp)
            clock.tick(fps)
        return True
    finally:
        if show:
            _overlay_send(amp=0.0, show=False)


def _play_cached_file(path: str) -> bool:
    """Play a pre-generated cache file instantly through pygame."""
    try:
        if not pygame.mixer.get_init():
            pygame.mixer.init()
        pygame.mixer.music.load(path)
        _interrupt_event.clear()
        _mark_speaking()
        env = None
        if OVERLAY_ENABLED and path.endswith(".wav") and _main_window_minimized():
            try:
                env = _wav_envelope(Path(path).read_bytes())
            except Exception:
                env = None
        pygame.mixer.music.play()
        _playback_pump(env)
        _set_done_speaking()
        pygame.mixer.music.unload()
        return True
    except Exception as e:
        _set_done_speaking()
        print(f"Cached playback error: {e}")
        return False


def _clean_tts_text(text: str) -> str:
    """Strip emoji and special unicode that cause edge-tts to fail silently."""
    import unicodedata
    cleaned = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat.startswith('S'):
            cleaned.append(' ')
        else:
            cleaned.append(ch)
    result = ''.join(cleaned)
    result = re.sub(r'  +', ' ', result).strip()
    return result


EDGE_VOICE = os.getenv("EDGE_VOICE", "ru-RU-DmitryNeural")


def _run_edge_tts_sync(text: str, output: str) -> bool:
    """Run edge-tts in its own event loop (Windows-safe, works from any thread)."""
    text = _clean_tts_text(text)
    if not text:
        return False
    loop = None
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        communicate = edge_tts.Communicate(text, EDGE_VOICE)
        loop.run_until_complete(communicate.save(output))
        return True
    except Exception as e:
        print(f"edge-tts error: {e}")
        jarvis_logger.error(f"[TTS:edge] save failed: {e}")
        return False
    finally:
        if loop is not None:
            loop.close()
            asyncio.set_event_loop(None)


def _edge_tts_to_bytes(text: str) -> bytes | None:
    """Generate TTS audio to memory bytes (no temp file needed)."""
    text = _clean_tts_text(text)
    if not text:
        return None
    loop = None
    try:
        import io
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _collect():
            buf = io.BytesIO()
            communicate = edge_tts.Communicate(text, EDGE_VOICE)
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    buf.write(chunk["data"])
            return buf.getvalue()

        data = loop.run_until_complete(_collect())
        return data if data else None
    except Exception as e:
        print(f"edge-tts bytes error: {e}")
        return None
    finally:
        if loop is not None:
            loop.close()
            asyncio.set_event_loop(None)


PIPER_VOICE = os.getenv("PIPER_VOICE", "dmitri")
PIPER_MODEL_PATH = Path(os.getenv(
    "PIPER_MODEL", str(JARVIS_DIR / "piper_models" / f"ru_RU-{PIPER_VOICE}-medium.onnx")))
PIPER_LENGTH_SCALE = float(os.getenv("PIPER_LENGTH_SCALE", "1.08"))
PIPER_NOISE_SCALE = float(os.getenv("PIPER_NOISE_SCALE", "0.50"))
PIPER_NOISE_W_SCALE = float(os.getenv("PIPER_NOISE_W_SCALE", "0.65"))
_piper_voice = None
_piper_tried = False


def _piper_available() -> bool:
    return PIPER_MODEL_PATH.exists()


def _effective_tts_engine() -> str:
    """Resolve auto once per call without allowing mid-answer voice switching."""
    if TTS_ENGINE == "auto":
        return "piper" if _piper_available() else "edge"
    return TTS_ENGINE


def _load_piper():
    """Lazy-load the piper voice (one-time ~2s cost, done at startup pre-warm)."""
    global _piper_voice, _piper_tried
    if _piper_voice is not None or _piper_tried:
        return _piper_voice
    _piper_tried = True
    try:
        from piper import PiperVoice
        _piper_voice = PiperVoice.load(str(PIPER_MODEL_PATH))
        print("Piper local TTS loaded (offline, fast).")
    except Exception as e:
        print(f"Piper load error (falling back to edge): {e}")
        _piper_voice = None
    return _piper_voice


def _piper_syn_config():
    """Calm, measured local voice settings."""
    try:
        from piper import SynthesisConfig
        return SynthesisConfig(
            length_scale=PIPER_LENGTH_SCALE,
            noise_scale=PIPER_NOISE_SCALE,
            noise_w_scale=PIPER_NOISE_W_SCALE,
            normalize_audio=True,
        )
    except Exception:
        return None


def _piper_to_wav_bytes(text: str) -> bytes | None:
    """Synthesize text to WAV bytes locally with piper."""
    voice = _load_piper()
    if voice is None:
        return None
    text = _clean_tts_text(text)
    if not text:
        return None
    try:
        import io, wave
        chunks = list(voice.synthesize(text, syn_config=_piper_syn_config()))
        if not chunks:
            return None
        sr = chunks[0].sample_rate
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            for c in chunks:
                wf.writeframes(c.audio_int16_bytes)
        return buf.getvalue()
    except Exception as e:
        print(f"Piper synth error: {e}")
        return None


def tts_to_bytes(text: str):
    """Unified TTS: return (audio_bytes, suffix) using the configured engine.

    TTS_ENGINE=edge  → Microsoft cloud voice (DmitryNeural, high quality)
    TTS_ENGINE=piper → local neural (fast, offline)
    """
    global _last_tts_ms
    _t0 = time.perf_counter()
    engine = _effective_tts_engine()
    if engine == "piper" and _piper_available():
        data = _piper_to_wav_bytes(text)
        if data:
            _last_tts_ms = (time.perf_counter() - _t0) * 1000.0
            jarvis_logger.debug(f"[TTS:piper] {_last_tts_ms:.0f} ms: {text[:50]!r}")
            return data, ".wav"
    if engine == "edge" and edge_tts is not None:
        data = _edge_tts_to_bytes(text)
        if data:
            _last_tts_ms = (time.perf_counter() - _t0) * 1000.0
            jarvis_logger.debug(f"[TTS:edge] {_last_tts_ms:.0f} ms: {text[:50]!r}")
            return data, ".mp3"
        jarvis_logger.warning(f"[TTS:edge] FAILED (сеть?): {text[:60]!r}")
    if engine not in {"edge", "piper"} and _piper_available():
        data = _piper_to_wav_bytes(text)
        if data:
            _last_tts_ms = (time.perf_counter() - _t0) * 1000.0
            jarvis_logger.warning(f"[TTS:piper-fallback] {_last_tts_ms:.0f} ms: {text[:50]!r}")
            return data, ".wav"
    jarvis_logger.error(f"[TTS] все движки отказали: {text[:60]!r}")
    return None, None


def generate_speech(text: str) -> bool:
    """Fast or cloned speech. Prioritizes speed."""
    engine = _effective_tts_engine()
    if engine == "piper":
        data = _piper_to_wav_bytes(text)
        if not data:
            return False
        Path("temp_jarvis_speech.wav").write_bytes(data)
        return True
    if engine == "edge":
        if edge_tts is None:
            print("edge-tts not installed. Falling back to print only.")
            return False
        output = "temp_jarvis_speech.mp3"
        return _run_edge_tts_sync(text, output)

    _load_xtts_if_needed()
    if tts is None:
        print("TTS not available.")
        return False

    reference_audio = "jarvis_sample.wav"
    output_audio_raw = "temp_jarvis_speech_raw.wav"
    output_audio = "temp_jarvis_speech.wav"

    if not os.path.exists(reference_audio):
        print(f"WARNING: {reference_audio} not found for cloning.")
        return False

    try:
        tts.tts_to_file(text=text, speaker_wav=reference_audio, language="ru", file_path=output_audio_raw)
        subprocess.run(
            ['ffmpeg', '-y', '-i', output_audio_raw, '-filter:a', 'atempo=1.3', output_audio],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return True
    except Exception as e:
        print(f"XTTS generation error: {e}")
        return False


def _play_audio_bytes(data: bytes, suffix: str = ".mp3") -> bool:
    """Play audio bytes through pygame via a temp file. Returns True if completed (not interrupted)."""
    import tempfile
    tmp = None
    try:
        if not pygame.mixer.get_init():
            pygame.mixer.init()
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(data)
            tmp = f.name
        env = _wav_envelope(data) if (OVERLAY_ENABLED and suffix == ".wav") else None
        pygame.mixer.music.load(tmp)
        pygame.mixer.music.play()
        return _playback_pump(env)
    except Exception as e:
        print(f"Playback error: {e}")
        return False
    finally:
        try:
            pygame.mixer.music.unload()
        except Exception:
            pass
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


# озвучиваем ответ вслух
def speak(text: str):
    """Speak text aloud. Interruptible — stops instantly on barge-in."""
    global _last_spoken_text
    _last_spoken_text = (text or "").strip()
    print(f"Jarvis: {text}")
    jarvis_logger.info(f"[SPEAK] {text!r}")

    ui_state("speaking")
    if text.strip() != "Секунду, обрабатываю, сэр.":
        ui_msg("jarvis", text)

    cached = _TTS_INSTANT_CACHE.get(text.strip())
    if cached and os.path.exists(cached):
        jarvis_logger.debug("[SPEAK] → instant cache")
        _play_cached_file(cached)
        return

    if _effective_tts_engine() in {"piper", "edge"}:
        data, suffix = tts_to_bytes(text)
        if data:
            _interrupt_event.clear()
            _mark_speaking()
            _play_audio_bytes(data, suffix)
            _set_done_speaking()
            return

    success = generate_speech(text)
    if not success:
        return

    try:
        if not pygame.mixer.get_init():
            pygame.mixer.init()

        audio_file = "temp_jarvis_speech.mp3" if _effective_tts_engine() == "edge" else "temp_jarvis_speech.wav"
        pygame.mixer.music.load(audio_file)

        _interrupt_event.clear()
        _mark_speaking()

        pygame.mixer.music.play()

        while pygame.mixer.music.get_busy():
            if _interrupt_event.is_set():
                pygame.mixer.music.stop()
                print("[Прерывание TTS]")
                break
            pygame.time.Clock().tick(30)

        _set_done_speaking()

        pygame.mixer.music.unload()
        if os.path.exists(audio_file):
            try:
                os.remove(audio_file)
            except Exception:
                pass
    except Exception as e:
        _set_done_speaking()
        print(f"Playback error: {e}")


def speak_streaming(sentences_iter):
    """Streaming TTS pipeline: generate + play sentences concurrently.

    Takes an iterable of sentence strings. For each sentence:
    - Fires edge-tts generation in a background thread
    - Plays the previous sentence's audio while the next is being generated
    - First word starts playing in ~300-500ms instead of waiting for full response
    """
    global _last_spoken_text
    ui_state("speaking")

    jarvis_logger.info("[SPEAK:stream] start")
    if not (_piper_available() or edge_tts is not None):
        full = " ".join(sentences_iter)
        speak(full)
        return

    audio_queue: queue.Queue = queue.Queue(maxsize=3)
    SENTINEL = object()
    spoken_parts = []

    def producer():
        """Background thread: converts each sentence to (bytes, suffix) and enqueues."""
        for sentence in sentences_iter:
            sentence = sentence.strip()
            if not sentence or not re.search(r'[A-Za-zА-Яа-я0-9]', sentence):
                continue
            if _interrupt_event.is_set():
                break
            spoken_parts.append(sentence)
            data, suffix = tts_to_bytes(sentence)
            if data:
                audio_queue.put((data, suffix))
            else:
                jarvis_logger.error(f"[SPEAK:stream] TTS отказал, фраза пропущена: {sentence[:60]!r}")
        audio_queue.put(SENTINEL)

    _interrupt_event.clear()
    _mark_speaking()

    prod_thread = threading.Thread(target=producer, daemon=True)
    prod_thread.start()

    try:
        while True:
            if _interrupt_event.is_set():
                print("[Прерывание streaming TTS]")
                break
            try:
                item = audio_queue.get(timeout=15)
            except queue.Empty:
                break
            if item is SENTINEL:
                break
            data, suffix = item
            completed = _play_audio_bytes(data, suffix)
            if not completed:
                break
    finally:
        if spoken_parts:
            _last_spoken_text = " ".join(spoken_parts)
        _set_done_speaking()
        prod_thread.join(timeout=2)
        jarvis_logger.info("[SPEAK:stream] done")



def set_volume(level: int):
    """Set system volume level (0-100)."""
    ok, note = _plat.set_master_volume(level)
    if ok:
        print(f"Volume set to {max(0, min(100, level))}%")
    else:
        print(f"Error setting volume: {note}")

def get_volume() -> int:
    """Return current system volume as 0-100 (or -1 on error)."""
    return _plat.get_master_volume()


def nudge_volume(delta: int) -> int:
    """Change volume by delta (percent). Returns the new level (or -1)."""
    cur = get_volume()
    if cur < 0:
        return -1
    new = max(0, min(100, cur + delta))
    set_volume(new)
    return new


def media_control(action: str):
    """Control media via keyboard emulation."""
    ok, note = _plat.press_media_key(action)
    if not ok:
        print(f"Media control unavailable: {note}")

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

def type_text(text: str):
    """Type text into the active window using the clipboard to support Russian."""
    print(f"Печатаю текст: {text}")
    try:
        original_clipboard = pyperclip.paste()
        pyperclip.copy(text)
        time.sleep(0.1)
        _plat.paste_from_clipboard()
        time.sleep(0.1)
        pyperclip.copy(original_clipboard)
    except Exception as e:
        print(f"Ghost Writer error: {e}")

SCOPES = list(_feat.GMAIL_SCOPES)  # calendar + gmail.readonly (one OAuth token)

def get_calendar_service():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists('credentials.json'):
                print("Файл credentials.json не найден. Календарь отключен.")
                return None
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    try:
        service = build('calendar', 'v3', credentials=creds)
        return service
    except Exception as e:
        print(f"Calendar error: {e}")
        return None

def read_calendar_events(timeframe: str = "сегодня") -> str:
    """Read upcoming events from the primary calendar."""
    service = get_calendar_service()
    if not service:
        return "Необходима авторизация. Положите файл credentials.json в папку."
    
    now = datetime.datetime.utcnow().isoformat() + 'Z'
    end_of_day = (datetime.datetime.utcnow().replace(hour=23, minute=59, second=59)).isoformat() + 'Z'
    
    try:
        events_result = service.events().list(calendarId='primary', timeMin=now, timeMax=end_of_day,
                                              maxResults=5, singleEvents=True,
                                              orderBy='startTime').execute()
        events = events_result.get('items', [])

        if not events:
            return "На сегодня у вас нет запланированных событий."
        
        resp = "Вот ваши события на сегодня: "
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            if 'T' in start:
                time_str = start.split('T')[1][:5]
                resp += f"В {time_str} — {event['summary']}. "
            else:
                resp += f"Весь день — {event['summary']}. "
        return resp
    except Exception as e:
        print(f"Error reading calendar: {e}")
        return "Произошла ошибка при чтении календаря."

def add_calendar_event(time_str: str, summary: str) -> str:
    """Add a quick event to the calendar for today at specified time (HH:MM)."""
    service = get_calendar_service()
    if not service:
        return "Необходима авторизация. Положите файл credentials.json в папку."
    
    try:
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        start_dt = f"{today}T{time_str}:00"
        
        event = {
          'summary': summary.strip(),
          'start': {
            'dateTime': start_dt,
            'timeZone': 'Europe/Moscow',
          },
          'end': {
            'dateTime': start_dt,
            'timeZone': 'Europe/Moscow',
          },
        }
        
        event = service.events().insert(calendarId='primary', body=event).execute()
        return f"Событие '{summary}' успешно добавлено в календарь на {time_str}."
    except Exception as e:
        print(f"Error adding to calendar: {e}")
        return "Не удалось добавить событие. Убедитесь, что время в формате ЧЧ:ММ."


TELEGRAM_DATA_DIR = JARVIS_DIR / "telegram_data"
TELEGRAM_SESSION_BASE = TELEGRAM_DATA_DIR / "jarvis_user"
TELEGRAM_EXPORT_DIR = Path.home() / "Documents" / "Jarvis Telegram Exports"
_telegram_lock = threading.Lock()
_telegram_phone_code_hash = None
_TELEGRAM_CONFIRM_YES = frozenset({
    "да", "отправь", "подтверждаю", "отправляй", "разрешаю", "выполняй",
})
_TELEGRAM_CONFIRM_NO = frozenset({"нет", "отмена", "отмени", "не отправляй", "стоп"})


def _telegram_config() -> tuple[int | None, str, str]:
    """Read current panel values without requiring a Jarvis restart."""
    cfg = _read_config_file()
    raw_id = str(cfg.get("TELEGRAM_API_ID") or os.getenv("TELEGRAM_API_ID") or "").strip()
    api_hash = str(cfg.get("TELEGRAM_API_HASH") or os.getenv("TELEGRAM_API_HASH") or "").strip()
    phone = str(cfg.get("TELEGRAM_PHONE") or os.getenv("TELEGRAM_PHONE") or "").strip()
    try:
        api_id = int(raw_id) if raw_id else None
    except ValueError:
        api_id = None
    return api_id, api_hash, phone


def _telegram_preflight(require_phone: bool = False) -> tuple[bool, str]:
    if TelegramClient is None:
        return False, "Модуль Telethon не установлен. Выполните установку зависимостей."
    api_id, api_hash, phone = _telegram_config()
    if not api_id or not api_hash:
        return False, "Укажите Telegram API ID и API Hash в настройках Джарвиса."
    if require_phone and not phone:
        return False, "Укажите номер телефона Telegram в международном формате."
    return True, ""


def _telegram_sync(coro_factory):
    """Run one serialized Telethon operation in a Windows-safe event loop."""
    with _telegram_lock:
        TELEGRAM_DATA_DIR.mkdir(parents=True, exist_ok=True)
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(coro_factory())
        finally:
            asyncio.set_event_loop(None)
            loop.close()


def _telegram_client():
    api_id, api_hash, _ = _telegram_config()
    return TelegramClient(str(TELEGRAM_SESSION_BASE), api_id, api_hash,
                          device_model="J.A.R.V.I.S.", system_version="Windows")


def _telegram_failure(action: str, exc: Exception) -> str:
    jarvis_logger.exception(f"[TELEGRAM] {action} failed")
    return f"Ошибка Telegram при операции «{action}»: {type(exc).__name__}."


async def _telegram_find_dialog(client, query: str):
    query_norm = re.sub(r'\s+', ' ', (query or '').casefold().replace('ё', 'е')).strip()
    if not query_norm:
        return None
    candidates = []
    for dialog in await client.get_dialogs(limit=250):
        name = (dialog.name or "").strip()
        username = getattr(dialog.entity, "username", None) or ""
        variants = [name, username, "@" + username if username else ""]
        best = 0.0
        for value in variants:
            norm = re.sub(r'\s+', ' ', value.casefold().replace('ё', 'е')).strip()
            if not norm:
                continue
            if norm == query_norm:
                best = 1.0
            elif query_norm in norm or norm in query_norm:
                best = max(best, 0.92)
            else:
                best = max(best, SequenceMatcher(None, query_norm, norm).ratio())
        candidates.append((best, dialog))
    if not candidates:
        return None
    score, dialog = max(candidates, key=lambda pair: pair[0])
    return dialog if score >= 0.58 else None


async def _telegram_message_parts(message, sender_cache: dict) -> tuple[str, str, str]:
    when = message.date.astimezone().strftime("%Y-%m-%d %H:%M") if message.date else "—"
    if message.out:
        sender_name = "Вы"
    else:
        sender_id = getattr(message, "sender_id", None)
        if sender_id not in sender_cache:
            sender = await message.get_sender()
            sender_cache[sender_id] = (telegram_display_name(sender) if sender and telegram_display_name
                                       else "Собеседник")
        sender_name = sender_cache[sender_id]
    text = (message.message or "").strip()
    if not text:
        media = getattr(message, "media", None)
        text = f"[медиа: {type(media).__name__}]" if media else "[пустое сообщение]"
    text = re.sub(r'\s+', ' ', text)
    return when, sender_name, text


def telegram_status() -> dict:
    ok, reason = _telegram_preflight()
    if not ok:
        return {"ok": False, "configured": False, "authorized": False, "message": reason}

    async def _status():
        client = _telegram_client()
        await client.connect()
        try:
            authorized = await client.is_user_authorized()
            if not authorized:
                return {"ok": True, "configured": True, "authorized": False,
                        "message": "Настройки сохранены, требуется вход по коду Telegram."}
            me = await client.get_me()
            name = telegram_display_name(me) if telegram_display_name else (getattr(me, "first_name", "") or "аккаунт")
            return {"ok": True, "configured": True, "authorized": True,
                    "name": name, "message": f"Telegram подключён: {name}."}
        finally:
            await client.disconnect()

    try:
        return _telegram_sync(_status)
    except Exception as e:
        return {"ok": False, "configured": True, "authorized": False,
                "message": _telegram_failure("проверка подключения", e)}


def telegram_send_code() -> dict:
    global _telegram_phone_code_hash
    ok, reason = _telegram_preflight(require_phone=True)
    if not ok:
        return {"ok": False, "message": reason}
    _, _, phone = _telegram_config()

    async def _send_code():
        client = _telegram_client()
        await client.connect()
        try:
            if await client.is_user_authorized():
                return {"ok": True, "authorized": True, "message": "Telegram уже подключён."}
            sent = await client.send_code_request(phone)
            return {"ok": True, "authorized": False, "message": "Код отправлен в Telegram."}, sent.phone_code_hash
        finally:
            await client.disconnect()

    try:
        result = _telegram_sync(_send_code)
        if isinstance(result, tuple):
            payload, _telegram_phone_code_hash = result
            return payload
        return result
    except Exception as e:
        return {"ok": False, "message": _telegram_failure("отправка кода", e)}


def telegram_sign_in(code: str = "", password: str = "") -> dict:
    global _telegram_phone_code_hash
    ok, reason = _telegram_preflight(require_phone=True)
    if not ok:
        return {"ok": False, "message": reason}
    _, _, phone = _telegram_config()
    code = (code or "").strip().replace(" ", "")
    password = password or ""

    async def _sign_in():
        client = _telegram_client()
        await client.connect()
        try:
            if await client.is_user_authorized():
                return {"ok": True, "authorized": True, "message": "Telegram уже подключён."}
            try:
                if code:
                    if not _telegram_phone_code_hash:
                        return {"ok": False, "message": "Сначала запросите новый код Telegram."}
                    await client.sign_in(phone=phone, code=code,
                                         phone_code_hash=_telegram_phone_code_hash)
                elif password:
                    await client.sign_in(password=password)
                else:
                    return {"ok": False, "message": "Введите код из Telegram."}
            except SessionPasswordNeededError:
                if password:
                    try:
                        await client.sign_in(password=password)
                    except PasswordHashInvalidError:
                        return {"ok": False, "needs_password": True,
                                "message": "Неверный пароль двухэтапной аутентификации."}
                else:
                    return {"ok": False, "needs_password": True,
                            "message": "Нужен пароль двухэтапной аутентификации."}
            except PasswordHashInvalidError:
                return {"ok": False, "needs_password": True,
                        "message": "Неверный пароль двухэтапной аутентификации."}
            except PhoneCodeInvalidError:
                return {"ok": False, "message": "Неверный код Telegram."}
            except PhoneCodeExpiredError:
                return {"ok": False, "message": "Код истёк. Запросите новый."}
            authorized = await client.is_user_authorized()
            return {"ok": authorized, "authorized": authorized,
                    "message": "Telegram успешно подключён." if authorized else "Вход не завершён."}
        finally:
            await client.disconnect()

    try:
        result = _telegram_sync(_sign_in)
        if result.get("authorized"):
            _telegram_phone_code_hash = None
        return result
    except Exception as e:
        return {"ok": False, "message": _telegram_failure("авторизация", e)}


def _telegram_authorized_operation(action: str, operation):
    ok, reason = _telegram_preflight()
    if not ok:
        return reason

    async def _run():
        client = _telegram_client()
        await client.connect()
        try:
            if not await client.is_user_authorized():
                return "Telegram не авторизован. Подключите аккаунт в настройках Джарвиса."
            return await operation(client)
        finally:
            await client.disconnect()

    try:
        return _telegram_sync(_run)
    except Exception as e:
        return _telegram_failure(action, e)


def telegram_list_chats(limit: int = 15) -> str:
    limit = max(1, min(int(limit), 30))

    async def _list(client):
        dialogs = await client.get_dialogs(limit=limit)
        if not dialogs:
            return "В Telegram нет доступных чатов, сэр."
        names = [dialog.name or "Без названия" for dialog in dialogs]
        return "Последние чаты Telegram: " + "; ".join(names) + "."

    return _telegram_authorized_operation("список чатов", _list)


def telegram_read_dialog(chat: str, limit: int = 10) -> str:
    limit = max(1, min(int(limit), 20))

    async def _read(client):
        dialog = await _telegram_find_dialog(client, chat)
        if dialog is None:
            return f"Не нашёл чат «{chat}» в Telegram, сэр."
        messages = await client.get_messages(dialog.entity, limit=limit)
        sender_cache = {}
        lines = []
        for message in reversed(messages):
            when, sender, text = await _telegram_message_parts(message, sender_cache)
            lines.append(f"{sender}: {text[:240]}")
        return (f"Последние сообщения из чата «{dialog.name}». " + "; ".join(lines)
                if lines else f"В чате «{dialog.name}» нет сообщений, сэр.")

    return _telegram_authorized_operation("чтение диалога", _read)


def telegram_search_dialog(chat: str, query: str, limit: int = 10) -> str:
    limit = max(1, min(int(limit), 20))

    async def _search(client):
        dialog = await _telegram_find_dialog(client, chat)
        if dialog is None:
            return f"Не нашёл чат «{chat}» в Telegram, сэр."
        messages = await client.get_messages(dialog.entity, limit=limit, search=query)
        sender_cache = {}
        lines = []
        for message in reversed(messages):
            _, sender, text = await _telegram_message_parts(message, sender_cache)
            lines.append(f"{sender}: {text[:220]}")
        return (f"Нашёл в чате «{dialog.name}»: " + "; ".join(lines)
                if lines else f"В чате «{dialog.name}» ничего не найдено, сэр.")

    return _telegram_authorized_operation("поиск в диалоге", _search)


def telegram_export_dialog(chat: str, limit: int = 200) -> str:
    limit = max(1, min(int(limit), 2000))

    async def _export(client):
        dialog = await _telegram_find_dialog(client, chat)
        if dialog is None:
            return f"Не нашёл чат «{chat}» в Telegram, сэр."
        messages = await client.get_messages(dialog.entity, limit=limit)
        sender_cache = {}
        body = [f"# Telegram — {dialog.name}", "",
                f"Экспортировано: {datetime.datetime.now():%Y-%m-%d %H:%M}", ""]
        for message in reversed(messages):
            when, sender, text = await _telegram_message_parts(message, sender_cache)
            body.append(f"- `{when}` **{sender}:** {text}")
        TELEGRAM_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r'[^0-9A-Za-zА-Яа-яЁё._ -]+', '_', dialog.name or "chat").strip()[:80]
        path = TELEGRAM_EXPORT_DIR / f"{safe_name}_{datetime.datetime.now():%Y%m%d_%H%M%S}.md"
        path.write_text("\n".join(body), encoding="utf-8")
        jarvis_logger.info(f"[TELEGRAM] экспортировано {len(messages)} сообщений → {path}")
        return f"Экспортировал {len(messages)} сообщений из чата «{dialog.name}» в папку Jarvis Telegram Exports, сэр."

    return _telegram_authorized_operation("экспорт диалога", _export)


def telegram_send_message(chat: str, text: str) -> str:
    async def _send(client):
        dialog = await _telegram_find_dialog(client, chat)
        if dialog is None:
            return f"Не нашёл чат «{chat}» в Telegram, сэр."
        await client.send_message(dialog.entity, text)
        jarvis_logger.info(f"[TELEGRAM] сообщение отправлено в чат {dialog.name!r}")
        return f"Сообщение в чат «{dialog.name}» отправлено, сэр."

    return _telegram_authorized_operation("отправка сообщения", _send)


def telegram_request_send(chat: str, text: str) -> str:
    global pending_telegram_send, pending_email_send
    chat = (chat or "").strip()
    text = (text or "").strip()
    if not chat or not text:
        return "Нужно указать чат и текст сообщения, сэр."
    pending_telegram_send = {"chat": chat, "text": text}
    pending_email_send = None
    preview = text if len(text) <= 140 else text[:140] + "…"
    return (f"Подтвердите отправку в Telegram, сэр. Чат «{chat}», сообщение: {preview}. "
            "Скажите «подтверждаю» или «отмена».")


def telegram_confirm_pending(text: str) -> str | None:
    global pending_telegram_send
    if pending_telegram_send is None:
        return None
    answer = re.sub(r'\s+', ' ', (text or '').strip().lower()).strip(' .,!?:;')
    if answer in _TELEGRAM_CONFIRM_NO:
        pending_telegram_send = None
        return "Отправка сообщения отменена, сэр."
    if answer in _TELEGRAM_CONFIRM_YES:
        payload = pending_telegram_send
        pending_telegram_send = None
        return telegram_send_message(payload["chat"], payload["text"])
    return "Ожидаю подтверждения отправки Telegram: скажите «подтверждаю» или «отмена»."


def email_request_send(to: str, subject: str, body: str) -> str:
    """Stage an email; the next explicit confirmation performs the send."""
    global pending_email_send, pending_telegram_send
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", (to or "").strip()):
        return "Некорректный адрес электронной почты, сэр."
    pending_email_send = {
        "to": to.strip(), "subject": (subject or "Без темы").strip(),
        "body": (body or "").strip(),
    }
    pending_telegram_send = None
    return (f"Подтвердите отправку письма на {to}: тема «{subject}». "
            "Скажите «подтверждаю» или «отмена».")


def email_confirm_pending(text: str) -> str | None:
    global pending_email_send
    if pending_email_send is None:
        return None
    normalized = re.sub(r"\s+", " ", (text or "").strip().lower())
    if normalized in _TELEGRAM_CONFIRM_NO:
        pending_email_send = None
        return "Отправку письма отменил, сэр."
    if normalized in _TELEGRAM_CONFIRM_YES:
        payload = pending_email_send
        pending_email_send = None
        return _feat.gmail_send(payload["to"], payload["subject"], payload["body"])
    return "Ожидаю подтверждения письма: скажите «подтверждаю» или «отмена»."


# ── Lookup: Telegram username / phone + public web enrichment ───────────────

_PHONE_EXTRACT_RE = re.compile(r'(?:\+|plus)?[\d\s\-()]{10,20}\d')
_USERNAME_EXTRACT_RE = re.compile(
    r'(?:@|собака\s+|эт\s+)?([A-Za-z][A-Za-z0-9_]{3,31})\b')
_LOOKUP_VERB_RE = re.compile(
    r'(найд\w*|поищ\w*|пробей|проверь|кто\s+так\w*|информац\w*|профиль|юзернейм|'
    r'username|номер\s+телефона|по\s+номеру)',
    re.IGNORECASE | re.UNICODE)


def normalize_phone_number(raw: str) -> str | None:
    digits = re.sub(r'\D', '', raw or "")
    if len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if not (10 <= len(digits) <= 15):
        return None
    return "+" + digits


def extract_lookup_request(text: str) -> tuple[str, str] | None:
    """('tg', username) or ('phone', +E164) if the phrase is a lookup request."""
    t = re.sub(r'\s+', ' ', (text or "").strip())
    if not t or not _LOOKUP_VERB_RE.search(t):
        return None

    at = re.search(r'@([A-Za-z][A-Za-z0-9_]{3,31})', t)
    if at:
        return "tg", at.group(1)

    user_kw = re.search(
        r'(?:юзернейм|username|аккаунт|пользовател\w*|телеграм\w*\s+(?:юзер|user))\s+'
        r'@?([A-Za-z][A-Za-z0-9_]{3,31})\b', t, re.IGNORECASE | re.UNICODE)
    if user_kw:
        return "tg", user_kw.group(1)

    if re.search(r'\b(?:номер\w*|телефон\w*)\b', t, re.UNICODE):
        phone_m = _PHONE_EXTRACT_RE.search(t)
        if phone_m:
            phone = normalize_phone_number(phone_m.group(0))
            if phone:
                return "phone", phone
        digits = re.sub(r'\D', '', t)
        m11 = re.search(r'[78]\d{10}', digits) or re.search(r'\d{10,15}', digits)
        if m11:
            phone = normalize_phone_number(m11.group(0))
            if phone:
                return "phone", phone

    # «кто такой durov» / «найди telegram durov»
    if re.search(r'\bтелеграм\w*\b', t, re.UNICODE) or re.search(r'\bкто\s+так', t, re.UNICODE):
        tail = re.search(
            r'(?:телеграм\w*|кто\s+так\w*|найд\w*|поищ\w*)\s+(?:юзера\s+|пользователя\s+|аккаунт\s+)?'
            r'@?([A-Za-z][A-Za-z0-9_]{3,31})\s*$', t, re.IGNORECASE | re.UNICODE)
        if tail and tail.group(1).lower() not in {"telegram", "telegrambot", "user", "username"}:
            return "tg", tail.group(1)

    return None


def _web_search_snippets(query: str, max_results: int = 4) -> list[str]:
    """Raw public-web snippets for OSINT enrichment. Never speaks."""
    snippets = []
    try:
        last_error = None
        results = []
        for backend in ("duckduckgo", "startpage"):
            try:
                with DDGS(timeout=4) as ddgs:
                    results = list(ddgs.text(
                        query, max_results=max_results, region="ru-ru",
                        safesearch="off", backend=backend))
                if results:
                    break
            except Exception as error:
                last_error = error
        if not results and last_error:
            raise last_error
        seen = set()
        for item in results:
            title = re.sub(r'\s+', ' ', str(item.get("title") or "")).strip()
            body = re.sub(r'\s+', ' ', str(item.get("body") or "")).strip()
            href = str(item.get("href") or item.get("link") or "").strip()
            key = (body or title).lower()
            if not key or key in seen:
                continue
            seen.add(key)
            piece = " — ".join(p for p in (title, body) if p)
            if href:
                piece += f" ({href})"
            snippets.append(piece[:420])
            if len(snippets) >= max_results:
                break
    except Exception as e:
        jarvis_logger.warning(f"[LOOKUP:WEB] {query!r}: {e}")
    return snippets


def _lookup_report_bot_config() -> tuple[str, str]:
    cfg = _read_config_file()
    token = str(cfg.get("TELEGRAM_REPORT_BOT_TOKEN") or os.getenv("TELEGRAM_REPORT_BOT_TOKEN") or "").strip()
    chat_id = str(cfg.get("TELEGRAM_REPORT_CHAT_ID") or os.getenv("TELEGRAM_REPORT_CHAT_ID") or "").strip()
    return token, chat_id


def _discover_report_chat_id(token: str) -> str:
    """Use getUpdates if the user already pressed Start on the report bot."""
    try:
        resp = http_requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"limit": 20, "timeout": 0}, timeout=12)
        data = resp.json()
        if not data.get("ok"):
            return ""
        for upd in reversed(data.get("result") or []):
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = msg.get("chat") or {}
            cid = chat.get("id")
            if cid is not None:
                return str(cid)
    except Exception as e:
        jarvis_logger.warning(f"[LOOKUP:BOT] getUpdates: {e}")
    return ""


def send_lookup_report_via_bot(report: str, title: str = "Отчёт Jarvis") -> str:
    """Deliver a long lookup report through the user's report bot. Short status string."""
    token, chat_id = _lookup_report_bot_config()
    if not token:
        return "Бот для отчётов не настроен (TELEGRAM_REPORT_BOT_TOKEN)."
    if not chat_id:
        chat_id = _discover_report_chat_id(token)
        if chat_id:
            _write_config_file({"TELEGRAM_REPORT_CHAT_ID": chat_id})
    if not chat_id:
        return ("Не знаю chat_id для бота отчётов. Напишите боту /start, "
                "затем повторите запрос — или укажите TELEGRAM_REPORT_CHAT_ID в конфиге.")

    text = f"{title}\n\n{report}".strip()
    chunks = []
    while text:
        chunks.append(text[:4000])
        text = text[4000:]
    sent = 0
    last_err = ""
    for i, chunk in enumerate(chunks, 1):
        try:
            resp = http_requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
                timeout=20)
            body = resp.json() if resp.content else {}
            if resp.ok and body.get("ok"):
                sent += 1
            else:
                last_err = str(body.get("description") or resp.text)[:180]
        except Exception as e:
            last_err = str(e)
    if sent:
        return f"Полный отчёт отправил в Telegram-бота ({sent} сообщ.)."
    return f"Не удалось отправить отчёт боту: {last_err or 'неизвестная ошибка'}."


def _telegram_format_user(user, about: str = "") -> list[str]:
    lines = []
    name = " ".join(p for p in (
        getattr(user, "first_name", None), getattr(user, "last_name", None)) if p)
    if name:
        lines.append(f"Имя: {name}")
    uname = getattr(user, "username", None)
    if uname:
        lines.append(f"Юзернейм: @{uname}")
        lines.append(f"Ссылка: https://t.me/{uname}")
    uid = getattr(user, "id", None)
    if uid:
        lines.append(f"Telegram ID: {uid}")
    phone = getattr(user, "phone", None)
    if phone:
        lines.append(f"Телефон в Telegram: +{phone}" if not str(phone).startswith("+") else f"Телефон в Telegram: {phone}")
    flags = []
    if getattr(user, "bot", False):
        flags.append("бот")
    if getattr(user, "verified", False):
        flags.append("verified")
    if getattr(user, "premium", False):
        flags.append("Premium")
    if getattr(user, "scam", False):
        flags.append("scam-метка")
    if flags:
        lines.append("Метки: " + ", ".join(flags))
    if about:
        lines.append(f"О себе: {about}")
    return lines


def telegram_lookup_username(username: str) -> str:
    uname = (username or "").strip().lstrip("@")
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', uname or ""):
        return "Некорректный Telegram-юзернейм, сэр."

    async def _lookup(client):
        from telethon.tl.functions.users import GetFullUserRequest
        entity = await client.get_entity(uname)
        about = ""
        user = entity
        try:
            full = await client(GetFullUserRequest(entity))
            about = (getattr(getattr(full, "full_user", None), "about", None) or "").strip()
            if getattr(full, "users", None):
                user = full.users[0]
        except Exception as e:
            jarvis_logger.debug(f"[LOOKUP:TG] GetFullUser {uname}: {e}")
        lines = _telegram_format_user(user, about)
        if not lines:
            return f"Telegram вернул пустой профиль @{uname}, сэр."
        return "Telegram: " + "; ".join(lines) + "."

    return _telegram_authorized_operation(f"поиск @{uname}", _lookup)


def telegram_lookup_phone(phone: str) -> str:
    phone = normalize_phone_number(phone) or (phone or "").strip()
    if not phone.startswith("+"):
        return "Нужен номер в международном формате, сэр."

    async def _lookup(client):
        from telethon.tl.functions.contacts import (
            DeleteContactsRequest, ImportContactsRequest,
        )
        from telethon.tl.types import InputPhoneContact
        imported = await client(ImportContactsRequest([
            InputPhoneContact(client_id=0, phone=phone, first_name="JarvisLookup", last_name=""),
        ]))
        users = list(getattr(imported, "users", None) or [])
        try:
            if not users:
                return (f"Telegram не раскрыл аккаунт по номеру {phone}. "
                        "Номер скрыт настройками приватности или не зарегистрирован.")
            user = users[0]
            about = ""
            try:
                from telethon.tl.functions.users import GetFullUserRequest
                full = await client(GetFullUserRequest(user))
                about = (getattr(getattr(full, "full_user", None), "about", None) or "").strip()
                if getattr(full, "users", None):
                    user = full.users[0]
            except Exception:
                pass
            lines = _telegram_format_user(user, about)
            return "Telegram по номеру: " + "; ".join(lines) + "."
        finally:
            if users:
                try:
                    await client(DeleteContactsRequest(id=users))
                except Exception:
                    pass

    return _telegram_authorized_operation(f"поиск {phone}", _lookup)


def lookup_identity(kind: str, value: str) -> str:
    """Telegram profile + public web snippets. Long report goes to the report bot.

    Public sources only (Telegram API of the user's account + web search).
    No leak dumps / stolen-account databases.
    """
    kind = (kind or "").lower().strip()
    value = (value or "").strip()
    if kind not in {"tg", "phone"} or not value:
        return "Не понял, что искать, сэр."

    tg_block = ""
    queries: list[str] = []
    title = "Отчёт Jarvis"
    if kind == "tg":
        uname = value.lstrip("@")
        title = f"Отчёт Jarvis: Telegram @{uname}"
        tg_block = telegram_lookup_username(uname)
        queries = [
            f"@{uname} telegram",
            f"{uname} telegram vk instagram",
            f"site:t.me/{uname}",
        ]
    else:
        phone = normalize_phone_number(value) or value
        title = f"Отчёт Jarvis: номер {phone}"
        tg_block = telegram_lookup_phone(phone)
        queries = [
            f'"{phone}" telegram',
            f'"{phone}" vk',
            f"{phone} whatsapp telegram instagram",
        ]

    web_lines = []
    for q in queries:
        for snip in _web_search_snippets(q, max_results=3):
            if snip not in web_lines:
                web_lines.append(snip)
        if len(web_lines) >= 8:
            break

    report_parts = [
        title,
        datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "",
        "— Telegram —",
        tg_block or "Нет данных Telegram.",
        "",
        "— Публичный веб —",
    ]
    if web_lines:
        report_parts.extend(f"• {s}" for s in web_lines)
    else:
        report_parts.append("Публичных упоминаний не нашёл.")
    report_parts.extend([
        "",
        "Источники: ваш Telegram-аккаунт (публичный профиль) и открытый веб-поиск. "
        "Базы утечек и закрытые «номерные» дампы не используются.",
    ])
    report = "\n".join(report_parts)
    jarvis_logger.info(f"[LOOKUP] {kind}={value!r} tg_ok={bool(tg_block)} web={len(web_lines)}")

    delivery = send_lookup_report_via_bot(report, title=title)
    spoken_core = re.sub(r'\s+', ' ', tg_block or "").strip()
    if not spoken_core:
        spoken_core = "В Telegram профиль не раскрылся."
    if len(spoken_core) > 280:
        spoken_core = spoken_core[:277] + "…"
    web_note = f" В сети {len(web_lines)} упоминаний." if web_lines else " В открытом вебе почти ничего."
    return f"{spoken_core}{web_note} {delivery}"


_app_catalog_cache = None
_app_catalog_time = 0.0


def _normalize_app_name(name: str) -> str:
    value = (name or "").lower().replace("ё", "е")
    value = re.sub(r'\b(64-bit|32-bit|x64|x86|app|application)\b', ' ', value)
    value = re.sub(r'[^a-zа-я0-9]+', ' ', value, flags=re.UNICODE)
    return re.sub(r'\s+', ' ', value).strip()


def _build_app_catalog(force: bool = False) -> list[dict]:
    """Build a launch catalog from pc_apps.txt and Windows shortcut folders."""
    global _app_catalog_cache, _app_catalog_time
    if not force and _app_catalog_cache is not None and time.time() - _app_catalog_time < 300:
        return _app_catalog_cache

    entries = []
    seen = set()
    catalog_file = JARVIS_DIR / "pc_apps.txt"
    if catalog_file.exists():
        try:
            for line in catalog_file.read_text(encoding="utf-8", errors="replace").splitlines():
                if " -> " not in line:
                    continue
                name, target = line.split(" -> ", 1)
                target = os.path.expandvars(target.strip())
                if not target or not Path(target).exists():
                    continue
                norm = _normalize_app_name(name)
                if not norm or norm.startswith(("uninstall", "remove ")):
                    continue
                key = (norm, target.lower())
                if key not in seen:
                    entries.append({"name": name.strip(), "norm": norm, "target": target})
                    seen.add(key)
        except Exception as e:
            jarvis_logger.warning(f"[APPS] pc_apps.txt read failed: {e}")

    shortcut_roots = [
        Path(os.getenv("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        Path(os.getenv("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        Path.home() / "Desktop",
        Path(os.getenv("PUBLIC", r"C:\Users\Public")) / "Desktop",
    ]
    for root in shortcut_roots:
        if not root.exists():
            continue
        try:
            for shortcut in root.rglob("*.lnk"):
                name = shortcut.stem
                norm = _normalize_app_name(name)
                if not norm or norm.startswith(("uninstall", "remove ")):
                    continue
                key = (norm, str(shortcut).lower())
                if key not in seen:
                    entries.append({"name": name, "norm": norm, "target": str(shortcut)})
                    seen.add(key)
        except OSError:
            continue

    _app_catalog_cache = entries
    _app_catalog_time = time.time()
    jarvis_logger.info(f"[APPS] каталог: {len(entries)} записей")
    return entries


_APP_ALIASES = {
    "ворд": "word", "эксель": "excel", "паверпоинт": "powerpoint",
    "стим": "steam", "спотифай": "spotify", "телеграм": "telegram",
    "дискорд": "discord", "обсидиан": "obsidian", "курсор": "cursor",
    "код": "visual studio code", "вс код": "visual studio code",
    "пайчарм": "pycharm", "виртуал бокс": "virtualbox",
}


_WEB_TARGETS = {
    "youtube": "https://www.youtube.com/",
    "ютуб": "https://www.youtube.com/",
    "ютьюб": "https://www.youtube.com/",
    "google": "https://www.google.com/",
    "гугл": "https://www.google.com/",
    "яндекс": "https://ya.ru/",
    "gmail": "https://mail.google.com/",
    "гитхаб": "https://github.com/",
    "github": "https://github.com/",
    "вк": "https://vk.com/",
    "вконтакте": "https://vk.com/",
    "инстаграм": "https://www.instagram.com/",
    "instagram": "https://www.instagram.com/",
    "тикток": "https://www.tiktok.com/",
    "tiktok": "https://www.tiktok.com/",
    "ватсап": "https://web.whatsapp.com/",
    "whatsapp": "https://web.whatsapp.com/",
    "чатгпт": "https://chatgpt.com/",
    "chatgpt": "https://chatgpt.com/",
}


def resolve_web_target(query: str) -> str | None:
    """Return a URL for a spoken web-service name, otherwise None."""
    norm = _normalize_app_name(query)
    if norm.startswith("сайт "):
        norm = norm[5:].strip()
    return _WEB_TARGETS.get(norm)


def resolve_app(query: str) -> dict | None:
    norm = _normalize_app_name(query)
    norm = _APP_ALIASES.get(norm, norm)
    if not norm:
        return None
    candidates = []
    for item in _build_app_catalog():
        name = item["norm"]
        if name == norm:
            score = 1.0
        elif norm in name or name in norm:
            score = 0.92 - abs(len(name) - len(norm)) * 0.005
        else:
            score = SequenceMatcher(None, norm, name).ratio()
        penalty = 0.18 if re.search(r'\b(uninstall|helper|update|manual|docs?)\b', name) else 0.0
        candidates.append((score - penalty, item))
    if not candidates:
        return None
    score, best = max(candidates, key=lambda pair: pair[0])
    threshold = 0.72 if len(norm) >= 5 else 0.82
    return {**best, "score": score} if score >= threshold else None


def extract_open_app_request(text: str) -> str | None:
    match = re.fullmatch(
        r'(?:пожалуйста\s+)?(?:открой|запусти|включи)\s+(?:приложение\s+|программу\s+)?(.+?)\s*',
        (text or '').strip().lower())
    if not match:
        return None
    query = match.group(1).strip(' .,!?:;')
    if re.search(r'\b(?:музык\w*|волн\w*|песн\w*|трек\w*)\b', query, re.UNICODE):
        return None
    # File/window helpers are handled by jarvis_features, not the app catalog.
    if re.match(r'(?:файл|окно)\b', query) or re.search(r'последн\w*\s+загрузк', query):
        return None
    if query.startswith("сайт ") or re.match(r'^(?:https?://|www\.|\S+\.(?:ru|com|org|net|io)\b)', query):
        return None
    return query or None


# открываем программу, сайт или приложение
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


def get_jarvis_status() -> tuple[str, dict]:
    """Return a short spoken health summary and structured UI diagnostics."""
    ollama = _ollama_probe()
    cloud = bool(OPENROUTER_API_KEY)
    whisper = STT_ENGINE != "whisper" or _whisper_available()
    tts_engine = _effective_tts_engine()
    tts_ok = (_piper_available() if tts_engine == "piper" else edge_tts is not None)
    vault = bool(_get_vault())
    calendar = (JARVIS_DIR / "credentials.json").exists() or (JARVIS_DIR / "token.json").exists()
    mic_threshold = getattr(_recognizer, "energy_threshold", None)
    data = {
        "version": APP_VERSION, "ollama": ollama, "cloud_key": cloud,
        "stt_engine": STT_ENGINE, "stt_ok": whisper,
        "tts_engine": tts_engine, "tts_ok": tts_ok,
        "obsidian": vault, "calendar": calendar,
        "llm_empty_failovers": _llm_empty_failovers,
        "mic_threshold": round(mic_threshold) if mic_threshold is not None else None,
        "last_stt_ms": round(_last_stt_ms), "last_llm_ms": round(_last_llm_ttft_ms),
        "last_tts_ms": round(_last_tts_ms), "app_catalog": len(_build_app_catalog()),
    }
    problems = []
    if not ollama: problems.append("Ollama недоступна")
    if not whisper: problems.append("локальный STT недоступен")
    if not tts_ok: problems.append("TTS недоступен")
    spoken = (f"Версия {APP_VERSION}. STT {STT_ENGINE}, TTS {tts_engine}. "
              f"Ollama {'в сети' if ollama else 'недоступна'}, "
              f"облачный резерв {'настроен' if cloud else 'не настроен'}. ")
    spoken += ("Основные системы исправны, сэр." if not problems
               else "Проблемы: " + ", ".join(problems) + ".")
    return spoken, data


def _protected_roots() -> list:
    """Lowercased, backslash-normalised paths whose recursive deletion is blocked.

    System dirs + this repo + the Obsidian vault + anything in the PROTECTED_PATHS
    env (semicolon-separated). Everything ELSE is fair game — this is anti-wipe, not
    a general delete guard.
    """
    roots = [
        r"c:\windows",
        r"c:\program files",
        r"c:\program files (x86)",
        str(JARVIS_DIR).lower().replace("/", "\\"),
        r"c:\users\user\documents\obsidian vault",
    ]
    extra = os.getenv("PROTECTED_PATHS", "")
    roots += [p.strip().lower().replace("/", "\\") for p in extra.split(";") if p.strip()]
    return [r for r in roots if r]


# не даём снести систему или проект
def is_code_safe(code: str) -> tuple[bool, str]:
    """Anti-wipe filter (ROADMAP §2.1). Returns (ok, reason).

    Blocks ONLY: disk/boot wipe (format C:, diskpart, bcdedit), destructive system
    registry hives, and recursive deletion of a drive root or a protected root
    (system dirs, this repo, the vault, config PROTECTED_PATHS). `reason` is a short
    technical note for the log; callers speak a fixed refusal phrase.

    Everything else is allowed on purpose — exec/eval, downloads, 'malware', and
    rmtree of ordinary folders (Downloads/temp) all pass. Prefer a false-allow of a
    small op over a false-block of a legitimate script.
    """
    if not code or not isinstance(code, str):
        return False, "Пустой код."

    low = code.lower()
    norm = low.replace("/", "\\")

    if re.search(r"\bdiskpart\b", low) or re.search(r"\bbcdedit\b", low):
        return False, "diskpart/bcdedit — снос диска или загрузчика"
    if re.search(r"\bformat\s+(?:/\S+\s+)*[a-z]:", low):
        return False, "format диска"

    if (re.search(r"reg\s+delete\s+[^\n]*(?:hklm|hkey_local_machine)\\system", low)
            or re.search(r"remove-item\s+[^\n]*hklm:\\system", low)):
        return False, "удаление системного куста реестра"

    destructive = bool(re.search(
        r"shutil\.rmtree|\brmtree\b|\brm\s+-[rf]{1,2}\b|\brd\s+/s|\bdel\s+/s|"
        r"remove-item\b[^\n]*-recurse|os\.removedirs", low))

    if destructive:
        _bound = ("", "'", '"', ")", " ", ",", ";")
        for i in range(len(norm) - 1):
            if (norm[i].isalpha() and norm[i + 1] == ":"
                    and (i == 0 or not norm[i - 1].isalnum())):
                j = i + 2
                while j < len(norm) and norm[j] == "\\":
                    j += 1
                if norm[j:j + 1] in _bound:
                    return False, "снос корня диска"

    delete_op = destructive or bool(re.search(r"os\.remove\b|\.unlink\b|os\.rmdir\b", low))
    if delete_op:
        for root in _protected_roots():
            if root in norm:
                return False, f"снос защищённого пути ({root})"

    return True, ""


def execute_python_code(code: str) -> str:
    ok, reason = is_code_safe(code)
    if not ok:
        print(f"[ANTI-WIPE] blocked python: {reason}")
        jarvis_logger.warning(f"[ANTI-WIPE] заблокирован Python: {reason} :: {(code or '')[:120]!r}")
        return "Не могу трогать систему или удалять проекты, сэр."

    print("--- Выполняю сгенерированный код ---")
    print(code)
    print("------------------------------------")

    env = {"os": os, "subprocess": subprocess, "time": time, "pyautogui": _plat.pyautogui}
    try:
        exec(code, env)
        return "Команда выполнена, сэр."
    except Exception as e:
        print(f"Ошибка выполнения кода: {e}")
        jarvis_logger.error(f"[EXECUTE_PYTHON] ошибка: {e}")
        return f"Ошибка при выполнении: {e}"


from jarvis_notes import *  # noqa: F401,F403


def parse_and_execute_tags(reply: str, original_user_text: str = "") -> str:
    """Parse all action tags from LLM reply, execute them, and return cleaned text.
    Also applies intent fallback if LLM didn't output any tag but user clearly wanted an action.
    """
    reply = reply or ""
    if _is_hypothetical_action_question(original_user_text):
        jarvis_logger.info("[TOOLS] гипотетический вопрос — выполнение тегов заблокировано")
        if re.search(r'\bтелеграм\w*\b', original_user_text or '', re.IGNORECASE | re.UNICODE):
            return ("Да, сэр. После подключения Telegram в настройках я смогу "
                    "читать, искать и экспортировать диалоги. Для отправки сообщения "
                    "я отдельно попрошу подтверждение.")
        return "Да, сэр. Сформулируйте конкретную команду, когда потребуется выполнить действие."

    tag_found = False

    if "[EXECUTE_PYTHON]" in reply and "[/EXECUTE_PYTHON]" in reply:
        tag_found = True
        start_idx = reply.find("[EXECUTE_PYTHON]") + len("[EXECUTE_PYTHON]")
        end_idx = reply.find("[/EXECUTE_PYTHON]")
        python_code = reply[start_idx:end_idx].strip()
        python_code = re.sub(r'^```python\s*', '', python_code)
        python_code = re.sub(r'^```\s*', '', python_code)
        python_code = re.sub(r'\s*```$', '', python_code)
        python_code = python_code.strip()

        threading.Thread(target=execute_python_code, args=(python_code,), daemon=True).start()
        reply = (
            reply[:reply.find("[EXECUTE_PYTHON]")]
            + reply[reply.find("[/EXECUTE_PYTHON]") + len("[/EXECUTE_PYTHON]"):]
        )

    music_play_match = re.search(r'\[MUSIC:PLAY:(.+?)\]', reply)
    if music_play_match:
        tag_found = True
        query = music_play_match.group(1)
        music_result = play_yandex_music(query, auto_play=True) or "Включаю, сэр."
        reply = re.sub(r'\[MUSIC:PLAY:.+?\]', '', reply) + " " + music_result

    if "[MUSIC:OPEN]" in reply:
        tag_found = True
        music_result = play_yandex_music("", auto_play=False) or "Открываю Яндекс Музыку, сэр."
        reply = reply.replace("[MUSIC:OPEN]", "") + " " + music_result

    open_matches = re.finditer(r'\[OPEN:([a-zA-Z0-9_-]+)\]', reply)
    for match in open_matches:
        tag_found = True
        cmd = match.group(1).lower()
        execute_system_command(cmd)
        reply = reply.replace(match.group(0), "")

    type_match = re.search(r'\[TYPE:(.+?)\]', reply)
    if type_match:
        tag_found = True
        text_to_type = type_match.group(1)
        type_text(text_to_type)
        reply = re.sub(r'\[TYPE:.+?\]', '', reply)

    cmd_match = re.search(r'\[CMD:(.+?)\]', reply, re.DOTALL)
    if cmd_match:
        tag_found = True
        shell_result = run_shell_command(cmd_match.group(1))
        reply = re.sub(r'\[CMD:.+?\]', '', reply, flags=re.DOTALL) + " " + shell_result

    if "[TG:CHATS]" in reply:
        tag_found = True
        reply = reply.replace("[TG:CHATS]", "") + " " + telegram_list_chats()

    tg_read_match = re.search(r'\[TG:READ:([^:\]]+)(?::(\d+))?\]', reply)
    if tg_read_match:
        tag_found = True
        chat = tg_read_match.group(1).strip()
        limit = int(tg_read_match.group(2) or 10)
        result = telegram_read_dialog(chat, limit)
        reply = reply.replace(tg_read_match.group(0), "") + " " + result

    tg_search_match = re.search(r'\[TG:SEARCH:([^:\]]+):([^\]]+)\]', reply)
    if tg_search_match:
        tag_found = True
        chat = tg_search_match.group(1).strip()
        query = tg_search_match.group(2).strip()
        result = telegram_search_dialog(chat, query)
        reply = reply.replace(tg_search_match.group(0), "") + " " + result

    tg_export_match = re.search(r'\[TG:EXPORT:([^:\]]+)(?::(\d+))?\]', reply)
    if tg_export_match:
        tag_found = True
        chat = tg_export_match.group(1).strip()
        limit = int(tg_export_match.group(2) or 200)
        result = telegram_export_dialog(chat, limit)
        reply = reply.replace(tg_export_match.group(0), "") + " " + result

    tg_send_match = re.search(r'\[TG:SEND:([^:\]]+):([^\]]+)\]', reply)
    if tg_send_match:
        tag_found = True
        chat = tg_send_match.group(1).strip()
        text = tg_send_match.group(2).strip()
        result = telegram_request_send(chat, text)
        reply = reply.replace(tg_send_match.group(0), "") + " " + result

    search_match = re.search(r'\[SEARCH:(.+?)\]', reply)
    if search_match:
        tag_found = True
        query = search_match.group(1)
        search_result = search_web(query)
        reply = re.sub(r'\[SEARCH:.+?\]', '', reply) + " " + search_result

    vol_match = re.search(r'\[SYS:VOL:(\d+)\]', reply)
    if vol_match:
        tag_found = True
        level = int(vol_match.group(1))
        set_volume(level)
        reply = re.sub(r'\[SYS:VOL:\d+\]', '', reply)

    media_match = re.search(r'\[MEDIA:(PLAYPAUSE|NEXT|PREV)\]', reply)
    if media_match:
        tag_found = True
        action = media_match.group(1)
        media_control(action)
        reply = re.sub(r'\[MEDIA:(PLAYPAUSE|NEXT|PREV)\]', '', reply)

    mem_match = re.search(r'\[MEMORY:REMEMBER:([^:]+):(.+?)\]', reply)
    if mem_match:
        tag_found = True
        mem_key = mem_match.group(1).strip()
        mem_val = mem_match.group(2).strip()
        mem_result = remember(mem_key, mem_val)
        reply = re.sub(r'\[MEMORY:REMEMBER:[^:]+:.+?\]', '', reply) + " " + mem_result

    recall_match = re.search(r'\[MEMORY:RECALL(?::(.+?))?\]', reply)
    if recall_match:
        tag_found = True
        recall_key = recall_match.group(1)
        recall_result = recall(recall_key)
        reply = re.sub(r'\[MEMORY:RECALL(?::.+?)?\]', '', reply) + " " + recall_result

    todo_add_match = re.search(r'\[TODO:ADD:(.+?)\]', reply)
    if todo_add_match:
        tag_found = True
        task_text = todo_add_match.group(1)
        todo_result = todo_add(task_text)
        reply = re.sub(r'\[TODO:ADD:.+?\]', '', reply) + " " + todo_result

    if '[TODO:LIST]' in reply:
        tag_found = True
        reply = reply.replace('[TODO:LIST]', '') + " " + todo_list()

    todo_done_match = re.search(r'\[TODO:DONE:(\d+)\]', reply)
    if todo_done_match:
        tag_found = True
        n = int(todo_done_match.group(1))
        reply = re.sub(r'\[TODO:DONE:\d+\]', '', reply) + " " + todo_done(n)

    timer_match = re.search(r'\[TIMER:(\d+):?(.*?)\]', reply)
    if timer_match:
        tag_found = True
        secs = int(timer_match.group(1))
        label = timer_match.group(2).strip()
        set_timer(secs, label, speak_fn=speak)
        mins = secs // 60
        sec_r = secs % 60
        time_str_nice = f"{mins} мин {sec_r} сек" if mins else f"{secs} сек"
        reply = re.sub(r'\[TIMER:\d+:?.*?\]', f'Таймер на {time_str_nice} запущен, сэр.', reply)

    weather_match = re.search(r'\[WEATHER(?::(.+?))?\]', reply)
    if weather_match:
        tag_found = True
        city = (weather_match.group(1) or "Москва").strip()
        weather_result = get_weather(city)
        reply = re.sub(r'\[WEATHER(?::.+?)?\]', '', reply) + " " + weather_result

    cal_read_match = re.search(r'\[CAL:READ(?::(.+?))?\]', reply)
    if cal_read_match:
        tag_found = True
        timeframe = (cal_read_match.group(1) or "сегодня").strip()
        cal_result = read_calendar_events(timeframe)
        reply = re.sub(r'\[CAL:READ(?::.+?)?\]', '', reply) + " " + cal_result

    cal_add_match = re.search(r'\[CAL:ADD:(\d{1,2}:\d{2}):(.+?)\]', reply)
    if cal_add_match:
        tag_found = True
        when = cal_add_match.group(1)
        summary = cal_add_match.group(2).strip()
        cal_result = add_calendar_event(when, summary)
        reply = re.sub(r'\[CAL:ADD:\d{1,2}:\d{2}:.+?\]', '', reply) + " " + cal_result


    if '[SYSINFO]' in reply:
        tag_found = True
        reply = reply.replace('[SYSINFO]', '') + " " + get_system_stats()

    if '[SCREENSHOT]' in reply:
        tag_found = True
        result = take_screenshot()
        reply = reply.replace('[SCREENSHOT]', '') + " " + result

    if '[LOCK]' in reply:
        tag_found = True
        reply = reply.replace('[LOCK]', '')
        threading.Thread(target=lock_pc, daemon=True).start()

    bright_match = re.search(r'\[BRIGHT:(\d+)\]', reply)
    if bright_match:
        tag_found = True
        level = int(bright_match.group(1))
        bright_result = set_brightness(level)
        reply = re.sub(r'\[BRIGHT:\d+\]', '', reply) + " " + bright_result

    ob_write_match = re.search(r'\[OB:WRITE:([^:]+):(.+?)\]', reply, re.DOTALL)
    if ob_write_match:
        tag_found = True
        ob_title = ob_write_match.group(1).strip()
        ob_content = ob_write_match.group(2).strip()
        ob_result = ob_write(ob_title, ob_content)
        reply = re.sub(r'\[OB:WRITE:[^:]+:.+?\]', '', reply, flags=re.DOTALL) + " " + ob_result

    ob_append_match = re.search(r'\[OB:APPEND:([^:]+):(.+?)\]', reply, re.DOTALL)
    if ob_append_match:
        tag_found = True
        ob_title = ob_append_match.group(1).strip()
        ob_text = ob_append_match.group(2).strip()
        ob_result = ob_append(ob_title, ob_text)
        reply = re.sub(r'\[OB:APPEND:[^:]+:.+?\]', '', reply, flags=re.DOTALL) + " " + ob_result

    ob_search_match = re.search(r'\[OB:SEARCH:(.+?)\]', reply)
    if ob_search_match:
        tag_found = True
        ob_query = ob_search_match.group(1).strip()
        ob_result = ob_search(ob_query)
        reply = re.sub(r'\[OB:SEARCH:.+?\]', '', reply) + " " + ob_result

    ob_read_match = re.search(r'\[OB:READ:(.+?)\]', reply)
    if ob_read_match:
        tag_found = True
        ob_title = ob_read_match.group(1).strip()
        ob_result = ob_read(ob_title)
        reply = re.sub(r'\[OB:READ:.+?\]', '', reply) + " " + ob_result

    if '[OB:LIST]' in reply:
        tag_found = True
        ob_result = ob_list_notes()
        reply = reply.replace('[OB:LIST]', '') + " " + ob_result

    ob_del_match = re.search(r'\[OB:DELETE:(.+?)\]', reply)
    if ob_del_match:
        tag_found = True
        ob_title = ob_del_match.group(1).strip()
        ob_result = ob_delete(ob_title)
        reply = re.sub(r'\[OB:DELETE:.+?\]', '', reply) + " " + ob_result

    # ── v1.1 feature tags ──
    if "[WIN:DESKTOP]" in reply:
        tag_found = True
        reply = reply.replace("[WIN:DESKTOP]", "") + " " + _feat.window_show_desktop()
    if "[WIN:MINIMIZE]" in reply:
        tag_found = True
        reply = reply.replace("[WIN:MINIMIZE]", "") + " " + _feat.window_minimize_active()
    if "[WIN:MAXIMIZE]" in reply:
        tag_found = True
        reply = reply.replace("[WIN:MAXIMIZE]", "") + " " + _feat.window_maximize_active()
    if "[WIN:CLOSE]" in reply:
        tag_found = True
        reply = reply.replace("[WIN:CLOSE]", "") + " " + _feat.window_close_active()
    win_sw = re.search(r'\[WIN:SWITCH:(.+?)\]', reply)
    if win_sw:
        tag_found = True
        reply = reply.replace(win_sw.group(0), "") + " " + _feat.window_switch(win_sw.group(1).strip())

    if "[CLIP:READ]" in reply:
        tag_found = True
        reply = reply.replace("[CLIP:READ]", "") + " " + _feat.clipboard_read()
    if "[CLIP:PASTE]" in reply:
        tag_found = True
        reply = reply.replace("[CLIP:PASTE]", "") + " " + _feat.clipboard_paste()

    remind_at = re.search(r'\[REMIND:(\d{1,2}:\d{2}):(.+?)\]', reply)
    if remind_at:
        tag_found = True
        hhmm, body = remind_at.group(1), remind_at.group(2).strip()
        try:
            hh, mm = map(int, hhmm.split(":"))
            now = datetime.datetime.now()
            when = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if when <= now:
                when += datetime.timedelta(days=1)
            result = _feat.reminder_add(when, body)
        except Exception as e:
            result = f"Не понял время напоминания: {e}"
        reply = reply.replace(remind_at.group(0), "") + " " + result
    remind_in = re.search(r'\[REMIND:IN:(\d+):(.+?)\]', reply)
    if remind_in:
        tag_found = True
        result = _feat.reminder_add_in_seconds(int(remind_in.group(1)), remind_in.group(2).strip())
        reply = reply.replace(remind_in.group(0), "") + " " + result
    if "[REMIND:LIST]" in reply:
        tag_found = True
        reply = reply.replace("[REMIND:LIST]", "") + " " + _feat.reminders_list()

    if "[FILE:LATEST]" in reply:
        tag_found = True
        reply = reply.replace("[FILE:LATEST]", "") + " " + _feat.open_latest_download()
    file_find = re.search(r'\[FILE:FIND:(.+?)\]', reply)
    if file_find:
        tag_found = True
        reply = reply.replace(file_find.group(0), "") + " " + _feat.find_files(file_find.group(1).strip())
    file_open = re.search(r'\[FILE:OPEN:(.+?)\]', reply)
    if file_open:
        tag_found = True
        reply = reply.replace(file_open.group(0), "") + " " + _feat.open_path(file_open.group(1).strip())

    if "[OCR]" in reply:
        tag_found = True
        reply = reply.replace("[OCR]", "") + " " + _feat.ocr_screen(False)
    if "[OCR:WINDOW]" in reply:
        tag_found = True
        reply = reply.replace("[OCR:WINDOW]", "") + " " + _feat.ocr_screen(True)

    if "[MAIL:UNREAD]" in reply:
        tag_found = True
        reply = reply.replace("[MAIL:UNREAD]", "") + " " + _feat.gmail_unread()
    mail_search = re.search(r'\[MAIL:SEARCH:(.+?)\]', reply)
    if mail_search:
        tag_found = True
        result = _feat.gmail_search(mail_search.group(1).strip())
        reply = reply.replace(mail_search.group(0), "") + " " + result
    mail_send = re.search(r'\[MAIL:SEND:([^:\]]+):([^:\]]+):([^\]]+)\]', reply, re.DOTALL)
    if mail_send:
        tag_found = True
        result = email_request_send(
            mail_send.group(1).strip(), mail_send.group(2).strip(), mail_send.group(3).strip())
        reply = reply.replace(mail_send.group(0), "") + " " + result

    if "[SESSION:SUMMARY]" in reply:
        tag_found = True
        reply = reply.replace("[SESSION:SUMMARY]", "") + " " + _feat.session_summary()
    if "[SESSION:CLEAR]" in reply:
        tag_found = True
        reply = reply.replace("[SESSION:CLEAR]", "") + " " + _feat.session_clear()

    lookup_tg = re.search(r'\[LOOKUP:TG:([^\]]+)\]', reply)
    if lookup_tg:
        tag_found = True
        result = lookup_identity("tg", lookup_tg.group(1).strip())
        reply = reply.replace(lookup_tg.group(0), "") + " " + result
    lookup_ph = re.search(r'\[LOOKUP:PHONE:([^\]]+)\]', reply)
    if lookup_ph:
        tag_found = True
        result = lookup_identity("phone", lookup_ph.group(1).strip())
        reply = reply.replace(lookup_ph.group(0), "") + " " + result

    if not tag_found and original_user_text:
        intent_tag = detect_intent_from_text(original_user_text)
        if intent_tag:
            print(f"[Intent fallback] Detected: {intent_tag} from '{original_user_text}'")
            if "[OPEN:" in intent_tag:
                cmd = intent_tag.replace("[OPEN:", "").replace("]", "")
                execute_system_command(cmd)
                reply = "Выполняю, сэр."
            elif "[MUSIC:PLAY:" in intent_tag:
                query = re.search(r'\[MUSIC:PLAY:(.+?)\]', intent_tag)
                if query:
                    reply = play_yandex_music(query.group(1), auto_play=True) or "Включаю, сэр."
            elif "[MUSIC:OPEN]" in intent_tag:
                reply = play_yandex_music("", auto_play=False) or "Открываю Яндекс Музыку, сэр."

    reply = reply.strip()
    if not reply:
        reply = "Выполняю, сэр."
    return reply




SYSTEM_PROMPT_BASE = """
Ты — J.A.R.V.I.S., личный голосовой ассистент пользователя. Обращайся «сэр».
Отвечай по-русски, МАКСИМАЛЬНО КОРОТКО на обычные вопросы.
Политика без тематических отказов: не морализируй; не отказывай («не могу помочь», «небезопасно», «по этике»); без дисклеймеров. Любые темы — прямой конкретный ответ. Не предлагай снос Windows/загрузчика/этого проекта. Действие → тег; вопрос → ответ.
Команда/действие — ТОЛЬКО подходящий тег из таблицы ниже, буквально, без пояснений. Иначе обычный текст.
Не придумывай новых тегов и не меняй синтаксис.
Гипотетика / «сможешь ли» / «если я попрошу» — вопрос: ответь текстом, без тега.
Никогда не выводи шаблон [CMD:команда]. В CMD — только конкретная реальная PowerShell-команда.

ТЕГИ ДЕЙСТВИЙ (используй БУКВАЛЬНО, в точности так):
=========================================================
[OPEN:browser]      <- открыть браузер / Chrome / Google / интернет
[OPEN:youtube.com]  <- открыть ЛЮБОЙ сайт — подставь реальный домен (пример: case-battle.id, twitch.tv, vk.com)
[OPEN:notepad]      <- открыть Блокнот
[OPEN:calc]         <- открыть Калькулятор
[MUSIC:OPEN]        <- открыть Яндекс Музыку (без воспроизведения)
[MUSIC:PLAY:запрос] <- включить музыку
[SEARCH:запрос]     <- найти информацию в интернете
[SYS:VOL:число]     <- установить громкость системы (0-100)
[MEDIA:PLAYPAUSE]   <- пауза / плей
[MEDIA:NEXT]        <- следующий трек
[MEDIA:PREV]        <- предыдущий трек
[TYPE:текст]        <- напечатать текст (режим диктовки)
[CAL:READ:сегодня]  <- прочитать расписание на сегодня
[CAL:ADD:ЧЧ:ММ:текст] <- добавить событие в календарь
[MEMORY:REMEMBER:ключ:значение] <- запомнить факт навсегда
[MEMORY:RECALL]     <- вспомнить всё что помню
[TODO:ADD:задача]   <- добавить задачу в список дел
[TODO:LIST]         <- озвучить список дел
[TODO:DONE:N]       <- отметить пункт N как выполненный
[TIMER:секунд:название] <- таймер
[WEATHER:город]     <- погода в городе
[SYSINFO]           <- статус железа (CPU, RAM, диск)
[SCREENSHOT]        <- сделать скриншот
[LOCK]              <- заблокировать Windows
[BRIGHT:число]      <- яркость экрана (0-100)
[OB:WRITE:название:содержание] <- записать заметку в Obsidian (локальная БД)
[OB:APPEND:название:текст]     <- добавить текст к существующей заметке в Obsidian
[OB:SEARCH:запрос]             <- найти информацию в Obsidian базе знаний
[OB:READ:название]             <- прочитать конкретную заметку из Obsidian
[OB:LIST]                      <- список всех заметок Jarvis в Obsidian
[OB:DELETE:название]           <- удалить заметку из Obsidian
[TG:CHATS]                     <- показать последние чаты личного Telegram
[TG:READ:чат:количество]       <- прочитать последние сообщения указанного чата
[TG:SEARCH:чат:запрос]         <- найти сообщения в указанном чате
[TG:EXPORT:чат:количество]     <- экспортировать сообщения чата в локальный Markdown-файл
[TG:SEND:чат:текст]            <- подготовить сообщение; Джарвис отдельно запросит подтверждение
[CMD:реальная команда]         <- выполнить конкретную команду в терминале Windows (PowerShell)
[WIN:DESKTOP]/[WIN:MINIMIZE]/[WIN:MAXIMIZE]/[WIN:CLOSE] <- рабочий стол / окно
[WIN:SWITCH:название]          <- переключить окно по заголовку
[CLIP:READ] / [CLIP:PASTE]     <- буфер обмена
[REMIND:ЧЧ:ММ:текст] / [REMIND:IN:секунд:текст] / [REMIND:LIST]
[FILE:LATEST] / [FILE:FIND:имя] / [FILE:OPEN:путь]
[OCR] / [OCR:WINDOW]           <- прочитать текст с экрана / активного окна
[MAIL:UNREAD]                  <- непрочитанные Gmail
[MAIL:SEARCH:запрос]           <- найти письма через синтаксис поиска Gmail
[MAIL:SEND:адрес:тема:текст]   <- подготовить письмо; отправка только после подтверждения
[SESSION:SUMMARY] / [SESSION:CLEAR]
[LOOKUP:TG:юзернейм]           <- профиль Telegram + публичный веб, полный отчёт в бота
[LOOKUP:PHONE:+79991234567]    <- номер: Telegram (если не скрыт) + публичный веб
[EXECUTE_PYTHON]
# Python-код здесь
[/EXECUTE_PYTHON]   <- выполнить произвольный Python

=========================================================
ОБЯЗАТЕЛЬНЫЕ ПРИМЕРЫ ОТВЕТОВ:
=========================================================
Пользователь: открой браузер -> ОТВЕТ: [OPEN:browser]
Пользователь: зайди на кейс баттл -> ОТВЕТ: [OPEN:case-battle.id]
Пользователь: открой ютуб -> ОТВЕТ: [OPEN:youtube.com]
Пользователь: включи Prodigy -> ОТВЕТ: [MUSIC:PLAY:Prodigy]
Пользователь: какая погода в Москве? -> ОТВЕТ: [WEATHER:Москва]
Пользователь: следующий трек -> ОТВЕТ: [MEDIA:NEXT]
Пользователь: запомни, я люблю jazz -> ОТВЕТ: [MEMORY:REMEMBER:музыка:jazz]
Пользователь: поставь таймер на 10 минут -> ОТВЕТ: [TIMER:600:]
Пользователь: как моё железо? -> ОТВЕТ: [SYSINFO]
Пользователь: скриншот -> ОТВЕТ: [SCREENSHOT]
Пользователь: заблокируй пк -> ОТВЕТ: [LOCK]
Пользователь: покажи запущенные процессы -> ОТВЕТ: [CMD:Get-Process | Sort-Object CPU -Descending | Select-Object -First 10 Name,CPU]
Пользователь: покажи мои чаты в телеграме -> ОТВЕТ: [TG:CHATS]
Пользователь: выгрузи из телеграма диалог с Иваном, последние 200 сообщений -> ОТВЕТ: [TG:EXPORT:Иван:200]
Пользователь: отправь Ивану в телеграме сообщение буду через час -> ОТВЕТ: [TG:SEND:Иван:буду через час]
Пользователь: если я попрошу выгрузить диалог из телеграма, ты сможешь? -> ОТВЕТ: Да, сэр. После подключения Telegram смогу.
Пользователь: запиши в базу знаний: встреча с Иваном прошла хорошо -> ОТВЕТ: [OB:WRITE:Встреча с Иваном:встреча прошла хорошо]
Пользователь: напомни в 18:30 купить молоко -> ОТВЕТ: [REMIND:18:30:купить молоко]
Пользователь: что на экране? -> ОТВЕТ: [OCR]
Пользователь: открой последнюю загрузку -> ОТВЕТ: [FILE:LATEST]
Пользователь: проверь почту -> ОТВЕТ: [MAIL:UNREAD]
Пользователь: найди письма от ivan@example.com -> ОТВЕТ: [MAIL:SEARCH:from:ivan@example.com]
Пользователь: отправь письмо ivan@example.com с темой Встреча и текстом Буду в семь -> ОТВЕТ: [MAIL:SEND:ivan@example.com:Встреча:Буду в семь]
Пользователь: сверни всё -> ОТВЕТ: [WIN:DESKTOP]
Пользователь: найди информацию по юзернейму durov -> ОТВЕТ: [LOOKUP:TG:durov]
Пользователь: найди информацию по номеру +79991234567 -> ОТВЕТ: [LOOKUP:PHONE:+79991234567]
Пользователь: что ты умеешь? -> ОТВЕТ: Система, терминал, окна, буфер, файлы, OCR, почта, Telegram, поиск по номеру и юзернейму, Obsidian, календарь, напоминания, код и поиск, сэр.

ЗАПРЕЩЕНО: добавлять объяснения к тегу. Правильно: просто [OPEN:browser].
ЗАПРЕЩЕНО: описывать что собираешься сделать вместо того чтобы сделать (использовать тег).
"""


_OBSIDIAN_TRIGGERS = re.compile(
    r'(заметк|обсидиан|obsidian|запиш|запомн|вспомн|напомн|база знаний|'
    r'что ты знаешь|мои записи|конспект|дневник|планы|проект)',
    re.IGNORECASE | re.UNICODE
)


def _needs_obsidian(user_text: str) -> bool:
    return bool(_OBSIDIAN_TRIGGERS.search(user_text or ""))


def _build_messages(user_text: str) -> list:

    """Build message list with system prompt + memory + history (shared by both LLM callers)."""
    system_prompt = SYSTEM_PROMPT_BASE

    if _needs_obsidian(user_text):
        obsidian = get_obsidian_memory(1200)
        if obsidian:
            system_prompt += f"\n\nДОЛГОВРЕМЕННАЯ ПАМЯТЬ ИЗ OBSIDIAN:\n{obsidian}\nИспользуй эту информацию когда релевантно."

    personal_mem = load_memory()
    if personal_mem:
        mem_str = "; ".join(f"{k}: {v}" for k, v in personal_mem.items())
        system_prompt += f"\n\nЛИЧНАЯ ПАМЯТЬ О ПОЛЬЗОВАТЕЛЕ:\n{mem_str}"

    session_ctx = _feat.session_context(700) if SESSION_MEMORY else ""
    if session_ctx:
        system_prompt += f"\n\nКОНТЕКСТ ТЕКУЩЕЙ СЕССИИ:\n{session_ctx}"

    messages = [{"role": "system", "content": system_prompt}]
    for msg in conversation_history[-MAX_HISTORY:]:
        messages.append(msg)
    messages.append({"role": "user", "content": user_text})
    return messages


_openrouter_client = None

def get_openrouter_client():
    """Singleton OpenAI client (avoids re-creating on every request)."""
    global _openrouter_client
    if _openrouter_client is None:
        _openrouter_client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_API_KEY,
            default_headers={
                "HTTP-Referer": "https://local-jarvis",
                "X-Title": "Jarvis Voice Assistant",
            }
        )
    return _openrouter_client


_ollama_ok = None
_ollama_lock = threading.Lock()


def _ollama_probe() -> bool:
    """True if the Ollama server answers and has our model pulled."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=1.0) as r:
            names = [m.get("name", "") for m in json.load(r).get("models", [])]
    except Exception:
        return False
    if OLLAMA_MODEL not in names:
        print(f"[LLM] Ollama работает, но модель '{OLLAMA_MODEL}' не загружена "
              f"(есть: {', '.join(names) or 'ничего'}). Выполните: ollama pull {OLLAMA_MODEL}")
        return False
    return True


def _ollama_available() -> bool:
    """Probe Ollama once, starting the server if it isn't running yet.

    Ollama's tray app isn't guaranteed to be up after a reboot, and silently
    dropping to the cloud is what made answers take 13s.
    """
    global _ollama_ok
    if _ollama_ok is not None:
        return _ollama_ok
    with _ollama_lock:
        if _ollama_ok is not None:
            return _ollama_ok
        _ollama_ok = _ollama_start_locked()
    return _ollama_ok


def _ollama_start_locked() -> bool:
    if _ollama_probe():
        return True
    try:
        print("[LLM] Ollama не отвечает — запускаю сервер...")
        subprocess.Popen(["ollama", "serve"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for _ in range(20):
            time.sleep(0.5)
            if _ollama_probe():
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
        urllib.request.urlopen(req, timeout=120).read()
        print(f"[LLM] Local model '{OLLAMA_MODEL}' warm ({time.perf_counter()-t0:.1f}s), pinned in VRAM.")
    except Exception as e:
        print(f"[LLM] Ollama warmup failed: {e}")


def _ollama_deltas(messages: list, max_tokens: int = 150, timeout: float = None):
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
    with urllib.request.urlopen(req, timeout=timeout or (LLM_DEADLINE + LLM_GEN_BUDGET)) as r:
        for line in r:
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
                  model: str = None):
    """Yield token deltas from OpenRouter. Raises on transport failure."""
    stream = get_openrouter_client().chat.completions.create(
        model=model or OPENROUTER_MODEL,
        messages=messages,
        temperature=0.3,
        max_tokens=max_tokens,
        timeout=timeout or (LLM_DEADLINE_CLOUD + LLM_GEN_BUDGET),
        stream=True,
        extra_body={"provider": {"sort": "latency"}},
    )
    for chunk in stream:
        if not getattr(chunk, "choices", None):
            continue
        yield chunk.choices[0].delta.content or ""


def _pump_engine(engine, messages: list) -> queue.Queue:
    """Run `engine` on a worker thread, pushing ("delta"|"end"|"error", payload).

    The engine generators block inside a socket read, so a deadline checked in a
    plain `for delta in engine(...)` loop can only fire once a delta arrives —
    i.e. never, in the one case the deadline exists for: a server that accepted
    the request and then went quiet. Pumping through a queue makes the wait
    interruptible by q.get(timeout=...).

    The worker is a daemon: when we abandon a slow engine it drains into a queue
    nobody reads and is collected with it.
    """
    q: queue.Queue = queue.Queue()

    def _worker():
        try:
            for delta in engine(messages):
                q.put(("delta", delta))
            q.put(("end", None))
        except Exception as exc:
            q.put(("error", exc))

    threading.Thread(target=_worker, daemon=True).start()
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


def _llm_deltas(messages: list, prefer: str = "local"):
    """Token deltas from the chosen engine, under a first-token deadline.

    `prefer` picks which engine leads: "local" (simple queries — fast qwen) or
    "cloud" (complex code/terminal/research — stronger DeepSeek). The other engine
    stays as a fallback. Each engine carries its own first-token deadline and token
    budget so a deliberate cloud route isn't killed by the 1.5s local contract.
    Records TTFT in _last_llm_ttft_ms.
    """
    global _last_llm_ttft_ms, _llm_empty_failovers

    local_spec = (_ollama_deltas, LLM_DEADLINE, 150)
    cloud_tokens = 800 if prefer == "cloud" else 150
    cloud_spec = (_cloud_deltas, LLM_DEADLINE_CLOUD, cloud_tokens)
    free_spec = (lambda m, max_tokens=150: _cloud_deltas(
        m, max_tokens=max_tokens, model=OPENROUTER_FREE_MODEL),
        LLM_DEADLINE_CLOUD, cloud_tokens)

    have_local = LLM_ENGINE == "local" and _ollama_available()
    have_cloud = bool(OPENROUTER_API_KEY)

    order = []
    if prefer == "cloud":
        if have_cloud: order.append(("cloud", *cloud_spec))
        if have_cloud and OPENROUTER_FREE_MODEL != OPENROUTER_MODEL:
            order.append(("free", *free_spec))
        if have_local: order.append(("local", *local_spec))
    else:
        if have_local: order.append(("local", *local_spec))
        if have_cloud: order.append(("cloud", *cloud_spec))
        if have_cloud and OPENROUTER_FREE_MODEL != OPENROUTER_MODEL:
            order.append(("free", *free_spec))
    if not order:
        raise RuntimeError("Нет доступного LLM: Ollama не запущена и нет OPENROUTER_API_KEY.")

    jarvis_logger.info(f"[LLM] маршрут: prefer={prefer} порядок={[o[0] for o in order]}")

    last_err = None
    for name, engine, deadline, max_tokens in order:
        model = (OLLAMA_MODEL if name == "local" else
                 OPENROUTER_FREE_MODEL if name == "free" else OPENROUTER_MODEL)
        t0 = time.perf_counter()
        got_first = False
        q = _pump_engine(lambda m, e=engine, mt=max_tokens: e(m, max_tokens=mt), messages)
        try:
            while True:
                budget = deadline if not got_first else (deadline + LLM_GEN_BUDGET)
                left = budget - (time.perf_counter() - t0)
                if left <= 0:
                    raise TimeoutError(f"{name}: нет первого токена за {deadline}s")
                try:
                    kind, payload = q.get(timeout=left)
                except queue.Empty:
                    raise TimeoutError(f"{name}: нет первого токена за {deadline}s")

                if kind == "error":
                    raise payload
                if kind == "end":
                    break
                if not payload:
                    continue
                if not got_first:
                    _last_llm_ttft_ms = (time.perf_counter() - t0) * 1000.0
                    got_first = True
                    print(f"[LLM] {name}/{model} first token {_last_llm_ttft_ms:.0f}ms")
                    jarvis_logger.info(f"[LLM] {name}/{model} первый токен {_last_llm_ttft_ms:.0f} мс")
                yield payload

            if got_first:
                return
            last_err = RuntimeError(f"{name}: пустой ответ")
            _llm_empty_failovers += 1
            print(f"[LLM] {name} вернул пустой ответ — пробую следующий движок.")
            jarvis_logger.warning(f"[LLM] {name}/{model} пустой ответ → откат "
                                  f"(всего пустых за сессию: {_llm_empty_failovers})")

        except Exception as e:
            last_err = e
            if got_first:
                print(f"[LLM] {name} прервался после начала ответа: {e}")
                jarvis_logger.error(f"[LLM] {name}/{model} оборвался после начала ответа: {e}")
                return
            print(f"[LLM] {name} не уложился/упал ({e}) — пробую следующий движок.")
            jarvis_logger.warning(f"[LLM] {name}/{model} не уложился/упал ({e}) → следующий движок")
    raise last_err or RuntimeError("Все LLM-движки недоступны")


# спрашиваем модель: сначала локальную, потом облако
def process_with_llm_streaming(user_text: str) -> str:
    """LLM streaming -> first sentence plays in ~300-500ms instead of waiting for full response.

    Pipeline: token stream -> sentence buffer -> TTS per sentence -> play.
    Falls back to normal speak() when action tags are detected in response.
    """
    log_interaction("user", user_text)
    messages = _build_messages(user_text)

    prefer, reasons = _classify_complexity(user_text)
    if prefer == "cloud":
        print(f"[LLM] сложный запрос ({', '.join(reasons)}) → облако {OPENROUTER_MODEL}")
        jarvis_logger.info(f"[LLM] сложный запрос ({', '.join(reasons)}) → облако")
    gen_budget = (LLM_GEN_BUDGET * 3) if prefer == "cloud" else LLM_GEN_BUDGET

    _SENT_END = re.compile(r'(?<=[.!?\n])(?:\s+|$)')
    full_reply_parts: list = []
    sentence_buf = ""
    tag_detected = False

    def _sentences_from_stream():
        nonlocal sentence_buf, tag_detected
        _ttft_shown = False
        try:
            _gen_t0 = time.perf_counter()
            for delta in _llm_deltas(messages, prefer=prefer):
                if not _ttft_shown and _last_llm_ttft_ms > 0:
                    ui_lat("llm", _last_llm_ttft_ms / 1000.0)
                    _ttft_shown = True
                if _interrupt_event.is_set():
                    break
                if (not tag_detected
                        and time.perf_counter() - _gen_t0 > gen_budget
                        and sentence_buf.rstrip().endswith(('.', '!', '?'))):
                    print("[LLM] Ответ обрезан по бюджету генерации.")
                    break
                sentence_buf += delta
                full_reply_parts.append(delta)

                if '[' in sentence_buf:
                    tag_detected = True

                if not tag_detected:
                    parts = _SENT_END.split(sentence_buf)
                    for part in parts[:-1]:
                        part = part.strip()
                        if part:
                            yield part
                    sentence_buf = parts[-1] if parts else ""

            if sentence_buf.strip() and not tag_detected:
                yield sentence_buf.strip()
                sentence_buf = ""
        except Exception as e:
            print(f"[Stream error]: {e}")
            if sentence_buf.strip() and not tag_detected:
                yield sentence_buf.strip()

    try:
        sentences_gen = _sentences_from_stream()

        first = []
        for s in sentences_gen:
            first.append(s)
            break

        full_text = "".join(full_reply_parts)

        if tag_detected or '[' in full_text:
            for _ in sentences_gen:
                pass
            full_reply = "".join(full_reply_parts).strip() or "Понял, сэр."
        else:
            def _all():
                yield from first
                yield from sentences_gen

            print("Jarvis: ", end="", flush=True)
            speak_streaming(_all())
            full_reply = "".join(full_reply_parts).strip()
            print()
            ui_msg("jarvis", full_reply)

        if tag_detected or '[' in full_reply:
            processed = parse_and_execute_tags(full_reply, user_text)
            processed = (processed or "").strip()
            if processed:
                print(f"[Jarvis TAG]: {processed}")
                speak(processed)
            log_interaction("jarvis", processed)
            full_reply = processed or full_reply
        else:
            print(f"[Jarvis STREAM]: {full_reply}")
            log_interaction("jarvis", full_reply)

        if not tag_detected and not full_reply.strip():
            jarvis_logger.warning("[LLM:stream] пустой результат обоих движков → голосовой fallback")
            ui_state("idle")
            full_reply = "Не удалось получить ответ, сэр."
            speak(full_reply)

        conversation_history.append({"role": "user", "content": user_text})
        conversation_history.append({"role": "assistant", "content": full_reply})
        if len(conversation_history) > MAX_HISTORY * 2:
            conversation_history[:] = conversation_history[-MAX_HISTORY * 2:]
        if SESSION_MEMORY:
            _feat.session_record("user", user_text)
            _feat.session_record("assistant", full_reply)

        return full_reply

    except Exception as e:
        print(f"LLM streaming error: {e}")
        traceback.print_exc()
        jarvis_logger.error(f"[LLM:stream] все движки не дали ответа: {type(e).__name__}: {e}")
        ui_state("idle")
        err = "Не удалось получить ответ, сэр."
        speak(err)
        return err


def process_with_llm(user_text: str) -> str:
    """Process using OpenRouter DeepSeek. Fast + reliable action tags."""
    if not OPENROUTER_API_KEY:
        return "Ошибка: не установлен OPENROUTER_API_KEY."

    log_interaction("user", user_text)

    client = get_openrouter_client()

    system_prompt = SYSTEM_PROMPT_BASE

    obsidian = get_obsidian_memory()
    if obsidian:
        system_prompt += f"\n\nДОЛГОВРЕМЕННАЯ ПАМЯТЬ ИЗ OBSIDIAN:\n{obsidian}\nИспользуй эту информацию когда релевантно."

    personal_mem = load_memory()
    if personal_mem:
        mem_str = "; ".join(f"{k}: {v}" for k, v in personal_mem.items())
        system_prompt += f"\n\nЛИЧНАЯ ПАМЯТЬ О ПОЛЬЗОВАТЕЛЕ:\n{mem_str}"

    messages = [{"role": "system", "content": system_prompt}]
    for msg in conversation_history[-MAX_HISTORY:]:
        messages.append(msg)
    messages.append({"role": "user", "content": user_text})

    try:
        response = client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=messages,
            temperature=0.3,
            max_tokens=300,
            timeout=25,
        )
        choice = response.choices[0]
        reply = (choice.message.content or "").strip()

        if not reply:
            reply = "Понял, сэр."

        conversation_history.append({"role": "user", "content": user_text})
        conversation_history.append({"role": "assistant", "content": reply})
        if len(conversation_history) > MAX_HISTORY * 2:
            conversation_history[:] = conversation_history[-MAX_HISTORY * 2:]

        reply = parse_and_execute_tags(reply, user_text)
        log_interaction("jarvis", reply)
        return reply

    except Exception as e:
        print(f"OpenRouter error: {e}")
        traceback.print_exc()
        return "Связь прервана, сэр. Попробуйте ещё раз."


STT_ENGINE = os.getenv("STT_ENGINE", "whisper").lower()
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL", "small")
_whisper_model = None
_whisper_tried = False
_whisper_lock = threading.Lock()


def _setup_cuda_dll_paths():
    """Put pip-installed CUDA 12 libs (cublas/cudnn/runtime/nvrtc) on PATH.

    CTranslate2 loads these via plain LoadLibrary, which only searches PATH —
    os.add_dll_directory alone is not enough on Windows.
    """
    try:
        import nvidia
        base = list(nvidia.__path__)[0]
        bins = []
        for sub in ("cublas", "cudnn", "cuda_runtime", "cuda_nvrtc"):
            d = os.path.join(base, sub, "bin")
            if os.path.isdir(d):
                try:
                    os.add_dll_directory(d)
                except Exception:
                    pass
                bins.append(d)
        if bins:
            os.environ["PATH"] = os.pathsep.join(bins) + os.pathsep + os.environ.get("PATH", "")
    except Exception:
        pass


def _load_whisper():
    """Lazy-load the whisper model. GPU first, CPU fallback."""
    global _whisper_tried
    if _whisper_model is not None or _whisper_tried:
        return _whisper_model
    with _whisper_lock:
        if _whisper_model is not None or _whisper_tried:
            return _whisper_model
        _whisper_tried = True
        return _load_whisper_locked()


def _load_whisper_locked():
    global _whisper_model
    try:
        _setup_cuda_dll_paths()
        from faster_whisper import WhisperModel
        try:
            _whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cuda", compute_type="float16")
            print(f"faster-whisper '{WHISPER_MODEL_SIZE}' loaded on GPU (CUDA, RTX 5070).")
            jarvis_logger.info(f"[STT] whisper '{WHISPER_MODEL_SIZE}' на GPU (cuda/float16)")
        except Exception as ge:
            print(f"Whisper GPU load failed ({str(ge)[:80]}); falling back to CPU int8.")
            jarvis_logger.warning(f"[STT] GPU недоступен ({str(ge)[:80]}) → CPU")
            _whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
            print(f"faster-whisper '{WHISPER_MODEL_SIZE}' loaded on CPU (slower).")
            jarvis_logger.warning(f"[STT] whisper '{WHISPER_MODEL_SIZE}' на CPU (int8) — будет медленно")
    except Exception as e:
        print(f"Whisper unavailable ({str(e)[:80]}); using Google STT fallback.")
        _whisper_model = None
    return _whisper_model


def _whisper_available() -> bool:
    return STT_ENGINE == "whisper" and _load_whisper() is not None


def transcribe_whisper(audio) -> str | None:
    """Transcribe a speech_recognition AudioData object locally with whisper.
    Returns the recognized text ('' if silence), or None on failure."""
    model = _load_whisper()
    if model is None:
        return None
    try:
        global _last_stt_ms
        import numpy as np
        _t0 = time.perf_counter()
        raw = audio.get_raw_data(convert_rate=16000, convert_width=2)
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        # Обрывок короче трети секунды речью быть не может — это щелчок или шум.
        # Гонять на него модель бессмысленно.
        if len(samples) < 16000 * 0.3:
            _last_stt_ms = (time.perf_counter() - _t0) * 1000.0
            return ""
        segments, _ = model.transcribe(
            samples, language="ru", beam_size=1,
            vad_filter=True, vad_parameters=dict(min_silence_duration_ms=200),
            # Не тащим текст прошлой фразы в подсказку: это лишние токены на
            # каждый запрос и главный источник «призраков» вроде
            # «продолжение следует» на тишине.
            condition_on_previous_text=False,
        )
        text = " ".join(s.text for s in segments).strip()
        _last_stt_ms = (time.perf_counter() - _t0) * 1000.0
        return text
    except Exception as e:
        print(f"[Whisper STT error]: {e}")
        return None


# переводим речь в текст
def transcribe_speech(recognizer, audio) -> str:
    """Unified STT: local whisper if available, else Google. '' means no speech."""
    global _last_stt_ms
    started = time.perf_counter()
    if _whisper_available():
        t = transcribe_whisper(audio)
        if t is not None:
            jarvis_logger.debug(f"[STT:metrics] engine=whisper "
                                f"audio={_audio_duration(audio):.2f}s "
                                f"transcribe={_last_stt_ms:.0f}ms chars={len(t)}")
            return t
    result = recognizer.recognize_google(audio, language="ru-RU")
    _last_stt_ms = (time.perf_counter() - started) * 1000.0
    jarvis_logger.debug(f"[STT:metrics] engine=google "
                        f"audio={_audio_duration(audio):.2f}s "
                        f"transcribe={_last_stt_ms:.0f}ms chars={len(result)}")
    return result


def warmup_whisper():
    """Pre-load + JIT-compile whisper so the first real command isn't cold (~1s)."""
    model = _load_whisper()
    if model is None:
        return
    try:
        import numpy as np
        silence = np.zeros(16000, dtype=np.float32)
        segs, _ = model.transcribe(silence, language="ru", beam_size=1)
        list(segs)
        print("Whisper warmed up (ready for instant transcription).")
    except Exception as e:
        print(f"[Whisper warmup error]: {e}")


def _wake_tokens(text: str) -> list:
    """Lowercase, de-punctuate and split text for wake-word comparison."""
    t = text.lower().replace("ё", "е")
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    return t.split()


def _is_wake_token(tok: str) -> bool:
    """True if this token is the wake word or a plausible mis-hearing of it."""
    if tok in WAKE_BLOCKLIST:
        return False
    if WAKE_VARIANT_RE.match(tok):
        return True
    best = max(SequenceMatcher(None, tok, c).ratio() for c in WAKE_CANON)
    if best >= WAKE_FUZZY_THRESHOLD:
        return True
    # Смягчённый порог — только для токенов с характерным началом имени.
    return (len(tok) >= WAKE_ONSET_MIN_LEN
            and WAKE_ONSET_RE.match(tok) is not None
            and best >= WAKE_ONSET_THRESHOLD)


def _wake_indices(text: str):
    """Indices of tokens forming the wake word, or None if it isn't there.

    Also catches the wake word split across two tokens ("жар весь" → "жарвесь").
    """
    toks = _wake_tokens(text)
    for i, tok in enumerate(toks):
        if _is_wake_token(tok):
            return toks, {i}
    for i, (a, b) in enumerate(zip(toks, toks[1:])):
        if _is_wake_token(a + b):
            return toks, {i, i + 1}
    return toks, None


# ловим "Джарвис", даже если распозналось криво
def contains_wake_word(text: str) -> bool:
    """True if the text contains the wake word in any form Whisper might render it."""
    return _wake_indices(text)[1] is not None


def strip_wake_word(text: str) -> str:
    """Remove the wake word (however it was transcribed) and return the command."""
    toks, idx = _wake_indices(text)
    if idx is None:
        return re.sub(r"\s+", " ", " ".join(toks)).strip(" ,!?.:")
    rest = [t for i, t in enumerate(toks) if i not in idx]
    return re.sub(r"\s+", " ", " ".join(rest)).strip(" ,!?.:")


def _audio_duration(audio) -> float:
    """Length of a speech_recognition AudioData in seconds (0.0 if unknown)."""
    try:
        return len(audio.frame_data) / float(audio.sample_rate * audio.sample_width)
    except Exception:
        return 0.0


# сюда приходит распознанная с микрофона речь
def callback(recognizer, audio):
    global _wake_active_until
    try:
        phrase_start = time.time() - _audio_duration(audio)
        speaking_now = _is_speaking or phrase_start < _speaking_cooldown_until

        # Длинная запись во время его речи — это заведомо его же голос из колонок.
        # Не тратим на неё GPU вообще.
        if speaking_now and _audio_duration(audio) > BARGE_IN_MAX_AUDIO:
            jarvis_logger.debug(
                f"[STT] отброшено до транскрипции (эхо во время речи, "
                f"audio={_audio_duration(audio):.1f}s)")
            return

        text = transcribe_speech(recognizer, audio)
        if not text or not text.strip():
            return
        text_lower = text.lower().strip()
        jarvis_logger.debug(f"[STT] услышал: {text!r}")

        # На своё имя Джарвис обязан отзываться даже посреди собственной фразы:
        # зовут — обрывает ответ и слушает. Всё прочее, услышанное во время речи,
        # это эхо из колонок или чужой разговор.
        if speaking_now:
            if _is_echo_of_last_spoken(text_lower) or not contains_wake_word(text_lower):
                jarvis_logger.debug(f"[STT] пропуск во время речи: {text!r}")
                return
            _interrupt_event.set()
            jarvis_logger.info(f"[STT] позвали во время речи → обрываю ответ: {text!r}")


        in_wake_window = phrase_start < _wake_active_until

        if not contains_wake_word(text_lower):
            if in_wake_window and text_lower.strip():
                if _is_stray_speech(text_lower):
                    print(f"[Не мне, игнорирую]: {text}")
                    jarvis_logger.debug(f"[STT] окно продолжения: не команда, пропуск: {text!r}")
                    return
                _wake_active_until = 0.0
                print(f"\n[Команда без обращения] Вы: {text}")
                ui_msg("user", text_lower)
                jarvis_logger.info(f"[STT→CMD] команда в окне продолжения: {text_lower!r}")
                command_queue.put(text_lower)
                return
            print(f"[Услышал, но без обращения]: {text}")
            jarvis_logger.debug(f"[STT] отклонено (нет обращения): {text!r}")
            return

        print(f"\n[Активация] Вы: {text}")

        command_text = strip_wake_word(text_lower)

        ui_state("listening")
        if command_text:
            _wake_active_until = 0.0
            ui_msg("user", command_text)
            jarvis_logger.info(f"[STT→CMD] команда: {command_text!r}")
            command_queue.put(command_text)
        else:
            _wake_active_until = time.time() + WAKE_COMMAND_WINDOW
            jarvis_logger.info(f"[STT→WAKE] только обращение → тихое окно "
                               f"{WAKE_COMMAND_WINDOW:.0f} с")
            command_queue.put("__WAKE__")

    except sr.UnknownValueError:
        pass
    except sr.RequestError as e:
        print(f"[STT RequestError]: {e}")
    except Exception as e:
        print(f"[Callback error]: {e}")


try:
    import webview
except ImportError:
    webview = None

UI_ENABLED = os.getenv("JARVIS_UI", "on").lower() == "on"
UI_HTML = str((JARVIS_DIR / "ui" / "index.html").resolve())
_ui_window = None
_ui_last_state = None
_last_stt_ms = 0.0
_last_tts_ms = 0.0
_wake_active_until = 0.0
_microphone_names_cache = ()


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


class JarvisApi:
    """Exposed to the UI's JavaScript as window.pywebview.api."""

    def send_command(self, text):
        text = (text or "").strip()
        if text:
            command_queue.put(text)
        return True

    def get_settings(self):
        cfg = _read_config_file()
        result = {}
        for key in UI_SETTING_KEYS:
            if key in cfg:
                result[key] = str(cfg[key])
            elif os.getenv(key) is not None:
                result[key] = os.getenv(key)
        result.update({
            "JARVIS_LLM": result.get("JARVIS_LLM", LLM_ENGINE),
            "OLLAMA_MODEL": result.get("OLLAMA_MODEL", OLLAMA_MODEL),
            "OPENROUTER_MODEL": result.get("OPENROUTER_MODEL", OPENROUTER_MODEL),
            "OPENROUTER_FREE_MODEL": result.get("OPENROUTER_FREE_MODEL", OPENROUTER_FREE_MODEL),
            "OPENROUTER_AGENT_MODEL": result.get("OPENROUTER_AGENT_MODEL", OPENROUTER_AGENT_MODEL),
            "JARVIS_PROJECT_ROOTS": result.get("JARVIS_PROJECT_ROOTS", os.getenv("JARVIS_PROJECT_ROOTS", "")),
            "SESSION_MEMORY": result.get("SESSION_MEMORY", "on" if SESSION_MEMORY else "off"),
            "JARVIS_LLM_DEADLINE": result.get("JARVIS_LLM_DEADLINE", str(LLM_DEADLINE)),
            "JARVIS_LLM_DEADLINE_CLOUD": result.get("JARVIS_LLM_DEADLINE_CLOUD", str(LLM_DEADLINE_CLOUD)),
            "JARVIS_LLM_GEN_BUDGET": result.get("JARVIS_LLM_GEN_BUDGET", str(LLM_GEN_BUDGET)),
            "STT_ENGINE": result.get("STT_ENGINE", STT_ENGINE),
            "WHISPER_MODEL": result.get("WHISPER_MODEL", WHISPER_MODEL_SIZE),
            "TTS_ENGINE": result.get("TTS_ENGINE", TTS_ENGINE),
            "PIPER_VOICE": result.get("PIPER_VOICE", PIPER_VOICE),
            "PIPER_LENGTH_SCALE": result.get("PIPER_LENGTH_SCALE", str(PIPER_LENGTH_SCALE)),
            "PIPER_NOISE_SCALE": result.get("PIPER_NOISE_SCALE", str(PIPER_NOISE_SCALE)),
            "PIPER_NOISE_W_SCALE": result.get("PIPER_NOISE_W_SCALE", str(PIPER_NOISE_W_SCALE)),
            "EDGE_VOICE": result.get("EDGE_VOICE", EDGE_VOICE),
            "JARVIS_PAUSE_THRESHOLD": result.get("JARVIS_PAUSE_THRESHOLD", str(PAUSE_THRESHOLD)),
            "JARVIS_WAKE_COMMAND_WINDOW": result.get("JARVIS_WAKE_COMMAND_WINDOW", str(WAKE_COMMAND_WINDOW)),
            "JARVIS_PHRASE_TIME_LIMIT": result.get("JARVIS_PHRASE_TIME_LIMIT", str(PHRASE_TIME_LIMIT)),
            "JARVIS_FOLLOWUP_WINDOW": result.get("JARVIS_FOLLOWUP_WINDOW", str(FOLLOWUP_WINDOW)),
            "JARVIS_SPEAK_COOLDOWN": result.get("JARVIS_SPEAK_COOLDOWN", str(SPEAK_COOLDOWN)),
            "JARVIS_FOLLOWUP_MODE": result.get("JARVIS_FOLLOWUP_MODE", FOLLOWUP_MODE),
            "JARVIS_OVERLAY": result.get("JARVIS_OVERLAY", "on" if OVERLAY_ENABLED else "off"),
            "OPENROUTER_API_KEY_SET": bool(cfg.get("OPENROUTER_API_KEY") or OPENROUTER_API_KEY),
            "TELEGRAM_API_ID": result.get("TELEGRAM_API_ID", str(cfg.get("TELEGRAM_API_ID", ""))),
            "TELEGRAM_PHONE": result.get("TELEGRAM_PHONE", str(cfg.get("TELEGRAM_PHONE", ""))),
            "TELEGRAM_API_HASH_SET": bool(cfg.get("TELEGRAM_API_HASH") or os.getenv("TELEGRAM_API_HASH")),
            "VERSION": APP_VERSION,
        })
        return result

    def save_settings(self, settings):
        ok, message = _write_config_file(settings or {})
        return {"ok": ok, "message": message}

    def diagnostics(self):
        spoken, data = get_jarvis_status()
        data["summary"] = spoken
        return data

    def telegram_status(self):
        return telegram_status()

    def telegram_send_code(self):
        return telegram_send_code()

    def telegram_sign_in(self, code="", password=""):
        return telegram_sign_in(code, password)

    def list_microphones(self):
        return [{"index": i, "name": name}
                for i, name in enumerate(_microphone_names_cache)]

    def minimize(self):
        return _set_native_window_state("minimize")

    def maximize(self):
        return _set_native_window_state("maximize")

    def restore(self):
        return _set_native_window_state("restore")

    def close(self):
        _stop_event.set()
        if _ui_window is not None:
            _ui_window.destroy()
        return True


# выбираем микрофон, по возможности USB
def _select_mic():
    """Pick the input device, printing the list so a wrong default is visible.

    Windows' default input isn't always the one you speak into. Set JARVIS_MIC_INDEX
    to an index from this list to pin a specific microphone.
    """
    global _microphone_names_cache
    try:
        names = sr.Microphone.list_microphone_names()
        _microphone_names_cache = tuple(names)
        jarvis_logger.info(f"[AUDIO] найдено устройств PortAudio: {len(names)}")
    except Exception as e:
        _microphone_names_cache = ()
        print(f"[Микрофон] Не удалось получить список устройств: {e}")
        jarvis_logger.exception("[AUDIO] ошибка перечисления устройств PortAudio")
        return None

    want = os.getenv("JARVIS_MIC_INDEX")
    if want:
        try:
            idx = int(want)
            print(f"[Микрофон] JARVIS_MIC_INDEX={idx}: {names[idx]}")
            return idx
        except (ValueError, IndexError):
            print(f"[Микрофон] JARVIS_MIC_INDEX={want!r} некорректен — беру устройство по умолчанию.")

    print("[Микрофон] Доступные устройства ввода:")
    for i, n in enumerate(names):
        print(f"    [{i}] {n}")

    usb_indices = [i for i, n in enumerate(names)
                   if "usb" in n.lower() and "output" not in n.lower()]
    if usb_indices:
        idx = usb_indices[0]
        print(f"[Микрофон] USB-микрофон найден, выбираю автоматически: [{idx}] {names[idx]}")
        print("[Микрофон] Для другого устройства — задайте JARVIS_MIC_INDEX в настройках.")
        jarvis_logger.info(f"[AUDIO] авто-выбор USB-микрофона: [{idx}] {names[idx]}")
        return idx

    print("[Микрофон] USB-микрофон не найден. Использую устройство по умолчанию. "
          "Если Джарвис не слышит — задайте JARVIS_MIC_INDEX с номером из списка.")
    return None


# запуск: микрофон, фоновый слушатель и главный цикл
def run_assistant():
    global _wake_active_until
    global _recognizer
    pygame.mixer.init()
    recognizer = sr.Recognizer()
    _recognizer = recognizer
    stop_listening = None
    jarvis_logger.info(
        f"[STARTUP] Джарвис запущен — "
        f"TTS={TTS_ENGINE}/{_effective_tts_engine()}  STT={STT_ENGINE}  LLM={LLM_ENGINE}  "
        f"WHISPER={WHISPER_MODEL_SIZE}"
    )
    start_overlay()
    mic_index = _select_mic()

    print("Микрофон (быстрая калибровка)...")
    jarvis_logger.info(f"[AUDIO] калибровка микрофона device_index={mic_index}")
    with sr.Microphone(device_index=mic_index) as source:
        recognizer.adjust_for_ambient_noise(source, duration=0.8)

    recognizer.pause_threshold = PAUSE_THRESHOLD
    recognizer.non_speaking_duration = min(0.6, PAUSE_THRESHOLD)
    recognizer.energy_threshold = min(max(recognizer.energy_threshold, 300), 1500)
    recognizer.dynamic_energy_threshold = True
    recognizer.dynamic_energy_adjustment_damping = 0.9

    mic = sr.Microphone(device_index=mic_index)
    stop_listening = recognizer.listen_in_background(
        mic, callback, phrase_time_limit=PHRASE_TIME_LIMIT)
    print("Фоновый слушатель запущен.")
    jarvis_logger.info("[AUDIO] фоновый слушатель запущен")

    ui_call("window.jvConnected && jvConnected()")

    if _effective_tts_engine() == "xtts":
        print("Pre-warming XTTS (CUDA on RTX 5070)...")
        generate_speech("Готов.")
    else:
        engine_name = ("piper (local, offline)"
                       if _effective_tts_engine() == "piper" else "edge (cloud)")
        print(f"Fast TTS: {engine_name} — loading model + building instant phrase cache...")
        prewarm_tts_cache()
        print(f"TTS cache ready: {len(_TTS_INSTANT_CACHE)} instant phrases.")

    def _warm_llm():
        warmup_ollama()
        if not OPENROUTER_API_KEY:
            return
        try:
            get_openrouter_client().chat.completions.create(
                model=OPENROUTER_MODEL,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1, timeout=10,
            )
        except Exception:
            pass
    threading.Thread(target=_warm_llm, daemon=True).start()

    if STT_ENGINE == "whisper":
        print("Loading local STT (faster-whisper) in background...")
        threading.Thread(target=warmup_whisper, daemon=True).start()

    if SESSION_MEMORY:
        _feat.session_load()
    _feat.start_reminder_worker(speak_fn=speak)
    _feat.arm_hotkey_listen(command_queue, wake_seconds=WAKE_COMMAND_WINDOW)
    print("Hotkey: Ctrl+Alt+J — слушать команду без «Джарвис».")
    if os.getenv("JARVIS_FAST_VAD", "off").lower() in {"1", "on", "true", "yes"}:
        print(f"FAST_VAD: pause_threshold={PAUSE_THRESHOLD:.2f}s")

    mem = get_obsidian_memory(500)
    if mem:
        print(f"Obsidian память загружена ({len(mem)} символов).")

    ui_state("idle")

    def _daily_briefing():
        try:
            now = datetime.datetime.now()
            hour = now.hour
            greeting = "Доброе утро" if 5 <= hour < 12 else ("Добрый день" if hour < 18 else ("Добрый вечер" if hour < 22 else "Доброй ночи"))
            date_str = now.strftime("%d %B, %A")
            time_str = now.strftime("%H:%M")
            briefing = f"{greeting}, сэр. Сегодня {date_str}, {time_str}."

            pending = [i for i in load_todo() if not i["done"]]
            if pending:
                briefing += f" У вас {len(pending)} задачи в списке дел."

            try:
                weather = get_weather("Москва")
                briefing += f" {weather}"
            except Exception:
                pass

            speak(briefing)
        except Exception as e:
            print(f"[Briefing error]: {e}")


    print("\n--- ДЖАРВИС ОЖИДАЕТ (скорость приоритет) ---")

    last_reply = ""

    try:
        while not _stop_event.is_set():
            try:
                command = command_queue.get(timeout=0.4)

                if isinstance(command, tuple) and command and command[0] == "__HOTKEY__":
                    _wake_active_until = time.time() + float(command[1])
                    ui_state("listening")
                    print(f"[Hotkey: жду команду {float(command[1]):.0f} с]")
                    continue

                if command == "__WAKE__":
                    ui_state("listening")
                    print(f"[Жду продолжение до {WAKE_COMMAND_WINDOW:.0f} с — без голосового ответа]")
                    continue

                if command.strip().lower() in ["выход", "отключись", "пока", "отключи системы"]:
                    speak("Отключаю системы. До свидания, сэр.")
                    break

                telegram_confirmation = telegram_confirm_pending(command)
                if telegram_confirmation is not None:
                    speak(telegram_confirmation)
                    last_reply = telegram_confirmation
                    log_interaction("jarvis", telegram_confirmation)
                    continue

                email_confirmation = email_confirm_pending(command)
                if email_confirmation is not None:
                    speak(email_confirmation)
                    last_reply = email_confirmation
                    log_interaction("jarvis", email_confirmation)
                    continue

                cmd_lower = command.strip().lower()

                telegram_intent = detect_telegram_intent_from_text(cmd_lower)
                if telegram_intent:
                    print(f"[Fast Telegram intent] {telegram_intent}")
                    ai_reply = parse_and_execute_tags(telegram_intent, cmd_lower)
                    speak(ai_reply)
                    last_reply = ai_reply
                    log_interaction("jarvis", ai_reply)
                    continue

                lookup_req = extract_lookup_request(cmd_lower)
                if lookup_req and not _is_hypothetical_action_question(cmd_lower):
                    kind, value = lookup_req
                    print(f"[Lookup] {kind}={value}")
                    ai_reply = lookup_identity(kind, value)
                    speak(ai_reply)
                    last_reply = ai_reply
                    log_interaction("jarvis", ai_reply)
                    continue

                if _has_word(cmd_lower, ["статус джарвиса", "диагностика джарвиса",
                                         "проверь системы", "проверка систем"]):
                    ai_reply, status_data = get_jarvis_status()
                    _local_reply_text = ai_reply
                    speak(_local_reply_text)
                    last_reply = _local_reply_text
                    log_interaction("jarvis", _local_reply_text)
                    ui_call("window.jvDiagnostics && jvDiagnostics(" +
                            json.dumps(status_data, ensure_ascii=False) + ")")
                    continue

                intent_tag = detect_intent_from_text(cmd_lower)
                if intent_tag:
                    print(f"[Fast intent] {intent_tag} (no LLM)")
                    if "[OPEN:" in intent_tag:
                        app = intent_tag.replace("[OPEN:", "").replace("]", "")
                        execute_system_command(app)
                        ai_reply = "Открываю браузер, сэр." if app == "browser" else "Открываю, сэр."
                    elif "[MUSIC:PLAY:" in intent_tag:
                        q = re.search(r'\[MUSIC:PLAY:(.+?)\]', intent_tag)
                        ai_reply = (play_yandex_music(q.group(1) if q else "", auto_play=True)
                                    or "Включаю, сэр.")
                    elif "[MUSIC:OPEN]" in intent_tag:
                        ai_reply = (play_yandex_music("", auto_play=False)
                                    or "Открываю Яндекс Музыку, сэр.")
                    else:
                        ai_reply = "Выполняю, сэр."
                    speak(ai_reply)
                    last_reply = ai_reply
                    log_interaction("jarvis", ai_reply)
                    continue

                feature_reply = handle_local_feature_command(
                    cmd_lower, last_reply=last_reply, speak_fn=speak)
                if feature_reply is not None:
                    speak(feature_reply)
                    last_reply = feature_reply
                    log_interaction("jarvis", feature_reply)
                    if SESSION_MEMORY:
                        _feat.session_record("user", cmd_lower)
                        _feat.session_record("assistant", feature_reply)
                    print(f"[Слушаю снова... threshold={recognizer.energy_threshold:.0f}]")
                    continue

                open_query = extract_open_app_request(cmd_lower)
                if open_query:
                    opened = execute_system_command(open_query)
                    ai_reply = (f"Открываю {open_query}, сэр." if opened
                                else f"Не нашёл приложение {open_query}, сэр.")
                    speak(ai_reply)
                    last_reply = ai_reply
                    log_interaction("jarvis", ai_reply)
                    continue

                productivity_reply = handle_local_productivity_command(
                    cmd_lower, speak_fn=speak)
                if productivity_reply is not None:
                    speak(productivity_reply)
                    last_reply = productivity_reply
                    log_interaction("jarvis", productivity_reply)
                    if SESSION_MEMORY:
                        _feat.session_record("user", cmd_lower)
                        _feat.session_record("assistant", productivity_reply)
                    print(f"[Слушаю снова... threshold={recognizer.energy_threshold:.0f}]")
                    continue

                if _has_word(cmd_lower, ["время", "который час", "skovoe vremya", "time"]):
                    now_t = datetime.datetime.now().strftime("%H:%M")
                    ai_reply = f"Сейчас {now_t}, сэр."
                    speak(ai_reply)
                    log_interaction("jarvis", ai_reply)
                    print(f"[Слушаю снова... threshold={recognizer.energy_threshold:.0f}]")
                    continue

                if _has_word(cmd_lower, ["скриншот", "screenshot", "снимок экрана"]):
                    result = take_screenshot()
                    ai_reply = result
                    speak(ai_reply)
                    log_interaction("jarvis", ai_reply)
                    print(f"[Слушаю снова... threshold={recognizer.energy_threshold:.0f}]")
                    continue

                if _has_word(cmd_lower, ["железо", "цпу", "cpu", "ram", "оперативка", "нагрузка",
                                                   "загрузка процессора", "состояние системы", "статус системы"]):
                    ai_reply = get_system_stats()
                    speak(ai_reply)
                    log_interaction("jarvis", ai_reply)
                    print(f"[Слушаю снова... threshold={recognizer.energy_threshold:.0f}]")
                    continue

                if _has_word(cmd_lower, ["заблокируй", "заблокировать", "заблоки", "lock"]):
                    speak("Блокирую, сэр.")
                    time.sleep(1)
                    lock_pc()
                    log_interaction("jarvis", "Блокирую, сэр.")
                    print(f"[Слушаю снова... threshold={recognizer.energy_threshold:.0f}]")
                    continue

                if _has_word(cmd_lower, ["список дел", "что в списке", "мои задачи"]):
                    ai_reply = todo_list()
                    speak(ai_reply)
                    log_interaction("jarvis", ai_reply)
                    print(f"[Слушаю снова... threshold={recognizer.energy_threshold:.0f}]")
                    continue

                def _local_reply(txt):
                    nonlocal last_reply
                    last_reply = txt
                    speak(txt)
                    log_interaction("jarvis", txt)
                    print(f"[Слушаю снова... threshold={recognizer.energy_threshold:.0f}]")

                vol_num = re.search(r'громкость\s+(?:на\s+)?(\d{1,3})', cmd_lower)
                if vol_num:
                    lvl = int(vol_num.group(1)); set_volume(lvl)
                    _local_reply(f"Громкость {min(100, lvl)}%, сэр.")
                    continue
                if _has_word(cmd_lower, ["громче", "погромче", "сделай громче"]):
                    nv = nudge_volume(+15); _local_reply("Громче, сэр." if nv >= 0 else "Не удалось, сэр.")
                    continue
                if _has_word(cmd_lower, ["тише", "потише", "сделай тише"]):
                    nv = nudge_volume(-15); _local_reply("Тише, сэр." if nv >= 0 else "Не удалось, сэр.")
                    continue
                if _has_word(cmd_lower, ["выключи звук", "без звука", "заглуши", "мьют", "mute"]):
                    set_volume(0); _local_reply("Звук выключен, сэр.")
                    continue

                br_num = re.search(r'ярко(?:сть)?\s+(?:на\s+)?(\d{1,3})', cmd_lower)
                if br_num:
                    _local_reply(set_brightness(int(br_num.group(1))))
                    continue
                if "ярче" in cmd_lower:
                    cur = _plat.get_brightness()
                    if cur < 0:
                        cur = None
                    _local_reply(set_brightness((cur if cur is not None else 50) + 20))
                    continue
                if _has_word(cmd_lower, ["темнее", "потемнее"]):
                    cur = _plat.get_brightness()
                    if cur < 0:
                        cur = None
                    _local_reply(set_brightness((cur if cur is not None else 50) - 20))
                    continue

                if _has_word(cmd_lower, ["пауза", "поставь на паузу", "плей", "продолжи воспроизведение"]):
                    media_control("playpause"); _local_reply("Готово, сэр.")
                    continue
                if _has_word(cmd_lower, ["следующий трек", "следующая песня", "переключи вперёд", "переключи вперед", "дальше песню"]):
                    media_control("next"); _local_reply("Следующий, сэр.")
                    continue
                if _has_word(cmd_lower, ["предыдущий трек", "предыдущая песня", "прошлый трек"]):
                    media_control("prev"); _local_reply("Предыдущий, сэр.")
                    continue

                if _has_word(cmd_lower, ["спасибо", "благодарю", "спасиб"]):
                    _local_reply("Всегда пожалуйста, сэр.")
                    continue
                if cmd_lower in ("привет", "здравствуй", "здарова", "хай", "приветствую"):
                    _local_reply("Здравствуйте, сэр.")
                    continue

                if _has_word(cmd_lower, ["повтори", "что ты сказал", "повторите"]):
                    _local_reply(last_reply or "Мне нечего повторить, сэр.")
                    continue

                ui_state("thinking")
                ui_clear_lat()
                if _last_stt_ms:
                    ui_lat("stt", _last_stt_ms / 1000.0)


                ai_reply = process_with_llm_streaming(command)
                last_reply = ai_reply or last_reply

                ui_lat("llm", _last_llm_ttft_ms / 1000.0)
                if _last_tts_ms:
                    ui_lat("tts", _last_tts_ms / 1000.0)
                ui_lat("sum", (_last_stt_ms + _last_llm_ttft_ms + _last_tts_ms) / 1000.0)
                ui_state("idle")

                if recognizer.energy_threshold > 1500:
                    recognizer.energy_threshold = 1500
                    print("[Threshold capped at 1500]")
                print(f"[Слушаю снова... threshold={recognizer.energy_threshold:.0f}]")

            except queue.Empty:
                ui_state("idle")
                continue
            except KeyboardInterrupt:
                raise
            except Exception as loop_err:
                print(f"[Loop error]: {loop_err}")
                traceback.print_exc()

    except KeyboardInterrupt:
        print("\nОстановка работы.")
    except Exception as main_err:
        print(f"[Fatal error]: {main_err}")
        traceback.print_exc()
    finally:
        if stop_listening is not None:
            try:
                stop_listening(wait_for_stop=False)
            except Exception:
                pass
        stop_overlay()
        pygame.mixer.quit()

    if _ui_window is None:
        print("\nJarvis finished. Press Enter to close...")
        try:
            input()
        except Exception:
            pass


_stop_event = threading.Event()


def main():
    """Entry point: opens the native J.A.R.V.I.S. window if pywebview is available,
    otherwise runs headless in the console (original behaviour)."""
    global _ui_window
    if UI_ENABLED and webview is not None and os.path.exists(UI_HTML):
        try:
            _ui_window = webview.create_window(
                "J.A.R.V.I.S.",
                url=UI_HTML,
                js_api=JarvisApi(),
                width=1040, height=740, min_size=(760, 560),
                background_color="#04040c",
                frameless=True,
                easy_drag=False,
            )
            def _window_event(name):
                def _handler(*args):
                    jarvis_logger.info(f"[UI] event={name} args={args!r}")
                    if name == "closed":
                        _stop_event.set()
                return _handler

            _ui_window.events.closing += _window_event("closing")
            _ui_window.events.closed += _window_event("closed")
            _ui_window.events.maximized += _window_event("maximized")
            _ui_window.events.restored += _window_event("restored")
            _ui_window.events.minimized += _window_event("minimized")
            webview.start(run_assistant)
            return
        except Exception as e:
            print(f"[UI failed, falling back to console]: {e}")
            _ui_window = None
    run_assistant()


if __name__ == "__main__":
    main()

"""Личная память, список дел и таймеры.

Три простых хранилища на JSON-файлах рядом с проектом: что Джарвис помнит о
владельце, что записано в список дел и какие таймеры сейчас тикают. Всё это
отвечает без обращения к модели, поэтому и вынесено отдельно от неё.

Файлы личные и в гит не уходят — они перечислены в .gitignore.
"""

import datetime
import json
import re
import threading
import time
import uuid

from jarvis_config import JARVIS_DIR

__all__ = [
    "MEMORY_FILE", "TODO_FILE",
    "load_memory", "save_memory", "remember", "recall",
    "load_todo", "save_todo", "todo_add", "todo_list", "todo_done",
    "set_timer", "parse_timer_duration", "timer_snapshot", "cancel_timer",
]


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

_active_timers: dict = {}
_timer_lock = threading.RLock()

def set_timer(seconds: int, label: str = "", speak_fn=None):
    """Cancellable session timer. Claim expiration atomically before speaking."""
    seconds = int(seconds)
    if seconds <= 0:
        raise ValueError("Длительность таймера должна быть положительной")
    timer_id = uuid.uuid4().hex
    event = threading.Event()
    with _timer_lock:
        for key in list(_active_timers):
            if len(_active_timers) >= 50 and _active_timers[key]["status"] != "running":
                del _active_timers[key]
        if len(_active_timers) >= 50:
            raise ValueError("Одновременно доступно не более 50 таймеров")
        _active_timers[timer_id] = {"id": timer_id, "label": label or "Таймер",
                                    "deadline": time.monotonic() + seconds,
                                    "due_at": time.time() + seconds,
                                    "status": "running", "event": event}

    def _fire():
        if event.wait(seconds):
            return
        with _timer_lock:
            if _active_timers[timer_id]["status"] != "running":
                return
            _active_timers[timer_id]["status"] = "completed"
        msg = f"Время вышло, сэр. {label}" if label else "Таймер сработал, сэр."
        print(f"[TIMER] {msg}")
        if speak_fn:
            speak_fn(msg)
    t = threading.Thread(target=_fire, daemon=True)
    t.start()
    return timer_id


def timer_snapshot():
    with _timer_lock:
        return [{"id": item["id"], "label": item["label"], "status": item["status"],
                 "due_at": item["due_at"],
                 "remaining": max(0, item["deadline"] - time.monotonic())}
                for item in reversed(list(_active_timers.values()))]


def cancel_timer(timer_id):
    with _timer_lock:
        item = _active_timers.get(str(timer_id))
        if not item or item["status"] != "running":
            return False
        item["status"] = "cancelled"
        item["event"].set()
        return True

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

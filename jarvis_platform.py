"""Платформенный слой Jarvis.

Здесь живёт всё, что умеет только Windows: громкость через pycaw, media-клавиши
и вставка через pyautogui, блокировка станции через user32, яркость через
screen-brightness-control.

Ядро должно импортироваться на Linux ARM (домашний сервер Orange Pi), где этих
библиотек нет вообще и нет графического дисплея. Поэтому импорты необязательные,
а на неподдерживаемой платформе функции возвращают честный отказ вместо падения.

Контракт:
  * ни одна функция не бросает исключение из-за отсутствия платформы;
  * действия возвращают (ok: bool, note: str) — note годится и в лог, и в озвучку;
  * геттеры возвращают -1 (громкость, яркость), None или False.
"""

import ctypes
import os

IS_WINDOWS = os.name == "nt"

# Нет дисплея / нет X11 / не установлен — на сервере это норма, не ошибка.
try:
    import pyautogui
except Exception:
    pyautogui = None

# Только Windows: на ARM пакет даже не собирается.
try:
    from ctypes import POINTER, cast
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
except Exception:
    AudioUtilities = IAudioEndpointVolume = CLSCTX_ALL = None
    cast = POINTER = None

try:
    import screen_brightness_control as sbc
except Exception:
    sbc = None


HAS_DESKTOP = pyautogui is not None
HAS_MIXER = AudioUtilities is not None
HAS_BRIGHTNESS = sbc is not None

_NO_DESKTOP = "Действия рабочего стола недоступны на этой машине."
_NO_MIXER = "Управление громкостью доступно только на Windows."
_NO_BRIGHTNESS = "Управление яркостью недоступно."

_MEDIA_KEYS = {"playpause": "playpause", "next": "nexttrack", "prev": "prevtrack"}


def capabilities() -> dict:
    """Что эта машина реально умеет — для «статуса» и для диагностики на сервере."""
    return {
        "windows": IS_WINDOWS,
        "desktop": HAS_DESKTOP,
        "mixer": HAS_MIXER,
        "brightness": HAS_BRIGHTNESS,
    }


# ── Клавиатура и media-клавиши ──────────────────────────────────────────────

def press_media_key(action: str) -> tuple[bool, str]:
    """Глобальная media-клавиша, а не слепой клик по центру экрана."""
    key = _MEDIA_KEYS.get((action or "").lower())
    if key is None:
        return False, f"Неизвестное медиа-действие: {action}"
    if pyautogui is None:
        return False, _NO_DESKTOP
    try:
        pyautogui.press(key)
        return True, ""
    except Exception as e:
        return False, f"Не удалось нажать media-клавишу: {e}"


def hotkey(*keys: str) -> tuple[bool, str]:
    """Сочетание клавиш в активном окне."""
    if pyautogui is None:
        return False, _NO_DESKTOP
    try:
        pyautogui.hotkey(*keys)
        return True, ""
    except Exception as e:
        return False, f"Не удалось нажать {'+'.join(keys)}: {e}"


def paste_from_clipboard() -> tuple[bool, str]:
    """Ctrl+V — так вставляется текст с кириллицей, посимвольный ввод её ломает."""
    return hotkey("ctrl", "v")


# ── Громкость ───────────────────────────────────────────────────────────────

def _endpoint_volume():
    if AudioUtilities is None:
        return None
    devices = AudioUtilities.GetSpeakers()
    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


def get_master_volume() -> int:
    """Текущая громкость 0-100, или -1 если микшер недоступен."""
    try:
        volume = _endpoint_volume()
        if volume is None:
            return -1
        return int(round(volume.GetMasterVolumeLevelScalar() * 100))
    except Exception:
        return -1


def set_master_volume(level: int) -> tuple[bool, str]:
    """Установить громкость 0-100."""
    try:
        volume = _endpoint_volume()
        if volume is None:
            return False, _NO_MIXER
        level = max(0, min(100, int(level)))
        volume.SetMasterVolumeLevelScalar(level / 100.0, None)
        return True, f"Громкость {level}%."
    except Exception as e:
        return False, f"Ошибка громкости: {e}"


# ── Яркость ─────────────────────────────────────────────────────────────────

def get_brightness() -> int:
    """Текущая яркость 0-100, или -1 если управление недоступно."""
    if sbc is None:
        return -1
    try:
        values = sbc.get_brightness()
        return int(values[0]) if values else -1
    except Exception:
        return -1


def set_brightness(level: int) -> tuple[bool, str]:
    """Установить яркость 0-100."""
    if sbc is None:
        return False, _NO_BRIGHTNESS
    try:
        level = max(0, min(100, int(level)))
        sbc.set_brightness(level)
        return True, f"Яркость установлена на {level}%."
    except Exception as e:
        return False, f"Ошибка яркости: {e}"


# ── Сеанс ───────────────────────────────────────────────────────────────────

def lock_workstation() -> tuple[bool, str]:
    """Заблокировать сеанс."""
    if not IS_WINDOWS:
        return False, "Блокировка сеанса доступна только на Windows."
    try:
        ctypes.windll.user32.LockWorkStation()
        return True, "Рабочая станция заблокирована, сэр."
    except Exception as e:
        return False, f"Ошибка блокировки: {e}"

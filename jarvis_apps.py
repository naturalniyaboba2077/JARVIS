"""Каталог установленных программ и разрешение «открой …».

Джарвис открывает программы не наугад: он строит каталог из ярлыков меню
«Пуск» и списка pc_apps.txt, а потом сопоставляет с ним сказанное — с учётом
русских алиасов («ворд», «вс код») и опечаток распознавания. Отдельно живёт
таблица сайтов, чтобы «зайди на ютуб» вело на youtube.com, а не в поиск.

Каталог строится один раз и кэшируется: обход меню «Пуск» слишком медленный,
чтобы делать это на каждую команду.
"""

import os
import re
import time
from difflib import SequenceMatcher
from pathlib import Path

from jarvis_config import JARVIS_DIR
from jarvis_log import jarvis_logger

__all__ = [
    "_normalize_app_name", "_build_app_catalog", "_APP_ALIASES", "_WEB_TARGETS",
    "resolve_web_target", "resolve_app", "extract_open_app_request",
]

# Обход меню «Пуск» слишком медленный, чтобы делать его на каждую команду.
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

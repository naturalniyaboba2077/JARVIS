"""Поиск сведений о человеке по юзернейму или номеру телефона.

Собирает только то, что отдают сами источники: публичный профиль Telegram
через ваш аккаунт и сниппеты из открытого веба. Базы утечек не используются.

Отчёт бывает длинным, и зачитывать его вслух бессмысленно, поэтому полный
текст уходит в Telegram-бота из TELEGRAM_REPORT_BOT_TOKEN, а голосом Джарвис
даёт короткую выжимку.
"""

import datetime
import os
import re

import requests as http_requests

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

from jarvis_config import _read_config_file
from jarvis_log import jarvis_logger
from jarvis_telegram import (
    normalize_phone_number, telegram_lookup_phone, telegram_lookup_username,
)

__all__ = [
    "extract_lookup_request", "lookup_identity", "send_lookup_report_via_bot",
    "_web_search_snippets", "_lookup_report_bot_config", "_discover_report_chat_id",
]


_PHONE_EXTRACT_RE = re.compile(r'(?:\+|plus)?[\d\s\-()]{10,20}\d')
_USERNAME_EXTRACT_RE = re.compile(
    r'(?:@|собака\s+|эт\s+)?([A-Za-z][A-Za-z0-9_]{3,31})\b')
_LOOKUP_VERB_RE = re.compile(
    r'(найд\w*|поищ\w*|пробей|проверь|кто\s+так\w*|информац\w*|профиль|юзернейм|'
    r'username|номер\s+телефона|по\s+номеру)',
    re.IGNORECASE | re.UNICODE)


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
    """Legacy hook: incoming bot messages cannot establish the owner's identity."""
    return ""


def send_lookup_report_via_bot(report: str, title: str = "Отчёт Jarvis") -> str:
    """Deliver a long lookup report through the user's report bot. Short status string."""
    token, chat_id = _lookup_report_bot_config()
    if not token:
        return "Бот для отчётов не настроен (TELEGRAM_REPORT_BOT_TOKEN)."
    if not chat_id:
        return ("Укажите TELEGRAM_REPORT_CHAT_ID нужного получателя в конфиге. "
                "Отчёт не отправлен: сообщения боту не подтверждают владельца чата.")

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




"""Личный аккаунт Telegram: чтение, поиск, экспорт и подтверждённая отправка.

Работает через Telethon от имени владельца, а не через бота: боту недоступны
обычные диалоги. Сессия лежит в telegram_data/ и в гит не уходит.

Отправка никогда не выполняется сразу — она кладётся в jarvis_state как
ожидающая подтверждения, и уходит только после явного «да» голосом.

Здесь же поиск профиля по юзернейму и номеру: только то, что Telegram отдаёт
сам, без баз утечек.
"""

import asyncio
import datetime
import os
import re
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path

import jarvis_state as _state
from jarvis_config import JARVIS_DIR, _read_config_file
from jarvis_log import jarvis_logger

__all__ = [
    "TELEGRAM_DATA_DIR", "TELEGRAM_SESSION_BASE", "TELEGRAM_EXPORT_DIR",
    "TelegramClient", "telegram_display_name",
    "telegram_status", "telegram_send_code", "telegram_sign_in",
    "telegram_list_chats", "telegram_read_dialog", "telegram_search_dialog",
    "telegram_export_dialog", "telegram_send_message", "telegram_request_send",
    "telegram_confirm_pending",
    "telegram_lookup_username", "telegram_lookup_phone",
    "normalize_phone_number",
    "_TELEGRAM_CONFIRM_YES", "_TELEGRAM_CONFIRM_NO",
    "_telegram_config", "_telegram_preflight", "_telegram_client",
    "_telegram_format_user",
]


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


TELEGRAM_DATA_DIR = JARVIS_DIR / "telegram_data"
TELEGRAM_SESSION_BASE = TELEGRAM_DATA_DIR / "jarvis_user"
TELEGRAM_EXPORT_DIR = Path.home() / "Documents" / "Jarvis Telegram Exports"
_telegram_lock = threading.Lock()
import jarvis_confirm as _confirm

_telegram_pending_lock = _confirm.LOCK
_telegram_phone_code_hash = None
_telegram_phone_lookup_after = 0.0
_TELEGRAM_CONFIRM_YES = _confirm.YES  # compatibility for existing voice callers
_TELEGRAM_CONFIRM_NO = _confirm.NO


def _telegram_config() -> tuple[int | None, str, str]:
    """Read current panel values without requiring a Jarvis restart."""
    from jarvis_settings import settings
    cfg = os.environ if settings.enabled else _read_config_file()
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


from jarvis_settings import activity as _settings_activity


@_settings_activity
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


def _telegram_chat_key(value: str) -> str:
    return re.sub(r'\s+', ' ', value.casefold().replace('ё', 'е')).strip()


def _telegram_dialog_label(dialog) -> str:
    username = getattr(dialog.entity, "username", None)
    return (f"{dialog.name or 'Без названия'}"
            f"{' @' + username if username else ''} (ID {dialog.id})")


async def _telegram_resolve_send_target(client, chat: str):
    """Resolve an exact name/username/marked ID; fuzzy matches are suggestions only."""
    from telethon.utils import get_input_peer

    query = _telegram_chat_key(chat)
    exact = []
    suggestions = []
    # Inspect all dialogs: a second matching name may be outside the recent 250.
    for dialog in await client.get_dialogs(limit=None):
        name = _telegram_chat_key(dialog.name or "")
        username = _telegram_chat_key(getattr(dialog.entity, "username", None) or "")
        if query.startswith("@"):
            matches = bool(username) and query == "@" + username
        elif re.fullmatch(r'-?\d+', query):
            matches = query == str(dialog.id)
        else:
            matches = query == name or bool(username) and query == username
        if matches:
            exact.append(dialog)
        elif query and any(value and (
                query in value or SequenceMatcher(None, query, value).ratio() >= 0.58)
                for value in (name, username)):
            suggestions.append(dialog)

    if len(exact) != 1:
        candidates = exact or suggestions
        if candidates:
            choices = "; ".join(_telegram_dialog_label(d) for d in candidates[:5])
            return ("Получатель не определён однозначно. Укажите точное имя, "
                    f"@username или ID чата. Варианты: {choices}.")
        return f"Не нашёл точного получателя «{chat}». Укажите имя, @username или ID чата."
    dialog = exact[0]
    try:
        # Concrete InputPeer carries ID/access_hash across our short-lived clients.
        # InputPeerSelf would instead depend on whoever owns the next client.
        peer = get_input_peer(dialog.entity, allow_self=False)
    except (TypeError, ValueError):
        return "Не удалось получить адрес получателя Telegram. Обновите чат и повторите запрос."
    return {"peer": peer, "chat": _telegram_dialog_label(dialog)}


async def _telegram_send_to_peer(client, payload: dict) -> str:
    from telethon.tl.types import InputPeerChannel, InputPeerChat, InputPeerUser

    peer = payload.get("peer")
    if not isinstance(peer, (InputPeerUser, InputPeerChat, InputPeerChannel)):
        return "Получатель не зафиксирован. Заново запросите отправку сообщения."
    await client.send_message(peer, payload["text"])
    jarvis_logger.info(f"[TELEGRAM] сообщение отправлено в чат {payload['chat']!r}")
    return f"Сообщение в чат «{payload['chat']}» отправлено, сэр."


def _telegram_send_resolved(payload: dict) -> str:
    """Send the confirmed peer without resolving its display name again."""
    return _telegram_authorized_operation(
        "отправка сообщения", lambda client: _telegram_send_to_peer(client, payload))


def telegram_send_message(chat: str, text: str) -> str:
    """Explicit caller-confirmed send. A string target must match exactly once."""
    chat, text = (chat or "").strip(), (text or "").strip()
    if not chat or not text:
        return "Нужно указать чат и текст сообщения, сэр."

    async def _send(client):
        target = await _telegram_resolve_send_target(client, chat)
        if isinstance(target, str):
            return target
        return await _telegram_send_to_peer(client, {**target, "text": text})

    return _telegram_authorized_operation("отправка сообщения", _send)


def telegram_request_send(chat: str, text: str) -> str:
    chat = (chat or "").strip()
    text = (text or "").strip()
    revision = _confirm.clear()
    if not chat or not text:
        return "Нужно указать чат и текст сообщения, сэр."
    # Do not block cancellation/UI snapshots on recipient network resolution.
    target = _telegram_authorized_operation(
        "выбор получателя", lambda client: _telegram_resolve_send_target(client, chat))
    if isinstance(target, str):
        return target
    request_id = _confirm.stage("telegram", {**target, "text": text}, expected_revision=revision)
    if request_id is None:
        return "Подготовка сообщения отменена или заменена новым запросом."
    preview = text if len(text) <= 140 else text[:140] + "…"
    return (f"Подтвердите отправку в Telegram, сэр. Чат «{target['chat']}», "
            f"сообщение: {preview}. Скажите «подтверждаю» или «отмена».")


def telegram_confirm_pending(text: str, request_id=None) -> str | None:
    outcome, payload = _confirm.consume("telegram", text, request_id)
    if outcome == "none":
        return None
    if outcome == "stale":
        return "Это подтверждение больше не действует. Проверьте текущую карточку сообщения."
    if outcome == "expired":
        return "Срок подтверждения сообщения истёк. Заново запросите отправку."
    if outcome == "cancelled":
        return "Отправка сообщения отменена, сэр."
    if outcome == "confirmed":
        return _telegram_send_resolved(payload)
    return "Ожидаю подтверждения отправки Telegram: скажите «подтверждаю» или «отмена»."


def normalize_phone_number(raw: str) -> str | None:
    digits = re.sub(r'\D', '', raw or "")
    if len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if not (10 <= len(digits) <= 15):
        return None
    return "+" + digits


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
    phone = normalize_phone_number(phone)
    if not phone:
        return "Нужен номер в международном формате, сэр."

    async def _lookup(client):
        global _telegram_phone_lookup_after
        try:
            from telethon.tl.functions.contacts import ResolvePhoneRequest
            from telethon.errors import PhoneNotOccupiedError
        except ImportError:
            return "Обновите Telethon: поиск номера без изменения контактов недоступен."
        # https://core.telegram.org/method/contacts.resolvePhone: at most 1 / 3 s.
        # _telegram_authorized_operation serializes these calls under _telegram_lock.
        now = time.monotonic()
        if now < _telegram_phone_lookup_after:
            return "Повторите поиск номера через несколько секунд, сэр."
        _telegram_phone_lookup_after = now + 3.0
        try:
            resolved = await client(ResolvePhoneRequest(phone=phone))
        except PhoneNotOccupiedError:
            return f"Telegram не раскрыл аккаунт по номеру {phone}."
        user_id = getattr(getattr(resolved, "peer", None), "user_id", None)
        user = next((u for u in (getattr(resolved, "users", None) or [])
                     if u.id == user_id), None)
        if user is None:
            return (f"Telegram не раскрыл аккаунт по номеру {phone}. "
                    "Номер скрыт настройками приватности или не зарегистрирован.")
        about = ""
        try:
            from telethon.tl.functions.users import GetFullUserRequest
            full = await client(GetFullUserRequest(user))
            about = (getattr(getattr(full, "full_user", None), "about", None) or "").strip()
        except Exception:
            pass
        lines = _telegram_format_user(user, about)
        return "Telegram по номеру: " + "; ".join(lines) + "."

    return _telegram_authorized_operation(f"поиск {phone}", _lookup)

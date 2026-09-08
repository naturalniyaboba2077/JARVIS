"""One expiring slot for mail, Telegram and project questions via voice or UI.

The browser submits an opaque request ID, never recipient/text. Claiming a send
removes its payload under the same lock; network IO happens after that claim.
Project selection claims its original task under this same lock before dispatch.
No pending payload is persisted. A stale button cannot approve a replacement.
"""

import re
import threading
import time
import unicodedata
import uuid

LOCK = threading.RLock()
TTL_SECONDS = 120
YES = frozenset({"да", "отправь", "подтверждаю", "отправляй", "разрешаю", "выполняй"})
NO = frozenset({"нет", "отмена", "отмени", "не отправляй", "стоп"})
_FIELDS = {"telegram": "pending_telegram_send", "email": "pending_email_send",
           "project": "pending_project_selection"}
_revision = 0


def normalize(text):
    value = re.sub(r"\s+", " ", str(text or "").strip().casefold())
    while value and (value[0].isspace() or unicodedata.category(value[0]).startswith("P")):
        value = value[1:]
    while value and (value[-1].isspace() or unicodedata.category(value[-1]).startswith("P")):
        value = value[:-1]
    return value


def clear():
    global _revision
    import jarvis_state as state
    with LOCK:
        _revision += 1
        for field in _FIELDS.values():
            setattr(state, field, None)
        return _revision


def stage(kind, payload, expected_revision=None):
    import jarvis_state as state
    field = _FIELDS[kind]
    with LOCK:
        if expected_revision is not None and expected_revision != _revision:
            return None
        clear()
        pending = dict(payload, request_id=uuid.uuid4().hex,
                       expires_at=time.time() + TTL_SECONDS,
                       deadline=time.monotonic() + TTL_SECONDS)
        setattr(state, field, pending)
        return pending["request_id"]


def clear_kind(kind):
    """Invalidate an in-flight question without cancelling a different domain."""
    global _revision
    import jarvis_state as state
    with LOCK:
        _revision += 1
        setattr(state, _FIELDS[kind], None)
        return _revision


def _expired(pending):
    cancel = pending.get("cancel")
    return (not pending.get("request_id") or time.monotonic() >= pending.get("deadline", 0)
            or cancel is not None and cancel.is_set())


def consume(kind, text, request_id=None):
    """Return (none/stale/expired/waiting/cancelled/confirmed, claimed payload)."""
    import jarvis_state as state
    field = _FIELDS[kind]
    with LOCK:
        pending = getattr(state, field, None)
        if pending is None:
            return ("stale" if request_id is not None else "none"), None
        if request_id is not None and request_id != pending.get("request_id"):
            return "stale", None
        if _expired(pending):
            setattr(state, field, None)
            return "expired", None
        answer = normalize(text)
        if answer not in YES | NO:
            return "waiting", None
        setattr(state, field, None)
        return ("cancelled", None) if answer in NO else ("confirmed", pending)


def snapshot():
    """JSON-only public preview. Telethon peers and credentials never cross UI."""
    import jarvis_state as state
    with LOCK:
        for kind, field in _FIELDS.items():
            pending = getattr(state, field, None)
            if not pending:
                continue
            if _expired(pending):
                setattr(state, field, None)
                continue
            if kind == "project":
                request = pending["request"]
                return {"kind": kind, "id": pending["request_id"],
                        "expires_at": pending["expires_at"], "task": request.task,
                        "mode": request.mode, "query": request.project,
                        "choices": [{"id": choice["id"], "name": choice["path"].name,
                                     "path": str(choice["path"])} for choice in pending["choices"]]}
            return {"kind": kind, "id": pending["request_id"],
                    "expires_at": pending["expires_at"],
                    "recipient": pending.get("to", pending.get("chat", "")),
                    "subject": pending.get("subject", ""),
                    "body": pending.get("body", pending.get("text", ""))}
    return None

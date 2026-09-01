# Extra daily features for J.A.R.V.I.S.: session memory, windows, clipboard,
# reminders, file find/open, OCR, Gmail readonly, hotkey arming.
# Imported by jarvis.py — keep side-effects minimal at import time.

from __future__ import annotations

import datetime
import base64
import email.message
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("jarvis")

JARVIS_DIR = Path(__file__).parent
REMINDERS_FILE = JARVIS_DIR / "jarvis_reminders.json"
SESSION_FILE = JARVIS_DIR / "jarvis_session.json"

# ── Session memory ──────────────────────────────────────────────────────────

_session_turns: list[dict] = []
_session_digest = ""
_SESSION_KEEP = 8          # raw recent turns kept verbatim
_SESSION_DIGEST_MAX = 900  # chars injected into the system prompt
_session_lock = threading.Lock()


def session_record(role: str, text: str) -> None:
    """Append a turn and periodically fold older turns into a short digest."""
    global _session_digest
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return
    with _session_lock:
        _session_turns.append({
            "role": role,
            "text": text[:400],
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        })
        overflow = len(_session_turns) - _SESSION_KEEP
        if overflow > 0:
            old = _session_turns[:overflow]
            del _session_turns[:overflow]
            bits = []
            for t in old:
                who = "Юзер" if t["role"] == "user" else "Джарвис"
                bits.append(f"{who}: {t['text']}")
            chunk = " | ".join(bits)
            _session_digest = (_session_digest + " | " + chunk).strip(" |")
            if len(_session_digest) > _SESSION_DIGEST_MAX:
                _session_digest = _session_digest[-_SESSION_DIGEST_MAX:]
        _persist_session()


def session_context(max_chars: int = 800) -> str:
    """Compact context for the LLM system prompt."""
    with _session_lock:
        parts = []
        if _session_digest:
            parts.append(f"Ранее в сессии: {_session_digest}")
        for t in _session_turns[-_SESSION_KEEP:]:
            who = "Юзер" if t["role"] == "user" else "Джарвис"
            parts.append(f"{who}: {t['text']}")
        blob = "\n".join(parts)
        return blob[-max_chars:] if len(blob) > max_chars else blob


def session_summary() -> str:
    ctx = session_context(1200)
    if not ctx:
        return "Пока нечего вспоминать о этой сессии, сэр."
    return f"Кратко по сессии, сэр. {ctx}"


def session_clear() -> str:
    global _session_digest
    with _session_lock:
        _session_turns.clear()
        _session_digest = ""
        _persist_session()
    return "Сессионную память очистил, сэр."


def _persist_session() -> None:
    try:
        data = {"digest": _session_digest, "turns": _session_turns[-_SESSION_KEEP:]}
        SESSION_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.debug(f"[SESSION] persist failed: {e}")


def session_load() -> None:
    global _session_digest, _session_turns
    if not SESSION_FILE.exists():
        return
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        _session_digest = str(data.get("digest") or "")[:_SESSION_DIGEST_MAX]
        turns = data.get("turns") or []
        if isinstance(turns, list):
            _session_turns = [t for t in turns if isinstance(t, dict)][-_SESSION_KEEP:]
    except Exception as e:
        log.debug(f"[SESSION] load failed: {e}")


# ── Windows / desktop ───────────────────────────────────────────────────────

def _active_window():
    try:
        import pygetwindow as gw
        return gw.getActiveWindow()
    except Exception:
        return None


def window_show_desktop() -> str:
    try:
        import pyautogui
        pyautogui.hotkey("win", "d")
        return "Рабочий стол, сэр."
    except Exception as e:
        return f"Не удалось свернуть окна: {e}"


def window_minimize_active() -> str:
    win = _active_window()
    if not win:
        return "Не вижу активного окна, сэр."
    try:
        win.minimize()
        return "Свернул окно, сэр."
    except Exception as e:
        return f"Не удалось свернуть: {e}"


def window_maximize_active() -> str:
    win = _active_window()
    if not win:
        return "Не вижу активного окна, сэр."
    try:
        win.maximize()
        return "На весь экран, сэр."
    except Exception as e:
        return f"Не удалось развернуть: {e}"


def window_close_active() -> str:
    win = _active_window()
    if not win:
        return "Не вижу активного окна, сэр."
    title = (win.title or "окно").strip()[:60]
    try:
        win.close()
        return f"Закрыл «{title}», сэр."
    except Exception as e:
        return f"Не удалось закрыть: {e}"


def window_switch(query: str) -> str:
    q = (query or "").strip().lower()
    if not q:
        return "Не понял, на какое окно переключиться, сэр."
    try:
        import pygetwindow as gw
        wins = [w for w in gw.getAllWindows() if w.title and w.title.strip()]
        scored = []
        for w in wins:
            title = w.title.lower()
            if q in title:
                scored.append((0, len(title), w))
            elif any(part and part in title for part in q.split()):
                scored.append((1, len(title), w))
        if not scored:
            return f"Окно «{query}» не найдено, сэр."
        scored.sort(key=lambda x: (x[0], x[1]))
        win = scored[0][2]
        try:
            if win.isMinimized:
                win.restore()
            win.activate()
        except Exception:
            # activate() is flaky on Windows; fallback hotkey path
            try:
                win.minimize()
                win.restore()
            except Exception:
                pass
        return f"Переключился на «{win.title[:60]}», сэр."
    except Exception as e:
        return f"Не удалось переключить окно: {e}"


# ── Clipboard ───────────────────────────────────────────────────────────────

def clipboard_read() -> str:
    try:
        import pyperclip
        text = (pyperclip.paste() or "").strip()
        if not text:
            return "Буфер обмена пуст, сэр."
        speakable = re.sub(r"\s+", " ", text)[:280]
        return f"В буфере, сэр: {speakable}"
    except Exception as e:
        return f"Не удалось прочитать буфер: {e}"


def clipboard_paste() -> str:
    try:
        import pyautogui
        import pyperclip
        text = pyperclip.paste() or ""
        if not text:
            return "Буфер обмена пуст, сэр."
        time.sleep(0.15)
        pyautogui.hotkey("ctrl", "v")
        return "Вставил из буфера, сэр."
    except Exception as e:
        return f"Не удалось вставить: {e}"


def clipboard_copy_text(text: str) -> str:
    try:
        import pyperclip
        pyperclip.copy(text or "")
        return "Скопировал в буфер, сэр."
    except Exception as e:
        return f"Не удалось скопировать: {e}"


# ── Reminders ───────────────────────────────────────────────────────────────

_reminders: list[dict] = []
_reminders_lock = threading.Lock()
_reminder_thread_started = False
_speak_fn = None


def reminders_set_speak(fn) -> None:
    global _speak_fn
    _speak_fn = fn


def _load_reminders() -> None:
    global _reminders
    if not REMINDERS_FILE.exists():
        _reminders = []
        return
    try:
        data = json.loads(REMINDERS_FILE.read_text(encoding="utf-8"))
        _reminders = [r for r in (data or []) if isinstance(r, dict) and r.get("when") and r.get("text")]
    except Exception:
        _reminders = []


def _save_reminders() -> None:
    try:
        REMINDERS_FILE.write_text(
            json.dumps(_reminders, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning(f"[REMIND] save failed: {e}")


def parse_reminder_request(text: str) -> tuple[datetime.datetime, str] | None:
    """Parse 'напомни в 18:30 …' / 'напомни через 2 часа …'."""
    t = re.sub(r"\s+", " ", (text or "").strip().lower()).strip(" .,!?:;")
    if not t.startswith("напомн"):
        return None
    now = datetime.datetime.now()

    m = re.match(
        r"напомн\w*\s+(?:мне\s+)?в\s+(\d{1,2})(?:[:\.](\d{2}))?\s+(.+)$", t)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2) or 0)
        body = m.group(3).strip()
        if not (0 <= hh <= 23 and 0 <= mm <= 59 and body):
            return None
        when = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if when <= now:
            when += datetime.timedelta(days=1)
        return when, body

    m = re.match(
        r"напомн\w*\s+(?:мне\s+)?через\s+(\d+)\s*"
        r"(минут\w*|мин|час\w*|часа|часов|секунд\w*|сек)\s*(.*)$", t)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        body = (m.group(3) or "").strip() or "напоминание"
        if unit.startswith("час"):
            delta = datetime.timedelta(hours=n)
        elif unit.startswith("сек"):
            delta = datetime.timedelta(seconds=n)
        else:
            delta = datetime.timedelta(minutes=n)
        return now + delta, body

    return None


def reminder_add(when: datetime.datetime, text: str) -> str:
    with _reminders_lock:
        _load_reminders()
        _reminders.append({
            "when": when.isoformat(timespec="seconds"),
            "text": text.strip()[:300],
        })
        _reminders.sort(key=lambda r: r["when"])
        _save_reminders()
    return f"Напомню в {when.strftime('%H:%M')}: {text.strip()[:80]}, сэр."


def reminder_add_in_seconds(seconds: int, text: str) -> str:
    seconds = max(5, int(seconds))
    return reminder_add(datetime.datetime.now() + datetime.timedelta(seconds=seconds), text)


def reminders_list() -> str:
    with _reminders_lock:
        _load_reminders()
        if not _reminders:
            return "Активных напоминаний нет, сэр."
        parts = []
        for i, r in enumerate(_reminders[:8], 1):
            try:
                when = datetime.datetime.fromisoformat(r["when"]).strftime("%H:%M")
            except Exception:
                when = r["when"]
            parts.append(f"{i}) в {when} — {r['text']}")
        return "Напоминания, сэр: " + "; ".join(parts)


def _reminder_worker() -> None:
    while True:
        try:
            now = datetime.datetime.now()
            due = []
            with _reminders_lock:
                _load_reminders()
                keep = []
                for r in _reminders:
                    try:
                        when = datetime.datetime.fromisoformat(r["when"])
                    except Exception:
                        continue
                    if when <= now:
                        due.append(r)
                    else:
                        keep.append(r)
                if due:
                    _reminders[:] = keep
                    _save_reminders()
            for r in due:
                msg = f"Напоминание, сэр: {r.get('text', '')}"
                log.info(f"[REMIND] fire: {msg}")
                fn = _speak_fn
                if fn:
                    try:
                        fn(msg)
                    except Exception as e:
                        log.warning(f"[REMIND] speak failed: {e}")
        except Exception as e:
            log.warning(f"[REMIND] worker: {e}")
        time.sleep(12)


def start_reminder_worker(speak_fn=None) -> None:
    global _reminder_thread_started
    if speak_fn:
        reminders_set_speak(speak_fn)
    with _reminders_lock:
        _load_reminders()
    if _reminder_thread_started:
        return
    _reminder_thread_started = True
    threading.Thread(target=_reminder_worker, daemon=True, name="jarvis-reminders").start()


# ── Files ───────────────────────────────────────────────────────────────────

_FILE_ROOTS = [
    Path.home() / "Downloads",
    Path.home() / "Desktop",
    Path.home() / "Documents",
    Path.home() / "Pictures",
]


def open_latest_download() -> str:
    folder = Path.home() / "Downloads"
    if not folder.is_dir():
        return "Папка загрузок не найдена, сэр."
    files = [p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")]
    if not files:
        return "В загрузках пусто, сэр."
    latest = max(files, key=lambda p: p.stat().st_mtime)
    try:
        os.startfile(str(latest))  # noqa: S606 — Windows open
        return f"Открыл последнюю загрузку: {latest.name}, сэр."
    except Exception as e:
        return f"Не удалось открыть файл: {e}"


def find_files(query: str, limit: int = 5) -> str:
    q = (query or "").strip().lower()
    if not q:
        return "Не понял, какой файл искать, сэр."
    hits: list[tuple[float, Path]] = []
    for root in _FILE_ROOTS:
        if not root.is_dir():
            continue
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                # keep walk shallow-ish
                depth = Path(dirpath).relative_to(root).parts
                if len(depth) > 3:
                    dirnames[:] = []
                    continue
                dirnames[:] = [d for d in dirnames if not d.startswith(".")
                               and d.lower() not in {"node_modules", ".git", "__pycache__"}]
                for name in filenames:
                    if q in name.lower():
                        p = Path(dirpath) / name
                        try:
                            hits.append((p.stat().st_mtime, p))
                        except OSError:
                            continue
                if len(hits) >= 40:
                    break
        except Exception:
            continue
        if len(hits) >= 40:
            break
    if not hits:
        return f"Файл «{query}» не нашёл, сэр."
    hits.sort(key=lambda x: x[0], reverse=True)
    top = hits[:limit]
    # auto-open best match if unique-ish
    best = top[0][1]
    try:
        os.startfile(str(best))
    except Exception:
        pass
    names = ", ".join(p.name for _, p in top)
    return f"Нашёл и открыл {best.name}. Ещё: {names}, сэр." if len(top) > 1 else f"Открыл {best.name}, сэр."


def open_path(path_str: str) -> str:
    p = Path((path_str or "").strip().strip('"'))
    if not p.exists():
        return f"Путь не найден: {path_str}, сэр."
    try:
        os.startfile(str(p))
        return f"Открыл {p.name}, сэр."
    except Exception as e:
        return f"Не удалось открыть: {e}"


# ── OCR ─────────────────────────────────────────────────────────────────────

def _grab_screenshot(active_window_only: bool = False):
    from PIL import ImageGrab
    if active_window_only:
        win = _active_window()
        if win:
            try:
                left, top, right, bottom = win.left, win.top, win.right, win.bottom
                if right > left and bottom > top:
                    return ImageGrab.grab(bbox=(left, top, right, bottom))
            except Exception:
                pass
    return ImageGrab.grab()


def _ocr_image(img) -> str | None:
    # 1) pytesseract
    try:
        import pytesseract
        text = pytesseract.image_to_string(img, lang="rus+eng")
        text = re.sub(r"\s+", " ", (text or "")).strip()
        if text:
            return text
    except Exception as e:
        log.debug(f"[OCR] pytesseract: {e}")

    # 2) tesseract CLI
    tmp = JARVIS_DIR / "logs" / "_ocr_tmp.png"
    try:
        tmp.parent.mkdir(exist_ok=True)
        img.save(tmp)
        r = subprocess.run(
            ["tesseract", str(tmp), "stdout", "-l", "rus+eng"],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
        text = re.sub(r"\s+", " ", (r.stdout or "")).strip()
        if text:
            return text
    except Exception as e:
        log.debug(f"[OCR] cli: {e}")
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
    return None


def ocr_screen(active_window_only: bool = False) -> str:
    try:
        img = _grab_screenshot(active_window_only=active_window_only)
    except Exception as e:
        return f"Не удалось сделать снимок: {e}"
    text = _ocr_image(img)
    if not text:
        return ("Не смог прочитать текст с экрана, сэр. "
                "Установите Tesseract OCR (и пакет pytesseract), затем повторите.")
    speakable = text[:400]
    return f"На экране, сэр: {speakable}"


# ── Gmail readonly ──────────────────────────────────────────────────────────

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


def _gmail_service():
    """Return an authorized Gmail service or a short user-facing error."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        return None, "Google API библиотеки не установлены, сэр."

    creds = None
    token_path = JARVIS_DIR / "token.json"
    cred_path = JARVIS_DIR / "credentials.json"
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
            if not creds.has_scopes(GMAIL_SCOPES):
                creds = None
        except Exception:
            creds = None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
        if not creds or not creds.valid:
            if not cred_path.exists():
                return None, "Нужен credentials.json для Gmail, сэр."
            try:
                flow = InstalledAppFlow.from_client_secrets_file(str(cred_path), GMAIL_SCOPES)
                creds = flow.run_local_server(port=0)
                token_path.write_text(creds.to_json(), encoding="utf-8")
            except Exception as exc:
                return None, f"Не удалось подключить Gmail: {exc}"
    try:
        return build("gmail", "v1", credentials=creds, cache_discovery=False), ""
    except Exception as exc:
        return None, f"Не удалось открыть Gmail: {exc}"


def gmail_unread(max_results: int = 5) -> str:
    """List unread inbox subjects. Reuses token.json; may require re-auth for new scope."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        return "Google API библиотеки не установлены, сэр."

    creds = None
    token_path = JARVIS_DIR / "token.json"
    cred_path = JARVIS_DIR / "credentials.json"
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
        except Exception:
            creds = None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
        if not creds or not creds.valid:
            if not cred_path.exists():
                return "Нужен credentials.json для Gmail, сэр."
            try:
                flow = InstalledAppFlow.from_client_secrets_file(str(cred_path), GMAIL_SCOPES)
                creds = flow.run_local_server(port=0)
                token_path.write_text(creds.to_json(), encoding="utf-8")
            except Exception as e:
                return (f"Не удалось авторизовать Gmail: {e}. "
                        "Удалите token.json и повторите, чтобы выдать доступ на чтение почты.")
    try:
        service = build("gmail", "v1", credentials=creds)
        result = service.users().messages().list(
            userId="me", q="is:unread in:inbox", maxResults=max_results).execute()
        messages = result.get("messages") or []
        if not messages:
            return "Непрочитанных писем нет, сэр."
        lines = []
        for item in messages:
            msg = service.users().messages().get(
                userId="me", id=item["id"], format="metadata",
                metadataHeaders=["From", "Subject"]).execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            subj = headers.get("Subject") or "(без темы)"
            frm = headers.get("From") or ""
            frm_short = re.sub(r"<.*?>", "", frm).strip() or frm
            lines.append(f"{frm_short}: {subj}")
        return "Непрочитанные, сэр: " + "; ".join(lines)
    except Exception as e:
        err = str(e)
        if "insufficient" in err.lower() or "403" in err:
            return ("Нет доступа к Gmail. Удалите token.json и снова авторизуйтесь "
                    "с правом чтения почты, сэр.")
        return f"Ошибка Gmail: {e}"


def gmail_search(query: str, max_results: int = 5) -> str:
    service, error = _gmail_service()
    if not service:
        return error
    query = (query or "").strip()
    if not query:
        return "Не понял, что искать в почте, сэр."
    try:
        result = service.users().messages().list(
            userId="me", q=query, maxResults=max(1, min(max_results, 10))).execute()
        items = result.get("messages") or []
        if not items:
            return "Писем по этому запросу не нашёл, сэр."
        lines = []
        for item in items:
            msg = service.users().messages().get(
                userId="me", id=item["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"]).execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            sender = re.sub(r"<.*?>", "", headers.get("From", "")).strip()
            lines.append(f"{sender}: {headers.get('Subject') or '(без темы)'}")
        return "Нашёл, сэр: " + "; ".join(lines)
    except Exception as exc:
        return f"Ошибка поиска Gmail: {exc}"


def gmail_send(to: str, subject: str, body: str) -> str:
    """Send one plain-text message. Caller must obtain user confirmation first."""
    service, error = _gmail_service()
    if not service:
        return error
    to = (to or "").strip()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", to):
        return "Некорректный адрес получателя, сэр."
    message = email.message.EmailMessage()
    message["To"] = to
    message["Subject"] = (subject or "Без темы").strip()[:180]
    message.set_content((body or "").strip())
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    try:
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return f"Письмо отправлено на {to}, сэр."
    except Exception as exc:
        return f"Ошибка отправки Gmail: {exc}"


# ── Local command router for new features ───────────────────────────────────

def handle_feature_command(text: str, last_reply: str = "") -> str | None:
    """Return a spoken reply if `text` matches a new local feature command."""
    t = re.sub(r"\s+", " ", (text or "").strip().lower()).strip(" .,!?:;")
    if not t:
        return None

    # Reminders
    parsed = parse_reminder_request(t)
    if parsed:
        when, body = parsed
        return reminder_add(when, body)
    if re.fullmatch(r"(?:покажи|прочитай)?\s*напоминани\w*", t):
        return reminders_list()

    # Session
    if re.fullmatch(r"(?:что мы (?:обсуждали|говорили)|резюме сессии|кратко по сессии)", t):
        return session_summary()
    if re.fullmatch(r"(?:очисти|сбрось)\s+(?:сессию|сессионную память|контекст)", t):
        return session_clear()

    # Clipboard
    if re.fullmatch(r"(?:что в буфере|прочитай буфер|буфер обмена)", t):
        return clipboard_read()
    if re.fullmatch(r"(?:вставь(?: из буфера)?|вставь буфер)", t):
        return clipboard_paste()
    if re.fullmatch(r"(?:скопируй ответ|скопируй последнее|скопируй в буфер)", t):
        if not last_reply:
            return "Нечего копировать, сэр."
        return clipboard_copy_text(last_reply)

    # Windows
    if re.fullmatch(r"(?:покажи рабочий стол|сверни всё|сверни все|minimize all)", t):
        return window_show_desktop()
    if re.fullmatch(r"(?:сверни окно|сверни активное окно)", t):
        return window_minimize_active()
    if re.fullmatch(r"(?:разверни окно|на весь экран|maximize)", t):
        return window_maximize_active()
    if re.fullmatch(r"(?:закрой окно|закрой активное окно)", t):
        return window_close_active()
    sw = re.fullmatch(r"(?:переключи(?:сь)?(?: на)?|открой окно)\s+(.+)", t)
    if sw:
        return window_switch(sw.group(1).strip())

    # Files
    if re.fullmatch(r"(?:открой последн\w* загрузк\w*|последняя загрузка)", t):
        return open_latest_download()
    fm = re.fullmatch(r"(?:найди|открой)\s+файл\s+(.+)", t)
    if fm:
        return find_files(fm.group(1).strip())

    # OCR
    if re.fullmatch(r"(?:что на экране|прочитай экран|ocr экрана)", t):
        return ocr_screen(False)
    if re.fullmatch(r"(?:что на окне|прочитай окно|ocr окна)", t):
        return ocr_screen(True)

    # Gmail
    if re.fullmatch(
        r"(?:проверь(?: почту)?|есть ли письма|непрочитанн\w*(?: письма)?|"
        r"прочитай почту|gmail|почта)", t):
        return gmail_unread()

    # Focus mode (mute + 25 min timer via caller — here just mute + message)
    if re.fullmatch(r"(?:режим фокус|режим фокуса|focus mode)", t):
        return "__FOCUS_MODE__"

    return None


def arm_hotkey_listen(command_queue, wake_seconds: float = 10.0) -> None:
    """Register Ctrl+Alt+J → silent wake window (no console required)."""
    try:
        import keyboard
    except ImportError:
        log.warning("[HOTKEY] пакет keyboard не установлен — Ctrl+Alt+J недоступен")
        return

    def _fire():
        try:
            command_queue.put(("__HOTKEY__", float(wake_seconds)))
            log.info(f"[HOTKEY] Ctrl+Alt+J → окно команд {wake_seconds:.0f} с")
        except Exception as e:
            log.warning(f"[HOTKEY] {e}")

    try:
        keyboard.add_hotkey("ctrl+alt+j", _fire, suppress=False)
        log.info("[HOTKEY] Ctrl+Alt+J зарегистрирован")
    except Exception as e:
        log.warning(f"[HOTKEY] не удалось зарегистрировать: {e}")

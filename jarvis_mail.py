"""Почта и календарь Google.

Оба сервиса ходят под одним токеном OAuth: календарь на чтение и запись,
Gmail на чтение и отправку. Токен лежит в token.json рядом с проектом и в гит
не уходит; при смене прав его нужно удалить и авторизоваться заново.

Письмо никогда не уходит сразу: оно кладётся в общее состояние как ожидающее
подтверждения и отправляется только после явного «да» голосом.
"""

import datetime
import json
import os
import re

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import jarvis_features as _feat
import jarvis_state as _state
from jarvis_telegram import _TELEGRAM_CONFIRM_NO, _TELEGRAM_CONFIRM_YES

__all__ = [
    "SCOPES", "get_calendar_service", "read_calendar_events", "add_calendar_event",
    "email_request_send", "email_confirm_pending",
]


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




def email_request_send(to: str, subject: str, body: str) -> str:
    """Stage an email; the next explicit confirmation performs the send."""
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", (to or "").strip()):
        return "Некорректный адрес электронной почты, сэр."
    _state.pending_email_send = {
        "to": to.strip(), "subject": (subject or "Без темы").strip(),
        "body": (body or "").strip(),
    }
    _state.pending_telegram_send = None
    return (f"Подтвердите отправку письма на {to}: тема «{subject}». "
            "Скажите «подтверждаю» или «отмена».")


def email_confirm_pending(text: str) -> str | None:
    if _state.pending_email_send is None:
        return None
    normalized = re.sub(r"\s+", " ", (text or "").strip().lower())
    if normalized in _TELEGRAM_CONFIRM_NO:
        _state.pending_email_send = None
        return "Отправку письма отменил, сэр."
    if normalized in _TELEGRAM_CONFIRM_YES:
        payload = _state.pending_email_send
        _state.pending_email_send = None
        return _feat.gmail_send(payload["to"], payload["subject"], payload["body"])
    return "Ожидаю подтверждения письма: скажите «подтверждаю» или «отмена»."


# ── Lookup: Telegram username / phone + public web enrichment ───────────────

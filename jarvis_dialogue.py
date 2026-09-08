"""Local, append-only dialogue history. No replay, model calls or audio recording.

UI messages are the source of truth, not duplicated technical log_interaction
calls. JSONL is canonical; TXT is a readable mirror. Each process owns its files.
"""
import argparse
import datetime as dt
import heapq
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading
import uuid

from jarvis_config import JARVIS_DIR

DIALOGUE_DIR = JARVIS_DIR / 'logs' / 'dialogues'
_LOG = logging.getLogger('jarvis')
_ROLES = {'user': 'Вы', 'assistant': 'Jarvis', 'system': 'Система'}
_SOURCES = {'text', 'voice', 'response', 'stream', 'control', 'input'}
_STATUSES = {'received', 'displayed', 'complete', 'interrupted', 'incomplete'}
_SECRET_NAME = re.compile(r'(?:API_KEY|API_HASH|TOKEN|PASSWORD|SECRET)$', re.I)


def redact(text):
    """Best-effort masking, not a guarantee that a dialogue has no private data."""
    for name, value in list(os.environ.items()):
        if len(value) >= 8 and _SECRET_NAME.search(name):
            text = text.replace(value, '[СКРЫТО]')
    text = re.sub(r'\bsk-[A-Za-z0-9_-]{10,}\b', '[СКРЫТО]', text)
    text = re.sub(r'(?i)(\bBearer\s+)\S+', r'\1[СКРЫТО]', text)
    text = re.sub(r'''(?ix)(\b(?:[a-z_]*(?:api_key|api_hash|password|token|secret)|пароль)\b["']?\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;]+)''', r'\1[СКРЫТО]', text)
    # Preserve tabs/newlines, but prevent terminal escape/control sequences.
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)


def format_entry(entry):
    label = _ROLES.get(entry['role'], 'Система')
    header = f"[{entry['timestamp']}] {label} ({entry['source']}, {entry['status']})"
    return header + '\n' + '\n'.join('    ' + line for line in entry['text'].splitlines()) + '\n\n'


class DialogueJournal:
    def __init__(self, directory, clock=None):
        self.directory = Path(directory)
        self.session_id = uuid.uuid4().hex[:12]
        self.clock = clock or (lambda: dt.datetime.now().astimezone())
        self.lock = threading.Lock()
        self.seq = 0

    def append(self, role, text, *, source='response', status='displayed'):
        if not isinstance(text, str) or not text.strip():
            return False
        role = 'assistant' if role == 'jarvis' else role
        if role not in _ROLES or source not in _SOURCES or status not in _STATUSES:
            return False
        try:
            with self.lock:
                now = self.clock()
                self.seq += 1
                entry = {'version': 2, 'timestamp': now.isoformat(timespec='milliseconds'),
                         'session': self.session_id, 'seq': self.seq, 'role': role,
                         'source': source, 'status': status, 'text': redact(text)}
                self.directory.mkdir(parents=True, exist_ok=True)
                stem = f'dialogue_{now:%Y-%m-%d}_{self.session_id}'
                # Open/close every message: no minutes-long user-space buffer.
                # Separate session filenames avoid interleaving between processes.
                with (self.directory / (stem + '.jsonl')).open('a', encoding='utf-8', newline='\n') as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
                try:
                    with (self.directory / (stem + '.txt')).open('a', encoding='utf-8', newline='\n') as handle:
                        handle.write(format_entry(entry))
                except OSError as exc:
                    _LOG.warning('[DIALOGUE] TXT mirror unavailable: %s', type(exc).__name__)
                return True
        except (OSError, ValueError, UnicodeError) as exc:
            # A full disk must not take down commands or echo secret text in errors.
            _LOG.warning('[DIALOGUE] history write failed: %s', type(exc).__name__)
            return False


_journal = DialogueJournal(DIALOGUE_DIR)


def record_message(role, text, *, source='response', status='displayed'):
    saved = _journal.append(role, text, source=source, status=status)
    # Only an active command-owner turn consumes an answer as model context.
    # UI ingress, unrelated notifications and archived text carry no turn token.
    from jarvis_chat_memory import capture_response
    capture_response(role, text, status)
    return saved


def recent_messages(directory=DIALOGUE_DIR, *, limit=50, date=None, contains=''):
    """Read saved text only. Ignore partial/corrupt rows; memory is O(limit)."""
    limit = max(1, min(int(limit), 1000))
    if date is not None:
        date = dt.date.fromisoformat(date).isoformat()
    pattern = f'dialogue_{date or "*"}_*.jsonl'

    def entries():
        for path in sorted(Path(directory).glob(pattern)):
            try:
                with path.open(encoding='utf-8', errors='replace') as handle:
                    for line in handle:
                        try:
                            entry = json.loads(line)
                            if (not isinstance(entry, dict) or entry.get('role') not in _ROLES
                                    or entry.get('source') not in _SOURCES or entry.get('status') not in _STATUSES
                                    or not isinstance(entry.get('text'), str)):
                                continue
                            stamp = dt.datetime.fromisoformat(entry['timestamp']).timestamp()
                            if contains.casefold() not in entry['text'].casefold():
                                continue
                            yield stamp, str(entry.get('session', '')), int(entry.get('seq', 0)), entry
                        except (ValueError, KeyError, TypeError, OverflowError):
                            continue
            except OSError:
                continue
    latest = heapq.nlargest(limit, entries(), key=lambda item: item[:3])
    return [entry for _, _, _, entry in reversed(latest)]


def main(argv=None):
    # Powershell/tool pipes otherwise use an OEM encoding and garble Cyrillic.
    if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='Прочитать локальную историю Jarvis без запуска ассистента.')
    parser.add_argument('--last', type=int, default=50, help='Число последних реплик (1–1000)')
    parser.add_argument('--date', help='Дата в формате YYYY-MM-DD')
    parser.add_argument('--contains', default='', help='Показать реплики, содержащие этот текст')
    args = parser.parse_args(argv)
    try:
        messages = recent_messages(limit=args.last, date=args.date, contains=args.contains)
    except ValueError:
        parser.error('Некорректная дата; ожидается YYYY-MM-DD.')
    print(''.join(format_entry(entry) for entry in messages) if messages else
          f'Подходящих записей пока нет. Папка истории: {DIALOGUE_DIR}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

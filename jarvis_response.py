"""Presentation only: written reports, spoken summaries and literal read-back.

Nothing here parses actions or calls a model. Keep original action/file payloads
out of these formatters; use them only after execution, at the reply boundary.
"""
import html
import re
import threading
import unicodedata


class Response(str):
    """String-compatible result with an explicitly separate voice channel."""

    def __new__(cls, text, *, speech, report=False, display_text=None):
        obj = super().__new__(cls, text)
        obj.speech = speech
        obj.report = report
        obj.display_text = str(text) if display_text is None else display_text
        return obj

    def __getnewargs_ex__(self):
        return ((str(self),), {'speech': self.speech, 'report': self.report,
                              'display_text': self.display_text})


def plain_reply(text):
    """Remove decorative prose Markdown; keep fenced/inline code and URLs exact."""
    parts = re.split(r'(```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)|`[^`\n]+`|https?://[^\s<>]+)', str(text))
    for i in range(0, len(parts), 2):
        value = parts[i]
        value = re.sub(r'(?m)^\s{0,3}#{1,6}\s+', '', value)
        value = re.sub(r'(?m)^\s{0,3}>\s?', '', value)
        value = re.sub(r'(?m)^\s*[-*_]{3,}\s*$', '', value)
        value = re.sub(r'(?m)^(\s*)[-*+]\s+', r'\1', value)
        value = re.sub(r'(\*{1,3}|~~)(\S[\s\S]*?)\1', r'\2', value)
        value = re.sub(r'(?<!\w)(_+)(\S[\s\S]*?)\1(?!\w)', r'\2', value)
        value = value.translate(str.maketrans('', '', '«»“”„"'))
        parts[i] = value
    return ''.join(parts)


class SpeechFences:
    """Suppress code blocks across arbitrary token boundaries, without buffering prose."""

    def __init__(self):
        self.pending = ''
        self.fence = None

    def feed(self, text, final=False):
        self.pending += text
        result = []
        while self.pending:
            markers = (self.fence,) if self.fence else ('```', '~~~')
            found = [(self.pending.find(m), m) for m in markers if m in self.pending]
            if found:
                at, marker = min(found)
                if self.fence is None:
                    result.extend((self.pending[:at], ' Код приведён в чате. '))
                    self.fence = marker
                else:
                    self.fence = None
                self.pending = self.pending[at + len(marker):]
                continue
            keep = 0
            if not final:
                for marker in markers:
                    for n in (1, 2):
                        if self.pending.endswith(marker[:n]):
                            keep = max(keep, n)
            ready = self.pending[:-keep] if keep else self.pending
            if self.fence is None:
                result.append(ready)
            self.pending = self.pending[-keep:] if keep else ''
            break
        return ''.join(result)


def clean_speech(text):
    """TTS text, never a replacement for the original chat/file/command data."""
    value = SpeechFences().feed(str(text), final=True)
    value = html.unescape(value)
    value = re.sub(r'!\[([^\]]*)\]\([^\n]*?\)', r'\1', value)
    value = re.sub(r'\[([^\]]+)\]\([^\n]*?\)', r'\1', value)
    value = re.sub(r'<[^>\n]+>', ' ', value)
    value = re.sub(r'(?:https?://|www\.)[^\s<>]+', ' ссылка в чате ', value)
    value = re.sub(r'(?m)^[ \t]*[-:|+=*_~ \t]{3,}$', ' ', value)
    value = re.sub(r'\\([*_`#~])', r'\1', value)
    value = re.sub(r'(?m)^\s*[-*+>]\s+', '', value)
    # Retain pauses, decimal separators, minus signs and ordinary punctuation.
    # Quotes/emphasis/table separators are presentation, not spoken words.
    value = value.translate(str.maketrans('', '', '#*`~"«»“”„\'‘’'))
    value = value.translate(str.maketrans({ch: ' ' for ch in '_|[]{}<>'}))
    value = ''.join(' ' if unicodedata.category(ch).startswith('S')
                   or unicodedata.category(ch) == 'Cf' else ch for ch in value)
    return re.sub(r'\s+', ' ', value).strip()


def wants_written_report(request):
    """Only defer speech for a requested written deliverable, not search snippets."""
    t = (request or '').casefold().replace('ё', 'е')
    return bool(re.search(r'\b(?:напиши|составь|подготовь|сделай)\b.{0,60}\b(?:отчет|доклад|обзор)\b', t))


def wants_full_speech(request):
    t = (request or '').casefold().replace('ё', 'е')
    if re.search(r'\b(?:не|без)\s+(?:читай|зачитывай|озвучивай|озвучки)', t):
        return False
    return bool(re.search(r'\b(?:прочитай|зачитай|озвучь)\s+(?:(?:весь|полный|полностью|вслух)\s+)*(?:отчет|ответ|его)\b', t))


def prepare_reply(text, request='', *, incomplete=False):
    if isinstance(text, Response):
        if wants_full_speech(request):
            return Response(text, speech=str(text), report=text.report, display_text=text.display_text)
        return text
    value = plain_reply(text)
    if (wants_written_report(request) and value.strip()
            and not (len(value) < 350 and re.match(r'\s*(?:не удалось|не могу|ошибка|запрос прерван|не выполнил|ничего не выполнил)', value, re.I))):
        brief = ('Ответ прервался. Доступная часть отчёта — в чате.' if incomplete else
                 'Отчёт оставил в чате. Скажите прочитай отчёт, если хотите послушать его.')
        return Response(value, speech=value if wants_full_speech(request) else brief, report=True)
    return value


_report_lock = threading.Lock()
_last_report = ''  # Only the last displayed report in this process, no archive replay.
MAX_SAVED_REPORT = 100_000


def remember_report(text):
    global _last_report
    with _report_lock:
        # Do not silently read a truncated report or an older report in its place.
        _last_report = str(text) if len(text) <= MAX_SAVED_REPORT else ''


def read_report_reply(request):
    t = re.sub(r'\s+', ' ', (request or '').casefold().replace('ё', 'е')).strip(' .,!?:;…')
    t = re.sub(r'^пожалуйста[, ]+|[, ]+пожалуйста$', '', t)
    if not re.fullmatch(r'(?:прочитай|зачитай|озвучь)\s+(?:(?:мне|вслух|полностью|весь|последний)\s+)*'
                        r'отчет(?:\s+(?:вслух|полностью))?', t):
        return None
    with _report_lock:
        text = _last_report
    if not text:
        return 'Нет доступного отчёта для чтения в этой сессии. Сначала попросите подготовить отчёт.'
    return Response(text, speech=text, display_text='Читаю последний отчёт.')

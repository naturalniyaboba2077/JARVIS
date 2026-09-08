"""Recent project identity for new explicit read-only follow-ups, never a queued task.

No original task, history or model output is replayed. The core re-resolves the
absolute path under current permissions before every use. Ambiguity expires.
"""
from pathlib import Path
import re
import stat
import threading
import time

from jarvis_actions import is_action_discussion
from jarvis_requests import ProjectRequest, normalize

_INSPECT = r'(?:посмотри|просмотри|проверь|проверяй|изучи|оцени|проанализируй|исследуй)'
_TARGET = r'(?:его|(?:этот|тот|данный|найденный|выбранный)\s+проект)'


def followup_kind(text):
    """Recognize a whole live request, never an imperative buried in other text."""
    if is_action_discussion(text):
        return None
    t = normalize(text)
    t = re.sub(r'^(?:(?:вот|да|тогда|теперь|пожалуйста)[, ]+)+', '', t)
    acknowledged = re.match(r'^(?:ты нашел (?:правильный|нужный|тот) проект|'
                            r'это (?:тот|нужный|правильный) проект)[.,;: ]+', t)
    if acknowledged:
        t = t[acknowledged.end():]
    clause = _INSPECT + r'(?:\s+' + _TARGET + r')?'
    if (re.fullmatch(clause + r'(?:\s*(?:,\s*(?:и\s+)?|\s+и\s+|\.\s*)' + clause + r')*', t)
            and (acknowledged or re.search(_TARGET, t))):
        return 'inspect'
    if re.fullmatch(r'(?:работай|поработай)\s+(?:с ним|над ним|с этим проектом)', t):
        return 'clarify'
    return None


class ProjectContext:
    def __init__(self, *, clock=time.monotonic, ttl=180):
        self.clock, self.ttl = clock, ttl
        self._lock = threading.Lock()
        self._paths = ()
        self._expires = 0.0
        self._cancel = None

    def clear(self):
        with self._lock:
            self._paths, self._expires = (), 0.0
            self._cancel = None

    def offer(self, paths, *, cancel=None):
        # Incomplete/unreadable candidates do not silently disappear in favour
        # of another candidate. Keep the original ambiguity count intact.
        captured = []
        for raw in tuple(paths)[:8]:
            path = Path(raw)
            try:
                info = path.stat(follow_symlinks=False)
                identity = (info.st_dev, info.st_ino) if stat.S_ISDIR(info.st_mode) else None
            except OSError:
                identity = None
            captured.append((path, identity))
        with self._lock:
            self._paths = tuple(captured)
            self._expires = self.clock() + self.ttl
            self._cancel = cancel

    def request(self, text):
        kind = followup_kind(text)
        if kind is None:
            return None
        with self._lock:
            choices = (self._paths if self.clock() < self._expires and
                       not (self._cancel is not None and self._cancel.is_set()) else ())
        if len(choices) != 1:
            return ProjectRequest('', text, clarification=(
                'Уточните название или полный путь проекта: сейчас нет единственного актуального выбора.'))
        path, identity = choices[0]
        try:
            info = path.stat(follow_symlinks=False)
            valid = (identity is not None and stat.S_ISDIR(info.st_mode)
                     and not getattr(info, 'st_file_attributes', 0) & 0x400
                     and identity == (info.st_dev, info.st_ino))
        except OSError:
            valid = False
        if not valid:
            return ProjectRequest('', text, clarification='Папка проекта изменилась или недоступна. Укажите проект заново.')
        if kind == 'clarify':
            return ProjectRequest(str(path), text, clarification=(
                f'Для проекта {path.name} уточните задачу: проверить его или внести конкретные изменения?'))
        return ProjectRequest(str(path), text, mode='inspect')

"""Bounded lexical project map. Reads source, never imports or executes it."""
import hashlib
import re
import time
from pathlib import Path

from jarvis_agent_context import clip_utf8
from jarvis_fileops import read_project_bytes
from jarvis_project_checks import iter_files

SOURCE_SUFFIXES = {'.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.html', '.css', '.go', '.rs', '.java', '.cs'}
MANIFESTS = {'package.json', 'pyproject.toml', 'requirements.txt', 'cargo.toml', 'go.mod'}
SYMBOL = re.compile(r'(?m)^[ \t]*(?:(?:async\s+)?def\s+|class\s+|(?:export\s+)?(?:async\s+)?function\s+|'
                    r'(?:export\s+)?(?:const|let|var)\s+)([\w$]+)|<script\b', re.I)
MAX_FILES = 48
MAX_FILE_BYTES = 384 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024


def is_test(path):
    p = Path(path)
    return (p.name.startswith('test_') or p.name.endswith(('_test.py', '.test.js', '.test.mjs', '.test.cjs'))
            or bool({'tests', 'test', '__tests__'} & set(p.parts[:-1])))


class ProjectMap:
    def __init__(self, root, task, cancel, *, seconds=1.5):
        self.root, self.task, self.cancel = Path(root), task, cancel
        self.deadline = time.monotonic() + seconds
        self.files, self.notes, self.scanned = [], [], 0

    def stopped(self):
        return self.cancel.is_set() or time.monotonic() >= self.deadline

    def build(self):
        candidates = []
        try:
            for path in iter_files(self.root):
                if self.stopped():
                    self.notes.append('Лимит времени/отмена карты; список неполный.')
                    break
                self.scanned += 1
                name = path.name.casefold()
                if path.suffix.casefold() not in SOURCE_SUFFIXES and name not in MANIFESTS and not name.startswith('readme'):
                    continue
                relative = path.relative_to(self.root).as_posix()
                explicit = name in self.task.casefold() or relative.casefold() in self.task.casefold()
                rank = 100 * explicit + (30 if name.startswith('readme') else 0) + (20 if path.suffix in SOURCE_SUFFIXES else 0)
                rank += 10 if name in {'main.py', 'app.py', 'index.html', 'index.js', 'main.ts', 'server.py'} else 0
                rank -= 10 * is_test(relative)
                candidates.append((rank, relative, path))
        except (ValueError, OSError) as exc:
            self.notes.append('Обход ограничен: ' + type(exc).__name__)
        candidates.sort(key=lambda item: (-item[0], item[1]))
        total = 0
        for rank, relative, path in candidates[:MAX_FILES]:
            if self.stopped():
                self.notes.append('Не все выбранные файлы прочитаны: лимит времени/отмена.')
                break
            try:
                size = path.stat().st_size
                if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
                    self.notes.append('Пропущен по лимиту: ' + relative)
                    continue
                raw = read_project_bytes(self.root, path, max_bytes=min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - total))
                total += len(raw)
                text = raw.decode('utf-8', errors='strict')
                if '\x00' in text:
                    continue
                symbols = []
                for match in SYMBOL.finditer(text):
                    offset = match.start()
                    symbols.append({'name': match.group(1) or '<script>', 'offset': offset,
                                    'line': text.count('\n', 0, offset) + 1})
                    if len(symbols) >= 160:
                        break
                tokens = set(re.findall(r'[\w$]{3,}', self.task.casefold()))
                hits = [s for s in symbols if s['name'].casefold() in tokens]
                rank += 80 * bool(hits)
                self.files.append({'path': relative, 'rank': rank, 'chars': len(text),
                                   'sha256': hashlib.sha256(raw).hexdigest(), 'symbols': symbols,
                                   'target': (hits or [s for s in symbols if s['name'] == '<script>'] or symbols or
                                              [{'offset': 0, 'line': 1, 'name': ''}])[0]})
            except (ValueError, OSError):
                self.notes.append('Не прочитан: ' + relative)
        if len(candidates) > MAX_FILES:
            self.notes.append('Карта ограничена первыми 48 выбранными файлами.')
        self.files.sort(key=lambda item: (-item['rank'], item['path']))
        return self

    def recommended_reads(self):
        # One implementation window plus its README. Not full-project coverage.
        source = next((f for f in self.files if Path(f['path']).suffix in SOURCE_SUFFIXES and not is_test(f['path'])), None)
        readme = next((f for f in self.files if Path(f['path']).name.casefold().startswith('readme')), None)
        return [{'path': f['path'], 'offset': f['target']['offset'] if f is source and f['chars'] > 1400 else 0,
                 'limit': 1200} for f in (source, readme) if f is not None]

    def describe(self, limit=700):
        lines = ['Карта (лексические ориентиры, не аудит; offset в символах):']
        if self.notes:
            lines.append('Ограничения: ' + '; '.join(self.notes[:2]))
        for item in self.files[:10]:
            symbols = sorted(item['symbols'], key=lambda s: (s != item['target'], s['offset']))[:5]
            labels = '; '.join(f"{s['name']} L{s['line']} offset={s['offset']}" for s in symbols)
            lines.append(item['path'] + (' [тест]' if is_test(item['path']) else '') + ': ' + labels)
        return clip_utf8('\n'.join(lines), limit)

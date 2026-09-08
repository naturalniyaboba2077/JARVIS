"""Bounded, local name discovery shared by projects and ordinary files.

Permission roots and search starting points are different: allowing C:/ includes
Documents, not just C:/'s immediate children. No contents are read during lookup.
Links are skipped; a selected target is revalidated by the caller before use.
"""
from collections import deque
from dataclasses import dataclass
from difflib import SequenceMatcher
import os
from pathlib import Path
import re
import stat
import time

from jarvis_fileops import checked_path
import jarvis_state as state

SKIP_DIRS = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', 'appdata',
             'windows', 'program files', 'program files (x86)', 'programdata',
             '$recycle.bin', 'system volume information', 'tts_cache', 'file_history'}


def configured_roots(defaults=None):
    values = [v.strip().strip('"') for v in os.getenv('JARVIS_PROJECT_ROOTS', '').split(';') if v.strip()]
    if not values:
        values = defaults or [Path.home() / 'Documents', Path.home() / 'Desktop']
    return list(dict.fromkeys(Path(os.path.abspath(os.path.expanduser(os.path.expandvars(str(p))))) for p in values))


def inside(path, roots):
    return any(path == root or root in path.parents for root in roots)


def search_starts(roots, location=''):
    preferred = [Path.home() / name for name in ('Documents', 'Desktop', 'Downloads', 'Pictures')]
    if location:
        wanted = Path.home() / location
        return [wanted] if inside(wanted, roots) else [r for r in roots if inside(r, [wanted])]
    return list(dict.fromkeys([p for p in preferred if inside(p, roots)] + roots))


def name_key(name):
    return re.sub(r'[\W_]+', ' ', name.casefold().replace('ё', 'е')).strip()


def directory_keys(name):
    """Dotted initials are a spelling variant, not arbitrary fuzzy matching.

    Preserve the old separated spelling as well, so both ЕК and Е К can match
    Е.К. A collision with a separate folder still requires an explicit choice.
    Do not use this for filenames: their extension punctuation is significant.
    """
    compact = re.sub(r'(?<!\w)(?:[a-zа-яё]\s*\.\s*)+[a-zа-яё]\.?(?!\w)',
                     lambda m: re.sub(r'[^a-zа-яё]', '', m[0], flags=re.I), name, flags=re.I)
    return {name_key(name), name_key(compact)}


def inflected_key(name):
    # Conservative noun inflections, not fuzzy edit-distance/autocorrect. Exact
    # spelling and this form are searched together so aliases cannot hide clashes.
    words = name_key(name).split()
    result = []
    for word in words:
        if re.fullmatch(r'[а-я]{5,}', word):
            word = re.sub(r'(?:иями|ием|иям|иях|ами|ями|ия|ие|ию|ии|ов|ом|ам|ах|а|у|е|ы)$', '', word)
        result.append(word)
    return ' '.join(result)


def split_location(text):
    match = re.search(r'\s+(?:(?:котор\w+\s+)?(?:находится|лежит)\s+)?'
                      r'(?:в|из|на)\s+(?:папке?\s+)?(документ\w*|documents|'
                      r'загрузк\w*|downloads|рабоч\w+\s+стол\w*)\s*[.!?]*$', text, re.I)
    if not match:
        return text, ''
    word = name_key(match[1])
    location = ('Documents' if word.startswith(('документ', 'documents')) else
                'Downloads' if word.startswith(('загруз', 'downloads')) else 'Desktop')
    return text[:match.start()].strip(' ,'), location


@dataclass
class Matches:
    paths: list
    partial: bool = False
    cancelled: bool = False


def discover(query, *, kind='file', roots=None, location='', exact=False,
             nearest=False, limit=20, max_entries=25000, seconds=5,
             similar=False, max_depth=None):
    roots = configured_roots() if roots is None else roots
    cancel = state.PipelineCancellation()
    pending = deque((root, 0) for root in search_starts(roots, location))
    visited, found = set(), []
    started, entries, matched_depth = time.monotonic(), 0, None
    key = name_key(query) if kind == 'directory' else query.casefold().replace('ё', 'е').strip()
    if not key:
        return Matches([])
    if max_entries <= 0:
        return Matches([], True, cancel.is_set())
    inflected = inflected_key(query)
    keys = directory_keys(query)
    def matches(name):
        if similar:
            return SequenceMatcher(None, inflected, inflected_key(name)).ratio() >= 0.72
        if kind == 'directory' and exact:
            return bool(directory_keys(name) & keys) or inflected_key(name) == inflected
        candidate = name_key(name) if kind == 'directory' else name.casefold().replace('ё', 'е')
        return candidate == key if exact else key in candidate
    screened_depth = None
    while pending:
        # Look at all directory names at this breadth before listing any of
        # their contents. A huge irrelevant sibling must not consume the budget
        # before we notice an already enumerated matching name beside it.
        level = pending[0][1]
        if nearest and kind == 'directory' and level != screened_depth:
            screened_depth = level
            peers = []
            for path, peer_depth in pending:
                if peer_depth != level:
                    break
                if cancel.is_set():
                    return Matches(peers, True, True)
                if time.monotonic() - started >= seconds:
                    return Matches(peers, True)
                if max_depth is not None and level > max_depth:
                    continue
                if path in visited or not matches(path.name):
                    continue
                try:
                    checked_path(path, '.')
                    if path.is_dir() and path not in peers:
                        peers.append(path)
                except (OSError, ValueError):
                    continue
                if len(peers) >= limit:
                    return Matches(peers, True)
            if peers:
                return Matches(peers)
        base, depth = pending.popleft()
        if max_depth is not None and depth > max_depth:
            continue
        if cancel.is_set():
            return Matches(found, True, True)
        if nearest and matched_depth is not None and depth > matched_depth:
            break
        if entries >= max_entries or time.monotonic() - started >= seconds:
            return Matches(found, True)
        if base in visited:
            continue
        visited.add(base)
        try:
            checked_path(base, '.')
            if kind == 'directory' and matches(base.name) and base not in found:
                found.append(base)
                matched_depth = depth
                if len(found) >= limit:
                    return Matches(found, True)
                if nearest:
                    continue
            with os.scandir(base) as children:
                for entry in children:
                    entries += 1
                    if entries > max_entries or time.monotonic() - started >= seconds:
                        return Matches(found, True)
                    if cancel.is_set():
                        return Matches(found, True, True)
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                            continue
                        directory = stat.S_ISDIR(info.st_mode)
                        if not directory and not stat.S_ISREG(info.st_mode):
                            continue
                        path = Path(entry.path)
                        if directory:
                            if entry.name.casefold() not in SKIP_DIRS:
                                pending.append((path, depth + 1))
                        elif kind == 'file' and matches(entry.name):
                            found.append(path)
                            matched_depth = depth
                    except OSError:
                        continue
                    if len(found) >= limit:
                        return Matches(found, True)
        except (OSError, ValueError):
            continue
    return Matches(list(dict.fromkeys(found)))


class NameNotFound(ValueError):
    """Expected lookup miss; suggestions are metadata only, never resolved targets."""

    def __init__(self, message, *, suggestions=()):
        super().__init__(message)
        self.suggestions = tuple(suggestions)


class NameNeedsChoice(NameNotFound):
    """Exact ambiguity or bounded partial match; carries candidates, not authority."""


def resolve_named(query, *, kind='file', roots=None, location=''):
    roots = configured_roots() if roots is None else roots
    candidate = Path(os.path.expandvars(query.strip().strip('"«»'))).expanduser()
    if '..' in candidate.parts or candidate.drive and not candidate.is_absolute():
        raise ValueError('Укажите абсолютный путь без переходов ..')
    if candidate.is_absolute():
        if not inside(candidate, roots):
            raise ValueError('Папка находится за пределами JARVIS_PROJECT_ROOTS')
        checked_path(candidate, '.')
        if not (candidate.is_dir() if kind == 'directory' else candidate.is_file()):
            raise ValueError(f'Путь не найден: {candidate}')
        return candidate
    # Relative subpaths are tried as concrete paths; a slash is not a fuzzy name.
    if len(candidate.parts) > 1:
        paths = [base / candidate for base in search_starts(roots, location)]
        matches = Matches(list(dict.fromkeys(p for p in paths if
                          (p.is_dir() if kind == 'directory' else p.is_file()))))
    else:
        matches = discover(str(candidate), kind=kind, roots=roots, location=location, exact=True, nearest=True)
    if matches.cancelled:
        raise ValueError('Поиск прерван')
    if not matches.paths:
        suggestions = (discover(str(candidate), kind='directory', roots=roots, location=location,
                               similar=True, max_depth=2, limit=3, max_entries=2000, seconds=0.7)
                       if kind == 'directory' and len(candidate.parts) == 1 else Matches([]))
        if suggestions.cancelled:
            raise ValueError('Поиск прерван')
        hint = (' Похожие папки: ' + '; '.join(map(str, suggestions.paths)) +
                '. Повторите поручение с точным именем или полным путём; пока ничего не выполнял.'
                if suggestions.paths else ' Укажите полный путь или другое имя.')
        raise NameNotFound(f'«{query}» не найден в разрешённых папках.' +
                           (' Поиск достиг лимита.' if matches.partial else '') + hint,
                           suggestions=suggestions.paths)
    if len(matches.paths) != 1:
        raise NameNeedsChoice('Найдено несколько совпадений; укажите полный путь: ' + '; '.join(map(str, matches.paths[:8])),
                              suggestions=matches.paths[:8])
    if matches.partial:
        raise NameNeedsChoice('Поиск достиг лимита; уточните полный путь: ' + str(matches.paths[0]),
                              suggestions=matches.paths)
    checked_path(matches.paths[0], '.')
    return matches.paths[0]

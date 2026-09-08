"""Grounded local file search/open/read. Searching never launches a file."""
import os
from pathlib import Path
import re

from jarvis_paths import configured_roots, discover, resolve_named, split_location, name_key
from jarvis_fileops import read_project_bytes, checked_path
from jarvis_actions import is_action_discussion
import jarvis_state as state


def file_roots():
    return configured_roots([Path.home() / n for n in ('Documents', 'Desktop', 'Downloads', 'Pictures')])


def find_files(query, limit=5):
    query, location = split_location((query or '').strip().rstrip('.!?').strip())
    query = query.strip('"«»')
    query = os.path.expandvars(query)
    if not query:
        return 'Укажите имя файла.'
    if Path(query).is_absolute():
        try:
            return 'Найден файл (ничего не открывал): ' + str(resolve_file(query))
        except ValueError as exc:
            return str(exc)
    result = discover(query, roots=file_roots(), location=location, limit=limit + 1)
    if result.cancelled:
        return 'Поиск файлов прерван.'
    if not result.paths:
        return f'Файл «{query}» не найден.' + (' Поиск достиг лимита; уточните папку или полный путь.' if result.partial else '')
    note = '\nПоиск ограничен; для точного выбора укажите полный путь.' if result.partial else ''
    return 'Найденные файлы (ничего не открывал):\n' + '\n'.join(map(str, result.paths[:limit])) + note


def resolve_file(query, kind='file'):
    query, location = split_location((query or '').strip().rstrip('.!?').strip())
    if kind == 'directory':
        aliases = {'документы': 'Documents', 'загрузки': 'Downloads', 'рабочий стол': 'Desktop'}
        folder = aliases.get(name_key(query))
        if folder:
            query = str(Path.home() / folder)
    return resolve_named(query, kind=kind, roots=file_roots(), location=location)


def open_named(query, kind='file'):
    cancel = state.PipelineCancellation()
    try:
        target = resolve_file(query, kind)
        checked_path(target, '.')
        if cancel.is_set():
            return 'Открытие прервано.'
        os.startfile(str(target))
        return f'Передал системе команду открыть: {target}'
    except (OSError, ValueError, AttributeError) as exc:
        return f'Не открыл: {exc}'


def read_named(query):
    cancel = state.PipelineCancellation()
    try:
        target = resolve_file(query)
        if cancel.is_set():
            return 'Чтение прервано.'
        data = read_project_bytes(target.parent, target.name)
        if data.startswith((b'%PDF-', b'PK\x03\x04', b'\x89PNG')) or b'\x00' in data:
            return 'Это не обычный текстовый файл. Для этого формата нужен отдельный просмотрщик.'
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = data.decode('cp1251')
        if cancel.is_set():
            return 'Чтение прервано.'
        import jarvis_dashboard as dashboard
        dashboard.register_file(target, title='Прочитан файл')
        suffix = '\nПоказаны первые 8000 символов.' if len(text) > 8000 else ''
        return f'Файл: {target}\n{text[:8000]}{suffix}'
    except (OSError, ValueError) as exc:
        return f'Не прочитал файл: {exc}'


def list_named(query):
    cancel = state.PipelineCancellation()
    try:
        target = resolve_file(query, 'directory')
        items = []
        with os.scandir(target) as entries:
            for entry in entries:
                if cancel.is_set():
                    return 'Просмотр папки прерван.'
                items.append(entry.name + ('/' if entry.is_dir(follow_symlinks=False) else ''))
                if len(items) >= 100:
                    break
        return f'Папка: {target}\n' + '\n'.join(sorted(items, key=str.casefold)) + ('\nПоказано не более 100 элементов.' if len(items) == 100 else '')
    except (OSError, ValueError) as exc:
        return f'Не прочитал папку: {exc}'


def handle_file_command(text):
    if is_action_discussion(text):
        return None
    match = re.fullmatch(r'\s*(?:пожалуйста[, ]+)?(найди|поищи|открой|прочитай)\s+(файл|папку)\s+(.+?)\s*[!?]?\s*', text or '', re.I)
    if match:
        verb, kind, query = match.groups()
        kind = 'directory' if kind.casefold() == 'папку' else 'file'
        if verb.casefold() in {'найди', 'поищи'}:
            if kind == 'file':
                return find_files(query)
            try:
                return 'Найдена папка: ' + str(resolve_file(query, kind))
            except ValueError as exc:
                return str(exc)
        if verb.casefold() == 'открой':
            return open_named(query, kind)
        return list_named(query) if kind == 'directory' else read_named(query)
    match = re.fullmatch(r'\s*(?:покажи|перечисли)\s+(?:содержимое|файлы)\s+(?:папки|в папке)\s+(.+)', text or '', re.I)
    return list_named(match[1]) if match else None

"""Non-executing project checks. This module NEVER starts a host subprocess.

Supported commands: 'compile [path]', 'check [path]',
'python -m py_compile file.py ...', 'python -m compileall [-q] [path]'.
They map to bounded in-memory compile(), without imports, execution or .pyc.
Search is bounded literal text search, not a configurable external executable.

No container backend has been verified/configured in this deployment. Tests,
shell, git and all other commands fail closed with an explicit explanation.
Installing Docker or finding its CLI alone must never enable code execution.
A future backend needs disposable storage, no host write mounts/network/secrets,
resource limits and explicit image trust; it must not fall back to host shell.
"""

import os
import shlex
from pathlib import Path

from jarvis_fileops import FileConflict, checked_path, read_project_bytes, _history_dir, _parents

MAX_ENTRIES = 5_000
MAX_CHECK_FILES = 200
MAX_CHECK_BYTES = 20_000_000
MAX_OUTPUT = 20_000
IGNORED = {".git", ".venv", "node_modules", "__pycache__", "dist", "build"}
REFUSAL = ("Команда заблокирована: безопасный backend для исполнения кода не настроен. "
           "Доступны compile/check, python -m py_compile и python -m compileall; "
           "они проверяют синтаксис без исполнения и без .pyc. Host shell не используется.")


def parse_check(command: str):
    if not isinstance(command, str) or len(command) > 4_000:
        raise ValueError(REFUSAL)
    try:
        words = shlex.split(command)
    except ValueError:
        raise ValueError(REFUSAL) from None
    if words and words[0] in {"compile", "check"} and len(words) <= 2:
        return words[1:] or ["."]
    if words[:3] == ["python", "-m", "py_compile"] and 3 < len(words) <= 23:
        return words[3:]
    if words[:3] == ["python", "-m", "compileall"]:
        paths = words[3:]
        if paths[:1] == ["-q"]:
            paths = paths[1:]
        if len(paths) <= 1:
            return paths or ["."]
    raise ValueError(REFUSAL)


def iter_files(root: Path, relative="."):
    base = checked_path(root, relative)
    if base.is_file():
        yield base
        return
    directories = [base]
    seen = 0
    while directories:
        directory = checked_path(root, directories.pop())
        with _parents(directory / "_scan"):
            with os.scandir(directory) as entries:
                for entry in entries:
                    seen += 1
                    if seen > MAX_ENTRIES:
                        raise FileConflict("Лимит обхода проекта; укажите более узкий путь")
                    if entry.name in IGNORED or entry.name.startswith(".jarvis-"):
                        continue
                    path = Path(entry.path)
                    if path == _history_dir():
                        continue
                    checked_path(root, path)
                    if entry.is_dir(follow_symlinks=False):
                        directories.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        yield path


def run_check(root: Path, command: str) -> str:
    try:
        paths = parse_check(command)
        count = total = 0
        visited = set()
        for relative in paths:
            base = checked_path(root, relative)
            for path in iter_files(root, base):
                if path.suffix != ".py" or path in visited:
                    continue
                visited.add(path)
                source = read_project_bytes(root, path)
                count += 1
                total += len(source)
                if count > MAX_CHECK_FILES or total > MAX_CHECK_BYTES:
                    return "exit=1\nЛимит проверки; укажите более узкий путь"
                try:
                    compile(source, str(path.relative_to(root)), "exec", dont_inherit=True)
                except (SyntaxError, ValueError, RecursionError) as exc:
                    return f"exit=1\n{type(exc).__name__}: {exc}"[:MAX_OUTPUT]
        if not count:
            return "exit=1\nPython-файлы для проверки не найдены"
        return f"exit=0\nСинтаксис проверен: {count} файлов. Код не исполнялся; .pyc не создавались."
    except (ValueError, OSError) as exc:
        return str(exc) if str(exc) == REFUSAL else f"exit=1\nПроверка отклонена: {exc}"


def search_text(root: Path, query: str, relative=".") -> str:
    if not isinstance(query, str) or not query or len(query) > 1_000:
        return "Укажите непустую строку поиска длиной до 1000 символов"
    output = []
    length = total = 0
    for path in iter_files(root, relative):
        content = read_project_bytes(root, path)
        total += len(content)
        if total > MAX_CHECK_BYTES:
            return "Лимит поиска; укажите более узкий путь"
        for number, line in enumerate(content.decode("utf-8", errors="replace").splitlines(), 1):
            if query in line:
                found = f"{path.relative_to(root).as_posix()}:{number}:{line}"
                output.append(found)
                length += len(found) + 1
                if length >= MAX_OUTPUT:
                    return "\n".join(output)[:MAX_OUTPUT]
    return "\n".join(output) or "Совпадений нет"

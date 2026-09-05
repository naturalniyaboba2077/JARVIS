"""Защита от разрушительных команд и кода.

Фильтр намеренно узкий: он не даёт снести систему, репозиторий Джарвиса,
хранилище заметок или корень диска — и не мешает всему остальному. Обычные
скрипты, скачивания и удаление файлов в загрузках проходят свободно.

Это статическая эвристика, не sandbox и не гарантия для вычисляемых путей.
Обычные exec/eval намеренно разрешены; голосового подтверждения здесь нет.
Python-литералы разбираются AST; буквальные аргументы известных shell-команд
удаления также нормализуются. Полный язык shell и выражения не интерпретируются.

Модуль используется действиями ядра. Проектный агент использует отдельные
неисполняющие проверки из jarvis_project_checks, а не этот фильтр как sandbox.
"""

import ast
import ntpath
import os
import re
from pathlib import Path

from jarvis_config import JARVIS_DIR

__all__ = ["is_code_safe", "_protected_roots"]


def _canonical_path(value: str) -> str:
    value = os.path.expandvars(os.path.expanduser(value))
    # Windows spellings must normalize correctly even on the Linux server.
    windows = bool(ntpath.splitdrive(value)[0]) or value.startswith("\\")
    if not windows or os.name == "nt":
        try:
            value = str(Path(value).resolve())
        except (OSError, ValueError, RuntimeError):
            pass
    return ntpath.normpath(value.replace("/", "\\")).casefold()


def _literal_paths(code: str):
    """Decode repr/raw/unicode literals without evaluating any supplied code."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError):
        return []  # Shell literals are handled separately, without evaluation.
    return [_canonical_path(node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and node.value and "\x00" not in node.value and "\n" not in node.value]


def _shell_literal_paths(code: str):
    """Read literal deletion arguments, preserving Windows backslashes/quotes.

    This is deliberately not a shell interpreter. Computed arguments and nested
    shell programs are outside this heuristic; unrelated commands stay allowed.
    """
    tokens = re.finditer(r"""'(?:[^']|'')*'|"(?:`.|[^"`])*"|[;&|\n]|[^\s'";&|]+""", code)
    commands = {"remove-item", "rd", "rmdir", "del", "rm"}
    value_options = {"-erroraction", "-ea", "-warningaction", "-wa", "-informationaction", "-ia",
                     "-errorvariable", "-ev", "-warningvariable", "-wv", "-outvariable", "-ov",
                     "-outbuffer", "-ob", "-filter", "-include", "-exclude"}
    at_command, deleting, skip_value, options = True, False, False, True
    for match in tokens:
        token = match.group()
        low = token.lower()
        if token in {";", "&", "|", "\n"}:
            at_command, deleting, skip_value, options = True, False, False, True
            continue
        if at_command:
            # Also accept an ordinary literal cmd /c prefix, without running it.
            if low in {"cmd", "cmd.exe", "/c", "/k"}:
                continue
            deleting, at_command = low in commands, False
            continue
        if not deleting:
            continue
        if skip_value:
            skip_value = False
            continue
        if options and token == "--":
            options = False
            continue
        if options and (token.startswith("-") or low in {"/s", "/q", "/f", "/a", "/p"}):
            skip_value = low in value_options
            continue
        if token.startswith("'"):
            value = token[1:-1].replace("''", "'")  # PowerShell literal single quote
        else:
            quoted = token.startswith('"')
            value = token[1:-1] if quoted else token
            if any(char in value for char in ("$`" if quoted else "$`(){}")):
                continue  # no evaluation/interpolation of supplied code
        if value and "\x00" not in value and "\n" not in value:
            yield _canonical_path(value)


def _protected_roots() -> list:
    """Lowercased, backslash-normalised paths whose recursive deletion is blocked.

    System dirs + this repo + the Obsidian vault + anything in the PROTECTED_PATHS
    env (semicolon-separated). Everything ELSE is fair game — this is anti-wipe, not
    a general delete guard.
    """
    roots = [
        r"c:\windows",
        r"c:\program files",
        r"c:\program files (x86)",
        str(JARVIS_DIR).lower().replace("/", "\\"),
        r"c:\users\user\documents\obsidian vault",
    ]
    extra = os.getenv("PROTECTED_PATHS", "")
    roots += [p.strip().lower().replace("/", "\\") for p in extra.split(";") if p.strip()]
    return [r for r in roots if r]


# не даём снести систему или проект
def is_code_safe(code: str) -> tuple[bool, str]:
    """Anti-wipe filter (ROADMAP §2.1). Returns (ok, reason).

    Blocks ONLY: disk/boot wipe (format C:, diskpart, bcdedit), destructive system
    registry hives, and recursive deletion of a drive root or a protected root
    (system dirs, this repo, the vault, config PROTECTED_PATHS). `reason` is a short
    technical note for the log; callers speak a fixed refusal phrase.

    Everything else is allowed on purpose — exec/eval, downloads, 'malware', and
    rmtree of ordinary folders (Downloads/temp) all pass. Prefer a false-allow of a
    small op over a false-block of a legitimate script.
    """
    if not code or not isinstance(code, str):
        return False, "Пустой код."

    low = code.lower()
    norm = low.replace("/", "\\")

    if re.search(r"\bdiskpart\b", low) or re.search(r"\bbcdedit\b", low):
        return False, "diskpart/bcdedit — снос диска или загрузчика"
    if re.search(r"\bformat(?:\.com|\.exe)?\s+(?:/\S+\s+)*[a-z]:", low):
        return False, "format диска"

    if (re.search(r"reg(?:\.exe)?\s+delete\s+[^\n]*(?:hklm|hkey_local_machine)\\system", low)
            or re.search(r"remove-item\s+[^\n]*hklm:\\system", low)):
        return False, "удаление системного куста реестра"

    destructive = bool(re.search(
        r"shutil\.rmtree|\brmtree\b|\brm\s+-[rf]{1,2}\b|\brd\s+/s|\bdel\s+/s|"
        r"remove-item\b[^\n]*-recurse|os\.removedirs", low))

    if destructive:
        _bound = ("", "'", '"', ")", " ", ",", ";")
        for i in range(len(norm) - 1):
            if (norm[i].isalpha() and norm[i + 1] == ":"
                    and (i == 0 or not norm[i - 1].isalnum())):
                j = i + 2
                while j < len(norm) and norm[j] == "\\":
                    j += 1
                if norm[j:j + 1] in _bound:
                    return False, "снос корня диска"

    delete_op = destructive or bool(re.search(r"os\.remove\b|\.unlink\b|os\.rmdir\b", low))
    if delete_op:
        # Compare decoded VALUES, not doubled backslashes in Python source.
        paths = _literal_paths(code)
        if destructive:
            paths.extend(_shell_literal_paths(code))
        for value in paths:
            path = value.rstrip("\\*")
            if destructive and (re.fullmatch(r"[a-z]:", path) or value == "\\"):
                return False, "снос корня диска"
            for protected in _protected_roots():
                root = _canonical_path(protected).rstrip("\\")
                # An ordinary single-file delete in the project is allowed.
                # Canonicalizing a relative filename must not turn anti-wipe
                # into a blanket ban on every file under a protected directory.
                if (path == root or (destructive and (
                        path.startswith(root + "\\") or
                        (path and root.startswith(path + "\\"))))):
                    return False, f"снос защищённого пути ({root})"
        if destructive:
            for root in _protected_roots():
                if root in norm:
                    return False, f"снос защищённого пути ({root})"

    return True, ""

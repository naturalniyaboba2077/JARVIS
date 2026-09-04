"""Защита от разрушительных команд и кода.

Фильтр намеренно узкий: он не даёт снести систему, репозиторий Джарвиса,
хранилище заметок или корень диска — и не мешает всему остальному. Обычные
скрипты, скачивания и удаление файлов в загрузках проходят свободно.

Так сделано осознанно: широкий фильтр ломает полезную работу чаще, чем
предотвращает вред, а от настоящей ошибки всё равно спасает не он, а
подтверждение перед необратимым действием.

Модуль общий: его используют и действия ядра, и проектный агент, который
правит файлы на сервере.
"""

import os
import re

from jarvis_config import JARVIS_DIR

__all__ = ["is_code_safe", "_protected_roots"]


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
    if re.search(r"\bformat\s+(?:/\S+\s+)*[a-z]:", low):
        return False, "format диска"

    if (re.search(r"reg\s+delete\s+[^\n]*(?:hklm|hkey_local_machine)\\system", low)
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
        for root in _protected_roots():
            if root in norm:
                return False, f"снос защищённого пути ({root})"

    return True, ""

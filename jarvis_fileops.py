"""Версионируемая правка файлов: каждое изменение можно отменить.

Джарвис правит файлы на сервере, где его никто не видит, поэтому любая запись
сначала сохраняет прежнее содержимое, и только потом перезаписывает файл.
История лежит рядом с самим Джарвисом, а не внутри проекта — чтобы не мусорить
в чужом репозитории и не попасть случайно в коммит.

Отменять можно по одному изменению, начиная с последнего: «Джарвис, отмени
последнюю правку». Создание файла отменяется его удалением, перезапись —
возвратом прежнего содержимого.
"""

import datetime
import hashlib
import json
import os
import re
from pathlib import Path

from jarvis_config import JARVIS_DIR

__all__ = [
    "MAX_FILE_BYTES", "history_root", "write_versioned", "list_history",
    "undo_last", "pending_changes",
]

MAX_FILE_BYTES = 300_000

def _history_dir() -> Path:
    """Куда складывать историю. По умолчанию — рядом с Джарвисом.

    На сервере том с проектами может быть отдельным, поэтому путь можно
    переопределить через JARVIS_FILE_HISTORY.
    """
    configured = os.getenv("JARVIS_FILE_HISTORY", "").strip()
    if configured:
        return Path(os.path.expandvars(configured)).expanduser()
    return JARVIS_DIR / "file_history"


def _slug(root: Path) -> str:
    """Имя папки истории: читаемое имя проекта плюс хвост от полного пути.

    Два разных проекта могут называться одинаково, поэтому одного имени мало.
    """
    name = re.sub(r"[^\w.-]", "_", root.name) or "project"
    tail = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:8]
    return f"{name}-{tail}"


def history_root(root: Path) -> Path:
    return _history_dir() / _slug(root)


def _journal(root: Path) -> Path:
    return history_root(root) / "journal.jsonl"


def _read_journal(root: Path) -> list[dict]:
    path = _journal(root)
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue          # битую строку журнала пропускаем, а не падаем
    return records


def _append(root: Path, record: dict) -> None:
    path = _journal(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def pending_changes(root: Path) -> list[dict]:
    """Изменения, которые ещё не отменены — новые идут первыми."""
    records = _read_journal(root)
    undone = {r["target"] for r in records if r.get("action") == "undo"}
    return [r for r in reversed(records)
            if r.get("action") == "write" and r["seq"] not in undone]


def write_versioned(root: Path, target: Path, content: str) -> str:
    """Записать файл, сохранив прежнее содержимое в историю."""
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        return "Содержимое слишком большое"

    records = _read_journal(root)
    seq = max([r.get("seq", 0) for r in records], default=0) + 1
    relative = target.relative_to(root).as_posix()
    existed = target.is_file()

    backup = None
    if existed:
        if target.stat().st_size > MAX_FILE_BYTES:
            return "Файл слишком большой для версионирования"
        blobs = history_root(root) / "blobs"
        blobs.mkdir(parents=True, exist_ok=True)
        backup = f"{seq:04d}_{re.sub(r'[^0-9A-Za-zА-Яа-я._-]', '_', relative)}"
        (blobs / backup).write_bytes(target.read_bytes())

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    _append(root, {
        "seq": seq,
        "action": "write",
        "path": relative,
        "existed": existed,
        "backup": backup,
        "at": datetime.datetime.now().isoformat(timespec="seconds"),
    })
    what = "Перезаписан" if existed else "Создан"
    return f"{what}: {relative} (правка №{seq}, отменяется командой «отмени последнюю правку»)"


def undo_last(root: Path) -> str:
    """Откатить последнее неотменённое изменение."""
    changes = pending_changes(root)
    if not changes:
        return "Отменять нечего, сэр — правок в этом проекте не было."

    change = changes[0]
    target = (root / change["path"]).resolve()
    if target != root and root not in target.parents:
        return "Файл оказался за границами проекта — откат отменён."

    if change["existed"]:
        blob = history_root(root) / "blobs" / (change["backup"] or "")
        if not blob.is_file():
            return f"Резервная копия для {change['path']} потеряна — откат невозможен."
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob.read_bytes())
        done = f"Вернул прежнее содержимое: {change['path']}"
    else:
        if target.is_file():
            target.unlink()
        done = f"Удалил созданный файл: {change['path']}"

    _append(root, {
        "seq": max(r.get("seq", 0) for r in _read_journal(root)) + 1,
        "action": "undo",
        "target": change["seq"],
        "path": change["path"],
        "at": datetime.datetime.now().isoformat(timespec="seconds"),
    })
    return done + ", сэр."


def list_history(root: Path, limit: int = 10) -> str:
    """Что менялось в проекте и что ещё можно отменить."""
    changes = pending_changes(root)
    if not changes:
        return "В этом проекте правок нет, сэр."
    lines = [f"Изменения в «{root.name}», новые сверху:"]
    for change in changes[:max(1, limit)]:
        what = "перезапись" if change["existed"] else "создание"
        lines.append(f"  №{change['seq']} — {change['path']} ({what}, {change['at']})")
    if len(changes) > limit:
        lines.append(f"  … и ещё {len(changes) - limit}")
    return "\n".join(lines)

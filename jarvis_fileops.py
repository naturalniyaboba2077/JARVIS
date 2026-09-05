"""Versioned writes with durable intent, atomic replacement and crash recovery.

Cooperating writers and undo/history calls serialize across threads and processes.
Prepared operations are recovered on the next call. A changed target causes a
conflict, never unconditional replay. Old JSONL records and backups are retained;
legacy writes without a recorded after-state are visible but not auto-undone.

Leaves are never resolved through links. Reparse points, ancestor symlinks, hard
links and special files are refused. Windows directory handles pin ancestors.
This is not an OS sandbox against hostile processes running as the same user;
history must be trusted/private. Power-loss durability depends on the filesystem
(Windows has no portable directory fsync).
"""

import contextlib
import datetime
import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from pathlib import Path

from jarvis_config import JARVIS_DIR

__all__ = ["MAX_FILE_BYTES", "history_root", "write_versioned", "list_history",
           "undo_last", "pending_changes"]
MAX_FILE_BYTES = 300_000
_locks = {}
_locks_guard = threading.Lock()


class FileConflict(ValueError):
    """The filesystem no longer matches the recorded operation."""


def _history_dir() -> Path:
    configured = os.getenv("JARVIS_FILE_HISTORY", "").strip()
    raw = (Path(os.path.expandvars(configured)).expanduser() if configured
           else JARVIS_DIR / "file_history").absolute()
    # Reject links BEFORE collapsing '..': alias/../history must not hide a
    # reparse component. abspath is lexical, unlike Path.resolve(). Check the
    # normalized path too, including when a discarded prefix does not exist.
    _check_parts(raw)
    normalized = Path(os.path.abspath(raw))
    _check_parts(normalized)
    return normalized


def _slug(root: Path) -> str:
    # Preserve the historic directory naming scheme.
    name = re.sub(r"[^\w.-]", "_", root.name) or "project"
    return f"{name}-{hashlib.sha1(str(root).encode('utf-8')).hexdigest()[:8]}"


def history_root(root: Path) -> Path:
    return _history_dir() / _slug(root)


def _linked(info) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _check_parts(path: Path) -> None:
    for item in [*reversed(path.parents), path]:
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if _linked(info):
            raise FileConflict(f"Ссылки/reparse points запрещены: {item.name}")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise FileConflict(f"Жёсткие ссылки запрещены: {item.name}")
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise FileConflict(f"Специальный файл запрещён: {item.name}")


def checked_path(root: Path, relative) -> Path:
    """Lexical containment + lstat of every component, WITHOUT final resolve."""
    root = Path(root).absolute()
    value = Path(relative or ".")
    if ".." in value.parts or (value.drive and not value.is_absolute()):
        raise FileConflict("Выход за границы проекта запрещён")
    target = value if value.is_absolute() else root / value
    try:
        parts = target.relative_to(root).parts
    except ValueError:
        raise FileConflict("Выход за границы проекта запрещён") from None
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                *[f"COM{i}" for i in range(10)], *[f"LPT{i}" for i in range(10)]}
    if any(":" in p or p.endswith((".", " ")) or p.split(".")[0].upper() in reserved
           for p in parts):
        raise FileConflict("Недопустимое имя файла")
    _check_parts(target)
    return target


def _win_open(path, *, directory=False, write=False, create=False):
    """Open the entry itself, never a reparse target. Caller owns the handle."""
    import ctypes
    from ctypes import wintypes
    fn = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    fn.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    fn.restype = wintypes.HANDLE
    # Metadata-only access does NOT participate in Windows sharing checks.
    # FILE_LIST_DIRECTORY makes omission of FILE_SHARE_DELETE pin the directory.
    access = 0x81 if directory else (0xC0000000 if write else 0x80000000)
    handle = fn(str(path), access, 3, None, 4 if create else 3,
                0x00200000 | (0x02000000 if directory else 0), None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def _win_close(handle):
    import ctypes
    from ctypes import wintypes
    fn = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    fn.argtypes = [wintypes.HANDLE]
    fn.restype = wintypes.BOOL
    fn(handle)


@contextlib.contextmanager
def _parents(path: Path, create=False):
    """Pin existing parents on Windows; recheck links before file IO."""
    handles = []
    try:
        for parent in reversed(path.parents):
            if create:
                parent.mkdir(exist_ok=True)
            _check_parts(parent)
            if os.name == "nt":
                handles.append(_win_open(parent, directory=True))
                _check_parts(parent)
        yield
    finally:
        for handle in reversed(handles):
            _win_close(handle)


def _open_regular(path: Path, write=False, create=False):
    _check_parts(path)
    if os.name == "nt":
        import msvcrt
        fd = msvcrt.open_osfhandle(_win_open(path, write=write, create=create),
                                  os.O_BINARY | (os.O_RDWR if write else os.O_RDONLY))
    else:
        flags = (os.O_RDWR if write else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(path, flags | (os.O_CREAT if create else 0), 0o600)
    info = os.fstat(fd)
    if _linked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise FileConflict(f"Ожидался обычный файл без ссылок: {path.name}")
    return fd


def _snapshot(path: Path):
    try:
        fd = _open_regular(path)
    except FileNotFoundError:
        return None, None
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        data = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(data) > MAX_FILE_BYTES:
        raise FileConflict("Файл слишком большой для версионирования")
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise FileConflict("Файл изменился во время чтения")
    return {"dev": after.st_dev, "ino": after.st_ino, "size": len(data),
            "mtime": after.st_mtime_ns, "mode": stat.S_IMODE(after.st_mode),
            "sha256": hashlib.sha256(data).hexdigest()}, data


def read_project_bytes(root: Path, relative) -> bytes:
    path = checked_path(root, relative)
    with _parents(path):
        info, data = _snapshot(path)
        if info is None:
            raise FileNotFoundError(path)
        return data


def _sync_dir(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _save_new(path: Path, data: bytes, mode=None):
    _check_parts(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        if mode is not None:
            os.chmod(path, mode)
        os.fsync(stream.fileno())
    _sync_dir(path.parent)


def _atomic(path: Path, data: bytes):
    _check_parts(path)
    stage = path.with_name(".jarvis-meta-" + uuid.uuid4().hex)
    try:
        _save_new(stage, data)
        os.replace(stage, path)
        _sync_dir(path.parent)
    finally:
        if stage.exists():
            stage.unlink()


@contextlib.contextmanager
def _locked(root: Path):
    root = Path(root).absolute()
    checked_path(root, ".")
    directory = history_root(root)
    key = os.path.normcase(str(directory))
    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    if not lock.acquire(timeout=10):
        raise FileConflict("История занята другой операцией")
    try:
        with _parents(directory / "lock", create=True):
            fd = _open_regular(directory / "lock", write=True, create=True)
            acquired = False
            try:
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                deadline = time.monotonic() + 10
                while not acquired:
                    try:
                        if os.name == "nt":
                            import msvcrt
                            os.lseek(fd, 0, 0)
                            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise FileConflict("История занята другим процессом")
                        time.sleep(0.02)
                yield root
            finally:
                if acquired:
                    if os.name == "nt":
                        os.lseek(fd, 0, 0)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
    finally:
        lock.release()


def _journal(root):
    return history_root(root) / "journal.jsonl"


def _read_metadata(path):
    try:
        fd = _open_regular(path)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        return stream.read()


def _read_journal(root):
    text = _read_metadata(_journal(root)) or ""
    records = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            if not isinstance(record, dict) or record.get("action") not in {"write", "undo"}:
                raise ValueError()
            if not isinstance(record.get("seq"), int) or record["seq"] < 1:
                raise ValueError()
            records.append(record)
        except (ValueError, TypeError):
            raise FileConflict("Журнал повреждён; сохранён без изменений для восстановления") from None
    if len({r["seq"] for r in records}) != len(records):
        raise FileConflict("Повторяющиеся номера старой истории; требуется ручное восстановление")
    return records


def _append(root, record):
    # Replacement also avoids a torn append hiding the next record.
    records = _read_journal(root)
    records.append(record)
    _atomic(_journal(root), ("".join(json.dumps(r, ensure_ascii=False) + "\n"
                                    for r in records)).encode("utf-8"))


def _continues_version(write, previous):
    """The write replaced this exact current version, not just equal bytes."""
    if write is None or previous is None or write.get("path") != previous.get("path"):
        return False
    expected = previous.get("after")
    return (isinstance(expected, dict)
            and {"dev", "ino", "size", "mtime", "mode", "sha256"}.issubset(expected)
            and expected == write.get("before"))


def _pending(records):
    active = {}
    for record in records:
        if record["action"] == "write":
            active[record["seq"]] = dict(record)
        else:
            cancelled = active.pop(record["target"], None)
            # Compare against the CURRENT after-state, including prior undos.
            # Equal bytes from an independently replaced file cannot authorize
            # an identity rebind. Recheck on replay, even for old undo records.
            previous = active.get(record.get("restores"))
            expected = previous.get("after") if previous is not None else None
            restored = record.get("after")
            if (_continues_version(cancelled, previous)
                    and previous.get("path") == record.get("path")
                    and isinstance(expected, dict) and isinstance(restored, dict)
                    and all(key in expected and key in restored
                            and expected[key] == restored[key]
                            for key in ("sha256", "size", "mode"))):
                previous["after"] = dict(restored)
    return list(reversed(list(active.values())))


def _intent(root):
    return history_root(root) / "pending.json"


def _recover(root):
    text = _read_metadata(_intent(root))
    if text is None:
        return None
    txn = json.loads(text)
    record = txn["record"]
    txid = record["txid"]
    if not re.fullmatch(r"[0-9a-f]{32}", txid):
        raise FileConflict("Некорректный идентификатор транзакции")
    target = checked_path(root, record["path"])
    stage = target.with_name(".jarvis-txn-" + txid)
    with _parents(target):
        records = _read_journal(root)
        if not any(r.get("txid") == txid for r in records):
            current, _ = _snapshot(target)
            if current != txn["after"]:
                if current != txn["before"]:
                    raise FileConflict("Конфликт незавершённой операции: " + record["path"])
                if txn["after"] is None:
                    if stage.exists():
                        raise FileConflict("Конфликт временного файла отката")
                    os.replace(target, stage)  # move entry, never its referent
                else:
                    if _snapshot(stage)[0] != txn["after"]:
                        raise FileConflict("Временная версия потеряна или изменена")
                    os.replace(stage, target)
                _sync_dir(target.parent)
            _append(root, record)
        # Committed retry only cleans metadata; it never replays against a file
        # another program may have changed after the commit.
        if stage.exists():
            expected = txn["before"] if txn["after"] is None else txn["after"]
            if _snapshot(stage)[0] != expected:
                raise FileConflict("Временный файл изменён; автоматическое удаление запрещено")
            stage.unlink()
        _intent(root).unlink()
        _sync_dir(history_root(root))
    return record


def pending_changes(root: Path) -> list[dict]:
    with _locked(root) as root:
        _recover(root)
        return _pending(_read_journal(root))


def _transaction(root, target, before, data, record):
    txid = uuid.uuid4().hex
    record.update(txid=txid, version=2, at=datetime.datetime.now().isoformat(timespec="seconds"))
    stage = target.with_name(".jarvis-txn-" + txid)
    after = None
    if data is not None:
        _save_new(stage, data, before["mode"] if before else None)
        after = _snapshot(stage)[0]
    record["after"] = after
    txn = {"before": before, "after": after, "record": record}
    # Before the durable intent, the target is untouched. On failure retain a
    # possible orphan stage rather than risk deleting an unrelated entry.
    _atomic(_intent(root), json.dumps(txn, ensure_ascii=False).encode("utf-8"))
    _recover(root)


def write_versioned(root: Path, target: Path, content: str) -> str:
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        return "Содержимое слишком большое"
    with _locked(root) as root:
        _recover(root)
        target = checked_path(root, target)
        if target == root or _history_dir() == target or _history_dir() in target.parents:
            raise FileConflict("Запись в корень или служебную историю запрещена")
        relative = target.relative_to(root).as_posix()
        with _parents(target, create=True):
            before, previous = _snapshot(target)
            records = _read_journal(root)
            seq = max((r["seq"] for r in records), default=0) + 1
            backup = None
            if before is not None:
                blobs = history_root(root) / "blobs"
                blobs.mkdir(exist_ok=True)
                _check_parts(blobs)
                backup = uuid.uuid4().hex
                _save_new(blobs / backup, previous)
            record = {"seq": seq, "action": "write", "path": relative,
                      "existed": before is not None, "backup": backup, "before": before}
            _transaction(root, target, before, data, record)
        what = "Перезаписан" if before else "Создан"
        return f"{what}: {relative} (правка №{seq}, отменяется командой «отмени последнюю правку»)"


def undo_last(root: Path) -> str:
    try:
        with _locked(root) as root:
            recovered = _recover(root)
            if recovered and recovered["action"] == "undo":
                return f"Завершён ранее начатый откат: {recovered['path']}, сэр."
            records = _read_journal(root)
            changes = _pending(records)
            if not changes:
                return "Отменять нечего, сэр — правок в этом проекте не было."
            change = changes[0]
            if change.get("version") != 2 or "after" not in change:
                return ("Старая правка без контрольной суммы и идентичности: " + change["path"] +
                        ". Автоматический откат небезопасен; история и резервная копия сохранены.")
            target = checked_path(root, change["path"])
            with _parents(target):
                current, _ = _snapshot(target)
                if current != change["after"]:
                    raise FileConflict("Файл изменён после правки: " + change["path"])
                data = None
                if change["existed"]:
                    backup = change["backup"]
                    if not backup or Path(backup).name != backup or ":" in backup:
                        raise FileConflict("Некорректный путь резервной копии")
                    _, data = _snapshot(history_root(root) / "blobs" / backup)
                    if data is None or hashlib.sha256(data).hexdigest() != change["before"]["sha256"]:
                        raise FileConflict("Резервная копия потеряна или изменена")
                previous = next((c for c in changes[1:] if c["path"] == change["path"]), None)
                preceding = previous["seq"] if _continues_version(change, previous) else None
                record = {"seq": max(r["seq"] for r in records) + 1, "action": "undo",
                          "target": change["seq"], "path": change["path"], "restores": preceding}
                _transaction(root, target, current, data, record)
            done = "Вернул прежнее содержимое" if change["existed"] else "Удалил созданный файл"
            return f"{done}: {change['path']}, сэр."
    except FileConflict as exc:
        return f"Откат отменён: {exc}"


def list_history(root: Path, limit: int = 10) -> str:
    changes = pending_changes(root)
    if not changes:
        return "В этом проекте правок нет, сэр."
    lines = [f"Изменения в «{root.name}», новые сверху:"]
    for change in changes[:max(1, limit)]:
        what = "перезапись" if change["existed"] else "создание"
        legacy = "; старая история, требуется ручной откат" if change.get("version") != 2 else ""
        lines.append(f"  №{change['seq']} — {change['path']} ({what}, {change['at']}{legacy})")
    if len(changes) > limit:
        lines.append(f"  … и ещё {len(changes) - limit}")
    return "\n".join(lines)

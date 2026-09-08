"""Bounded in-memory UI activity and capability-based file previews.

Cards are produced by completed tools, never parsed out of model prose. The UI
can preview only a file registered by a tool in this process, using its opaque
ID. No browser-supplied paths, shell or HTML enter this bridge.
"""

from collections import deque
import base64
import hashlib
from pathlib import Path
import threading
import time
import uuid

_lock = threading.RLock()
_activities = deque(maxlen=30)
_resources = {}
_services = {}


def service(name, *, engine="", model="", status="unknown", detail=""):
    with _lock:
        _services[name] = {"engine": engine, "model": model, "status": status,
                           "detail": detail, "at": time.time()}


def record(kind, title, detail="", **extra):
    with _lock:
        entry = {"id": uuid.uuid4().hex, "kind": kind, "title": str(title),
                 "detail": str(detail)[:4000], "at": time.time(), **extra}
        if len(_activities) == _activities.maxlen:
            _resources.pop(_activities[0]["id"], None)
        _activities.append(entry)
        return entry["id"]


def register_file(path, *, title=None, project=None, seq=None):
    """Best effort presentation; a card failure must never invalidate a write."""
    try:
        import jarvis_fileops as fileops
        path = Path(path).absolute()
        data = fileops.read_project_bytes(path.parent, path.name, max_bytes=8 * 1024 * 1024)
        with _lock:
            card_id = record("file", title or path.name, str(path),
                             filename=path.name, project=str(project or ""), seq=seq)
            _resources[card_id] = {"path": path, "sha256": hashlib.sha256(data).hexdigest(),
                                   "project": Path(project) if project else None, "seq": seq}
        return card_id
    except Exception:
        return None


def resource(card_id):
    with _lock:
        value = _resources.get(str(card_id))
        return dict(value) if value else None


def preview(card_id):
    try:
        import jarvis_fileops as fileops
        item = resource(card_id)
        if not item:
            raise ValueError("Эта карточка больше недоступна.")
        path = item["path"]
        data = fileops.read_project_bytes(path.parent, path.name, max_bytes=8 * 1024 * 1024)
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError("Файл изменён после создания карточки. Запросите его заново.")
        result = {"ok": True, "title": path.name}
        # No HTML/SVG/PDF execution in the preview. Images are explicit rasters.
        if path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
            if len(data) > 8 * 1024 * 1024:
                raise ValueError("Изображение слишком большое для предпросмотра.")
            mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            result["image"] = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
        else:
            result["text"] = data[:100_000].decode("utf-8", errors="replace")
            result["truncated"] = len(data) > 100_000
        return result
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def snapshot():
    with _lock:
        return {"activities": [dict(item) for item in reversed(_activities)],
                "services": {key: dict(value) for key, value in _services.items()}}

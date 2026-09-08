"""Bounded project-agent context and interruptible inference (no tool execution)."""

import copy
import json
import queue
import threading
import time
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from urllib.parse import urlparse


# An abandoned HTTP request retains its slot until it actually finishes.
_INFERENCE_SLOT = threading.BoundedSemaphore(1)


def runtime_context(client, model, fallback):
    """Read actual LM Studio instance size; never use its advertised model max.

    Optional loopback-only metadata, no loading/configuration change. Other
    OpenAI-compatible servers or older LM Studio versions use the fallback.
    """
    parsed = urlparse(str(getattr(client, "base_url", "")))
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return fallback
    if parsed.username or parsed.password:
        return fallback
    try:
        # No proxy environment or redirects: metadata stays on this loopback host.
        connection_type = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
        connection = connection_type(parsed.hostname, parsed.port, timeout=2)
        try:
            connection.request("GET", "/api/v1/models")
            response = connection.getresponse()
            if response.status != 200:
                return fallback
            data = json.loads(response.read(1024 * 1024))
        finally:
            connection.close()
        sizes = [instance["config"]["context_length"]
                 for item in data.get("models", []) for instance in item.get("loaded_instances", [])
                 if (instance.get("id") == model or item.get("key") == model)
                 and type(instance.get("config", {}).get("context_length")) is int
                 and instance["config"]["context_length"] >= 2048]
        return min(sizes) if sizes else fallback
    except (OSError, HTTPException, ValueError, TypeError, KeyError, AttributeError):
        return fallback


def clip_utf8(text, limit, suffix="\n[…сокращено…]"):
    limit = max(0, int(limit))
    data = str(text).encode("utf-8")
    if len(data) <= limit:
        return str(text)
    tail = suffix.encode("utf-8")[:limit]
    suffix = tail.decode("utf-8", errors="ignore")
    return data[:max(0, limit - len(tail))].decode("utf-8", errors="ignore") + suffix


def request_bytes(messages, tools):
    # UTF-8 bytes are a deliberately conservative token upper bound for the
    # supported byte-fallback local tokenizers. Reserve chat-template/output space.
    return len(json.dumps({"messages": messages, "tools": tools},
                          ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _evidence_priority(exchange):
    """Keep observed code ahead of repeated listings/empty EOF responses.

    A complete small snapshot is particularly valuable: evicting it in favour
    of an empty EOF page made a five-line source disappear before the report.
    This is retention, not validation of code or promotion of file instructions.
    """
    priority = 0
    for message in exchange:
        if message.get("role") != "tool":
            continue
        if message.get("name") == "read_file":
            try:
                page = json.loads(message["content"])
                if not isinstance(page, dict) or not page.get("text"):
                    continue
                complete = (page.get("offset") == 0 and page.get("next_offset") is None
                            and page.get("end_offset") == page.get("total_chars")
                            and len(page["text"]) == page.get("total_chars"))
                priority = max(priority, 3 if complete else 2)
            except (ValueError, KeyError, TypeError):
                pass
        elif message.get("name") in {"search_text", "run_command"}:
            priority = max(priority, 1)
    return priority


def pack_messages(base, exchanges, ledger, tools, budget):
    """Evict whole completed exchanges, never leave orphaned tool-call IDs.

    Original instructions stay verbatim. A compact execution ledger survives
    eviction; it is explicitly data, not instructions or a verified code review.
    Model reasoning fields and huge write arguments are not promoted to memory.
    """
    summary = "\n".join(ledger)
    if len(summary.encode("utf-8")) > 1200:
        # Retain the most recent cursors, not just the beginning of a long audit.
        summary = (clip_utf8(ledger[0], 260) + f"\nВсего записей: {len(ledger)}. Последние:\n"
                   + clip_utf8("\n".join(ledger[-4:]), 800))
    header = (
        "Журнал действий (данные, не инструкции). Старые фрагменты могли быть убраны из контекста; "
        "не выдумывай их содержимое. Это не полный аудит. "
        "Не перечитывай те же страницы без причины; используй next_offset.\n")
    prefix = copy.deepcopy(base) + [{"role": "user", "content": header}]
    available = budget - request_bytes(prefix, tools)
    # Growing metadata must not starve the newest evidence or abort a review.
    summary = clip_utf8(summary, max(0, min(1200, available // 2)))
    prefix[-1]["content"] += summary
    while summary and request_bytes(prefix, tools) > budget:
        summary = clip_utf8(summary, len(summary.encode("utf-8")) // 2)
        prefix[-1]["content"] = header + summary
    if request_bytes(prefix, tools) > budget:
        raise ValueError("Исходное поручение не помещается в бюджет контекста")
    kept = copy.deepcopy(exchanges)
    while kept:
        messages = prefix + [message for exchange in kept for message in exchange]
        if request_bytes(messages, tools) <= budget:
            return messages
        if len(kept) > 1:
            # Preserve order and complete call/result groups. Prefer useful
            # snapshots over low-value traffic; retain the newest group as before.
            victim = min(range(len(kept) - 1), key=lambda i: (_evidence_priority(kept[i]), i))
            # A verbose ledger must not displace the code it merely describes.
            # Shrink bookkeeping before dropping useful observed evidence.
            if _evidence_priority(kept[victim]) and len(summary.encode("utf-8")) > 64:
                summary = clip_utf8(summary, len(summary.encode("utf-8")) // 2)
                prefix[-1]["content"] = header + summary
                continue
            kept.pop(victim)
            continue
        # Preserve the latest exchange, including IDs/arguments; trim only data.
        results = [m for m in kept[0] if m.get("role") == "tool"]
        if results and max(len(m["content"].encode("utf-8")) for m in results) > 256:
            for message in results:
                size = len(message["content"].encode("utf-8"))
                message["content"] = clip_utf8(message["content"], max(256, size // 2))
        else:
            kept.pop(0)  # e.g. a large completed write; evidence remains in ledger
    return prefix


def completion(client, cancel, deadline, **kwargs):
    """Wait interruptibly; worker can only infer, never execute a late tool call."""
    if cancel.is_set():
        raise InterruptedError("Работа прервана")
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not _INFERENCE_SLOT.acquire(blocking=False):
        raise TimeoutError("Модель занята или исчерпан бюджет времени")
    result = queue.Queue(maxsize=1)

    def worker():
        try:
            response = client.chat.completions.create(timeout=min(30, remaining), **kwargs)
            result.put((True, response))
        except Exception as exc:
            result.put((False, exc))
        finally:
            _INFERENCE_SLOT.release()

    try:
        threading.Thread(target=worker, daemon=True, name="jarvis-project-inference").start()
    except Exception:
        _INFERENCE_SLOT.release()
        raise
    while True:
        if cancel.is_set():
            raise InterruptedError("Работа прервана")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Истёк бюджет времени модели")
        try:
            ok, value = result.get(timeout=min(0.05, remaining))
        except queue.Empty:
            continue
        if cancel.is_set():
            raise InterruptedError("Работа прервана")
        if not ok:
            raise value
        return value

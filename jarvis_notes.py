"""Заметки в Obsidian — долговременная память Jarvis.

Джарвис пишет и читает обычные markdown-файлы в хранилище Obsidian: свои
записи складывает в подпапку «Jarvis DB», а искать умеет по всему хранилищу.
Отсюда же берётся долговременная память, которая подмешивается в системный
промпт.

Модуль ни от чего в ядре не зависит — только стандартная библиотека, поэтому
одинаково работает и на Windows, и на домашнем сервере.
"""

import datetime
import os
import re
import time
from pathlib import Path

__all__ = [
    "OBSIDIAN_VAULT_CANDIDATES", "JARVIS_DB_FOLDER", "OBSIDIAN_CACHE_TTL",
    "ob_write", "ob_append", "ob_search", "ob_read", "ob_delete", "ob_list_notes",
    "get_obsidian_memory",
    "_get_vault", "_get_jarvis_db", "_safe_filename", "_invalidate_obsidian_cache",
]

# Кэш долговременной памяти: собирать её на каждый запрос к модели слишком дорого.
_obsidian_cache = None
_obsidian_cache_time = 0
OBSIDIAN_CACHE_TTL = 120


OBSIDIAN_VAULT_CANDIDATES = [
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Obsidian Vault")),
    os.path.abspath("Obsidian Vault"),
    r"C:\Users\user\Documents\Obsidian Vault",
]
JARVIS_DB_FOLDER = "Jarvis DB"


def _get_vault() -> str | None:
    """Return path to the Obsidian vault, or None if not found."""
    return next((p for p in OBSIDIAN_VAULT_CANDIDATES if os.path.isdir(p)), None)


def _get_jarvis_db() -> Path | None:
    """Return Path to Jarvis DB subfolder (creates it if needed)."""
    vault = _get_vault()
    if not vault:
        return None
    db = Path(vault) / JARVIS_DB_FOLDER
    db.mkdir(exist_ok=True)
    return db


def _safe_filename(title: str) -> str:
    """Convert a note title to a safe filename."""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', title)
    return safe.strip('. ') or "untitled"


def ob_write(title: str, content: str, tags: list[str] | None = None) -> str:
    """Create or overwrite a note in the Jarvis DB folder.
    
    Content is stored as Markdown with YAML frontmatter.
    """
    db = _get_jarvis_db()
    if db is None:
        return "Obsidian Vault не найден."

    fname = _safe_filename(title) + ".md"
    fpath = db / fname

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    tag_line = ""
    if tags:
        tag_line = "tags: [" + ", ".join(tags) + "]\n"

    full = (
        f"---\n"
        f"title: {title}\n"
        f"{tag_line}"
        f"created: {ts}\n"
        f"updated: {ts}\n"
        f"source: jarvis\n"
        f"---\n\n"
        f"{content.strip()}\n"
    )
    try:
        fpath.write_text(full, encoding="utf-8")
        _invalidate_obsidian_cache()
        return f"Заметка '{title}' сохранена в Obsidian."
    except Exception as e:
        return f"Ошибка записи в Obsidian: {e}"


def ob_append(title: str, text: str) -> str:
    """Append text to an existing note (or create it if it doesn't exist)."""
    db = _get_jarvis_db()
    if db is None:
        return "Obsidian Vault не найден."

    fname = _safe_filename(title) + ".md"
    fpath = db / fname

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    if fpath.exists():
        existing = fpath.read_text(encoding="utf-8")
        existing = re.sub(r'(updated: )[\d\-: ]+', f'\\g<1>{ts}', existing)
        new_content = existing.rstrip() + f"\n\n**[{ts}]** {text.strip()}\n"
        try:
            fpath.write_text(new_content, encoding="utf-8")
            _invalidate_obsidian_cache()
            return f"Добавлено в заметку '{title}'."
        except Exception as e:
            return f"Ошибка дозаписи: {e}"
    else:
        return ob_write(title, text)


def ob_search(query: str, max_results: int = 5) -> str:
    """Search all vault notes for keyword matches. Returns snippets."""
    vault = _get_vault()
    if not vault:
        return "Obsidian Vault не найден."

    query_lower = query.lower()
    results = []

    for root, _, files in os.walk(vault):
        if ".obsidian" in root:
            continue
        for fname in files:
            if not fname.lower().endswith((".md", ".markdown")):
                continue
            fpath = os.path.join(root, fname)
            try:
                text = Path(fpath).read_text(encoding="utf-8", errors="ignore")
                text_lower = text.lower()
                if query_lower in text_lower:
                    idx = text_lower.find(query_lower)
                    start = max(0, idx - 80)
                    end = min(len(text), idx + 120)
                    snippet = text[start:end].replace("\n", " ").strip()
                    rel = os.path.relpath(fpath, vault)
                    results.append(f"📄 {rel}: ...{snippet}...")
                    if len(results) >= max_results:
                        break
            except Exception:
                pass
        if len(results) >= max_results:
            break

    if not results:
        return f"По запросу '{query}' ничего не найдено в Obsidian."
    return "Нашёл в базе знаний:\n" + "\n".join(results)


def ob_list_notes(subfolder: str = JARVIS_DB_FOLDER) -> str:
    """List all notes in the Jarvis DB folder (or any subfolder of the vault)."""
    vault = _get_vault()
    if not vault:
        return "Obsidian Vault не найден."

    target = Path(vault) / subfolder
    if not target.exists():
        return f"Папка '{subfolder}' в Obsidian пуста или не существует."

    notes = sorted(target.glob("*.md"))
    if not notes:
        return "База знаний Jarvis пуста."

    names = [n.stem for n in notes[:20]]
    return "Заметки в базе Jarvis: " + ", ".join(names) + "."


def ob_read(title: str) -> str:
    """Read a specific note by title from the Jarvis DB folder."""
    db = _get_jarvis_db()
    if db is None:
        return "Obsidian Vault не найден."

    fname = _safe_filename(title) + ".md"
    fpath = db / fname

    if not fpath.exists():
        matches = list(db.glob(f"*{_safe_filename(title)}*.md"))
        if not matches:
            return f"Заметка '{title}' не найдена."
        fpath = matches[0]

    try:
        text = fpath.read_text(encoding="utf-8")
        text = re.sub(r'^---\n.*?\n---\n', '', text, flags=re.DOTALL).strip()
        if len(text) > 600:
            text = text[:600] + "... (заметка обрезана)"
        return f"Заметка '{fpath.stem}': {text}"
    except Exception as e:
        return f"Ошибка чтения: {e}"


def ob_delete(title: str) -> str:
    """Delete a note from the Jarvis DB folder."""
    db = _get_jarvis_db()
    if db is None:
        return "Obsidian Vault не найден."

    fname = _safe_filename(title) + ".md"
    fpath = db / fname
    if not fpath.exists():
        return f"Заметка '{title}' не найдена."
    try:
        fpath.unlink()
        _invalidate_obsidian_cache()
        return f"Заметка '{title}' удалена."
    except Exception as e:
        return f"Ошибка удаления: {e}"


def _invalidate_obsidian_cache():
    """Force the next obsidian read to reload from disk."""
    global _obsidian_cache_time
    _obsidian_cache_time = 0


def get_obsidian_memory(max_chars: int = 2500) -> str:
    """Load content from Obsidian Vault as long-term memory (cached).
    Bot will know notes you (or other agents) put in the vault.
    """
    global _obsidian_cache, _obsidian_cache_time
    now = time.time()
    if _obsidian_cache is not None and (now - _obsidian_cache_time) < OBSIDIAN_CACHE_TTL:
        return _obsidian_cache

    vault = _get_vault()
    if not vault:
        _obsidian_cache = ""
        _obsidian_cache_time = now
        return ""

    parts = []
    total_len = 0
    for root, _, files in os.walk(vault):
        if ".obsidian" in root:
            continue
        for fname in files:
            if fname.lower().endswith((".md", ".markdown")):
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read().strip()
                    if content:
                        rel = os.path.relpath(fpath, vault)
                        chunk = f"--- {rel} ---\n{content}\n"
                        if total_len + len(chunk) > max_chars:
                            break
                        parts.append(chunk)
                        total_len += len(chunk)
                except Exception:
                    pass

    result = "\n".join(parts)
    _obsidian_cache = result
    _obsidian_cache_time = now
    return result

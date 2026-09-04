"""Проектный агент: правит код в явно разрешённых папках.

Работает без присмотра, поэтому ограничений у него больше, чем у команд,
которые человек отдаёт голосом. Любая запись версионируется и откатывается
(jarvis_fileops), рекурсивное удаление и разрушительные операции git ему
запрещены, а выйти за корень проекта он не может.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from jarvis_fileops import MAX_FILE_BYTES, list_history, write_versioned
from jarvis_safety import is_code_safe

MAX_TOOL_STEPS = 12

# Агенту запрещено и то, что человеку разрешено: он не переспросит и не
# заметит, что снёс каталог, с которым только что работал.
_AGENT_FORBIDDEN = (
    (r"\brm\s+-[rf]{1,2}\b|remove-item\b[^\n]*-recurse|\brd\s+/s\b|\bdel\s+/s\b",
     "рекурсивное удаление"),
    (r"git\s+(?:reset\s+--hard|clean\s+-[a-z]*f|push\s+--force)",
     "разрушительная операция git"),
)


def _command_safe(command: str) -> tuple[bool, str]:
    """Общий анти-вайп плюс дополнительные запреты для автономной работы."""
    ok, reason = is_code_safe(command)
    if not ok:
        return False, reason
    low = command.lower()
    for pattern, why in _AGENT_FORBIDDEN:
        if re.search(pattern, low):
            return False, why
    return True, ""


def _roots() -> list[Path]:
    configured = os.getenv("JARVIS_PROJECT_ROOTS", "").strip()
    values = [p for p in configured.split(";") if p.strip()]
    if not values:
        values = [str(Path.home() / "Documents"), str(Path.home() / "Desktop")]
    return [Path(os.path.expandvars(p.strip())).resolve() for p in values]


def _resolve_project(project: str) -> Path:
    candidate = Path(os.path.expandvars((project or "").strip().strip('"')))
    if not candidate.is_absolute():
        matches = []
        for root in _roots():
            direct = root / candidate
            if direct.is_dir():
                matches.append(direct)
            if root.is_dir():
                matches.extend(p for p in root.iterdir()
                               if p.is_dir() and p.name.casefold() == candidate.name.casefold())
        if not matches:
            raise ValueError(f"Проект «{project}» не найден в разрешённых папках")
        candidate = matches[0]
    resolved = candidate.resolve()
    if not any(resolved == root or root in resolved.parents for root in _roots()):
        raise ValueError("Папка находится за пределами JARVIS_PROJECT_ROOTS")
    if not resolved.is_dir():
        raise ValueError("Папка проекта не существует")
    return resolved


def _inside(root: Path, relative: str) -> Path:
    target = (root / (relative or ".")).resolve()
    if target != root and root not in target.parents:
        raise ValueError("Выход за границы проекта запрещён")
    return target


def _tools() -> list[dict]:
    def tool(name, description, properties, required=None):
        return {"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": required or [], "additionalProperties": False}}}
    return [
        tool("list_files", "Список файлов проекта", {
            "path": {"type": "string"}, "limit": {"type": "integer"}}),
        tool("read_file", "Прочитать текстовый файл", {
            "path": {"type": "string"}}, ["path"]),
        tool("search_text", "Найти текст во всех файлах через ripgrep", {
            "query": {"type": "string"}, "path": {"type": "string"}}, ["query"]),
        tool("write_file", "Создать или полностью заменить текстовый файл", {
            "path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
        tool("run_command", "Запустить проверочную команду в корне проекта", {
            "command": {"type": "string"}}, ["command"]),
        tool("list_changes", "Показать свои правки в этом проекте", {}),
    ]


def _execute(root: Path, name: str, args: dict) -> str:
    if name == "list_files":
        base = _inside(root, args.get("path", "."))
        limit = max(1, min(int(args.get("limit", 200)), 500))
        if not base.exists():
            return "Путь не найден"
        ignored = {".git", ".venv", "node_modules", "__pycache__", "dist", "build"}
        items = []
        for path in base.rglob("*"):
            if any(part in ignored for part in path.parts):
                continue
            items.append(str(path.relative_to(root)).replace("\\", "/"))
            if len(items) >= limit:
                break
        return "\n".join(items)
    if name == "read_file":
        path = _inside(root, args["path"])
        if not path.is_file():
            return "Файл не найден"
        if path.stat().st_size > MAX_FILE_BYTES:
            return "Файл слишком большой"
        return path.read_text(encoding="utf-8", errors="replace")
    if name == "search_text":
        base = _inside(root, args.get("path", "."))
        proc = subprocess.run(
            ["rg", "-n", "--hidden", "-g", "!.git/**", "-g", "!.venv/**",
             "-g", "!node_modules/**", args["query"], str(base)],
            cwd=root, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (proc.stdout or proc.stderr or "Совпадений нет")[:20_000]
    if name == "write_file":
        path = _inside(root, args["path"])
        return write_versioned(root, path, args["content"])
    if name == "list_changes":
        return list_history(root)
    if name == "run_command":
        command = args["command"].strip()
        ok, reason = _command_safe(command)
        if not ok:
            return f"Разрушительная команда заблокирована: {reason}"
        # На сервере ядро крутится под Linux, где powershell отсутствует.
        argv = (["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
                if os.name == "nt" else ["/bin/sh", "-c", command])
        proc = subprocess.run(
            argv,
            cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return f"exit={proc.returncode}\n{(proc.stdout + proc.stderr)[:20_000]}"
    return "Неизвестный инструмент"


def run_project_agent(client, model: str, project: str, task: str) -> str:
    """Run a bounded OpenAI-compatible tool loop and return its final report."""
    root = _resolve_project(project)
    messages = [{
        "role": "system",
        "content": (
            "Ты работаешь как самостоятельный senior-разработчик внутри одного проекта. "
            "Сначала изучи код, затем внеси минимальные изменения, запусти подходящие тесты "
            "и кратко отчитайся. Не выходи за корень проекта. Не удаляй проект и системные файлы."
        )}, {
        "role": "user", "content": f"Проект: {root.name}\nЗадача: {task}"
    }]
    tools = _tools()
    for _ in range(MAX_TOOL_STEPS):
        response = client.chat.completions.create(
            model=model, messages=messages, tools=tools, tool_choice="auto",
            temperature=0.2, max_tokens=1200, timeout=60)
        msg = response.choices[0].message
        messages.append(msg)
        calls = getattr(msg, "tool_calls", None) or []
        if not calls:
            return (msg.content or "Задача завершена без отчёта.").strip()
        for call in calls:
            try:
                args = json.loads(call.function.arguments or "{}")
                output = _execute(root, call.function.name, args)
            except Exception as exc:
                output = f"Ошибка инструмента: {type(exc).__name__}: {exc}"
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "name": call.function.name, "content": output})
    return "Агент остановлен после лимита шагов. Проверьте изменения и продолжите отдельной задачей."

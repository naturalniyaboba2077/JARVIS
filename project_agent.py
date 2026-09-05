"""Проектный агент: правит код в явно разрешённых папках.

Записи проходят через транзакционный jarvis_fileops. Проверки compile/check
компилируют Python в памяти без исполнения. Произвольный shell и запуск тестов
без отдельно проверенного изолированного backend запрещены. Это ограничение
инструментов, не sandbox всего процесса; детали — jarvis_project_checks.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from jarvis_fileops import checked_path, list_history, read_project_bytes, write_versioned
from jarvis_project_checks import iter_files, parse_check, run_check, search_text

MAX_TOOL_STEPS = 12


def _command_safe(command: str) -> tuple[bool, str]:
    """Compatibility predicate for the non-executing check command grammar."""
    try:
        parse_check(command)
        return True, ""
    except ValueError as exc:
        return False, str(exc)


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
    return checked_path(root, relative)


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
        tool("search_text", "Буквальный поиск текста в файлах, без регулярных выражений", {
            "query": {"type": "string"}, "path": {"type": "string"}}, ["query"]),
        tool("write_file", "Создать или полностью заменить текстовый файл", {
            "path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
        tool("run_command", "Проверить синтаксис: compile/check [path], python -m py_compile file.py. "
             "Код и тесты не исполняются; произвольные shell/git-команды запрещены.", {
            "command": {"type": "string"}}, ["command"]),
        tool("list_changes", "Показать свои правки в этом проекте", {}),
    ]


def _execute(root: Path, name: str, args: dict) -> str:
    if name == "list_files":
        base = _inside(root, args.get("path", "."))
        limit = max(1, min(int(args.get("limit", 200)), 500))
        if not base.exists():
            return "Путь не найден"
        items = []
        for path in iter_files(root, base):
            items.append(str(path.relative_to(root)).replace("\\", "/"))
            if len(items) >= limit:
                break
        return "\n".join(items)
    if name == "read_file":
        path = _inside(root, args["path"])
        if not path.is_file():
            return "Файл не найден"
        return read_project_bytes(root, path).decode("utf-8", errors="replace")
    if name == "search_text":
        return search_text(root, args["query"], args.get("path", "."))
    if name == "write_file":
        path = _inside(root, args["path"])
        return write_versioned(root, path, args["content"])
    if name == "list_changes":
        return list_history(root)
    if name == "run_command":
        return run_check(root, args["command"])
    return "Неизвестный инструмент"


def run_project_agent(client, model: str, project: str, task: str) -> str:
    """Run a bounded OpenAI-compatible tool loop and return its final report."""
    root = _resolve_project(project)
    messages = [{
        "role": "system",
        "content": (
            "Ты работаешь как самостоятельный senior-разработчик внутри одного проекта. "
            "Сначала изучи код, затем внеси минимальные изменения и проверь синтаксис командой "
            "compile или check. Эти проверки не исполняют код. Тесты и shell недоступны без "
            "изолированного backend; не утверждай, что тесты прошли. Кратко отчитайся. "
            "Не выходи за корень проекта. Не удаляй проект и системные файлы."
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

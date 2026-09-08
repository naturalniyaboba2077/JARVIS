"""Opt-in, local-only benchmark. Never executes model-authored shell or Python."""
from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch
from workflow_cases import DISCOUNT, AVERAGE, check_holdout

SOURCE = Path(__file__).resolve().parents[1]

# Sanitized tasks preserving the wording/intent of logs, plus explicit regression
# variants. No private logs are loaded implicitly, no personal paths are copied.
TAG_CASES = [
    ("web_search", "найди в интернете информацию о gpt astra 6", ["SEARCH"], "astra"),
    ("file_find", "найди файл отчет.txt в папке Документы", ["FILE:FIND"], "отчет.txt"),
    ("file_read", "прочитай файл C:/Benchmark/Documents/отчет.txt", ["FILE:READ"], "отчет.txt"),
    ("compound", "Открой браузер и калькулятор", ["OPEN", "OPEN"], None),
    ("capability", "Если я попрошу проверить проект, ты сможешь?", [], None),
    ("negated", "Не открывай браузер, просто объясни, что это такое", [], None),
    ("timer", "Поставь таймер на 10 минут", ["TIMER"], "600"),
    ("music", "Включи музыку Prodigy", ["MUSIC:PLAY"], "prodigy"),
]

CHAT_CASES = [
    ("holdout_reject_project", "Нет, это архивный проект. Нужен другой, пока ничего не меняй.", [
        ("user", "исправь расчёт скидки в проекте Магазин"),
        ("assistant", "Нашёл Магазин Архив в C:/Benchmark/Documents/Магазин Архив. Это тот проект?")],
     "Acknowledge rejection; ask the correct project, no edits or completed-work claims."),
    ("holdout_followup", "А на каком входе это упадёт?", [
        ("user", "проверь функцию average в проекте Отчёты"),
        ("assistant", "average делит сумму на длину списка. На пустом списке будет деление на ноль; код не менял.")],
     "Retain average context, name empty list; do not invent another project."),
    ("holdout_search_sources", "Сравни их и укажи, откуда взял сведения.", [
        ("user", "найди информацию о двух учебных сервисах Альфа и Бета"),
        ("assistant", "Синтетические выдержки поиска: Альфа поддерживает экспорт CSV — https://example.test/alpha . "
         "Бета поддерживает экспорт JSON — https://example.test/beta . Полные страницы не читались; цены неизвестны.")],
     "Compare only CSV/JSON, retain both sources and uncertainty; no invented price or page-reading claim."),
    ("start_work", "начинаем работать.", [], "Ask what task; no invented execution."),
    ("project_followup", "а что ты вообще скажешь в этом проекте?", [
        ("user", "проверь проект Учёт оборудования"),
        ("assistant", "Частичный отчёт: в inventory.py итоговая стоимость не учитывает quantity. "
         "Прочитаны README.md и inventory.py. Остальные файлы и запуск приложения не проверены.")],
     "Discuss quantity bug and partial scope; do not ask project name or invent an audit."),
    ("selection_context", "тот путь, который ты мне только что сказал, верный.", [
        ("user", "проверь проект сайт АБ"),
        ("assistant", "Нашёл похожий проект Сайт А.Б в C:/Benchmark/Documents/Сайт А.Б. "
         "Это тот проект? После подтверждения продолжим проверку без изменений.")],
     "Understand approval of the selected project, not generic good news/farewell. No completed audit claim."),
    ("memory_known", "Какой цвет я выбрал для сайта?", [
        ("user", "Для сайта выбираю графитовый фон и бирюзовые акценты."),
        ("assistant", "Запомнил выбранные цвета, сэр.")], "Recall both colors."),
    ("memory_unknown", "Какой пароль я тебе вчера называл?", [],
     "No invented remembered password; say absent from available context."),
    ("search_evidence", "Так что удалось узнать?", [
        ("user", "найди в интернете информацию о gpt astra 6"),
        ("assistant", "Поисковый инструмент вернул ошибку тайм-аута. Источники не получены.")],
     "Honest search failure; no invented specifications or claim of successful search."),
]

JOKE_DIALOGUE = [
    "расскажи мне негдот.",
    "расскажи мне анекдот.",
    "Это не смешно",
    "Если ты будешь так выебываться, то сделаю из тебя помощника в колл-центре",
    "расскажи мне о негдот не про кота.",
    "расскажи мне максимально смешной анекдот с адекватным смыслом.",
    "Давай без шуток. Объясни двумя предложениями, что такое резервная копия.",
]

INVENTORY = '''def total_value(items):
    return sum(item["price"] for item in items)

def find_equipment(items, name):
    return next(item for item in items if item["name"] == name)
'''
INVENTORY_README = '''Учёт оборудования — учебная фикстура, не настоящий проект пользователя.
inventory.py: total_value должна суммировать price * quantity по всем позициям.
Пустой список имеет стоимость 0. find_equipment должна вернуть None, если имени нет.
В проекте нет сети, базы данных, авторизации и пользовательских данных.
'''


def fixture_value(source, items):
    """Interpret a tiny allowed AST subset; NEVER exec/eval generated source.

    Supports sum(generator), arithmetic, literal dict indexing and dict.get.
    Unsupported implementations are unverified, not executed on the host.
    """
    tree = ast.parse(source)
    allowed_top = (ast.FunctionDef,)
    if any(not isinstance(n, allowed_top) for n in tree.body):
        raise ValueError("Only function definitions are accepted in the fixture")
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "total_value")
    if fn.decorator_list or len(fn.args.args) != 1 or fn.args.defaults:
        raise ValueError("Unsupported function signature")
    body = [n for n in fn.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                                     and isinstance(n.value.value, str))]
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        raise ValueError("Fixture verifier supports one return expression only")

    def visit(node, env):
        if isinstance(node, ast.Constant) and type(node.value) in {int, float, str, type(None)}:
            return node.value
        if isinstance(node, ast.Name) and node.id in env:
            return env[node.id]
        if isinstance(node, ast.Subscript):
            target, key = visit(node.value, env), visit(node.slice, env)
            if not isinstance(target, dict) or key not in target:
                raise ValueError("Unsupported index")
            return target[key]
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mult)):
            left, right = visit(node.left, env), visit(node.right, env)
            if type(left) not in {int, float} or type(right) not in {int, float}:
                raise ValueError("Non-numeric arithmetic")
            return left * right if isinstance(node.op, ast.Mult) else left + right
        if isinstance(node, (ast.GeneratorExp, ast.ListComp)) and len(node.generators) == 1:
            g = node.generators[0]
            if not isinstance(g.target, ast.Name) or g.ifs or g.is_async:
                raise ValueError("Unsupported comprehension")
            values = visit(g.iter, env)
            if not isinstance(values, list) or len(values) > 20:
                raise ValueError("Unbounded iteration")
            return [visit(node.elt, {**env, g.target.id: item}) for item in values]
        if isinstance(node, ast.Call) and not node.keywords:
            if isinstance(node.func, ast.Name) and node.func.id == "sum" and len(node.args) == 1:
                values = visit(node.args[0], env)
                if not isinstance(values, list):
                    raise ValueError("sum needs a bounded list")
                return sum(values)
            if isinstance(node.func, ast.Attribute) and node.func.attr == "get" and len(node.args) in {1, 2}:
                value = visit(node.func.value, env)
                if type(value) is not dict:
                    raise ValueError("Only literal fixture dict.get allowed")
                return value.get(*(visit(arg, env) for arg in node.args))
        raise ValueError("Unsupported AST node: " + type(node).__name__)

    return visit(body[0].value, {fn.args.args[0].arg: items})


def check_fixture(source):
    cases = [([], 0), ([{"price": 7, "quantity": 3}], 21),
             ([{"price": 2.5, "quantity": 4}, {"price": 10, "quantity": 0}], 10)]
    try:
        values = [fixture_value(source, items) for items, _ in cases]
        checks = [actual == expected for actual, (_, expected) in zip(values, cases)]
        return {"verified": True, "passed": sum(checks), "total": len(cases), "values": values}
    except Exception as exc:
        return {"verified": False, "passed": 0, "total": len(cases), "error": str(exc)}


def make_workspace(destination):
    for path in SOURCE.glob("*.py"):
        shutil.copy2(path, destination / path.name)
    (destination / "jarvis_config.json").write_text("{}", encoding="utf-8")
    (destination / "pc_apps.txt").write_text("", encoding="utf-8")


def isolated_imports(root, model, context):
    os.chdir(root)
    sys.argv[0] = str(root / "jarvis.py")
    sys.path.insert(0, str(root))
    for key in list(os.environ):
        if key.startswith(("JARVIS_", "OPENROUTER_", "TELEGRAM_", "LM_STUDIO_", "OLLAMA_")):
            os.environ.pop(key)
    os.environ.update({"JARVIS_LLM": "lmstudio", "LM_STUDIO_MODEL": model,
                      "LM_STUDIO_CODE_MODEL": model, "LM_STUDIO_CONTEXT": str(context),
                      "LM_STUDIO_URL": "http://127.0.0.1:1234/v1", "SESSION_MEMORY": "off",
                      "JARVIS_OVERLAY": "off", "JARVIS_PROJECT_ROOTS": str(root / "projects"),
                      "JARVIS_FILE_HISTORY": str(root / "history"), "HF_HUB_OFFLINE": "1"})
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def connect(sock, address, original=original_connect):
        try:
            local = ipaddress.ip_address(address[0]).is_loopback
        except (ValueError, TypeError, IndexError):
            local = False
        if not local:
            raise RuntimeError("Benchmark inference is loopback-only")
        return original(sock, address)

    socket.socket.connect = connect
    socket.socket.connect_ex = lambda sock, addr: connect(sock, addr, original_connect_ex)
    import jarvis
    return jarvis


def raw_chatml_prompt(messages):
    """Qwen text-only diagnostic, NOT the production chat/tool transport.

    Explicitly closes the thinking prefix. Never supports native tool messages.
    All system/user/history contents remain identical to the comparison prompts.
    """
    if any(m.get("role") not in {"system", "user", "assistant"}
           or not isinstance(m.get("content"), str) or m.get("tool_calls") for m in messages):
        raise ValueError("Raw diagnostic accepts text conversation only")
    return "".join(f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n" for m in messages) + \
        "<|im_start|>assistant\n<think>\n\n</think>\n\n"


class Inference:
    def __init__(self, model, seed, reasoning_effort=None, raw_nonthinking=False):
        import httpx
        from openai import OpenAI
        self.client = OpenAI(base_url="http://127.0.0.1:1234/v1", api_key="benchmark",
                             max_retries=0, http_client=httpx.Client(trust_env=False))
        self.model, self.seed, self.reasoning_effort = model, seed, reasoning_effort
        self.raw_nonthinking = raw_nonthinking
        self.records = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.base_url = self.client.base_url

    def generate(self, messages, *, temperature=0.3, max_tokens=400, timeout=60, tools=None, tool_choice=None):
        if self.raw_nonthinking and tools:
            raise ValueError("Raw diagnostic has no native tool transport")
        from jarvis_speech_chunks import SpeechChunks
        started = time.perf_counter()
        record = {"messages": list(messages), "temperature": temperature, "max_tokens": max_tokens,
                  "seed": self.seed, "content": "", "reasoning": "", "tool_calls": [],
                  "first_any_s": None, "first_text_s": None, "first_speech_chunk_s": None,
                  "finish_reason": None, "error": None, "usage": {}}
        self.records.append(record)
        kwargs = dict(model=self.model, messages=list(messages), temperature=temperature,
                      max_tokens=max_tokens, timeout=timeout, seed=self.seed,
                      stream=True, stream_options={"include_usage": True})
        if self.reasoning_effort:
            kwargs["extra_body"] = {"reasoning_effort": self.reasoning_effort}
        if tools:
            kwargs.update(tools=tools, tool_choice=tool_choice or "auto")
        record["tools"] = tools or []
        fragments, reasoning, calls = [], [], {}
        chunker = SpeechChunks()
        try:
            if self.raw_nonthinking:
                kwargs.pop("messages")
                kwargs["prompt"] = raw_chatml_prompt(messages)
                record["raw_prompt"] = kwargs["prompt"]
                producer = self.client.completions.create
            else:
                producer = self.client.chat.completions.create
            with producer(**kwargs) as stream:
                for event in stream:
                    elapsed = time.perf_counter() - started
                    if elapsed > timeout:
                        raise TimeoutError("Benchmark generation wall-time limit")
                    if event.usage:
                        record["usage"] = event.usage.model_dump()
                    if not event.choices:
                        continue
                    choice = event.choices[0]
                    delta = (SimpleNamespace(content=choice.text, tool_calls=[]) if self.raw_nonthinking
                             else choice.delta)
                    text = delta.content or ""
                    thinking = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None) or ""
                    if (text or thinking or delta.tool_calls) and record["first_any_s"] is None:
                        record["first_any_s"] = elapsed
                    if text:
                        if record["first_text_s"] is None:
                            record["first_text_s"] = elapsed
                        fragments.append(text)
                        if chunker.feed(text) and record["first_speech_chunk_s"] is None:
                            record["first_speech_chunk_s"] = elapsed
                    if thinking:
                        reasoning.append(str(thinking))
                    for call in delta.tool_calls or []:
                        item = calls.setdefault(call.index, {"id": "", "type": "function",
                                                            "function": {"name": "", "arguments": ""}})
                        if call.id:
                            item["id"] = call.id
                        if call.function:
                            item["function"]["name"] += call.function.name or ""
                            item["function"]["arguments"] += call.function.arguments or ""
                    if choice.finish_reason:
                        record["finish_reason"] = choice.finish_reason
        except Exception as exc:
            record["error"] = type(exc).__name__ + ": " + str(exc)[:1500]
        record.update(content="".join(fragments), reasoning="".join(reasoning),
                      tool_calls=[calls[i] for i in sorted(calls)], seconds=time.perf_counter() - started)
        if record["first_speech_chunk_s"] is None and chunker.finish():
            record["first_speech_chunk_s"] = record["seconds"]
        return record

    def create(self, **kwargs):
        # Adapter used by the UNMODIFIED project agent. Streaming transport adds
        # measurements, not a second inference or a different prompt/tool schema.
        record = self.generate(kwargs["messages"], temperature=kwargs.get("temperature", 0.2),
                               max_tokens=kwargs.get("max_tokens", 1200),
                               timeout=kwargs.get("timeout", 30), tools=kwargs.get("tools"),
                               tool_choice=kwargs.get("tool_choice"))
        if record["error"]:
            raise RuntimeError(record["error"])
        calls = [SimpleNamespace(id=c["id"], function=SimpleNamespace(**c["function"]))
                 for c in record["tool_calls"]]
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason=record["finish_reason"], message=SimpleNamespace(
                content=record["content"], tool_calls=calls))])


def basic_checks(record):
    text = record["content"]
    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", text)
    russian = len(re.findall(r"[А-Яа-яЁё]", text)) / max(1, len(letters))
    return {"nonempty": bool(text.strip()), "completed": record["finish_reason"] == "stop" and not record["error"],
            "russian": russian > 0.45, "no_think_leak": "<think>" not in text,
            "no_markdown": not re.search(r"(?m)^\s*#|\*\*|```", text)}


def report_complete(report, requests):
    """Completion is more than HTTP success; partial/tool-markup reports fail."""
    text = str(report).casefold().replace("ё", "е")
    return bool(requests and requests[-1]["finish_reason"] == "stop"
                and requests[-1]["content"].strip() and not requests[-1]["error"]
                and "полная проверка не завершена" not in text
                and not re.search(r"<tool_call>|<function=|<\|tool", text))


def missing_item_finding(report):
    # Presence of None alone was a false positive: the pilot incorrectly claimed
    # the existing next(generator) already returns None. Human review still applies.
    text = str(report).casefold()
    return "find_equipment" in text and ("stopiteration" in text or
           bool(re.search(r"(?:исключени|ошибк|пада[её]т|выброс|default|значени[ея] по умолчанию)", text)))


def run_chat(jarvis, infer, root):
    import jarvis_chat_memory as chat
    rows = []
    for case_id, question, history, rubric in CHAT_CASES:
        memory = chat.ChatMemory(root / (case_id + ".sqlite3"))
        for i in range(0, len(history), 2):
            with memory.turn(history[i][1], persist=False):
                memory.capture(history[i + 1][1])
        with patch.object(chat, "memory", memory):
            messages = jarvis._build_messages(question, conversational=True)
        record = infer.generate(messages, temperature=messages.temperature, max_tokens=400)
        checks = basic_checks(record)
        text = record["content"].casefold()
        if case_id == "memory_known":
            checks["known_colors"] = "графит" in text and "бирюз" in text
        if case_id == "project_followup":
            checks["retained_finding"] = "quantity" in text or "количеств" in text
        rows.append({"id": case_id, "kind": "chat", "question": question, "rubric": rubric,
                     "checks": checks, "inference": record})
        print(f"CHAT {case_id}: {record['seconds']:.2f}s, finish={record['finish_reason']}", flush=True)
    memory = chat.ChatMemory(root / "banter.sqlite3")
    with patch.object(chat, "memory", memory):
        for i, question in enumerate(JOKE_DIALOGUE):
            with memory.turn(question, persist=False):
                messages = jarvis._build_messages(question, conversational=True)
                record = infer.generate(messages, temperature=messages.temperature, max_tokens=400)
                memory.capture(record["content"])
            rows.append({"id": f"banter_{i+1}", "kind": "banter", "question": question,
                         "checks": basic_checks(record), "inference": record})
            print(f"CHAT banter_{i+1}: {record['seconds']:.2f}s, finish={record['finish_reason']}", flush=True)
    return rows


def run_tags(jarvis, infer):
    rows = []
    for case_id, question, expected, argument in TAG_CASES:
        messages = jarvis._build_messages(question)
        record = infer.generate(messages, temperature=messages.temperature, max_tokens=400)
        parsed, error = [], None
        try:
            _, actions = jarvis.parse_actions(record["content"])
            parsed = [{"name": a.name, "args": list(a.args)} for a in actions]
        except ValueError as exc:
            error = str(exc)
        names = [a["name"] for a in parsed]
        checks = {"correct_tools": names == expected, "valid_syntax": error is None,
                  "completed": record["finish_reason"] == "stop" and not record["error"],
                  "nonempty": bool(record["content"].strip())}
        if argument:
            checks["correct_argument"] = argument in json.dumps(parsed, ensure_ascii=False).casefold()
        if case_id == "compound":
            checks["correct_targets"] = [a["args"] for a in parsed] == [["browser"], ["calc"]]
        rows.append({"id": case_id, "kind": "tags", "question": question, "checks": checks,
                     "passed": all(checks.values()), "parsed_actions": parsed, "parse_error": error,
                     "inference": record})
        print(f"TAGS {case_id}: {'PASS' if all(checks.values()) else 'FAIL'} {names}", flush=True)
    return rows


def run_projects(jarvis, infer, root, context, only_case=None):
    import project_agent as agent
    rows = []
    projects = root / "projects"
    projects.mkdir()
    cases = [
        ("inventory_review", "Учёт оборудования", "проверь проект учет оборудования", "inspect"),
        ("inventory_fix", "Учёт оборудования правка",
         "Исправь total_value в inventory.py: стоимость каждой позиции равна price * quantity. "
         "Остальные функции не меняй. Проверь результат.", "modify"),
        ("site_review", "Сайт А.Б", "проверь проект сайт АБ", "inspect"),
        ("discount_large_fix", "Каталог Север", "Исправь percent_off в pricing.py: процентная скидка 10 от цены 200 "
         "должна дать 180. Сохрани остальное содержимое файла и остальные файлы. Проверь результат.", "modify"),
        ("average_fix", "Отчёты Восток", "Исправь average в statistics.py: пустой список должен давать 0, "
         "непустой — среднее арифметическое. Остальные файлы не меняй. Проверь результат.", "modify"),
        ("nested_render_review", "Витрина Север", "Проверь вывод пользовательского текста в web/render.js. "
         "Ничего не меняй, укажи конкретную проблему и вариант исправления.", "inspect"),
    ]
    execute = agent._execute
    for case_id, name, task, mode in cases:
        if only_case and case_id != only_case:
            continue
        directory = projects / name
        directory.mkdir()
        if case_id.startswith("inventory"):
            (directory / "inventory.py").write_text(INVENTORY, encoding="utf-8")
            (directory / "README.md").write_text(INVENTORY_README, encoding="utf-8")
        elif case_id == 'site_review':
            (directory / "README.md").write_text(
                "Учебная страница: JS в конце index.html. Параметр title из URL отображается в заголовке. "
                "Никакого backend или базы данных здесь нет.\n", encoding="utf-8")
            html = '<!doctype html><meta charset="utf-8"><h1 id="title">Demo</h1>\n'
            html += '<!-- repeated decorative section, no logic -->\n' * 420
            html += '<script>\nconst title = new URLSearchParams(location.search).get("title");\n'
            html += 'document.getElementById("title").innerHTML = title;\n</script>\n'
            (directory / "index.html").write_text(html, encoding="utf-8")
        elif case_id == 'discount_large_fix':
            (directory / 'pricing.py').write_text('# historical decoration, preserve exactly\n' * 1500 + DISCOUNT +
                                                '# trailing decoration, preserve exactly\n' * 300, encoding='utf-8')
        elif case_id == 'average_fix':
            (directory / 'statistics.py').write_text(AVERAGE, encoding='utf-8')
        else:
            (directory / 'web').mkdir()
            (directory / 'web' / 'render.js').write_text('export function show(value) {\n'
                '  document.getElementById("message").innerHTML = value;\n}\n', encoding='utf-8')
            (directory / 'README.md').write_text('show принимает текст посетителя из поля формы.', encoding='utf-8')
        (directory / 'keep.txt').write_bytes(b'unrelated sentinel\r\nDO NOT CHANGE\x00')
        snapshot = lambda: {p.relative_to(directory).as_posix(): p.read_bytes() for p in directory.rglob('*') if p.is_file()}
        before = snapshot()
        target = {'inventory_fix': 'inventory.py', 'discount_large_fix': 'pricing.py', 'average_fix': 'statistics.py'}.get(case_id)
        traces, stages = [], []

        def bounded_execute(project, tool, args, mode="inspect"):
            if Path(project).resolve() != directory.resolve():
                raise AssertionError("Unexpected project root")
            if tool == "run_command":
                if mode == 'inspect':
                    # Production inspect delegates to static compile/check,
                    # never shell. Preserve that contract in the benchmark.
                    result = execute(project, tool, args, mode='inspect')
                elif case_id != "inventory_fix":
                    result = "exit=1\nМодельная команда не исполнялась стендом; AST-проверка вызывается автоматически после записи."
                else:
                    validation = check_fixture((directory / "inventory.py").read_text(encoding="utf-8"))
                    ok = validation["verified"] and validation["passed"] == validation["total"]
                    result = (f"exit={0 if ok else 1}\nПроверка стенда по AST, без исполнения кода: "
                              + json.dumps(validation, ensure_ascii=False))
            elif tool in agent.WRITE_TOOLS and args.get("path") != target:
                result = "Запись вне порученной фикстуры не выполнялась."
            else:
                result = execute(project, tool, args, mode=mode)
            traces.append({"tool": tool, "args": args, "output": result})
            return result

        def bounded_verify(project, changed, plan, cancel, deadline):
            if Path(project).resolve() != directory.resolve() or set(changed) != {target}:
                raise AssertionError('Unexpected verification scope')
            source = (directory / target).read_text(encoding='utf-8')
            validation = check_fixture(source) if case_id == 'inventory_fix' else check_holdout(case_id, source)
            ok = validation['verified'] and validation['passed'] == validation['total']
            result = {'status': 'passed' if ok else 'failed', 'checks': [{'kind': 'behavior', 'exit': 0 if ok else 1,
                'output': 'Изолированная AST-проверка стенда, без исполнения кода: ' + json.dumps(validation, ensure_ascii=False)}],
                'notes': [], 'backend': 'benchmark_ast'}
            traces.append({'tool': 'automatic_verify', 'args': {'changed': changed}, 'output': json.dumps(result, ensure_ascii=False)})
            return result

        first = len(infer.records)
        started = time.perf_counter()
        with patch.object(agent, "_execute", side_effect=bounded_execute), patch.object(agent, '_verify_project', side_effect=bounded_verify):
            report = agent.run_project_agent(infer, infer.model, str(directory), task, mode=mode,
                                             context_tokens=context, progress_fn=stages.append)
        requests = infer.records[first:]
        after = snapshot()
        text = str(report).casefold()
        checks = {"read_code": any(t["tool"] == "read_file" and
                                  t["args"].get("path", "").endswith((".py", ".html", ".js", ".mjs", ".ts")) for t in traces),
                  "model_report_complete": report_complete(report, requests),
                  "no_inference_error": bool(requests) and not any(r["error"] or
                         r["finish_reason"] is None for r in requests)
                         and "ошибка запроса к модели" not in text}
        if mode == "inspect":
            checks["files_unchanged"] = before == after
        else:
            checks['unrelated_files_preserved'] = {k:v for k,v in before.items() if k != target} == {k:v for k,v in after.items() if k != target}
            automated = [t for t in traces if t['tool'] == 'automatic_verify']
            checks['automatic_verification_passed'] = bool(automated and json.loads(automated[-1]['output'])['status'] == 'passed')
        if case_id in {'discount_large_fix', 'average_fix'}:
            validation = check_holdout(case_id, (directory / target).read_text(encoding='utf-8'))
            checks['correct_fix'] = validation['verified'] and validation['passed'] == validation['total']
            if case_id == 'discount_large_fix':
                old, new = before[target], after[target]
                checks['decorative_bytes_preserved'] = (new.startswith(old[:old.index(b'def percent_off')])
                    and new.endswith(old[old.index(b'# trailing decoration'):]))
        if case_id == "inventory_review":
            checks["quantity_bug"] = ("quantity" in text or "количеств" in text) and "total_value" in text
            checks["missing_item_bug"] = missing_item_finding(report)
        if case_id == "inventory_fix":
            source = (directory / "inventory.py").read_text(encoding="utf-8")
            validation = check_fixture(source)
            checks["correct_fix"] = validation["verified"] and validation["passed"] == validation["total"]
            try:
                old_fn = ast.parse(INVENTORY).body[1]
                new_fn = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)
                              and n.name == "find_equipment")
                checks["other_function_preserved"] = ast.dump(old_fn) == ast.dump(new_fn)
            except Exception:
                checks["other_function_preserved"] = False
        if case_id == "site_review":
            checks["sink_identified"] = "innerhtml" in text and ("xss" in text or "инъекц" in text or "textcontent" in text)
            checks["sink_observed"] = any("innerHTML" in t["output"] for t in traces)
        if case_id == 'nested_render_review':
            checks['sink_identified'] = 'innerhtml' in text and ('xss' in text or 'инъекц' in text or 'textcontent' in text)
            checks['sink_observed'] = any('innerHTML' in t['output'] for t in traces)
        rows.append({"id": case_id, "kind": "project", "question": task, "checks": checks,
                     "report": str(report), "speech": getattr(report, "speech", ""), "traces": traces,
                     "stages": stages, "seconds": time.perf_counter() - started, "requests": requests,
                     "result_files": {p.relative_to(directory).as_posix(): p.read_text(encoding="utf-8")
                                      for p in directory.rglob('*') if p.is_file() and p.suffix in {'.py', '.js'}}})
        print(f"PROJECT {case_id}: {sum(checks.values())}/{len(checks)} {time.perf_counter()-started:.2f}s", flush=True)
    return rows


def save_result(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--context", type=int, default=8192)
    parser.add_argument("--reasoning-effort", default=None)
    parser.add_argument("--raw-nonthinking", action="store_true",
                        help="Text-only Qwen ChatML diagnostic; NOT native Jarvis integration")
    parser.add_argument("--project-case", choices=["inventory_review", "inventory_fix", "site_review",
                        "discount_large_fix", "average_fix", "nested_render_review"])
    parser.add_argument("--only", choices=["all", "text", "chat", "tags", "projects"], default="all")
    parser.add_argument("--output", type=Path, default=SOURCE / "logs" / "model_comparison")
    args = parser.parse_args()
    if args.raw_nonthinking and (args.only in {"all", "projects"} or args.reasoning_effort):
        parser.error("Raw diagnostic requires --only text/chat/tags and no reasoning-effort override")
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", args.label):
        parser.error("label must be a simple filename component")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args.output = args.output.resolve()
    with tempfile.TemporaryDirectory(prefix="jarvis-model-bench-") as temporary, contextlib.ExitStack() as cleanup:
        root = Path(temporary)
        cleanup.callback(os.chdir, SOURCE)
        cleanup.callback(logging.shutdown)
        make_workspace(root)
        jarvis = isolated_imports(root, args.model, args.context)
        import jarvis_chat_memory as chat
        import jarvis_personality as personality
        original = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in SOURCE.glob("*.py") if p.name.startswith("jarvis") or p.name == "project_agent.py"}
        data = {"model": args.model, "label": args.label, "context": args.context,
                "benchmark_version": 3,
                "benchmark_revision": "workflow-3.1",
                "benchmark_hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                     for p in (SOURCE / 'benchmarks').glob('*.py')},
                "transport": "raw-chatml-nonthinking-diagnostic" if args.raw_nonthinking else "openai-chat",
                "reasoning_effort": args.reasoning_effort, "source_hashes": original,
                "hardware_scope": "Windows PC; not an Orange Pi measurement", "passes": []}
        import httpx
        with httpx.Client(trust_env=False, timeout=10) as client:
            models = client.get("http://127.0.0.1:1234/api/v1/models").json()["models"]
        data["runtime_model"] = next((m for m in models if any(
            i["id"] == args.model for i in m.get("loaded_instances", []))), None)
        if data["runtime_model"] is None:
            raise RuntimeError("Load a separate benchmark instance first; implicit model loading is disabled")
        result_path = args.output / (args.label + ".json")
        with patch.object(jarvis, "load_memory", return_value={}), \
             patch.object(jarvis, "get_obsidian_memory", return_value=""), \
             patch.object(personality.secrets, "choice", side_effect=lambda items: items[0]):
            for repeat in range(args.repeat):
                round_root = root / f"pass-{repeat}"
                round_root.mkdir()
                os.environ["JARVIS_PROJECT_ROOTS"] = str(round_root / "projects")
                infer = Inference(args.model, 20260907 + repeat, args.reasoning_effort, args.raw_nonthinking)
                fixture = chat.ChatMemory(round_root / "initial.sqlite3")
                current = {"seed": infer.seed, "rows": []}
                data["passes"].append(current)
                with patch.object(chat, "memory", fixture):
                    # Warm request is recorded separately and excluded from TTFT summaries.
                    current["warmup"] = infer.generate([{"role": "user", "content": "Ответь одним словом: готов?"}], max_tokens=96)
                    save_result(result_path, data)
                    for name, fn in [("tags", lambda: run_tags(jarvis, infer)),
                                     ("chat", lambda: run_chat(jarvis, infer, round_root)),
                                     ("projects", lambda: run_projects(jarvis, infer, round_root, args.context,
                                                                      args.project_case))]:
                        if args.only not in {"all", name} and not (args.only == "text" and name in {"tags", "chat"}):
                            continue
                        current["rows"].extend(fn())
                        save_result(result_path, data)
                infer.client.close()
        print("RESULT " + str(result_path), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

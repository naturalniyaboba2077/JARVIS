"""Pure, one-pass action parsing. Tool output is data, never parser input."""

from dataclasses import dataclass
import json
import re


@dataclass(frozen=True)
class Action:
    name: str
    args: tuple = ()


# Values are required argument counts. Only the last argument may contain colons.
_ARITY = {
    "OPEN": 1, "MUSIC:OPEN": 0, "MUSIC:PLAY": 1, "SEARCH": 1,
    "SYS:VOL": 1, "MEDIA:PLAYPAUSE": 0, "MEDIA:NEXT": 0, "MEDIA:PREV": 0,
    "TYPE": 1, "CAL:READ": 1, "CAL:ADD": 3,
    "MEMORY:REMEMBER": 2, "MEMORY:RECALL": 1,
    "TODO:ADD": 1, "TODO:LIST": 0, "TODO:DONE": 1, "TIMER": 2,
    "WEATHER": 1, "SYSINFO": 0, "SCREENSHOT": 0, "LOCK": 0, "BRIGHT": 1,
    "OB:WRITE": 2, "OB:APPEND": 2, "OB:SEARCH": 1, "OB:READ": 1,
    "OB:LIST": 0, "OB:DELETE": 1, "TG:CHATS": 0, "TG:READ": 2,
    "TG:SEARCH": 2, "TG:EXPORT": 2, "TG:SEND": 2, "CMD": 1,
    "WIN:DESKTOP": 0, "WIN:MINIMIZE": 0, "WIN:MAXIMIZE": 0,
    "WIN:CLOSE": 0, "WIN:SWITCH": 1, "CLIP:READ": 0, "CLIP:PASTE": 0,
    "REMIND:IN": 2, "REMIND:LIST": 0, "REMIND": 3,
    "FILE:LATEST": 0, "FILE:FIND": 1, "FILE:OPEN": 1,
    "OCR:WINDOW": 0, "OCR": 0, "MAIL:UNREAD": 0, "MAIL:SEARCH": 1,
    "MAIL:SEND": 3, "SESSION:SUMMARY": 0, "SESSION:CLEAR": 0,
    "LOOKUP:TG": 1, "LOOKUP:PHONE": 1,
}
_ROOTS = {name.split(":")[0] for name in _ARITY} | {"EXECUTE_PYTHON"}
_START = re.compile(r"\[([A-Z_]+)(?=[:\]])")


def is_action_discussion(text: str) -> bool:
    """Conservative guard for explanations, hypothetical and negated requests."""
    t = re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е")).strip()
    if re.search(r"\bесли\b.*\b(?:скажу|попрошу|дам команду|захочу)\b", t):
        return True
    if re.search(r"\b(?:можешь|сможешь|умеешь|будешь|нужно|надо)\s+ли\b", t):
        return True
    if re.match(r"^(?:пожалуйста[, ]+)?(?:почему|зачем|что\s+(?:значит|означает|такое)|"
                r"(?:расскажи|объясни)\b|как\s+(?:мне\s+)?(?:открыть|запустить|"
                r"удалить|заблокировать|отправить|выполнить|настроить|сделать|снять))\b", t):
        return True
    if re.search(r"\bне\s+(?:надо|нужно|следует|открывай|открыть|запускай|запускать|"
                 r"включай|выключай|удаляй|удалять|блокируй|заблокируй|отправляй|"
                 r"отправить|выполняй|делай|пиши|печатай|сохраняй|меняй|снимай|"
                 r"снимать|трогай|закрывай|отмечай|добавляй|читай|ищи|блокировать)\b", t):
        return True
    return bool(re.search(r"\b(?:фраза|фразу|команда|команду|слово)\s+[«\"']", t))


def is_compound_action_request(text: str) -> bool:
    t = (text or "").strip().lower()
    return bool(re.match(r"^(?:пожалуйста[, ]+)?(?:открой|запусти|включи|выключи|"
                         r"сделай|создай|сохрани|запиши|добавь|удали|отправь|поставь)\b", t)
                and re.search(r"\s+(?:и|затем|потом|а потом|после этого)\s+|;\s*", t))


def needs_action_buffer(text: str) -> bool:
    """Do not speak speculative prose before validating an actionable answer.

    Conversation without action vocabulary can stream; it has no tool authority.
    An ambiguous request is buffered. This favors honest action results over a
    premature spoken acknowledgement, without delaying ordinary conversation.
    """
    return is_action_discussion(text) or bool(re.search(
        r"откр|запуст|запуск|включ|выключ|созда|сдела|сним|скрин|сохрани|запис|"
        r"запиш|добав|удал|отправ|постав|установ|измени|переключ|заблок|блокир|"
        r"выполн|набери|печат|пиши|напиш|встав|скопир|прочит|покаж|найди|поищи|загугл|"
        r"погугл|напомн|запомн|вспомн|заметк|файл|задач|таймер|письм|почт|"
        r"календар|свет|громк|ярк|музык|трек|пауз|браузер|команд|терминал|"
        r"обсидиан|телеграм|буфер|код|python|powershell|погод|экран|диалог|"
        r"проект|окно|окна|нагрузк|систем|^\s*(?:open|run|write|send|delete|lock)\b",
        text or "", re.IGNORECASE))


def is_cancel_request(text: str) -> bool:
    t = re.sub(r"\s+", " ", (text or "").strip().lower()).strip(" .,!?:;")
    return t in {"стоп", "остановись", "прерви", "прерви выполнение",
                 "останови выполнение", "stop"}


def _integer(value: str, low: int, high: int) -> int:
    if not re.fullmatch(r"\d+", value):
        raise ValueError("ожидалось целое число")
    number = int(value)
    if not low <= number <= high:
        raise ValueError(f"число должно быть от {low} до {high}")
    return number


def _decode(body: str) -> Action:
    name = next((key for key in sorted(_ARITY, key=len, reverse=True)
                 if body == key or body.startswith(key + ":")), None)
    if name is None:
        raise ValueError("неизвестное действие")
    payload = body[len(name):]
    count = _ARITY[name]
    if count == 0:
        if payload:
            raise ValueError(f"{name}: лишние аргументы")
        return Action(name)
    payload = payload[1:] if payload.startswith(":") else ""
    args = [part.strip() for part in payload.split(":", count - 1)]
    if name != "CMD" and args and args[-1].startswith('"'):
        try:
            args[-1] = json.loads(args[-1])
        except ValueError as exc:
            raise ValueError(f"{name}: некорректная JSON-строка") from exc
        if not isinstance(args[-1], str):
            raise ValueError(f"{name}: ожидалась текстовая строка")
    if name in {"CAL:READ", "WEATHER", "MEMORY:RECALL"} and not payload:
        args = [{"CAL:READ": "сегодня", "WEATHER": "Москва", "MEMORY:RECALL": None}[name]]
    if name in {"TG:READ", "TG:EXPORT", "TIMER"} and len(args) == 1:
        args.append({"TG:READ": "10", "TG:EXPORT": "200", "TIMER": ""}[name])
    if len(args) != count or any(not arg for i, arg in enumerate(args)
                                if not (name == "TIMER" and i == 1)
                                and name != "MEMORY:RECALL"):
        raise ValueError(f"{name}: не хватает аргументов")
    if name in {"SYS:VOL", "BRIGHT"}:
        args[0] = _integer(args[0], 0, 100)
    elif name == "TODO:DONE":
        args[0] = _integer(args[0], 1, 1000000)
    elif name in {"TIMER", "REMIND:IN"}:
        args[0] = _integer(args[0], 1, 31536000)
    elif name in {"TG:READ", "TG:EXPORT"}:
        args[1] = _integer(args[1], 1, 10000)
    elif name in {"CAL:ADD", "REMIND"}:
        hh, mm = _integer(args[0], 0, 23), _integer(args[1], 0, 59)
        args = [f"{hh:02d}:{mm:02d}", args[2]]
    return Action(name, tuple(args))


def _end_of_tag(text: str, start: int, shell: bool) -> int:
    # JSON-quoted final text fields support arbitrary brackets without guessing
    # where a literal closing bracket ends and the action delimiter begins.
    if not shell:
        for name in sorted(_ARITY, key=len, reverse=True):
            prefix = "[" + name + ":"
            if _ARITY[name] and text.startswith(prefix, start):
                field = start + len(prefix)
                for _ in range(_ARITY[name] - 1):
                    colon = text.find(":", field)
                    closing = text.find("]", field)
                    if colon < 0 or (closing >= 0 and closing < colon):
                        break
                    field = colon + 1
                while field < len(text) and text[field].isspace():
                    field += 1
                if field < len(text) and text[field] == '"':
                    try:
                        value, consumed = json.JSONDecoder().raw_decode(text[field:])
                    except ValueError as exc:
                        raise ValueError("некорректная JSON-строка аргумента") from exc
                    end = field + consumed
                    while end < len(text) and text[end].isspace():
                        end += 1
                    if not isinstance(value, str) or end >= len(text) or text[end] != "]":
                        raise ValueError("ожидался конец тега после JSON-строки")
                    return end
                break
    depth, quote, i = 1, None, start + 1
    while i < len(text):
        char = text[i]
        if shell and char == "`" and quote != "'":
            i += 2
            continue
        if shell and char in "\"'":
            if quote == char:
                if i + 1 < len(text) and text[i + 1] == char:
                    i += 2
                    continue
                quote = None
            elif quote is None:
                quote = char
        elif quote is None:
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    raise ValueError("незакрытый тег действия")


def parse_actions(text: str) -> tuple[str, tuple[Action, ...]]:
    """Validate the entire original response before allowing any side effects.

    Repeated tags keep their order. Nested brackets belong to the outer payload;
    PowerShell quotes and backticks protect literal brackets inside CMD.
    """
    text = text or ""
    prose, actions, cursor = [], [], 0
    while match := _START.search(text, cursor):
        if match.group(1) not in _ROOTS:
            prose.append(text[cursor:match.end()])
            cursor = match.end()
            continue
        start = match.start()
        prose.append(text[cursor:start])
        if text.startswith("[EXECUTE_PYTHON]", start):
            code_start = start + len("[EXECUTE_PYTHON]")
            end = text.find("[/EXECUTE_PYTHON]", code_start)
            if end < 0:
                raise ValueError("незакрытый блок Python")
            code = text[code_start:end].strip()
            code = re.sub(r"^```(?:python)?\s*", "", code)
            code = re.sub(r"\s*```$", "", code).strip()
            if not code:
                raise ValueError("пустой блок Python")
            actions.append(Action("EXECUTE_PYTHON", (code,)))
            cursor = end + len("[/EXECUTE_PYTHON]")
        else:
            end = _end_of_tag(text, start, text.startswith("[CMD:", start))
            actions.append(_decode(text[start + 1:end]))
            cursor = end + 1
    prose.append(text[cursor:])
    cleaned = "".join(prose).strip()
    if actions:
        depth = 0
        for char in cleaned:
            depth += (char == "[") - (char == "]")
            if depth < 0:
                raise ValueError("лишняя закрывающая скобка; заключите текстовый аргумент в JSON-строку")
    return cleaned, tuple(actions)

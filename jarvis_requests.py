"""Ground requests before dispatch. Pure parsing; no tools or model calls here.

These checks are conservative intent checks, not a semantic sandbox for shell.
Project requests have their own route and must never become unrelated UI tags.
"""

from dataclasses import dataclass
from pathlib import PureWindowsPath
import re

from jarvis_actions import is_action_discussion


def normalize(text):
    return re.sub(r"\s+", " ", (text or "").casefold().replace("ё", "е")).strip(" .,!?:;")


@dataclass(frozen=True)
class ProjectRequest:
    project: str
    task: str
    mode: str = "inspect"
    location: str = ""
    clarification: str = ""


def _project_location(value):
    match = re.search(r'\s+(?:(?:котор\w+\s+)?(?:находится|лежит)\s+)?'
                      r'(?:в|из|на)\s+(?:папке?\s+)?(документ\w*|documents|'
                      r'рабоч\w+\s+стол\w*)\s*[.!?]*$', value, re.I)
    if not match:
        return value, ''
    location = 'Documents' if normalize(match[1]).startswith(('документ', 'documents')) else 'Desktop'
    return value[:match.start()].strip(' ,'), location


def project_request(text):
    original = (text or "").strip()
    t = normalize(original)
    if re.match(r'^(?:пожалуйста[, ]+)?(?:найди|поищи|открой|прочитай)\s+(?:файл|папку)\s+', t):
        return None  # A filename containing "проект" is not a project-agent task.
    if re.match(r'^(?:пожалуйста[, ]+)?(?:погугли|загугли)\b', t) or (
            re.match(r'^(?:пожалуйста[, ]+)?(?:найди|поищи)\b', t)
            and re.search(r'\b(?:интернет\w*|гугл\w*|google|сети)\b', t)):
        return None  # A web search about a project does not select a local folder.
    # A read-only restriction after a concrete inspection is not a cancellation
    # of the inspection itself. Other negations/hypotheticals keep their guard.
    discussion_text = t
    target_text = original
    if re.match(r"^(?:пожалуйста[, ]+)?(?:проверь|проверяй|посмотри|просмотри|изучи|оцени|проанализируй)\b", t):
        discussion_text = re.sub(r"(?:[:,;]\s*|\s+но\s+)(?:не меняй файлы|ничего не меняй)$", "", t)
        target_text = re.sub(r"(?:[:,;]\s*|\s+но\s+)(?:не меняй файлы|ничего не меняй)[.!?]*$",
                             "", original, flags=re.I)
    if is_action_discussion(discussion_text) or not re.search(r"\bпроект\w*\b", t):
        return None
    # History commands retain their separate, exact handler.
    if re.match(r"^(?:отмени|откати|покажи|какие)\s+(?:последн\w+\s+)?(?:правк|изменени)", t):
        return None
    t = re.sub(r"^пожалуйста[, ]+", "", t)
    # A correctly recognized refactoring is an explicit modification request.
    # The damaged 'проведили факторинг' remains a clarification, never guessed.
    t = re.sub(r'^(?:проведи|сделай|выполни)\s+рефакторинг\b', 'отрефактори', t)
    verb = re.match(r"^(проверь|проверяй|проверить|посмотри|просмотри|проанализируй|изучи|оцени|исследуй|"
                    r"исправь|доработай|поработай|работай|измени|добавь|реализуй|"
                    r"запусти|выполни|отрефактори)\b", t)
    if not verb:
        if 'факторинг' in t:
            return ProjectRequest('', original, clarification=(
                'Распознано «факторинг». Вы имели в виду рефакторинг кода? '
                'Если да, скажите: «проведи рефакторинг проекта Учёт оборудования». Пока ничего не менял.'))
        # A damaged imperative (the reported 'травей') is not guessed into action.
        if re.match(r"^(?:что|как|где|какой|какие|мой|мои|у меня|расскажи)\b", t):
            return None
        return ProjectRequest("", original, clarification=(
            "Не уверен, что правильно понял действие над проектом. "
            "Скажите, например: «проверь проект Учёт оборудования» или «исправь проект …: …»."))
    mode = "inspect" if verb[1] in {"проверь", "проверяй", "проверить", "посмотри", "просмотри", "проанализируй", "изучи", "оцени", "исследуй"} else "modify"
    tail = re.split(r"\bпроект\w*\s*", target_text, maxsplit=1, flags=re.I)[-1].strip()
    tail = re.sub(r"^(?:под названием|с названием|по имени|который называется)\s+", "", tail, flags=re.I)
    # A second explicit imperative is a task, not part of the directory name.
    parts = re.split(r"\s+(?:и|а затем|затем)\s+(?=(?:исправь|добавь|измени|доработай|запусти|выполни|проверь|прочитай|зачитай|озвучь)\b)",
                     tail, maxsplit=1, flags=re.I)
    tail, following = parts[0], parts[1] if len(parts) == 2 else ""
    tail, location = _project_location(tail)
    # Colon is optional for natural inspection requests; keep drive-letter colons.
    quoted = re.match(r'^[«"]([^»"]+)[»"]\s*:?(.*)$', tail, re.S)
    if quoted:
        name, task_tail = quoted.groups()
    elif re.match(r"^[A-Za-z]:[\\/]", tail):
        name, task_tail = tail, ""
    else:
        name, sep, task_tail = tail.partition(":")
    name, name_location = _project_location(name)
    location = name_location or location
    task_tail = (task_tail + " " + following).strip()
    if re.match(r"^(?:исправь|добавь|измени|доработай|запусти|выполни)\b", normalize(task_tail)):
        mode = "modify"
    if mode == "modify" and target_text != original:
        return ProjectRequest('', original, clarification=(
            'В поручении есть и изменение проекта, и запрет менять файлы. Уточните: только проверить или исправить?'))
    name = name.strip(" ,.!?«»\"")
    if not name or len(name) > 260:
        return ProjectRequest("", original, clarification="Укажите название или полный путь проекта и конкретную задачу.")
    if mode == "modify" and not task_tail.strip() and verb[1] in {"поработай", "работай", "измени", "добавь", "реализуй"}:
        return ProjectRequest(name, original, clarification=f"Что именно нужно изменить в проекте «{name}»?")
    return ProjectRequest(name, original, mode, location)


_PURPOSES = {
    "MUSIC": r"музык|песн|трек|волн|включи|поставь",
    "SEARCH": r"найди|поищи|поиск|загугл|погугл|интернет|исследуй|сравни",
    "SYS": r"громк|звук|мьют|mute", "MEDIA": r"пауза|плей|воспроизвед|трек|песн|музык",
    "TYPE": r"напечат|печатай|набери|введи|вставь|диктов|\b(?:пиши|напиши)\b",
    "CAL": r"календар|расписан|встреч|событи", "MEMORY": r"запомн|помниш|вспомн|памят",
    "TODO": r"задач|список дел|в список дел|пункт", "TIMER": r"таймер",
    "WEATHER": r"погод|прогноз", "SYSINFO": r"желез|процессор|cpu|ram|памят|нагрузк|систем",
    "SCREENSHOT": r"скриншот|снимок экрана|сними экран", "LOCK": r"заблок|\block\b",
    "BRIGHT": r"ярк|ярче|темнее", "OB": r"заметк|obsidian|обсидиан|баз[ауе] знаний",
    "TG": r"телеграм|telegram|\bчат\w*\b|переписк|диалог",
    "WIN": r"окн|рабочий стол|сверни|разверни|переключи",
    "CLIP": r"буфер|вставь|скопируй", "REMIND": r"напомн|напоминан",
    "FILE": r"файл|папк|документ|загрузк|найди|открой",
    "OCR": r"экран|окно|окне|\bocr\b", "MAIL": r"почт|письм|gmail|email",
    "SESSION": r"сесси|разговор|диалог|истори|итог",
    "LOOKUP": r"юзернейм|username|номер|профил|информаци|телефон",
    "CMD": r"терминал|powershell|\bcmd\b|shell|команд[уы]|скрипт|код|процесс|служб|установи|настрой",
    "EXECUTE_PYTHON": r"python|питон|код|скрипт|программ|вычисли|посчитай",
}

_OPEN_ALIASES = {
    "browser": r"браузер|хром|chrome|google|гугл|интернет",
    "notepad": r"блокнот|notepad", "calc": r"калькулятор|calc",
    "youtube.com": r"youtube|ютуб|ютюб", "telegram": r"телеграм|телега|telegram",
    "vscode": r"vscode|vs code|визуал|редактор кода", "obsidian": r"obsidian|обсидиан|заметки",
    "claude": r"claude|клод", "discord": r"discord|дискорд",
}


def action_mismatch(actions, request):
    """Return a reason BEFORE any action runs, or None. Empty source is internal."""
    if not request or not actions:
        return None
    t = normalize(request)
    if project_request(request) is not None:
        return "Запрос относится к проекту; общие команды не заменяют проверку или правку проекта."
    for action in actions:
        name = action.name
        if name == "FILE:LATEST":
            opening = re.search(r"\b(?:открой|открыть|запусти)\b", t)
            download = re.search(r"последн\w*\s+(?:загрузк\w*|скачан\w*\s+файл\w*|файл\w*\s+из\s+загруз\w*)", t)
            if not opening or not download:
                return "Вы не просили открывать последнюю загрузку."
        if name == "OPEN":
            if not re.search(r"\b(?:открой|запусти|включи|перейди|зайди|открыть|open)\b", t):
                return "Не получил явного поручения открыть приложение или сайт."
            target = normalize(str(action.args[0])).removeprefix("https://").removeprefix("http://").removeprefix("www.").rstrip("/")
            pattern = _OPEN_ALIASES.get(target)
            if not (re.search(pattern, t) if pattern else target in t):
                return "Выбранное приложение или сайт не совпадает с указанным в поручении."
            continue
        purpose = _PURPOSES.get(name.split(":")[0])
        if not purpose or not re.search(purpose, t):
            return f"Действие {name} не соответствует указанной задаче."
        if name in {"FILE:OPEN", "FILE:READ"}:
            target = normalize(str(action.args[0]))
            stem = normalize(PureWindowsPath(target).stem)
            verbs = r"открой|открыть|запусти" if name == 'FILE:OPEN' else r"прочитай|читай|содержимое"
            if not re.search(verbs, t) or not stem or stem not in t:
                return "Нужно уточнить, какой именно файл открыть; произвольный путь не выбран."
        if name == 'FILE:LIST' and not re.search(r'содержимое\s+папки|файлы\s+в\s+папке|прочитай\s+папку|перечисли\s+файлы', t):
            return 'Просмотр папки не заменяет чтение или открытие указанного файла.'
        if name in {"TG:SEND", "MAIL:SEND"} and not re.search(r"отправ|напиши|пошли", t):
            return "Чтение сообщений не разрешает подготовку отправки."
    return None

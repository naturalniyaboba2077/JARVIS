"""Cheap address checks for a bounded follow-up window, never a speaker identity claim.

Only live microphone input belongs here. A clarification does not retain/replay
an action, and tool results or dialogue archives must never enter this gate.
"""
from dataclasses import dataclass
import re
from jarvis_confirm import YES, NO, normalize as normalize_confirmation
from jarvis_project_context import followup_kind
from jarvis_project_selection import parse_answer as project_answer
from jarvis_personality import banter_turn


def normalized(text):
    return re.sub(r"\s+", " ", (text or "").casefold().replace("ё", "е")).strip(" .,!?:;…")


@dataclass(frozen=True)
class AddressDecision:
    action: str  # accept / ignore / clarify
    reason: str


def classify_followup(text, *, previous_user="", previous_reply="",
                      explicit_window=False, confirmation_pending=False, project_pending=False):
    t = normalized(text)
    def result(action, reason):
        return AddressDecision(action, reason)
    if not t or t in {"а", "э", "ага", "угу", "хм", "ну", "вот", "ладно", "понятно", "спасибо"}:
        return result("ignore", "backchannel")
    if re.search(r"субтитр|продолжение следует|спасибо за просмотр|подписывайтесь на канал", t):
        return result("ignore", "stt_noise")
    if re.search(r"\b(?:не тебе|не к тебе|не тебя|говорю по телефону|спрашиваю (?:маму|папу|его|ее))\b", t):
        return result("ignore", "other_addressee")
    # Explicit common vocatives and everyday physical requests are not PC commands.
    if re.match(r"^(?:эй[, ]+)?(?:мам(?:а)?|пап(?:а)?|маша|маш|саша|саш|вася|петя|катя|ребята|коллеги|алло)\b", t):
        return result("ignore", "other_addressee")
    if re.search(r"\b(?:передай|принеси|подай)\b.*\b(?:соль|хлеб|вод[уы]|тарелк|чашк)", t):
        return result("ignore", "physical_request")
    if re.match(r"^(?:я|он|она|мы|они)\s+(?:вот\s+)?(?:сказал\w*|спросил\w*|говор\w*|делаю|потом|сейчас)\b", t):
        return result("ignore", "reported_speech")
    if confirmation_pending and normalize_confirmation(text) in YES | NO:
        return result("accept", "pending_confirmation")
    if project_pending and project_answer(text) is not None:
        return result("accept", "project_confirmation")
    if t in {"стоп", "отмена", "хватит", "замолчи", "прекрати"}:
        return result("accept", "stop")
    if followup_kind(text) is not None:
        return result("accept", "project_followup")
    # Unknown explicit vocative: asking is safer than dispatching to an arbitrary
    # other person's name. Preserve commas in STT so this cue is not destroyed.
    if re.match(r"^[а-яa-z]+,\s+(?:ты\b|проверь\b|открой\b|скажи\b|сделай\b|удали\b)", t) and not re.match(r"^(?:пожалуйста|слушай|ну|да|тогда|теперь),", t):
        return result("clarify", "possible_vocative")
    t = re.sub(r"^(?:(?:пожалуйста|ну|да|тогда|теперь)[, ]+)+", "", t)
    # Even inside a conversation, deictic destructive/sending requests lack a
    # reliable target. Never queue them pending a casual 'yes'.
    if re.match(r"^(?:удали|сотри|отправь|перешли|измени|закрой)\b", t) and (
            len(t.split()) <= 2 or re.search(r"\b(?:это|его|ее|ему|ей|все|там|туда)\b", t)):
        return result("clarify", "ambiguous_action_target")
    if re.match(r"^(?:открой|закрой)\s+(?:дверь|окно на кухне|форточку)\b", t):
        return result("ignore", "physical_request")
    if re.match(r"^(?:открой|запусти|включи|выключи|покажи|скажи|расскажи|объясни|"
                r"найди|поищи|сделай|поставь|добавь|запомни|напомни|напиши|проверь|проверяй|посмотри|просмотри|"
                r"прочитай|зачитай|озвучь|повтори|сравни|исправь|изучи|оцени|проанализируй|отрефактори|"
                r"проведи|посчитай|вычисли|сохрани|скопируй|удали|перешли|отправь)\b", t):
        return result("accept", "direct_request")
    if t in {"громче", "тише", "ярче", "темнее", "пауза", "следующий", "предыдущий"}:
        return result("accept", "assistant_control")
    context = bool(previous_user or previous_reply or explicit_window)
    if context and banter_turn(text, previous_user, previous_reply):
        return result('accept', 'banter_in_context')
    if context and re.search(r'\?\s*$', previous_reply or ''):
        return result('accept', 'answer_to_assistant_question')
    if context and re.match(r'^(?:ты|тебе|тебя|с тобой|'
                            r'(?:нет[, ]+)?я (?:имел в виду|имела в виду|люблю|предпочитаю|думаю|считаю)|'
                            r'меня зовут|мне (?:нравится|не нравится)|(?:давай )?без шуток|давай серьезно)\b', t):
        return result('accept', 'dialogue_in_context')
    if re.match(r"^(?:а\s+)?(?:почему|как|что|какой|какая|какие|который|когда|где|сколько|зачем|чем)\b", t):
        return result("accept" if context else "clarify", "question_in_context" if context else "question_without_context")
    if re.match(r"^(?:а\s+)?(?:подробнее|дальше|продолжай|еще|затем|то есть)\b", t) and context:
        return result("accept", "contextual_followup")
    if re.match(r"^(?:ты\s+)?(?:можешь|умеешь|сможешь)\b", t):
        return result("accept" if context else "clarify", "assistant_question")
    if explicit_window and len(t) > 2:
        return result("accept", "explicit_address_window")
    if "?" in (text or ""):
        return result("clarify", "uncertain_question")
    return result("ignore", "no_address_cue")


def followup_setting_request(text):
    """None = not this setting; -1 = needs duration; 0..60 = explicit update.

    This narrow route handles 'сделай так, чтобы не нужно было обращаться'
    without weakening the global negation/hypothetical action guard.
    """
    t = normalized(text)
    if not re.match(r"^(?:сделай|настрой|включи|слушай|принимай|отключи)\b", t):
        return None
    if re.search(r"\b(?:не меняй|не настраивай|не включай)\b", t):
        return None
    if not (re.search(r"без (?:обращения|имени)|не нужно.*(?:обращ|имя|называть)|продолжени[ея] разговор", t)):
        return None
    if re.search(r"\b(?:если|допустим)\b", t):
        return -1  # Example/condition is not authority to save a duration.
    if t.startswith("отключи"):
        return 0
    duration = re.search(r"\b(\d+)\s*(?:секунд\w*|с\b)", t)
    if duration:
        value = int(duration[1])
        return value if 0 <= value <= 60 else -1
    if re.search(r"\b(?:минуту|одну минуту|60 секунд)\b", t):
        return 60
    return -1

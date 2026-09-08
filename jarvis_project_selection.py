"""One-shot project question. Only a live, ID-bound answer resumes its exact task.

Shares the send-confirmation slot so 'yes' cannot select a project and send mail.
No model output, path supplied by the browser or dialogue archive is executable.
"""
from dataclasses import replace
from pathlib import Path
import re
import stat
import uuid

import jarvis_confirm as confirm
from jarvis_paths import resolve_named
from jarvis_response import Response

STALE = 'Вопрос о проекте уже закрыт или устарел. Повторите поручение с названием проекта.'
REJECTED = 'Хорошо, этот выбор отменён. Укажите название или полный путь нужного проекта.'


def parse_answer(text):
    """Return (approved, one-based ordinal or None); match the complete utterance."""
    if '?' in (text or ''):
        return None
    t = re.sub(r'\s+', ' ', (text or '').casefold().replace('ё', 'е')).strip(' .,!?:;…')
    t = re.sub(r'[,!.]+\s*', ' ', t).strip()
    t = re.sub(r'^вот\s+', '', t)
    negative = (r'(?:нет(?:\s+(?:это\s+)?(?:не\s+тот(?:\s+проект)?|другой(?:\s+проект)?))?|'
                r'(?:это\s+)?не\s+(?:тот|этот)(?:\s+проект)?|это не он|это другой проект|'
                r'ты нашел не тот проект|отмена|отмени|ни один|ни один не подходит)')
    if re.fullmatch(negative, t):
        return False, None
    positive = (r'(?:да(?:\s+(?:это\s+)?(?:тот(?:\s+самый)?(?:\s+проект)?|он(?:\s+самый)?|'
                r'нужный проект|правильный проект))?|'
                r'(?:это\s+)?(?:именно\s+)?тот(?:\s+самый)?(?:\s+проект)?|это он|он самый|'
                r'это (?:нужный|правильный) проект|'
                r'ты нашел (?:правильный|нужный|тот) проект|верно|правильно|подтверждаю)')
    if re.fullmatch(positive, t):
        return True, None
    numbers = {'первый': 1, 'второй': 2, 'третий': 3, 'четвертый': 4,
               'пятый': 5, 'шестой': 6, 'седьмой': 7, 'восьмой': 8}
    number = re.fullmatch(r'(?:да\s+)?(?:выбираю\s+|это\s+)?(первый|второй|третий|четвертый|'
                          r'пятый|шестой|седьмой|восьмой|[1-8])(?:\s+(?:проект|вариант))?', t)
    if number:
        return True, numbers.get(number[1], int(number[1]) if number[1].isdigit() else None)
    return None


def clear():
    return confirm.clear_kind('project')


def snapshot():
    value = confirm.snapshot()
    return value if value and value['kind'] == 'project' else None


def _identity(path):
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError('Папка проекта недоступна или является ссылкой.')
    return info.st_dev, info.st_ino


def stage(request, candidates, *, cancel, revision):
    choices = []
    for path in dict.fromkeys(map(Path, candidates)):
        if len(choices) >= 8 or cancel.is_set():
            break
        try:
            checked = resolve_named(str(path), kind='directory')
            choices.append({'id': uuid.uuid4().hex, 'path': checked, 'identity': _identity(checked)})
        except (ValueError, OSError):
            continue
    if not choices or cancel.is_set():
        return None
    request_id = confirm.stage('project', {'request': request, 'choices': choices, 'cancel': cancel},
                               expected_revision=revision)
    return question() if request_id else None


def question():
    pending = snapshot()
    if not pending:
        return STALE
    choices = pending['choices']
    mode = 'проверка без изменений' if pending['mode'] == 'inspect' else 'изменения по исходному поручению'
    if len(choices) == 1:
        target = choices[0]
        text = (f'Нашёл подходящий вариант: {target["name"]}. Это тот проект?\n'
                f'Папка: {target["path"]}\nПоручение: {pending["task"]}\nРежим: {mode}.\n'
                'Нажмите «Да, этот проект» или «Не тот». Можно сказать «это тот проект» или «не тот». '
                'После подтверждения продолжу исходное поручение.')
        speech = (f'Нашёл проект {target["name"]}. Это тот проект? '
                  f'После подтверждения продолжу: {mode}. '
                  + (f'Ваше поручение: {pending["task"]}. ' if pending['mode'] != 'inspect' else '')
                  + 'Ответьте: это тот проект, или не тот. Можно выбрать кнопкой.')
        return Response(text, speech=speech)
    listing = '\n'.join(f'{i}. {c["name"]} — {c["path"]}' for i, c in enumerate(choices, 1))
    text = (f'Нашёл несколько вариантов. Какой проект вы имели в виду?\n{listing}\n'
            f'Поручение: {pending["task"]}\nРежим: {mode}.\n'
            'Выберите проект кнопкой или скажите «первый», «второй» и так далее. '
            'Если ни один не подходит, нажмите «Ни один» или скажите «не тот».')
    names = '; '.join(f'{i}: {c["name"]}' for i, c in enumerate(choices, 1))
    speech = (f'Нашёл несколько проектов: {names}. Какой выбрать? Назовите номер или нажмите кнопку. '
              f'После выбора продолжу: {mode}. '
              + (f'Ваше поручение: {pending["task"]}.' if pending['mode'] != 'inspect' else ''))
    return Response(text, speech=speech)


def command(text, request_id=None):
    answer = parse_answer(text)
    pending = snapshot()
    if answer is None:
        return None
    approved, ordinal = answer
    if request_id is not None and (not pending or pending['id'] != request_id):
        return ('__PROJECT_CONFIRM__', request_id, None, approved)
    if not pending:
        return None
    choice_id = None
    if ordinal is not None:
        choice_id = pending['choices'][ordinal - 1]['id'] if ordinal <= len(pending['choices']) else 'invalid'
    elif approved and len(pending['choices']) == 1:
        choice_id = pending['choices'][0]['id']
    return ('__PROJECT_CONFIRM__', pending['id'], choice_id, approved)


def consume(request_id, choice_id, approved):
    """Atomic consume, also for queued voice/UI races. Returns (request, choice, reply)."""
    if not isinstance(approved, bool) or not isinstance(request_id, str):
        return None, None, STALE
    with confirm.LOCK:
        pending = snapshot()
        if not pending or pending['id'] != request_id:
            return None, None, STALE
        if not approved:
            confirm.consume('project', 'отмена', request_id=request_id)
            return None, None, REJECTED
        if choice_id is None and len(pending['choices']) > 1:
            return None, None, 'Укажите номер проекта или выберите его кнопкой: вариантов несколько.'
        if choice_id not in {choice['id'] for choice in pending['choices']}:
            return None, None, 'Такого варианта нет. Выберите проект из показанного списка.'
        status, payload = confirm.consume('project', 'подтверждаю', request_id=request_id)
        if status != 'confirmed':
            return None, None, STALE
        choice = next(c for c in payload['choices'] if c['id'] == choice_id)
        return replace(payload['request'], project=str(choice['path']), location=''), choice, ''


def validate(choice):
    """Recheck permissions and identity immediately before dispatch, never rediscover."""
    path = resolve_named(str(choice['path']), kind='directory')
    if _identity(path) != choice['identity']:
        raise ValueError('Папка проекта изменилась после вопроса. Укажите проект заново.')
    return path

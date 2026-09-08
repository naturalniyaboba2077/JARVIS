"""Execution-derived project status, separate from untrusted model conclusions.

The prose guard catches explicit contradictory claims, not every possible lie.
Deterministic status/coverage/check results remain authoritative. No tool calls,
model inference, filesystem access or action-tag parsing occurs in this module.
"""

import re

from jarvis_agent_context import clip_utf8


_WRITE_CLAIM = re.compile(
    r"\b(?:исправил[аи]?|исправлен[аоы]?|изменил[аи]?|измен[её]н[аоы]?|"
    r"перезаписа[лн]\w*|обновил[аи]?|создал[аи]?|сохранил[аи]?|"
    r"внес[её]н[аоы]?|вн[её]с|применил[аи]?|fixed|modified|updated|patched|implemented|wrote|created)\b", re.I)
_TEST_CLAIM = re.compile(
    r"\b(?:тест\w*|проверк\w*)\b[^.\n]{0,65}\b(?:пройден\w*|прош\w*|успеш\w*|зел[её]н\w*)\b|"
    r"\b(?:tests?\s+(?:all\s+)?passed|all\s+tests|passed\s+(?:all\s+)?tests?)\b", re.I)
_READ_CLAIM = re.compile(r"\b(?:изучил[аи]?|проанализировал[аи]?|прочитал[аи]?|просмотрел[аи]?|"
                         r"провед[её]н\w*\s+анализ|reviewed|inspected)\b", re.I)
_CHECK_CLAIM = re.compile(
    r"\b(?:проверка|компиляция)\s+(?:(?:синтаксиса|кода)\s+)?(?:была\s+)?(?:выполнена|запущена|проведена)\b|"
    r"\b(?:запустил[аи]?|выполнил[аи]?)\s+(?:команду|проверку|тесты)\b", re.I)
_TASK_CLAIM = re.compile(r"\b(?:поручение|задача|задание)\s+(?:полностью\s+)?(?:выполнен[аоы]?|завершен[аоы]?|завершён[аоы]?)\b", re.I)
_FULL_CLAIM = re.compile(
    r"\b(?:изучил\w*|прочитал\w*|проверил\w*)\s+(?:\w+\s+){0,3}(?:весь\s+(?:проект|код)|все\s+файлы)\b|"
    r"\b(?:все\s+файлы|весь\s+проект)\s+(?:полностью\s+)?(?:изучен\w*|прочитан\w*|проверен\w*)\b|"
    r"\b(?:полностью\s+проверен\w*|полная\s+проверка\s+завершена|(?:reviewed|checked)\s+all\s+files)\b", re.I)


class ExecutionEvidence:
    def __init__(self):
        self.writes = []
        self.checks = []
        self.shell_attempted = False
        self.revision = 0
        self.workflow_required = False
        self.verification = None

    def set_verification(self, result):
        self.verification = {'revision': self.revision, **result}

    def verification_summary(self):
        if self.verification is None:
            return 'Автопроверка после записи ещё не выполнена.'
        result = self.verification
        lines = ['Автопроверка: ' + {'passed': 'выбранные проверки пройдены', 'failed': 'ошибка',
                 'unavailable': 'неполная', 'interrupted': 'прервана'}.get(result['status'], 'не подтверждена')]
        if result['revision'] != self.revision:
            lines.append('Результат устарел после новой операции изменения.')
        for check in result['checks']:
            label = 'Синтаксис, без исполнения кода' if check['kind'] == 'syntax' else 'Проверка поведения'
            lines.append(f"{label}: exit={check['exit']}\n" + clip_utf8(check['output'], 1200))
        lines.extend(result.get('notes', []))
        return '\n'.join(lines)

    def mutation_attempt(self):
        # Even a failed operation might already have touched a file.
        self.revision += 1

    def record_write(self, path, sha256):
        self.mutation_attempt()
        self.writes.append({"path": path, "sha256": sha256})

    def record_check(self, output, *, syntax=False):
        status = re.match(r"exit=(-?\d+)\b", output)
        self.checks.append({"write_revision": self.revision, "syntax": syntax,
                            "exit": int(status.group(1)) if status else None,
                            "output": clip_utf8(output, 700)})

    def checked_after_write(self):
        if self.workflow_required:
            return bool(self.writes and self.verification and self.verification['revision'] == self.revision
                        and self.verification['status'] == 'passed')
        return bool(self.writes and self.checks and
                    self.checks[-1]["write_revision"] == self.revision and
                    self.checks[-1]["exit"] == 0)

    def contradictions(self, text, *, read_pages, mode):
        # Only inspect prose; quoted source code is data, not a completion claim.
        prose = re.sub(r"```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)|`[^`\n]*`", "", text)
        # Do not treat explicit negative reports as positive success claims.
        prose = re.sub(r"\b(?:не|not|never)\s+\w+", "", prose, flags=re.I)
        issues = []
        if not read_pages and _READ_CLAIM.search(prose):
            issues.append("чтение исходников не подтверждено")
        if _WRITE_CLAIM.search(prose) and (mode == "inspect" or not self.writes):
            issues.append("заявленные изменения не подтверждены записью файла")
        # A zero exit code (especially compile) is not proof that all tests passed.
        # Show actual command results below instead of model-authored test claims.
        if _TEST_CLAIM.search(prose):
            issues.append("итог проверок определяется результатами команд, не словами модели")
        if not self.checks and not (self.verification and self.verification['checks']) and _CHECK_CLAIM.search(prose):
            issues.append("запуск проверочной команды не подтверждён")
        if mode == "modify" and not self.checked_after_write() and _TASK_CLAIM.search(prose):
            issues.append("завершение правки с последующей проверкой не подтверждено")
        if _FULL_CLAIM.search(prose):
            issues.append("полный охват проекта не подтверждён")
        return issues

    def status(self, mode):
        lines = []
        if self.workflow_required and self.writes:
            lines.append(self.verification_summary())
        if mode == "modify":
            if self.writes:
                paths = list(dict.fromkeys(item["path"] for item in self.writes))
                lines.append("Подтверждена запись: " + ", ".join(paths) + ".")
                if not self.checked_after_write():
                    lines.append("Успешная проверка после последней записи не подтверждена.")
            else:
                lines.append("Изменения файлов не подтверждены через write_file.")
            if self.shell_attempted:
                lines.append("Запрашивалась команда host shell; её возможные правки не подтверждены журналом write_file.")
        if not self.checks and not (self.verification and self.verification['checks']):
            lines.append("Проверочные команды не запускались.")
        for check in self.checks[-3:]:
            label = "Синтаксис, без исполнения кода" if check["syntax"] else "Результат команды (не доказательство всех тестов)"
            lines.append(label + ":\n" + check["output"])
        return "\n".join(lines)

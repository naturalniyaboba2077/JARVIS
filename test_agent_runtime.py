"""Project-loop regressions: synthetic files, no network/audio/personal projects."""

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import jarvis
import jarvis_agent_context as context
from jarvis_agent_evidence import ExecutionEvidence
import project_agent as agent


def response(content="", calls=(), finish="stop"):
    return NS(choices=[NS(message=NS(content=content, tool_calls=[
        NS(id=f"call_{i}", function=NS(name=name, arguments=json.dumps(args)))
        for i, (name, args) in enumerate(calls)]), finish_reason=finish)])


class AgentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "demo"
        self.project.mkdir()
        self.source = self.project / "app.py"
        self.source.write_text("x = 1\n" * 6000, encoding="utf-8")
        env = patch.dict(os.environ, {"JARVIS_PROJECT_ROOTS": str(self.root),
                                     "LM_STUDIO_CONTEXT": "8192"})
        env.start()
        self.addCleanup(env.stop)
        jarvis._state.interrupt_event.clear()
        self.client = Mock()
        self.requests = []
        # These legacy tests exercise model-requested paging in isolation.
        # Automatic preparation and verification have their own workflow suite.
        prepare = patch.object(agent, '_prepare_project', return_value=('', [], {
            'runners': [], 'frozen': {}, 'notes': ['Тесты не найдены.'], 'complete': False}))
        prepare.start()
        self.addCleanup(prepare.stop)

    def run_agent(self, answers, **kwargs):
        replies = iter(answers)

        def create(**request):
            self.requests.append(copy.deepcopy(request))
            result = next(replies)
            if isinstance(result, Exception):
                raise result
            return result

        self.client.chat.completions.create.side_effect = create
        mode = kwargs.pop("mode", "inspect")
        task = kwargs.pop("task", "Проверь приложение, не меняй файлы" if mode == "inspect" else "Исправь app.py")
        return agent.run_project_agent(self.client, "synthetic", str(self.project), task, mode=mode, **kwargs)

    def test_large_unicode_file_pages_reassemble_without_loss(self):
        text = ("Привет, мир 😀\n" + '"\\' * 50) * 60
        self.source.write_text(text, encoding="utf-8", newline="")
        offset, chunks = 0, []
        while offset is not None:
            raw = agent._execute(self.project, "read_file", {"path": "app.py", "offset": offset})
            page = json.loads(raw)
            self.assertLessEqual(len(raw.encode("utf-8")), agent.MAX_TOOL_BYTES)
            chunks.append(page["text"])
            if page["next_offset"] is not None:
                self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.assertEqual("".join(chunks), text)

    def test_page_does_not_send_entire_big_file(self):
        page = json.loads(agent._execute(self.project, "read_file", {"path": "app.py"}))
        self.assertLess(len(page["text"]), page["total_chars"])
        self.assertEqual(page["next_offset"], len(page["text"]))

    def test_invalid_offsets_rejected(self):
        for value in (-1, True, "1"):
            with self.assertRaises(ValueError):
                agent._execute(self.project, "read_file", {"path": "app.py", "offset": value})

    def test_empty_file_and_offset_past_eof_terminate(self):
        self.source.write_text("", encoding="utf-8")
        for offset in (0, 100):
            page = json.loads(agent._execute(self.project, "read_file", {"path": "app.py", "offset": offset}))
            self.assertIsNone(page["next_offset"])

    def test_no_tool_report_does_not_claim_inspection(self):
        result = self.run_agent([response("Нужны исходники.")])
        self.assertIn("содержимое кода модель не проверила", result)

    def test_repeated_pages_do_not_execute_again_and_force_report(self):
        read = response(calls=[("read_file", {"path": "app.py"})])
        with patch.object(agent, "_execute", wraps=agent._execute) as execute:
            result = self.run_agent([read, read, read, response("Прочитана только часть app.py.")])
        reads = [c for c in execute.call_args_list if c.args[1] == "read_file"]
        self.assertEqual(len(reads), 1)
        self.assertIn("повторные вызовы", result)
        self.assertIn("Прочитана только часть", result)
        self.assertNotIn("tools", self.requests[-1])

    def test_page_size_or_dot_alias_does_not_bypass_repeat_guard(self):
        self.assertEqual(agent._call_key(self.project, "read_file", {"path": "app.py"}),
                         agent._call_key(self.project, "read_file", {"path": "./app.py", "offset": 0, "limit": 1}))

    def test_distinct_pages_are_progress(self):
        result = self.run_agent([response(calls=[("read_file", {"path": "app.py", "offset": 0})]),
                                 response(calls=[("read_file", {"path": "app.py", "offset": 1800})]),
                                 response("Рассмотрены два фрагмента.")])
        self.assertNotIn("повторные вызовы", result)
        self.assertIn("offset=1800", result)

    def test_step_limit_gets_summary_not_generic_changes_message(self):
        with patch.object(agent, "MAX_TOOL_STEPS", 1):
            result = self.run_agent([response(calls=[("read_file", {"path": "app.py"})]),
                                     response("Проверен фрагмент; остальная часть не изучена.")])
        self.assertIn("лимит шагов", result)
        self.assertIn("Проверен фрагмент", result)
        self.assertIn("тесты приложения не запускались", result)
        self.assertNotIn("Проверьте изменения", result)
        self.assertEqual(len(self.requests), 2)

    def test_summary_failure_keeps_real_evidence(self):
        with patch.object(agent, "MAX_TOOL_STEPS", 1):
            result = self.run_agent([response(calls=[("read_file", {"path": "app.py"})]), RuntimeError("private-key")])
        self.assertIn("app.py", result)
        self.assertIn("прочитана страница", result)
        self.assertIn("Осталось", result)
        self.assertNotIn("private-key", result)

    def test_final_tool_calls_are_never_executed(self):
        with patch.object(agent, "MAX_TOOL_STEPS", 0), patch.object(agent, "_execute", wraps=agent._execute) as execute:
            result = self.run_agent([response(calls=[("write_file", {"path": "bad.py", "content": "oops"})])])
        self.assertEqual(execute.call_count, 1)  # initial listing only
        self.assertFalse((self.project / "bad.py").exists())
        self.assertIn("Частичный отчёт", result)

    def test_invalid_json_and_unknown_tool_never_execute(self):
        bad = response(calls=[("read_file", {"path": "app.py"})])
        bad.choices[0].message.tool_calls[0].function.arguments = "<path>app.py</path>"
        with patch.object(agent, "_execute", wraps=agent._execute) as execute:
            result = self.run_agent([bad, response(calls=[("delete_project", {})]), response("Не удалось продолжить.")])
        self.assertEqual(execute.call_count, 1)
        self.assertIn("ошибки инструментов", result)

    def test_missing_file_errors_are_not_success(self):
        bad = response(calls=[("read_file", {"path": "missing.py"})])
        result = self.run_agent([bad, bad, response("Файл не найден.")])
        self.assertIn("read_file: missing.py", result)
        self.assertIn("error", result)
        self.assertIn("содержимое кода модель не проверила", result)

    def test_oversized_batch_executes_no_partial_plan(self):
        with patch.object(agent, "_execute", wraps=agent._execute) as execute:
            result = self.run_agent([response(calls=[("list_files", {})] * 5), response("Нужен более узкий обзор.")])
        self.assertEqual(execute.call_count, 1)
        self.assertIn("слишком много", result)

    def test_48k_context_regression_all_requests_stay_bounded(self):
        answers = [response(calls=[("read_file", {"path": "app.py", "offset": i * 1800})])
                   for i in range(12)] + [response("Частичный обзор.")]
        result = self.run_agent(answers)
        self.assertIn("Частичный", result)
        self.assertEqual(len(self.requests), 13)
        for request in self.requests:
            messages = request["messages"]
            self.assertLessEqual(context.request_bytes(messages, request.get("tools", [])), 8192 - 1800)
            self.assertIn("Проверь приложение, не меняй файлы", messages[1]["content"])
            for index, message in enumerate(messages):
                if message.get("tool_calls"):
                    ids = [c["id"] for c in message["tool_calls"]]
                    self.assertEqual(ids, [m["tool_call_id"] for m in messages[index+1:index+1+len(ids)]])

    def test_provider_context_error_goes_to_smaller_tool_free_summary(self):
        result = self.run_agent([RuntimeError("request 48138 tokens exceeds 20224 secret"), response("Не удалось начать чтение.")])
        self.assertIn("ошибка запроса", result)
        self.assertNotIn("secret", result)
        if len(self.requests) > 1:
            self.assertNotIn("tools", self.requests[-1])
            self.assertLess(context.request_bytes(self.requests[-1]["messages"], []), 4000)

    def test_empty_or_truncated_model_answer_gets_final_attempt(self):
        for initial in (response(), response("Незаконченная фраза", finish="length")):
            with self.subTest(initial=initial):
                result = self.run_agent([initial, response("Не удалось закончить обзор.")])
                self.assertIn("Частичный отчёт", result)
                self.assertIn("Не удалось закончить", result)

    def test_cancel_before_start_does_no_io(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(agent, "_execute") as execute:
            result = self.run_agent([], cancel_event=cancel)
        execute.assert_not_called()
        self.assertIn("прервана", result)

    def test_cancel_from_tool_progress_prevents_execution(self):
        cancel = threading.Event()
        def progress(text):
            if "Читаю файл" in text:
                cancel.set()
        with patch.object(agent, "_execute", wraps=agent._execute) as execute:
            result = self.run_agent([response(calls=[("read_file", {"path": "app.py"})])],
                                    cancel_event=cancel, progress_fn=progress)
        self.assertEqual(execute.call_count, 1)
        self.assertIn("прервана", result)

    def test_nonzero_exit_status_is_never_success(self):
        execute = agent._execute
        def fake(root, name, args, mode="inspect"):
            return "exit=2\nsynthetic failure" if name == "run_command" else execute(root, name, args, mode=mode)
        with patch.object(agent, "_execute", side_effect=fake):
            result = self.run_agent([response(calls=[("run_command", {"command": "compile"})]), response("Проверка не прошла.")])
        self.assertIn("run_command — error", result)

    def test_page_metadata_distinguishes_characters_from_lines(self):
        self.source.write_text("one\ntwo\nthree\n", encoding="utf-8", newline="")
        page = json.loads(agent._execute(self.project, "read_file", {"path": "app.py", "offset": 4, "limit": 4}))
        self.assertEqual((page["start_line"], page["end_line"], page["text"]), (2, 2, "two\n"))
        self.assertEqual(page["unit"], "characters")

    def test_cancel_after_response_prevents_tools_and_summary(self):
        cancel = threading.Event()

        def answer(**kwargs):
            cancel.set()
            return response(calls=[("read_file", {"path": "app.py"})])

        self.client.chat.completions.create.side_effect = answer
        with patch.object(agent, "_execute", wraps=agent._execute) as execute:
            result = agent.run_project_agent(self.client, "mock", str(self.project), "проверь",
                                             mode="inspect", cancel_event=cancel)
        self.assertEqual(execute.call_count, 1)
        self.assertIn("прервана", result)
        self.assertEqual(self.client.chat.completions.create.call_count, 1)

    def test_progress_includes_tool_and_final_stage(self):
        stages = []
        with patch.object(agent, "MAX_TOOL_STEPS", 1):
            self.run_agent([response(calls=[("read_file", {"path": "app.py"})]), response("Часть проверена.")], progress_fn=stages.append)
        self.assertTrue(any("app.py" in s for s in stages))
        self.assertTrue(any("частичный отчёт" in s for s in stages))

    def test_failing_progress_callback_does_not_break_agent(self):
        result = self.run_agent([response("Ответ")], progress_fn=Mock(side_effect=RuntimeError()))
        self.assertIn("Ответ", result)

    def test_user_task_too_large_is_not_silently_truncated(self):
        result = agent.run_project_agent(self.client, "mock", str(self.project), "задача " * 5000, mode="inspect")
        self.client.chat.completions.create.assert_not_called()
        self.assertIn("Частичный отчёт", result)

    def test_validation_rejects_non_object_extra_args_and_boolean_offset(self):
        for raw in ('[]', '{"path":"app.py","oops":1}', '{"path":"app.py","offset":true}'):
            with self.assertRaises(ValueError):
                agent._validated_args("read_file", raw, agent._tools("inspect"))

    def test_partial_read_cannot_overwrite_entire_file(self):
        before = self.source.read_bytes()
        result = self.run_agent([response(calls=[("read_file", {"path": "app.py"})]),
                                response(calls=[("write_file", {"path": "app.py", "content": "x = 2\n"})]),
                                response("Не завершил изменение.")], mode="modify")
        self.assertEqual(self.source.read_bytes(), before)
        self.assertIn("Полная перезапись не выполнена", result)

    def test_full_read_allows_versioned_write(self):
        self.source.write_text("x = 1\n", encoding="utf-8")
        result = self.run_agent([response(calls=[("read_file", {"path": "app.py"})]),
                                response(calls=[("write_file", {"path": "app.py", "content": "x = 2\n"})]),
                                response("Изменено.")], mode="modify")
        self.assertEqual(self.source.read_text(encoding="utf-8"), "x = 2\n")
        self.assertIn("write_file: app.py — ok", result)

    def test_gap_or_changed_hash_refuses_full_overwrite(self):
        content = self.source.read_bytes()
        record = {"sha256": hashlib.sha256(content).hexdigest(), "total": len(content),
                  "spans": [(0, 2), (4, len(content))]}
        coverage = {os.path.normcase(str(self.source)): record}
        with self.assertRaisesRegex(ValueError, "частично"):
            agent._check_complete_read(self.project, "app.py", coverage)
        record["spans"] = [(0, len(content))]
        self.source.write_text("external edit", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "изменился"):
            agent._check_complete_read(self.project, "app.py", coverage)

    def test_unread_model_success_is_not_displayed_as_fact(self):
        result = self.run_agent([response("Я изучил весь код и исправил ошибки. Тесты пройдены.")])
        self.assertNotIn("Я изучил весь код", result)
        self.assertNotIn("Тесты пройдены", result)
        self.assertIn("содержимое кода модель не проверила", result)

    def test_read_without_write_does_not_claim_fixed(self):
        self.source.write_text("x = 1\n", encoding="utf-8")
        result = self.run_agent([response(calls=[("read_file", {"path": "app.py"})]),
                                 response("Исправил app.py. Изменения внесены, тесты пройдены.")], mode="modify")
        self.assertNotIn("Изменения внесены", result)
        self.assertNotIn("тесты пройдены", result.lower())
        self.assertIn("не подтверждены", result)
        self.assertEqual(self.source.read_text(encoding="utf-8"), "x = 1\n")

    def test_partial_summary_cannot_invent_writes(self):
        result = self.run_agent([RuntimeError("synthetic template failure"),
                                 response("Изменения внесены. Все файлы исправлены.")], mode="modify")
        self.assertNotIn("Изменения внесены", result)
        self.assertNotIn("Все файлы исправлены", result)

    def test_verification_before_write_is_not_after_write_verification(self):
        self.source.write_text("x = 1\n", encoding="utf-8")
        result = self.run_agent([response(calls=[("read_file", {"path": "app.py"})]),
                                 response(calls=[("run_command", {"command": "compile app.py"})]),
                                 response(calls=[("write_file", {"path": "app.py", "content": "x = 2\n"})]),
                                 response("Изменения внесены. Тесты пройдены.")], mode="modify")
        self.assertNotIn("Тесты пройдены", result)
        self.assertIn("после последней записи", result)

    def test_compile_success_does_not_become_passed_application_tests(self):
        self.source.write_text("x = 1\n", encoding="utf-8")
        result = self.run_agent([response(calls=[("read_file", {"path": "app.py"})]),
                                 response(calls=[("write_file", {"path": "app.py", "content": "x = 2\n"})]),
                                 response(calls=[("run_command", {"command": "compile app.py"})]),
                                 response("Исправлено. Все тесты пройдены.")], mode="modify")
        self.assertNotIn("Все тесты пройдены", result)
        self.assertIn("Синтаксис", result)

    def test_real_8k_prompt_retains_complete_source_beside_empty_eof(self):
        code = ('def total_value(items):\n    return sum(item["price"] for item in items)\n\n'
                'def find_equipment(items, name):\n    return next(item for item in items if item["name"] == name)\n')
        self.source.write_text(code, encoding="utf-8", newline="")
        self.run_agent([response(calls=[("read_file", {"path": "app.py"})]),
                        response(calls=[("read_file", {"path": "app.py", "offset": len(code)})]),
                        response("Фрагмент рассмотрен.")], mode="modify", context_tokens=8192,
                       task="Исправь total_value в app.py: стоимость каждой позиции равна price * quantity. "
                            "Не меняй find_equipment. После правки проверь результат и напиши отчёт.")
        request = self.requests[2]
        self.assertLessEqual(context.request_bytes(request["messages"], request["tools"]), 6392)
        pages = [json.loads(m["content"]) for m in request["messages"]
                 if m.get("role") == "tool" and m.get("name") == "read_file"]
        self.assertTrue(any(p.get("text") == code for p in pages))
        self.assertTrue(any(p.get("eof") and not p.get("text") for p in pages))

    def test_write_invalidates_only_target_snapshot_even_when_tool_fails(self):
        self.source.write_text("old_target = 1\n", encoding="utf-8")
        (self.project / "other.py").write_text("other_evidence = 2\n", encoding="utf-8")
        execute = agent._execute
        for fails in (False, True):
            self.requests.clear()
            self.source.write_text("old_target = 1\n", encoding="utf-8")
            def mutate(root, name, args, mode="modify"):
                if name == "write_file" and fails:
                    self.source.write_text("partial_mutation = 9\n", encoding="utf-8")
                    raise OSError("synthetic failure after mutation")
                return execute(root, name, args, mode=mode)
            with patch.object(agent, "_execute", side_effect=mutate):
                self.run_agent([response(calls=[("read_file", {"path": "app.py"}),
                                                ("read_file", {"path": "other.py"})]),
                                response(calls=[("write_file", {"path": "app.py", "content": "new_target = 3\n"})]),
                                response("Не завершил проверку.")], mode="modify", context_tokens=16000)
            observations = [m["content"] for m in self.requests[2]["messages"]
                            if m.get("role") == "tool" and m.get("name") == "read_file"]
            self.assertFalse(any("old_target" in text for text in observations))
            self.assertTrue(any("other_evidence" in text for text in observations))

    def test_faked_write_success_is_not_recorded(self):
        self.source.write_text("x = 1\n", encoding="utf-8")
        execute = agent._execute
        def fake(root, name, args, mode="modify"):
            return "Перезаписан: app.py" if name == "write_file" else execute(root, name, args, mode=mode)
        with patch.object(agent, "_execute", side_effect=fake):
            result = self.run_agent([response(calls=[("read_file", {"path": "app.py"})]),
                                    response(calls=[("write_file", {"path": "app.py", "content": "x = 2\n"})]),
                                    response("Не завершил задачу.")], mode="modify")
        self.assertIn("Запись не подтверждена", result)
        self.assertNotIn("Подтверждена запись:", result)
        self.assertEqual(self.source.read_text(encoding="utf-8"), "x = 1\n")


class EvidenceStatusTests(unittest.TestCase):
    def test_mutation_or_second_write_invalidates_previous_check(self):
        evidence = ExecutionEvidence()
        evidence.record_write("app.py", "first")
        evidence.record_check("exit=0\nsyntax OK", syntax=True)
        self.assertTrue(evidence.checked_after_write())
        evidence.mutation_attempt()
        self.assertFalse(evidence.checked_after_write())
        evidence.record_check("exit=0\nsyntax OK", syntax=True)
        evidence.record_write("app.py", "second")
        self.assertFalse(evidence.checked_after_write())

    def test_negative_reports_and_remaining_work_are_not_success_claims(self):
        evidence = ExecutionEvidence()
        for text in ("Я не исправил app.py.", "Осталось проверить все файлы.", "Тесты не прошли.",
                     "Для внесения изменений нужно исправление кода.",
                     "Проверка компиляции может быть выполнена после изменений."):
            self.assertEqual(evidence.contradictions(text, read_pages=1, mode="modify"), [])

    def test_invented_command_execution_is_not_an_observation(self):
        evidence = ExecutionEvidence()
        self.assertTrue(evidence.contradictions("Проверка выполнена через run_command.", read_pages=1, mode="inspect"))

    def test_task_completion_without_postwrite_check_is_not_confirmed(self):
        evidence = ExecutionEvidence()
        evidence.record_write("app.py", "hash")
        self.assertTrue(evidence.contradictions("Поручение выполнено.", read_pages=1, mode="modify"))
        for text in ("Файл исправлен.", "Запись изменена.", "Изменения внесены."):
            self.assertTrue(ExecutionEvidence().contradictions(text, read_pages=1, mode="modify"))


class EvidencePackingTests(unittest.TestCase):
    def test_small_complete_source_survives_empty_eof_and_listings(self):
        def exchange(number, name, output):
            return [{"role": "assistant", "content": "", "tool_calls": [
                {"id": str(number), "type": "function", "function": {"name": name, "arguments": "{}"}}]},
                {"role": "tool", "name": name, "tool_call_id": str(number), "content": output}]
        code = json.dumps({"path": "app.py", "offset": 0, "end_offset": 6,
                           "total_chars": 6, "next_offset": None, "text": "x = 1\n"})
        groups = [exchange(0, "read_file", code)]
        groups += [exchange(i, "list_files", "irrelevant.txt\n" * 30) for i in range(1, 6)]
        groups += [exchange(6, "read_file", json.dumps({"text": "", "next_offset": None}))]
        original = copy.deepcopy(groups)
        messages = context.pack_messages([{"role": "system", "content": "Keep instructions"}], groups, [], [], 1800)
        self.assertTrue(any(m.get("tool_call_id") == "0" for m in messages))
        self.assertLessEqual(context.request_bytes(messages, []), 1800)
        self.assertEqual(groups, original)
        for i, msg in enumerate(messages):
            if msg.get("tool_calls"):
                self.assertEqual(messages[i + 1]["tool_call_id"], msg["tool_calls"][0]["id"])


class ContextMetadataTests(unittest.TestCase):
    def test_loaded_instance_not_advertised_capacity(self):
        payload = {"models": [{"key": "qwen", "max_context_length": 262144,
                    "loaded_instances": [{"id": "active", "config": {"context_length": 20000}}]}]}
        connection = Mock()
        connection.getresponse.return_value = NS(status=200, read=Mock(return_value=json.dumps(payload).encode()))
        with patch.object(context, "HTTPConnection", return_value=connection):
            self.assertEqual(context.runtime_context(NS(base_url="http://127.0.0.1:1234/v1"), "active", 8192), 20000)
        connection.request.assert_called_once_with("GET", "/api/v1/models")
        connection.close.assert_called_once()

    def test_remote_endpoint_never_probed(self):
        with patch.object(context, "HTTPSConnection") as connect:
            self.assertEqual(context.runtime_context(NS(base_url="https://remote.example/v1"), "qwen", 8192), 8192)
        connect.assert_not_called()

    def test_metadata_failure_uses_configured_fallback(self):
        with patch.object(context, "HTTPConnection", side_effect=OSError()):
            self.assertEqual(context.runtime_context(NS(base_url="http://localhost:1234/v1"), "qwen", 8192), 8192)

    def test_local_redirect_is_not_followed(self):
        connection = Mock()
        connection.getresponse.return_value.status = 302
        with patch.object(context, "HTTPConnection", return_value=connection):
            self.assertEqual(context.runtime_context(NS(base_url="http://localhost:1234/v1"), "qwen", 8192), 8192)
        self.assertEqual(connection.request.call_count, 1)


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        jarvis._state.interrupt_event.clear()

    def test_announcement_precedes_discovery_and_progress_is_displayed(self):
        events = []
        def resolve(*args):
            events.append("resolve")
            return Path("C:/demo")
        def run(*args, **kwargs):
            kwargs["progress_fn"]("Читаю app.py")
            return "Итог"
        with patch.object(agent, "_resolve_project", side_effect=resolve), patch.object(agent, "run_project_agent", side_effect=run), patch.object(jarvis, "LLM_ENGINE", "lmstudio"), patch.object(jarvis, "get_lmstudio_client"), patch.object(jarvis, "ui_sub") as sub:
            result = jarvis.handle_local_feature_command("проверь проект Demo", progress_fn=events.append)
        self.assertEqual(events, ["Начинаю проверку проекта, сэр.", "resolve"])
        self.assertEqual(result, "Итог")
        sub.assert_any_call("Читаю app.py")

    def test_cancellation_during_announcement_latches_even_after_clear(self):
        def stop(text):
            jarvis._state.interrupt_event.set()
            jarvis._state.interrupt_event.clear()
        with patch.object(agent, "_resolve_project") as resolve:
            result = jarvis.handle_local_feature_command("проверь проект Demo", progress_fn=stop)
        resolve.assert_not_called()
        self.assertIn("прервана", result)

    def test_ambiguous_task_does_not_announce(self):
        speak = Mock()
        jarvis.handle_local_feature_command("поработай над проектом Demo", progress_fn=speak)
        speak.assert_not_called()

    def test_streaming_entry_speaks_ack_and_result_once(self):
        def handle(text, **kwargs):
            kwargs["progress_fn"]("Начинаю проверку проекта, сэр.")
            return "Итог"
        with patch.object(jarvis, "handle_local_feature_command", side_effect=handle), patch.object(jarvis, "speak") as speak:
            jarvis.process_with_llm_streaming("проверь проект Demo")
        self.assertEqual([c.args[0] for c in speak.call_args_list], ["Начинаю проверку проекта, сэр.", "Итог"])

    def test_silent_compatibility_entry_stays_silent(self):
        with patch.object(jarvis, "handle_local_feature_command", return_value="Итог") as handle, patch.object(jarvis, "speak") as speak:
            jarvis.process_with_llm("проверь проект Demo")
        speak.assert_not_called()
        self.assertNotIn("progress_fn", handle.call_args.kwargs)


class InferenceWaitTests(unittest.TestCase):
    def test_timeout_keeps_single_slot_until_worker_finishes(self):
        entered, release = threading.Event(), threading.Event()
        cancel = threading.Event()
        client = Mock()
        def slow(**kwargs):
            entered.set()
            release.wait(2)
            return response("late")
        client.chat.completions.create.side_effect = slow
        slot = threading.BoundedSemaphore(1)
        with patch.object(context, "_INFERENCE_SLOT", slot):
            try:
                with self.assertRaises(TimeoutError):
                    context.completion(client, cancel, time.monotonic() + 0.08)
                self.assertTrue(entered.is_set())
                with self.assertRaises(TimeoutError):
                    context.completion(client, cancel, time.monotonic() + 1)
                self.assertEqual(client.chat.completions.create.call_count, 1)
            finally:
                release.set()
                self.assertTrue(slot.acquire(timeout=2))
                slot.release()


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""September audit regressions: no real desktop, accounts, or network actions."""

from contextlib import ExitStack
from pathlib import Path
import queue
import json
import threading
import types
import unittest
from unittest.mock import patch, call

import jarvis
import jarvis_tools
from jarvis_actions import Action, is_action_discussion, parse_actions


class ParserTests(unittest.TestCase):
    def test_repeated_actions_keep_source_order(self):
        prose, actions = parse_actions("ok [TODO:ADD:first] [TODO:ADD:second] [TODO:LIST]")
        self.assertEqual(prose, "ok")
        self.assertEqual(actions, (Action("TODO:ADD", ("first",)),
                                   Action("TODO:ADD", ("second",)), Action("TODO:LIST")))

    def test_shell_brackets_and_quotes(self):
        for command in ("Write-Output (1,2,3)[0]", 'Write-Output "[x]"',
                        "Write-Output 'don''t ] stop'", 'Write-Output "literal `]"',
                        "$a = @(1,2)\n$a[0]", '[string]::Join(",", @(1,2))'):
            with self.subTest(command=command):
                self.assertEqual(parse_actions(f"[CMD:{command}]")[1],
                                 (Action("CMD", (command,)),))

    def test_multiline_nested_payload_is_data(self):
        payload = "Don't delete [OB:DELETE:note]\nsecond:line"
        self.assertEqual(parse_actions(f"[OB:WRITE:title:{payload}]")[1],
                         (Action("OB:WRITE", ("title", payload)),))

    def test_typed_arguments_and_defaults(self):
        cases = {"[WEATHER]": Action("WEATHER", ("Москва",)),
                 "[MEMORY:RECALL]": Action("MEMORY:RECALL", (None,)),
                 "[CAL:ADD:9:05:meeting]": Action("CAL:ADD", ("09:05", "meeting")),
                 "[REMIND:09:30:test]": Action("REMIND", ("09:30", "test")),
                 "[TIMER:60]": Action("TIMER", (60, "")),
                 "[TG:READ:peer]": Action("TG:READ", ("peer", 10)),
                 "[TG:EXPORT:peer]": Action("TG:EXPORT", ("peer", 200)),
                 "[FILE:OPEN:C:\\tmp\\file.txt]": Action("FILE:OPEN", ("C:\\tmp\\file.txt",)),
                 "[MAIL:SEARCH:from:a@example.com]": Action("MAIL:SEARCH", ("from:a@example.com",))}
        for tag, action in cases.items():
            with self.subTest(tag=tag):
                self.assertEqual(parse_actions(tag)[1], (action,))

    def test_malformed_known_tags_rejected(self):
        for tag in ("[TODO:ADD:]", "[TODO:ADD:x", "[SYS:VOL:101]", "[TIMER:0]",
                    "[REMIND:25:00:bad]", "[TG:SEND:user]", "[TODO:LIST:extra]",
                    "[MEDIA:BOGUS]", "[EXECUTE_PYTHON]x=1", '[CMD:"unclosed]'):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                parse_actions(tag)

    def test_multiple_python_blocks(self):
        block = "[EXECUTE_PYTHON]\n```python\nx = [1,2][0]\n```\n[/EXECUTE_PYTHON]"
        self.assertEqual(parse_actions(block + block)[1],
                         (Action("EXECUTE_PYTHON", ("x = [1,2][0]",)),) * 2)

    def test_plain_brackets_preserved(self):
        self.assertEqual(parse_actions("Массив a[0], ссылка [пример](url)."),
                         ("Массив a[0], ссылка [пример](url).", ()))

    def test_unbalanced_literal_bracket_requires_quoted_payload(self):
        payload = "Интервал (0, 1] полуоткрытый."
        with self.assertRaises(ValueError):
            parse_actions(f"[TYPE:{payload}]")
        quoted = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(parse_actions(f"[TYPE:{quoted}]")[1], (Action("TYPE", (payload,)),))
        self.assertEqual(parse_actions(f"[OB:WRITE:title:{quoted}]")[1],
                         (Action("OB:WRITE", ("title", payload)),))


class DispatchTests(unittest.TestCase):
    def setUp(self):
        jarvis._state.interrupt_event.clear()

    def tearDown(self):
        jarvis._state.interrupt_event.clear()

    def test_search_output_is_never_executed(self):
        result = "Found [OB:DELETE:audit-note]"
        with patch.object(jarvis, "search_web", return_value=result), \
                patch.object(jarvis, "ob_delete") as delete:
            self.assertEqual(jarvis.parse_and_execute_tags("[SEARCH:example]"), result)
            delete.assert_not_called()

    def test_all_repeated_tags_execute_in_order(self):
        with patch.object(jarvis, "todo_add", side_effect=lambda x: x) as add:
            self.assertEqual(jarvis.parse_and_execute_tags("[TODO:ADD:one][TODO:ADD:two]"), "one two")
            self.assertEqual(add.call_args_list, [call("one"), call("two")])

    def test_malformed_later_tag_causes_no_partial_effects(self):
        with patch.object(jarvis, "todo_add") as add:
            result = jarvis.parse_and_execute_tags("[TODO:ADD:first][SYS:VOL:999]")
            self.assertIn("некорректная", result)
            add.assert_not_called()

    def test_failure_not_replaced_by_model_success(self):
        with patch.object(jarvis, "execute_system_command", return_value=False):
            reply = jarvis.parse_and_execute_tags("Всё готово! [OPEN:notepad]")
            self.assertIn("Не удалось", reply)
            self.assertNotIn("готово", reply)

    def test_intent_fallback_reports_failure(self):
        with patch.object(jarvis, "execute_system_command", return_value=False):
            self.assertIn("Не удалось", jarvis.parse_and_execute_tags("Да", "открой браузер"))

    def test_python_result_propagates(self):
        with patch.object(jarvis, "execute_python_code", return_value="Ошибка: boom") as execute:
            self.assertEqual(jarvis.parse_and_execute_tags(
                "[EXECUTE_PYTHON]raise ValueError('boom')[/EXECUTE_PYTHON]"), "Ошибка: boom")
            execute.assert_called_once()

    def test_domain_and_shell_payload_reach_handler_intact(self):
        with patch.object(jarvis, "execute_system_command", return_value=True) as opening, \
                patch.object(jarvis, "run_shell_command", return_value="ok") as shell:
            jarvis.parse_and_execute_tags("[OPEN:youtube.com][CMD:Write-Output (1,2,3)[0]]")
            opening.assert_called_once_with("youtube.com")
            shell.assert_called_once_with("Write-Output (1,2,3)[0]")

    def test_handler_exception_is_reported_and_stops_following_actions(self):
        with patch.object(jarvis, "todo_add", side_effect=RuntimeError("disk full")), \
                patch.object(jarvis, "lock_pc") as lock:
            self.assertIn("disk full", jarvis.parse_and_execute_tags("[TODO:ADD:x][LOCK]"))
            lock.assert_not_called()

    def test_cancel_between_actions_prevents_next_action(self):
        def first(_):
            jarvis._state.interrupt_event.set()
            return "first done"
        with patch.object(jarvis, "todo_add", side_effect=first), patch.object(jarvis, "lock_pc") as lock:
            result = jarvis.parse_and_execute_tags("[TODO:ADD:x][LOCK]")
            self.assertIn("прервано", result)
            lock.assert_not_called()

    def test_discussion_and_negation_block_actions(self):
        phrases = ("если я скажу открой браузер, ты сможешь?",
                   "что значит заблокировать компьютер?", "как открыть браузер?",
                   "не открывай браузер", "не надо заблокировать компьютер",
                   "можешь ли ты отправить сообщение?", 'команда «открой браузер»',
                   "не снимай скриншот", "как сделать скриншот?", "почему громкость 80?")
        with patch.object(jarvis, "execute_system_command") as opening, \
                patch.object(jarvis, "lock_pc") as lock:
            for phrase in phrases:
                with self.subTest(phrase=phrase):
                    self.assertTrue(is_action_discussion(phrase))
                    self.assertIsNone(jarvis.detect_intent_from_text(phrase))
                    jarvis.parse_and_execute_tags("[OPEN:browser][LOCK]", phrase)
            opening.assert_not_called()
            lock.assert_not_called()

    def test_negated_action_does_not_repeat_model_success_prose(self):
        with patch.object(jarvis, "lock_pc") as lock:
            reply = jarvis.parse_and_execute_tags("Готово, компьютер заблокирован. [LOCK]",
                                                 "не блокируй компьютер")
            self.assertNotIn("компьютер заблокирован", reply)
            self.assertIn("никаких действий", reply)
            lock.assert_not_called()

    def test_malformed_type_never_types_a_truncated_prefix(self):
        with patch.object(jarvis, "type_text") as typing:
            reply = jarvis.parse_and_execute_tags("[TYPE:Интервал (0, 1] полуоткрытый.]")
            self.assertIn("некорректная", reply)
            typing.assert_not_called()

    def test_compound_request_does_not_use_single_intent_fallback(self):
        self.assertIsNone(jarvis.detect_intent_from_text("Открой калькулятор и браузер"))

    def test_action_preamble_waits_for_actual_result_before_speech(self):
        def deltas(*args, **kwargs):
            yield "Заметка сохранена. "
            yield "[OB:WRITE:test:content]"
        with patch.object(jarvis, "_build_messages", return_value=[]), \
                patch.object(jarvis, "_classify_complexity", return_value=("local", [])), \
                patch.object(jarvis, "_llm_deltas", side_effect=deltas), \
                patch.object(jarvis, "ob_write", side_effect=OSError("disk full")), \
                patch.object(jarvis, "speak_streaming") as streaming, \
                patch.object(jarvis, "speak") as speak:
            result = jarvis.process_with_llm_streaming("Сохрани заметку")
            self.assertIn("disk full", result)
            streaming.assert_not_called()
            speak.assert_called_once_with(result)
            self.assertNotIn("Заметка сохранена", result)

    def test_plain_conversation_still_streams(self):
        with patch.object(jarvis, "_build_messages", return_value=[]), \
                patch.object(jarvis, "_classify_complexity", return_value=("local", [])), \
                patch.object(jarvis, "_llm_deltas", return_value=iter(["Здравствуйте. ", "Рад встрече."])), \
                patch.object(jarvis, "speak_streaming", side_effect=lambda it: list(it)) as streaming, \
                patch.object(jarvis, "speak") as speak:
            result = jarvis.process_with_llm_streaming("привет")
            self.assertIn("Рад встрече", result)
            streaming.assert_called_once()
            speak.assert_not_called()

    def test_explicit_typing_forms_execute_once_before_speech(self):
        for command in ("печатай привет в активном приложении", "напечатай привет",
                        "пиши привет", "напиши привет"):
            with self.subTest(command=command), \
                    patch.object(jarvis, "_build_messages", return_value=[]), \
                    patch.object(jarvis, "_classify_complexity", return_value=("local", [])), \
                    patch.object(jarvis, "_llm_deltas", return_value=iter(["[TYPE:привет]"])), \
                    patch.object(jarvis, "type_text", return_value=True) as typing, \
                    patch.object(jarvis, "speak_streaming") as streaming, \
                    patch.object(jarvis, "speak") as speak:
                result = jarvis.process_with_llm_streaming(command)
                typing.assert_called_once_with("привет")
                streaming.assert_not_called()
                speak.assert_called_once_with(result)

    def test_voice_stop_cancels_before_waiting_for_main_queue(self):
        with patch.object(jarvis, "_audio_duration", return_value=0.2), \
                patch.object(jarvis, "transcribe_speech", return_value="Джарвис стоп"), \
                patch.object(jarvis._state, "is_speaking", False), \
                patch.object(jarvis._state, "speaking_cooldown_until", 0), \
                patch.object(jarvis, "command_queue", queue.Queue()):
            jarvis.callback(None, object())
            self.assertTrue(jarvis._state.interrupt_event.is_set())
            self.assertEqual(jarvis.command_queue.get_nowait(), "__CANCEL__")

    def test_ui_stop_cancels_before_waiting_for_main_queue(self):
        with patch.object(jarvis, "command_queue", queue.Queue()):
            jarvis.JarvisApi().send_command("стоп")
            self.assertTrue(jarvis._state.interrupt_event.is_set())
            self.assertEqual(jarvis.command_queue.get_nowait(), "__CANCEL__")

    def test_real_requests_remain_enabled(self):
        for phrase in ("открой браузер", "включи музыку", "можешь открыть браузер?",
                       "какая погода в Москве", "покажи мои задачи"):
            with self.subTest(phrase=phrase):
                self.assertFalse(is_action_discussion(phrase))
        self.assertEqual(jarvis.detect_intent_from_text("открой браузер"), "[OPEN:browser]")

    def test_interrupted_generation_never_executes_tags_or_fallback_speech(self):
        def deltas(*args, **kwargs):
            yield "[LOCK]"
            jarvis._state.interrupt_event.set()
        with patch.object(jarvis, "_build_messages", return_value=[]), \
                patch.object(jarvis, "_classify_complexity", return_value=("local", [])), \
                patch.object(jarvis, "_llm_deltas", side_effect=deltas), \
                patch.object(jarvis, "lock_pc") as lock, patch.object(jarvis, "speak") as speak:
            self.assertIn("прервано", jarvis.process_with_llm_streaming("заблокируй компьютер"))
            lock.assert_not_called()
            speak.assert_not_called()

    def test_timers_bind_a_fresh_notification_not_the_old_answer(self):
        with patch.object(jarvis, "set_timer") as timer, \
                patch.object(jarvis, "speak_notification") as notification, \
                patch.object(jarvis, "set_volume", return_value=False):
            jarvis.parse_and_execute_tags("[TIMER:60:test]")
            self.assertIs(timer.call_args.kwargs["speak_fn"], notification)
            jarvis.handle_local_productivity_command("таймер на 1 минуту")
            self.assertIs(timer.call_args.kwargs["speak_fn"], notification)
            result = jarvis.handle_local_feature_command("режим фокуса")
            self.assertIs(timer.call_args.kwargs["speak_fn"], notification)
            self.assertIn("звук выключить не удалось", result)


class ToolOutcomeTests(unittest.TestCase):
    def test_volume_and_media_propagate_failure(self):
        with patch.object(jarvis_tools._plat, "set_master_volume", return_value=(False, "unavailable")), \
                patch.object(jarvis_tools._plat, "press_media_key", return_value=(False, "unavailable")), \
                patch.object(jarvis_tools, "get_volume", return_value=50):
            self.assertFalse(jarvis_tools.set_volume(30))
            self.assertFalse(jarvis_tools.media_control("next"))
            self.assertEqual(jarvis_tools.nudge_volume(10), -1)

    def test_type_failure_restores_clipboard(self):
        with patch.object(jarvis_tools.pyperclip, "paste", return_value="original"), \
                patch.object(jarvis_tools.pyperclip, "copy") as copy, \
                patch.object(jarvis_tools._plat, "paste_from_clipboard", return_value=(False, "no UI")), \
                patch.object(jarvis_tools.time, "sleep"):
            self.assertFalse(jarvis_tools.type_text("test"))
            self.assertEqual(copy.call_args_list, [call("test"), call("original")])

    def test_python_real_error_and_success(self):
        event = threading.Event()
        result = jarvis_tools.execute_python_code("raise ValueError('audit-boom')", cancel_event=event)
        self.assertIn("ValueError: audit-boom", result)
        self.assertNotIn("выполнена", result)
        self.assertIn("выполнена", jarvis_tools.execute_python_code("x=1+1", cancel_event=event))

    def test_python_timeout_is_bounded(self):
        result = jarvis_tools.execute_python_code("while True: pass", timeout=0.2,
                                                 cancel_event=threading.Event())
        self.assertIn("остановлен", result)

    def test_python_pre_cancel_does_not_start_process(self):
        event = threading.Event()
        event.set()
        with patch.object(jarvis_tools.subprocess, "Popen") as start:
            self.assertIn("прервано", jarvis_tools.execute_python_code("x=1", cancel_event=event))
            start.assert_not_called()

    def test_python_large_input_does_not_use_a_blocking_pipe(self):
        original = jarvis_tools.subprocess.Popen
        def observed_start(*args, **kwargs):
            self.assertNotEqual(kwargs["stdin"], jarvis_tools.subprocess.PIPE)
            return original(*args, **kwargs)
        with patch.object(jarvis_tools.subprocess, "Popen", side_effect=observed_start):
            result = jarvis_tools.execute_python_code("#" + "x" * 100000 + "\nwhile True: pass",
                                                     timeout=0.2, cancel_event=threading.Event())
        self.assertIn("остановлен", result)

    def test_microphone_dependency_is_declared_and_checked(self):
        root = Path(__file__).parent
        self.assertIn("PyAudio>=0.2.14", (root / "requirements.txt").read_text(encoding="utf-8"))
        self.assertIn('"pyaudio": "PyAudio"', (root / "health_check.py").read_text(encoding="utf-8"))


class MainRouteTests(unittest.TestCase):
    def test_discussion_guard_precedes_all_quick_routes(self):
        class Mic:
            def __init__(self, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass

        class Recognizer:
            energy_threshold = 300
            def adjust_for_ambient_noise(self, *args, **kwargs): pass
            def listen_in_background(self, *args, **kwargs): return lambda **kwargs: None

        class NoThread:
            def __init__(self, *args, **kwargs): pass
            def start(self): pass

        for phrase in ("если я скажу открой браузер, ты сможешь?",
                       "что значит заблокировать компьютер?", "не открывай браузер",
                       "не снимай скриншот", "как сделать скриншот?", "почему громкость 80?",
                       "открой калькулятор и браузер"):
            with self.subTest(phrase=phrase), ExitStack() as stack:
                commands = queue.Queue()
                commands.put(phrase)
                commands.put("выход")
                for name in ("start_overlay", "stop_overlay", "prewarm_tts_cache", "ui_call", "ui_state", "speak"):
                    stack.enter_context(patch.object(jarvis, name))
                stack.enter_context(patch.object(jarvis, "command_queue", commands))
                stack.enter_context(patch.object(jarvis, "_select_mic", return_value=None))
                stack.enter_context(patch.object(jarvis, "get_obsidian_memory", return_value=""))
                opening = stack.enter_context(patch.object(jarvis, "execute_system_command"))
                lock = stack.enter_context(patch.object(jarvis, "lock_pc"))
                screenshot = stack.enter_context(patch.object(jarvis, "take_screenshot"))
                volume = stack.enter_context(patch.object(jarvis, "set_volume"))
                llm = stack.enter_context(patch.object(jarvis, "process_with_llm_streaming", return_value="explanation"))
                stack.enter_context(patch.object(jarvis, "sr", types.SimpleNamespace(Recognizer=Recognizer, Microphone=Mic)))
                stack.enter_context(patch.object(jarvis.pygame.mixer, "init"))
                stack.enter_context(patch.object(jarvis.pygame.mixer, "quit"))
                stack.enter_context(patch.object(jarvis.threading, "Thread", NoThread))
                stack.enter_context(patch.object(jarvis.time, "sleep"))
                stack.enter_context(patch.object(jarvis._feat, "start_reminder_worker"))
                stack.enter_context(patch.object(jarvis._feat, "arm_hotkey_listen"))
                stack.enter_context(patch.object(jarvis._ui, "_ui_window", object()))
                stack.enter_context(patch.object(jarvis, "telegram_confirm_pending", return_value=None))
                stack.enter_context(patch.object(jarvis, "email_confirm_pending", return_value=None))
                jarvis._stop_event.clear()
                jarvis.run_assistant()
                opening.assert_not_called()
                lock.assert_not_called()
                screenshot.assert_not_called()
                volume.assert_not_called()
                llm.assert_called_once_with(phrase)
        jarvis._state.interrupt_event.clear()


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)

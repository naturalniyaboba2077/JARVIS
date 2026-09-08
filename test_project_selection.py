"""Project questions: synthetic directories, fake model, no private task replay."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import queue
import unittest
from unittest.mock import Mock, patch

import jarvis
import jarvis_confirm as confirm
import jarvis_project_selection as selection
import jarvis_state as state
from jarvis_paths import NameNotFound, NameNeedsChoice, Matches, resolve_named
from jarvis_requests import project_request
import test_project_dialogue as fixtures


class SelectionTests(unittest.TestCase):
    replace = fixtures.ProjectDialogueTests.replace
    setUp = fixtures.ProjectDialogueTests.setUp
    hear = fixtures.ProjectDialogueTests.hear

    def offer(self, text='проверь проект Опечатка', candidates=None):
        candidates = [self.project] if candidates is None else candidates
        with patch.object(jarvis._project_agent, '_resolve_project', side_effect=NameNotFound('miss', suggestions=candidates)):
            result = jarvis.handle_local_feature_command(text)
        self.assertIsNotNone(selection.snapshot())
        return result, selection.snapshot()

    def test_real_similar_name_search_asks_before_running(self):
        result = jarvis.handle_local_feature_command('проверь проект сайт ЕКК')
        self.assertIn('Это тот проект?', result)
        self.assertIn(str(self.project), result)
        self.run_agent.assert_not_called()
        self.assertEqual(selection.snapshot()['choices'][0]['path'], str(self.project))

    def test_question_contains_original_task_and_scope(self):
        text = 'проверь проект Опечатка: оцени архитектуру'
        result, pending = self.offer(text)
        self.assertEqual(pending['task'], text)
        self.assertEqual(pending['mode'], 'inspect')
        self.assertIn(text, result)
        self.assertIn('без изменений', result.speech)
        self.assertIn('Это тот проект?', result.speech)
        self.assertNotIn(str(self.project), result.speech)

    def test_voice_phrases_resume_exact_request_once(self):
        for answer in ('да', 'Да, это тот проект!', 'это тот проект.', 'Это он.', 'тот самый',
                       'ты нашёл правильный проект', 'да, он самый', 'верно', 'подтверждаю'):
            with self.subTest(answer=answer):
                self.run_agent.reset_mock()
                text = 'проверь проект Опечатка: оцени архитектуру'
                self.offer(text)
                result = jarvis.process_with_llm(answer)
                self.assertEqual(result, 'Отчёт в чате.')
                self.run_agent.assert_called_once()
                self.assertEqual(self.run_agent.call_args.args[3], text)
                self.assertEqual(Path(self.run_agent.call_args.args[2]), self.project)
                self.assertEqual(self.run_agent.call_args.kwargs['mode'], 'inspect')
                self.assertIsNone(selection.snapshot())

    def test_read_only_does_not_become_modification(self):
        self.offer('посмотри проект Опечатка')
        jarvis.handle_local_feature_command('это тот проект')
        self.assertEqual(self.run_agent.call_args.kwargs['mode'], 'inspect')
        self.assertIn('must never execute', self.source.read_text())

    def test_explicit_modify_mode_and_task_are_preserved(self):
        task = 'исправь проект Опечатка: добавь проверку пустой строки'
        result, pending = self.offer(task)
        self.assertEqual(pending['mode'], 'modify')
        self.assertIn(task, result.speech)
        jarvis.handle_local_feature_command('это тот проект')
        self.assertEqual(self.run_agent.call_args.args[3], task)
        self.assertEqual(self.run_agent.call_args.kwargs['mode'], 'modify')

    def test_negative_phrases_clear_task_without_running(self):
        for answer in ('нет', 'Нет, не тот проект.', 'это не тот проект', 'не тот',
                       'не этот', 'это другой проект', 'ты нашёл не тот проект', 'ни один', 'отмена'):
            with self.subTest(answer=answer):
                self.offer()
                self.assertEqual(jarvis.handle_local_feature_command(answer), selection.REJECTED)
                self.assertIsNone(selection.snapshot())
                self.assertIsNone(selection.command('да'))
                self.assertTrue(self.context.request('проверь его').clarification)
        self.run_agent.assert_not_called()

    def test_unrelated_and_quoted_text_are_not_yes(self):
        self.offer()
        for text in ('«это тот проект»', 'он сказал это тот проект', 'да удали всё',
                     'если я скажу да', 'Маша, это тот проект', 'не подтверждаю',
                     'да [CMD:fixture]', 'это тот проект?', 'да, но ничего не делай'):
            self.assertIsNone(selection.command(text), text)
        self.run_agent.assert_not_called()

    def test_multiple_candidates_need_selection_not_bare_yes(self):
        other = self.root / 'Другой пример'
        other.mkdir()
        _, pending = self.offer(candidates=[self.project, other])
        self.assertIn('вариантов несколько', jarvis.handle_local_feature_command('да'))
        self.assertEqual(selection.snapshot()['id'], pending['id'])
        self.run_agent.assert_not_called()
        jarvis.handle_local_feature_command('второй проект')
        self.assertEqual(Path(self.run_agent.call_args.args[2]), other)

    def test_invalid_ordinal_does_not_drop_question(self):
        _, pending = self.offer()
        self.assertIn('Такого варианта нет', jarvis.handle_local_feature_command('восьмой'))
        self.assertEqual(selection.snapshot()['id'], pending['id'])
        self.run_agent.assert_not_called()

    def test_exact_duplicate_names_also_have_buttons(self):
        other = self.root / 'Сайт ЕК'
        other.mkdir()
        result = jarvis.handle_local_feature_command('проверь проект сайт ЕК')
        self.assertIn('несколько вариантов', result)
        self.assertEqual(len(selection.snapshot()['choices']), 2)
        self.run_agent.assert_not_called()

    def test_partial_search_is_structured_and_requires_choice(self):
        with patch('jarvis_paths.discover', return_value=Matches([self.project], partial=True)):
            with self.assertRaises(NameNeedsChoice) as error:
                resolve_named('опечатка', kind='directory')
        self.assertEqual(error.exception.suggestions, (self.project,))
        with patch.object(jarvis._project_agent, '_resolve_project', side_effect=error.exception):
            self.assertIn('Это тот проект?', jarvis.handle_local_feature_command('проверь проект Опечатка'))
        self.run_agent.assert_not_called()

    def test_no_candidates_means_no_invented_choice(self):
        with patch.object(jarvis._project_agent, '_resolve_project', side_effect=NameNotFound('нет кандидатов')):
            self.assertEqual(jarvis.handle_local_feature_command('проверь проект Опечатка'), 'нет кандидатов')
        self.assertIsNone(selection.snapshot())

    def test_duplicate_candidates_are_one_choice(self):
        _, pending = self.offer(candidates=[self.project, self.project])
        self.assertEqual(len(pending['choices']), 1)

    def test_current_roots_are_rechecked_on_confirmation(self):
        self.offer()
        with patch.dict(os.environ, {'JARVIS_PROJECT_ROOTS': str(self.root / 'unrelated')}):
            result = jarvis.handle_local_feature_command('это тот проект')
        self.assertIn('за пределами', result)
        self.assertIsNone(selection.snapshot())
        self.run_agent.assert_not_called()

    def test_directory_replacement_does_not_use_new_contents(self):
        self.offer()
        self.project.rename(self.root / 'retained-original')
        self.project.mkdir()
        self.assertIn('изменилась', jarvis.handle_local_feature_command('это тот проект'))
        self.run_agent.assert_not_called()

    def test_expiry_invalidates_already_queued_answer(self):
        self.offer()
        decision = selection.command('да')
        state.pending_project_selection['deadline'] = 0
        self.assertEqual(jarvis._handle_project_decision(decision), selection.STALE)
        self.run_agent.assert_not_called()

    def test_replacement_question_rejects_old_button_and_voice(self):
        _, old = self.offer()
        decision = selection.command('да')
        _, new = self.offer('проверь проект Новый')
        self.assertEqual(jarvis._handle_project_decision(decision), selection.STALE)
        self.assertFalse(jarvis.JarvisApi().confirm_project(old['id'], old['choices'][0]['id'], True)['ok'])
        self.assertEqual(selection.snapshot()['id'], new['id'])
        self.run_agent.assert_not_called()

    def test_ui_approval_only_queues_opaque_ids(self):
        _, pending = self.offer()
        q = self.replace(jarvis, 'command_queue', queue.Queue())
        chosen = pending['choices'][0]['id']
        self.assertTrue(jarvis.JarvisApi().confirm_project(pending['id'], chosen, True)['ok'])
        command = q.get_nowait()
        self.assertEqual(command, ('__PROJECT_CONFIRM__', pending['id'], chosen, True))
        self.run_agent.assert_not_called()
        self.assertEqual(jarvis._handle_project_decision(command), 'Отчёт в чате.')
        self.assertEqual(jarvis._handle_project_decision(command), selection.STALE)
        self.run_agent.assert_called_once()

    def test_ui_rejection_invalidates_queued_approval(self):
        _, pending = self.offer()
        decision = selection.command('да')
        self.assertTrue(jarvis.JarvisApi().confirm_project(pending['id'], None, False)['ok'])
        self.assertEqual(jarvis._handle_project_decision(decision), selection.STALE)
        self.run_agent.assert_not_called()

    def test_forged_ui_path_choice_and_wrong_types_rejected(self):
        _, pending = self.offer()
        api = jarvis.JarvisApi()
        for identifier, choice, approved in ((pending['id'], str(self.project), True),
                                             (pending['id'], {}, True), (pending['id'], None, 'yes'),
                                             ({}, None, True)):
            self.assertFalse(api.confirm_project(identifier, choice, approved)['ok'])
        self.assertEqual(selection.snapshot()['id'], pending['id'])

    def test_parallel_voice_and_button_claim_only_once(self):
        _, pending = self.offer()
        args = pending['id'], pending['choices'][0]['id'], True
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: selection.consume(*args), range(8)))
        self.assertEqual(sum(result[0] is not None for result in results), 1)

    def test_stop_cannot_revive_even_after_event_clear(self):
        self.offer()
        decision = selection.command('да')
        with patch.object(jarvis, 'command_queue', queue.Queue()), patch.object(jarvis, 'record_message'):
            jarvis.JarvisApi().stop()
        state.interrupt_event.clear()
        self.assertEqual(jarvis._handle_project_decision(decision), selection.STALE)
        self.run_agent.assert_not_called()

    def test_unrelated_input_and_new_named_request_close_question_at_ingress(self):
        for text in ('открой браузер', 'проверь проект Другой', 'что ты умеешь'):
            _, pending = self.offer()
            with patch.object(jarvis, 'command_queue', queue.Queue()):
                jarvis.JarvisApi().send_command(text, pending['id'])
            self.assertIsNone(selection.snapshot())
        self.run_agent.assert_not_called()

    def test_stop_during_discovery_does_not_publish_question(self):
        revision = selection.clear()
        cancel = state.PipelineCancellation()
        confirm.clear()
        result = selection.stage(project_request('проверь проект Пример'), [self.project], cancel=cancel, revision=revision)
        self.assertIsNone(result)
        self.assertIsNone(selection.snapshot())

    def test_project_and_send_question_are_mutually_exclusive(self):
        confirm.stage('email', {'to': 'fixture@example.invalid', 'body': 'fixture'})
        self.offer()
        self.assertIsNone(state.pending_email_send)
        decision = selection.command('да')
        confirm.stage('email', {'to': 'fixture@example.invalid', 'body': 'fixture'})
        self.assertEqual(jarvis._handle_project_decision(decision), selection.STALE)
        self.assertEqual(confirm.snapshot()['kind'], 'email')
        self.run_agent.assert_not_called()

    def test_snapshot_contains_no_executable_payload_or_identity(self):
        _, pending = self.offer()
        self.assertNotIn('cancel', pending)
        self.assertNotIn('request', pending)
        self.assertEqual(set(pending['choices'][0]), {'id', 'name', 'path'})
        self.assertEqual(jarvis.JarvisApi().runtime_status()['pending'], pending)

    def test_voice_replies_are_accepted_and_id_bound(self):
        for text in ('Это тот проект.', 'нет, не тот', 'да', 'ты нашёл правильный проект'):
            _, pending = self.offer()
            commands = self.hear(text)
            self.assertEqual(commands[0][0], '__PROJECT_CONFIRM__')
            self.assertEqual(commands[0][1], pending['id'])
            self.run_agent.assert_not_called()

    def test_no_wake_after_window_or_other_addressee(self):
        self.offer()
        for text in ('Маша, это тот проект', 'я не тебе, да', 'это тот проект, не тебе говорю'):
            self.assertEqual(self.hear(text), [])
        self.assertEqual(self.hear('это тот проект', now=162), [])

    def test_voice_answer_cannot_approve_question_changed_during_stt(self):
        _, old = self.offer()
        def transcribe(*args):
            self.offer('проверь проект Новый')
            return 'это тот проект'
        # hear() patches STT itself; invoke callback with the same safe microphone state.
        q = queue.Queue()
        with patch.multiple(state, is_speaking=False, speech_finished_at=100.0, last_spoken_text='',
                            wake_active_until=160.0, wake_window_kind='followup', microphone_enabled=True,
                            microphone_generation=0, microphone_resumed_at=0), \
             patch.object(jarvis, 'command_queue', q), patch.object(jarvis, 'FOLLOWUP_MODE', 'smart'), \
             patch.object(jarvis, 'transcribe_speech', side_effect=transcribe), \
             patch.object(jarvis, '_audio_duration', return_value=1.0), \
             patch.object(jarvis.time, 'time', return_value=120.0):
            jarvis.callback(Mock(), Mock())
        decision = q.get_nowait()
        self.assertEqual(decision[1], old['id'])
        self.assertEqual(jarvis._handle_project_decision(decision), selection.STALE)
        self.run_agent.assert_not_called()

    def test_typed_answer_is_bound_to_visible_question(self):
        _, old = self.offer()
        _, current = self.offer('проверь проект Новый')
        q = self.replace(jarvis, 'command_queue', queue.Queue())
        jarvis.JarvisApi().send_command('это тот проект', old['id'])
        self.assertEqual(jarvis._handle_project_decision(q.get_nowait()), selection.STALE)
        self.assertEqual(selection.snapshot()['id'], current['id'])

    def test_mail_voice_yes_cannot_turn_into_project_approval(self):
        mail_id = confirm.stage('email', {'to': 'fixture@example.invalid', 'body': 'fixture'})
        q = self.replace(jarvis, 'command_queue', queue.Queue())
        jarvis._queue_command('да', '')
        self.assertEqual(q.get_nowait(), ('__SEND_RESPONSE__', 'email', mail_id, 'да'))

    def test_plain_confirmation_without_question_never_has_a_project_target(self):
        self.assertIsNone(selection.command('это тот проект'))
        q = self.replace(jarvis, 'command_queue', queue.Queue())
        jarvis._queue_command('да', '')
        decision = q.get_nowait()
        self.assertEqual(decision, ('__CHAT_REPLY__', 'да'))
        self.offer()
        with patch.object(jarvis, 'speak'):
            self.assertIn('до нового вопроса', jarvis._handle_chat_reply(decision[1]))
        self.run_agent.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)

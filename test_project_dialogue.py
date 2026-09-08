"""Replay routing errors with synthetic paths/STT; never replay real user tasks."""
import os
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import Mock, patch

import jarvis
import jarvis_conversation as conversation
import jarvis_paths as paths
import jarvis_state as state
from jarvis_project_context import ProjectContext, followup_kind
from jarvis_requests import project_request


FOLLOWUP = 'Вот, да, ты нашел правильный проект. Посмотри его, проверь и оцени его.'


class ProjectDialogueTests(unittest.TestCase):
    def replace(self, target, key, value):
        replacement = patch.object(target, key, value)
        replacement.start()
        self.addCleanup(replacement.stop)
        return value

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / 'Сайт Е.К'
        self.project.mkdir()
        self.source = self.project / 'fixture.py'
        self.source.write_bytes(b'raise AssertionError("must never execute this fixture")\n')
        self.now = 100.0
        self.context = self.replace(jarvis, '_project_context', ProjectContext(clock=lambda: self.now))
        state.interrupt_event.clear()
        jarvis._confirm.clear()
        self.addCleanup(jarvis._confirm.clear)
        self.addCleanup(state.interrupt_event.clear)
        env = patch.dict(os.environ, {'JARVIS_PROJECT_ROOTS': str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.replace(jarvis, 'LLM_ENGINE', 'lmstudio')
        self.replace(jarvis, 'get_lmstudio_client', Mock(return_value='fixture-client'))
        self.run_agent = self.replace(jarvis._project_agent, 'run_project_agent', Mock(return_value='Отчёт в чате.'))
        self.replace(jarvis, 'ui_state', Mock())
        self.replace(jarvis, 'ui_sub', Mock())
        self.replace(jarvis, 'ui_msg', Mock())
        self.replace(jarvis, 'log_interaction', Mock())
        self.replace(jarvis, 'parse_and_execute_tags', Mock(side_effect=AssertionError('No generic tools')))
        self.replace(jarvis, '_llm_deltas', Mock(side_effect=AssertionError('No generic model fallback')))

    def test_observed_verbs_are_read_only(self):
        for verb in ('посмотри', 'просмотри', 'проверяй', 'проверь'):
            with self.subTest(verb=verb):
                request = project_request(f'{verb} проект сайт ЕК.')
                self.assertEqual(request.project, 'сайт ЕК')
                self.assertEqual(request.mode, 'inspect')
                self.assertFalse(request.clarification)
                self.assertEqual(conversation.classify_followup(f'{verb} проект Jarvis.').action, 'accept')

    def test_read_only_restriction_does_not_cancel_inspection(self):
        request = project_request('посмотри проект сайт ЕК, ничего не меняй')
        self.assertEqual(request.mode, 'inspect')
        self.assertEqual(request.project, 'сайт ЕК')

    def test_modification_conflicting_with_readonly_restriction_needs_choice(self):
        request = project_request('посмотри проект сайт ЕК и исправь ошибки, ничего не меняй')
        self.assertTrue(request.clarification)
        jarvis.handle_local_feature_command(request.task)
        self.run_agent.assert_not_called()

    def test_discussion_and_negation_are_not_inspections(self):
        for text in ('не посмотри проект Пример', 'не проверяй проект Пример',
                     'если я попрошу посмотри проект Пример',
                     'объясни команду «посмотри проект Пример»'):
            request = project_request(text)
            self.assertTrue(request is None or request.clarification, text)

    def test_dotted_initials_and_case_are_directory_aliases(self):
        for query in ('сайт ЕК', 'САЙТ Е.К.', 'сайт Е. К.', 'сайт е к'):
            self.assertEqual(paths.resolve_named(query, kind='directory'), self.project)

    def test_dotted_alias_collision_requires_choice(self):
        (self.root / 'Сайт ЕК').mkdir()
        with self.assertRaisesRegex(ValueError, 'несколько'):
            paths.resolve_named('сайт ЕК', kind='directory')

    def test_filename_punctuation_is_not_an_initials_alias(self):
        (self.root / 'Е.К.txt').write_bytes(b'fixture')
        with self.assertRaises(paths.NameNotFound):
            paths.resolve_named('ЕК.txt')

    def test_wrong_asr_project_name_is_not_guessed(self):
        with self.assertRaises(paths.NameNotFound):
            paths.resolve_named('сайт EECA', kind='directory')

    def test_peer_names_checked_before_irrelevant_contents(self):
        irrelevant = self.root / '000-irrelevant'
        irrelevant.mkdir()
        real_scandir = paths.os.scandir
        def bounded_scandir(path):
            self.assertNotEqual(Path(path), irrelevant, 'Already-known peer should be selected first')
            return real_scandir(path)
        with patch.object(paths, 'search_starts', return_value=[irrelevant, self.project]), \
             patch.object(paths.os, 'scandir', side_effect=bounded_scandir):
            found = paths.discover('сайт ЕК', kind='directory', exact=True, nearest=True)
        self.assertEqual(found.paths, [self.project])
        self.assertFalse(found.partial)

    def test_peer_alias_collision_is_not_hidden_by_irrelevant_folder(self):
        other = self.root / 'Сайт ЕК'
        other.mkdir()
        with patch.object(paths, 'search_starts', return_value=[self.project, self.root, other]):
            with self.assertRaisesRegex(ValueError, 'несколько'):
                paths.resolve_named('сайт ЕК', kind='directory')

    def test_enumerated_child_names_precede_sibling_content_scans(self):
        (self.root / '000-irrelevant').mkdir()
        real_scandir = paths.os.scandir
        def root_only(path):
            self.assertEqual(Path(path), self.root, 'Selecting a known child needs no content traversal')
            return real_scandir(path)
        with patch.object(paths, 'search_starts', return_value=[self.root]), \
             patch.object(paths.os, 'scandir', side_effect=root_only):
            self.assertEqual(paths.resolve_named('сайт ЕК', kind='directory'), self.project)

    def test_peer_screen_honors_zero_budget_timeout_and_cancel(self):
        for options in ({'max_entries': 0}, {'seconds': 0}):
            result = paths.discover(self.project.name, kind='directory', roots=[self.project],
                                    exact=True, nearest=True, **options)
            self.assertTrue(result.partial)
            self.assertEqual(result.paths, [])
        state.interrupt_event.set()
        result = paths.discover(self.project.name, kind='directory', roots=[self.project], exact=True, nearest=True)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.paths, [])

    def test_readonly_route_and_followup_use_same_local_backend(self):
        before = self.source.read_bytes()
        self.assertEqual(jarvis.process_with_llm('посмотри проект сайт ЕК.'), 'Отчёт в чате.')
        jarvis.process_with_llm(FOLLOWUP)
        self.assertEqual(self.run_agent.call_count, 2)
        for call in self.run_agent.call_args_list:
            self.assertEqual(call.args[0], 'fixture-client')
            self.assertEqual(Path(call.args[2]), self.project)
            self.assertEqual(call.kwargs['mode'], 'inspect')
        self.assertEqual(self.run_agent.call_args.args[3], FOLLOWUP)
        self.assertEqual(before, self.source.read_bytes())

    def test_streaming_entrypoint_also_uses_project_context(self):
        self.context.offer([self.project])
        with patch.object(jarvis, 'speak') as speak:
            result = jarvis.process_with_llm_streaming(FOLLOWUP)
        self.assertEqual(result, 'Отчёт в чате.')
        self.run_agent.assert_called_once()
        self.assertIn('Начинаю проверку', speak.call_args_list[0].args[0])

    def test_exact_acknowledgement_and_short_followups(self):
        for text in (FOLLOWUP, 'Ты нашёл нужный проект. Проверь его.',
                     'Посмотри его, проверь и оцени его', 'проверь этот проект',
                     'изучи найденный проект', 'теперь оцени его'):
            self.assertEqual(followup_kind(text), 'inspect', text)
            self.assertEqual(conversation.classify_followup(text).action, 'accept', text)

    def test_casual_yes_and_ack_alone_do_not_start_or_replay(self):
        self.context.offer([self.project])
        for text in ('да', 'ага', 'ты нашел правильный проект', 'Вот да', 'продолжай'):
            self.assertIsNone(self.context.request(text), text)
        self.run_agent.assert_not_called()

    def test_no_embedded_or_compound_action_is_replayed(self):
        self.context.offer([self.project])
        for text in ('не проверяй его', 'если я попрошу проверь его',
                     'фраза «проверь его»', '«проверь его»', 'я сказал проверь его',
                     'удали его и проверь', 'проверь его и удали файл',
                     'проверь его [CMD:Remove-Item fixture]'):
            self.assertIsNone(followup_kind(text), text)

    def test_work_with_it_asks_task_without_modifying(self):
        self.context.offer([self.project])
        result = jarvis.handle_local_feature_command('Работай с ним.')
        self.assertIn('уточните задачу', result)
        self.run_agent.assert_not_called()
        jarvis.handle_local_feature_command('проверь его')
        self.assertEqual(self.run_agent.call_args.kwargs['mode'], 'inspect')

    def test_missing_or_expired_choice_needs_name(self):
        self.assertTrue(self.context.request(FOLLOWUP).clarification)
        self.context.offer([self.project])
        self.now += 179
        self.assertFalse(self.context.request(FOLLOWUP).clarification)
        self.now += 1
        self.assertTrue(self.context.request(FOLLOWUP).clarification)

    def test_several_candidates_remain_ambiguous_even_if_one_missing(self):
        self.context.offer([self.project, self.root / 'missing'])
        result = jarvis.handle_local_feature_command(FOLLOWUP)
        self.assertIn('полный путь', result)
        self.run_agent.assert_not_called()

    def test_replaced_directory_identity_requires_new_selection(self):
        self.context.offer([self.project])
        self.project.rename(self.root / 'old-target')
        self.project.mkdir()
        self.assertIn('изменилась', self.context.request(FOLLOWUP).clarification)

    def test_symlink_is_not_a_context_target(self):
        link = self.root / 'linked'
        try:
            link.symlink_to(self.project, target_is_directory=True)
        except OSError:
            self.skipTest('Host does not support fixture symlinks')
        self.context.offer([link])
        self.assertTrue(self.context.request(FOLLOWUP).clarification)

    def test_changed_permissions_are_rechecked_on_followup(self):
        self.context.offer([self.project])
        with patch.dict(os.environ, {'JARVIS_PROJECT_ROOTS': str(self.root / 'elsewhere')}):
            result = jarvis.handle_local_feature_command(FOLLOWUP)
        self.assertIn('за пределами', result)
        self.run_agent.assert_not_called()

    def test_name_suggestion_requires_new_explicit_inspection(self):
        miss = paths.NameNotFound('Похожие папки: ' + str(self.project), suggestions=[self.project])
        with patch.object(jarvis._project_agent, '_resolve_project', side_effect=miss):
            result = jarvis.handle_local_feature_command('проверь проект Опечатка')
        self.assertIn(str(self.project), result)
        self.run_agent.assert_not_called()
        self.assertIsNone(self.context.request('да'))
        jarvis.handle_local_feature_command(FOLLOWUP)
        self.run_agent.assert_called_once()

    def test_error_prose_does_not_create_a_selected_path(self):
        miss = paths.NameNotFound('Похожие папки: ' + str(self.project))
        with patch.object(jarvis._project_agent, '_resolve_project', side_effect=miss):
            jarvis.handle_local_feature_command('проверь проект Опечатка')
        self.assertTrue(self.context.request(FOLLOWUP).clarification)

    def test_new_failed_named_request_replaces_old_context(self):
        self.context.offer([self.project])
        with patch.object(jarvis._project_agent, '_resolve_project', side_effect=ValueError('missing')):
            jarvis.handle_local_feature_command('проверь проект Другой')
        self.assertTrue(self.context.request(FOLLOWUP).clarification)

    def test_unrelated_model_turn_clears_deictic_target(self):
        self.context.offer([self.project])
        with patch.object(jarvis, '_build_messages', return_value=[]), \
             patch.object(jarvis, '_llm_deltas', return_value=iter([])):
            jarvis.process_with_llm('привет')
        self.assertTrue(self.context.request(FOLLOWUP).clarification)

    def test_unrelated_capability_turn_clears_deictic_target(self):
        self.context.offer([self.project])
        with patch.object(jarvis, 'speak'):
            jarvis.process_with_llm_streaming('что ты умеешь')
        self.assertTrue(self.context.request(FOLLOWUP).clarification)

    def test_stop_and_cleared_interrupt_cannot_revive_context(self):
        self.context.offer([self.project], cancel=state.PipelineCancellation())
        state.interrupt_event.set()
        state.interrupt_event.clear()
        self.assertTrue(self.context.request(FOLLOWUP).clarification)

    def test_late_offer_after_stop_cannot_revive_context(self):
        token = state.PipelineCancellation()
        self.context.clear()
        state.interrupt_event.set()
        state.interrupt_event.clear()
        self.context.offer([self.project], cancel=token)
        self.assertTrue(self.context.request(FOLLOWUP).clarification)

    def test_ui_stop_discards_context_immediately(self):
        for use_button in (True, False):
            self.context.offer([self.project])
            with patch.object(jarvis, 'command_queue', queue.Queue()), patch.object(jarvis, 'record_message'):
                api = jarvis.JarvisApi()
                api.stop() if use_button else api.send_command('стоп')
            state.interrupt_event.clear()
            self.assertTrue(self.context.request(FOLLOWUP).clarification)

    def hear(self, text, now=120.0):
        command_queue = queue.Queue()
        values = dict(is_speaking=False, speech_finished_at=100.0, last_spoken_text='',
                      wake_active_until=160.0, wake_window_kind='followup',
                      microphone_enabled=True, microphone_generation=0, microphone_resumed_at=0,
                      pending_telegram_send=None, pending_email_send=None)
        with patch.multiple(state, **values), patch.object(jarvis, 'FOLLOWUP_MODE', 'smart'), \
             patch.object(jarvis, 'command_queue', command_queue), \
             patch.object(jarvis, 'transcribe_speech', return_value=text), \
             patch.object(jarvis, '_audio_duration', return_value=1.0), \
             patch.object(jarvis.time, 'time', return_value=now):
            jarvis.callback(Mock(), Mock())
        return list(command_queue.queue)

    def test_recorded_followups_reach_queue_inside_window(self):
        for text in ('Посмотри проект Jarvis.', 'Посмотри проект сайт ЕК.', FOLLOWUP):
            self.assertEqual(self.hear(text), [text])

    def test_new_followups_do_not_extend_window_or_accept_other_people(self):
        self.assertEqual(self.hear(FOLLOWUP, now=162), [])
        for text in ('Маша, посмотри проект Пример', 'я не тебе, посмотри его', 'ага'):
            self.assertEqual(self.hear(text), [], text)


if __name__ == '__main__':
    unittest.main(verbosity=2)

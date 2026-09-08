"""Synthetic microphone/address regressions; no audio, personal projects or models."""
from pathlib import Path
import queue
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import jarvis
import jarvis_config as config
import jarvis_conversation as conversation
import jarvis_paths as paths
import jarvis_state as state
import jarvis_stt as stt
import jarvis_tts as tts
import jarvis_ui as ui
from jarvis_requests import project_request


class ConversationTests(unittest.TestCase):
    def replace(self, module, name, value):
        p = patch.object(module, name, value)
        p.start(); self.addCleanup(p.stop)
        return value

    def setUp(self):
        state.interrupt_event.clear()
        self.addCleanup(state.interrupt_event.clear)
        for key, value in {
            'is_speaking': False, 'speech_finished_at': 100.0, 'speaking_cooldown_until': 105.0,
            'wake_active_until': 160.0, 'wake_window_kind': 'followup',
            'last_response_text': 'Проверка завершена.', 'last_user_command': 'проверь проект Пример',
            'last_spoken_text': '', 'microphone_enabled': True, 'microphone_ready': True,
            'microphone_generation': 0, 'microphone_resumed_at': 0.0,
            'pending_telegram_send': None, 'pending_email_send': None,
            'recognizer': None, 'threshold_before_speech': None,
        }.items():
            self.replace(state, key, value)
        self.replace(jarvis, 'FOLLOWUP_MODE', 'smart')
        self.replace(jarvis, 'FOLLOWUP_WINDOW', 60.0)
        self.queue = self.replace(jarvis, 'command_queue', queue.Queue())
        self.ui_msg = self.replace(jarvis, 'ui_msg', Mock())
        self.replace(jarvis, 'ui_state', Mock())

    def hear(self, text, now=102.0, duration=1.0):
        with patch.object(jarvis, 'transcribe_speech', return_value=text), \
             patch.object(jarvis, '_audio_duration', return_value=duration), \
             patch.object(jarvis.time, 'time', return_value=now):
            jarvis.callback(Mock(), Mock())
        return list(self.queue.queue)

    def test_direct_requests_and_contextual_questions(self):
        for text in ['проверь проект учет оборудования', 'открой браузер', 'напиши скрипт на python',
                     'а почему?', 'а подробнее', 'сколько это займёт?', 'ты можешь объяснить?',
                     'найди информацию в интернете', 'который час', 'да, открой']:
            with self.subTest(text=text):
                self.assertEqual(conversation.classify_followup(text, previous_reply='Ответ').action, 'accept')

    def test_other_people_and_background_are_ignored(self):
        for text in ['Маша, проверь проект', 'мам, что на ужин?', 'папа открой дверь',
                     'ребята сколько это стоит?', 'я не тебе, открой браузер', 'алло ты где?',
                     'Я вот делаю ему задачу.', 'мы потом пойдем в магазин', 'передай соль',
                     'спасибо за просмотр', 'Открываю браузер сэр выполняю команду', 'ага']:
            with self.subTest(text=text):
                self.assertEqual(conversation.classify_followup(text, previous_reply='Ответ').action, 'ignore')

    def test_ambiguous_actions_are_not_authorized(self):
        for text in ['удали это', 'отправь ему', 'закрой', 'измени там', 'Олег, открой браузер']:
            with self.subTest(text=text):
                self.assertEqual(conversation.classify_followup(text, previous_reply='Ответ').action, 'clarify')

    def test_question_without_conversation_needs_address(self):
        self.assertEqual(conversation.classify_followup('сколько стоит?').action, 'clarify')

    def test_explicit_window_accepts_fragment_but_not_other_person(self):
        self.assertEqual(conversation.classify_followup('погода завтра', explicit_window=True).action, 'accept')
        self.assertEqual(conversation.classify_followup('Маша проверь проект', explicit_window=True).action, 'ignore')

    def test_confirmation_requires_pending_action(self):
        self.assertEqual(conversation.classify_followup('Подтверждаю!', confirmation_pending=True).action, 'accept')
        self.assertEqual(conversation.classify_followup('да', confirmation_pending=False).action, 'ignore')

    def test_jarvis_observed_aliases_are_direct_addresses(self):
        for name in ['Джарвис', 'Jarvis', 'Жарвис', 'Джарвец', 'Джарез', 'жар весь']:
            with self.subTest(name=name):
                self.assertTrue(stt.is_direct_address(name + ', проверь проект Пример'))
                self.assertEqual(stt.strip_wake_word(name + ', проверь проект Пример'), 'проверь проект Пример')

    def test_removed_name_does_not_activate_even_through_fuzzy_matching(self):
        state.wake_active_until = 0
        for name in ['Чарльз', 'Charles', 'шарльз', 'шарлес', 'чарлес', 'Джарльз', 'Джарльес']:
            for phrase in [name, name + ', открой браузер', 'Привет ' + name, name + ' да']:
                with self.subTest(phrase=phrase):
                    self.assertFalse(stt.contains_wake_word(phrase))
                    self.assertFalse(stt.is_direct_address(phrase))
                    self.assertEqual(self.hear(phrase), [])

    def test_mentions_are_not_wake_addresses(self):
        for text in ['что такое Jarvis', 'расскажи про Чарльза', 'я говорю о Джарвисе',
                     'проверь проект Jarvis', 'как пользоваться Jarvis', 'проверь проект "C:\\Work\\Jarvis"',
                     'Сделай так, чтобы не нужно было говорить Джарвис, когда я к нему обращаюсь']:
            with self.subTest(text=text):
                self.assertFalse(stt.is_direct_address(text))

    def test_preserves_paths_quotes_and_colons(self):
        command = 'проверь проект "C:\\Work\\Sample": найди баги.'
        self.assertEqual(stt.strip_wake_word('Джарвис, ' + command), command)
        self.assertEqual(stt.strip_wake_word('открой браузер, Джарвис!'), 'открой браузер')
        self.assertEqual(stt.strip_wake_word(command), command)
        command = 'проверь проект "C:\\Work\\проект, Пример": найди баг.'
        self.assertEqual(stt.normalize_voice_command(command), command)
        self.assertTrue(stt.is_direct_address('проверь, Джарвис, проект Пример'))
        self.assertTrue(stt.is_direct_address('открой браузер, Джарвис'))

    def test_observed_filler_and_readonly_asr_repair(self):
        value = stt.normalize_voice_command(stt.strip_wake_word('и Жарвис. Проверь проект учет оборудование.'))
        self.assertEqual(project_request(value).project, 'учет оборудование')
        value = stt.normalize_voice_command(stt.strip_wake_word('Джарез, провер проект, учет оборудования.'))
        self.assertEqual(project_request(value).mode, 'inspect')
        self.assertEqual(stt.normalize_voice_command('поверь проект Пример'), 'проверь проект Пример')
        self.assertEqual(stt.normalize_voice_command('поверь мне'), 'поверь мне')
        self.assertEqual(stt.normalize_voice_command('исправ проект Пример'), 'исправ проект Пример')

    def test_under_name_is_not_part_of_folder_name(self):
        request = project_request('проверь проект под названием Учёт оборудования')
        self.assertEqual(request.project, 'Учёт оборудования')
        self.assertEqual(request.mode, 'inspect')
        self.assertEqual(project_request('проверь проект под названием «Пример»: найди баг').project, 'Пример')
        self.assertEqual(project_request('проверь проект «под названием Пример»').project, 'под названием Пример')

    def test_followup_at_59_seconds_is_accepted(self):
        self.assertEqual(self.hear('проверь проект Пример', now=160), ['проверь проект Пример'])

    def test_followup_after_60_seconds_requires_wake(self):
        self.assertEqual(self.hear('проверь проект Пример', now=162), [])

    def test_stt_completion_time_does_not_expire_captured_phrase(self):
        # Window evaluated at audio capture, not after slow transcription.
        with patch.object(jarvis, 'transcribe_speech', return_value='проверь проект Пример'), \
             patch.object(jarvis, '_audio_duration', return_value=1.0), \
             patch.object(jarvis.time, 'time', side_effect=[160.0] + [180.0] * 100):
            jarvis.callback(Mock(), Mock())
        self.assertEqual(list(self.queue.queue), ['проверь проект Пример'])

    def test_repeating_assistant_example_is_not_echo_after_playback(self):
        state.last_spoken_text = 'Скажите например проверь проект учет оборудования'
        self.assertEqual(self.hear('Проверь проект учет оборудования.'), ['Проверь проект учет оборудования.'])

    def test_actual_overlapping_echo_is_dropped(self):
        state.last_spoken_text = 'Проверь проект Пример'
        self.assertEqual(self.hear('Проверь проект Пример', now=100), [])

    def test_named_barge_in_still_interrupts(self):
        state.is_speaking = True
        state.last_spoken_text = 'Совсем другой ответ'
        self.assertEqual(self.hear('Джарвис, стоп'), ['__CANCEL__'])
        self.assertTrue(state.interrupt_event.is_set())

    def test_other_person_stop_does_not_interrupt(self):
        self.assertEqual(self.hear('Маша, стоп'), [])
        self.assertFalse(state.interrupt_event.is_set())

    def test_ambiguous_command_queues_only_clarification(self):
        self.assertEqual(self.hear('удали это'), [('__ADDRESS_CLARIFY__',)])
        self.assertEqual(state.wake_active_until, 0)
        self.assertEqual(self.ui_msg.call_args.args[1], 'удали это')

    def test_bare_yes_after_clarification_does_not_replay(self):
        state.last_response_text = 'Это мне? Повторите поручение с обращением.'
        self.assertEqual(self.hear('да'), [])

    def test_wake_only_creates_explicit_window(self):
        state.wake_active_until = 0
        self.assertEqual(self.hear('Джарвис'), ['__WAKE__'])
        self.assertEqual(state.wake_window_kind, 'address')
        self.assertGreater(state.wake_active_until, 102)

    def test_wake_mention_alone_does_not_activate(self):
        state.wake_active_until = 0
        self.assertEqual(self.hear('я говорю о Джарвисе'), [])

    def test_other_person_not_written_to_dialogue_archive(self):
        self.hear('Маша, проверь проект')
        self.ui_msg.assert_not_called()
        self.assertEqual(state.wake_active_until, 160)

    def test_muted_microphone_never_accepts_followup(self):
        state.microphone_enabled = False
        self.assertEqual(self.hear('проверь проект Пример'), [])

    def test_tts_finish_opens_full_60_seconds(self):
        with patch.object(tts, 'FOLLOWUP_WINDOW', 60), patch.object(tts, 'FOLLOWUP_MODE', 'smart'), \
             patch.object(tts.time, 'time', return_value=200), patch.object(tts, 'ui_state'), patch.object(tts, 'ui_sub'):
            tts._set_done_speaking()
        self.assertEqual(state.wake_active_until, 260)
        self.assertEqual(state.speech_finished_at, 200)
        self.assertEqual(state.wake_window_kind, 'followup')

    def test_notifications_neither_open_nor_extend_window(self):
        for deadline in [0.0, 160.0]:
            state.wake_active_until = deadline
            with patch.object(tts, 'speak', side_effect=lambda *a, **kw: tts._set_done_speaking()), \
                 patch.object(tts, 'ui_state'), patch.object(tts, 'ui_sub'):
                tts.speak_notification('Синтетический таймер')
            self.assertEqual(state.wake_active_until, deadline)
            self.assertFalse(getattr(tts._speech_context, 'notification', False))

    def test_notification_flag_restored_on_exception(self):
        with patch.object(tts, 'speak', side_effect=RuntimeError('fixture')):
            with self.assertRaises(RuntimeError):
                tts.speak_notification('fixture')
        self.assertFalse(getattr(tts._speech_context, 'notification', False))

    def test_off_does_not_open_followup(self):
        with patch.object(tts, 'FOLLOWUP_MODE', 'off'), patch.object(tts, 'ui_state'), patch.object(tts, 'ui_sub'):
            tts._set_done_speaking()
        self.assertEqual(state.wake_active_until, 0)

    def test_setting_parser_accepts_only_explicit_bounded_request(self):
        for text, expected in [
            ('слушай без обращения 60 секунд', 60), ('принимай команды без имени минуту', 60),
            ('отключи продолжение разговора', 0), ('настрой продолжение разговора на 30 секунд', 30),
            ('слушай без обращения 500 секунд', -1), ('как включить команды без обращения?', None),
            ('не включай продолжение разговора', None), ('если я попрошу слушать без обращения 60 секунд', None),
            ('сделай так, чтобы не нужно было каждый раз обращаться, допустим я дал команду', -1),
        ]:
            with self.subTest(text=text):
                self.assertEqual(conversation.followup_setting_request(text), expected)

    def test_setting_route_does_not_need_model_or_generic_action_tags(self):
        self.replace(config, 'FOLLOWUP_MODE', 'smart'); self.replace(config, 'FOLLOWUP_WINDOW', 60.0)
        self.replace(tts, 'FOLLOWUP_MODE', 'smart'); self.replace(tts, 'FOLLOWUP_WINDOW', 60.0)
        with patch.object(jarvis._settings, 'save', return_value={'ok': True, 'message': 'saved'}) as save, \
             patch.object(jarvis, 'parse_and_execute_tags', side_effect=AssertionError('no tags')):
            result = jarvis.process_with_llm('сделай так, чтобы не нужно было обращаться 60 секунд после ответа')
        self.assertIn('60 секунд', result)
        save.assert_called_once_with({'JARVIS_FOLLOWUP_MODE': 'smart', 'JARVIS_FOLLOWUP_WINDOW': '60'})

    def test_garbled_setting_request_asks_duration_instead_of_refusal(self):
        with patch.object(jarvis._settings, 'save') as save:
            result = jarvis.handle_local_feature_command('сделай так чтобы мне не нужно было каждый раз поговорить у Джарвис тогда я к нему обращаюсь допустим я дал команду')
        self.assertIn('На сколько секунд', result)
        save.assert_not_called()

    def test_failed_config_save_does_not_change_live_settings(self):
        with patch.object(jarvis._settings, 'save', return_value={'ok': False, 'message': 'fixture'}):
            self.assertIn('Не сохранил', jarvis.handle_followup_setting('слушай без обращения 30 секунд'))
        self.assertEqual(jarvis.FOLLOWUP_WINDOW, 60)

    def test_runtime_status_reports_window_and_honors_mute(self):
        with patch.object(jarvis.time, 'time', return_value=101):
            self.assertEqual(jarvis.JarvisApi().runtime_status()['followup']['remaining_seconds'], 59)
            state.microphone_enabled = False
            self.assertEqual(jarvis.JarvisApi().runtime_status()['followup']['remaining_seconds'], 0)

    def test_real_phase_survives_reconnect_and_clears_on_state_transition(self):
        self.replace(ui, '_ui_last_state', 'idle'); self.replace(ui, '_ui_last_sub', '')
        self.replace(ui, '_ui_phase_started_at', 0)
        with patch.object(ui, 'ui_call'), patch.object(ui.time, 'monotonic', return_value=100):
            ui.ui_state('thinking'); ui.ui_sub('Читаю файл fixture.py')
        with patch.object(ui.time, 'monotonic', return_value=112):
            phase = jarvis.JarvisApi().runtime_status()['phase']
        self.assertEqual(phase, {'state': 'thinking', 'text': 'Читаю файл fixture.py', 'elapsed_seconds': 12})
        with patch.object(ui, 'ui_call'):
            ui.ui_state('idle')
        self.assertEqual(ui.phase_snapshot()['text'], '')

    def test_every_supported_confirmation_word_survives_ingress(self):
        from jarvis_confirm import YES, NO
        for text in YES | NO:
            with self.subTest(text=text):
                self.assertEqual(conversation.classify_followup(text, confirmation_pending=True).action, 'accept')

    def test_lmstudio_diagnostics_do_not_probe_unused_ollama(self):
        client = Mock()
        client.models.list.return_value.data = [SimpleNamespace(id='fixture')]
        with patch.object(jarvis, 'LLM_ENGINE', 'lmstudio'), \
             patch.object(jarvis, 'LM_STUDIO_MODEL', 'fixture'), patch.object(jarvis, 'LM_STUDIO_CODE_MODEL', 'fixture'), \
             patch.object(jarvis, 'get_lmstudio_client', return_value=client), \
             patch.object(jarvis, '_ollama_probe', side_effect=AssertionError('unused backend')) as probe, \
             patch.object(jarvis, '_whisper_available', return_value=True), \
             patch.object(jarvis, '_effective_tts_engine', return_value='piper'), \
             patch.object(jarvis, '_piper_available', return_value=True), \
             patch.object(jarvis, '_get_vault', return_value=None), patch.object(jarvis, '_build_app_catalog', return_value=[]):
            spoken, data = jarvis.get_jarvis_status()
        self.assertTrue(data['llm_ok'])
        self.assertNotIn('Ollama недоступна', spoken)
        self.assertIn('генерация не проверялась', spoken)
        probe.assert_not_called(); client.models.list.assert_called_once_with(timeout=2)

    def test_missing_folder_suggests_metadata_but_never_resolves_guess(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'Учет оборудования').mkdir()
            with self.assertRaises(paths.NameNotFound) as error:
                paths.resolve_named('учета вырубования', kind='directory', roots=[root])
            self.assertIn('Похожие папки', str(error.exception))
            self.assertIn('Учет оборудования', str(error.exception))
            self.assertEqual(paths.resolve_named('учета оборудования', kind='directory', roots=[root]), root / 'Учет оборудования')

    def test_suggestions_stay_inside_permission_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = root / 'allowed'; allowed.mkdir()
            (root / 'Учет оборудования').mkdir()
            with self.assertRaises(paths.NameNotFound) as error:
                paths.resolve_named('учета вырубования', kind='directory', roots=[allowed])
            self.assertNotIn('Похожие папки', str(error.exception))


if __name__ == '__main__':
    unittest.main()

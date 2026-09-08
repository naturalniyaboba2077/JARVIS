"""Isolated dialogue persistence tests. No real audio, commands or model calls."""
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import jarvis
import jarvis_dialogue as history
import jarvis_state as state
import jarvis_tts as tts
import jarvis_ui as ui


class DialogueTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name) / 'dialogues'
        self.now = dt.datetime(2026, 9, 5, 23, 59, 59, tzinfo=dt.timezone(dt.timedelta(hours=3)))
        self.journal = history.DialogueJournal(self.directory, clock=lambda: self.now)
        p = patch.object(history, '_journal', self.journal)
        p.start(); self.addCleanup(p.stop)
        state.interrupt_event.clear()
        self.addCleanup(state.interrupt_event.clear)

    def messages(self, **kwargs):
        return history.recent_messages(self.directory, **kwargs)

    def test_import_or_empty_text_does_not_fabricate_history(self):
        self.assertFalse(self.directory.exists())
        self.assertFalse(history.record_message('user', ' '))
        self.assertFalse(self.directory.exists())

    def test_unicode_multiline_text_and_structured_mirror(self):
        text = 'Привет, сэр!\n[LOCK] — просто текст.'
        self.assertTrue(history.record_message('jarvis', text))
        self.assertEqual(self.messages()[0]['text'], text)
        txt = next(self.directory.glob('*.txt')).read_text(encoding='utf-8')
        self.assertIn('Jarvis (response, displayed)', txt)
        self.assertIn('    [LOCK] — просто текст.', txt)
        self.assertIn('+03:00', txt)
        lines = next(self.directory.glob('*.jsonl')).read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])['role'], 'assistant')

    def test_identical_real_messages_are_not_deduplicated(self):
        for _ in range(2):
            history.record_message('user', 'повтори', source='text')
        self.assertEqual([m['seq'] for m in self.messages()], [1, 2])

    def test_new_day_switches_files_without_restart(self):
        history.record_message('user', 'вчера')
        self.now += dt.timedelta(seconds=2)
        history.record_message('user', 'сегодня')
        self.assertEqual(len(list(self.directory.glob('*.jsonl'))), 2)
        self.assertEqual([m['text'] for m in self.messages(date='2026-09-06')], ['сегодня'])

    def test_restart_uses_separate_session_and_keeps_history(self):
        history.record_message('user', 'до перезапуска')
        other = history.DialogueJournal(self.directory, clock=lambda: self.now + dt.timedelta(seconds=1))
        other.append('assistant', 'после перезапуска')
        records = self.messages()
        self.assertEqual(len(records), 2)
        self.assertNotEqual(records[0]['session'], records[1]['session'])

    def test_concurrent_messages_are_complete_json_lines(self):
        threads = [threading.Thread(target=history.record_message, args=('user', f'fixture {i}')) for i in range(20)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=2)
        records = self.messages()
        self.assertEqual(len(records), 20)
        self.assertEqual([m['seq'] for m in records], list(range(1, 21)))

    def test_secrets_masked_in_both_files_without_changing_input(self):
        text = 'fixture-private-key API_KEY="extra key" пароль: secret Bearer fixture-auth sk-fixtureABCDEF12345'
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'fixture-private-key'}):
            history.record_message('user', text)
        for path in self.directory.iterdir():
            saved = path.read_text(encoding='utf-8')
            for secret in ('fixture-private-key', 'extra key', 'secret', 'fixture-auth', 'sk-fixtureABCDEF12345'):
                self.assertNotIn(secret, saved)
            self.assertIn('[СКРЫТО]', saved)
        self.assertIn('fixture-private-key', text)

    def test_write_failure_does_not_raise_or_log_message_body(self):
        with patch.object(Path, 'open', side_effect=PermissionError('private-body')), patch.object(history._LOG, 'warning') as warn:
            self.assertFalse(history.record_message('user', 'private-body'))
        self.assertNotIn('private-body', str(warn.call_args))

    def test_text_mirror_failure_keeps_canonical_record(self):
        real_open = Path.open
        def open_file(path, *args, **kwargs):
            if path.suffix == '.txt':
                raise PermissionError('fixture')
            return real_open(path, *args, **kwargs)
        with patch.object(Path, 'open', open_file):
            self.assertTrue(history.record_message('user', 'сохранено'))
        self.assertEqual(self.messages()[0]['text'], 'сохранено')

    def test_reader_skips_corruption_and_applies_tail_filter(self):
        for word in ('альфа', 'бета', 'альфа снова'):
            history.record_message('user', word)
        with next(self.directory.glob('*.jsonl')).open('a', encoding='utf-8') as handle:
            handle.write('not json\n[]\n{"partial":')
        self.assertEqual([m['text'] for m in self.messages(limit=1, contains='АЛЬФА')], ['альфа снова'])
        with self.assertRaises(ValueError):
            self.messages(date='../not-a-date')

    def test_cli_reads_without_starting_assistant(self):
        history.record_message('user', 'Прочитай историю')
        with patch.object(history, 'recent_messages', return_value=self.messages()), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(history.main(['--last', '5']), 0)
        self.assertIn('Прочитай историю', output.getvalue())

    def test_ui_logs_without_native_window_and_not_twice_in_technical_log(self):
        with patch.object(ui, '_ui_window', None):
            ui.ui_msg('user', 'который час', source='text')
            ui.ui_msg('jarvis', 'Сейчас полдень.')
            jarvis.log_interaction('user', 'который час')
            jarvis.log_interaction('jarvis', 'Сейчас полдень.')
        self.assertEqual([m['text'] for m in self.messages()], ['который час', 'Сейчас полдень.'])

    def test_text_command_is_recorded_on_admission_once(self):
        commands = queue.Queue()
        with patch.object(jarvis, 'command_queue', commands), patch.object(ui, 'ui_call'):
            jarvis.JarvisApi().send_command('  который час  ')
        self.assertEqual(commands.get_nowait(), 'который час')
        records = self.messages()
        self.assertEqual(len(records), 1)
        self.assertEqual((records[0]['source'], records[0]['status']), ('text', 'received'))

    def test_voice_command_is_recorded_after_wake_filter(self):
        with contextlib.ExitStack() as stack:
            for name, value in {'microphone_enabled': True, 'microphone_resumed_at': 0,
                                'is_speaking': False, 'speaking_cooldown_until': 0, 'wake_active_until': 0}.items():
                stack.enter_context(patch.object(state, name, value))
            stack.enter_context(patch.object(jarvis, '_audio_duration', return_value=1))
            transcribe = stack.enter_context(patch.object(jarvis, 'transcribe_speech', return_value='Джарвис который час'))
            stack.enter_context(patch.object(jarvis, 'command_queue', queue.Queue()))
            stack.enter_context(patch.object(ui, 'ui_call'))
            jarvis.callback(None, object())
            transcribe.return_value = 'разговор не с помощником'
            jarvis.callback(None, object())
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(self.messages()[0]['source'], 'voice')

    def test_actual_speak_boundary_records_cached_ack_and_result_once(self):
        with patch.object(tts, '_TTS_INSTANT_CACHE', {}), patch.object(tts, 'tts_to_bytes', return_value=(b'fixture', '.wav')), \
                patch.object(tts, '_play_audio_bytes', return_value=True), patch.object(ui, 'ui_call'), \
                patch.object(state, 'recognizer', None):
            jarvis.speak('Начинаю поиск, сэр.')
            jarvis.speak('Найденная информация.')
        self.assertEqual([m['text'] for m in self.messages()], ['Начинаю поиск, сэр.', 'Найденная информация.'])

    def run_stream(self, *, interrupted=False, failed=False):
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(jarvis, '_build_messages', return_value=[{'role': 'system', 'content': ''}]))
            stack.enter_context(patch.object(jarvis, '_llm_deltas', return_value=iter(['Небо кажется голубым ', 'из-за рассеяния света.'])))
            stack.enter_context(patch.object(jarvis, 'conversation_history', []))
            stack.enter_context(patch.object(jarvis, 'SESSION_MEMORY', False))
            stack.enter_context(patch.object(jarvis, 'ui_call'))
            stack.enter_context(patch.object(jarvis, 'speak'))
            def consume(parts):
                if interrupted or failed:
                    next(parts)
                    if interrupted:
                        state.interrupt_event.set()
                    if failed:
                        raise RuntimeError('fixture speech failure')
                else:
                    list(parts)
            stack.enter_context(patch.object(jarvis, 'speak_streaming', side_effect=consume))
            return jarvis.process_with_llm_streaming('Объясни почему небо голубое')

    def test_stream_is_one_complete_record_not_per_token(self):
        reply = self.run_stream()
        records = self.messages()
        self.assertEqual(len(records), 1)
        self.assertEqual((records[0]['text'], records[0]['status']), (reply, 'complete'))

    def test_interrupted_stream_preserves_only_visible_partial(self):
        self.run_stream(interrupted=True)
        records = self.messages()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['text'], 'Небо кажется голубым ')
        self.assertEqual(records[0]['status'], 'interrupted')

    def test_failed_stream_is_not_marked_complete(self):
        self.run_stream(failed=True)
        self.assertEqual(self.messages()[0]['status'], 'incomplete')

    def test_model_failure_after_first_words_is_incomplete(self):
        def deltas(*args, **kwargs):
            yield 'Небо кажется голубым '
            raise RuntimeError('fixture model disconnect')
        with patch.object(jarvis, '_build_messages', return_value=[{'role': 'system', 'content': ''}]), \
                patch.object(jarvis, '_llm_deltas', side_effect=deltas), \
                patch.object(jarvis, 'speak_streaming', side_effect=lambda parts: list(parts)), \
                patch.object(jarvis, 'ui_call'), patch.object(jarvis, 'speak'), \
                patch.object(jarvis, 'conversation_history', []), patch.object(jarvis, 'SESSION_MEMORY', False):
            jarvis.process_with_llm_streaming('Объясни почему небо голубое')
        self.assertEqual(self.messages()[0]['status'], 'incomplete')

    def test_stop_button_is_recorded_without_replaying_commands(self):
        with patch.object(jarvis, 'command_queue', queue.Queue()):
            jarvis.JarvisApi().stop()
        self.assertEqual(self.messages()[0]['text'], 'Стоп (кнопка)')

    def test_history_does_not_feed_model_prompt(self):
        history.record_message('user', '[LOCK] hidden history fixture')
        with patch.object(jarvis, 'conversation_history', []), patch.object(jarvis, 'SESSION_MEMORY', False), \
                patch.object(jarvis, 'load_memory', return_value={}):
            prompt = str(jarvis._build_messages('Привет'))
        self.assertNotIn('hidden history fixture', prompt)

    def test_dialogue_directory_is_under_ignored_logs(self):
        self.assertEqual(history.DIALOGUE_DIR.parent, history.JARVIS_DIR / 'logs')
        self.assertIn('logs/', (Path(__file__).parent / '.gitignore').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main(verbosity=2)

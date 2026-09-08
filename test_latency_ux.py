"""Latency/UX contracts, offline. Execute via run_tests.py, not in personal data."""
import io
import json
import threading
import unittest
from unittest.mock import Mock, patch

import jarvis
import jarvis_config as config
import jarvis_state as state
import jarvis_tts as tts
import overlay
from jarvis_speech_chunks import SpeechChunks, capability_reply, can_stream_reply, CAPABILITY_REPLY


class ChunkTests(unittest.TestCase):
    def test_starts_before_sentence_ends(self):
        chunks = SpeechChunks(clock=lambda: 0)
        self.assertEqual(chunks.feed("Могу помочь вам "), ["Могу помочь вам"])
        self.assertEqual(chunks.feed("разобраться с этим вопросом"), [])
        self.assertEqual(chunks.finish(), ["разобраться с этим вопросом"])

    def test_partial_tokens_and_decimal_are_preserved(self):
        chunks = SpeechChunks(clock=lambda: 0)
        out = []
        for delta in ["Вер", "сия 3.", "14 уже ", "работает. ", "Остаётся проверить результат."]:
            out.extend(chunks.feed(delta))
        out.extend(chunks.finish())
        self.assertEqual(" ".join(out), "Версия 3.14 уже работает. Остаётся проверить результат.")
        self.assertNotIn("Версия 3.", out)

    def test_initial_whitespace_does_not_stall(self):
        chunks = SpeechChunks(clock=lambda: 0)
        self.assertEqual(chunks.feed("  Один два три четыре "), ["Один два три"])

    def test_slow_stream_yields_complete_words_only(self):
        now = [0]
        chunks = SpeechChunks(clock=lambda: now[0])
        self.assertEqual(chunks.feed("Небо ка"), [])
        now[0] = .3
        self.assertEqual(chunks.feed("жется"), ["Небо"])
        self.assertEqual(chunks.finish(), ["кажется"])

    def test_later_chunks_not_per_word_or_comma(self):
        chunks = SpeechChunks(clock=lambda: 0)
        chunks.feed("Один два три ")
        self.assertEqual(chunks.feed("яблоки, груши, сливы, "), [])

    def test_capabilities_exact_question_only(self):
        self.assertEqual(capability_reply(" Что ты умеешь?! "), CAPABILITY_REPLY)
        self.assertIsNone(capability_reply("что ты умеешь и открой браузер"))

    def test_streaming_does_not_grant_action_authority(self):
        self.assertTrue(can_stream_reply("Объясни, почему небо голубое"))
        for text in ("открой браузер", "проверь мой проект", "объясни как удалить файл",
                     "не отправляй письмо", "расскажи про команду [LOCK]"):
            self.assertFalse(can_stream_reply(text), text)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        state.interrupt_event.clear()
        self.addCleanup(state.interrupt_event.clear)
        for p in (patch.object(jarvis, 'load_memory', return_value={}),
                  patch.object(jarvis, 'SESSION_MEMORY', False),
                  patch.object(jarvis, 'conversation_history', []),
                  patch.object(jarvis, 'ui_call'), patch.object(jarvis, 'ui_msg'),
                  patch.object(jarvis, 'log_interaction'), patch.object(jarvis, 'speak')):
            p.start(); self.addCleanup(p.stop)

    def test_help_never_waits_for_llm(self):
        with patch.object(jarvis, '_llm_deltas', side_effect=AssertionError('Not needed')):
            self.assertEqual(jarvis.process_with_llm_streaming('что ты умеешь?'), CAPABILITY_REPLY)
        jarvis.speak.assert_called_once_with(CAPABILITY_REPLY)

    def test_short_prompt_preserves_policy_but_omits_tool_table(self):
        short = jarvis._build_messages('Объясни почему небо голубое')[0]['content']
        full = jarvis._build_messages('открой браузер')[0]['content']
        self.assertLess(len(short), len(full) / 2)
        self.assertIn(jarvis.SYSTEM_PROMPT_BASE.split('Команда/действие')[0], short)
        self.assertIn('[CMD:', full)

    def test_actual_stream_hands_first_words_to_speech_before_eos(self):
        received = []
        def deltas(*args, **kwargs):
            yield 'Небо кажется голубым '
            self.assertEqual(received, ['Небо кажется голубым'])
            yield 'из-за рассеяния света.'
        def consume(items):
            for item in items:
                received.append(item)
        with patch.object(jarvis, '_llm_deltas', side_effect=deltas), \
                patch.object(jarvis, 'speak_streaming', side_effect=consume):
            result = jarvis.process_with_llm_streaming('Объясни почему небо голубое')
        self.assertEqual(result, 'Небо кажется голубым из-за рассеяния света.')
        self.assertEqual(len(received), 2)
        self.assertTrue(any('jvStream' in call.args[0] for call in jarvis.ui_call.call_args_list))

    def test_action_request_never_speaks_speculative_success(self):
        with patch.object(jarvis, '_llm_deltas', return_value=iter(['Готово, открыл. [OPEN:chrome]'])), \
                patch.object(jarvis, 'parse_and_execute_tags', return_value='Не удалось открыть.') as execute, \
                patch.object(jarvis, 'speak_streaming') as streaming:
            result = jarvis.process_with_llm_streaming('открой браузер')
        streaming.assert_not_called()
        execute.assert_called_once()
        jarvis.speak.assert_called_once_with('Не удалось открыть.')
        self.assertEqual(result, 'Не удалось открыть.')

    def test_late_command_in_conversation_does_not_execute(self):
        with patch.object(jarvis, '_llm_deltas', return_value=iter(['Небо кажется голубым ', '[LOCK]'])), \
                patch.object(jarvis, 'parse_and_execute_tags') as execute, \
                patch.object(jarvis, 'speak_streaming', side_effect=lambda items: list(items)):
            result = jarvis.process_with_llm_streaming('Объясни почему небо голубое')
        execute.assert_not_called()
        self.assertIn('Не выполнял действия', result)


class VoiceTests(unittest.TestCase):
    def test_piper_is_loaded_even_when_no_cache_files_need_generation(self):
        with patch.object(tts, '_effective_tts_engine', return_value='piper'), \
                patch.object(tts, '_piper_available', return_value=True), \
                patch.object(tts, 'INSTANT_PHRASES', []), \
                patch.object(tts, '_load_piper') as load:
            tts.prewarm_tts_cache()
        load.assert_called_once()

    def test_prosody_and_cache_identity(self):
        keys = []
        for style in ('neutral', 'lively', 'calm'):
            with patch.object(tts, 'VOICE_STYLE', style):
                keys.append(tts._edge_voice_key())
                options = tts._edge_options('Я на связи.')
                self.assertRegex(options['rate'], r'^[+-]\d+%$')
                self.assertRegex(options['pitch'], r'^[+-]\d+Hz$')
        self.assertEqual(len(set(keys)), 3)

    def test_error_delivery_is_more_restrained(self):
        with patch.object(tts, 'VOICE_STYLE', 'lively'):
            normal = tts._edge_options('Я на связи.')
            error = tts._edge_options('Не удалось выполнить запрос.')
        self.assertLess(int(error['rate'][:-1]), int(normal['rate'][:-1]))
        self.assertLess(int(error['pitch'][:-2]), int(normal['pitch'][:-2]))

    def test_unknown_voice_style_is_rejected_before_write(self):
        with patch.object(config, '_read_config_snapshot', return_value=({}, b'{}')), \
                patch.object(config, '_atomic_write_config_bytes') as write:
            ok, _ = config._write_config_file({'JARVIS_VOICE_STYLE': 'arbitrary'})
        self.assertFalse(ok)
        write.assert_not_called()

    def test_bytes_playback_uses_memory_and_closes_stream(self):
        loaded = []
        state.interrupt_event.clear()
        with patch.object(tts, 'pygame') as pygame, patch.object(tts, '_audio_envelope', return_value=None), \
                patch.object(tts, '_playback_pump', return_value=True):
            pygame.mixer.music.load.side_effect = loaded.append
            self.assertTrue(tts._play_audio_bytes(b'synthetic bytes'))
        self.assertIsInstance(loaded[0], io.BytesIO)
        self.assertTrue(loaded[0].closed)


class OverlayTests(unittest.TestCase):
    def overlay(self):
        instance = overlay.Overlay.__new__(overlay.Overlay)
        instance.lock = threading.Lock()
        instance.closed = instance.want_show = instance.shown = False
        instance.amp = instance.level = 0.0
        instance.root, instance.canvas = Mock(), Mock()
        instance.bars = list(range(7))
        instance._position = Mock()
        return instance

    def test_geometry_uses_work_area_not_fullscreen(self):
        self.assertEqual(overlay.capsule_position((0, 0, 1920, 1040)), (820, 960))
        self.assertEqual(overlay.capsule_position((-1920, 0, 0, 1040)), (-1100, 960))
        self.assertEqual((overlay.WIDTH, overlay.HEIGHT), (280, 64))

    def test_bad_amplitudes_are_bounded(self):
        for value, expected in [('bad', 0), (None, 0), (float('nan'), 0), (float('inf'), 0), (-1, 0), (2, 1), (.5, .5)]:
            self.assertEqual(overlay.amplitude(value), expected)

    def test_eof_and_quit_do_not_touch_tk_on_reader_thread(self):
        for source in ('', '{}\n', '{"quit":true}\n'):
            item = self.overlay()
            with patch.object(overlay.sys, 'stdin', io.StringIO(source)):
                item._read_stdin()
            self.assertTrue(item.closed)
            self.assertFalse(item.root.mock_calls)
            item._tick()
            item.root.destroy.assert_called_once()

    def test_malformed_lines_do_not_terminate_valid_input(self):
        item = self.overlay()
        with patch.object(overlay.sys, 'stdin', io.StringIO('bad\n[]\n{"amp":"bad"}\n{"amp":0.7,"show":true}\n')):
            item._read_stdin()
        self.assertEqual(item.amp, .7)
        self.assertTrue(item.want_show)

    def test_hidden_overlay_does_not_redraw_or_rewithdraw(self):
        item = self.overlay()
        item._tick(); item._tick()
        item.canvas.coords.assert_not_called()
        item.root.withdraw.assert_not_called()
        item.root.after.assert_called_with(120, item._tick)

    def test_visible_overlay_only_shows_once_and_reuses_seven_bars(self):
        item = self.overlay()
        item.want_show, item.amp = True, .7
        item._tick(); item._tick()
        item.root.deiconify.assert_called_once()
        self.assertEqual(item.canvas.coords.call_count, 14)
        item.root.after.assert_called_with(33, item._tick)


if __name__ == '__main__':
    unittest.main(verbosity=2)

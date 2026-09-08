"""Offline regression tests for deadlines, routing and speech cancellation.

Run: py -3.10 -I -B test_audit_pipeline.py
Loads the real modules with config, logging, audio and cloud imports stubbed.
No application startup, real configuration, models, microphone or network.
"""
import asyncio
import importlib.util
import logging
from pathlib import Path
import queue
import sys
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parent


def load_source(filename):
    spec = importlib.util.spec_from_file_location('_audit_' + filename[:-3], ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PipelineCase(unittest.TestCase):
    def setUp(self):
        logger = logging.getLogger('audit-pipeline-offline')
        logger.addHandler(logging.NullHandler())
        self.state = load_source('jarvis_state.py')
        config = types.SimpleNamespace(
            FOLLOWUP_MODE='normal', FOLLOWUP_WINDOW=10., SPEAK_COOLDOWN=.5,
            JARVIS_DIR=ROOT, _pythonw_exe=Mock(side_effect=AssertionError('No processes')))
        self.music = Mock()
        pygame = types.SimpleNamespace(mixer=types.SimpleNamespace(
            get_init=lambda: True, music=self.music), time=types.SimpleNamespace(Clock=Mock()))
        self.ui_states = []
        ui = types.SimpleNamespace(OVERLAY_ENABLED=False, _main_window_minimized=lambda: False,
            _overlay_send=Mock(), ui_msg=Mock(), ui_state=self.ui_states.append, ui_sub=Mock())
        stubs = {'jarvis_state': self.state, 'jarvis_config': config,
            'jarvis_log': types.SimpleNamespace(jarvis_logger=logger), 'jarvis_ui': ui,
            'pygame': pygame, 'edge_tts': types.SimpleNamespace(
                Communicate=Mock(side_effect=AssertionError('No edge network'))),
            'openai': types.SimpleNamespace(OpenAI=Mock(side_effect=AssertionError('No cloud client')))}
        values = {'OPENROUTER_API_KEY':'mock-key', 'JARVIS_LLM':'local', 'TTS_ENGINE':'piper'}
        with patch.dict(sys.modules, stubs), patch('os.getenv', lambda key, default=None: values.get(key, default)):
            self.llm = load_source('jarvis_llm.py')
            self.tts = load_source('jarvis_tts.py')
        self.state.recognizer = types.SimpleNamespace(energy_threshold=333)
        self.tts._piper_available = Mock(return_value=True)
        self.tts._piper_to_wav_bytes = Mock(side_effect=AssertionError('No native Piper'))
        self.tts._xtts_to_wav_bytes = Mock(side_effect=AssertionError('No native XTTS'))
        self.native_playback = self.tts._play_audio_bytes
        self.tts._play_audio_bytes = Mock(return_value=True)
        self.llm._ollama_probe = Mock(side_effect=AssertionError('No probe network'))
        self.ollama_transport = self.llm._ollama_deltas
        self.llm._ollama_deltas = Mock(side_effect=AssertionError('No local network'))
        self.cloud_transport = self.llm._cloud_deltas
        self.llm._cloud_deltas = Mock(side_effect=AssertionError('No cloud network'))
        self.lmstudio_transport = self.llm._lmstudio_deltas
        self.llm._lmstudio_deltas = Mock(side_effect=AssertionError('No LM Studio network'))

    def capture_llm_workers(self):
        result = []
        original = self.llm._pump_engine
        def pump(*args, **kwargs):
            q = original(*args, **kwargs)
            result.append(q)
            return q
        self.llm._pump_engine = pump
        return result

    def capture_tts_workers(self):
        workers = []
        def start(**kwargs):
            worker = threading.Thread(**kwargs)
            workers.append(worker)
            return worker
        self.tts.threading = types.SimpleNamespace(Thread=start, Event=threading.Event)
        return workers


class LLMTests(PipelineCase):
    def test_lmstudio_routes_normal_and_code_models(self):
        self.llm.LLM_ENGINE = 'lmstudio'
        self.llm.LM_STUDIO_MODEL = 'normal-model'
        self.llm.LM_STUDIO_CODE_MODEL = 'code-model'
        self.llm._ollama_available = Mock(side_effect=AssertionError('LM Studio is primary'))
        seen = []

        def lmstudio(_messages, model=None, **_kwargs):
            seen.append(model)
            return iter([model])

        self.llm._lmstudio_deltas = lmstudio
        self.assertEqual(list(self.llm._llm_deltas([], prefer='local')), ['normal-model'])
        self.assertEqual(list(self.llm._llm_deltas([], prefer='cloud')), ['code-model'])
        self.assertEqual(seen, ['normal-model', 'code-model'])
        self.llm._ollama_available.assert_not_called()

    def test_cloud_preference_never_waits_for_ollama_lock(self):
        self.llm._ollama_lock.acquire()
        self.addCleanup(self.llm._ollama_lock.release)
        self.llm._ollama_available = Mock(side_effect=AssertionError('Cloud must not probe local'))
        self.llm._cloud_deltas = lambda *a, **k: iter(['cloud'])
        started = time.perf_counter()
        self.assertEqual(list(self.llm._llm_deltas([], prefer='cloud')), ['cloud'])
        self.assertLess(time.perf_counter() - started, .3)
        self.llm._ollama_available.assert_not_called()

    def test_cloud_mode_overrides_simple_query_preference(self):
        self.llm.LLM_ENGINE = 'cloud'
        self.llm._cloud_deltas = lambda *a, **k: iter(['cloud primary'])
        self.llm._ollama_available = Mock(side_effect=AssertionError('Not selected'))
        self.assertEqual(list(self.llm._llm_deltas([], prefer='local')), ['cloud primary'])
        self.llm._ollama_available.assert_not_called()

    def test_cloud_mode_falls_back_to_local(self):
        self.llm.LLM_ENGINE = 'cloud'
        self.llm._cloud_deltas = Mock(side_effect=RuntimeError('mock offline'))
        self.llm._ollama_available = Mock(return_value=True)
        self.llm._ollama_deltas = Mock(return_value=iter(['local fallback']))
        self.assertEqual(list(self.llm._llm_deltas([], prefer='cloud')), ['local fallback'])
        self.assertEqual(self.llm._cloud_deltas.call_count, 2)
        self.llm._ollama_available.assert_called_once()
        self.llm._ollama_deltas.assert_called_once()

    def test_local_mode_still_respects_classifier_preference(self):
        self.llm._ollama_available = Mock(return_value=True)
        self.llm._ollama_deltas = lambda *a, **k: iter(['local'])
        self.llm._cloud_deltas = lambda *a, **k: iter(['cloud'])
        for text, expected in [('который час', 'local'), ('напиши python скрипт', 'cloud')]:
            prefer, _ = self.llm._classify_complexity(text)
            self.assertEqual(list(self.llm._llm_deltas([], prefer=prefer)), [expected])

    def test_local_availability_is_inside_deadline_and_late_worker_cannot_start_generation(self):
        self.llm.LLM_DEADLINE = .04
        self.llm._cloud_deltas = lambda *a, **k: iter(['fallback'])
        release = threading.Event()
        def unavailable_until_released(**kwargs):
            release.wait(1)
            return True
        self.llm._ollama_available = unavailable_until_released
        workers = self.capture_llm_workers()
        try:
            started = time.perf_counter()
            self.assertEqual(list(self.llm._llm_deltas([])), ['fallback'])
            self.assertLess(time.perf_counter() - started, .4)
        finally:
            release.set()
            for q in workers:
                q.worker.join(.5)
        self.llm._ollama_deltas.assert_not_called()
        self.assertTrue(all(not q.worker.is_alive() for q in workers))
        self.assertEqual(workers[0].qsize(), 0)

    def test_startup_lock_wait_is_cancellable(self):
        self.llm._ollama_lock.acquire()
        self.addCleanup(self.llm._ollama_lock.release)
        self.llm.LLM_DEADLINE = .04
        self.llm._cloud_deltas = lambda *a, **k: iter(['cloud'])
        workers = self.capture_llm_workers()
        self.assertEqual(list(self.llm._llm_deltas([])), ['cloud'])
        workers[0].worker.join(.3)
        self.assertFalse(workers[0].worker.is_alive())
        self.llm._ollama_probe.assert_not_called()

    def test_ttft_includes_failed_availability_and_fallback_with_virtual_clock(self):
        clock = types.SimpleNamespace(now=100.)
        self.llm.time = types.SimpleNamespace(perf_counter=lambda: clock.now)
        def unavailable(**kwargs):
            clock.now += .6
            return False
        def cloud(*args, **kwargs):
            clock.now += .2
            yield 'answer'
        self.llm._ollama_available = unavailable
        self.llm._cloud_deltas = cloud
        self.assertEqual(list(self.llm._llm_deltas([])), ['answer'])
        self.assertAlmostEqual(self.state.last_llm_ttft_ms, 800., places=6)

    def test_startup_probe_uses_remaining_budget(self):
        clock = types.SimpleNamespace(now=0.)
        self.llm.time = types.SimpleNamespace(perf_counter=lambda: clock.now)
        timeouts = []
        def probe(timeout):
            timeouts.append(timeout)
            clock.now += timeout
            return False
        self.llm._ollama_probe = probe
        with patch.object(self.llm.subprocess, 'Popen') as spawn:
            self.assertFalse(self.llm._ollama_available(deadline_at=.25))
        self.assertEqual(timeouts, [.25])
        spawn.assert_not_called()
        self.assertIsNone(self.llm._ollama_ok)

    def test_token_already_queued_after_deadline_is_not_accepted(self):
        clock = types.SimpleNamespace(now=0.)
        self.llm.time = types.SimpleNamespace(perf_counter=lambda: clock.now)
        self.llm.LLM_DEADLINE = 1.
        self.llm._ollama_available = lambda **kw: True
        def late(*args, **kwargs):
            clock.now = 1.01
            yield 'too late'
        self.llm._ollama_deltas = late
        self.llm._cloud_deltas = lambda *a, **k: iter(['fallback'])
        self.assertEqual(list(self.llm._llm_deltas([])), ['fallback'])
        self.assertAlmostEqual(self.state.last_llm_ttft_ms, 1010.)

    def test_cancellation_while_waiting_for_llm_stops_cooperative_worker(self):
        entered = threading.Event()
        finished = threading.Event()
        def tokens(*args, cancel_event=None, **kwargs):
            try:
                entered.set()
                while not cancel_event.wait(.01):
                    pass
                yield 'must be discarded'
            finally:
                finished.set()
        self.llm._ollama_available = lambda **kw: True
        self.llm._ollama_deltas = tokens
        workers = self.capture_llm_workers()
        received, errors = [], []
        def consume():
            try:
                received.extend(self.llm._llm_deltas([]))
            except BaseException as exc:
                errors.append(exc)
        consumer = threading.Thread(target=consume)
        consumer.start()
        try:
            self.assertTrue(entered.wait(.5))
        finally:
            self.state.interrupt_event.set()
            consumer.join(.5)
            for q in workers:
                q.worker.join(.5)
        self.assertFalse(consumer.is_alive())
        self.assertTrue(finished.is_set())
        self.assertEqual(received, [])
        self.assertEqual(errors, [])
        self.assertTrue(self.state.interrupt_event.is_set())
        self.llm._cloud_deltas.assert_not_called()

    def test_cancelled_request_resets_ttft_without_starting_engine(self):
        self.state.last_llm_ttft_ms = 9000.
        self.state.interrupt_event.set()
        self.llm._ollama_available = Mock(side_effect=AssertionError('Cancelled'))
        self.assertEqual(list(self.llm._llm_deltas([])), [])
        self.assertEqual(self.state.last_llm_ttft_ms, 0.)
        self.llm._ollama_available.assert_not_called()

    def test_empty_answer_falls_back_and_counts(self):
        self.llm._ollama_available = lambda **kw: True
        self.llm._ollama_deltas = lambda *a, **k: iter(['', ''])
        self.llm._cloud_deltas = lambda *a, **k: iter(['answer'])
        self.assertEqual(list(self.llm._llm_deltas([])), ['answer'])
        self.assertEqual(self.state.llm_empty_failovers, 1)

    def test_success_does_not_log_a_spurious_stream_error(self):
        self.llm._ollama_available = lambda **kw: True
        self.llm._ollama_deltas = lambda *a, **k: iter(['complete'])
        with patch.object(self.llm.jarvis_logger, 'error') as error:
            self.assertEqual(list(self.llm._llm_deltas([])), ['complete'])
        error.assert_not_called()

    def test_error_after_first_token_propagates_without_mixing_models(self):
        def broken(*args, **kwargs):
            yield 'partial'
            raise RuntimeError('stream disconnected')
        self.llm._ollama_available = lambda **kw: True
        self.llm._ollama_deltas = broken
        stream = self.llm._llm_deltas([])
        self.assertEqual(next(stream), 'partial')
        with self.assertRaisesRegex(RuntimeError, 'stream disconnected'):
            next(stream)
        self.llm._cloud_deltas.assert_not_called()

    def test_cancel_full_engine_queue_closes_generator(self):
        full = threading.Event()
        closed = threading.Event()
        def tokens(_):
            try:
                for i in range(10000):
                    if i == 16:
                        full.set()
                    yield str(i)
            finally:
                closed.set()
        q = self.llm._pump_engine(tokens, [])
        self.assertTrue(full.wait(.5))
        q.cancel_event.set()
        q.worker.join(.3)
        self.assertFalse(q.worker.is_alive())
        self.assertTrue(closed.is_set())
        self.assertTrue(q.done.is_set())
        self.assertLessEqual(q.qsize(), 16)

    def test_closing_consumer_stops_worker_without_unbounded_drain(self):
        closed = threading.Event()
        def tokens(*a, **kw):
            try:
                yield from (str(i) for i in range(10000))
            finally:
                closed.set()
        self.llm._ollama_available = lambda **kw: True
        self.llm._ollama_deltas = tokens
        workers = self.capture_llm_workers()
        stream = self.llm._llm_deltas([])
        self.assertEqual(next(stream), '0')
        stream.close()
        workers[0].worker.join(.3)
        self.assertTrue(closed.is_set())
        self.assertFalse(workers[0].worker.is_alive())

    def test_cloud_stream_closes_on_early_exit_and_skips_usage(self):
        self.llm._cloud_deltas = self.cloud_transport
        chunks = [types.SimpleNamespace(choices=[]), types.SimpleNamespace(choices=[
            types.SimpleNamespace(delta=types.SimpleNamespace(content='token'))])]
        stream = Mock()
        stream.__iter__ = Mock(return_value=iter(chunks))
        create = Mock(return_value=stream)
        self.llm.get_openrouter_client = lambda: types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
        generator = self.llm._cloud_deltas([], timeout=.3)
        self.assertEqual(next(generator), 'token')
        generator.close()
        stream.close.assert_called_once()
        self.assertEqual(create.call_args.kwargs['timeout'], .3)

    def test_ollama_transport_closes_response_and_uses_native_timeout(self):
        import urllib.request
        response = Mock()
        response.__enter__ = Mock(return_value=iter([b'{"message":{"content":"token"}}\n']))
        response.__exit__ = Mock(return_value=False)
        with patch.object(urllib.request, 'urlopen', return_value=response) as open_url:
            stream = self.ollama_transport([], timeout=.25)
            self.assertEqual(next(stream), 'token')
            stream.close()
        self.assertEqual(open_url.call_args.kwargs['timeout'], .25)
        response.__exit__.assert_called_once()


class TTSTests(PipelineCase):
    def setUp(self):
        super().setUp()
        self.real_tts_to_bytes = self.tts.tts_to_bytes
        self.tts.tts_to_bytes = Mock(return_value=(b'mock audio', '.wav'))

    def test_producer_error_reaches_caller_promptly_and_restores_voice_state(self):
        closed = threading.Event()
        def sentences():
            try:
                raise RuntimeError('upstream failed')
                yield
            finally:
                closed.set()
        workers = self.capture_tts_workers()
        started = time.perf_counter()
        with self.assertRaisesRegex(RuntimeError, 'upstream failed'):
            self.tts.speak_streaming(sentences())
        self.assertLess(time.perf_counter() - started, .4)
        self.assertTrue(closed.is_set())
        self.assertFalse(workers[0].is_alive())
        self.assertFalse(self.state.is_speaking)
        self.assertIsNone(self.state.threshold_before_speech)
        self.assertEqual(self.state.recognizer.energy_threshold, 333)
        self.assertEqual(self.ui_states[-1], 'idle')

    def test_synthesis_error_reaches_caller_and_closes_upstream(self):
        closed = threading.Event()
        def sentences():
            try:
                yield 'first'
                self.fail('must not advance after synthesis failure')
            finally:
                closed.set()
        self.tts.tts_to_bytes.side_effect = ValueError('synthesis failed')
        with self.assertRaisesRegex(ValueError, 'synthesis failed'):
            self.tts.speak_streaming(sentences())
        self.assertTrue(closed.is_set())
        self.assertFalse(self.state.is_speaking)

    def test_upstream_close_failure_is_delivered_on_terminal_channel(self):
        class Source:
            def __iter__(self): return self
            def __next__(self): raise StopIteration
            def close(self): raise RuntimeError('close failed')
        with self.assertRaisesRegex(RuntimeError, 'close failed'):
            self.tts.speak_streaming(Source())
        self.assertFalse(self.state.is_speaking)

    def test_end_delivered_for_empty_stream(self):
        workers = self.capture_tts_workers()
        self.tts.speak_streaming(iter([]))
        self.assertFalse(workers[0].is_alive())
        self.assertFalse(self.state.is_speaking)
        self.tts._play_audio_bytes.assert_not_called()

    def test_final_audio_after_empty_is_played_before_terminal_end_or_error(self):
        # Force the real ordering: get times out, producer puts its last audio
        # and publishes done, then the consumer observes the original Empty.
        for fail_after_audio in (False, True):
            with self.subTest(error=fail_after_audio):
                release = threading.Event()
                workers = self.capture_tts_workers()
                queued_at_done = []
                case = self

                class CompletionRaceQueue(queue.Queue):
                    raced = False

                    def get(self, *args, **kwargs):
                        try:
                            return super().get(*args, **kwargs)
                        except queue.Empty:
                            if self.maxsize == 3 and kwargs.get('timeout') and not self.raced:
                                self.raced = True
                                release.set()
                                workers[0].join(.5)
                                case.assertFalse(workers[0].is_alive())
                                queued_at_done.append(self.qsize())
                            raise

                def synth(*args, **kwargs):
                    if not release.wait(1):
                        raise AssertionError('Consumer did not reach Empty')
                    return b'final audio', '.wav'

                def sentences():
                    yield 'Final sentence.'
                    if fail_after_audio:
                        raise RuntimeError('terminal after audio')

                self.tts.queue = types.SimpleNamespace(
                    Queue=CompletionRaceQueue, Empty=queue.Empty, Full=queue.Full)
                self.tts.tts_to_bytes.side_effect = synth
                self.tts._play_audio_bytes.reset_mock()
                try:
                    if fail_after_audio:
                        with self.assertRaisesRegex(RuntimeError, 'terminal after audio'):
                            self.tts.speak_streaming(sentences())
                    else:
                        self.tts.speak_streaming(sentences())
                finally:
                    release.set()
                    for worker in workers:
                        worker.join(.5)
                self.assertEqual(queued_at_done, [1])
                self.tts._play_audio_bytes.assert_called_once()
                self.assertEqual(self.tts._play_audio_bytes.call_args.args[0], b'final audio')
                self.assertEqual(self.state.last_spoken_text, 'Final sentence.')
                self.assertFalse(self.state.is_speaking)

    def test_notification_ignores_stale_interrupt_without_resetting_old_response(self):
        old_response = self.state.PipelineCancellation()
        self.state.interrupt_event.set()
        generation = self.state.interrupt_event.generation
        self.assertIn('speak_notification', self.tts.__all__)
        self.tts.speak_notification('Synthetic timer notification.')
        self.tts.speak_notification('Synthetic reminder notification.')
        self.assertEqual(self.tts._play_audio_bytes.call_count, 2)
        self.assertEqual(self.tts.tts_to_bytes.call_count, 2)
        self.assertTrue(self.state.interrupt_event.is_set())
        self.assertEqual(self.state.interrupt_event.generation, generation)
        self.assertTrue(old_response.is_set())
        # Ordinary response continuations must not opt into notification semantics.
        self.tts.speak('Cancelled ordinary response.')
        self.tts.speak_streaming(iter(['Cancelled ordinary stream.']))
        self.assertEqual(self.tts.tts_to_bytes.call_count, 2)

    def test_new_barge_in_during_notification_synthesis_prevents_playback(self):
        self.state.interrupt_event.set()
        generation = self.state.interrupt_event.generation

        def synth(*args, **kwargs):
            self.state.interrupt_event.set()  # Already true: still a NEW barge-in.
            return b'cancelled notification', '.wav'

        self.tts.tts_to_bytes.side_effect = synth
        self.tts.speak_notification('Synthetic notification interrupted during synthesis.')
        self.assertEqual(self.state.interrupt_event.generation, generation + 1)
        self.tts.tts_to_bytes.assert_called_once()
        self.tts._play_audio_bytes.assert_not_called()
        self.assertTrue(self.state.interrupt_event.is_set())
        self.assertFalse(self.state.is_speaking)
        self.assertEqual(self.state.recognizer.energy_threshold, 333)

    def test_new_barge_in_stops_notification_playback_even_if_cleared_before_poll(self):
        self.state.interrupt_event.set()

        def barge_in():
            self.state.interrupt_event.set()
            self.state.interrupt_event.clear()  # New dispatcher boundary before poll.

        self.music.play.side_effect = barge_in
        self.music.get_busy.return_value = True
        self.tts._play_audio_bytes = self.native_playback
        self.tts.speak_notification('Synthetic notification interrupted during playback.')
        self.music.play.assert_called_once()
        self.music.stop.assert_called_once()
        self.music.unload.assert_called_once()
        self.assertFalse(self.state.is_speaking)
        self.assertEqual(self.state.recognizer.energy_threshold, 333)

    def test_notification_does_not_revive_cancelled_native_worker(self):
        entered, release = threading.Event(), threading.Event()
        workers = self.capture_tts_workers()
        errors = []

        def synth(text, **kwargs):
            if text == 'Old response.':
                entered.set()
                if not release.wait(2):
                    raise AssertionError('Old synthesis was not released')
                return b'old late audio', '.wav'
            return b'fresh notification audio', '.wav'

        def consume():
            try:
                self.tts.speak_streaming(iter(['Old response.']))
            except BaseException as exc:
                errors.append(exc)

        self.tts.tts_to_bytes.side_effect = synth
        consumer = threading.Thread(target=consume)
        consumer.start()
        try:
            self.assertTrue(entered.wait(.5))
            self.state.interrupt_event.set()
            consumer.join(.6)
            self.assertFalse(consumer.is_alive())
            self.assertTrue(workers[0].is_alive())
            self.tts.speak_notification('Fresh notification.')
            self.assertTrue(self.state.interrupt_event.is_set())
        finally:
            release.set()
            consumer.join(1)
            for worker in workers:
                worker.join(.5)
        self.assertFalse(errors)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.tts._play_audio_bytes.assert_called_once()
        self.assertEqual(self.tts._play_audio_bytes.call_args.args[0], b'fresh notification audio')
        self.assertFalse(self.state.is_speaking)

    def test_voice_is_frozen_for_entire_response(self):
        self.tts._effective_tts_engine = Mock(side_effect=['piper', 'edge'])
        self.tts.speak_streaming(iter(['One.', 'Two.']))
        self.tts._effective_tts_engine.assert_called_once()
        self.assertEqual([call.kwargs['engine'] for call in self.tts.tts_to_bytes.call_args_list],
                         ['piper', 'piper'])
        self.assertEqual(self.state.last_spoken_text, 'One. Two.')

    def test_explicit_xtts_never_uses_piper(self):
        self.tts._xtts_to_wav_bytes = Mock(return_value=b'cloned voice')
        self.assertEqual(self.real_tts_to_bytes('text', engine='xtts'), (b'cloned voice', '.wav'))
        self.tts._piper_to_wav_bytes.assert_not_called()

    def test_explicit_edge_failure_does_not_switch_voice(self):
        self.tts._edge_tts_to_bytes = Mock(return_value=None)
        self.assertEqual(self.real_tts_to_bytes('text', engine='edge'), (None, None))
        self.tts._piper_to_wav_bytes.assert_not_called()
        self.tts._xtts_to_wav_bytes.assert_not_called()

    def test_selected_edge_does_not_fallback_in_normal_or_streaming_speak(self):
        self.tts.TTS_ENGINE = 'edge'
        self.tts.tts_to_bytes = self.real_tts_to_bytes
        self.tts._edge_tts_to_bytes = Mock(return_value=None)
        self.tts.speak('normal edge phrase')
        self.tts.speak_streaming(iter(['streaming edge phrase']))
        self.assertEqual(self.tts._edge_tts_to_bytes.call_count, 2)
        self.tts._piper_to_wav_bytes.assert_not_called()
        self.tts._xtts_to_wav_bytes.assert_not_called()
        self.tts._play_audio_bytes.assert_not_called()
        self.assertFalse(self.state.is_speaking)

    def test_preexisting_cancel_does_not_get_cleared_by_either_speak_api(self):
        self.state.interrupt_event.set()
        source = Mock()
        self.tts.speak('cancelled text')
        self.tts.speak_streaming(source)
        self.assertTrue(self.state.interrupt_event.is_set())
        self.tts.tts_to_bytes.assert_not_called()
        self.tts._play_audio_bytes.assert_not_called()
        self.assertEqual(self.ui_states, [])

    def test_cancel_during_normal_synthesis_prevents_playback(self):
        def synth(*a, **kw):
            self.state.interrupt_event.set()
            return b'late audio', '.wav'
        self.tts.tts_to_bytes.side_effect = synth
        self.tts.speak('phrase')
        self.tts._play_audio_bytes.assert_not_called()
        self.assertTrue(self.state.interrupt_event.is_set())
        self.assertFalse(self.state.is_speaking)
        self.assertEqual(self.ui_states[-1], 'idle')

    def test_failed_normal_synthesis_also_restores_idle(self):
        self.tts.tts_to_bytes.return_value = (None, None)
        self.tts.speak('phrase')
        self.assertFalse(self.state.is_speaking)
        self.assertEqual(self.ui_states[-1], 'idle')

    def test_cancel_full_audio_queue_joins_producer(self):
        fifth = threading.Event()
        count = []
        def synth(*a, **kw):
            count.append(1)
            if len(count) == 5:
                fifth.set()
            return b'audio', '.wav'
        def play(*a, **kw):
            self.assertTrue(fifth.wait(.5))
            self.state.interrupt_event.set()
            return False
        self.tts.tts_to_bytes.side_effect = synth
        self.tts._play_audio_bytes.side_effect = play
        workers = self.capture_tts_workers()
        started = time.perf_counter()
        self.tts.speak_streaming(iter('Sentence %d' % i for i in range(20)))
        self.assertLess(time.perf_counter() - started, .5)
        self.assertFalse(workers[0].is_alive())
        self.assertEqual(len(count), 5)
        self.assertFalse(self.state.is_speaking)
        self.assertTrue(self.state.interrupt_event.is_set())

    def test_cancel_while_waiting_for_audio_wakes_consumer_and_discards_late_result(self):
        entered, release = threading.Event(), threading.Event()
        def synth(*a, **kw):
            entered.set()
            release.wait(2)
            return b'late audio', '.wav'
        self.tts.tts_to_bytes.side_effect = synth
        workers = self.capture_tts_workers()
        errors = []
        def run():
            try:
                self.tts.speak_streaming(iter(['Sentence.']))
            except BaseException as exc:
                errors.append(exc)
        consumer = threading.Thread(target=run)
        consumer.start()
        try:
            self.assertTrue(entered.wait(.5))
            self.state.interrupt_event.set()
            consumer.join(.6)
            self.assertFalse(consumer.is_alive())
            # Simulate the dispatcher's next command while the OLD native call
            # is still running. Its private cancellation must remain latched.
            self.state.interrupt_event.clear()
        finally:
            release.set()
            consumer.join(1)
            for worker in workers:
                worker.join(.5)
        self.assertFalse(errors)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.tts._play_audio_bytes.assert_not_called()
        self.assertFalse(self.state.is_speaking)

    def test_playback_gate_rechecks_after_dequeue(self):
        original_queue = queue.Queue
        state = self.state
        class GateQueue(original_queue):
            def get(self, *args, **kwargs):
                item = super().get(*args, **kwargs)
                if self.maxsize == 3:
                    state.interrupt_event.set()
                return item
        self.tts.queue = types.SimpleNamespace(Queue=GateQueue, Empty=queue.Empty, Full=queue.Full)
        self.tts.speak_streaming(iter(['Sentence.']))
        self.tts._play_audio_bytes.assert_not_called()
        self.assertFalse(self.state.is_speaking)

    def test_native_playback_gate_rechecks_after_load(self):
        self.tts._play_audio_bytes = self.native_playback
        self.music.load.side_effect = lambda _: self.state.interrupt_event.set()
        self.assertFalse(self.tts._play_audio_bytes(b'mock audio', '.wav'))
        self.music.play.assert_not_called()
        self.music.unload.assert_called_once()

    def test_cached_playback_never_clears_cancel_during_load(self):
        self.music.load.side_effect = lambda _: self.state.interrupt_event.set()
        self.assertFalse(self.tts._play_cached_file('mock.wav'))
        self.music.play.assert_not_called()
        self.assertTrue(self.state.interrupt_event.is_set())
        self.assertFalse(self.state.is_speaking)

    def test_cancel_before_cached_playback_restores_ui(self):
        self.tts._TTS_INSTANT_CACHE = {'cached phrase': 'mock.wav'}
        def exists(_):
            self.state.interrupt_event.set()
            return True
        with patch.object(self.tts.os.path, 'exists', side_effect=exists):
            self.tts.speak('cached phrase')
        self.music.load.assert_not_called()
        self.assertEqual(self.ui_states[-1], 'idle')
        self.assertFalse(self.state.is_speaking)

    def test_playback_failure_cancels_and_closes_producer(self):
        self.tts._play_audio_bytes.side_effect = RuntimeError('playback mock failed')
        workers = self.capture_tts_workers()
        with self.assertRaisesRegex(RuntimeError, 'playback mock failed'):
            self.tts.speak_streaming(iter(['one'] * 30))
        self.assertFalse(workers[0].is_alive())
        self.assertFalse(self.state.is_speaking)

    def test_edge_collection_has_timeout_and_async_cleanup(self):
        closed = threading.Event()
        async def stream():
            try:
                await asyncio.sleep(30)
                yield {'type':'audio', 'data':b'no network'}
            finally:
                closed.set()
        self.tts.edge_tts = types.SimpleNamespace(Communicate=lambda *a, **kw: types.SimpleNamespace(stream=stream))
        self.tts._TTS_NETWORK_TIMEOUT = .01
        started = time.perf_counter()
        self.assertIsNone(self.tts._edge_tts_to_bytes('text'))
        self.assertLess(time.perf_counter() - started, .4)
        self.assertTrue(closed.is_set())


class CancellationTests(PipelineCase):
    def test_interrupt_event_preserves_event_interface_and_counts_every_set(self):
        event = self.state.InterruptEvent()
        self.assertIsInstance(event, threading.Event)
        self.assertEqual(event.snapshot(), (0, False))
        self.assertFalse(event.wait(0))
        event.set()
        self.assertTrue(event.wait(0))
        event.set()
        self.assertEqual(event.snapshot(), (2, True))
        event.clear()
        self.assertEqual(event.snapshot(), (2, False))
        self.assertFalse(event.wait(0))
        event.set()
        self.assertEqual(event.generation, 3)

    def test_response_catches_set_then_clear_before_first_poll(self):
        response = self.state.PipelineCancellation()
        self.state.interrupt_event.set()
        self.state.interrupt_event.clear()
        self.assertTrue(response.is_set())
        self.assertTrue(response.wait(0))
        self.assertFalse(self.state.PipelineCancellation().is_set())

    def test_fresh_notification_token_observes_next_epoch_and_stays_latched(self):
        self.state.interrupt_event.set()
        ordinary = self.state.PipelineCancellation()
        fresh = self.state.PipelineCancellation.for_notification()
        nested = self.state.PipelineCancellation(fresh)
        self.assertFalse(fresh.wait(0))
        self.state.interrupt_event.clear()
        self.assertTrue(ordinary.is_set())
        self.assertFalse(fresh.is_set())
        self.state.interrupt_event.set()
        self.state.interrupt_event.clear()
        self.assertTrue(nested.is_set())
        self.assertTrue(fresh.wait(0))
        self.assertFalse(self.state.PipelineCancellation.for_notification().is_set())

    def test_private_cancellation_survives_global_clear(self):
        run = self.state.PipelineCancellation()
        self.state.interrupt_event.set()
        self.assertTrue(run.is_set())
        self.state.interrupt_event.clear()
        self.assertTrue(run.is_set())
        self.assertFalse(self.state.PipelineCancellation().is_set())

    def test_explicit_new_standalone_run_does_not_clear_global(self):
        self.state.interrupt_event.set()
        fresh = threading.Event()
        self.tts.tts_to_bytes = Mock(return_value=(b'mock', '.wav'))
        self.tts.speak('new standalone command', cancel_event=fresh)
        self.tts._play_audio_bytes.assert_called_once()
        self.assertTrue(self.state.interrupt_event.is_set())


if __name__ == '__main__':
    unittest.main(verbosity=2)

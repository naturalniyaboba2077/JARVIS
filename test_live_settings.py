"""Hot settings tests: temporary config, fake audio/models, no personal data."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

import jarvis
import jarvis_config as cfg
import jarvis_settings as live
import jarvis_llm as llm
import jarvis_tts as tts
import jarvis_stt as stt
import jarvis_telegram as telegram


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.path = Path(folder) / "config.json"
        self.stack.enter_context(patch.object(cfg, "CONFIG_PATH", self.path))
        self.stack.enter_context(patch.object(cfg, "ENV_OVERRIDES", {}))
        self.stack.enter_context(patch.dict(os.environ))
        self.manager = live.Settings()
        self.stack.enter_context(patch.object(live, "settings", self.manager))
        self.stack.enter_context(patch.object(jarvis, "_settings", self.manager))

    def test_save_persists_but_does_not_run_tools_or_change_active_env(self):
        os.environ["EDGE_RATE"] = "-5%"
        result = self.manager.save({"EDGE_RATE": "+5%"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "pending")
        self.assertEqual(json.loads(self.path.read_text())["EDGE_RATE"], "+5%")
        self.assertEqual(os.environ["EDGE_RATE"], "-5%")
        apply = Mock()
        self.assertTrue(self.manager.try_apply(apply))
        apply.assert_called_once_with({"EDGE_RATE": "+5%"})
        self.assertEqual(self.manager.snapshot()["state"], "applied")

    def test_invalid_json_and_values_do_not_schedule_activation(self):
        for contents, values in (("broken", {"EDGE_RATE": "+5%"}),
                                 ("{}", {"JARVIS_MIC_INDEX": "-1"}),
                                 ("{}", {"EDGE_RATE": "invalid"})):
            self.path.write_text(contents)
            self.assertFalse(self.manager.save(values)["ok"])
            self.assertEqual(self.path.read_text(), contents)
            apply = Mock()
            self.assertFalse(self.manager.try_apply(apply))
            apply.assert_not_called()

    def test_busy_work_defers_and_keeps_latest_saved_values(self):
        with self.manager.operation():
            self.manager.save({"EDGE_RATE": "+5%"})
            self.manager.save({"EDGE_RATE": "+8%", "EDGE_PITCH": "-5Hz"})
            self.assertFalse(self.manager.try_apply(Mock()))
        apply = Mock()
        self.assertTrue(self.manager.try_apply(apply))
        apply.assert_called_once_with({"EDGE_RATE": "+8%", "EDGE_PITCH": "-5Hz"})
        self.assertEqual(self.manager.snapshot()["applied_revision"], 2)

    def test_settings_do_not_clear_interrupt_or_confirmation(self):
        before = jarvis._state.interrupt_event.snapshot()
        with patch.object(jarvis._confirm, "clear") as clear:
            self.manager.save({"EDGE_RATE": "+5%"})
            self.manager.try_apply(Mock())
        clear.assert_not_called()
        self.assertEqual(jarvis._state.interrupt_event.snapshot(), before)

    def test_pending_telegram_send_defers_credentials_only(self):
        self.manager.save({"TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": "fixture"})
        apply = Mock()
        self.assertFalse(self.manager.try_apply(apply, lambda: {"kind": "telegram"}))
        self.assertIn("подтвердите", self.manager.snapshot()["message"])
        apply.assert_not_called()
        self.assertTrue(self.manager.try_apply(apply, lambda: None))

    def test_failure_is_visible_without_leaking_secret_and_retry_is_explicit(self):
        self.manager.save({"OPENROUTER_API_KEY": "secret-fixture"})
        self.assertFalse(self.manager.try_apply(Mock(side_effect=RuntimeError("secret-fixture"))))
        self.assertEqual(self.manager.snapshot()["state"], "error")
        self.assertNotIn("secret-fixture", str(self.manager.snapshot()))
        apply = Mock()
        self.assertFalse(self.manager.try_apply(apply))
        self.manager.save({})
        self.assertTrue(self.manager.try_apply(apply))

    def test_new_save_during_apply_is_not_lost(self):
        self.manager.save({"EDGE_RATE": "+5%"})
        self.manager.try_apply(lambda _: self.manager.save({"EDGE_RATE": "+8%"}))
        self.assertEqual(self.manager.snapshot()["state"], "pending")
        apply = Mock()
        self.manager.try_apply(apply)
        apply.assert_called_once_with({"EDGE_RATE": "+8%"})

    def test_env_overrides_win_and_only_names_are_exposed(self):
        with patch.object(cfg, "ENV_OVERRIDES", {"OPENROUTER_API_KEY": "env-secret"}):
            result = self.manager.save({"OPENROUTER_API_KEY": "saved-secret"})
            self.assertNotIn("env-secret", str(result))
            self.assertNotIn("saved-secret", str(result))
            self.assertEqual(result["overridden_keys"], ["OPENROUTER_API_KEY"])
            self.assertFalse(self.manager.try_apply(Mock()))
        self.assertEqual(json.loads(self.path.read_text())["OPENROUTER_API_KEY"], "saved-secret")

    def test_telegram_reads_active_values_until_activation(self):
        self.manager.enabled = True
        os.environ.update(TELEGRAM_API_ID="123", TELEGRAM_API_HASH="old", TELEGRAM_PHONE="+70000000001")
        self.manager.save({"TELEGRAM_API_ID": "456", "TELEGRAM_API_HASH": "new"})
        self.assertEqual(telegram._telegram_config()[:2], (123, "old"))
        self.manager.try_apply(lambda updates: os.environ.update(updates))
        self.assertEqual(telegram._telegram_config()[:2], (456, "new"))

    def test_blank_secrets_preserve_existing_value(self):
        self.path.write_text(json.dumps({"OPENROUTER_API_KEY": "fixture"}))
        self.manager.save({"OPENROUTER_API_KEY": ""})
        self.assertEqual(json.loads(self.path.read_text())["OPENROUTER_API_KEY"], "fixture")
        self.assertFalse(self.manager.try_apply(Mock()))

    def test_voice_test_does_not_silently_use_old_pending_voice(self):
        self.manager.save({"EDGE_RATE": "+5%"})
        with patch.object(jarvis.command_queue, "put") as put:
            self.assertFalse(jarvis.JarvisApi().test_voice()["ok"])
        put.assert_not_called()

    def test_callback_drops_during_apply_instead_of_deadlocking_listener_stop(self):
        self.manager.save({"EDGE_RATE": "+5%"})
        ran = Mock()
        wrapped = live.activity(ran, drop_during_apply=True)
        self.manager.try_apply(lambda updates: wrapped())
        ran.assert_not_called()

    def test_real_worker_lifetime_blocks_change_until_it_finishes(self):
        entered, release = threading.Event(), threading.Event()
        @live.activity
        def worker():
            entered.set()
            release.wait(3)
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            self.manager.save({"EDGE_RATE": "+5%"})
            self.assertFalse(self.manager.try_apply(Mock()))
        finally:
            release.set()
            thread.join(3)
        self.assertTrue(self.manager.try_apply(Mock()))

    def test_generator_stays_busy_until_closed(self):
        @live.activity
        def stream():
            yield "one"
            yield "two"
        result = stream()
        next(result)
        self.manager.save({"EDGE_RATE": "+5%"})
        self.assertFalse(self.manager.try_apply(Mock()))
        result.close()
        self.assertTrue(self.manager.try_apply(Mock()))

    def test_bindings_and_resource_reset_without_restart(self):
        fields = {"JARVIS_LLM": "lmstudio", "LM_STUDIO_MODEL": "fixture-model",
                  "LM_STUDIO_CODE_MODEL": "fixture-code", "LM_STUDIO_URL": "http://127.0.0.1:9999/v1/",
                  "OPENROUTER_API_KEY": "fixture-key", "JARVIS_FOLLOWUP_WINDOW": "23",
                  "JARVIS_FOLLOWUP_MODE": "strict", "EDGE_RATE": "+6%", "TTS_ENGINE": "edge",
                  "WHISPER_MODEL": "fixture-small", "JARVIS_PROJECT_ROOTS": "C:/fixture",
                  "SESSION_MEMORY": "on", "LM_STUDIO_CONTEXT": "4096"}
        for key in fields:
            os.environ.pop(key, None)
        # Restore all mutable module attributes even if an assertion fails.
        for module in (jarvis, cfg, llm, tts, stt):
            for name, value in list(vars(module).items()):
                if name.isupper() or name in {"_lmstudio_client", "_openrouter_client", "_whisper_model", "_whisper_tried", "_piper_voice", "_piper_tried"}:
                    self.stack.enter_context(patch.object(module, name, value))
        self.stack.enter_context(patch.object(tts, "_TTS_INSTANT_CACHE", {"old": "old.wav"}))
        lmclient, cloudclient = Mock(), Mock()
        llm._lmstudio_client, llm._openrouter_client = lmclient, cloudclient
        stt._whisper_model, stt._whisper_tried = object(), True
        history = list(jarvis.conversation_history)
        with patch.object(jarvis._dashboard, "service"), patch.object(jarvis, "main") as main:
            live.apply_runtime(jarvis, fields)
        self.assertEqual(jarvis.LM_STUDIO_MODEL, llm.LM_STUDIO_MODEL)
        self.assertEqual(llm.LM_STUDIO_MODEL, "fixture-model")
        self.assertEqual(llm.LM_STUDIO_URL, "http://127.0.0.1:9999/v1")
        self.assertEqual(jarvis.EDGE_RATE, tts.EDGE_RATE)
        self.assertEqual(tts.EDGE_RATE, "+6%")
        self.assertEqual((jarvis.FOLLOWUP_WINDOW, cfg.FOLLOWUP_WINDOW, tts.FOLLOWUP_WINDOW), (23.0,) * 3)
        self.assertIsNone(stt._whisper_model)
        self.assertFalse(stt._whisper_tried)
        self.assertFalse(tts._TTS_INSTANT_CACHE)
        lmclient.close.assert_called_once()
        cloudclient.close.assert_called_once()
        self.assertEqual(os.environ["JARVIS_PROJECT_ROOTS"], "C:/fixture")
        self.assertTrue(jarvis.SESSION_MEMORY)
        self.assertEqual(jarvis.conversation_history, history)
        main.assert_not_called()

    def test_hardware_failure_leaves_bindings_and_environment_unchanged(self):
        os.environ["EDGE_RATE"] = "-5%"
        previous = tts.EDGE_RATE
        hook = Mock(side_effect=live.ApplyError("Fixture microphone unavailable"))
        with self.assertRaises(live.ApplyError):
            live.apply_runtime(jarvis, {"EDGE_RATE": "+8%", "JARVIS_MIC_INDEX": "999999"}, hook)
        self.assertEqual(os.environ["EDGE_RATE"], "-5%")
        self.assertEqual(tts.EDGE_RATE, previous)

    def test_every_editable_key_has_a_runtime_owner_or_dynamic_consumer(self):
        covered = set()
        for groups in (live.TEXT_BINDINGS, live.FLOAT_BINDINGS):
            for fields in groups.values():
                covered.update(fields)
        covered.update({"LM_STUDIO_CONTEXT", "LM_STUDIO_AUTOLOAD", "SESSION_MEMORY",
                        "JARVIS_OVERLAY", "JARVIS_MIC_INDEX", "JARVIS_PROJECT_ROOTS",
                        "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_PHONE",
                        "TELEGRAM_REPORT_BOT_TOKEN", "TELEGRAM_REPORT_CHAT_ID"})
        self.assertEqual(covered, cfg.WRITABLE_SETTING_KEYS)

    def _run_audio_change(self, changes, fail_new=False):
        manager = self.manager
        manager.save(changes)
        owner = threading.get_ident()
        listeners, stops, active = [], [], [False]
        class Mic:
            def __init__(self, device_index=None):
                self.index = device_index
            def __enter__(self):
                self_test.assertEqual(threading.get_ident(), owner)
                self_test.assertFalse(active[0], "No second PortAudio stream may open")
                if fail_new and self.index == 1:
                    raise OSError("fixture unavailable")
                return self
            def __exit__(self, *args):
                pass
        class Recognizer:
            energy_threshold = 300
            def adjust_for_ambient_noise(self, *args, **kwargs):
                pass
            def listen_in_background(self, mic, callback, phrase_time_limit):
                self_test.assertEqual(threading.get_ident(), owner)
                self_test.assertFalse(active[0])
                active[0] = True
                listeners.append((mic.index, phrase_time_limit))
                def stop(wait_for_stop):
                    stops.append(wait_for_stop)
                    active[0] = False
                    if wait_for_stop:
                        callback(None, object())  # dropped during apply, no deadlock/STT
                return stop
        class NoThread:
            def __init__(self, *args, **kwargs): pass
            def start(self): pass
        self_test = self
        for key in changes:
            os.environ.pop(key, None)
        recognizer = Recognizer()
        commands = queue.Queue()
        commands.put("выход")
        with ExitStack() as stack:
            for name in ("start_overlay", "stop_overlay", "start_tts_cache_warmup", "ui_call", "ui_state", "speak"):
                stack.enter_context(patch.object(jarvis, name))
            stack.enter_context(patch.object(jarvis, "sr", types.SimpleNamespace(Recognizer=lambda: recognizer, Microphone=Mic)))
            stack.enter_context(patch.object(jarvis, "MeteredMicrophone", Mic))
            stack.enter_context(patch.object(jarvis, "_select_mic", return_value=0))
            stack.enter_context(patch.object(jarvis, "_microphone_names_cache", ("Fixture 0", "Fixture 1")))
            stack.enter_context(patch.object(jarvis, "command_queue", commands))
            stack.enter_context(patch.object(jarvis, "_stop_event", threading.Event()))
            stack.enter_context(patch.object(jarvis, "_startup_mark", None))
            stack.enter_context(patch.object(jarvis, "SESSION_MEMORY", False))
            stack.enter_context(patch.object(jarvis, "PHRASE_TIME_LIMIT", 45.0))
            stack.enter_context(patch.object(cfg, "PHRASE_TIME_LIMIT", 45.0))
            stack.enter_context(patch.object(jarvis, "PAUSE_THRESHOLD", 2.6))
            stack.enter_context(patch.object(cfg, "PAUSE_THRESHOLD", 2.6))
            stack.enter_context(patch.object(jarvis, "transcribe_speech", side_effect=AssertionError("No STT during apply")))
            stack.enter_context(patch.object(jarvis.pygame.mixer, "init"))
            stack.enter_context(patch.object(jarvis.pygame.mixer, "quit"))
            stack.enter_context(patch.object(jarvis.threading, "Thread", NoThread))
            stack.enter_context(patch.object(jarvis._feat, "start_reminder_worker"))
            stack.enter_context(patch.object(jarvis._feat, "arm_hotkey_listen"))
            stack.enter_context(patch.object(jarvis._ui, "_ui_window", object()))
            jarvis.run_assistant()
        return listeners, stops, recognizer

    def test_microphone_reconnect_owned_by_main_loop_without_overlap(self):
        listeners, stops, _ = self._run_audio_change({"JARVIS_MIC_INDEX": "1", "JARVIS_PHRASE_TIME_LIMIT": "30"})
        self.assertEqual(listeners, [(0, 45.0), (1, 30.0)])
        self.assertEqual(stops, [True, False])
        self.assertEqual(self.manager.snapshot()["state"], "applied")

    def test_failed_device_restores_old_listener_and_runtime(self):
        listeners, stops, _ = self._run_audio_change({"JARVIS_MIC_INDEX": "1"}, fail_new=True)
        self.assertEqual(listeners, [(0, 45.0), (0, 45.0)])
        self.assertEqual(stops, [True, False])
        self.assertEqual(self.manager.snapshot()["state"], "error")
        self.assertNotIn("JARVIS_MIC_INDEX", os.environ)

    def test_pause_change_does_not_restart_microphone(self):
        listeners, stops, recognizer = self._run_audio_change({"JARVIS_PAUSE_THRESHOLD": "1.2"})
        self.assertEqual(listeners, [(0, 45.0)])
        self.assertEqual(stops, [False])
        self.assertEqual(recognizer.pause_threshold, 1.2)

    def test_voice_settings_share_revision_order_with_panel(self):
        with patch.object(jarvis._state.interrupt_event, "is_set", return_value=False):
            self.manager.save({"JARVIS_FOLLOWUP_WINDOW": "10", "JARVIS_FOLLOWUP_MODE": "off"})
            reply = jarvis.handle_followup_setting("слушай без обращения 30 секунд")
        self.assertIn("30 секунд", reply)
        apply = Mock()
        self.manager.try_apply(apply)
        apply.assert_called_once_with({"JARVIS_FOLLOWUP_WINDOW": "30", "JARVIS_FOLLOWUP_MODE": "smart"})

    def test_hotkey_reads_current_window_without_registering_again(self):
        keyboard = types.SimpleNamespace(add_hotkey=Mock())
        current = [10.0]
        commands = queue.Queue()
        with patch.dict("sys.modules", {"keyboard": keyboard}):
            jarvis._feat.arm_hotkey_listen(commands, wake_seconds=lambda: current[0])
        handler = keyboard.add_hotkey.call_args.args[1]
        handler()
        current[0] = 20.0
        handler()
        self.assertEqual(commands.get_nowait(), ("__HOTKEY__", 10.0))
        self.assertEqual(commands.get_nowait(), ("__HOTKEY__", 20.0))
        keyboard.add_hotkey.assert_called_once()


if __name__ == "__main__":
    unittest.main()

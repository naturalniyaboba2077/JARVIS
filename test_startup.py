"""Startup regressions with fake native surfaces; no audio or external services."""

import ast
import hashlib
from pathlib import Path
import threading
import types
import unittest
import tempfile
from unittest.mock import Mock, patch

import jarvis_bootstrap as boot


def core_fixture():
    state = types.SimpleNamespace(assistant_ready=False, interrupt_event=threading.Event())
    api = Mock()
    api.runtime_status.side_effect = lambda: {"ready": state.assistant_ready}
    return types.SimpleNamespace(_state=state, _stop_event=threading.Event(),
        _ui=types.SimpleNamespace(_ui_window=None), JarvisApi=Mock(return_value=api),
        run_assistant=Mock())


class StartupTests(unittest.TestCase):
    def test_entry_point_dispatches_before_heavy_imports(self):
        nodes = ast.parse(Path("jarvis.py").read_text(encoding="utf-8")).body
        first = nodes[0]
        self.assertIsInstance(first, ast.If)
        self.assertIn("__main__", ast.unparse(first.test))
        self.assertIn("jarvis_bootstrap", ast.unparse(first))
        self.assertTrue(any(isinstance(n, ast.Raise) for n in first.body))

    def test_bridge_is_explicit_and_has_identical_public_signatures(self):
        tree = ast.parse(Path("jarvis.py").read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "JarvisApi")
        expected = {n.name: ast.unparse(n.args) for n in cls.body
                    if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}
        proxy = ast.parse(Path("jarvis_bootstrap.py").read_text(encoding="utf-8"))
        actual_cls = next(n for n in proxy.body if isinstance(n, ast.ClassDef))
        actual = {n.name: ast.unparse(n.args) for n in actual_cls.body
                  if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}
        self.assertEqual(actual, expected)

    def test_constructing_and_polling_does_not_load_backend(self):
        load = Mock()
        bridge = boot.StartupApi(loader=load)
        self.assertFalse(bridge.runtime_status()["ready"])
        self.assertEqual(bridge.audio_levels()["output"], 0)
        load.assert_not_called()

    def test_no_commands_or_settings_written_before_ready(self):
        bridge = boot.StartupApi(loader=Mock())
        for name, args in (("send_command", ("fixture",)), ("save_settings", ({},)),
                           ("confirm_send", ("email", "fixture", True)), ("test_voice", ())):
            with self.assertRaises(RuntimeError):
                getattr(bridge, name)(*args)
        bridge._loader.assert_not_called()

    def test_loaded_event_starts_worker_only_once(self):
        bridge = boot.StartupApi(loader=Mock())
        with patch.object(boot.threading, "Thread") as thread:
            bridge._loaded()
            bridge._loaded()
            thread.assert_called_once()
            thread.return_value.start.assert_called_once()
        bridge._loader.assert_not_called()

    def test_close_before_loaded_never_starts_loader(self):
        bridge = boot.StartupApi(loader=Mock())
        bridge._window = Mock()
        bridge.close()
        bridge._loaded()
        bridge._run_backend()
        bridge._loader.assert_not_called()
        bridge._window.destroy.assert_called_once()

    def test_close_during_import_never_attaches_or_starts_core(self):
        core = core_fixture()
        bridge = boot.StartupApi()
        def load():
            bridge._closed_event()
            return core
        bridge._loader = load
        bridge._run_backend()
        core.run_assistant.assert_not_called()
        core.JarvisApi.assert_not_called()

    def test_attach_ready_forward_and_close_cancel(self):
        core = core_fixture()
        bridge = boot.StartupApi(loader=lambda: core)
        bridge._window = Mock()
        def running():
            self.assertIs(core._ui._ui_window, bridge._window)
            with self.assertRaises(RuntimeError):
                bridge.send_command("not ready")
            core._state.assistant_ready = True
            self.assertTrue(bridge.runtime_status()["ready"])
            bridge.send_command("fixture")
            core.JarvisApi.return_value.send_command.assert_called_once_with("fixture")
            bridge._closed_event()
            self.assertTrue(core._stop_event.is_set())
            self.assertTrue(core._state.interrupt_event.is_set())
        core.run_assistant.side_effect = running
        bridge._run_backend()
        self.assertFalse(bridge._error)

    def test_failed_import_is_visible_and_does_not_expose_exception_secrets(self):
        bridge = boot.StartupApi(loader=Mock(side_effect=ValueError("private-detail")))
        bridge._run_backend()
        result = bridge.runtime_status()
        self.assertFalse(result["ready"])
        self.assertTrue(result["startup"]["error"])
        self.assertNotIn("private-detail", str(result))

    def test_native_controls_work_before_core(self):
        bridge = boot.StartupApi(loader=Mock())
        bridge._window = Mock()
        for name in ("minimize", "maximize", "restore"):
            self.assertTrue(getattr(bridge, name)())
            getattr(bridge._window, name).assert_called_once()
        bridge._loader.assert_not_called()

    def test_warmup_is_never_inline_in_run_assistant(self):
        tree = ast.parse(Path("jarvis.py").read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run_assistant")
        calls = [n.func.id for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        self.assertIn("start_tts_cache_warmup", calls)
        self.assertNotIn("prewarm_tts_cache", calls)
        self.assertNotIn("get_obsidian_memory", calls)

    def test_native_loop_starts_without_backend_func(self):
        class Event:
            def __init__(self):
                self.handlers = []
            def __iadd__(self, handler):
                self.handlers.append(handler)
                return self
        view = Mock()
        events = types.SimpleNamespace(**{k: Event() for k in (
            "shown", "loaded", "closed", "closing", "minimized", "maximized", "restored")})
        view.create_window.return_value.events = events
        bridge = boot.StartupApi(loader=Mock())
        with patch.object(boot.threading, "Thread") as thread:
            def start():
                bridge._loader.assert_not_called()
                events.shown.handlers[0]()
                thread.assert_not_called()
                events.loaded.handlers[0]()
                thread.assert_called_once()
            view.start.side_effect = start
            boot.open_window(view, bridge)
        view.start.assert_called_once_with()
        self.assertTrue(bridge._closed)


class VoiceStartupTests(unittest.TestCase):
    def test_every_voice_engine_uses_one_background_worker(self):
        import jarvis_tts as tts
        for engine in ("edge", "piper", "xtts"):
            with self.subTest(engine=engine), patch.object(tts, "_effective_tts_engine", return_value=engine), \
                    patch.object(tts, "_tts_warmup_thread", None), \
                    patch.object(tts, "prewarm_tts_cache") as warm, \
                    patch.object(tts.threading, "Thread") as thread:
                thread.return_value.is_alive.return_value = True
                tts.start_tts_cache_warmup()
                tts.start_tts_cache_warmup()
                warm.assert_not_called()
                thread.assert_called_once()
                thread.return_value.start.assert_called_once()

    def test_piper_concurrent_load_waits_for_the_same_voice(self):
        import jarvis_tts as tts
        entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
        voice, results = object(), []
        def load(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return voice
        fake = types.SimpleNamespace(PiperVoice=types.SimpleNamespace(load=Mock(side_effect=load)))
        def second():
            second_started.set()
            results.append(tts._load_piper())
        with patch.dict("sys.modules", {"piper": fake}), patch.object(tts, "_piper_voice", None), \
                patch.object(tts, "_piper_tried", False):
            first = threading.Thread(target=lambda: results.append(tts._load_piper()))
            other = threading.Thread(target=second)
            first.start()
            try:
                self.assertTrue(entered.wait(3))
                other.start()
                self.assertTrue(second_started.wait(3))
                self.assertFalse(tts._piper_tried)  # not published half-loaded
            finally:
                release.set()
                first.join(3)
                if other.ident is not None:
                    other.join(3)
            self.assertEqual(results, [voice, voice])
            fake.PiperVoice.load.assert_called_once()

    def test_cache_is_published_before_piper_load(self):
        import jarvis_tts as tts
        phrase = "Fixture"
        voice_id = (f"{tts.PIPER_MODEL_PATH.stem}:{tts.PIPER_LENGTH_SCALE}:"
                    f"{tts.PIPER_NOISE_SCALE}:{tts.PIPER_NOISE_W_SCALE}:{tts.VOICE_STYLE}")
        key = hashlib.md5(f"piper:{voice_id}:{phrase}".encode()).hexdigest()[:12]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(tts, "_TTS_CACHE_DIR", Path(directory)), \
                patch.object(tts, "_TTS_INSTANT_CACHE", {}), \
                patch.object(tts, "INSTANT_PHRASES", [phrase]), \
                patch.object(tts, "_effective_tts_engine", return_value="piper"), \
                patch.object(tts, "_piper_available", return_value=True), \
                patch.object(tts, "tts_to_bytes") as synth:
            path = Path(directory) / f"{key}.wav"
            path.write_bytes(b"fixture")
            with patch.object(tts, "_load_piper", side_effect=lambda: self.assertEqual(tts._TTS_INSTANT_CACHE[phrase], str(path))):
                tts.prewarm_tts_cache()
            synth.assert_not_called()


if __name__ == "__main__":
    unittest.main()

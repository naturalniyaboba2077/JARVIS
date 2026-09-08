"""Offline desktop bridge regressions. Run only through run_tests.py.

Network, mail, Telegram, native windows, playback and microphone IO are mocked.
Temporary fixture files exercise real preview/undo validation, not personal data.
"""

import concurrent.futures
import json
from pathlib import Path
import queue
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import jarvis
import jarvis_confirm as confirm
import jarvis_dashboard as dashboard
import jarvis_fileops as fileops
import jarvis_mail as mail
import jarvis_state as state
import jarvis_store as store
import jarvis_telegram as telegram
from jarvis_audio_meter import MeteredStream, pcm_level


class RedesignTests(unittest.TestCase):
    def setUp(self):
        confirm.clear()
        state.interrupt_event.clear()
        state.microphone_enabled = True
        state.microphone_ready = True
        state.microphone_resumed_at = 0
        self.api = jarvis.JarvisApi()
        self.queue = queue.Queue()
        self.patches = [patch.object(jarvis, "command_queue", self.queue),
                        patch.object(jarvis, "ui_msg", Mock()),
                        patch.object(mail._feat, "gmail_send", Mock(return_value="Письмо отправлено")),
                        patch.object(telegram, "_telegram_send_resolved", Mock(return_value="Отправлено"))]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(confirm.clear)
        self.addCleanup(state.interrupt_event.clear)
        self.addCleanup(lambda: [store.cancel_timer(item["id"]) for item in store.timer_snapshot()])

    def stage(self, body="Полный текст письма"):
        mail.email_request_send("fixture@example.invalid", "Тема", body)
        return confirm.snapshot()

    def test_confirmation_normalizes_punctuation_and_case(self):
        for text in ("подтверждаю.", "Подтверждаю!", " ДА… ", "«подтверждаю»"):
            with self.subTest(text=text):
                self.stage()
                self.assertEqual(mail.email_confirm_pending(text), "Письмо отправлено")
                self.assertIsNone(state.pending_email_send)
        self.assertEqual(mail._feat.gmail_send.call_count, 4)

    def test_punctuated_cancel_cannot_be_followed_by_accidental_yes(self):
        self.stage()
        self.assertIn("отменил", mail.email_confirm_pending("отмена."))
        self.assertIsNone(mail.email_confirm_pending("да"))
        mail._feat.gmail_send.assert_not_called()

    def test_mentions_and_negation_are_not_confirmation(self):
        for text in ("не подтверждаю", "да, но сначала исправь", "почему нужно сказать да"):
            self.stage()
            self.assertIn("Ожидаю", mail.email_confirm_pending(text))
        mail._feat.gmail_send.assert_not_called()

    def test_new_invalid_email_clears_old_pending(self):
        self.stage()
        mail.email_request_send("invalid", "", "")
        self.assertIsNone(confirm.snapshot())
        self.assertIsNone(mail.email_confirm_pending("да"))

    def test_expired_request_fails_closed(self):
        self.stage()
        state.pending_email_send["deadline"] = time.monotonic() - 1
        self.assertIn("истёк", mail.email_confirm_pending("да"))
        mail._feat.gmail_send.assert_not_called()

    def test_snapshot_expires_pending(self):
        self.stage()
        state.pending_email_send["deadline"] = 0
        self.assertIsNone(confirm.snapshot())
        self.assertIsNone(state.pending_email_send)

    def test_stale_card_cannot_approve_replacement(self):
        old = self.stage()
        new = self.stage("Другое письмо")
        self.assertNotEqual(old["id"], new["id"])
        self.assertFalse(self.api.confirm_send("email", old["id"], True)["ok"])
        self.assertEqual(confirm.snapshot()["id"], new["id"])
        self.assertTrue(self.queue.empty())

    def test_double_click_and_voice_send_only_once(self):
        item = self.stage()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: mail.email_confirm_pending("да.", item["id"]), range(8)))
        self.assertEqual(results.count("Письмо отправлено"), 1)
        mail._feat.gmail_send.assert_called_once()

    def test_send_is_queued_but_cancel_is_immediate(self):
        item = self.stage()
        self.assertTrue(self.api.confirm_send("email", item["id"], True)["ok"])
        mail._feat.gmail_send.assert_not_called()
        command = self.queue.get_nowait()
        self.assertEqual(command, ("__CONFIRM__", "email", item["id"]))
        self.assertTrue(self.api.confirm_send("email", item["id"], False)["ok"])
        self.assertIn("не действует", jarvis._execute_ui_request(command))
        mail._feat.gmail_send.assert_not_called()

    def test_ui_queue_dispatch_sends_claimed_payload_once(self):
        item = self.stage()
        self.api.confirm_send("email", item["id"], True)
        command = self.queue.get_nowait()
        self.assertEqual(jarvis._execute_ui_request(command), "Письмо отправлено")
        self.assertIn("не действует", jarvis._execute_ui_request(command))
        mail._feat.gmail_send.assert_called_once()

    def test_wrong_kind_or_nonboolean_approval_rejected(self):
        item = self.stage()
        for kind, approved in (("telegram", True), ("email", "false"), ("other", True)):
            self.assertFalse(self.api.confirm_send(kind, item["id"], approved)["ok"])

    def test_stop_cancels_pending_and_interrupts(self):
        self.stage()
        self.api.stop()
        self.assertTrue(state.interrupt_event.is_set())
        self.assertIsNone(confirm.snapshot())
        self.assertEqual(self.queue.get_nowait(), "__CANCEL__")

    def test_cancel_during_telegram_resolution_does_not_rearm(self):
        entered, release = threading.Event(), threading.Event()
        def resolve(*args):
            entered.set()
            self.assertTrue(release.wait(2))
            return {"chat": "Fixture", "peer": object()}
        with patch.object(telegram, "_telegram_authorized_operation", side_effect=resolve):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(telegram.telegram_request_send, "Fixture", "Message")
                self.assertTrue(entered.wait(2))
                confirm.clear()
                release.set()
                self.assertIn("отменена", future.result(timeout=2))
        self.assertIsNone(confirm.snapshot())

    def test_pending_preview_is_complete_and_has_no_peer(self):
        text = "Полный текст. " * 100
        confirm.stage("telegram", {"chat": "Fixture (ID 123)", "text": text, "peer": object()})
        item = confirm.snapshot()
        self.assertEqual(item["body"], text)
        self.assertNotIn("peer", json.dumps(item, ensure_ascii=False))

    def test_text_command_is_logged_once_and_queued(self):
        self.api.send_command("который час")
        jarvis.ui_msg.assert_called_once_with("user", "который час", source="text")
        self.assertEqual(self.queue.get_nowait(), "который час")

    def test_meter_measures_pcm_not_a_clock(self):
        self.assertEqual(pcm_level(bytes(200)), 0)
        signal = struct.pack("<100h", *([16000] * 100))
        self.assertGreater(pcm_level(signal), 0.5)
        self.assertLessEqual(pcm_level(signal), 1)
        self.assertEqual(pcm_level(b"x", 2), 0)

    def test_paused_stream_discards_audio_without_reopening_device(self):
        signal = struct.pack("<10h", *([16000] * 10))
        stream = Mock(read=Mock(return_value=signal))
        wrapped = MeteredStream(stream, state)
        self.assertEqual(wrapped.read(10), signal)
        self.api.set_microphone_enabled(False)
        self.assertEqual(wrapped.read(10), bytes(len(signal)))
        self.assertEqual(self.api.audio_levels()["input"], 0)
        self.api.set_microphone_enabled(True)
        self.assertGreater(state.microphone_resumed_at, 0)
        self.assertEqual(wrapped.read(10), signal)
        stream.close.assert_not_called()

    def test_paused_callback_never_transcribes(self):
        state.microphone_enabled = False
        with patch.object(jarvis, "transcribe_speech") as transcribe:
            jarvis.callback(Mock(), Mock())
        transcribe.assert_not_called()

    def test_muting_during_transcription_drops_result(self):
        def transcribe(*args):
            self.api.set_microphone_enabled(False)
            self.api.set_microphone_enabled(True)
            return "Джарвис открой браузер"
        with patch.object(jarvis, "transcribe_speech", side_effect=transcribe), \
                patch.object(jarvis, "_audio_duration", return_value=1):
            jarvis.callback(Mock(), Mock())
        self.assertTrue(self.queue.empty())

    def test_status_is_local_and_does_not_return_config_secrets(self):
        with patch.object(jarvis, "_ollama_probe", side_effect=AssertionError("No network")), \
                patch.object(jarvis, "_read_config_file", side_effect=AssertionError("No config")):
            data = self.api.runtime_status()
        self.assertNotIn("OPENROUTER_API_KEY", json.dumps(data))
        self.assertIn("services", data)

    def test_timers_cancel_real_wait_and_keep_other_timer(self):
        alarm = Mock()
        first = store.set_timer(30, "Первый", alarm)
        second = store.set_timer(30, "Второй", alarm)
        self.assertTrue(self.api.cancel_timer(first)["ok"])
        self.assertFalse(self.api.cancel_timer(first)["ok"])
        snapshot = {item["id"]: item for item in store.timer_snapshot()}
        self.assertEqual(snapshot[first]["status"], "cancelled")
        self.assertEqual(snapshot[second]["status"], "running")
        alarm.assert_not_called()

    def test_file_preview_and_stale_identity(self):
        with tempfile.TemporaryDirectory(prefix="jarvis-ui-fixture-") as directory:
            path = Path(directory) / "note.md"
            path.write_text("<script>fixture</script>", encoding="utf-8")
            card_id = dashboard.register_file(path)
            result = self.api.preview_file(card_id)
            self.assertEqual(result["text"], "<script>fixture</script>")
            path.write_text("New content", encoding="utf-8")
            self.assertFalse(self.api.preview_file(card_id)["ok"])
        self.assertFalse(self.api.preview_file("../../token.json")["ok"])

    def test_undo_card_is_bound_to_exact_last_change(self):
        with tempfile.TemporaryDirectory(prefix="jarvis-ui-fixture-") as directory:
            root = Path(directory)
            fileops.write_versioned(root, root / "a.txt", "A")
            seq = fileops.pending_changes(root)[0]["seq"]
            fileops.write_versioned(root, root / "b.txt", "B")
            self.assertIn("другая правка", fileops.undo_last(root, expected_seq=seq))
            self.assertTrue((root / "a.txt").exists())
            self.assertTrue((root / "b.txt").exists())

    def test_diff_preview_validates_backup_and_current_file(self):
        with tempfile.TemporaryDirectory(prefix="jarvis-ui-fixture-") as directory:
            root = Path(directory)
            path = root / "change.txt"
            path.write_text("before\n", encoding="utf-8")
            fileops.write_versioned(root, path, "after\n")
            change = fileops.pending_changes(root)[0]
            text = fileops.preview_change(root, change["seq"])
            self.assertIn("-before", text)
            self.assertIn("+after", text)
            self.assertEqual(path.read_text(encoding="utf-8"), "after\n")
            path.write_text("manual edit", encoding="utf-8")
            with self.assertRaises(fileops.FileConflict):
                fileops.preview_change(root, change["seq"])

    def test_raster_card_can_exceed_project_write_limit(self):
        with tempfile.TemporaryDirectory(prefix="jarvis-ui-fixture-") as directory:
            path = Path(directory) / "fixture.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(400_000))
            card = dashboard.register_file(path)
            self.assertIsNotNone(card)
            self.assertTrue(self.api.preview_file(card)["image"].startswith("data:image/png;base64,"))
            with self.assertRaises(fileops.FileConflict):
                fileops.read_project_bytes(path.parent, path.name)

    def test_disconnected_microphone_updates_status(self):
        stream = Mock(read=Mock(side_effect=OSError("synthetic disconnect")))
        with self.assertRaises(OSError):
            MeteredStream(stream, state).read(10)
        self.assertFalse(state.microphone_ready)
        self.assertIn("Потеряна связь", state.microphone_error)

    def test_compact_mode_restores_previous_size(self):
        window = Mock(width=1100, height=800)
        with patch.object(jarvis._ui, "_ui_window", window), \
                patch.object(jarvis, "_set_native_window_state", return_value=True):
            self.assertTrue(self.api.set_compact_mode(True)["compact"])
            window.resize.assert_called_with(440, 330)
            self.assertFalse(self.api.set_compact_mode(False)["compact"])
            window.resize.assert_called_with(1100, 800)


if __name__ == "__main__":
    unittest.main(verbosity=2)

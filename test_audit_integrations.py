"""C1-C4 regressions. Stdlib only; no real config, session, client or network.

Run: python -B test_audit_integrations.py
Production modules execute under temporary __file__ paths with mocked imports.
Coroutines are driven synchronously: the fake clients never yield actual I/O.
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, Mock, patch


SOURCE_DIR = Path(__file__).resolve().parent


def source_module(name, directory):
    module = types.ModuleType(name)
    module.__file__ = str(directory / (name + ".py"))
    source = SOURCE_DIR / (name + ".py")
    exec(compile(source.read_text(encoding="utf-8-sig"), str(source), "exec"),
         module.__dict__)
    return module


def module_stub(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


def complete(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as done:
        return done.value
    finally:
        coroutine.close()
    raise AssertionError("Fake client unexpectedly attempted asynchronous I/O")


class InputPeerUser:
    def __init__(self, user_id, access_hash):
        self.user_id, self.access_hash = user_id, access_hash


class InputPeerChat:
    def __init__(self, chat_id):
        self.chat_id = chat_id


class InputPeerChannel:
    def __init__(self, channel_id, access_hash):
        self.channel_id, self.access_hash = channel_id, access_hash


def input_peer(entity, allow_self=True):
    if entity.kind == "chat":
        return InputPeerChat(entity.id)
    if entity.access_hash is None:
        raise TypeError("No usable access hash")
    if entity.kind == "channel":
        return InputPeerChannel(entity.id, entity.access_hash)
    return InputPeerUser(entity.id, entity.access_hash)


def dialog(name, ident, username=None, kind="user", access_hash=123456):
    entity = types.SimpleNamespace(id=ident, username=username, kind=kind,
                                   access_hash=access_hash)
    marked_id = (-(1000000000000 + ident) if kind == "channel"
                 else -ident if kind == "chat" else ident)
    return types.SimpleNamespace(name=name, id=marked_id, entity=entity)


class ResolvePhoneRequest:
    def __init__(self, phone):
        self.phone = phone


class GetFullUserRequest:
    def __init__(self, user):
        self.user = user


class PhoneNotOccupiedError(Exception):
    pass


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="jarvis-audit-integrations-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        for target in ("socket.socket.connect", "socket.create_connection"):
            guard = patch(target, side_effect=AssertionError("Network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        # Never inherit live integration credentials or timing overrides.
        getenv = patch("os.getenv", side_effect=lambda key, default=None: default)
        getenv.start()
        self.addCleanup(getenv.stop)


class ConfigTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.cfg = source_module("jarvis_config", self.directory)
        self.path = self.cfg.CONFIG_PATH
        self.backup = self.path.with_name(self.path.name + ".bak")
        self.original = b'{"OLLAMA_MODEL": "old", "JARVIS_LLM": "cloud"}\n'

    def seed(self):
        self.path.write_bytes(self.original)

    def assert_no_temporaries(self):
        self.assertEqual(list(self.directory.glob(".*.tmp")), [])

    def test_new_config_and_public_read_api(self):
        ok, _ = self.cfg._write_config_file({"TTS_ENGINE": "edge"})
        self.assertTrue(ok)
        self.assertEqual(self.cfg._read_config_file(), {"TTS_ENGINE": "edge"})
        self.assert_no_temporaries()

    def test_update_preserves_fields_and_exact_backup(self):
        self.seed()
        ok, _ = self.cfg._write_config_file({"TTS_ENGINE": "piper"})
        self.assertTrue(ok)
        self.assertEqual(self.cfg._read_config_file(), {
            "OLLAMA_MODEL": "old", "JARVIS_LLM": "cloud", "TTS_ENGINE": "piper"})
        self.assertEqual(self.backup.read_bytes(), self.original)
        self.assert_no_temporaries()

    def test_corrupt_or_non_object_json_is_never_overwritten(self):
        for content in (b'{', b'[]', b'null', b'"text"'):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                self.backup.write_bytes(self.original)
                ok, message = self.cfg._write_config_file({"TTS_ENGINE": "edge"})
                self.assertFalse(ok)
                self.assertIn("не изменены", message)
                self.assertEqual(self.path.read_bytes(), content)
                self.assertEqual(self.backup.read_bytes(), self.original)
                self.assert_no_temporaries()

    def test_invalid_setting_does_not_touch_config_or_backup(self):
        self.seed()
        ok, _ = self.cfg._write_config_file({"TTS_ENGINE": "unsupported"})
        self.assertFalse(ok)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse(self.backup.exists())

    def test_fsync_failure_preserves_config_and_backup(self):
        self.seed()
        self.backup.write_bytes(b'{"previous": true}')
        with patch.object(self.cfg.os, "fsync", side_effect=OSError("disk full")):
            ok, _ = self.cfg._write_config_file({"TTS_ENGINE": "edge"})
        self.assertFalse(ok)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.backup.read_bytes(), b'{"previous": true}')
        self.assert_no_temporaries()

    def test_main_replace_failure_preserves_old_config(self):
        self.seed()
        real_replace = os.replace

        def fail_main(source, destination):
            if Path(destination) == self.path:
                raise PermissionError("fixture sharing violation")
            return real_replace(source, destination)

        with patch.object(self.cfg.os, "replace", side_effect=fail_main):
            ok, _ = self.cfg._write_config_file({"TTS_ENGINE": "edge"})
        self.assertFalse(ok)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.backup.read_bytes(), self.original)
        self.assert_no_temporaries()

    def test_partial_temporary_write_preserves_previous_config(self):
        self.seed()
        create_temp = self.cfg.tempfile.NamedTemporaryFile

        def partial_file(*args, **kwargs):
            stream = create_temp(*args, **kwargs)
            write = stream.write

            def partial_write(content):
                write(content[:2])
                raise OSError("fixture interrupted write")

            stream.write = partial_write
            return stream

        with patch.object(self.cfg.tempfile, "NamedTemporaryFile", side_effect=partial_file):
            ok, _ = self.cfg._write_config_file({"TTS_ENGINE": "edge"})
        self.assertFalse(ok)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assert_no_temporaries()

    def test_backup_failure_aborts_update(self):
        self.seed()
        self.backup.write_bytes(b'{"previous": true}')
        with patch.object(self.cfg.os, "replace", side_effect=OSError("backup denied")):
            ok, _ = self.cfg._write_config_file({"TTS_ENGINE": "edge"})
        self.assertFalse(ok)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.backup.read_bytes(), b'{"previous": true}')
        self.assert_no_temporaries()

    def test_read_error_cannot_be_converted_to_empty_config(self):
        self.seed()
        with patch.object(self.cfg, "_read_config_snapshot",
                          side_effect=PermissionError("read denied")):
            ok, _ = self.cfg._write_config_file({"TTS_ENGINE": "edge"})
        self.assertFalse(ok)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_concurrent_read_merge_write_preserves_both_updates(self):
        self.seed()
        first_read = threading.Event()
        second_started = threading.Event()
        second_read = threading.Event()
        release_first = threading.Event()
        original_read = self.cfg._read_config_snapshot

        def controlled_read():
            result = original_read()
            if not first_read.is_set():
                first_read.set()
                if not release_first.wait(3):
                    raise TimeoutError("test did not release first writer")
            else:
                second_read.set()
            return result

        def second_writer():
            second_started.set()
            return self.cfg._write_config_file({"TELEGRAM_REPORT_CHAT_ID": "101"})

        with patch.object(self.cfg, "_read_config_snapshot", side_effect=controlled_read):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.cfg._write_config_file, {"TTS_ENGINE": "edge"})
                try:
                    self.assertTrue(first_read.wait(2))
                    second = pool.submit(second_writer)
                    self.assertTrue(second_started.wait(2))
                    self.assertFalse(second_read.wait(0.1), "second writer read a stale snapshot")
                finally:
                    release_first.set()
                self.assertTrue(first.result(timeout=3)[0])
                self.assertTrue(second.result(timeout=3)[0])
        saved = self.cfg._read_config_file()
        self.assertEqual(saved["TTS_ENGINE"], "edge")
        self.assertEqual(saved["TELEGRAM_REPORT_CHAT_ID"], "101")
        self.assertEqual(saved["OLLAMA_MODEL"], "old")


class IntegrationTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.settings = {"TELEGRAM_API_ID": "12345", "TELEGRAM_API_HASH": "fixture-hash",
                         "TELEGRAM_REPORT_BOT_TOKEN": "fixture-token"}
        self.config = module_stub("jarvis_config", JARVIS_DIR=self.directory,
                                  _read_config_file=Mock(side_effect=lambda: dict(self.settings)),
                                  _write_config_file=Mock())
        self.state = module_stub("jarvis_state", pending_telegram_send=None,
                                 pending_email_send=None)
        self.http = module_stub("requests", get=Mock(side_effect=AssertionError("GET forbidden")),
                                post=Mock(side_effect=AssertionError("POST not stubbed")))
        errors = {name: type(name, (Exception,), {}) for name in (
            "PasswordHashInvalidError", "PhoneCodeExpiredError", "PhoneCodeInvalidError",
            "SessionPasswordNeededError")}
        errors["PhoneNotOccupiedError"] = PhoneNotOccupiedError
        self.contacts_api = module_stub("telethon.tl.functions.contacts",
                                        ResolvePhoneRequest=ResolvePhoneRequest)
        stubs = {
            "jarvis_config": self.config, "jarvis_state": self.state,
            "jarvis_log": module_stub("jarvis_log", jarvis_logger=Mock()),
            "requests": self.http, "ddgs": module_stub("ddgs", DDGS=Mock()),
            "telethon": module_stub("telethon", TelegramClient=Mock(
                side_effect=AssertionError("Real client construction forbidden"))),
            "telethon.errors": module_stub("telethon.errors", **errors),
            "telethon.utils": module_stub("telethon.utils", get_display_name=Mock(),
                                          get_input_peer=input_peer),
            "telethon.tl.types": module_stub("telethon.tl.types", InputPeerUser=InputPeerUser,
                                             InputPeerChat=InputPeerChat,
                                             InputPeerChannel=InputPeerChannel),
            "telethon.tl.functions.contacts": self.contacts_api,
            "telethon.tl.functions.users": module_stub("telethon.tl.functions.users",
                                                       GetFullUserRequest=GetFullUserRequest),
        }
        imports = patch.dict(sys.modules, stubs)
        imports.start()
        self.addCleanup(imports.stop)
        self.tg = source_module("jarvis_telegram", self.directory)
        sys.modules["jarvis_telegram"] = self.tg
        self.lookup = source_module("jarvis_lookup", self.directory)
        self.client = self.new_client()
        self.tg._telegram_client = Mock(return_value=self.client)
        self.tg._telegram_sync = lambda factory: complete(factory())

    @staticmethod
    def new_client():
        client = Mock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.is_user_authorized = AsyncMock(return_value=True)
        client.get_dialogs = AsyncMock(return_value=[])
        client.send_message = AsyncMock()
        return client

    def test_report_without_chat_id_never_discovers_sends_or_saves(self):
        reply = self.lookup.send_lookup_report_via_bot("fixture report")
        self.assertIn("TELEGRAM_REPORT_CHAT_ID", reply)
        self.http.get.assert_not_called()
        self.http.post.assert_not_called()
        self.config._write_config_file.assert_not_called()
        self.assertEqual(self.lookup._discover_report_chat_id("fixture-token"), "")
        self.http.get.assert_not_called()

    def test_report_uses_only_explicit_recipient(self):
        self.settings["TELEGRAM_REPORT_CHAT_ID"] = "101"
        self.http.post.side_effect = None
        self.http.post.return_value = types.SimpleNamespace(
            ok=True, content=b"{}", json=lambda: {"ok": True})
        self.lookup.send_lookup_report_via_bot("fixture report")
        self.assertEqual(self.http.post.call_args.kwargs["json"]["chat_id"], "101")
        self.http.get.assert_not_called()
        self.config._write_config_file.assert_not_called()

    def test_partial_or_duplicate_names_do_not_stage_a_message(self):
        for names in (("Ivan Work", "Ivan Family"), ("Ivan", "Ivan")):
            with self.subTest(names=names):
                self.client.get_dialogs.return_value = [dialog(names[0], 101), dialog(names[1], 202)]
                reply = self.tg.telegram_request_send("Ivan", "fixture")
                self.assertIn("ID 101", reply)
                self.assertIn("ID 202", reply)
                self.assertIsNone(self.state.pending_telegram_send)
        self.client.send_message.assert_not_called()

    def test_confirmation_keeps_peer_across_new_client_and_renamed_dialogs(self):
        self.client.get_dialogs.return_value = [dialog("Ivan Work", 101, access_hash=987),
                                                dialog("Ivan Family", 202)]
        reply = self.tg.telegram_request_send("Ivan Work", "fixture")
        self.assertIn("ID 101", reply)
        self.client.send_message.assert_not_called()
        fresh = self.new_client()
        fresh.get_dialogs.side_effect = AssertionError("Confirmation must not resolve names")
        self.tg._telegram_client.return_value = fresh
        self.tg._telegram_find_dialog = Mock(side_effect=AssertionError("Fuzzy send forbidden"))
        self.tg.telegram_confirm_pending("подтверждаю.")
        peer, text = fresh.send_message.call_args.args
        self.assertEqual((peer.user_id, peer.access_hash, text), (101, 987, "fixture"))
        fresh.get_dialogs.assert_not_called()
        self.assertIsNone(self.state.pending_telegram_send)
        self.assertIsNone(self.tg.telegram_confirm_pending("подтверждаю"))
        fresh.send_message.assert_awaited_once()

    def test_username_and_marked_ids_identify_the_requested_peer(self):
        self.client.get_dialogs.return_value = [dialog("@work_user", 202),
            dialog("Ivan", 101, username="work_user"), dialog("Team", 33, kind="chat"),
            dialog("News", 44, kind="channel", access_hash=555)]
        for query, attr, expected in (("@work_user", "user_id", 101),
                                     ("-33", "chat_id", 33),
                                     ("-1000000000044", "channel_id", 44)):
            with self.subTest(query=query):
                self.tg.telegram_request_send(query, "fixture")
                self.assertEqual(getattr(self.state.pending_telegram_send["peer"], attr), expected)
                self.tg.telegram_confirm_pending("отмена")
        self.client.send_message.assert_not_called()

    def test_public_helper_rejects_fuzzy_target_even_when_only_one_candidate(self):
        self.client.get_dialogs.return_value = [dialog("Ivan Work", 101)]
        self.tg.telegram_send_message("Ivan", "fixture")
        self.client.send_message.assert_not_called()
        self.tg.telegram_send_message("Ivan Work", "fixture")
        self.client.send_message.assert_awaited_once()
        self.assertEqual(self.client.send_message.call_args.args[0].user_id, 101)

    def test_failed_new_request_clears_old_confirmation(self):
        self.state.pending_telegram_send = {"chat": "old", "text": "old"}
        self.state.pending_email_send = {"to": "fixture@example.invalid"}
        self.tg.telegram_request_send("Unknown", "fixture")
        self.assertIsNone(self.state.pending_telegram_send)
        self.assertIsNone(self.state.pending_email_send)
        self.assertIsNone(self.tg.telegram_confirm_pending("да"))
        self.client.send_message.assert_not_called()

    def test_legacy_pending_without_peer_is_not_sent(self):
        self.state.pending_telegram_send = {"chat": "Ivan", "text": "fixture"}
        reply = self.tg.telegram_confirm_pending("да")
        self.assertIn("Заново", reply)
        self.client.send_message.assert_not_called()
        self.client.get_dialogs.assert_not_called()

    def test_peer_without_hash_is_not_staged(self):
        self.client.get_dialogs.return_value = [dialog("Ivan", 101, access_hash=None)]
        self.tg.telegram_request_send("Ivan", "fixture")
        self.assertIsNone(self.state.pending_telegram_send)
        self.client.send_message.assert_not_called()

    def test_phone_lookup_only_resolves_and_reads_profile(self):
        existing = types.SimpleNamespace(id=77, first_name="Existing", last_name=None, contact=True)
        unrelated = types.SimpleNamespace(id=88, first_name="Wrong")
        resolved = types.SimpleNamespace(peer=types.SimpleNamespace(user_id=77),
                                          users=[unrelated, existing])
        full = types.SimpleNamespace(full_user=types.SimpleNamespace(about="fixture bio"))
        api = AsyncMock(side_effect=[resolved, full])
        self.tg._telegram_client.return_value = api
        api.connect = AsyncMock()
        api.disconnect = AsyncMock()
        api.is_user_authorized = AsyncMock(return_value=True)
        reply = self.tg.telegram_lookup_phone("+70000000000")
        self.assertIn("Existing", reply)
        self.assertIn("fixture bio", reply)
        self.assertEqual([type(c.args[0]) for c in api.await_args_list],
                         [ResolvePhoneRequest, GetFullUserRequest])
        self.assertEqual(api.await_args_list[0].args[0].phone, "+70000000000")
        self.assertTrue(existing.contact)

    def test_unsupported_phone_resolver_never_falls_back_to_contact_import(self):
        with patch.dict(sys.modules, {"telethon.tl.functions.contacts":
                                     module_stub("telethon.tl.functions.contacts")}):
            reply = self.tg.telegram_lookup_phone("+70000000000")
        self.assertIn("Обновите Telethon", reply)
        self.client.assert_not_called()

    def test_unavailable_phone_has_no_cleanup_side_effects(self):
        api = AsyncMock(side_effect=PhoneNotOccupiedError())
        api.connect, api.disconnect = AsyncMock(), AsyncMock()
        api.is_user_authorized = AsyncMock(return_value=True)
        self.tg._telegram_client.return_value = api
        reply = self.tg.telegram_lookup_phone("+70000000000")
        self.assertIn("не раскрыл", reply)
        api.assert_awaited_once()
        self.assertIsInstance(api.await_args.args[0], ResolvePhoneRequest)

    def test_phone_resolver_observes_three_second_rate_limit(self):
        api = AsyncMock(return_value=types.SimpleNamespace(peer=None, users=[]))
        api.connect, api.disconnect = AsyncMock(), AsyncMock()
        api.is_user_authorized = AsyncMock(return_value=True)
        self.tg._telegram_client.return_value = api
        with patch.object(self.tg.time, "monotonic", side_effect=[10.0, 11.0, 13.0]):
            self.tg.telegram_lookup_phone("+70000000000")
            reply = self.tg.telegram_lookup_phone("+70000000000")
            self.tg.telegram_lookup_phone("+70000000000")
        self.assertIn("через несколько секунд", reply)
        self.assertEqual(api.await_count, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

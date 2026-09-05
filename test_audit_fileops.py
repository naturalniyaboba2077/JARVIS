"""B1-B4 regressions. Standard-library only; synthetic temp data, no real config.

Run: python -B test_audit_fileops.py
The subprocesses in the concurrency test run this fixed synthetic worker only.
No supplied project code, shell command or anti-wipe example is executed.
"""

import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
SOURCE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location("_audit_" + name, SOURCE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dummy = types.ModuleType("jarvis_config")
dummy.JARVIS_DIR = Path(tempfile.gettempdir()) / "unused-audit-config"
with patch.dict(sys.modules, {"jarvis_config": dummy}):
    fo = load("jarvis_fileops")
    safety = load("jarvis_safety")
    with patch.dict(sys.modules, {"jarvis_fileops": fo}):
        checks = load("jarvis_project_checks")
        with patch.dict(sys.modules, {"jarvis_project_checks": checks}):
            agent = load("project_agent")


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jarvis-audit-fixes-")
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        self.history = self.base / "history"
        self.env = patch.dict(os.environ, {"JARVIS_FILE_HISTORY": str(self.history),
                                          "JARVIS_PROJECT_ROOTS": str(self.base),
                                          "PROTECTED_PATHS": str(self.base / "protected")})
        self.env.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.env.stop)

    def write(self, name="a.txt", text="agent"):
        return fo.write_versioned(self.root, self.root / name, text)

    def pending(self):
        return fo.pending_changes(self.root)

    def test_byte_exact_undo_chain_and_creation(self):
        target = self.root / "a.txt"
        original = b"first\r\nsecond\n\x00\xff"
        target.write_bytes(original)
        self.write(text="one")
        self.write(text="two")
        self.assertIn("Вернул", fo.undo_last(self.root))
        self.assertEqual(target.read_bytes(), b"one")
        self.assertIn("Вернул", fo.undo_last(self.root))
        self.assertEqual(target.read_bytes(), original)
        self.write("new.txt")
        self.assertIn("Удалил", fo.undo_last(self.root))
        self.assertFalse((self.root / "new.txt").exists())
        self.assertEqual(self.pending(), [])

    def test_undo_chain_preserves_manual_edit_between_writes(self):
        for existed in (False, True):
            with self.subTest(first_write_overwrites=existed):
                name = f"manual-{existed}.txt"
                target = self.root / name
                if existed:
                    target.write_text("original")
                self.write(name, "one")
                first = self.pending()[0]
                target.write_text("manual")
                self.write(name, "two")
                self.assertIn("Вернул", fo.undo_last(self.root))
                self.assertEqual(target.read_text(), "manual")
                result = fo.undo_last(self.root)
                self.assertIn("отменён", result)
                self.assertEqual(target.read_text(), "manual")
                self.assertEqual(self.pending()[0]["seq"], first["seq"])
                self.assertEqual(self.pending()[0]["after"], first["after"])

    def test_pending_rebind_requires_matching_mode(self):
        self.write(text="one")
        first = self.pending()[0]
        self.write(text="two")
        self.assertIn("Вернул", fo.undo_last(self.root))
        records = fo._read_journal(self.root)
        # Synthetic mode-only restoration mismatch. Windows chmod cannot model
        # all POSIX modes, so alter the loaded record rather than live metadata.
        records[-1]["after"]["mode"] ^= 0o200
        self.assertEqual(fo._pending(records)[0]["after"], first["after"])

    def test_undo_chain_preserves_identical_independent_replacement(self):
        for existed in (False, True):
            with self.subTest(first_write_overwrites=existed):
                name = f"identical-{existed}.txt"
                target = self.root / name
                if existed:
                    target.write_text("original")
                self.write(name, "one")
                first = self.pending()[0]
                replacement = self.root / "replacement"
                replacement.write_text("one")
                os.replace(replacement, target)
                self.assertNotEqual(fo._snapshot(target)[0]["ino"], first["after"]["ino"])
                self.assertIn("отменён", fo.undo_last(self.root))
                self.write(name, "two")
                self.assertIn("Вернул", fo.undo_last(self.root))
                self.assertIn("отменён", fo.undo_last(self.root))
                self.assertEqual(target.read_text(), "one")
                self.assertEqual(self.pending()[0]["after"], first["after"])
                self.assertIsNone(fo._read_journal(self.root)[-1]["restores"])

    def test_undo_rebind_uses_current_after_not_original_journal_identity(self):
        self.write(text="one")
        original = self.pending()[0]["after"]
        self.write(text="two")
        self.assertIn("Вернул", fo.undo_last(self.root))
        restored = self.pending()[0]["after"]
        self.assertNotEqual(original, restored)
        self.write(text="three")
        self.assertEqual(self.pending()[0]["before"], restored)
        # Recovery must apply the same continuity rule as an uninterrupted undo.
        with patch.object(fo, "_append", side_effect=OSError("injected undo failure")):
            with self.assertRaises(OSError):
                fo.undo_last(self.root)
        self.assertIn("ранее начатый", fo.undo_last(self.root))
        self.assertIn("Удалил", fo.undo_last(self.root))
        self.assertFalse((self.root / "a.txt").exists())
        self.assertEqual(self.pending(), [])

    def test_replay_rebind_requires_complete_before_identity(self):
        self.write(text="one")
        first = self.pending()[0]
        self.write(text="two")
        self.assertIn("Вернул", fo.undo_last(self.root))
        records = fo._read_journal(self.root)
        for key in ("dev", "ino", "mtime", "mode", "size", "sha256"):
            with self.subTest(field=key):
                changed = json.loads(json.dumps(records))
                before = changed[1]["before"]
                before[key] = "other" if key == "sha256" else before[key] + 1
                self.assertEqual(fo._pending(changed)[0]["after"], first["after"])
        del records[1]["before"]
        self.assertEqual(fo._pending(records)[0]["after"], first["after"])

    def test_direct_traversal_absolute_ads_and_reserved_names(self):
        outside = self.base / "outside.txt"
        outside.write_text("keep")
        for target in (self.root / ".." / "outside.txt", outside,
                       self.root / "a.txt:stream", self.root / "NUL", self.root / "trailing."):
            with self.subTest(target=target), self.assertRaises(ValueError):
                fo.write_versioned(self.root, target, "bad")
        self.assertEqual(outside.read_text(), "keep")

    def test_no_hardlink_write_or_undo(self):
        outside = self.base / "outside.txt"
        outside.write_text("keep")
        os.link(outside, self.root / "link.txt")
        with self.assertRaises(ValueError):
            self.write("link.txt", "bad")
        self.write()
        os.link(self.root / "a.txt", self.base / "second-link.txt")
        self.assertIn("отменён", fo.undo_last(self.root))
        self.assertEqual(outside.read_text(), "keep")
        self.assertEqual((self.base / "second-link.txt").read_text(), "agent")

    def symlink(self, link, target, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError as exc:
            self.skipTest(f"OS does not permit synthetic symlink: {exc}")

    def test_undo_never_deletes_symlink_referent(self):
        referent = self.root / "preexisting.txt"
        referent.write_text("keep")
        self.write("created.txt")
        link = self.root / "created.txt"
        link.unlink()
        self.symlink(link, referent)
        self.assertIn("отменён", fo.undo_last(self.root))
        self.assertEqual(referent.read_text(), "keep")
        self.assertTrue(link.is_symlink())

    def test_ancestor_symlink_refuses_read_write_and_undo(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "a.txt").write_text("keep")
        self.symlink(self.root / "alias", outside, True)
        with self.assertRaises(ValueError):
            self.write("alias/a.txt", "bad")
        with self.assertRaises(ValueError):
            agent._execute(self.root, "read_file", {"path": "alias/a.txt"})
        self.write("dir/a.txt")
        (self.root / "dir" / "a.txt").unlink()
        (self.root / "dir").rmdir()
        self.symlink(self.root / "dir", outside, True)
        self.assertIn("отменён", fo.undo_last(self.root))
        self.assertEqual((outside / "a.txt").read_text(), "keep")

    def test_undo_refuses_changed_contents(self):
        for existed in (False, True):
            name = f"changed-{existed}.txt"
            target = self.root / name
            if existed:
                target.write_text("original")
            self.write(name)
            target.write_text("later-independent-edit")
            self.assertIn("отменён", fo.undo_last(self.root))
            self.assertEqual(target.read_text(), "later-independent-edit")

    def test_undo_refuses_replacement_with_identical_contents(self):
        self.write()
        replacement = self.root / "replacement"
        replacement.write_text("agent")
        os.replace(replacement, self.root / "a.txt")
        self.assertIn("отменён", fo.undo_last(self.root))
        self.assertEqual((self.root / "a.txt").read_text(), "agent")

    def test_backup_corruption_refuses_undo(self):
        (self.root / "a.txt").write_text("old")
        self.write()
        record = self.pending()[0]
        (fo.history_root(self.root) / "blobs" / record["backup"]).write_text("corrupt")
        self.assertIn("отменён", fo.undo_last(self.root))
        self.assertEqual((self.root / "a.txt").read_text(), "agent")

    def test_failure_before_intent_does_not_change_target(self):
        target = self.root / "a.txt"
        target.write_text("old")
        original_atomic = fo._atomic

        def fail_intent(path, data):
            if path.name == "pending.json":
                raise OSError("injected intent failure")
            return original_atomic(path, data)

        with patch.object(fo, "_atomic", side_effect=fail_intent), self.assertRaises(OSError):
            self.write()
        self.assertEqual(target.read_text(), "old")
        self.assertEqual(self.pending(), [])

    def test_failure_after_intent_recovers_before_replace(self):
        target = self.root / "a.txt"
        target.write_text("old")
        original_replace = os.replace

        def fail_target(source, dest):
            if Path(dest) == target:
                raise OSError("injected replace failure")
            return original_replace(source, dest)

        with patch.object(fo.os, "replace", side_effect=fail_target), self.assertRaises(OSError):
            self.write()
        self.assertEqual(target.read_text(), "old")
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(target.read_text(), "agent")
        self.assertIn("Вернул", fo.undo_last(self.root))
        self.assertEqual(target.read_text(), "old")

    def test_failed_append_recovers_real_write_and_backup(self):
        target = self.root / "a.txt"
        target.write_text("old")
        with patch.object(fo, "_append", side_effect=OSError("injected append failure")):
            with self.assertRaises(OSError):
                self.write()
        self.assertEqual(target.read_text(), "agent")
        self.assertTrue(fo._intent(self.root).is_file())
        self.assertEqual(len(self.pending()), 1)
        fo.undo_last(self.root)
        self.assertEqual(target.read_text(), "old")

    def test_journal_replace_failure_preserves_previous_records(self):
        self.write("first.txt")
        original_replace = os.replace

        def fail_journal(source, dest):
            if Path(dest) == fo._journal(self.root):
                raise OSError("injected journal replace failure")
            return original_replace(source, dest)

        with patch.object(fo.os, "replace", side_effect=fail_journal), self.assertRaises(OSError):
            self.write("second.txt")
        self.assertEqual(len(fo._read_journal(self.root)), 1)
        self.assertEqual(len(self.pending()), 2)

    def test_postcommit_cleanup_retry_does_not_reapply(self):
        original_unlink = Path.unlink

        def fail_cleanup(path, *args, **kwargs):
            if path == fo._intent(self.root):
                raise OSError("injected cleanup failure")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_cleanup), self.assertRaises(OSError):
            self.write()
        (self.root / "a.txt").write_text("independent")
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual((self.root / "a.txt").read_text(), "independent")
        self.assertEqual(len(fo._read_journal(self.root)), 1)

    def test_retry_undo_completes_only_that_operation(self):
        self.write("first.txt")
        self.write("second.txt")
        with patch.object(fo, "_append", side_effect=OSError("injected undo failure")):
            with self.assertRaises(OSError):
                fo.undo_last(self.root)
        self.assertFalse((self.root / "second.txt").exists())
        self.assertIn("ранее начатый", fo.undo_last(self.root))
        self.assertTrue((self.root / "first.txt").exists())
        self.assertEqual(len(self.pending()), 1)

    def test_failed_undo_does_not_delete_recreated_file(self):
        self.write()
        with patch.object(fo, "_append", side_effect=OSError("injected undo failure")):
            with self.assertRaises(OSError):
                fo.undo_last(self.root)
        (self.root / "a.txt").write_text("independent")
        self.assertIn("отменён", fo.undo_last(self.root))
        self.assertEqual((self.root / "a.txt").read_text(), "independent")

    def test_recovery_detects_external_edit_before_apply(self):
        target = self.root / "a.txt"
        target.write_text("old")
        original_replace = os.replace

        def fail_target(source, dest):
            if Path(dest) == target:
                raise OSError("injected replace failure")
            return original_replace(source, dest)

        with patch.object(os, "replace", side_effect=fail_target), self.assertRaises(OSError):
            self.write()
        target.write_text("independent")
        with self.assertRaises(fo.FileConflict):
            self.pending()
        self.assertEqual(target.read_text(), "independent")

    def test_thread_writers_have_unique_sequences_and_all_undo(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda number: self.write(f"{number}.txt"), range(8)))
        self.assertEqual(len({r["seq"] for r in self.pending()}), 8)
        for _ in range(8):
            self.assertIn("Удалил", fo.undo_last(self.root))
        self.assertEqual(self.pending(), [])

    def test_process_writers_share_lock(self):
        env = {"SystemRoot": os.environ.get("SystemRoot", ""), "TEMP": str(self.base),
               "TMP": str(self.base), "PATH": ""}
        workers = [subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).resolve()),
                                     "--worker", str(self.root), str(self.history), f"p{i}.txt"],
                                    cwd=self.base, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE) for i in range(2)]
        for worker in workers:
            out, err = worker.communicate(timeout=20)
            self.assertEqual(worker.returncode, 0, (out, err))
        self.assertEqual(len({r["seq"] for r in self.pending()}), 2)

    def test_recovery_after_process_crash_releases_os_lock(self):
        env = {"SystemRoot": os.environ.get("SystemRoot", ""), "TEMP": str(self.base),
               "TMP": str(self.base), "PATH": ""}
        worker = subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).resolve()),
                                 "--crash-worker", str(self.root), str(self.history), "p0.txt"],
                                cwd=self.base, env=env, capture_output=True, timeout=20)
        self.assertEqual(worker.returncode, 23, worker.stderr)
        self.assertTrue(fo._intent(self.root).exists())
        self.assertEqual((self.root / "p0.txt").read_text(), "synthetic-worker")
        self.assertEqual(len(self.pending()), 1)
        self.assertIn("Удалил", fo.undo_last(self.root))
        self.assertFalse((self.root / "p0.txt").exists())

    @unittest.skipUnless(os.name == "nt", "Windows directory handle semantics")
    def test_windows_parent_handle_prevents_ancestor_swap(self):
        directory = self.root / "pinned"
        directory.mkdir()
        with fo._parents(directory / "a.txt"):
            with self.assertRaises(PermissionError):
                directory.rename(self.root / "moved")
        self.assertTrue(directory.is_dir())

    def test_legacy_history_preserved_and_new_writes_work(self):
        directory = fo.history_root(self.root)
        (directory / "blobs").mkdir(parents=True)
        (directory / "blobs" / "0001_a.txt").write_text("old")
        legacy = {"seq": 1, "action": "write", "path": "a.txt", "existed": True,
                  "backup": "0001_a.txt", "at": "2026-09-05"}
        journal = directory / "journal.jsonl"
        journal.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
        (self.root / "a.txt").write_text("legacy-current")
        self.assertIn("старая история", fo.list_history(self.root))
        self.assertIn("Старая правка", fo.undo_last(self.root))
        self.write("a.txt", "new")
        self.assertIn("Вернул", fo.undo_last(self.root))
        self.assertEqual((self.root / "a.txt").read_text(), "legacy-current")
        self.assertEqual(json.loads(journal.read_text().splitlines()[0]), legacy)

    def test_corrupt_history_fails_closed(self):
        self.write()
        journal = fo._journal(self.root)
        original = journal.read_bytes() + b'{"torn":'
        journal.write_bytes(original)
        with self.assertRaises(fo.FileConflict):
            self.write("second.txt")
        self.assertFalse((self.root / "second.txt").exists())
        self.assertEqual(journal.read_bytes(), original)

    def test_history_is_not_agent_writable(self):
        with patch.dict(os.environ, {"JARVIS_FILE_HISTORY": str(self.root / "history")}):
            with self.assertRaises(ValueError):
                self.write("history/journal.jsonl", "bad")

    def test_history_dotdot_alias_cannot_overwrite_journal(self):
        (self.root / "intermediate").mkdir()
        history = self.root / "intermediate" / ".." / "private-history"
        with patch.dict(os.environ, {"JARVIS_FILE_HISTORY": str(history)}):
            self.write()
            journal = Path(os.path.abspath(fo._journal(self.root)))
            before = journal.read_bytes()
            with self.assertRaises(fo.FileConflict):
                agent._execute(self.root, "write_file", {
                    "path": journal.relative_to(self.root).as_posix(), "content": "corrupt"})
            self.assertEqual(journal.read_bytes(), before)
            self.assertFalse(fo._intent(self.root).exists())
            # Normalization is lexical: never turn a link into its referent.
            with patch.object(Path, "resolve", side_effect=AssertionError("followlinks")):
                self.assertEqual(fo._history_dir(), self.root / "private-history")
            self.assertIn("Удалил", fo.undo_last(self.root))

    def test_history_rejects_symlink_even_when_dotdot_would_remove_it(self):
        outside = self.base / "outside"
        outside.mkdir()
        link = self.root / "alias"
        self.symlink(link, outside, True)
        for history in (link / "history", link / ".." / "history",
                        self.root / "missing" / ".." / "alias" / "history"):
            with self.subTest(history=history), \
                    patch.dict(os.environ, {"JARVIS_FILE_HISTORY": str(history)}):
                with self.assertRaises(fo.FileConflict):
                    self.write()
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.root / "a.txt").exists())

    def test_history_rejects_reparse_component_before_lexical_normalization(self):
        junction = self.root / "junction"
        original_lstat = Path.lstat

        def reparse_info(path, *args, **kwargs):
            if path == junction:
                # A junction is a directory, not necessarily S_ISLNK.
                return types.SimpleNamespace(st_mode=0o040755, st_file_attributes=0x400)
            return original_lstat(path, *args, **kwargs)

        history = junction / ".." / "history"
        with patch.dict(os.environ, {"JARVIS_FILE_HISTORY": str(history)}), \
                patch.object(Path, "lstat", reparse_info):
            with self.assertRaises(fo.FileConflict):
                self.write()
        self.assertFalse((self.root / "a.txt").exists())
        self.assertFalse((self.root / "history").exists())

    def test_unsafe_commands_never_spawn(self):
        commands = ["python -c \"print('ok')\"", "pytest", "python -m unittest",
                    "Set-Content ../outside.txt changed", "git status", "git diff --check",
                    "git -c color.ui=false reset --hard", "rm -rf .", "echo ok > out"]
        with patch.object(subprocess, "Popen", side_effect=AssertionError("host subprocess")), \
                patch.object(os, "system", side_effect=AssertionError("host shell")):
            for command in commands:
                with self.subTest(command=command):
                    result = agent._execute(self.root, "run_command", {"command": command})
                    self.assertIn("заблокирована", result)
                    self.assertIn("backend", result)

    def test_compile_checks_syntax_without_execution_or_pyc(self):
        (self.root / "app.py").write_text("raise RuntimeError('must never run')\n", encoding="utf-8")
        for command in ("compile", "check app.py", "python -m py_compile app.py",
                        "python -m compileall -q ."):
            with self.subTest(command=command):
                self.assertIn("exit=0", agent._execute(self.root, "run_command", {"command": command}))
        self.assertFalse((self.root / "__pycache__").exists())
        (self.root / "app.py").write_text("return 1\n")
        self.assertIn("SyntaxError", checks.run_check(self.root, "compile"))

    def test_compile_refuses_outside_links_and_limits(self):
        (self.base / "outside.py").write_text("x=1")
        self.assertIn("отклонена", checks.run_check(self.root, "compile ../outside.py"))
        (self.root / "one.py").write_text("x=1")
        (self.root / "two.py").write_text("x=2")
        with patch.object(checks, "MAX_CHECK_FILES", 1):
            self.assertIn("Лимит", checks.run_check(self.root, "compile"))

    def test_search_flags_are_literal_and_no_subprocess(self):
        (self.root / "a.txt").write_text("--files\n-TODO\n")
        with patch.object(subprocess, "Popen", side_effect=AssertionError("external search")):
            self.assertEqual(checks.search_text(self.root, "--files"), "a.txt:1:--files")
            self.assertEqual(checks.search_text(self.root, "-TODO"), "a.txt:2:-TODO")

    def test_antiwipe_repr_canonical_and_regular_exec_eval(self):
        protected = self.base / "protected"
        variants = [str(protected), str(protected).replace("\\", "/"),
                    str(self.base / "other" / ".." / "protected")]
        for value in variants:
            with self.subTest(value=value):
                self.assertFalse(safety.is_code_safe("shutil.rmtree(" + repr(value) + ")")[0])
        self.assertTrue(safety.is_code_safe("shutil.rmtree(" + repr(str(self.base / "ordinary")) + ")")[0])
        self.assertTrue(safety.is_code_safe("exec('print(1)'); eval('1+1')")[0])

    def test_antiwipe_normalizes_shell_literal_paths_without_execution(self):
        protected = self.base / "protected"
        alias = self.base / "spare" / ".." / "protected"
        ordinary = self.base / "spare" / ".." / "ordinary"
        for pattern in ("Remove-Item -LiteralPath '{path}' -Recurse -Force",
                        'Remove-Item -Path "{path}" -Recurse -Force',
                        "rd /s /q \"{path}\"", "cmd /c rd /s /q \"{path}\"",
                        "del /s /q '{path}'", "rm -rf '{path}'"):
            with self.subTest(pattern=pattern):
                self.assertFalse(safety.is_code_safe(pattern.format(path=alias))[0])
                self.assertTrue(safety.is_code_safe(pattern.format(path=ordinary))[0])
        with patch.object(os, "getcwd", return_value=str(protected)):
            self.assertFalse(safety.is_code_safe("Remove-Item -LiteralPath . -Recurse -Force")[0])
            self.assertFalse(safety.is_code_safe("rd /s /q .")[0])
            # Named option values and other commands are not deletion targets.
            command = f"Write-Output .; Remove-Item -LiteralPath '{ordinary}' -Recurse -ErrorAction Stop"
            self.assertTrue(safety.is_code_safe(command)[0])
            self.assertTrue(safety.is_code_safe("Write-Output .; Get-ChildItem .")[0])
            self.assertTrue(safety.is_code_safe("exec('print(1)'); eval('1+1')")[0])

    def test_antiwipe_shell_literal_quotes_spaces_and_unquoted_paths(self):
        protected = self.base / "protected space's (literal)"
        alias = self.base / "spare" / ".." / protected.name
        with patch.dict(os.environ, {"PROTECTED_PATHS": str(protected)}):
            quoted = str(alias).replace("'", "''")
            self.assertFalse(safety.is_code_safe(f"Remove-Item -LiteralPath '{quoted}' -Recurse")[0])
            self.assertFalse(safety.is_code_safe(f'Remove-Item -Path "{alias}" -Recurse')[0])
        with patch.object(os, "getcwd", return_value=str(self.base)):
            self.assertFalse(safety.is_code_safe("Remove-Item -Path ./spare/../protected -Recurse")[0])
            self.assertTrue(safety.is_code_safe("Remove-Item -Path ./spare/../ordinary -Recurse")[0])

    def test_single_file_delete_in_jarvis_cwd_remains_allowed(self):
        with patch.object(safety, "JARVIS_DIR", self.root), \
                patch.object(os, "getcwd", return_value=str(self.root)):
            self.assertTrue(safety.is_code_safe("import os\nos.remove('somefile.txt')")[0])
            self.assertTrue(safety.is_code_safe("Path('somefile.txt').unlink()")[0])
            absolute_file = repr(str(self.root / "somefile.txt"))
            self.assertTrue(safety.is_code_safe("os.remove(" + absolute_file + ")")[0])
            self.assertFalse(safety.is_code_safe("shutil.rmtree(" + repr(str(self.root)) + ")")[0])
            self.assertFalse(safety.is_code_safe("os.rmdir(" + repr(str(self.root)) + ")")[0])


if __name__ == "__main__":
    if sys.argv[1:2] in (["--worker"], ["--crash-worker"]):
        root, history, name = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
        assert root.parent.name.startswith("jarvis-audit-fixes-") and root.name == "project"
        assert history == root.parent / "history" and name in {"p0.txt", "p1.txt"}
        os.environ["JARVIS_FILE_HISTORY"] = str(history)
        if sys.argv[1] == "--crash-worker":
            fo._append = lambda *args: os._exit(23)
        fo.write_versioned(root, root / name, "synthetic-worker")
    else:
        unittest.main(verbosity=2)

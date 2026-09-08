"""Run all regressions in a disposable source copy, never against personal data.

This isolates trusted test fixtures, not arbitrary hostile code. External network
and the clipboard are stubbed in each worker; no real app/audio startup is used.
Run with the app's Python: python -B run_tests.py [test_name.py ...].
"""

import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


SUITES = (
    "test_regression.py", "test_jarvis_functions.py", "test_safety.py",
    "test_portability.py", "test_fileops.py", "test_project_agent.py",
    "test_audit_actions.py", "test_audit_fileops.py",
    "test_audit_integrations.py", "test_audit_pipeline.py",
    "test_ui_redesign.py",
    "test_request_routing.py",
    "test_latency_ux.py",
    "test_discovery_search.py",
    "test_dialogue.py",
    "test_agent_runtime.py",
    "test_project_workflow.py",
    "test_conversation.py",
    "test_startup.py",
    "test_live_settings.py",
    "test_response.py",
    "test_project_dialogue.py",
    "test_project_selection.py",
    "test_chat_memory.py",
)


def worker(directory: str, test: str) -> None:
    import ipaddress
    import runpy
    import socket
    import types

    root = Path(directory).resolve()
    os.chdir(root)
    # Do not leave the real application directory on the module search path.
    actual = Path(__file__).resolve().parent
    sys.path[:] = [str(root)] + [p for p in sys.path if p and Path(p).resolve() != actual]
    for key in list(os.environ):
        if key.startswith(("OPENROUTER_", "TELEGRAM_", "JARVIS_")):
            os.environ.pop(key)
    os.environ["PROTECTED_PATHS"] = str(actual)
    os.environ["SESSION_MEMORY"] = "off"
    os.environ["JARVIS_OVERLAY"] = "off"
    os.environ["JARVIS_FILE_HISTORY"] = str(root / "test-history")

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def guarded_connect(sock, address, original=original_connect):
        try:
            loopback = ipaddress.ip_address(address[0]).is_loopback
        except (ValueError, TypeError, IndexError):
            loopback = False
        if loopback:  # Windows asyncio's internal socketpair needs local TCP.
            return original(sock, address)
        raise RuntimeError("External network disabled by the test runner")

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = lambda sock, address: guarded_connect(sock, address, original_connect_ex)
    clipboard = types.ModuleType("pyperclip")
    clipboard._text = ""
    clipboard.copy = lambda text: setattr(clipboard, "_text", text)
    clipboard.paste = lambda: clipboard._text
    sys.modules["pyperclip"] = clipboard
    sys.argv = [str(root / test)]
    runpy.run_path(str(root / test), run_name="__main__")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        worker(sys.argv[2], sys.argv[3])
        return 0
    requested = tuple(sys.argv[1:]) or SUITES
    if any(name not in SUITES for name in requested):
        print("Unknown suite. Choose from: " + ", ".join(SUITES))
        return 2
    source = Path(__file__).resolve().parent
    failures = []
    with tempfile.TemporaryDirectory(prefix="jarvis-tests-") as temporary:
        destination = Path(temporary)
        files = {"jarvis.py", "project_agent.py", "overlay.py", "requirements.txt",
                 "health_check.py", "jarvis_config.example.json", ".gitignore",
                 "pc_apps.txt", "ui/index.html", "ui/jarvis.css", "ui/jarvis.js", *SUITES}
        files.update(path.name for path in source.glob("jarvis_*.py"))
        for name in sorted(files):
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, target)
        for suite in requested:
            try:
                process = subprocess.run(
                    [sys.executable, "-B", str(Path(__file__).resolve()), "--worker", temporary, suite],
                    cwd=temporary, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=120,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                print(f"{'PASS' if process.returncode == 0 else 'FAIL'} {suite}")
                if process.returncode:
                    failures.append(suite)
                    print(process.stdout)
                    print(process.stderr)
                else:
                    lines = (process.stdout + process.stderr).splitlines()
                    for line in lines:
                        if any(marker in line for marker in ("ИТОГ:", "Results:", "Ran ")) or re.fullmatch(r"\d+/\d+ верно", line):
                            print("  " + line.strip())
            except subprocess.TimeoutExpired:
                failures.append(suite)
                print(f"FAIL {suite}: exceeded 120 seconds")
    print(f"\n{len(requested) - len(failures)}/{len(requested)} suites passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

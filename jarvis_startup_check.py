"""Opt-in native window timing. Synthetic bridge: no assistant/audio/model startup.

python -B jarvis_startup_check.py --runs 3
Each disposable child owns one window and closes it after verifying the JS bridge.
This measures process launch -> shown/loaded, NOT cold LLM/STT/TTS readiness.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time


def child(started_at):
    from jarvis_bootstrap import StartupApi, open_window
    import webview

    class ProbeApi(StartupApi):
        def _loaded(self):
            self._mark("interface_loaded")
            try:
                check = self._window.evaluate_js("""({
                    width: document.getElementById('jvRoot').clientWidth,
                    composer: !!document.getElementById('cmd'),
                    bridge: typeof window.pywebview.api.send_command === 'function',
                    ui: typeof window.jvSetState === 'function'
                })""")
                print("STARTUP_CHECK " + json.dumps({"timings": self._marks,
                      "checks": check, "synthetic_backend": True}), flush=True)
            finally:
                self.close()

    probe = ProbeApi(started_at)
    probe._log_label = "BOOT-CHECK"
    open_window(webview, probe)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, choices=range(1, 6), default=3)
    parser.add_argument("--child", type=float)
    args = parser.parse_args()
    if args.child is not None:
        child(args.child)
        return 0
    results = []
    for _ in range(args.runs):
        started_at = time.perf_counter()
        process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()),
                                    "--child", str(started_at)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace",
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            out, err = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            # Only the child created above; never kill unrelated Jarvis/browsers.
            process.kill()
            process.communicate(timeout=5)
            print("FAIL: native window exceeded 20 seconds; probe child stopped.")
            return 1
        lines = [line for line in out.splitlines() if line.startswith("STARTUP_CHECK ")]
        if process.returncode or len(lines) != 1:
            print("FAIL: probe did not finish", err[-1500:])
            return 1
        result = json.loads(lines[0].split(" ", 1)[1])
        if not all(result["checks"].values()):
            print("FAIL:", result)
            return 1
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    print("WINDOW_ONLY: model, microphone and audible reply latency were not measured.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

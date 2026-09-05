"""Read-only environment check for J.A.R.V.I.S. v1.0."""
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent
required = {
    "speech_recognition": "speechrecognition", "openai": "openai",
    "pyaudio": "PyAudio",
    "pygame": "pygame", "pyautogui": "pyautogui", "webview": "pywebview",
    "faster_whisper": "faster-whisper", "piper": "piper-tts",
    "requests": "requests", "psutil": "psutil", "telethon": "Telethon",
}

print(f"Python: {sys.version.split()[0]} ({sys.executable})")
missing = []
for module, package in required.items():
    ok = importlib.util.find_spec(module) is not None
    print(f"{'OK  ' if ok else 'MISS'} {package}")
    if not ok:
        missing.append(package)

cfg_path = ROOT / "jarvis_config.json"
cfg = {}
if cfg_path.exists():
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        print("OK   jarvis_config.json")
    except Exception as exc:
        print(f"FAIL jarvis_config.json: {exc}")
else:
    print("MISS jarvis_config.json")

model = ROOT / "piper_models" / f"ru_RU-{cfg.get('PIPER_VOICE', 'dmitri')}-medium.onnx"
print(f"{'OK  ' if model.exists() else 'INFO'} Piper model: {model.name}")
print(f"{'OK  ' if (ROOT / 'ui/index.html').exists() else 'MISS'} UI")
print(f"{'OK  ' if os.getenv('LOCALAPPDATA') else 'MISS'} Windows environment")

if missing:
    print("\nInstall missing packages with:")
    print(f'"{sys.executable}" -m pip install -r "{ROOT / "requirements.txt"}"')
    raise SystemExit(1)
print("\nEnvironment looks ready.")

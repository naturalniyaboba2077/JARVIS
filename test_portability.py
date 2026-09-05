# -*- coding: utf-8 -*-
"""Переносимость ядра: Jarvis должен импортироваться на Linux ARM.

Ядро переезжает на домашний сервер (Orange Pi 5 Pro), где нет ни pyautogui,
ни pycaw, ни comtypes, ни графического дисплея. Один неосторожный импорт
верхнего уровня снова сделает ядро незапускаемым — и выяснится это только при
разворачивании на плате. Этот файл ловит такое сразу.

Проверяется две вещи:
  1. статически — в модулях ядра нет незащищённых платформенных импортов;
  2. в рантайме — при заблокированных Windows-библиотеках ядро импортируется,
     а платформенные функции отдают отказ вместо исключения.

Запуск:  python test_portability.py
"""

import ast
import builtins
import importlib
import io
import os
import symtable
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
        print("  OK   " + name)
    else:
        _failed.append(name)
        print("  FAIL " + name + ((" :: " + detail) if detail else ""))


def section(title):
    print("\n=== " + title + " ===")


# Модули, которых на Linux ARM нет вообще либо которые требуют дисплея.
PLATFORM_ONLY = {
    "pyautogui", "pycaw", "comtypes", "screen_brightness_control",
    "pygetwindow", "keyboard", "webview", "winreg",
    "win32api", "win32gui", "win32con", "pywin32",
}

# Ядро. jarvis_platform намеренно исключён: он и есть место, где эти
# библиотеки живут, и все его импорты уже обёрнуты в try/except.
CORE_MODULES = ("jarvis.py", "jarvis_features.py", "project_agent.py")


def unguarded_platform_imports(path):
    """Платформенные импорты на уровне модуля вне try/except.

    Импорт внутри try/except или внутри функции безопасен: он либо
    перехватывается, либо вообще не выполняется на сервере.
    """
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    found = []
    for stmt in tree.body:          # только верхний уровень
        if isinstance(stmt, ast.Import):
            names = [a.name for a in stmt.names]
        elif isinstance(stmt, ast.ImportFrom):
            names = [stmt.module or ""]
        else:
            continue                # Try / def / class — безопасно
        for n in names:
            if n.split(".")[0] in PLATFORM_ONLY:
                found.append((n, stmt.lineno))
    return found


section("Статически: в ядре нет незащищённых платформенных импортов")
for mod in CORE_MODULES:
    bad = unguarded_platform_imports(mod)
    check("%s — чистый верхний уровень" % mod, not bad,
          "; ".join("%s (строка %d)" % (n, l) for n, l in bad))

check("jarvis_platform.py существует", os.path.exists("jarvis_platform.py"))
_plat_bad = unguarded_platform_imports("jarvis_platform.py")
check("jarvis_platform прячет платформенные импорты в try/except", not _plat_bad,
      "; ".join("%s (строка %d)" % (n, l) for n, l in _plat_bad))



def undefined_globals(path):
    """Обращения к глобальным именам, которых в модуле нет.

    При выносе кода из монолита тело функции легко оставляет ссылку на
    глобаль из jarvis.py, которая не переехала. Импорт такое не ловит —
    только вызов, а вызов в тестах часто подменён заглушкой. Так уже был
    пропущен NameError на `re` в jarvis_config._write_config_file.
    """
    text = io.open(path, encoding="utf-8").read()
    tree = ast.parse(text)
    top = symtable.symtable(text, path, "exec")

    # Именно связывания на уровне модуля, а не symtable.get_identifiers():
    # объявление `global X` внутри функции тоже попадает в identifiers, из-за
    # чего осиротевшая глобаль выглядела бы определённой. Так был пропущен
    # переезд _app_catalog_cache в jarvis_apps.
    bound = set(dir(builtins)) | {
        "__file__", "__name__", "__doc__", "__package__",
        "__spec__", "__loader__", "__builtins__", "__path__",
    }
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                bound |= {n.id for n in ast.walk(tgt) if isinstance(n, ast.Name)}
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            bound |= {n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)}
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name != "*":
                    bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, (ast.Try, ast.If, ast.For, ast.While, ast.With)):
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    bound.add(sub.name)
                elif isinstance(sub, ast.Assign):
                    for tgt in sub.targets:
                        bound |= {n.id for n in ast.walk(tgt) if isinstance(n, ast.Name)}
                elif isinstance(sub, (ast.Import, ast.ImportFrom)):
                    for alias in sub.names:
                        if alias.name != "*":
                            bound.add(alias.asname or alias.name.split(".")[0])
    known = bound

    # `from X import *` приносит имена, которых в тексте модуля не видно.
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            mod = importlib.import_module(node.module)
            known |= set(getattr(mod, "__all__", None) or dir(mod))

    missing = []

    def walk(table, scope):
        module_level = table.get_type() == "module"
        for sym in table.get_symbols():
            name = sym.get_name()
            # На уровне модуля имя не бывает "global" — там оно локальное,
            # поэтому проверяем отдельно: так был пропущен threading.Lock()
            # в jarvis_telegram, падавший прямо при импорте.
            unresolved = (sym.is_global() or
                          (module_level and sym.is_referenced() and not sym.is_assigned()))
            if unresolved and name not in known:
                missing.append("%s -> %s" % (scope, name))
        for child in table.get_children():
            walk(child, scope + "." + child.get_name())

    walk(top, os.path.basename(path))
    return sorted(set(missing))


section("Модули ядра не ссылаются на чужие глобали")
for _mod in sorted(f for f in os.listdir(".")
                   if f.startswith("jarvis") and f.endswith(".py")):
    _missing = undefined_globals(_mod)
    check("%s — все имена объявлены" % _mod, not _missing, "; ".join(_missing))

section("В рантайме: ядро при заблокированных Windows-библиотеках")

_BLOCKED_ROOTS = {"pyautogui", "pycaw", "comtypes",
                  "screen_brightness_control", "pygetwindow", "keyboard"}

# Статическая часть выше импортирует модули ядра, чтобы разрешить звёздочные
# импорты. Их надо выгрузить, иначе они останутся в кэше уже подхватившими
# настоящие Windows-библиотеки, и симуляция ничего не проверит.
for _m in list(sys.modules):
    _root = _m.split(".")[0]
    if _root in _BLOCKED_ROOTS or _root.startswith("jarvis"):
        del sys.modules[_m]

_real_import = builtins.__import__


def _blocking_import(name, *args, **kwargs):
    if name.split(".")[0] in _BLOCKED_ROOTS:
        raise ImportError("нет модуля %s (симуляция Linux ARM)" % name)
    return _real_import(name, *args, **kwargs)


builtins.__import__ = _blocking_import
try:
    import jarvis_platform as plat
    check("jarvis_platform импортируется", True)

    caps = plat.capabilities()
    check("capabilities() честно сообщает об отсутствии рабочего стола",
          caps["desktop"] is False and caps["mixer"] is False, str(caps))

    ok, note = plat.press_media_key("playpause")
    check("media-клавиша: отказ с пояснением", ok is False and bool(note), note)
    ok, note = plat.set_master_volume(50)
    check("громкость: отказ с пояснением", ok is False and bool(note), note)
    ok, note = plat.set_brightness(50)
    check("яркость: отказ с пояснением", ok is False and bool(note), note)
    ok, note = plat.paste_from_clipboard()
    check("вставка: отказ с пояснением", ok is False and bool(note), note)
    ok, note = plat.press_media_key("чепуха")
    check("неизвестное медиа-действие отсекается", ok is False, note)
    check("get_master_volume отдаёт -1", plat.get_master_volume() == -1)
    check("get_brightness отдаёт -1", plat.get_brightness() == -1)

    import jarvis_features as feat
    check("jarvis_features импортируется", True)
    check("рабочий стол без pyautogui: отказ, а не падение",
          isinstance(feat.window_show_desktop(), str))
    check("вставка из буфера без pyautogui: отказ, а не падение",
          isinstance(feat.clipboard_paste(), str))

    import jarvis
    check("jarvis (ядро) импортируется", True)
    check("get_volume отдаёт -1", jarvis.get_volume() == -1)
    check("set_volume честно возвращает отказ", jarvis.set_volume(50) is False)
    check("media_control честно возвращает отказ",
          jarvis.media_control("playpause") is False)
    check("set_brightness возвращает текст", isinstance(jarvis.set_brightness(50), str))

    # lock_pc и type_text имеют реальные побочные эффекты (блокировка сеанса,
    # вставка в активное окно) и от заблокированных библиотек не зависят.
    # Проверяем только маршрут через платформенный слой, сами действия не трогаем.
    _real_lock = plat.lock_workstation
    plat.lock_workstation = lambda: (False, "заглушка теста")
    try:
        check("lock_pc идёт через платформенный слой",
              jarvis.lock_pc() == "заглушка теста")
    finally:
        plat.lock_workstation = _real_lock

    class _FakeClipboard(object):
        def __init__(self):
            self.buf = ""

        def paste(self):
            return self.buf

        def copy(self, text):
            self.buf = text

    _real_paste = plat.paste_from_clipboard
    import jarvis_tools
    _real_clip = jarvis_tools.pyperclip
    _calls = []
    plat.paste_from_clipboard = lambda: (_calls.append(1), (False, "заглушка теста"))[1]
    jarvis_tools.pyperclip = _FakeClipboard()
    try:
        check("type_text честно возвращает отказ", jarvis.type_text("проверка") is False)
        check("type_text вставляет через платформенный слой", _calls == [1])
    finally:
        plat.paste_from_clipboard = _real_paste
        jarvis_tools.pyperclip = _real_clip
except ImportError as e:
    check("ядро импортируется при заблокированных Windows-библиотеках", False, str(e))
except Exception as e:
    check("ядро не падает при заблокированных Windows-библиотеках", False,
          "%s: %s" % (type(e).__name__, e))
finally:
    builtins.__import__ = _real_import

print("\n" + "=" * 60)
total = _passed + len(_failed)
if _failed:
    print("ИТОГ: %d/%d — провалено: %s" % (_passed, total, ", ".join(_failed)))
    sys.exit(1)
print("ИТОГ: %d/%d проверок прошло" % (_passed, total))

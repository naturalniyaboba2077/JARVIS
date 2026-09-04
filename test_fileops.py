# -*- coding: utf-8 -*-
"""Версионирование правок: каждое изменение должно откатываться.

Джарвис правит файлы на сервере без свидетелей, поэтому обещание «любую
правку можно отменить» обязано выполняться буквально — иначе на защите его
проверят первым же вопросом.

Запуск:  python test_fileops.py
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

os.chdir(os.path.dirname(os.path.abspath(__file__)))

# История уходит во временную папку: настоящую историю рядом с Джарвисом
# тест трогать не должен.
_HISTORY = Path(tempfile.mkdtemp(prefix="jarvis_history_"))
os.environ["JARVIS_FILE_HISTORY"] = str(_HISTORY)

import jarvis_fileops as fops

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


_projects = []


def new_project(name="проект"):
    root = Path(tempfile.mkdtemp(prefix="jarvis_proj_")) / name
    root.mkdir(parents=True)
    _projects.append(root.parent)
    return root.resolve()


try:
    section("Создание файла и его откат")
    root = new_project()
    target = root / "src" / "новый.txt"
    msg = fops.write_versioned(root, target, "первое содержимое")
    check("новый файл создан", target.is_file() and "Создан" in msg, msg)
    check("вложенные папки созданы", target.parent.is_dir())
    check("правка попала в историю", "src/новый.txt" in fops.list_history(root))

    msg = fops.undo_last(root)
    check("откат удалил созданный файл", not target.exists(), msg)
    check("после отката отменять нечего", "нечего" in fops.undo_last(root))

    section("Перезапись и возврат прежнего содержимого")
    root = new_project()
    target = root / "конфиг.txt"
    target.write_text("исходный текст", encoding="utf-8")
    fops.write_versioned(root, target, "новый текст")
    check("файл перезаписан", target.read_text(encoding="utf-8") == "новый текст")

    fops.undo_last(root)
    check("прежнее содержимое вернулось",
          target.read_text(encoding="utf-8") == "исходный текст",
          target.read_text(encoding="utf-8"))

    section("Несколько правок откатываются по одной, с конца")
    root = new_project()
    a, b = root / "a.txt", root / "b.txt"
    a.write_text("A0", encoding="utf-8")
    fops.write_versioned(root, a, "A1")
    fops.write_versioned(root, b, "B1")
    check("в истории две правки", len(fops.pending_changes(root)) == 2)

    fops.undo_last(root)
    check("сначала откатилась последняя (b)", not b.exists() and a.read_text(encoding="utf-8") == "A1")
    fops.undo_last(root)
    check("затем предыдущая (a)", a.read_text(encoding="utf-8") == "A0")
    check("история опустела", not fops.pending_changes(root))

    section("Границы и отказы")
    root = new_project()
    huge = "я" * (fops.MAX_FILE_BYTES + 10)
    msg = fops.write_versioned(root, root / "big.txt", huge)
    check("слишком большое содержимое отклонено",
          not (root / "big.txt").exists() and "большое" in msg, msg)
    check("отклонённая запись не попала в историю", not fops.pending_changes(root))

    section("Проекты с одинаковым именем не делят историю")
    one, two = new_project("общий"), new_project("общий")
    check("имена совпадают", one.name == two.name)
    check("папки истории разные", fops.history_root(one) != fops.history_root(two))
    fops.write_versioned(one, one / "f.txt", "первый")
    check("правка видна только в своём проекте",
          len(fops.pending_changes(one)) == 1 and not fops.pending_changes(two))

    section("Потерянная резервная копия не притворяется откатом")
    root = new_project()
    target = root / "важное.txt"
    target.write_text("ценные данные", encoding="utf-8")
    fops.write_versioned(root, target, "испорчено")
    for blob in (fops.history_root(root) / "blobs").iterdir():
        blob.unlink()
    msg = fops.undo_last(root)
    check("о потере копии сообщается честно", "потеряна" in msg, msg)
    check("файл не тронут вслепую", target.read_text(encoding="utf-8") == "испорчено")
finally:
    shutil.rmtree(_HISTORY, ignore_errors=True)
    for parent in _projects:
        shutil.rmtree(parent, ignore_errors=True)

print("\n" + "=" * 60)
total = _passed + len(_failed)
if _failed:
    print("ИТОГ: %d/%d — провалено: %s" % (_passed, total, ", ".join(_failed)))
    sys.exit(1)
print("ИТОГ: %d/%d проверок прошло" % (_passed, total))

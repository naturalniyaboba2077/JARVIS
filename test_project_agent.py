"""Offline checks for the bounded project-agent tool layer."""

import os
import tempfile
from pathlib import Path

import project_agent


passed = 0
failed = 0


def check(name, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp).resolve()
    project = root / "demo"
    project.mkdir()
    (project / "app.py").write_text("print('old')\n", encoding="utf-8")
    old_roots = os.environ.get("JARVIS_PROJECT_ROOTS")
    os.environ["JARVIS_PROJECT_ROOTS"] = str(root)
    try:
        resolved = project_agent._resolve_project("demo")
        check("проект разрешается только внутри разрешённого корня", resolved == project)
        try:
            project_agent._inside(project, "../outside.txt")
            escaped = True
        except ValueError:
            escaped = False
        check("выход через .. блокируется", not escaped)
        check("агент читает файл", "old" in project_agent._execute(
            project, "read_file", {"path": "app.py"}))
        result = project_agent._execute(
            project, "write_file", {"path": "app.py", "content": "print('new')\n"})
        check("агент пишет файл", "Записано" in result and "new" in
              (project / "app.py").read_text(encoding="utf-8"))
        check("разрушительная команда блокируется", "заблокирована" in
              project_agent._execute(project, "run_command", {"command": "rm -rf ."}))
        check("проверочная команда выполняется", "exit=0" in
              project_agent._execute(project, "run_command", {
                  "command": "python -c \"print('ok')\""}))
    finally:
        if old_roots is None:
            os.environ.pop("JARVIS_PROJECT_ROOTS", None)
        else:
            os.environ["JARVIS_PROJECT_ROOTS"] = old_roots

print(f"\n{passed}/{passed + failed} верно")
raise SystemExit(1 if failed else 0)

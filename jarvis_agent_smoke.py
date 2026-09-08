"""Opt-in real local inference on a large SYNTHETIC file; no app/audio/shell.

python -B jarvis_agent_smoke.py
Uses the selected LM Studio code model. Does not modify/restart the live app.
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch
from urllib.parse import urlparse


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    import jarvis_config  # config must precede any model environment reads
    import jarvis_llm as llm
    import project_agent as agent

    if urlparse(llm.LM_STUDIO_URL).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("Only an explicitly local LM Studio endpoint is supported")
    observations, stages = [], []
    execute = agent._execute

    def tracked(root, name, args, mode="inspect"):
        assert mode == "inspect"
        result = execute(root, name, args, mode=mode)
        observations.append({"tool": name, "path": args.get("path"),
                             "output_bytes": len(result.encode("utf-8"))})
        return result

    def progress(stage):
        stages.append(stage)
        print(stage, flush=True)

    with tempfile.TemporaryDirectory(prefix="jarvis-agent-smoke-") as directory:
        project = Path(directory) / "SyntheticAgentCheck"
        project.mkdir()
        source = "def divide(a, b):\n    return a / b\n\n" + "# synthetic padding 0123456789\n" * 7500
        file = project / "app.py"
        file.write_text(source, encoding="utf-8", newline="")
        start = time.perf_counter()
        with patch.dict(os.environ, {"JARVIS_PROJECT_ROOTS": directory}), \
                patch.object(agent, "_execute", side_effect=tracked), \
                patch.object(agent, "MAX_TOOL_STEPS", 1 if "--partial" in sys.argv else 3), \
                patch.object(agent.subprocess, "run", side_effect=AssertionError("No host shell")):
            report = agent.run_project_agent(llm.get_lmstudio_client(), llm.LM_STUDIO_CODE_MODEL,
                str(project), "Прочитай начало app.py. Проверь только функцию divide в начале файла: "
                "объясни, что произойдёт при b=0. Остальной файл — комментарии-заполнители, "
                "не нужно читать их все. Не меняй файлы и не запускай код. Дай отчёт.",
                mode="inspect", progress_fn=progress)
        assert file.read_bytes() == source.encode("utf-8"), "Synthetic file changed"
        reads = [item for item in observations if item["tool"] == "read_file"]
        assert reads, "Model did not read the synthetic file"
        assert all(item["output_bytes"] <= agent.MAX_TOOL_BYTES for item in reads), "Unbounded page"
        assert "Фактически выполнено" in report and "тесты приложения не запускались" in report
        if "--partial" in sys.argv:
            assert "Частичный отчёт" in report and "лимит шагов" in report
        # This assertion checks the known fixture fact, not a user's project's correctness.
        assert "ZeroDivisionError" in report or "делени" in report.lower(), "No useful fixture finding"
        print(json.dumps({"model": llm.LM_STUDIO_CODE_MODEL, "seconds": round(time.perf_counter()-start, 2),
                          "fixture_bytes": len(source.encode("utf-8")), "tools": observations}, ensure_ascii=False), flush=True)
        print(report, flush=True)


if __name__ == "__main__":
    main()

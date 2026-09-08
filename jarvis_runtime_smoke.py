"""Opt-in LOCAL integration checks on synthetic input, without audio/app actions.

python -B jarvis_runtime_smoke.py
Requires the configured LM Studio model and Ollama server. No cloud fallback,
microphone, personal memory, personal project or host-shell tools are used.
Not part of run_tests.py: real model inference consumes RAM/VRAM.
"""
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
    import jarvis
    import jarvis_llm as llm
    import jarvis_dashboard as dashboard
    import project_agent as agent

    for url in (llm.LM_STUDIO_URL, llm.OLLAMA_URL):
        if urlparse(url).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise SystemExit("Smoke-check accepts loopback servers only")
    messages = [{"role": "system", "content": jarvis.SYSTEM_PROMPT_BASE},
                {"role": "user", "content": "Что ты умеешь?"}]
    start = time.perf_counter()
    with patch.object(llm, "LLM_ENGINE", "lmstudio"), patch.object(llm, "OPENROUTER_API_KEY", None), \
            patch.object(llm, "_ollama_deltas", side_effect=AssertionError("Primary must work")):
        result = "".join(llm._llm_deltas(messages))
    assert result.strip(), "No primary response"
    assert dashboard.snapshot()["services"]["llm"]["engine"] == "lmstudio"
    print(f"PRIMARY full prompt: {time.perf_counter()-start:.2f}s; {result}", flush=True)

    calls = []
    execute = agent._execute
    def tracked(root, name, args, mode="modify"):
        assert mode == "inspect", "No modifying tools in this check"
        calls.append(name)
        return execute(root, name, args, mode=mode)

    with tempfile.TemporaryDirectory(prefix="jarvis-local-smoke-") as directory:
        project = Path(directory) / "Demo"
        project.mkdir()
        source = "def divide(a, b):\n    return a / b\n"
        (project / "app.py").write_text(source, encoding="utf-8")
        start = time.perf_counter()
        with patch.dict(os.environ, {"JARVIS_PROJECT_ROOTS": directory}), \
                patch.object(agent, "_execute", side_effect=tracked), \
                patch.object(agent, "MAX_TOOL_STEPS", 5), \
                patch.object(agent.subprocess, "run", side_effect=AssertionError("No host shell")):
            report = agent.run_project_agent(llm.get_lmstudio_client(), llm.LM_STUDIO_CODE_MODEL,
                str(project), "Прочитай app.py. Объясни, что происходит при b=0. Ничего не меняй.", mode="inspect")
        assert (project / "app.py").read_text(encoding="utf-8") == source
        assert "read_file" in calls, "Model did not actually read the synthetic code"
        assert "Фактически выполнено" in report, "No execution evidence in report"
        print(f"PROJECT tools={calls}, {time.perf_counter()-start:.2f}s\n{report}", flush=True)

    start = time.perf_counter()
    with patch.object(llm, "LLM_ENGINE", "lmstudio"), patch.object(llm, "OPENROUTER_API_KEY", None), \
            patch.object(llm, "_lmstudio_deltas", side_effect=RuntimeError("Synthetic primary failure")):
        response = "".join(llm._llm_deltas([
            {"role": "user", "content": "Одним предложением: что такое локальная языковая модель?"}]))
    assert response.strip(), "Ollama fallback returned no text"
    assert dashboard.snapshot()["services"]["llm"]["engine"] == "local"
    print(f"OLLAMA fallback: {time.perf_counter()-start:.2f}s; {response}", flush=True)


if __name__ == "__main__":
    main()

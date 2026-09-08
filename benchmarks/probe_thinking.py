"""Loopback-only diagnostics; no tools, config changes, downloads or fallback."""
import json
from pathlib import Path
import sys
import time

import httpx


def main():
    model = sys.argv[1]
    output = Path(__file__).resolve().parents[1] / "logs/model_comparison/fast-code-thinking-probes.json"
    records = []
    messages = [{"role": "user", "content": "Ответь одним словом: готов?"}]
    probes = [
        ("openai-none", "/v1/chat/completions", {"messages": messages, "max_tokens": 64,
                                                "reasoning_effort": "none"}),
        ("openai-template", "/v1/chat/completions", {"messages": messages, "max_tokens": 64,
                     "chat_template_kwargs": {"enable_thinking": False}}),
        ("native-off", "/api/v1/chat", {"input": messages[0]["content"], "max_output_tokens": 64,
                                       "reasoning": "off", "store": False}),
        ("raw-closed-think", "/v1/completions", {"max_tokens": 64,
          "prompt": "<|im_start|>user\nОтветь одним словом: готов?<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"}),
    ]
    with httpx.Client(base_url="http://127.0.0.1:1234", trust_env=False, timeout=30) as client:
        models = client.get("/api/v1/models").json()["models"]
        if not any(i["id"] == model for m in models for i in m.get("loaded_instances", [])):
            raise RuntimeError("Explicitly loaded benchmark instance required")
        for name, route, body in probes:
            body.update(model=model, temperature=0)
            started = time.perf_counter()
            try:
                response = client.post(route, json=body)
                record = {"name": name, "request": body, "status": response.status_code,
                          "response": response.json(), "seconds": time.perf_counter() - started}
            except Exception as exc:
                record = {"name": name, "error": type(exc).__name__, "seconds": time.perf_counter()-started}
            records.append(record)
            output.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(record, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()

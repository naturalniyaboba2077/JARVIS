"""Diagnostic: replay synthetic dialogue with a short system instruction.

Not a production benchmark and not a Jarvis configuration change. Keeps saved
non-system history verbatim. No tools, cloud fallback, or personal data loading.
"""
import argparse
import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile

from model_compare import Inference, SOURCE, isolated_imports, make_workspace, save_result

COMPACT = (
    "Ты Джарвис, личный помощник владельца. Отвечай по-русски, на вы, иногда сэр. "
    "Сейчас доступен только разговор, инструменты не выполняются. "
    "Учитывай историю. Не выдумывай воспоминания, результаты поиска, прочитанные файлы "
    "и выполненные действия. Если данных нет, прямо скажи об этом. "
    "Отвечай кратко, без Markdown, эмодзи и кавычек."
)


def compact_messages(messages):
    if not messages or messages[0]["role"] != "system":
        raise ValueError("Expected saved Jarvis system instruction")
    return [{"role": "system", "content": COMPACT}, *messages[1:]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    if not args.model.startswith("jarvis-bench-") or not args.label.replace("-", "").isalnum():
        parser.error("Use a separate jarvis-bench- instance and a simple label")
    original = json.loads(args.source.read_text(encoding="utf-8"))
    output = SOURCE / "logs" / "model_comparison" / (args.label + ".json")
    if output.exists():
        raise FileExistsError(output)
    data = {"model": args.model, "diagnostic": "compact-prompt-not-native-Jarvis",
            "source": str(args.source), "source_sha256": hashlib.sha256(args.source.read_bytes()).hexdigest(),
            "system": COMPACT, "rows": []}
    with tempfile.TemporaryDirectory(prefix="jarvis-prompt-probe-") as temporary, contextlib.ExitStack() as cleanup:
        root = Path(temporary)
        cleanup.callback(os.chdir, SOURCE)
        cleanup.callback(logging.shutdown)
        make_workspace(root)
        isolated_imports(root, args.model, 8192)
        import httpx
        with httpx.Client(trust_env=False, timeout=10) as client:
            models = client.get("http://127.0.0.1:1234/api/v1/models").json()["models"]
        if not any(i["id"] == args.model for m in models for i in m.get("loaded_instances", [])):
            raise RuntimeError("Benchmark instance is not loaded")
        for current in original["passes"]:
            infer = Inference(args.model, current["seed"])
            try:
                for row in current["rows"]:
                    if row["kind"] != "chat":
                        continue
                    request = row["inference"]
                    result = infer.generate(compact_messages(request["messages"]),
                                            temperature=request["temperature"], max_tokens=request["max_tokens"])
                    data["rows"].append({"id": row["id"], "seed": current["seed"], "inference": result})
                    save_result(output, data)
                    print(f"PROBE {current['seed']} {row['id']}: {result['seconds']:.2f}s", flush=True)
            finally:
                infer.client.close()
    print("RESULT " + str(output), flush=True)


if __name__ == "__main__":
    main()

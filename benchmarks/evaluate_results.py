"""Offline, versioned regrading of saved model outputs; no inference or actions.

Raw benchmark v2 checks remain intact. Semantic honesty and humor still require
human review; this script never calls a model response a completed user task.
"""
import argparse
import json
from pathlib import Path
import re
import statistics


def tag_checks(row):
    """Correct two v2 rubric gaps without altering raw model responses."""
    checks = dict(row["checks"])
    actions = row.get("parsed_actions", [])
    if row["id"] == "file_find":
        # Jarvis discovery folds ё to е for name matching; this is not a bad path.
        checks["correct_argument"] = bool(actions and actions[0]["name"] == "FILE:FIND"
            and any("отчет.txt" in value.casefold().replace("ё", "е")
                    for value in actions[0]["args"]))
    if row["id"] == "web_search":
        # The old 'astra' substring check also accepted dropping the model number.
        argument = " ".join(str(x) for a in actions for x in a["args"]).casefold()
        words = set(re.findall(r"\w+", argument))
        checks["correct_argument"] = {"gpt", "astra", "6"}.issubset(words)
    return checks


def median(records, key):
    values = [r[key] for r in records if isinstance(r.get(key), (int, float))]
    return round(statistics.median(values), 3) if values else None


def summarize(data):
    rows = [row for p in data["passes"] for row in p["rows"]]
    tags = [row for row in rows if row["kind"] == "tags"]
    dialogue = [row for row in rows if row["kind"] in {"chat", "banter"}]
    records = [row["inference"] for row in dialogue]
    failures = [{"id": r["id"], "failed_checks": [k for k, v in tag_checks(r).items() if not v],
                 "response": r["inference"]["content"]} for r in tags if not all(tag_checks(r).values())]
    return {"label": data["label"], "rubric_version": 3, "transport": data.get("transport"),
            "context": data["context"], "rows": len(rows), "tag_total": len(tags),
            "tag_passed": len(tags) - len(failures), "tag_failures": failures,
            "dialogue_total": len(dialogue),
            "dialogue_completed": sum(r["checks"]["completed"] for r in dialogue),
            "dialogue_first_text_median_s": median(records, "first_text_s"),
            "dialogue_first_speech_chunk_median_s": median(records, "first_speech_chunk_s"),
            "dialogue_full_median_s": median(records, "seconds"),
            "tag_full_median_s": median([r["inference"] for r in tags], "seconds"),
            "projects": [{"id": r["id"], "seconds": round(r["seconds"], 3), "checks": r["checks"],
                          "verification_requested": any(t["tool"] == "run_command" for t in r["traces"])}
                         for r in rows if r["kind"] == "project"],
            "warning": "Automatic checks are not a semantic task-success score. Human review required."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps([summarize(json.loads(p.read_text(encoding="utf-8"))) for p in args.files],
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

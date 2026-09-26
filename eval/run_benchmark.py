import argparse
import json
import time
from pathlib import Path
from typing import Any

from app.pipeline import run_pipeline


def load_questions(path: str, db: str | None, limit: int | None) -> list[dict[str, str]]:
    questions = json.loads(Path(path).read_text(encoding="utf-8"))
    if db:
        questions = [item for item in questions if item["db"] == db]
    return questions[:limit] if limit else questions


def run(
    questions_path: str,
    output_path: str | None = None,
    db: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    questions = load_questions(questions_path, db, limit)
    if not questions:
        raise ValueError("No benchmark questions matched the selected filters")

    output = Path(output_path) if output_path else None
    results: list[dict[str, Any]] = []
    started = time.perf_counter()
    output_file = output.open("w", encoding="utf-8") if output else None
    try:
        for number, item in enumerate(questions, start=1):
            prompt_started = time.perf_counter()
            print(f"[{number}/{len(questions)}] {item['db']}: {item['question']}", flush=True)
            try:
                result = run_pipeline(item["question"], item["db"])
                record = {
                    **item,
                    "result": result.model_dump(),
                    "elapsed_ms": round((time.perf_counter() - prompt_started) * 1000, 3),
                }
            except Exception as error:
                record = {
                    **item,
                    "result": {
                        "status": "failed",
                        "reason": str(error),
                        "attempts": 0,
                    },
                    "elapsed_ms": round((time.perf_counter() - prompt_started) * 1000, 3),
                }
            results.append(record)
            line = json.dumps(record, default=str)
            print(line, flush=True)
            if output_file:
                output_file.write(line + "\n")
                output_file.flush()
    finally:
        if output_file:
            output_file.close()

    successful = sum(record["result"]["status"] == "success" for record in results)
    elapsed_ms = (time.perf_counter() - started) * 1000
    summary = {
        "questions": len(results),
        "successful": successful,
        "execution_accuracy": round(successful / len(results), 3),
        "mean_elapsed_ms": round(sum(record["elapsed_ms"] for record in results) / len(results), 3),
        "total_elapsed_ms": round(elapsed_ms, 3),
        "output": str(output) if output else None,
    }
    print(json.dumps({"summary": summary}, sort_keys=True), flush=True)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run SchemaDrill prompts sequentially")
    parser.add_argument("--questions", default="eval/questions.json")
    parser.add_argument("--output", help="Optional JSONL result file")
    parser.add_argument("--db", help="Only run questions for this schema")
    parser.add_argument("--limit", type=int, help="Run only the first N matching questions")
    args = parser.parse_args()
    run(args.questions, args.output, args.db, args.limit)


if __name__ == "__main__":
    main()

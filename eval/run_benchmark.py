import argparse
import json
import time
from pathlib import Path
from typing import Any

from app.db import readonly_connection
from app.pipeline import run_pipeline
from eval.evaluator import evaluate_sql_pair
from eval.models import SQLEvalSummary


def load_questions(path: str, db: str | None, limit: int | None) -> list[dict[str, Any]]:
    questions = json.loads(Path(path).read_text(encoding="utf-8"))
    if db:
        questions = [item for item in questions if item["db"] == db]
    return questions[:limit] if limit else questions


def run(
    questions_path: str,
    output_path: str | None = None,
    db: str | None = None,
    limit: int | None = None,
    dataset: str | None = None,
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
                generated_sql = None

                def pre_execute_check(sql: str) -> None:
                    nonlocal generated_sql
                    generated_sql = sql

                result = run_pipeline(
                    item["question"], item["db"], pre_execute_check=pre_execute_check
                )
                generated_sql = result.sql or generated_sql
                evaluation = evaluate_sql_pair(
                    item.get("question_id", f"{item['db']}:{number}"),
                    item["gold_sql"],
                    generated_sql,
                    readonly_connection,
                )
                record = {
                    **item,
                    "result": result.model_dump(),
                    "generated_sql": generated_sql,
                    **evaluation.model_dump(exclude={"question_id", "gold_sql", "generated_sql"}),
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
                    "generated_sql": None,
                    "gold_executed": False,
                    "generated_executed": False,
                    "gold_error": None,
                    "generated_error": str(error),
                    "execution_match": False,
                    "ast_match": None,
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

    evaluated = [record for record in results if "execution_match" in record]
    execution_successes = sum(record["generated_executed"] for record in evaluated)
    execution_matches = sum(record["execution_match"] for record in evaluated)
    ast_matches = sum(record["ast_match"] is True for record in evaluated)
    elapsed_ms = (time.perf_counter() - started) * 1000
    total = len(evaluated)
    summary_model = SQLEvalSummary(
        dataset=dataset or Path(questions_path).stem,
        total_examples=total,
        execution_accuracy=execution_matches / total if total else 0,
        execution_success_rate=execution_successes / total if total else 0,
        ast_match_rate=ast_matches / total if total else 0,
        execution_failures=total - execution_successes,
        result_mismatches=total - execution_matches,
    )
    summary = {
        **summary_model.model_dump(exclude={"records", "diagnostics"}),
        "questions": len(results),
        "successful": execution_successes,
        "sql_accuracy": summary_model.ast_match_rate,
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
    parser.add_argument("--dataset", help="Dataset name to include in the summary")
    args = parser.parse_args()
    run(args.questions, args.output, args.db, args.limit, args.dataset)


if __name__ == "__main__":
    main()

import argparse
import json
from pathlib import Path
from typing import Any

from app.db import readonly_connection
from eval.evaluator import evaluate_sql_pair
from eval.run_benchmark import run


def run_evaluator_smoke(questions_path: str, dataset: str) -> dict[str, Any]:
    questions = json.loads(Path(questions_path).read_text(encoding="utf-8"))
    if not questions:
        raise ValueError("No evaluation cases found")

    results = [
        evaluate_sql_pair(
            case.get("question_id", f"{dataset}:{number}"),
            case["gold_sql"],
            case["gold_sql"],
            readonly_connection,
        )
        for number, case in enumerate(questions, start=1)
    ]
    summary = {
        "dataset": dataset,
        "total_examples": len(results),
        "result_set_accuracy": sum(result.result_match for result in results) / len(results),
        "execution_success_rate": sum(result.generated_executed for result in results)
        / len(results),
        "ast_match_rate": sum(result.ast_match is True for result in results) / len(results),
    }
    summary["execution_accuracy"] = summary["result_set_accuracy"]
    print(json.dumps(summary, sort_keys=True))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a SchemaDrill SQL evaluation dataset")
    parser.add_argument("--questions", default="evaluation/chinook/cases.json")
    parser.add_argument("--output")
    parser.add_argument("--db")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dataset", default="chinook")
    parser.add_argument(
        "--gold-as-generated",
        action="store_true",
        help="Run evaluator/database smoke checks without calling Gemini",
    )
    args = parser.parse_args()
    if args.gold_as_generated:
        run_evaluator_smoke(args.questions, args.dataset)
    else:
        run(args.questions, args.output, args.db, args.limit, args.dataset)


if __name__ == "__main__":
    main()

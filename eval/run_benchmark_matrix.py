"""Run the 2x2 retry/FK benchmark matrix and compare resume metrics."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

RUNS = (
    ("no_retry_no_fk", True, False),
    ("retry_no_fk", False, False),
    ("no_retry_fk", True, True),
    ("retry_fk", False, True),
)


def load_summary(path: Path) -> dict[str, Any]:
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        if line.strip():
            record = json.loads(line)
            if isinstance(record, dict) and isinstance(record.get("summary"), dict):
                return record["summary"]
    raise ValueError(f"No benchmark summary found in {path}")


def _metric(summary: dict[str, Any]) -> dict[str, Any]:
    total = int(summary["total_examples"])
    accuracy = float(summary["result_set_accuracy"])
    latency_ms = float(summary["mean_elapsed_ms"])
    return {
        "examples": total,
        "result_matches": total - int(summary["result_mismatches"]),
        "result_set_accuracy": accuracy,
        "accuracy_percent": accuracy * 100,
        "mean_elapsed_ms": latency_ms,
        "mean_elapsed_seconds": latency_ms / 1000,
    }


def _comparison(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "baseline": baseline,
        "candidate": candidate,
        "accuracy_delta_percentage_points": (
            (candidate["result_matches"] - baseline["result_matches"]) * 100 / baseline["examples"]
        ),
        "mean_elapsed_delta_ms": candidate["mean_elapsed_ms"] - baseline["mean_elapsed_ms"],
        "mean_elapsed_delta_seconds": (candidate["mean_elapsed_ms"] - baseline["mean_elapsed_ms"])
        / 1000,
    }


def build_report(summaries: dict[str, dict[str, Any]], dataset: str) -> dict[str, Any]:
    metrics = {name: _metric(summaries[name]) for name, _, _ in RUNS}
    example_counts = {item["examples"] for item in metrics.values()}
    if len(example_counts) != 1:
        raise ValueError("Benchmark runs evaluated different numbers of examples")

    return {
        "dataset": dataset,
        "examples_per_run": example_counts.pop(),
        "metrics": metrics,
        "comparisons": {
            "self_correction_without_fk": _comparison(
                metrics["no_retry_no_fk"], metrics["retry_no_fk"]
            ),
            "self_correction_with_fk": _comparison(metrics["no_retry_fk"], metrics["retry_fk"]),
            "fk_expansion_with_retries": _comparison(metrics["retry_no_fk"], metrics["retry_fk"]),
            "fk_expansion_without_retries": _comparison(
                metrics["no_retry_no_fk"], metrics["no_retry_fk"]
            ),
        },
    }


def run_matrix(
    questions: Path,
    output_dir: Path,
    db: str | None = None,
    limit: int | None = None,
    dataset: str = "chinook",
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    summaries: dict[str, dict[str, Any]] = {}

    for name, disable_self_correction, enable_fk_expansion in RUNS:
        output_path = output_dir / f"{name}.jsonl"
        command = [
            sys.executable,
            "-m",
            "eval.run_benchmark",
            "--questions",
            str(questions),
            "--output",
            str(output_path),
            "--dataset",
            f"{dataset}-{name}",
            "--warmup",
        ]
        if db:
            command.extend(("--db", db))
        if limit is not None:
            command.extend(("--limit", str(limit)))
        if disable_self_correction:
            command.append("--disable-self-correction")

        env = os.environ.copy()
        env["DISABLE_SELF_CORRECTION"] = str(disable_self_correction).lower()
        env["ENABLE_FK_EXPANSION"] = str(enable_fk_expansion).lower()

        print(
            f"Starting {name}: disable_self_correction={disable_self_correction}, "
            f"enable_fk_expansion={enable_fk_expansion}",
            flush=True,
        )
        completed = subprocess.run(command, env=env, capture_output=True, text=True)
        if completed.returncode:
            details = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(f"Benchmark run {name} failed:\n{details[-4000:]}")
        summaries[name] = load_summary(output_path)
        print(
            f"Finished {name}: "
            f"result_set_accuracy={summaries[name]['result_set_accuracy']}, "
            f"mean_elapsed_ms={summaries[name]['mean_elapsed_ms']}; "
            f"raw results: {output_path}",
            flush=True,
        )

    report = build_report(summaries, dataset)
    report_path = output_dir / "matrix_summary.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True), flush=True)
    print(f"Aggregate report: {report_path}", flush=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run all retry/FK combinations and compare result accuracy and latency"
    )
    parser.add_argument("--questions", type=Path, default=Path("eval/questions.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("benchmark-results"))
    parser.add_argument("--db", help="Only run questions for this schema")
    parser.add_argument("--limit", type=int, help="Run only the first N matching questions")
    parser.add_argument("--dataset", default="chinook")
    args = parser.parse_args()
    run_matrix(args.questions, args.output_dir, args.db, args.limit, args.dataset)


if __name__ == "__main__":
    main()

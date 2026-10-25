"""Reproducible orchestration and metrics for SchemaDrill benchmarks."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
import subprocess
import time
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psycopg

from app.config import Settings, get_settings
from app.instrumentation import PipelineTelemetry
from app.models import QueryResult
from app.pipeline import run_pipeline
from eval.evaluator import evaluate_sql_pair
from evaluation.datasets import dataset_questions, get_dataset

MATRIX_CONFIGS = (
    {"name": "no_retry_no_fk", "disable_self_correction": True, "enable_fk_expansion": False},
    {"name": "retry_no_fk", "disable_self_correction": False, "enable_fk_expansion": False},
    {"name": "no_retry_fk", "disable_self_correction": True, "enable_fk_expansion": True},
    {"name": "retry_fk", "disable_self_correction": False, "enable_fk_expansion": True},
)

_SECRET_PATTERNS = (
    (
        re.compile(r"(?i)(password|passwd|api[_-]?key|token|secret)(\s*[=:]\s*)\S+"),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"(?i)(postgres(?:ql)?://)([^/\s:@]+):([^@\s]+)@"), r"\1[REDACTED]:[REDACTED]@"),
    (re.compile(r"(?i)\bAIza[0-9A-Za-z_-]+"), "[REDACTED]"),
)


def redact_error(value: str | None) -> str | None:
    if value is None:
        return None
    redacted = value
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted[:2000]


def percentile(values: list[float], percent: float) -> float | None:
    """Return the nearest-rank percentile, with linear interpolation."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percent / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _git_commit() -> str | None:
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            or None
        )
    except (OSError, subprocess.CalledProcessError):
        return None


def dataset_hash(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _config_settings(base: Settings, config: dict[str, Any]) -> Settings:
    values = base.model_dump()
    values.update(
        disable_self_correction=bool(config["disable_self_correction"]),
        enable_fk_expansion=bool(config["enable_fk_expansion"]),
    )
    return Settings(**values)


def aggregate_records(
    records: list[dict[str, Any]], dataset: str, config: dict[str, Any]
) -> dict[str, Any]:
    latencies = [record["elapsed_ms"] for record in records]
    retries = [record["retry_count"] for record in records]
    successful = sum(bool(record["sql_execution_succeeded"]) for record in records)
    correct = sum(bool(record["result_set_correct"]) for record in records)
    failures = Counter(
        record["failure_category"] for record in records if record["failure_category"] is not None
    )
    input_tokens = [
        record["input_tokens"] for record in records if record["input_tokens"] is not None
    ]
    output_tokens = [
        record["output_tokens"] for record in records if record["output_tokens"] is not None
    ]
    costs = [record["estimated_cost"] for record in records if record["estimated_cost"] is not None]
    pipeline_latencies = [
        record["pipeline_latency_ms"]
        for record in records
        if record.get("pipeline_latency_ms") is not None
    ]
    evaluation_latencies = [
        record["evaluation_overhead_ms"]
        for record in records
        if record.get("evaluation_overhead_ms") is not None
    ]

    def mean(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    stage_stats: dict[str, Any] = {}
    for record in records:
        for span in record.get("spans", []):
            stage_stats.setdefault(span["stage"], []).append(span)

    def summarize_spans(spans: list[dict[str, Any]]) -> dict[str, Any]:
        durations = [span["duration_ms"] for span in spans]
        return {
            "observations": len(durations),
            "mean_ms": mean(durations),
            "median_ms": percentile(durations, 50),
            "p90_ms": percentile(durations, 90),
            "p95_ms": percentile(durations, 95),
            "min_ms": min(durations) if durations else None,
            "max_ms": max(durations) if durations else None,
            "failures": sum(span["status"] == "failure" for span in spans),
            "timeouts": sum(span["status"] == "timeout" for span in spans),
        }

    return {
        "dataset": dataset,
        "configuration": config,
        "total_questions": len(records),
        "result_set_accuracy": correct / len(records) if records else 0,
        "sql_execution_success_rate": successful / len(records) if records else 0,
        "latency_ms": {
            "mean": mean(latencies),
            "median": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
        },
        "pipeline_latency_ms": {
            "mean": mean(pipeline_latencies),
            "median": percentile(pipeline_latencies, 50),
            "p90": percentile(pipeline_latencies, 90),
            "p95": percentile(pipeline_latencies, 95),
        },
        "evaluation_overhead_ms": {
            "mean": mean(evaluation_latencies),
            "median": percentile(evaluation_latencies, 50),
            "p90": percentile(evaluation_latencies, 90),
            "p95": percentile(evaluation_latencies, 95),
        },
        "mean_retries": mean(retries),
        "failure_breakdown": dict(sorted(failures.items())),
        "tokens": {
            "input": sum(input_tokens) if input_tokens else None,
            "output": sum(output_tokens) if output_tokens else None,
            "mean_input": sum(input_tokens) / len(input_tokens) if input_tokens else None,
            "mean_output": sum(output_tokens) / len(output_tokens) if output_tokens else None,
            "questions_with_usage": sum(
                record["input_tokens"] is not None or record["output_tokens"] is not None
                for record in records
            ),
        },
        "estimated_cost": sum(costs) if costs else None,
        "stage_statistics": {
            stage: summarize_spans(spans) for stage, spans in sorted(stage_stats.items())
        },
        "model_calls_per_question": mean(
            [len(record.get("model_calls", [])) for record in records]
        ),
    }


def _failed_evaluation(
    question_id: str,
    gold_sql: str,
    error: BaseException,
    *,
    generated_sql: str | None = None,
    retry_count: int = 0,
) -> dict[str, Any]:
    detail = redact_error(str(error)) or type(error).__name__
    return {
        "question_id": question_id,
        "gold_sql": gold_sql,
        "generated_sql": generated_sql,
        "result_set_correct": False,
        "sql_execution_succeeded": False,
        "failure_category": "pipeline_error",
        "error": detail,
        "generated_execution_time_ms": None,
        "retry_count": retry_count,
    }


def run_configuration(
    questions: list[dict[str, Any]],
    dataset: str,
    config: dict[str, Any],
    *,
    base_settings: Settings | None = None,
    pipeline: Callable[..., QueryResult] = run_pipeline,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    settings = _config_settings(base_settings or get_settings(), config)
    records: list[dict[str, Any]] = []
    for index, question in enumerate(questions, 1):
        question_id = str(question.get("question_id", f"{dataset}:{index}"))
        started = time.perf_counter()
        generated_sql: str | None = None
        telemetry = PipelineTelemetry()
        try:
            pipeline_kwargs = {"settings": settings}
            if "telemetry" in inspect.signature(pipeline).parameters:
                pipeline_kwargs["telemetry"] = telemetry
            result = pipeline(question["question"], question["db"], **pipeline_kwargs)
            generated_sql = result.sql
            if result.status == "failed":
                record = _failed_evaluation(
                    question_id,
                    question["gold_sql"],
                    RuntimeError(result.reason or "pipeline failed"),
                    generated_sql=result.sql,
                    retry_count=max(result.attempts - 1, 0),
                )
                record["failure_category"] = "pipeline_failure"
            else:
                evaluation = evaluate_sql_pair(
                    question_id, question["gold_sql"], generated_sql, _readonly_connection
                )
                record = {
                    "question_id": question_id,
                    "dataset": dataset,
                    "generated_sql": generated_sql,
                    "gold_sql": question["gold_sql"],
                    "result_set_correct": evaluation.result_match,
                    "sql_execution_succeeded": evaluation.generated_executed,
                    "gold_sql_execution_succeeded": evaluation.gold_executed,
                    "failure_category": (
                        evaluation.generated_error_category
                        if not evaluation.generated_executed
                        else (
                            f"gold_{evaluation.gold_error_category}"
                            if not evaluation.gold_executed
                            else ("result_mismatch" if not evaluation.result_match else None)
                        )
                    ),
                    "error": redact_error(evaluation.generated_error or evaluation.gold_error),
                    "generated_error": redact_error(evaluation.generated_error),
                    "gold_error": redact_error(evaluation.gold_error),
                    "generated_execution_time_ms": evaluation.generated_execution_time_ms,
                    "evaluation_time_ms": getattr(evaluation, "evaluation_time_ms", None),
                    "comparison_time_ms": getattr(evaluation, "comparison_time_ms", None),
                    "pipeline_latency_ms": telemetry_span_duration(telemetry, "pipeline"),
                    "evaluation_overhead_ms": getattr(evaluation, "evaluation_time_ms", None),
                    "spans": telemetry.spans,
                    "model_calls": telemetry.model_calls,
                    "retrieval": telemetry.retrieval or None,
                    "retry_count": max(result.attempts - 1, 0),
                    "input_tokens": None,
                    "output_tokens": None,
                    "estimated_cost": None,
                }
            record = {
                **record,
                "run_id": run_id,
                "configuration": config,
                "difficulty": question.get("difficulty"),
                "comparison": question.get("comparison", "unordered"),
                "retrieval_time_ms": None,
                "generation_time_ms": None,
                "validation_time_ms": None,
                "execution_time_ms": record["generated_execution_time_ms"],
            }
        except Exception as error:
            record = _failed_evaluation(question_id, question["gold_sql"], error)
            record.update(
                dataset=dataset,
                run_id=run_id,
                configuration=config,
                difficulty=question.get("difficulty"),
                comparison=question.get("comparison", "unordered"),
                input_tokens=None,
                output_tokens=None,
                estimated_cost=None,
                retrieval_time_ms=None,
                generation_time_ms=None,
                validation_time_ms=None,
                execution_time_ms=None,
                evaluation_time_ms=None,
                comparison_time_ms=None,
                pipeline_latency_ms=telemetry_span_duration(telemetry, "pipeline"),
                evaluation_overhead_ms=None,
                spans=telemetry.spans,
                model_calls=telemetry.model_calls,
                retrieval=telemetry.retrieval or None,
            )
        record["elapsed_ms"] = (time.perf_counter() - started) * 1000
        records.append(record)
    return records


def telemetry_span_duration(telemetry: PipelineTelemetry, stage: str) -> float | None:
    durations = [span["duration_ms"] for span in telemetry.spans if span["stage"] == stage]
    return durations[-1] if durations else None


def _readonly_connection() -> Any:
    from app.db import readonly_connection

    return readonly_connection()


def run_benchmark(
    questions_path: str | Path | None,
    output_dir: str | Path,
    *,
    dataset: str | None = None,
    configs: list[dict[str, Any]] | None = None,
    limit: int | None = None,
    seed: int | None = None,
    base_settings: Settings | None = None,
    gold_as_generated: bool = False,
    initialize: bool = False,
) -> dict[str, Any]:
    dataset_definition = get_dataset(dataset or "chinook")
    question_path = Path(questions_path) if questions_path else None
    questions = (
        json.loads(question_path.read_text(encoding="utf-8"))
        if question_path
        else dataset_questions(dataset_definition.dataset_id)
    )
    if limit is not None:
        questions = questions[:limit]
    if not questions:
        raise ValueError("No benchmark questions selected")
    dataset_name = dataset or dataset_definition.dataset_id
    if initialize:
        initialize_dataset(dataset_definition, base_settings or get_settings())
    selected_configs = configs or [
        {"name": "default", "disable_self_correction": False, "enable_fk_expansion": False}
    ]
    run_id = uuid.uuid4().hex
    timestamp = datetime.now(UTC).isoformat()
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    summaries: dict[str, Any] = {}
    for config in selected_configs:
        records = run_configuration(
            questions,
            dataset_name,
            config,
            base_settings=base_settings,
            run_id=run_id,
            pipeline=(
                lambda question, db, **kwargs: (
                    QueryResult(
                        status="success",
                        sql=next(
                            item["gold_sql"] for item in questions if item["question"] == question
                        ),
                        attempts=1,
                    )
                    if gold_as_generated
                    else run_pipeline(question, db, **kwargs)
                )
            ),
        )
        filename = output / f"{run_id}_{config['name']}.jsonl"
        filename.write_text(
            "".join(json.dumps(record, sort_keys=True, default=str) + "\n" for record in records),
            encoding="utf-8",
        )
        summaries[config["name"]] = aggregate_records(records, dataset_name, config)

    metadata = {
        "run_id": run_id,
        "timestamp": timestamp,
        "git_commit": _git_commit(),
        "dataset": dataset_name,
        "dataset_path": str(question_path) if question_path else f"registry:{dataset_name}",
        "dataset_sha256": (
            dataset_hash(question_path)
            if question_path
            else hashlib.sha256(
                json.dumps(
                    {
                        "version": dataset_definition.version,
                        "schema_sql": dataset_definition.schema_sql,
                        "questions": questions,
                    },
                    sort_keys=True,
                    default=str,
                ).encode()
            ).hexdigest()
        ),
        "dataset_version": dataset_definition.version,
        "dialect": dataset_definition.dialect,
        "model": (base_settings or get_settings()).model_name,
        "random_seed": seed,
        "question_count": len(questions),
        "configurations": summaries,
    }
    summary_path = output / f"{run_id}_summary.json"
    summary_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path = output / f"{run_id}_summary.md"
    markdown_path.write_text(render_markdown_summary(metadata), encoding="utf-8")
    metadata["artifacts"] = {
        "summary_json": str(summary_path),
        "summary_markdown": str(markdown_path),
        "jsonl": {name: str(output / f"{run_id}_{name}.jsonl") for name in summaries},
    }
    summary_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return metadata


def initialize_dataset(dataset: Any, settings: Settings) -> None:
    """Initialize only an explicitly selected local fixture database."""
    parsed = urlparse(settings.ingest_database_url)
    if parsed.hostname not in {"localhost", "127.0.0.1", "db"}:
        raise RuntimeError(
            "Refusing benchmark fixture initialization outside a local database host"
        )
    with psycopg.connect(settings.ingest_database_url) as connection:
        dataset.seed(connection, seed=0)


def render_markdown_summary(summary: dict[str, Any]) -> str:
    lines = [
        f"# SchemaDrill benchmark `{summary['run_id']}`",
        "",
        f"- Dataset: `{summary['dataset']}` (`{summary['dataset_sha256']}`)",
        f"- Questions: {summary['question_count']}",
        f"- Git commit: `{summary['git_commit'] or 'unknown'}`",
        "",
        "| Configuration | Result accuracy | SQL execution | Mean ms | Median ms | "
        "p95 ms | Mean retries |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, metrics in summary["configurations"].items():
        latency = metrics["latency_ms"]
        lines.append(
            f"| {name} | {metrics['result_set_accuracy']:.3f} | "
            f"{metrics['sql_execution_success_rate']:.3f} | "
            f"{latency['mean']:.3f} | {latency['median']:.3f} | {latency['p95']:.3f} | "
            f"{metrics['mean_retries']:.3f} |"
        )
    return "\n".join(lines) + "\n"

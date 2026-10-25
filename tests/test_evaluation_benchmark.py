import json

from app.instrumentation import SpanRecorder
from app.models import QueryResult
from evaluation.benchmark import aggregate_records, percentile, run_configuration


def _record(**values):
    result = {
        "elapsed_ms": 10.0,
        "retry_count": 0,
        "sql_execution_succeeded": True,
        "result_set_correct": True,
        "failure_category": None,
        "input_tokens": None,
        "output_tokens": None,
        "estimated_cost": None,
    }
    result.update(values)
    return result


def test_percentile_interpolates_and_handles_empty():
    assert percentile([10, 20, 30, 40], 50) == 25
    assert percentile([], 95) is None


def test_span_recorder_records_nested_success_and_failure():
    clock = iter([0, 1_000_000, 2_000_000, 5_000_000, 6_000_000, 9_000_000])
    recorder = SpanRecorder(clock_ns=lambda: next(clock))
    with recorder.span("outer") as outer_id:
        with recorder.span("inner", parent_id=outer_id):
            pass
    try:
        with recorder.span("failed"):
            raise TimeoutError("timed out")
    except TimeoutError:
        pass

    assert recorder.spans[0]["stage"] == "inner"
    assert recorder.spans[0]["parent_span_id"] == recorder.spans[1]["span_id"]
    assert recorder.spans[0]["duration_ms"] == 1
    assert recorder.spans[1]["duration_ms"] == 5
    assert recorder.spans[2]["status"] == "timeout"


def test_aggregate_uses_all_records_and_includes_failures():
    summary = aggregate_records(
        [
            _record(elapsed_ms=10, retry_count=1, input_tokens=4, output_tokens=2),
            _record(
                elapsed_ms=30,
                sql_execution_succeeded=False,
                result_set_correct=False,
                failure_category="syntax error",
            ),
        ],
        "chinook",
        {"name": "test"},
    )
    assert summary["total_questions"] == 2
    assert summary["result_set_accuracy"] == 0.5
    assert summary["sql_execution_success_rate"] == 0.5
    assert summary["latency_ms"]["p95"] == 29
    assert summary["mean_retries"] == 0.5
    assert summary["failure_breakdown"] == {"syntax error": 1}
    assert summary["tokens"] == {
        "input": 4,
        "output": 2,
        "mean_input": 4.0,
        "mean_output": 2.0,
        "questions_with_usage": 1,
    }


def test_failed_pipeline_is_serialized_and_counted(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("password=secret-value")

    monkeypatch.setattr("evaluation.benchmark.evaluate_sql_pair", lambda *args: None)
    records = run_configuration(
        [{"question_id": "q1", "db": "chinook", "question": "bad", "gold_sql": "SELECT 1"}],
        "chinook",
        {"name": "test", "disable_self_correction": False, "enable_fk_expansion": False},
        pipeline=fail,
    )
    assert records[0]["failure_category"] == "pipeline_error"
    assert records[0]["sql_execution_succeeded"] is False
    assert "secret-value" not in records[0]["error"]
    json.dumps(records[0])


def test_incomplete_usage_metadata_is_not_invented(monkeypatch):
    monkeypatch.setattr(
        "evaluation.benchmark.evaluate_sql_pair",
        lambda *args: type(
            "Evaluation",
            (),
            {
                "result_match": True,
                "gold_executed": True,
                "gold_error_category": None,
                "gold_error": None,
                "generated_executed": True,
                "generated_error_category": None,
                "generated_error": None,
                "generated_execution_time_ms": 2.0,
            },
        )(),
    )
    records = run_configuration(
        [{"question_id": "q1", "db": "chinook", "question": "ok", "gold_sql": "SELECT 1"}],
        "chinook",
        {"name": "test", "disable_self_correction": False, "enable_fk_expansion": False},
        pipeline=lambda *args, **kwargs: QueryResult(status="success", sql="SELECT 1", attempts=2),
    )
    assert records[0]["input_tokens"] is None
    assert records[0]["output_tokens"] is None
    assert records[0]["estimated_cost"] is None
    assert records[0]["retry_count"] == 1

from eval.run_benchmark_matrix import RUNS, build_report


def _summary(result_matches, examples, mean_elapsed_ms):
    return {
        "total_examples": examples,
        "result_set_accuracy": result_matches / examples,
        "result_mismatches": examples - result_matches,
        "mean_elapsed_ms": mean_elapsed_ms,
    }


def test_build_report_calculates_percent_points_and_latency_deltas():
    summaries = {
        "no_retry_no_fk": _summary(5, 10, 100.125),
        "retry_no_fk": _summary(7, 10, 150.125),
        "no_retry_fk": _summary(6, 10, 110.125),
        "retry_fk": _summary(8, 10, 175.125),
    }

    report = build_report(summaries, "chinook")

    assert report["examples_per_run"] == 10
    assert report["metrics"]["retry_fk"]["result_matches"] == 8
    assert (
        report["comparisons"]["self_correction_without_fk"]["accuracy_delta_percentage_points"]
        == 20
    )
    assert report["comparisons"]["self_correction_without_fk"]["mean_elapsed_delta_seconds"] == 0.05
    assert (
        report["comparisons"]["fk_expansion_with_retries"]["accuracy_delta_percentage_points"] == 10
    )
    assert len(RUNS) == 4


def test_build_report_rejects_different_question_counts():
    summaries = {
        name: _summary(1, 2 if index < 3 else 3, 1.0) for index, (name, _, _) in enumerate(RUNS)
    }

    try:
        build_report(summaries, "chinook")
    except ValueError as error:
        assert "different numbers" in str(error)
    else:
        raise AssertionError("Expected differing benchmark sample sizes to be rejected")

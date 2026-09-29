from app.models import QueryResult
from eval.models import SQLEvalResult
from eval.run_benchmark import load_questions, run
from evaluation.runner import run_evaluator_smoke


def test_benchmark_reports_sql_accuracy_without_result_comparison(monkeypatch, tmp_path):
    questions = tmp_path / "questions.json"
    questions.write_text(
        '[{"db": "chinook", "question": "List artists.", '
        '"gold_sql": "SELECT name FROM chinook.artist;"}]',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "eval.run_benchmark.run_pipeline",
        lambda question, db, pre_execute_check: (
            pre_execute_check("SELECT name FROM chinook.artist")
            or QueryResult(
                status="success",
                sql="SELECT name FROM chinook.artist",
                attempts=1,
                table_markdown="different output",
            )
        ),
    )
    monkeypatch.setattr(
        "eval.run_benchmark.evaluate_sql_pair",
        lambda question_id, gold_sql, generated_sql, connection_factory: SQLEvalResult(
            question_id=question_id,
            gold_sql=gold_sql,
            generated_sql=generated_sql,
            gold_executed=True,
            generated_executed=True,
            execution_match=True,
            ast_match=True,
        ),
    )

    summary = run(str(questions))

    assert summary["execution_accuracy"] == 1.0
    assert summary["result_set_accuracy"] == 1.0
    assert summary["ast_match_rate"] == 1.0


def test_chinook_dataset_contains_five_cases():
    questions = load_questions("evaluation/chinook/cases.json", "chinook", None)

    assert len(questions) == 5
    assert [question["question_id"] for question in questions] == [
        "chinook-001",
        "chinook-002",
        "chinook-003",
        "chinook-004",
        "chinook-005",
    ]


def test_evaluator_smoke_uses_gold_sql_without_generation(monkeypatch, tmp_path):
    cases = tmp_path / "cases.json"
    cases.write_text('[{"question_id": "q1", "gold_sql": "SELECT 1"}]', encoding="utf-8")
    monkeypatch.setattr(
        "evaluation.runner.evaluate_sql_pair",
        lambda question_id, gold_sql, generated_sql, connection_factory: SQLEvalResult(
            question_id=question_id,
            gold_sql=gold_sql,
            generated_sql=generated_sql,
            gold_executed=True,
            generated_executed=True,
            execution_match=True,
            ast_match=True,
        ),
    )

    summary = run_evaluator_smoke(str(cases), "test")

    assert summary["execution_accuracy"] == 1.0
    assert summary["ast_match_rate"] == 1.0

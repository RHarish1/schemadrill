from app.models import QueryResult
from eval.models import SQLEvalResult
from eval.run_benchmark import run


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
    assert summary["execution_accuracy"] == 1.0
    assert summary["ast_match_rate"] == 1.0

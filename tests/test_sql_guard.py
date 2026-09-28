import pytest

from app.config import Settings
from app.models import DDLBlock
from app.pipeline import run_pipeline
from app.sql_guard import (
    AUTHORIZATION_DENIED_MESSAGE,
    SECURITY_REJECTED_MESSAGE,
    default_guard_config,
    guard_sql,
)

ALLOWED = {"public.customers", "public.orders"}


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO public.customers VALUES (1, 'A')",
        "UPDATE public.customers SET name = 'A'",
        "DELETE FROM public.customers",
        "DROP TABLE public.customers",
        "ALTER TABLE public.customers ADD COLUMN x integer",
        "SELECT * FROM public.customers; DROP TABLE public.customers",
    ],
)
def test_non_select_statements_are_rejected(sql):
    result = guard_sql(sql, ALLOWED)

    assert not result.allowed
    assert result.user_facing_message == SECURITY_REJECTED_MESSAGE


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM public.secret_data",
        "SELECT c.name FROM public.customers c JOIN public.secret_data s ON s.customer_id = c.id",
        "SELECT * FROM public.customers WHERE id IN (SELECT customer_id FROM public.secret_data)",
        "SELECT * FROM public.customers c WHERE EXISTS ("
        "SELECT 1 FROM public.secret_data s WHERE s.customer_id = c.id)",
        "WITH leaked AS (SELECT * FROM public.secret_data) SELECT * FROM public.customers",
        "WITH outer_cte AS (WITH inner_cte AS ("
        "SELECT * FROM public.secret_data) SELECT * FROM inner_cte) "
        "SELECT * FROM outer_cte",
    ],
)
def test_unauthorized_tables_are_rejected_at_any_ast_depth(sql):
    result = guard_sql(sql, ALLOWED)

    assert not result.allowed
    assert "public.secret_data" in result.referenced_tables
    assert result.user_facing_message == AUTHORIZATION_DENIED_MESSAGE
    assert "public.secret_data" not in result.user_facing_message


@pytest.mark.parametrize(
    "sql, function_name",
    [
        ("SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
        ("SELECT pg_read_binary_file('/etc/passwd')", "pg_read_binary_file"),
        ("SELECT pg_ls_dir('/tmp')", "pg_ls_dir"),
        ("SELECT lo_import('/tmp/file')", "lo_import"),
        ("SELECT lo_export(1, '/tmp/file')", "lo_export"),
        ("SELECT dblink_exec('host=internal-db', 'SELECT 1')", "dblink_exec"),
        (
            "SELECT * FROM dblink('host=internal-db dbname=secret', 'SELECT 1') AS t(id integer)",
            "dblink",
        ),
    ],
)
def test_blocked_postgres_capabilities_are_rejected(sql, function_name):
    result = guard_sql(sql, ALLOWED)

    assert not result.allowed
    assert result.blocked_functions == [function_name]
    assert result.user_facing_message == SECURITY_REJECTED_MESSAGE


def test_legitimate_queries_and_cte_are_allowed():
    simple = guard_sql("SELECT name FROM public.customers", ALLOWED)
    join = guard_sql(
        "SELECT c.name, COUNT(o.id) FROM public.customers c "
        "JOIN public.orders o ON o.customer_id = c.id GROUP BY c.name",
        ALLOWED,
    )
    cte = guard_sql("WITH recent AS (SELECT * FROM public.orders) SELECT * FROM recent", ALLOWED)

    assert simple.allowed and simple.limit_injected
    assert join.allowed
    assert cte.allowed
    assert cte.referenced_tables == ["public.orders"]


def test_blocked_function_string_literal_is_not_a_function_call():
    result = guard_sql("SELECT 'pg_read_file'", set())

    assert result.allowed
    assert result.blocked_functions == []


def test_limit_policy_is_ast_based_and_explicit():
    injected = guard_sql("SELECT * FROM public.customers", ALLOWED, default_guard_config(100))
    preserved = guard_sql(
        "SELECT * FROM public.customers LIMIT 20", ALLOWED, default_guard_config(100)
    )
    excessive = guard_sql(
        "SELECT * FROM public.customers LIMIT 101", ALLOWED, default_guard_config(100)
    )
    fetch = guard_sql(
        "SELECT * FROM public.customers FETCH FIRST 20 ROWS ONLY",
        ALLOWED,
        default_guard_config(100),
    )

    assert injected.allowed and injected.sql.endswith("LIMIT 100")
    assert injected.limit_injected
    assert preserved.allowed and not preserved.limit_injected and "LIMIT 20" in preserved.sql
    assert not excessive.allowed
    assert "maximum of 100" in excessive.rejection_reason
    assert fetch.allowed and not fetch.limit_injected


def test_guard_is_deterministic():
    sql = "WITH recent AS (SELECT * FROM public.orders) SELECT * FROM recent"
    results = [guard_sql(sql, ALLOWED, default_guard_config(50)).model_dump() for _ in range(10)]

    assert all(result == results[0] for result in results)


def test_pipeline_hides_unauthorized_table_details(monkeypatch):
    settings = Settings(max_retries=1, max_result_rows=100)
    monkeypatch.setattr(
        "app.pipeline.readonly_connection",
        lambda: pytest.fail("guard must reject before DB"),
    )

    result = run_pipeline(
        "question",
        "chinook",
        settings=settings,
        retrieve=lambda question, db, top_k: [
            DDLBlock(
                schema_name="public",
                table_name="customers",
                ddl_text="CREATE TABLE public.customers;",
            )
        ],
        generate_sql=lambda messages: {"sql": "SELECT * FROM public.salary"},
    )

    assert result.status == "failed"
    assert result.reason == AUTHORIZATION_DENIED_MESSAGE
    assert "salary" not in result.reason

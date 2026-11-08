import pytest

from evaluation.datasets import DATASETS, dataset_catalog, dataset_questions, get_dataset


def test_registry_contains_all_datasets_and_stable_questions():
    assert set(DATASETS) == {"chinook", "ecommerce", "organization", "complex_synthetic"}
    for dataset_id in DATASETS:
        questions = dataset_questions(dataset_id)
        assert questions
        assert len({question["question_id"] for question in questions}) == len(questions)
        assert all(question["db"] == dataset_id for question in questions)
        assert all(question["gold_sql"].strip() for question in questions)
        assert {question.get("difficulty") for question in questions} <= {
            "easy",
            "medium",
            "hard",
            None,
        }


def test_registry_rejects_unknown_dataset():
    with pytest.raises(ValueError, match="Unknown benchmark dataset"):
        get_dataset("not-a-dataset")


def test_dataset_catalog_exposes_only_curated_datasets():
    catalog = dataset_catalog()
    assert [dataset["id"] for dataset in catalog] == list(DATASETS)
    assert all(set(dataset) == {"id", "label", "description", "examples"} for dataset in catalog)
    assert all(dataset["examples"] for dataset in catalog)
    assert all(dataset["id"] != "postgres" for dataset in catalog)


def test_synthetic_questions_cover_expected_relationship_traps():
    questions = dataset_questions("complex_synthetic")
    sql = "\n".join(question["gold_sql"] for question in questions)
    assert "tenant_id" in sql
    assert "IS NULL" in sql
    assert "DISTINCT" in sql


def test_provisioning_database_url_uses_ingest_for_readonly_role():
    from app.config import Settings
    from app.tenant import _provisioning_database_url

    local_settings = Settings(
        database_url="postgresql://schemadrill_readonly:schemadrill@db:5432/schemadrill",
        ingest_database_url="postgresql://schemadrill_admin:schemadrill@db:5432/schemadrill",
    )
    assert _provisioning_database_url(local_settings) == local_settings.ingest_database_url

    managed_settings = Settings(
        database_url="postgresql://neondb_owner:secret@ep-demo.neon.tech/neondb",
        ingest_database_url="postgresql://schemadrill_admin:schemadrill@localhost:5432/schemadrill",
    )
    assert _provisioning_database_url(managed_settings) == managed_settings.database_url


def test_provision_user_skips_ddl_when_connected_as_readonly_role(monkeypatch):
    from app.config import Settings
    from app.tenant import _ensure_tenant_schema, provision_user

    _ensure_tenant_schema.cache_clear()
    executed_sql: list[str] = []
    executemany_calls: list[tuple[str, list[tuple[str, str, str, str]]]] = []

    class FakeCursor:
        def fetchone(self):
            return ("user-123",)

        def executemany(self, query, params):
            executemany_calls.append((query, list(params)))

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            executed_sql.append(query)
            return FakeCursor()

        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

    monkeypatch.setattr("app.tenant.psycopg.connect", lambda url: FakeConnection())
    readonly_remote_settings = Settings(
        database_url="postgresql://schemadrill_readonly:secret@ep-demo.neon.tech/neondb",
        ingest_database_url="postgresql://schemadrill_admin:schemadrill@localhost:5432/schemadrill",
    )

    assert provision_user("user-123", readonly_remote_settings) is True
    assert len(executed_sql) == 1
    assert "INSERT INTO public.users" in executed_sql[0]
    assert len(executemany_calls) == 1
    assert len(executemany_calls[0][1]) == 4


def test_provision_user_captures_identity_and_backfills_existing_user(monkeypatch):
    from app.config import Settings
    from app.models import AuthenticatedUser
    from app.tenant import _ensure_tenant_schema, get_user, provision_user

    _ensure_tenant_schema.cache_clear()
    executed_calls: list[tuple[str, tuple | None]] = []
    is_new = True
    stored_row = ("user-456", "dana@example.com", "Dana Scully")

    class FakeCursor:
        def __init__(self, row):
            self._row = row

        def fetchone(self):
            return self._row

        def executemany(self, query, params):
            executed_calls.append((query, tuple(params)))

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            executed_calls.append((query, params))
            if "INSERT INTO public.users" in query:
                return FakeCursor(("user-456",) if is_new else None)
            if "SELECT user_id, email, display_name" in query:
                return FakeCursor(stored_row)
            return FakeCursor(None)

        def cursor(self):
            return FakeCursor(None)

        def commit(self):
            pass

    monkeypatch.setattr("app.tenant.psycopg.connect", lambda url: FakeConnection())
    settings = Settings(
        database_url="postgresql://schemadrill_readonly:secret@ep-demo.neon.tech/neondb",
        ingest_database_url="postgresql://schemadrill_admin:schemadrill@localhost:5432/schemadrill",
    )

    assert (
        provision_user(
            "user-456",
            settings,
            email=" dana@example.com ",
            display_name=" Dana Scully ",
        )
        is True
    )
    insert_sql, insert_params = executed_calls[0]
    assert "INSERT INTO public.users (user_id, email, display_name)" in insert_sql
    assert insert_params == ("user-456", "dana@example.com", "Dana Scully")

    executed_calls.clear()
    is_new = False
    assert (
        provision_user(
            "user-456",
            settings,
            email="dana@example.com",
            display_name="Dana Scully",
        )
        is False
    )
    assert len(executed_calls) == 2
    assert "INSERT INTO public.users" in executed_calls[0][0]
    update_sql, update_params = executed_calls[1]
    assert "UPDATE public.users" in update_sql
    assert "IS NOT NULL" not in update_sql
    assert update_sql.count("%s") == update_sql.count("%s::text") == 5
    assert update_params == (
        "dana@example.com",
        "Dana Scully",
        "user-456",
        "dana@example.com",
        "Dana Scully",
    )

    executed_calls.clear()
    assert (
        provision_user(
            "user-456",
            settings,
            email="dana@example.com",
            display_name=None,
        )
        is False
    )
    assert executed_calls[1][1] == (
        "dana@example.com",
        None,
        "user-456",
        "dana@example.com",
        None,
    )

    executed_calls.clear()
    assert provision_user("user-456", settings, email=None, display_name="   ") is False
    assert len(executed_calls) == 1

    assert get_user("user-456", settings) == AuthenticatedUser(
        user_id="user-456",
        email="dana@example.com",
        display_name="Dana Scully",
    )

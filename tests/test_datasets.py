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

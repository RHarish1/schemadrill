from app.config import Settings


def test_disable_self_correction_reads_environment(monkeypatch):
    monkeypatch.setenv("DISABLE_SELF_CORRECTION", "true")

    settings = Settings(_env_file=None)

    assert settings.disable_self_correction is True

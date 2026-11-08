import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app.auth import _jwks_client, current_user, current_user_id
from app.config import Settings
from app.main import app
from app.models import AuthenticatedUser, QueryResult


def _make_ed25519_keypair():
    private_key = Ed25519PrivateKey.generate()
    return private_key, private_key.public_key()


def _sign_token(private_key, claims: dict[str, object]) -> str:
    return jwt.encode(claims, private_key, algorithm="EdDSA")


def _configure_auth(
    monkeypatch, public_key, *, issuer: str = "https://auth.example.neon.tech/neondb/auth"
):
    _jwks_client.cache_clear()
    settings = Settings(
        _env_file=None,
        neon_auth_jwks_url="https://auth.example.neon.tech/neondb/auth/.well-known/jwks.json",
        neon_auth_issuer=issuer,
        preload_embedding_model=False,
    )
    monkeypatch.setattr("app.auth.get_settings", lambda: settings)
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    fake_jwks = SimpleNamespace(
        get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=public_key)
    )
    monkeypatch.setattr("app.auth._jwks_client", lambda: fake_jwks)
    return settings


def _make_request(authorization: str | None = None):
    headers = {}
    if authorization is not None:
        headers["Authorization"] = authorization
    return SimpleNamespace(headers=headers)


def test_current_user_extracts_sub_email_and_name(monkeypatch):
    private_key, public_key = _make_ed25519_keypair()
    _configure_auth(monkeypatch, public_key)
    now = int(time.time())
    token = _sign_token(
        private_key,
        {
            "sub": "user-google-1",
            "email": "  alice@example.com ",
            "name": " Alice Example ",
            "iss": "https://auth.example.neon.tech",
            "iat": now,
            "exp": now + 900,
        },
    )

    request = _make_request(f"Bearer {token}")
    user = current_user(request)

    assert user == AuthenticatedUser(
        user_id="user-google-1",
        email="alice@example.com",
        display_name="Alice Example",
    )
    assert current_user_id(request) == "user-google-1"


def test_current_user_supports_nested_user_claims_and_display_name_fallback(monkeypatch):
    private_key, public_key = _make_ed25519_keypair()
    _configure_auth(monkeypatch, public_key)
    now = int(time.time())
    token = _sign_token(
        private_key,
        {
            "sub": "user-google-2",
            "user": {"email": "bob@example.com", "display_name": "Bob Builder"},
            "iss": "https://auth.example.neon.tech/neondb/auth",
            "iat": now,
            "exp": now + 900,
        },
    )

    user = current_user(_make_request(f"Bearer {token}"))
    assert user.email == "bob@example.com"
    assert user.display_name == "Bob Builder"


def test_current_user_distinguishes_missing_expired_and_invalid_tokens(monkeypatch):
    private_key, public_key = _make_ed25519_keypair()
    other_private_key, _ = _make_ed25519_keypair()
    _configure_auth(monkeypatch, public_key)
    now = int(time.time())

    with pytest.raises(Exception) as missing_exc:
        current_user(_make_request())
    assert missing_exc.value.status_code == 401
    assert missing_exc.value.detail == "Authentication required"
    assert missing_exc.value.headers == {"WWW-Authenticate": "Bearer"}

    expired_token = _sign_token(
        private_key,
        {
            "sub": "user-expired",
            "iss": "https://auth.example.neon.tech",
            "iat": now - 1200,
            "exp": now - 60,
        },
    )
    with pytest.raises(Exception) as expired_exc:
        current_user(_make_request(f"Bearer {expired_token}"))
    assert expired_exc.value.status_code == 401
    assert expired_exc.value.detail == "Authentication token expired"
    assert expired_exc.value.headers == {"WWW-Authenticate": "Bearer"}

    wrong_sig_token = _sign_token(
        other_private_key,
        {
            "sub": "user-wrong-sig",
            "iss": "https://auth.example.neon.tech",
            "iat": now,
            "exp": now + 900,
        },
    )
    with pytest.raises(Exception) as invalid_exc:
        current_user(_make_request(f"Bearer {wrong_sig_token}"))
    assert invalid_exc.value.status_code == 401
    assert invalid_exc.value.detail == "Invalid authentication token"


def test_current_user_returns_503_when_auth_not_configured(monkeypatch):
    _jwks_client.cache_clear()
    unconfigured = Settings(_env_file=None, neon_auth_jwks_url="", preload_embedding_model=False)
    monkeypatch.setattr("app.auth.get_settings", lambda: unconfigured)

    with pytest.raises(Exception) as exc_info:
        current_user(_make_request("Bearer any.jwt.token"))
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Authentication is not configured"


def test_endpoints_provision_email_and_display_name_and_expose_me(monkeypatch):
    private_key, public_key = _make_ed25519_keypair()
    _configure_auth(monkeypatch, public_key)
    now = int(time.time())
    token = _sign_token(
        private_key,
        {
            "sub": "user-777",
            "email": "carol@example.com",
            "name": "Carol Dan",
            "iss": "https://auth.example.neon.tech",
            "iat": now,
            "exp": now + 900,
        },
    )

    provision_calls: list[tuple[str, str | None, str | None]] = []

    def fake_provision_user(user_id, settings=None, *, email=None, display_name=None):
        provision_calls.append((user_id, email, display_name))
        return False

    monkeypatch.setattr("app.main.provision_user", fake_provision_user)
    monkeypatch.setattr(
        "app.main.get_user",
        lambda user_id, settings=None: AuthenticatedUser(
            user_id=user_id,
            email="carol@example.com",
            display_name="Carol Dan",
        ),
    )
    monkeypatch.setattr("app.main.user_has_dataset", lambda user_id, db, settings=None: True)
    monkeypatch.setattr(
        "app.main.run_pipeline",
        lambda question, db, settings=None: QueryResult(
            status="success",
            sql="SELECT 1 AS ok LIMIT 1",
            columns=["ok"],
            rows=[{"ok": 1}],
            attempts=1,
        ),
    )

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        me_response = client.get("/api/me", headers=headers)
        assert me_response.status_code == 200
        assert me_response.json() == {
            "user_id": "user-777",
            "email": "carol@example.com",
            "display_name": "Carol Dan",
        }

        datasets_response = client.get("/api/datasets", headers=headers)
        assert datasets_response.status_code == 200

        query_response = client.post(
            "/query",
            headers=headers,
            json={"db": "chinook", "question": "How many artists are there?"},
        )
        assert query_response.status_code == 200

    assert provision_calls == [
        ("user-777", "carol@example.com", "Carol Dan"),
        ("user-777", "carol@example.com", "Carol Dan"),
        ("user-777", "carol@example.com", "Carol Dan"),
    ]

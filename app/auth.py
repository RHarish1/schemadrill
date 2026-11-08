import json
import logging
from functools import lru_cache
from urllib.parse import urlparse

import jwt
from fastapi import HTTPException, Request

from app.config import get_settings
from app.models import AuthenticatedUser

logger = logging.getLogger(__name__)
_WWW_AUTHENTICATE_BEARER = {"WWW-Authenticate": "Bearer"}


def _allowed_issuers(issuer: str) -> list[str] | None:
    cleaned = issuer.strip().rstrip("/")
    if not cleaned:
        return None
    parsed = urlparse(cleaned)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else cleaned
    return list(dict.fromkeys([cleaned, origin]))


def _optional_claim_str(claims: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        value = claims.get(key)
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned:
                return cleaned
    return None


@lru_cache
def _jwks_client() -> jwt.PyJWKClient:
    settings = get_settings()
    if not settings.neon_auth_jwks_url:
        raise RuntimeError("NEON_AUTH_JWKS_URL is required for authenticated requests")
    return jwt.PyJWKClient(settings.neon_auth_jwks_url)


def current_user(request: Request) -> AuthenticatedUser:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers=_WWW_AUTHENTICATE_BEARER,
        )
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers=_WWW_AUTHENTICATE_BEARER,
        )
    try:
        signing_key = _jwks_client().get_signing_key_from_jwt(token)
        settings = get_settings()
        allowed_issuers = _allowed_issuers(settings.neon_auth_issuer)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["EdDSA", "RS256"],
            audience=settings.neon_auth_audience or None,
            issuer=allowed_issuers,
            options={
                "verify_aud": bool(settings.neon_auth_audience),
                "verify_iss": bool(allowed_issuers),
            },
        )
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail="Authentication is not configured") from error
    except jwt.ExpiredSignatureError as error:
        logger.info("authentication_token_expired")
        raise HTTPException(
            status_code=401,
            detail="Authentication token expired",
            headers=_WWW_AUTHENTICATE_BEARER,
        ) from error
    except (jwt.PyJWTError, json.JSONDecodeError, OSError) as error:
        logger.warning("authentication_token_rejected error_type=%s", type(error).__name__)
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication token",
            headers=_WWW_AUTHENTICATE_BEARER,
        ) from error
    user_id = claims.get("sub")
    if not isinstance(user_id, str) or not user_id.strip():
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication token",
            headers=_WWW_AUTHENTICATE_BEARER,
        )
    nested_user = claims.get("user")
    nested_claims = nested_user if isinstance(nested_user, dict) else {}
    email = _optional_claim_str(claims, "email") or _optional_claim_str(nested_claims, "email")
    display_name = _optional_claim_str(claims, "name", "display_name") or _optional_claim_str(
        nested_claims, "name", "display_name"
    )
    return AuthenticatedUser(
        user_id=user_id.strip(),
        email=email,
        display_name=display_name,
    )


def current_user_id(request: Request) -> str:
    return current_user(request).user_id

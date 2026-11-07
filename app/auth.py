import json
import logging
from functools import lru_cache
from urllib.parse import urlparse

import jwt
from fastapi import HTTPException, Request

from app.config import get_settings

logger = logging.getLogger(__name__)


def _allowed_issuers(issuer: str) -> list[str] | None:
    cleaned = issuer.strip().rstrip("/")
    if not cleaned:
        return None
    parsed = urlparse(cleaned)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else cleaned
    return list(dict.fromkeys([cleaned, origin]))


@lru_cache
def _jwks_client() -> jwt.PyJWKClient:
    settings = get_settings()
    if not settings.neon_auth_jwks_url:
        raise RuntimeError("NEON_AUTH_JWKS_URL is required for authenticated requests")
    return jwt.PyJWKClient(settings.neon_auth_jwks_url)


def current_user_id(request: Request) -> str:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
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
    except (jwt.PyJWTError, json.JSONDecodeError, OSError) as error:
        logger.warning("authentication_token_rejected error_type=%s", type(error).__name__)
        raise HTTPException(status_code=401, detail="Invalid authentication token") from error
    user_id = claims.get("sub")
    if not isinstance(user_id, str) or not user_id:
        raise HTTPException(status_code=401, detail="Invalid authentication token")
    return user_id

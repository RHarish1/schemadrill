import json
from functools import lru_cache

import jwt
from fastapi import HTTPException, Request

from app.config import get_settings


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
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["EdDSA", "RS256"],
            audience=settings.neon_auth_audience or None,
            issuer=settings.neon_auth_issuer or None,
            options={
                "verify_aud": bool(settings.neon_auth_audience),
                "verify_iss": bool(settings.neon_auth_issuer),
            },
        )
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail="Authentication is not configured") from error
    except (jwt.PyJWTError, json.JSONDecodeError, OSError) as error:
        raise HTTPException(status_code=401, detail="Invalid authentication token") from error
    user_id = claims.get("sub")
    if not isinstance(user_id, str) or not user_id:
        raise HTTPException(status_code=401, detail="Invalid authentication token")
    return user_id

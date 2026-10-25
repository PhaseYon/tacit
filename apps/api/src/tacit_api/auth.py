"""Verify Better Auth JWTs against its JWKS endpoint.

PyJWKClient caches the keys and refetches when it sees an unknown `kid`, so key
rotation on the Better Auth side just works.
"""

from dataclasses import dataclass
from functools import lru_cache

import jwt
from fastapi import HTTPException, status
from starlette.concurrency import run_in_threadpool

from .config import get_settings

ALLOWED_ALGS = ["EdDSA", "ES256", "RS256"]


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str | None
    name: str | None


@lru_cache
def get_jwk_client() -> jwt.PyJWKClient:
    return jwt.PyJWKClient(get_settings().auth_jwks_url, cache_keys=True, lifespan=600)


async def verify_token(token: str) -> Principal:
    settings = get_settings()
    try:
        # Fetching keys is blocking I/O (only when the cache is cold).
        signing_key = await run_in_threadpool(get_jwk_client().get_signing_key_from_jwt, token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=ALLOWED_ALGS,
            audience=settings.auth_jwt_audience,
            issuer=settings.auth_jwt_issuer,
            options={"require": ["exp", "sub", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    return Principal(user_id=claims["sub"], email=claims.get("email"), name=claims.get("name"))

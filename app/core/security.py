"""Supabase JWT verification.

This module is the ONLY place that knows how to authenticate a request. It
verifies a Supabase-issued JWT (signature, expiry, audience, issuer) and
exposes the result as a small `AuthUser` model via two FastAPI dependencies:

- `get_current_user`: raises 401 if no valid token is present.
- `get_optional_user`: returns `None` if no valid token is present, for
  device-first / anonymous flows that don't require a signed-in user.

Keeping verification isolated here means we can swap Supabase Auth for a
different provider later without touching the rest of the app.
"""

from functools import lru_cache

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from pydantic import BaseModel

from app.core.config import Settings, get_settings

_bearer_scheme = HTTPBearer(auto_error=False)


class AuthUser(BaseModel):
    """Minimal identity extracted from a verified Supabase JWT."""

    id: str
    email: str | None = None
    role: str | None = None


@lru_cache
def _get_jwks_client(jwks_url: str) -> PyJWKClient:
    return PyJWKClient(jwks_url)


def _decode_token(token: str, settings: Settings) -> dict[str, object]:
    # PyJWT verifies `exp` by default; `sub` presence is checked by the
    # caller (_payload_to_user) since it's not a standard required option.
    try:
        if settings.SUPABASE_JWT_JWKS_URL:
            signing_key = _get_jwks_client(settings.SUPABASE_JWT_JWKS_URL).get_signing_key_from_jwt(
                token
            )
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience=settings.SUPABASE_JWT_AUDIENCE,
                issuer=settings.supabase_issuer,
            )
        else:
            payload = jwt.decode(
                token,
                settings.SUPABASE_JWT_SECRET,
                algorithms=["HS256"],
                audience=settings.SUPABASE_JWT_AUDIENCE,
                issuer=settings.supabase_issuer,
            )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid authentication token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    return payload


def _payload_to_user(payload: dict[str, object]) -> AuthUser:
    sub = payload.get("sub")
    if not sub or not isinstance(sub, str):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing 'sub' claim",
        )
    return AuthUser(
        id=sub,
        email=payload.get("email") if isinstance(payload.get("email"), str) else None,
        role=payload.get("role") if isinstance(payload.get("role"), str) else None,
    )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> AuthUser:
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = _decode_token(credentials.credentials, settings)
    return _payload_to_user(payload)


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> AuthUser | None:
    if credentials is None or not credentials.credentials:
        return None

    try:
        payload = _decode_token(credentials.credentials, settings)
        return _payload_to_user(payload)
    except HTTPException:
        return None

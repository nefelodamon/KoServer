import time
from typing import Annotated

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings

# Simple in-memory token cache: token -> (valid: bool, expires_at: float)
_token_cache: dict[str, tuple[bool, float]] = {}
_CACHE_TTL = 60.0  # seconds

security = HTTPBearer(auto_error=False)


async def _validate_token_with_ha(token: str) -> bool:
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
            resp = await client.get(
                f"{settings.ha_url}/api/",
                headers={"Authorization": f"Bearer {token}"},
            )
            return resp.status_code == 200
    except Exception:
        return False


async def require_ha_auth(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
) -> str:
    """FastAPI dependency that validates a HA long-lived access token."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    now = time.monotonic()

    cached = _token_cache.get(token)
    if cached is not None:
        valid, expires_at = cached
        if now < expires_at:
            if not valid:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid Home Assistant token",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return token

    valid = await _validate_token_with_ha(token)
    _token_cache[token] = (valid, now + _CACHE_TTL)

    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Home Assistant token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return token


def require_api_key(request: Request) -> None:
    """Dependency for upload endpoints — validates X-Api-Key header."""
    settings = get_settings()
    key = request.headers.get("X-Api-Key", "")
    if not settings.api_key or key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )

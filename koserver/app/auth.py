import logging
import time
from typing import Annotated

import httpx
from fastapi import Cookie, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings

logger = logging.getLogger(__name__)

# In-memory token cache: token -> (valid: bool, expires_at: float)
_token_cache: dict[str, tuple[bool, float]] = {}
_CACHE_TTL = 60.0  # seconds

_COOKIE_NAME = "ko_token"

# Internal HA URLs tried in order for token validation and exchange.
_HA_INTERNAL_BASES = [
    "https://homeassistant:8123",
    "http://homeassistant:8123",
]

security = HTTPBearer(auto_error=False)


async def _validate_token_with_ha(token: str) -> bool:
    async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
        for base in _HA_INTERNAL_BASES:
            try:
                resp = await client.get(
                    f"{base}/api/",
                    headers={"Authorization": f"Bearer {token}"},
                )
                logger.info("Token validation at %s → %d", base, resp.status_code)
                if resp.status_code == 200:
                    return True
            except Exception as exc:
                logger.warning("Token validation at %s failed: %s", base, exc)
    return False


async def validate_token(token: str) -> bool:
    """Validate a token, using the in-memory cache."""
    now = time.monotonic()
    cached = _token_cache.get(token)
    if cached is not None:
        valid, expires_at = cached
        if now < expires_at:
            return valid
    valid = await _validate_token_with_ha(token)
    _token_cache[token] = (valid, now + _CACHE_TTL)
    return valid


async def exchange_code_for_token(code: str, client_id: str, redirect_uri: str) -> str | None:
    """Exchange an OAuth2 authorisation code for an HA access token."""
    async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
        for base in _HA_INTERNAL_BASES:
            try:
                resp = await client.post(
                    f"{base}/auth/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "client_id": client_id,
                        "redirect_uri": redirect_uri,
                    },
                )
                logger.info("Token exchange at %s → %d", base, resp.status_code)
                if resp.status_code == 200:
                    return resp.json().get("access_token")
            except Exception as exc:
                logger.warning("Token exchange at %s failed: %s", base, exc)
    return None


def _login_url(request: Request) -> str:
    root = request.scope.get("root_path", "").rstrip("/")
    return f"{root}/login"


async def require_ha_auth(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
    ko_token: Annotated[str | None, Cookie()] = None,
) -> str:
    """Accept a HA token from Bearer header (API clients) or cookie (browser)."""
    token = None

    if credentials is not None:
        token = credentials.credentials
    elif ko_token:
        token = ko_token

    if not token:
        if "text/html" in request.headers.get("accept", ""):
            raise HTTPException(
                status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                headers={"Location": _login_url(request)},
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not await validate_token(token):
        if "text/html" in request.headers.get("accept", ""):
            raise HTTPException(
                status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                headers={"Location": _login_url(request)},
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return token


def set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,  # 30 days
    )


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(key=_COOKIE_NAME)


def require_api_key(request: Request) -> None:
    """Dependency for upload endpoints — validates X-Api-Key header."""
    settings = get_settings()
    key = request.headers.get("X-Api-Key", "")
    if not settings.api_key or key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )

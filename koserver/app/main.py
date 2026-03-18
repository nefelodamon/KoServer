import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.auth import (
    clear_auth_cookie,
    exchange_code_for_token,
    set_auth_cookie,
)
from app.config import get_settings
from app.services.kocharacters import router as kocharacters_router
from app.services.kocharacters.storage import init_db

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
VERSION = os.getenv("KOSERVER_VERSION", "dev")


def _root(request: Request) -> str:
    return request.scope.get("root_path", "").rstrip("/")


def _base_url(request: Request) -> str:
    """Absolute base URL of this app as seen by the browser."""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "localhost")
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{scheme}://{host}"


def _ha_url(request: Request) -> str:
    """External HA URL for browser redirects.

    Uses ha_url from config if set, otherwise derives it from the incoming
    request by keeping the same hostname but switching to port 8123.
    """
    settings = get_settings()
    if settings.ha_url:
        return settings.ha_url
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "localhost")
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    hostname = host.split(":")[0]
    return f"{scheme}://{hostname}:8123"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("KoServer starting %s", VERSION)
    settings = get_settings()
    settings.portraits_dir.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    await init_db(settings.db_path)
    yield


app = FastAPI(title="KoServer", version=VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["X-Api-Key", "Authorization", "Content-Type"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(kocharacters_router.router, prefix="/services/kocharacters")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def root(request: Request):
    return RedirectResponse(url=f"{_root(request)}/services/kocharacters")


@app.get("/login")
async def login(request: Request):
    client_id = _base_url(request) + "/"
    redirect_uri = _base_url(request) + "/auth/callback"
    params = urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
    })
    return RedirectResponse(url=f"{_ha_url(request)}/auth/authorize?{params}")


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = "", error: str = ""):
    if error or not code:
        logger.warning("OAuth callback error: %s", error or "no code")
        return RedirectResponse(url=f"{_root(request)}/login")

    client_id = _base_url(request) + "/"
    redirect_uri = _base_url(request) + "/auth/callback"

    token = await exchange_code_for_token(code, client_id, redirect_uri)
    if not token:
        return RedirectResponse(url=f"{_root(request)}/login")

    response = RedirectResponse(url=f"{_root(request)}/services/kocharacters", status_code=303)
    set_auth_cookie(response, token)
    return response


@app.get("/logout")
async def logout(request: Request):
    response = RedirectResponse(url=f"{_root(request)}/login")
    clear_auth_cookie(response)
    return response

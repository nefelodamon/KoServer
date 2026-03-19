import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader

from app.auth import (
    clear_auth_cookie,
    exchange_code_for_token,
    require_ha_auth,
    set_auth_cookie,
)
from app.config import get_settings
from app.services.kocharacters import router as kocharacters_router
from app.services.kocharacters.storage import init_db as init_kocharacters_db, get_setting, set_setting
from app.services.kosync import router as kosync_router
from app.services.kosync.storage import init_db as init_kosync_db
from app.services.kostats import router as kostats_router
from app.services.kostats.storage import init_db as init_kostats_db
from app.tz import COMMON_TIMEZONES, get_current_tz, localtime_filter, set_current_tz

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
_BASE_TEMPLATES = Path(__file__).parent / "templates"
VERSION = os.getenv("KOSERVER_VERSION", "dev")
TZ_KEY = "timezone"

_main_env = Environment(loader=FileSystemLoader(str(_BASE_TEMPLATES)), autoescape=True)
_main_env.filters["localtime"] = localtime_filter
_main_templates = Jinja2Templates(env=_main_env)


def _root(request: Request) -> str:
    return request.scope.get("root_path", "").rstrip("/")


def _base_url(request: Request) -> str:
    """Absolute base URL of this app as seen by the browser."""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "localhost")
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{scheme}://{host}"


def _ha_url(request: Request) -> str:
    """External HA URL for browser redirects.

    Derived from the incoming request: same hostname, port 8123.
    Override with ha_url in add-on options only if HA is on a different host.
    """
    settings = get_settings()
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "localhost")
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    hostname = host.split(":")[0]
    derived = f"{scheme}://{hostname}:8123"
    # Only use configured ha_url if it looks like an external address
    configured = settings.ha_url
    if configured and "homeassistant" not in configured and "supervisor" not in configured:
        return configured
    return derived


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("KoServer starting %s", VERSION)
    settings = get_settings()
    settings.portraits_dir.mkdir(parents=True, exist_ok=True)
    settings.kostats_dir.mkdir(parents=True, exist_ok=True)
    settings.kosync_dir.mkdir(parents=True, exist_ok=True)
    await init_kocharacters_db(settings.kocharacters_db_path)
    await init_kosync_db(settings.kosync_db_path)
    await init_kostats_db(settings.kostats_db_path)
    # Load saved timezone
    saved_tz = get_setting(settings.kocharacters_db_path, TZ_KEY, "UTC")
    set_current_tz(saved_tz)
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
app.include_router(kosync_router.router, prefix="/services/kosync")
app.include_router(kostats_router.router, prefix="/services/kostats")


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


@app.get("/settings")
async def global_settings(
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    return _main_templates.TemplateResponse(
        "global_settings.html",
        {"request": request, "timezones": COMMON_TIMEZONES, "current_tz": get_current_tz()},
    )


@app.post("/settings/save")
async def global_settings_save(
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    form = await request.form()
    tz = str(form.get("timezone", "UTC"))
    set_current_tz(tz)
    set_setting(settings.kocharacters_db_path, TZ_KEY, get_current_tz())
    return RedirectResponse(url=f"{_root(request)}/settings", status_code=303)

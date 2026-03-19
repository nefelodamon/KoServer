import time
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader

from app.auth import require_ha_auth
from app.config import get_settings
from app.services.kosync import storage
from app.services.kosync.storage import ALLOW_REGISTRATION_KEY
from app.tz import localtime_filter

_SERVICE_TEMPLATES = Path(__file__).parent / "templates"
_BASE_TEMPLATES = Path(__file__).parent.parent.parent / "templates"

_env = Environment(
    loader=ChoiceLoader([
        FileSystemLoader(str(_SERVICE_TEMPLATES)),
        FileSystemLoader(str(_BASE_TEMPLATES)),
    ]),
    autoescape=True,
)
_env.filters["localtime"] = localtime_filter
templates = Jinja2Templates(env=_env)

router = APIRouter()


# ---------------------------------------------------------------------------
# KOReader sync API
# KOReader authenticates with x-auth-user / x-auth-key (MD5 of password).
# ---------------------------------------------------------------------------

def _require_kosync_auth(
    x_auth_user: Annotated[str | None, Header()] = None,
    x_auth_key: Annotated[str | None, Header()] = None,
) -> str:
    if not x_auth_user or not x_auth_key:
        raise HTTPException(status_code=401, detail="Missing credentials")
    settings = get_settings()
    if not storage.authenticate(settings.kosync_db_path, x_auth_user, x_auth_key):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return x_auth_user


@router.post("/users/create")
async def create_user(request: Request):
    settings = get_settings()
    if storage.get_setting(settings.kosync_db_path, ALLOW_REGISTRATION_KEY, "true") != "true":
        raise HTTPException(status_code=403, detail="Registration is disabled")

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", "")).strip()
    else:
        form = await request.form()
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", "")).strip()

    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password are required")

    if not storage.create_user(settings.kosync_db_path, username, password):
        raise HTTPException(status_code=402, detail="Username is already registered")

    return JSONResponse({"username": username}, status_code=201)


@router.get("/users/auth")
async def auth_user(username: Annotated[str, Depends(_require_kosync_auth)]):
    return JSONResponse({"username": username})


@router.put("/syncs/progress")
async def update_progress(
    request: Request,
    username: Annotated[str, Depends(_require_kosync_auth)],
):
    settings = get_settings()
    body = await request.json()
    document = body.get("document", "")
    if not document:
        raise HTTPException(status_code=400, detail="document is required")

    ts = int(body.get("timestamp") or time.time())
    storage.upsert_progress(
        settings.kosync_db_path,
        username=username,
        document=document,
        progress=body.get("progress", ""),
        percentage=float(body.get("percentage", 0)),
        device=body.get("device", ""),
        device_id=body.get("device_id", ""),
        timestamp=ts,
    )
    return JSONResponse({"document": document, "timestamp": ts})


@router.get("/syncs/progress/{document}")
async def get_progress(
    document: str,
    username: Annotated[str, Depends(_require_kosync_auth)],
):
    settings = get_settings()
    progress = storage.get_progress(settings.kosync_db_path, username, document)
    if not progress:
        raise HTTPException(status_code=404, detail="No progress found")
    return JSONResponse({
        "document": progress.document,
        "progress": progress.progress,
        "percentage": progress.percentage,
        "device": progress.device,
        "device_id": progress.device_id,
        "timestamp": progress.timestamp,
    })


# ---------------------------------------------------------------------------
# Web UI (HA auth)
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    users = storage.list_users(settings.kosync_db_path)
    all_progress = storage.list_all_progress(settings.kosync_db_path)
    return templates.TemplateResponse(
        "dashboard.html", {"request": request, "users": users, "all_progress": all_progress}
    )


@router.get("/users/{username}", response_class=HTMLResponse)
async def user_detail(
    username: str,
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    users = storage.list_users(settings.kosync_db_path)
    user = next((u for u in users if u.username == username), None)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    progress = storage.list_user_progress(settings.kosync_db_path, username)
    return templates.TemplateResponse(
        "user_detail.html", {"request": request, "user": user, "progress": progress}
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    allow_registration = storage.get_setting(settings.kosync_db_path, ALLOW_REGISTRATION_KEY, "true") == "true"
    users = storage.list_users(settings.kosync_db_path)
    return templates.TemplateResponse(
        "settings.html",
        {"request": request, ALLOW_REGISTRATION_KEY: allow_registration, "users": users},
    )


@router.post("/settings")
async def update_settings(
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    form = await request.form()
    value = "true" if form.get(ALLOW_REGISTRATION_KEY) == "on" else "false"
    storage.set_setting(settings.kosync_db_path, ALLOW_REGISTRATION_KEY, value)
    root = request.scope.get("root_path", "").rstrip("/")
    return RedirectResponse(url=f"{root}/services/kosync/settings", status_code=303)


@router.post("/settings/delete-user/{username}")
async def delete_user(
    username: str,
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    storage.delete_user(settings.kosync_db_path, username)
    root = request.scope.get("root_path", "").rstrip("/")
    return RedirectResponse(url=f"{root}/services/kosync/settings", status_code=303)

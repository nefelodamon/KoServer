import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader

from app.auth import require_ha_auth
from app.config import get_settings
from app.services.kolibrary import scheduler, storage, sync
from app.tz import localtime_filter

logger = logging.getLogger(__name__)

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
_env.globals["version"] = __import__("os").getenv("KOSERVER_VERSION", "dev")
templates = Jinja2Templates(env=_env)

router = APIRouter()

SYNC_INTERVALS = [
    ("manual", "Manual only"),
    ("hourly", "Every hour"),
    ("6h",     "Every 6 hours"),
    ("daily",  "Daily"),
    ("weekly", "Weekly"),
]


# ---------------------------------------------------------------------------
# Library grid
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def library(
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
    device_id: int = Query(default=0),
    search: str = Query(default=""),
    status: str = Query(default=""),
):
    settings = get_settings()
    devices = storage.list_devices(settings.kolibrary_db_path)
    books = storage.list_books(
        settings.kolibrary_db_path,
        device_id=device_id or None,
        search=search,
        status_filter=status,
    )
    return templates.TemplateResponse("library.html", {
        "request": request,
        "devices": devices,
        "books": books,
        "selected_device": device_id,
        "search": search,
        "status_filter": status,
    })


# ---------------------------------------------------------------------------
# Cover images
# ---------------------------------------------------------------------------

@router.get("/covers/{device_id}/{filename}")
async def serve_cover(
    device_id: int,
    filename: str,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    cover_path = settings.kolibrary_covers_dir / str(device_id) / filename
    if not cover_path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(str(cover_path))


# ---------------------------------------------------------------------------
# Sync API
# ---------------------------------------------------------------------------

@router.post("/devices/{device_id}/sync")
async def trigger_sync(
    device_id: int,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    device = storage.get_device(settings.kolibrary_db_path, device_id)
    if not device:
        raise HTTPException(status_code=404)
    import asyncio
    asyncio.create_task(sync.sync_device(
        device_id,
        settings.kolibrary_db_path,
        settings.kolibrary_covers_dir,
        settings.kolibrary_key_path,
    ))
    return JSONResponse({"status": "started"})


@router.get("/devices/{device_id}/sync-status")
async def sync_status(
    device_id: int,
    _: Annotated[str, Depends(require_ha_auth)],
):
    return JSONResponse(sync.get_sync_status(device_id))


# ---------------------------------------------------------------------------
# Settings — device management
# ---------------------------------------------------------------------------

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
    device_id: int = Query(default=0),
):
    settings = get_settings()
    devices = storage.list_devices(settings.kolibrary_db_path)
    logs: dict = {}
    for d in devices:
        logs[d.id] = storage.list_sync_logs(settings.kolibrary_db_path, d.id, limit=10)
    edit_device = storage.get_device(settings.kolibrary_db_path, device_id) if device_id else None
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "devices": devices,
        "logs": logs,
        "sync_intervals": SYNC_INTERVALS,
        "edit_device": edit_device,
        "sync_status": {d.id: sync.get_sync_status(d.id) for d in devices},
    })


@router.post("/settings/create-device")
async def create_device(
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    form = await request.form()
    name = str(form.get("name", "")).strip()
    friendly_name = str(form.get("friendly_name", "")).strip()
    host = str(form.get("host", "")).strip()
    port = int(form.get("port", 22) or 22)
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", "")).strip()
    books_path = str(form.get("books_path", "/mnt/us/documents")).strip()
    sync_interval = str(form.get("sync_interval", "daily")).strip()
    root = request.scope.get("root_path", "").rstrip("/")
    if name and host and username and password:
        enc = storage.encrypt_password(settings.kolibrary_key_path, password)
        storage.create_device(settings.kolibrary_db_path, name, friendly_name, host, port,
                              username, enc, books_path, sync_interval)
    return RedirectResponse(url=f"{root}/services/kolibrary/settings", status_code=303)


@router.post("/settings/update-device/{device_id}")
async def update_device(
    device_id: int,
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    form = await request.form()
    name = str(form.get("name", "")).strip()
    friendly_name = str(form.get("friendly_name", "")).strip()
    host = str(form.get("host", "")).strip()
    port = int(form.get("port", 22) or 22)
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", "")).strip()
    books_path = str(form.get("books_path", "/mnt/us/documents")).strip()
    sync_interval = str(form.get("sync_interval", "daily")).strip()
    root = request.scope.get("root_path", "").rstrip("/")
    enc = storage.encrypt_password(settings.kolibrary_key_path, password) if password else None
    storage.update_device(settings.kolibrary_db_path, device_id, name, friendly_name, host,
                          port, username, books_path, sync_interval, enc)
    return RedirectResponse(url=f"{root}/services/kolibrary/settings", status_code=303)


@router.post("/settings/delete-device/{device_id}")
async def delete_device(
    device_id: int,
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    storage.delete_device(settings.kolibrary_db_path, device_id)
    root = request.scope.get("root_path", "").rstrip("/")
    return RedirectResponse(url=f"{root}/services/kolibrary/settings", status_code=303)

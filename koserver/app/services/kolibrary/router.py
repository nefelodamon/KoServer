import asyncio
import logging
import socket
from pathlib import Path
from typing import Annotated

import asyncssh

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader

from app.auth import require_ha_auth
from app.config import get_settings
from app.services.kolibrary import scheduler, storage, sync
from app.tz import localtime_filter, mins_hm

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
_env.filters["mins_hm"] = mins_hm
_env.globals["version"] = __import__("os").getenv("KOSERVER_VERSION", "dev")
templates = Jinja2Templates(env=_env)

router = APIRouter()


def _deduplicate_by_md5(books):
    """Merge books with the same MD5 into one entry (highest progress wins)."""
    from app.services.kolibrary.models import KoBook
    md5_groups: dict[str, list] = {}
    no_md5 = []
    for b in books:
        if b.md5:
            md5_groups.setdefault(b.md5, []).append(b)
        else:
            no_md5.append(b)

    result = []
    for group in md5_groups.values():
        def _key(b):
            return 1.0 if b.status == "complete" else b.progress_pct
        best = max(group, key=_key)
        if not best.cover_file:
            for b in group:
                if b.cover_file:
                    best.cover_file = b.cover_file
                    break
        if len(group) > 1:
            names = sorted(set(b.device_display_name for b in group))
            best.device_display_name = ", ".join(names)
        result.append(best)

    result.extend(no_md5)
    result.sort(key=lambda b: b.title.lower())
    return result


SYNC_INTERVALS = [
    ("manual", "Manual only"),
    ("5m",     "Every 5 minutes"),
    ("10m",    "Every 10 minutes"),
    ("30m",    "Every 30 minutes"),
    ("hourly", "Every hour"),
    ("6h",     "Every 6 hours"),
    ("daily",  "Daily"),
    ("weekly", "Weekly"),
]

SYNC_DEFER_OPTIONS = [
    ("none",  "No defer"),
    ("30m",   "30 minutes"),
    ("1h",    "1 hour"),
    ("2h",    "2 hours"),
    ("6h",    "6 hours"),
    ("12h",   "12 hours"),
    ("daily", "1 day"),
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
    has_chars: str = Query(default=""),
):
    settings = get_settings()
    devices = storage.list_devices(settings.kolibrary_db_path)
    books = storage.list_books(
        settings.kolibrary_db_path,
        device_id=device_id or None,
        search=search,
        status_filter=status,
    )
    # Override progress with KoSync data where available
    kosync_pct = storage.load_kosync_progress(settings.kosync_db_path)
    for b in books:
        if b.md5 and b.md5 in kosync_pct:
            b.progress_pct = kosync_pct[b.md5]

    # Deduplicate same book across devices (all-devices view only)
    if not device_id:
        books = _deduplicate_by_md5(books)

    # Books that have characters in KoCharacters
    kochar_book_ids: set[int] = set()
    try:
        from app.services.kocharacters import storage as kochar_storage
        kc_titles, kc_md5s = kochar_storage.get_book_identifiers(settings.kocharacters_db_path)
        for b in books:
            if (b.md5 and b.md5 in kc_md5s) or b.title.lower().strip() in kc_titles:
                kochar_book_ids.add(b.id)
    except Exception:
        pass

    if has_chars:
        books = [b for b in books if b.id in kochar_book_ids]

    root = request.scope.get("root_path", "").rstrip("/")
    return templates.TemplateResponse("library.html", {
        "request": request,
        "root": root,
        "devices": devices,
        "books": books,
        "selected_device": device_id,
        "search": search,
        "status_filter": status,
        "has_chars_filter": bool(has_chars),
        "kochar_book_ids": kochar_book_ids,
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
# Book detail
# ---------------------------------------------------------------------------

@router.get("/books/{book_id}", response_class=HTMLResponse)
async def book_detail(
    book_id: int,
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    book = storage.get_book_by_id(settings.kolibrary_db_path, book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    # Gather per-user KoStats for this book title
    book_stats: dict = {}
    try:
        from app.services.kostats import storage as kostats_storage
        from app.services.kostats.stats_reader import get_book_detail_stats
        read_pct = int(kostats_storage.get_setting(settings.kostats_db_path, "read_pct_threshold", "95"))
        for user in kostats_storage.list_users(settings.kostats_db_path):
            db = settings.kostats_dir / user.username / "statistics.sqlite3"
            if db.is_file():
                bs = get_book_detail_stats(
                    db, book.title,
                    kosync_db_path=settings.kosync_db_path,
                    read_pct_threshold=read_pct,
                )
                if bs:
                    book_stats[user.username] = bs
    except Exception:
        pass  # KoStats not configured — degrade gracefully

    # KoCharacters link
    kocharacters_book_id: str | None = None
    try:
        from app.services.kocharacters import storage as kochar_storage
        kocharacters_book_id = kochar_storage.find_book_id_for_library_book(
            settings.kocharacters_db_path,
            title=book.title,
            partial_md5=book.md5 or "",
        )
    except Exception:
        pass

    root = request.scope.get("root_path", "").rstrip("/")
    return templates.TemplateResponse("book_detail.html", {
        "request": request,
        "book": book,
        "book_stats": book_stats,
        "kocharacters_book_id": kocharacters_book_id,
        "root": root,
    })


# ---------------------------------------------------------------------------
# Sync API
# ---------------------------------------------------------------------------

@router.get("/devices/{device_id}/test-connection")
async def test_connection(
    device_id: int,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    device = storage.get_device(settings.kolibrary_db_path, device_id)
    if not device:
        raise HTTPException(status_code=404)

    steps = []

    # Step 1: TCP socket connect
    try:
        loop = asyncio.get_event_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, lambda: socket.create_connection((device.host, device.port), timeout=5)),
            timeout=6,
        )
        steps.append({"step": f"TCP {device.host}:{device.port}", "ok": True, "msg": "Connected"})
    except Exception as e:
        steps.append({"step": f"TCP {device.host}:{device.port}", "ok": False, "msg": str(e)})
        return JSONResponse({"steps": steps, "success": False})

    # Step 2: SSH handshake + auth
    try:
        password = storage.decrypt_password(settings.kolibrary_key_path, device.encrypted_password)
        async with asyncssh.connect(
            device.host, port=device.port, username=device.username,
            password=password, known_hosts=None, connect_timeout=10,
        ) as conn:
            steps.append({"step": "SSH auth", "ok": True, "msg": f"Logged in as {device.username}"})

            # Step 3: check books path exists
            result = await conn.run(f'test -d {device.books_path} && echo EXISTS || echo MISSING', check=False)
            exists = result.stdout.strip() == "EXISTS"
            steps.append({
                "step": f"Books path {device.books_path}",
                "ok": exists,
                "msg": "Directory exists" if exists else "Directory not found",
            })

            if exists:
                # Step 4: quick count of .sdr dirs
                result = await conn.run(
                    f'timeout 10 find {device.books_path} -name "*.sdr" -type d 2>/dev/null | wc -l',
                    check=False,
                )
                count = result.stdout.strip()
                steps.append({"step": "Find .sdr dirs", "ok": True, "msg": f"{count} book(s) found"})

    except asyncssh.PermissionDenied:
        steps.append({"step": "SSH auth", "ok": False, "msg": "Permission denied — wrong username/password"})
    except asyncssh.Error as e:
        steps.append({"step": "SSH auth", "ok": False, "msg": str(e)})
    except Exception as e:
        steps.append({"step": "SSH", "ok": False, "msg": str(e)})

    return JSONResponse({"steps": steps, "success": all(s["ok"] for s in steps)})


@router.post("/devices/{device_id}/sync")
async def trigger_sync(
    device_id: int,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    device = storage.get_device(settings.kolibrary_db_path, device_id)
    if not device:
        raise HTTPException(status_code=404)
    sync.create_sync_task(sync.sync_device(
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
        "sync_defer_options": SYNC_DEFER_OPTIONS,
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
    sync_defer = str(form.get("sync_defer", "none")).strip()
    root = request.scope.get("root_path", "").rstrip("/")
    if name and host and username and password:
        enc = storage.encrypt_password(settings.kolibrary_key_path, password)
        storage.create_device(settings.kolibrary_db_path, name, friendly_name, host, port,
                              username, enc, books_path, sync_interval, sync_defer)
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
    sync_defer = str(form.get("sync_defer", "none")).strip()
    root = request.scope.get("root_path", "").rstrip("/")
    enc = storage.encrypt_password(settings.kolibrary_key_path, password) if password else None
    storage.update_device(settings.kolibrary_db_path, device_id, name, friendly_name, host,
                          port, username, books_path, sync_interval, sync_defer, enc)
    return RedirectResponse(url=f"{root}/services/kolibrary/settings", status_code=303)


@router.post("/settings/clear-log/{device_id}")
async def clear_log(
    device_id: int,
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    storage.clear_sync_logs(settings.kolibrary_db_path, device_id)
    root = request.scope.get("root_path", "").rstrip("/")
    return RedirectResponse(url=f"{root}/services/kolibrary/settings", status_code=303)


@router.post("/settings/clear-books/{device_id}")
async def clear_device_books(
    device_id: int,
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    storage.delete_device_books(settings.kolibrary_db_path, device_id)
    root = request.scope.get("root_path", "").rstrip("/")
    return RedirectResponse(url=f"{root}/services/kolibrary/settings", status_code=303)


@router.post("/settings/clear-all-books")
async def clear_all_books(
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    storage.delete_all_books(settings.kolibrary_db_path)
    root = request.scope.get("root_path", "").rstrip("/")
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

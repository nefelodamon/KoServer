import io
import json
import logging
import re
import shutil
import tarfile
from pathlib import Path
from typing import Annotated

import aiofiles
from PIL import Image
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader

from app.auth import require_api_key, require_ha_auth

logger = logging.getLogger(__name__)

_THUMB_SIZE = (200, 200)
from app.config import get_settings
from app.services.kocharacters import storage

_SERVICE_TEMPLATES = Path(__file__).parent / "templates"
_BASE_TEMPLATES = Path(__file__).parent.parent.parent / "templates"

# Search service templates first, then shared base templates
templates = Jinja2Templates(env=Environment(
    loader=ChoiceLoader([
        FileSystemLoader(str(_SERVICE_TEMPLATES)),
        FileSystemLoader(str(_BASE_TEMPLATES)),
    ]),
    autoescape=True,
))

router = APIRouter()


def _make_thumbnail(source: Path) -> None:
    try:
        thumb_dir = source.parent / "thumbnails"
        thumb_dir.mkdir(exist_ok=True)
        with Image.open(source) as img:
            img.thumbnail(_THUMB_SIZE, Image.LANCZOS)
            img.save(thumb_dir / source.name, "PNG", optimize=True)
    except Exception as exc:
        logger.warning("Thumbnail generation failed for %s: %s", source.name, exc)


def _book_id_from_name(name: str) -> str:
    """Sanitize a string into a safe book_id slug."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name).strip("_") or "unknown"


# ---------------------------------------------------------------------------
# Upload endpoint (API key auth)
# ---------------------------------------------------------------------------

@router.post("/api/upload", status_code=status.HTTP_200_OK)
async def upload_archive(
    file: Annotated[UploadFile, File(description="KoCharacters tar.gz export")],
    _: Annotated[None, Depends(require_api_key)],
):
    settings = get_settings()

    if not file.filename or not file.filename.lower().endswith(".tar.gz"):
        raise HTTPException(status_code=400, detail="Uploaded file must be a .tar.gz")

    raw = await file.read()
    try:
        tf = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
    except tarfile.TarError:
        raise HTTPException(status_code=400, detail="File is not a valid tar.gz archive")

    names = tf.getnames()

    # Locate characters.json (may be at root or inside a single subdirectory)
    json_candidates = [n for n in names if n.endswith("characters.json")]
    if not json_candidates:
        raise HTTPException(status_code=400, detail="characters.json not found in archive")

    json_path = sorted(json_candidates, key=len)[0]  # prefer shortest (root level)
    prefix = json_path[: -len("characters.json")]     # e.g. "" or "Heresy_7708/"

    # Parse characters.json
    try:
        member = tf.getmember(json_path)
        characters_raw = json.loads(tf.extractfile(member).read().decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to parse characters.json: {exc}")

    if not isinstance(characters_raw, list):
        raise HTTPException(status_code=400, detail="characters.json must be a JSON array")

    # Derive book_id and title from filename stem (strip .tar.gz)
    fname = file.filename
    if fname.lower().endswith(".tar.gz"):
        fname = fname[:-7]
    book_id = _book_id_from_name(fname or prefix.strip("/"))
    title = fname.replace("_", " ").rsplit(" ", 1)[0] if "_" in fname else fname

    # Read optional book_context.txt
    context = ""
    context_path = f"{prefix}book_context.txt"
    if context_path in names:
        context = tf.extractfile(tf.getmember(context_path)).read().decode("utf-8", errors="replace").strip()

    # Extract portraits
    portrait_dir = settings.portraits_dir / book_id
    portrait_dir.mkdir(parents=True, exist_ok=True)

    portrait_prefix = f"{prefix}portraits/"
    for name in names:
        if name.startswith(portrait_prefix) and name != portrait_prefix:
            filename = Path(name).name
            if filename:
                member = tf.getmember(name)
                if member.isfile():
                    data = tf.extractfile(member).read()
                    dest = portrait_dir / filename
                    async with aiofiles.open(dest, "wb") as f:
                        await f.write(data)
                    _make_thumbnail(dest)

    # Persist to DB
    storage.upsert_book(settings.kocharacters_db_path, book_id, title, context)
    storage.upsert_characters(settings.kocharacters_db_path, book_id, characters_raw)

    return {
        "status": "ok",
        "book_id": book_id,
        "title": title,
        "characters_imported": len(characters_raw),
    }


# ---------------------------------------------------------------------------
# Web UI endpoints (HA token auth)
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def library(
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    books = storage.list_books(settings.kocharacters_db_path)
    return templates.TemplateResponse(
        "library.html", {"request": request, "books": books}
    )


@router.get("/books/{book_id}", response_class=HTMLResponse)
async def book_detail(
    book_id: str,
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    book = storage.get_book(settings.kocharacters_db_path, book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    characters = storage.get_characters(settings.kocharacters_db_path, book_id)
    return templates.TemplateResponse(
        "book.html",
        {"request": request, "book": book, "characters": characters},
    )


@router.post("/books/{book_id}/delete")
async def delete_book(
    book_id: str,
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    storage.soft_delete_book(settings.kocharacters_db_path, book_id)
    root = request.scope.get("root_path", "").rstrip("/")
    return RedirectResponse(url=f"{root}/services/kocharacters", status_code=303)


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    deleted_books = storage.list_deleted_books(settings.kocharacters_db_path)
    return templates.TemplateResponse(
        "settings.html", {"request": request, "deleted_books": deleted_books}
    )


@router.post("/settings/restore/{book_id}")
async def restore_book(
    book_id: str,
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    storage.restore_book(settings.kocharacters_db_path, book_id)
    root = request.scope.get("root_path", "").rstrip("/")
    return RedirectResponse(url=f"{root}/services/kocharacters/settings", status_code=303)


@router.post("/settings/purge/{book_id}")
async def purge_book(
    book_id: str,
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    if storage.purge_book(settings.kocharacters_db_path, book_id):
        shutil.rmtree(settings.portraits_dir / book_id, ignore_errors=True)
    root = request.scope.get("root_path", "").rstrip("/")
    return RedirectResponse(url=f"{root}/services/kocharacters/settings", status_code=303)


@router.post("/settings/purge-all")
async def purge_all(
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    purged_ids = storage.purge_all_deleted(settings.kocharacters_db_path)
    for book_id in purged_ids:
        shutil.rmtree(settings.portraits_dir / book_id, ignore_errors=True)
    root = request.scope.get("root_path", "").rstrip("/")
    return RedirectResponse(url=f"{root}/services/kocharacters/settings", status_code=303)


@router.get("/debug", response_class=HTMLResponse)
async def debug(
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    books = storage.list_books(settings.kocharacters_db_path)
    all_characters = storage.get_all_characters(settings.kocharacters_db_path)
    return templates.TemplateResponse(
        "debug.html", {"request": request, "books": books, "all_characters": all_characters}
    )


@router.get("/portraits/{book_id}/thumbnails/{filename}")
async def serve_thumbnail(
    book_id: str,
    filename: str,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    safe_filename = Path(filename).name
    thumb_path = settings.portraits_dir / book_id / "thumbnails" / safe_filename
    # Fall back to full-size if thumbnail not yet generated
    full_path = settings.portraits_dir / book_id / safe_filename
    for path in (thumb_path, full_path):
        if path.exists() and path.is_file():
            return FileResponse(str(path), media_type="image/png")
    return Response(content=_placeholder_svg(), media_type="image/svg+xml")


@router.get("/portraits/{book_id}/{filename}")
async def serve_portrait(
    book_id: str,
    filename: str,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    safe_filename = Path(filename).name
    portrait_path = settings.portraits_dir / book_id / safe_filename
    if not portrait_path.exists() or not portrait_path.is_file():
        return Response(content=_placeholder_svg(), media_type="image/svg+xml")
    return FileResponse(str(portrait_path), media_type="image/png")


def _placeholder_svg() -> str:
    return """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
  <rect width="200" height="200" fill="#2d2d2d"/>
  <circle cx="100" cy="80" r="40" fill="#555"/>
  <ellipse cx="100" cy="180" rx="60" ry="40" fill="#555"/>
  <text x="100" y="210" text-anchor="middle" fill="#888" font-size="12" font-family="sans-serif">No portrait</text>
</svg>"""

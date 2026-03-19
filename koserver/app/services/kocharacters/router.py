import asyncio
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
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader

from app.auth import require_api_key, require_ha_auth
from app.config import get_settings
from app.services.kocharacters import storage
from app.services.kocharacters.storage import DEFAULT_THUMBNAIL_SIZE, THUMBNAIL_SIZE_KEY
from app.services.kosync import storage as kosync_storage
from app.tz import localtime_filter, mins_hm

logger = logging.getLogger(__name__)

_SERVICE_TEMPLATES = Path(__file__).parent / "templates"
_BASE_TEMPLATES = Path(__file__).parent.parent.parent / "templates"

# Search service templates first, then shared base templates
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


def _make_thumbnail(source: Path, size: int) -> None:
    try:
        thumb_dir = source.parent / "thumbnails"
        thumb_dir.mkdir(exist_ok=True)
        with Image.open(source) as img:
            img.thumbnail((size, size), Image.LANCZOS)
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

    # Parse optional book_meta.json (preferred) or fall back to book_context.txt
    meta: dict = {}
    meta_path = f"{prefix}book_meta.json"
    if meta_path in names:
        try:
            meta = json.loads(tf.extractfile(tf.getmember(meta_path)).read().decode("utf-8"))
        except Exception as exc:
            logger.warning("Failed to parse book_meta.json: %s", exc)

    # Derive title: prefer book_meta, then filename stem
    title = meta.get("title") or title

    # Context: prefer book_meta.book_context, fall back to book_context.txt
    context = meta.get("book_context", "")
    if not context:
        context_path = f"{prefix}book_context.txt"
        if context_path in names:
            context = tf.extractfile(tf.getmember(context_path)).read().decode("utf-8", errors="replace").strip()

    # Extract portraits and optional cover
    portrait_dir = settings.portraits_dir / book_id
    portrait_dir.mkdir(parents=True, exist_ok=True)
    thumb_size = int(storage.get_setting(
        settings.kocharacters_db_path, THUMBNAIL_SIZE_KEY, str(DEFAULT_THUMBNAIL_SIZE)
    ))

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
                    _make_thumbnail(dest, thumb_size)

    cover_filename = ""
    if meta.get("cover"):
        cover_arc_path = f"{prefix}{meta['cover']}"
        if cover_arc_path in names:
            try:
                cover_data = tf.extractfile(tf.getmember(cover_arc_path)).read()
                cover_filename = Path(meta["cover"]).name
                async with aiofiles.open(portrait_dir / cover_filename, "wb") as f:
                    await f.write(cover_data)
            except Exception as exc:
                logger.warning("Failed to extract cover: %s", exc)

    # Persist to DB
    storage.upsert_book(
        settings.kocharacters_db_path,
        book_id,
        title=title,
        context=context,
        authors=meta.get("authors", ""),
        series=meta.get("series", ""),
        series_index=meta.get("series_index"),
        language=meta.get("language", ""),
        description=meta.get("description", ""),
        identifiers=json.dumps(meta.get("identifiers") or {}),
        keywords=json.dumps(meta.get("keywords") or []),
        total_pages=meta.get("total_pages"),
        percent_finished=meta.get("percent_finished"),
        reading_status=meta.get("reading_status", ""),
        last_read=meta.get("last_read", ""),
        highlights=meta.get("highlights"),
        notes=meta.get("notes"),
        partial_md5=meta.get("partial_md5", ""),
        cover_filename=cover_filename,
    )
    storage.upsert_characters(settings.kocharacters_db_path, book_id, characters_raw)

    return {
        "status": "ok",
        "book_id": book_id,
        "title": title,
        "authors": meta.get("authors", ""),
        "series": meta.get("series", ""),
        "characters_imported": len(characters_raw),
        "has_cover": bool(cover_filename),
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
    root = request.scope.get("root_path", "").rstrip("/")
    return templates.TemplateResponse(
        "library.html", {"request": request, "root": root, "books": books}
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
    # Auto-detect cover if not recorded in DB
    if not book.cover_filename:
        for candidate in ("cover.jpg", "cover.jpeg", "cover.png", "cover.webp"):
            if (settings.portraits_dir / book_id / candidate).exists():
                book.cover_filename = candidate
                storage.upsert_book(
                    settings.kocharacters_db_path, book_id, title=book.title,
                    context=book.context, authors=book.authors, series=book.series,
                    series_index=book.series_index, language=book.language,
                    description=book.description,
                    identifiers=json.dumps(book.identifiers),
                    keywords=json.dumps(book.keywords),
                    total_pages=book.total_pages, percent_finished=book.percent_finished,
                    reading_status=book.reading_status, last_read=book.last_read,
                    highlights=book.highlights, notes=book.notes,
                    partial_md5=book.partial_md5, cover_filename=candidate,
                )
                break
    characters = storage.get_characters(settings.kocharacters_db_path, book_id)
    # Sort: protagonists first, then characters with portraits, then alphabetical
    characters.sort(key=lambda c: (
        0 if c.role == "protagonist" else 1,
        0 if c.portrait_file else 1,
        c.name.lower(),
    ))
    kosync_progress = (
        kosync_storage.get_progress_by_document(settings.kosync_db_path, book.partial_md5)
        if book.partial_md5 else []
    )
    root = request.scope.get("root_path", "").rstrip("/")
    return templates.TemplateResponse(
        "book.html",
        {"request": request, "root": root, "book": book, "characters": characters, "kosync_progress": kosync_progress},
    )


@router.get("/books/{book_id}/characters/{character_id}", response_class=HTMLResponse)
async def character_detail(
    book_id: str,
    character_id: str,
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    book = storage.get_book(settings.kocharacters_db_path, book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    char = storage.get_character(settings.kocharacters_db_path, book_id, character_id)
    if not char:
        raise HTTPException(status_code=404, detail="Character not found")
    root = request.scope.get("root_path", "").rstrip("/")
    return templates.TemplateResponse(
        "character.html",
        {"request": request, "root": root, "book": book, "char": char},
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
    thumbnail_size = int(storage.get_setting(
        settings.kocharacters_db_path, THUMBNAIL_SIZE_KEY, str(DEFAULT_THUMBNAIL_SIZE)
    ))
    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "deleted_books": deleted_books, "thumbnail_size": thumbnail_size},
    )


@router.get("/settings/regenerate-thumbnails/stream")
async def regenerate_thumbnails_stream(
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    thumb_size = int(storage.get_setting(
        settings.kocharacters_db_path, THUMBNAIL_SIZE_KEY, str(DEFAULT_THUMBNAIL_SIZE)
    ))

    async def generate():
        files = [
            f for f in settings.portraits_dir.rglob("*")
            if f.is_file() and "thumbnails" not in f.parts
        ]
        total = len(files)
        yield f"data: {json.dumps({'done': 0, 'total': total})}\n\n"
        loop = asyncio.get_event_loop()
        for i, portrait_file in enumerate(files, 1):
            await loop.run_in_executor(None, _make_thumbnail, portrait_file, thumb_size)
            yield f"data: {json.dumps({'done': i, 'total': total, 'file': portrait_file.name})}\n\n"
        logger.info("Regenerated %d thumbnails at %dpx", total, thumb_size)
        yield f"data: {json.dumps({'done': total, 'total': total, 'complete': True})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/settings/save")
async def save_settings(
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    form = await request.form()
    try:
        size = max(100, min(1000, int(form.get("thumbnail_size", DEFAULT_THUMBNAIL_SIZE))))
    except (ValueError, TypeError):
        size = DEFAULT_THUMBNAIL_SIZE
    storage.set_setting(settings.kocharacters_db_path, THUMBNAIL_SIZE_KEY, str(size))
    root = request.scope.get("root_path", "").rstrip("/")
    return RedirectResponse(url=f"{root}/services/kocharacters/settings", status_code=303)


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
    _NC = {"Cache-Control": "no-cache"}
    for path in (thumb_path, full_path):
        if path.exists() and path.is_file():
            return FileResponse(str(path), media_type=_media_type(path), headers=_NC)
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
    return FileResponse(str(portrait_path), media_type=_media_type(portrait_path),
                        headers={"Cache-Control": "no-cache"})


_MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def _media_type(path: Path) -> str:
    return _MEDIA_TYPES.get(path.suffix.lower(), "image/png")


def _placeholder_svg() -> str:
    return """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
  <rect width="200" height="200" fill="#2d2d2d"/>
  <circle cx="100" cy="80" r="40" fill="#555"/>
  <ellipse cx="100" cy="180" rx="60" ry="40" fill="#555"/>
  <text x="100" y="210" text-anchor="middle" fill="#888" font-size="12" font-family="sans-serif">No portrait</text>
</svg>"""

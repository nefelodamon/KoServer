import io
import json
import re
import zipfile
from pathlib import Path
from typing import Annotated

import aiofiles
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse
from jinja2 import ChoiceLoader, FileSystemLoader
from fastapi.templating import Jinja2Templates

from app.auth import require_api_key, require_ha_auth
from app.config import get_settings
from app.services.kobooks import storage

_SERVICE_TEMPLATES = Path(__file__).parent / "templates"
_BASE_TEMPLATES = Path(__file__).parent.parent.parent / "templates"

templates = Jinja2Templates(env=None)  # placeholder; configured below
templates.env = templates.env  # satisfy type checker

# Build a Jinja2 env that searches service templates first, then shared base templates
from jinja2 import Environment
_env = Environment(
    loader=ChoiceLoader([
        FileSystemLoader(str(_SERVICE_TEMPLATES)),
        FileSystemLoader(str(_BASE_TEMPLATES)),
    ]),
    autoescape=True,
)
templates = Jinja2Templates(env=_env)

router = APIRouter()


def _book_id_from_name(name: str) -> str:
    """Sanitize a string into a safe book_id slug."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name).strip("_") or "unknown"


# ---------------------------------------------------------------------------
# Upload endpoint (API key auth)
# ---------------------------------------------------------------------------

@router.post("/api/upload", status_code=status.HTTP_200_OK)
async def upload_zip(
    file: Annotated[UploadFile, File(description="KoCharacters ZIP export")],
    _: Annotated[None, Depends(require_api_key)],
):
    settings = get_settings()

    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Uploaded file must be a .zip")

    raw = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="File is not a valid ZIP archive")

    names = zf.namelist()

    # Locate characters.json (may be at root or inside a single subdirectory)
    json_candidates = [n for n in names if n.endswith("characters.json")]
    if not json_candidates:
        raise HTTPException(status_code=400, detail="characters.json not found in ZIP")

    json_path = sorted(json_candidates, key=len)[0]  # prefer shortest (root level)
    prefix = json_path[: -len("characters.json")]     # e.g. "" or "Heresy_7708/"

    # Parse characters.json
    try:
        characters_raw = json.loads(zf.read(json_path).decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to parse characters.json: {exc}")

    if not isinstance(characters_raw, list):
        raise HTTPException(status_code=400, detail="characters.json must be a JSON array")

    # Derive book_id and title
    zip_stem = Path(file.filename).stem                         # e.g. "Heresy_7708"
    book_id = _book_id_from_name(zip_stem or prefix.strip("/"))
    title = zip_stem.replace("_", " ").rsplit(" ", 1)[0] if "_" in zip_stem else zip_stem

    # Read optional book_context.txt
    context = ""
    context_path = f"{prefix}book_context.txt"
    if context_path in names:
        context = zf.read(context_path).decode("utf-8", errors="replace").strip()

    # Extract portraits
    portrait_dir = settings.portraits_dir / book_id
    portrait_dir.mkdir(parents=True, exist_ok=True)

    portrait_prefix = f"{prefix}portraits/"
    for name in names:
        if name.startswith(portrait_prefix) and name != portrait_prefix:
            filename = Path(name).name
            if filename:
                dest = portrait_dir / filename
                async with aiofiles.open(dest, "wb") as f:
                    f.write(zf.read(name))

    # Persist to DB
    storage.upsert_book(settings.db_path, book_id, title, context)
    storage.upsert_characters(settings.db_path, book_id, characters_raw)

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
    books = storage.list_books(settings.db_path)
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
    book = storage.get_book(settings.db_path, book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    characters = storage.get_characters(settings.db_path, book_id)
    return templates.TemplateResponse(
        "book.html",
        {"request": request, "book": book, "characters": characters},
    )


@router.get("/portraits/{book_id}/{filename}")
async def serve_portrait(
    book_id: str,
    filename: str,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    # Sanitize to prevent path traversal
    safe_filename = Path(filename).name
    portrait_path = settings.portraits_dir / book_id / safe_filename

    if not portrait_path.exists() or not portrait_path.is_file():
        # Return placeholder SVG
        placeholder = _placeholder_svg()
        from fastapi.responses import Response
        return Response(content=placeholder, media_type="image/svg+xml")

    return FileResponse(str(portrait_path), media_type="image/png")


def _placeholder_svg() -> str:
    return """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
  <rect width="200" height="200" fill="#2d2d2d"/>
  <circle cx="100" cy="80" r="40" fill="#555"/>
  <ellipse cx="100" cy="180" rx="60" ry="40" fill="#555"/>
  <text x="100" y="210" text-anchor="middle" fill="#888" font-size="12" font-family="sans-serif">No portrait</text>
</svg>"""

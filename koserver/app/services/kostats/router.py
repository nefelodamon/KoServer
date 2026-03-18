import base64
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader

from app.auth import require_ha_auth
from app.config import get_settings
from app.services.kostats import storage
from app.services.kostats.models import UploadedFile

logger = logging.getLogger(__name__)

_SERVICE_TEMPLATES = Path(__file__).parent / "templates"
_BASE_TEMPLATES = Path(__file__).parent.parent.parent / "templates"

templates = Jinja2Templates(env=Environment(
    loader=ChoiceLoader([
        FileSystemLoader(str(_SERVICE_TEMPLATES)),
        FileSystemLoader(str(_BASE_TEMPLATES)),
    ]),
    autoescape=True,
))

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_dav_auth(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """HTTP Basic auth dependency for WebDAV routes. Returns username."""
    if not authorization or not authorization.lower().startswith("basic "):
        raise HTTPException(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="KoStats"'},
            detail="Authentication required",
        )
    try:
        decoded = base64.b64decode(authorization[6:]).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    settings = get_settings()
    if not storage.authenticate(settings.kostats_db_path, username, password):
        raise HTTPException(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="KoStats"'},
            detail="Invalid credentials",
        )
    return username


def _user_dir(username: str) -> Path:
    settings = get_settings()
    d = settings.kostats_dir / username
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_path(base: Path, rel: str) -> Path:
    """Resolve rel under base; raise 403 on traversal attempts."""
    target = (base / rel.lstrip("/")).resolve()
    if not str(target).startswith(str(base.resolve())):
        raise HTTPException(status_code=403, detail="Path traversal not allowed")
    return target


def _http_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S GMT"
    )


def _propfind_xml(request: Request, entries: list[dict]) -> str:
    root = request.scope.get("root_path", "").rstrip("/")
    items = []
    for e in entries:
        href = e["href"]
        if e.get("is_dir"):
            rtype = "<D:resourcetype><D:collection/></D:resourcetype>"
        else:
            rtype = "<D:resourcetype/>"
        props = [rtype, f"<D:displayname>{e.get('name', '')}</D:displayname>"]
        if not e.get("is_dir") and "size" in e:
            props.append(f"<D:getcontentlength>{e['size']}</D:getcontentlength>")
            props.append("<D:getcontenttype>application/octet-stream</D:getcontenttype>")
        if "modified" in e:
            props.append(f"<D:getlastmodified>{e['modified']}</D:getlastmodified>")
        props_xml = "\n        ".join(props)
        items.append(
            f"  <D:response>\n"
            f"    <D:href>{href}</D:href>\n"
            f"    <D:propstat>\n"
            f"      <D:prop>\n"
            f"        {props_xml}\n"
            f"      </D:prop>\n"
            f"      <D:status>HTTP/1.1 200 OK</D:status>\n"
            f"    </D:propstat>\n"
            f"  </D:response>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<D:multistatus xmlns:D="DAV:">\n'
        + "\n".join(items)
        + "\n</D:multistatus>"
    )


def _list_files(user_dir: Path) -> list[UploadedFile]:
    files = []
    for f in sorted(user_dir.rglob("*")):
        if f.is_file():
            stat = f.stat()
            files.append(UploadedFile(
                name=f.name,
                path=str(f.relative_to(user_dir)),
                size=stat.st_size,
                modified=_http_date(stat.st_mtime),
            ))
    return files


# ---------------------------------------------------------------------------
# WebDAV routes
# ---------------------------------------------------------------------------

_DAV_METHODS = ["OPTIONS", "PROPFIND", "GET", "PUT", "MKCOL", "DELETE", "HEAD"]


@router.api_route("/dav", methods=_DAV_METHODS)
@router.api_route("/dav/", methods=_DAV_METHODS)
@router.api_route("/dav/{path:path}", methods=_DAV_METHODS)
async def webdav(
    request: Request,
    username: Annotated[str, Depends(_require_dav_auth)],
    path: str = "",
):
    method = request.method.upper()
    user_dir = _user_dir(username)
    target = _safe_path(user_dir, path)
    root = request.scope.get("root_path", "").rstrip("/")
    dav_base = f"{root}/services/kostats/dav"

    # OPTIONS
    if method == "OPTIONS":
        return Response(
            status_code=200,
            headers={
                "DAV": "1",
                "Allow": "OPTIONS, GET, HEAD, PUT, DELETE, PROPFIND, MKCOL",
                "Content-Length": "0",
            },
        )

    # PROPFIND
    if method == "PROPFIND":
        depth = request.headers.get("Depth", "1")
        entries = []

        def _entry(p: Path) -> dict:
            rel = str(p.relative_to(user_dir))
            href = f"{dav_base}/{rel}".replace("\\", "/")
            if p == user_dir:
                href = f"{dav_base}/"
            e = {"href": href, "name": p.name or username, "is_dir": p.is_dir()}
            if p.is_file():
                stat = p.stat()
                e["size"] = stat.st_size
                e["modified"] = _http_date(stat.st_mtime)
            return e

        if not target.exists():
            raise HTTPException(status_code=404)

        entries.append(_entry(target))
        if depth != "0" and target.is_dir():
            for child in sorted(target.iterdir()):
                entries.append(_entry(child))

        xml = _propfind_xml(request, entries)
        return Response(
            content=xml,
            status_code=207,
            media_type="application/xml; charset=utf-8",
        )

    # GET / HEAD
    if method in ("GET", "HEAD"):
        if not target.exists() or target.is_dir():
            raise HTTPException(status_code=404)
        if method == "HEAD":
            stat = target.stat()
            return Response(
                headers={
                    "Content-Length": str(stat.st_size),
                    "Last-Modified": _http_date(stat.st_mtime),
                }
            )
        return FileResponse(str(target))

    # PUT
    if method == "PUT":
        target.parent.mkdir(parents=True, exist_ok=True)
        body = await request.body()
        target.write_bytes(body)
        storage.touch_last_upload(get_settings().kostats_db_path, username)
        logger.info("KoStats: %s uploaded %s (%d bytes)", username, path, len(body))
        existed = target.stat().st_size > 0  # always true after write
        return Response(status_code=204 if existed else 201)

    # MKCOL
    if method == "MKCOL":
        if target.exists():
            raise HTTPException(status_code=405, detail="Already exists")
        target.mkdir(parents=True, exist_ok=True)
        return Response(status_code=201)

    # DELETE
    if method == "DELETE":
        if not target.exists():
            raise HTTPException(status_code=404)
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return Response(status_code=204)

    raise HTTPException(status_code=405)


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
    users = storage.list_users(settings.kostats_db_path)
    user_files: dict[str, list[UploadedFile]] = {}
    for user in users:
        d = settings.kostats_dir / user.username
        user_files[user.username] = _list_files(d) if d.exists() else []
        user.file_count = len(user_files[user.username])
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "users": users, "user_files": user_files},
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    users = storage.list_users(settings.kostats_db_path)
    return templates.TemplateResponse(
        "settings.html", {"request": request, "users": users}
    )


@router.post("/settings/create-user")
async def create_user(
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", "")).strip()
    root = request.scope.get("root_path", "").rstrip("/")
    if username and password:
        storage.create_user(settings.kostats_db_path, username, password)
    return RedirectResponse(url=f"{root}/services/kostats/settings", status_code=303)


@router.post("/settings/delete-user/{username}")
async def delete_user(
    username: str,
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    storage.delete_user(settings.kostats_db_path, username)
    user_dir = settings.kostats_dir / username
    shutil.rmtree(user_dir, ignore_errors=True)
    root = request.scope.get("root_path", "").rstrip("/")
    return RedirectResponse(url=f"{root}/services/kostats/settings", status_code=303)


@router.post("/settings/change-password/{username}")
async def change_password(
    username: str,
    request: Request,
    _: Annotated[str, Depends(require_ha_auth)],
):
    settings = get_settings()
    form = await request.form()
    new_password = str(form.get("password", "")).strip()
    if new_password:
        storage.change_password(settings.kostats_db_path, username, new_password)
    root = request.scope.get("root_path", "").rstrip("/")
    return RedirectResponse(url=f"{root}/services/kostats/settings", status_code=303)

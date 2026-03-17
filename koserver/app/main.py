import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.auth import clear_auth_cookie, set_auth_cookie, validate_token
from app.config import get_settings
from app.services.kocharacters import router as kocharacters_router
from app.services.kocharacters.storage import init_db

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"
VERSION = os.getenv("KOSERVER_VERSION", "dev")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _root(request: Request) -> str:
    """Return the ingress root path prefix (e.g. /app/ac7e9e47_koserver)."""
    return request.scope.get("root_path", "").rstrip("/")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("KoServer starting %s", VERSION)
    settings = get_settings()
    settings.portraits_dir.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    await init_db(settings.db_path)
    yield


app = FastAPI(title="KoServer", version=VERSION, lifespan=lifespan)



app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(kocharacters_router.router, prefix="/services/kocharacters")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def root(request: Request):
    return RedirectResponse(url=f"{_root(request)}/services/kocharacters")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": error}
    )


@app.post("/login")
async def login_submit(request: Request, token: str = Form(...)):
    if not await validate_token(token):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "invalid"},
            status_code=401,
        )
    response = RedirectResponse(
        url=f"{_root(request)}/services/kocharacters", status_code=303
    )
    set_auth_cookie(response, token)
    return response


@app.get("/logout")
async def logout(request: Request):
    response = RedirectResponse(url=f"{_root(request)}/login")
    clear_auth_cookie(response)
    return response

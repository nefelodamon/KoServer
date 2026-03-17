import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.services.kobooks import router as kobooks_router
from app.services.kobooks.storage import init_db

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
VERSION = os.getenv("KOSERVER_VERSION", "dev")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("KoServer starting %s", VERSION)
    settings = get_settings()
    settings.portraits_dir.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    await init_db(settings.db_path)
    yield


app = FastAPI(title="KoServer", version=VERSION, lifespan=lifespan)


@app.middleware("http")
async def ingress_middleware(request: Request, call_next):
    """Read HA ingress path header and set it as the ASGI root_path.

    When accessed via HA ingress the supervisor sets X-Ingress-Path to the
    URL prefix it strips before forwarding (e.g. /app/ac7e9e47_koserver).
    Setting root_path ensures FastAPI generates correct redirect URLs.
    """
    ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")
    if ingress_path:
        request.scope["root_path"] = ingress_path
    return await call_next(request)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.include_router(kobooks_router.router, prefix="/services/kobooks")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def root():
    return RedirectResponse(url="services/kobooks")

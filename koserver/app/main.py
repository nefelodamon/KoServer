import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.services.kobooks import router as kobooks_router
from app.services.kobooks.storage import init_db

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
VERSION = "0.1.1"


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

app.include_router(kobooks_router.router, prefix="/services/kobooks")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/services/kobooks")

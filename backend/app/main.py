"""FastAPI application entry point."""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import assets, jobs, media, projects, segments, system
from .config import get_settings
from .database import init_db
from .security import extract_token, get_token
from .services import analyzer, indexer  # noqa: F401 - registers job tasks
from .services.runner import runner

logger = logging.getLogger(__name__)

settings = get_settings()

# Paths that must stay reachable before the token is known.
EXEMPT_PATHS = {"/api/system/health", "/api/system/token", "/api/system/config"}

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    token = get_token()
    logger.info("=" * 60)
    logger.info("Travel Storyboard v%s", __version__)
    logger.info("Local access token: %s", token)
    logger.info("=" * 60)
    yield
    runner.shutdown()


app = FastAPI(title="Travel Storyboard", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api") and path not in EXEMPT_PATHS:
        supplied = extract_token(request)
        if not supplied or not secrets.compare_digest(supplied, get_token()):
            return JSONResponse(
                status_code=401, content={"detail": "Invalid or missing access token"}
            )
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(system.router, prefix="/api")
app.include_router(projects.router, prefix="/api")
app.include_router(assets.router, prefix="/api")
app.include_router(segments.router, prefix="/api")
app.include_router(jobs.router, prefix="/api")
app.include_router(media.router, prefix="/api")


@app.get("/api", include_in_schema=False)
def api_root() -> dict:
    return {"name": "Travel Storyboard", "version": __version__}


# Serve the built frontend (production mode) if present.
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")

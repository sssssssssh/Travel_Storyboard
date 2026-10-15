"""FastAPI application entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import (
    assets,
    export,
    jobs,
    media,
    packages,
    projects,
    segments,
    story,
    system,
    vision,
)
from .config import get_settings
from .database import init_db
from .security import check_request_origin, extract_token, get_token, token_matches
from .services import (  # noqa: F401 - registers job tasks
    analyzer,
    indexer,
    review_package,
    roughcut,
    story_planner,
)
from .services.vision.motion import register_motion_tasks
from .services.vision.semantic import register_vision_tasks
from .services.runner import converge_pending_cancellations, runner, resume_interrupted_jobs

# M5 job types are registered explicitly rather than by import side effect, so
# they cannot be silently missing from TASK_REGISTRY.
register_vision_tasks()
register_motion_tasks()

logger = logging.getLogger(__name__)

settings = get_settings()

# Only liveness/config introspection stays reachable without the token; neither
# reveals the token or any user data. `/api/system/token` is deliberately NOT
# here — handing out the token unauthenticated would defeat the whole boundary.
EXEMPT_PATHS = {"/api/system/health", "/api/system/config"}

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    token = get_token()
    logger.info("=" * 60)
    logger.info("Travel Storyboard v%s", __version__)
    logger.info(
        "Open: http://%s:%s/?token=%s", settings.bind_host, settings.bind_port, token
    )
    logger.info("(Token is also stored in %s)", settings.data_path / "token.txt")
    logger.info("=" * 60)
    # TSB-261: settle any `cancelling` row left behind by a previous process
    # *before* resuming, so an abandoned cancel cannot keep a project locked
    # (nor be mistaken for work worth restarting). At this point the old process
    # is provably gone, so nothing is still encoding.
    settled = converge_pending_cancellations()
    if settled:
        logger.info("Settled %d orphaned cancelling job(s): %s", len(settled), settled)
    resumed = resume_interrupted_jobs()
    if resumed:
        logger.info("Re-dispatched %d interrupted job(s): %s", len(resumed), resumed)
    yield
    runner.shutdown()


app = FastAPI(title="Travel Storyboard", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    """Loopback-only origin validation + token auth for /api/*."""
    try:
        check_request_origin(request)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    path = request.url.path
    if path.startswith("/api") and path not in EXEMPT_PATHS:
        if not token_matches(extract_token(request)):
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
# Specific visual job routes must precede /jobs/{job_type}; otherwise the UI's
# flat consent/options are silently parsed as an empty generic JobCreate.
app.include_router(vision.router, prefix="/api")
app.include_router(jobs.router, prefix="/api")
app.include_router(media.router, prefix="/api")
app.include_router(packages.router, prefix="/api")
app.include_router(story.router, prefix="/api")
app.include_router(export.router, prefix="/api")


@app.get("/api", include_in_schema=False)
def api_root() -> dict:
    return {"name": "Travel Storyboard", "version": __version__}


# Serve the built frontend (production mode) if present.
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")

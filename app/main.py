import logging
import secrets
from pathlib import Path
from logging.handlers import RotatingFileHandler
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings, PROJECT_ROOT
from app.database import init_db
from workflows.base import registry
from app.routers import (
    pages_router,
    settings_router,
    workflows_router,
    jobs_router,
    chat_router,
    runs_router
)


# Configure rotating structured logger (Max 5MB per file, max 3 backups)
LOGS_DIR = PROJECT_ROOT / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
log_file = LOGS_DIR / "ForgeX.log"

logger = logging.getLogger("ForgeX")
logger.setLevel(logging.INFO)
file_handler = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
file_handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s"))
logger.addHandler(file_handler)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize Database and Discover Workflows
    logger.info("Initializing ForgeX database...")
    await init_db()
    
    logger.info("Discovering workflow plug-ins...")
    workflows_dir = PROJECT_ROOT / "workflows"
    registry.discover(workflows_dir)
    logger.info(f"Loaded {len(registry.list_all())} workflows: {[w.name for w in registry.list_all()]}")
    
    # Ensure default data folders exist
    settings.DEFAULT_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (settings.DEFAULT_DATA_DIR / "screenshots").mkdir(parents=True, exist_ok=True)
    settings.BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    
    yield
    logger.info("ForgeX shutting down cleanly.")

app = FastAPI(
    title="ForgeX",
    version="1.0.0",
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url=None,
    lifespan=lifespan
)

# Host validation & Double-Submit CSRF Middleware
@app.middleware("http")
async def security_middleware(request: Request, call_next):
    # 1. Host header validation (Strictly 127.0.0.1 or localhost)
    raw_host = request.headers.get("host", "")
    host = raw_host.split(":")[0].strip("[]")
    if host not in settings.ALLOWED_HOSTS:
        logger.warning(f"Rejected request with unauthorized Host header: {raw_host}")
        return JSONResponse(status_code=400, content={"detail": f"Invalid Host header: {raw_host}"})

    # 2. CSRF Token handling
    cookie_csrf = request.cookies.get("csrftoken")
    new_csrf = None
    if not cookie_csrf:
        cookie_csrf = secrets.token_hex(32)
        new_csrf = cookie_csrf

    # Enforce CSRF on state-changing methods
    if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
        # Skip CSRF check only for multipart file uploads or read from custom header
        header_csrf = request.headers.get("x-csrf-token")
        
        # Also check form body if available and header missing
        form_csrf = None
        content_type = request.headers.get("content-type", "")
        if not header_csrf and "application/x-www-form-urlencoded" in content_type:
            try:
                form = await request.form()
                form_csrf = form.get("_csrf")
            except Exception:
                pass

        token_to_verify = header_csrf or form_csrf
        if not token_to_verify or token_to_verify != request.cookies.get("csrftoken"):
            logger.warning(f"CSRF validation failed for {request.method} {request.url.path}")
            return JSONResponse(
                status_code=403,
                content={"detail": "CSRF token missing or mismatch. Please refresh the page."}
            )

    response: Response = await call_next(request)

    # Set CSRF cookie if it was newly generated
    if new_csrf:
        response.set_cookie(
            key="csrftoken",
            value=new_csrf,
            httponly=False,  # Accessible to client JS for double-submit header
            samesite="strict",
            secure=False  # Localhost HTTP
        )

    return response

# Mount static and media directories
static_dir = PROJECT_ROOT / "app" / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

data_dir = settings.DEFAULT_DATA_DIR
data_dir.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(data_dir)), name="media")
app.mount("/data", StaticFiles(directory=str(data_dir)), name="data")

# Include Routers
app.include_router(pages_router)
app.include_router(settings_router)
app.include_router(workflows_router)
app.include_router(jobs_router)
app.include_router(chat_router)
app.include_router(runs_router)


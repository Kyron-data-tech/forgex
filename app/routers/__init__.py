from app.routers.pages import router as pages_router
from app.routers.api_settings import router as settings_router
from app.routers.api_workflows import router as workflows_router
from app.routers.api_jobs import router as jobs_router
from app.routers.api_chat import router as chat_router
from app.routers.api_runs import router as runs_router

__all__ = [
    "pages_router",
    "settings_router",
    "workflows_router",
    "jobs_router",
    "chat_router",
    "runs_router",
]

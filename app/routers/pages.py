from pathlib import Path
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from app.database import get_db
from app.models.job import Job, AppSetting
from app.config import settings
from workflows.base import registry

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

@router.get("/")
async def root_redirect():
    return RedirectResponse(url="/jobs/new")

@router.get("/settings")
async def settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    stmt = select(AppSetting).where(AppSetting.key == "user_data_dir")
    setting = (await db.execute(stmt)).scalar_one_or_none()
    current_path = setting.value if setting else str(settings.DEFAULT_DATA_DIR.resolve())
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "current_path": current_path,
            "page_title": "Storage & Authentication Settings"
        }
    )

@router.get("/jobs/new")
async def new_job_page(request: Request):
    workflows = registry.list_all()
    return templates.TemplateResponse(
        request=request,
        name="new_job.html",
        context={
            "workflows": workflows,
            "page_title": "Create New RPA Job"
        }
    )

@router.get("/jobs/{job_id}/review")
async def review_page(job_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    stmt = select(Job).where(Job.id == job_id)
    job = (await db.execute(stmt)).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    workflow = registry.get(job.workflow_name)
    review_schema = workflow.get_review_schema() if workflow else {}

    return templates.TemplateResponse(
        request=request,
        name="review.html",
        context={
            "job": job,
            "review_schema": review_schema,
            "page_title": f"Review & Approval - {job.subject_name}"
        }
    )

@router.get("/history")
async def history_page(request: Request, db: AsyncSession = Depends(get_db)):
    stmt = select(Job).order_by(desc(Job.id))
    jobs = (await db.execute(stmt)).scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="history.html",
        context={
            "jobs": jobs,
            "page_title": "Job History & Audit Trail"
        }
    )

@router.get("/agent")
async def agent_dashboard_page(request: Request, db: AsyncSession = Depends(get_db)):
    # Scan inputs directory for CSV/XLSX files
    inputs_dir = settings.DEFAULT_DATA_DIR / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    input_files = [f.name for f in inputs_dir.glob("*.*") if f.suffix.lower() in (".csv", ".tsv", ".xlsx")]

    workflows = registry.list_all()

    return templates.TemplateResponse(
        request=request,
        name="agent.html",
        context={
            "workflows": workflows,
            "input_files": input_files,
            "page_title": "ForgeX Operator Agent — Live Dashboard"
        }
    )


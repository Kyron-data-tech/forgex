from typing import Any
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from app.database import get_db
from app.models.job import Job
from app.routers.api_jobs import create_job, CreateJobRequest
from app.services.storage import StorageService

router = APIRouter(prefix="/api/chat", tags=["chat"])

ALLOWED_CHAT_ACTIONS = {
    "create_job",
    "get_job_status",
    "list_history",
    "update_review_field"
}

class ChatActionRequest(BaseModel):
    action: str = Field(description="Must be strictly one of ALLOWED_CHAT_ACTIONS")
    params: dict = Field(default_factory=dict)

@router.post("/action")
async def execute_chat_action(req: ChatActionRequest, db: AsyncSession = Depends(get_db)):
    """
    Strict allowlist router for client-side WebGPU chat assistant.
    Prevents execution of any arbitrary commands or unauthorized endpoints.
    Publishing can NEVER be invoked through this endpoint.
    """
    if req.action not in ALLOWED_CHAT_ACTIONS:
        raise HTTPException(
            status_code=403,
            detail=f"Action '{req.action}' is forbidden. Allowed actions: {sorted(list(ALLOWED_CHAT_ACTIONS))}"
        )

    # 1. create_job
    if req.action == "create_job":
        subject_name = req.params.get("subject_name", "").strip()
        work_date = req.params.get("work_date", "").strip()
        if not subject_name:
            raise HTTPException(status_code=400, detail="Missing required 'subject_name' in params.")
        if not work_date:
            from datetime import date
            work_date = date.today().isoformat()

        job_req = CreateJobRequest(
            workflow_name=req.params.get("workflow_name", "worldheroes"),
            content_type=req.params.get("content_type", "Hero"),
            subject_name=subject_name,
            work_date=work_date,
            notes=req.params.get("notes", "")
        )
        return await create_job(job_req, db)

    # 2. get_job_status
    elif req.action == "get_job_status":
        job_id = req.params.get("job_id")
        if not job_id:
            raise HTTPException(status_code=400, detail="Missing required 'job_id' in params.")
        stmt = select(Job).where(Job.id == int(job_id))
        job = (await db.execute(stmt)).scalar_one_or_none()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found.")
        return {
            "id": job.id,
            "subject_name": job.subject_name,
            "status": job.status,
            "error_message": job.error_message,
            "summary": job.get_review().get("summary", "")[:200],
            "published_url": job.published_url
        }

    # 3. list_history
    elif req.action == "list_history":
        limit = min(int(req.params.get("limit", 5)), 10)
        stmt = select(Job).order_by(desc(Job.id)).limit(limit)
        jobs = (await db.execute(stmt)).scalars().all()
        return [
            {
                "id": j.id,
                "subject_name": j.subject_name,
                "status": j.status,
                "work_date": j.work_date,
                "published_url": j.published_url
            }
            for j in jobs
        ]

    # 4. update_review_field
    elif req.action == "update_review_field":
        job_id = req.params.get("job_id")
        field = req.params.get("field")
        value = req.params.get("value")
        if not job_id or not field:
            raise HTTPException(status_code=400, detail="Missing 'job_id' or 'field' in params.")

        stmt = select(Job).where(Job.id == int(job_id))
        job = (await db.execute(stmt)).scalar_one_or_none()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found.")

        review = job.get_review()
        # Security: sanitize field names to alphanumeric only
        safe_field = StorageService.sanitize_slug(field).replace("-", "_")
        review[safe_field] = str(value)
        job.set_review(review)
        await db.commit()

        return {
            "success": True,
            "job_id": job.id,
            "field": safe_field,
            "value": value
        }

import os
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from app.database import get_db
from app.models.job import Job, JobLog, AppSetting
from app.config import settings
from app.services.storage import StorageService
from app.services.images import WikimediaCommonsProvider
from app.services.queue import job_queue

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

class CreateJobRequest(BaseModel):
    workflow_name: str = "worldheroes"
    content_type: str = "Hero"
    subject_name: str
    work_date: str
    notes: Optional[str] = ""

class UpdateReviewRequest(BaseModel):
    review_data: dict

class SelectImageRequest(BaseModel):
    image_index: int

async def get_active_storage_dir(db: AsyncSession) -> Path:
    stmt = select(AppSetting).where(AppSetting.key == "user_data_dir")
    res = await db.execute(stmt)
    setting = res.scalar_one_or_none()
    if setting and setting.value:
        return Path(setting.value).resolve()
    return settings.DEFAULT_DATA_DIR.resolve()

@router.post("")
async def create_job(req: CreateJobRequest, db: AsyncSession = Depends(get_db)):
    slug = StorageService.sanitize_slug(req.subject_name)
    
    # Duplicate check: same subject slug and work date
    stmt = select(Job).where(
        Job.person_slug == slug,
        Job.work_date == req.work_date,
        Job.status != "ARCHIVED"
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing:
        return {
            "duplicate": True,
            "job_id": existing.id,
            "message": f"A job for '{req.subject_name}' on date {req.work_date} already exists (Job #{existing.id})."
        }

    job = Job(
        workflow_name=req.workflow_name,
        content_type=req.content_type,
        subject_name=req.subject_name.strip(),
        person_slug=slug,
        work_date=req.work_date,
        notes=req.notes,
        status="DRAFT"
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    # Automatically prepare research in background or synchronous
    try:
        await job_queue.prepare_job(job.id)
        await db.refresh(job)
    except Exception as e:
        # Preparation error recorded in job record
        pass

    return {
        "success": True,
        "job_id": job.id,
        "status": job.status,
        "person_slug": job.person_slug
    }

@router.get("")
async def list_jobs(db: AsyncSession = Depends(get_db)):
    stmt = select(Job).order_by(desc(Job.id))
    jobs = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": j.id,
            "workflow_name": j.workflow_name,
            "content_type": j.content_type,
            "subject_name": j.subject_name,
            "person_slug": j.person_slug,
            "work_date": j.work_date,
            "status": j.status,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "selected_image_path": j.selected_image_path,
            "published_url": j.published_url
        }
        for j in jobs
    ]

@router.get("/{job_id}")
async def get_job_detail(job_id: int, db: AsyncSession = Depends(get_db)):
    stmt = select(Job).where(Job.id == job_id)
    job = (await db.execute(stmt)).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    log_stmt = select(JobLog).where(JobLog.job_id == job_id).order_by(JobLog.id)
    logs = (await db.execute(log_stmt)).scalars().all()

    return {
        "id": job.id,
        "workflow_name": job.workflow_name,
        "content_type": job.content_type,
        "subject_name": job.subject_name,
        "person_slug": job.person_slug,
        "work_date": job.work_date,
        "notes": job.notes,
        "status": job.status,
        "research": job.get_research(),
        "candidate_images": job.get_candidate_images(),
        "review": job.get_review(),
        "selected_image_index": job.selected_image_index,
        "selected_image_path": job.selected_image_path,
        "selected_image_sidecar": job.selected_image_sidecar,
        "dry_run_screenshot": job.dry_run_screenshot,
        "published_url": job.published_url,
        "error_message": job.error_message,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "logs": [
            {
                "id": l.id,
                "step_name": l.step_name,
                "level": l.level,
                "message": l.message,
                "screenshot_path": l.screenshot_path,
                "timestamp": l.timestamp.isoformat() if l.timestamp else None
            }
            for l in logs
        ]
    }

@router.post("/{job_id}/prepare")
async def trigger_prepare(job_id: int):
    try:
        job = await job_queue.prepare_job(job_id)
        return {"success": True, "status": job.status}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/{job_id}/review")
async def save_review(job_id: int, req: UpdateReviewRequest, db: AsyncSession = Depends(get_db)):
    stmt = select(Job).where(Job.id == job_id)
    job = (await db.execute(stmt)).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    current_review = job.get_review()
    updated = {**current_review, **req.review_data}
    if not updated.get("selected_image_path") and job.selected_image_path:
        updated["selected_image_path"] = job.selected_image_path
    elif updated.get("selected_image_path"):
        job.selected_image_path = updated["selected_image_path"]

    job.set_review(updated)
    await db.commit()
    return {"success": True, "message": "Review fields updated.", "selected_image_path": job.selected_image_path}

@router.post("/{job_id}/select-image")
async def select_candidate_image(job_id: int, req: SelectImageRequest, db: AsyncSession = Depends(get_db)):
    stmt = select(Job).where(Job.id == job_id)
    job = (await db.execute(stmt)).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    candidates = job.get_candidate_images()
    if req.image_index < 0 or req.image_index >= len(candidates):
        raise HTTPException(status_code=400, detail="Invalid candidate image index")

    chosen = candidates[req.image_index]
    image_url = chosen.get("url")
    if not image_url:
        raise HTTPException(status_code=400, detail="Candidate image has no valid download URL")

    # Safe streaming download up to 10 MB cap
    provider = WikimediaCommonsProvider()
    try:
        raw_bytes = await provider.download_image_bytes(image_url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Image download failed: {str(e)}")

    # Save to user storage with sidecar JSON
    storage_dir = await get_active_storage_dir(db)
    storage = StorageService(root_dir=storage_dir)

    ext = chosen.get("mime", "image/jpeg").split("/")[-1].replace("jpeg", "jpg")
    filename = f"hero_photo.{ext}"

    meta = {
        "source_url": chosen.get("url", ""),
        "license": chosen.get("license", "Unknown"),
        "author": chosen.get("author", "Unknown"),
        "attribution": chosen.get("attribution", ""),
        "page_url": chosen.get("description_url", "")
    }

    try:
        img_path, sidecar_path = storage.save_image_with_metadata(
            person_slug=job.person_slug,
            filename=filename,
            image_data=raw_bytes,
            metadata=meta,
            base_dir=storage_dir
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Image saving failed: {str(e)}")

    job.selected_image_index = req.image_index
    job.selected_image_path = str(img_path)
    job.selected_image_sidecar = str(sidecar_path)
    
    # Update review data with image attribution
    review = job.get_review()
    review["selected_image_path"] = str(img_path)
    job.set_review(review)

    await db.commit()
    return {
        "success": True,
        "image_path": str(img_path),
        "sidecar_path": str(sidecar_path),
        "attribution": chosen.get("attribution")
    }

@router.post("/{job_id}/upload-image")
async def upload_custom_image(
    job_id: int,
    file: UploadFile = File(...),
    license_type: str = Form("User Provided / Licensed"),
    author: str = Form("User Upload"),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(Job).where(Job.id == job_id)
    job = (await db.execute(stmt)).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    content = await file.read()
    storage_dir = await get_active_storage_dir(db)
    storage = StorageService(root_dir=storage_dir)

    meta = {
        "source_url": "local_upload",
        "license": license_type,
        "author": author,
        "attribution": f"Uploaded by user ({license_type})"
    }

    try:
        img_path, sidecar_path = storage.save_image_with_metadata(
            person_slug=job.person_slug,
            filename=file.filename or "upload.jpg",
            image_data=content,
            metadata=meta,
            base_dir=storage_dir
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Upload rejected: {str(e)}")

    job.selected_image_index = -1  # indicates custom upload
    job.selected_image_path = str(img_path)
    job.selected_image_sidecar = str(sidecar_path)
    
    review = job.get_review()
    review["selected_image_path"] = str(img_path)
    job.set_review(review)

    await db.commit()
    return {
        "success": True,
        "image_path": str(img_path),
        "sidecar_path": str(sidecar_path)
    }

@router.post("/{job_id}/dry-run")
async def run_dry_run(job_id: int):
    try:
        result = await job_queue.run_dry_run(job_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/{job_id}/publish")
async def approve_and_publish(job_id: int):
    try:
        result = await job_queue.run_publish(job_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/{job_id}/archive")
async def archive_job(job_id: int, db: AsyncSession = Depends(get_db)):
    stmt = select(Job).where(Job.id == job_id)
    job = (await db.execute(stmt)).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.status = "ARCHIVED"
    await db.commit()
    return {"success": True, "message": "Job archived successfully."}

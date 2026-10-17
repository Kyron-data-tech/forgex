"""
ForgeX — Runs & Agent Dashboard API Router
==========================================
Provides:
  - POST /api/runs: Spawns asynchronous runner subprocess and returns immediately
  - GET /api/runs/{id}: Detailed run metadata and step list
  - GET /api/runs/{id}/events: Server-Sent Events (SSE) streaming steps, status, and live frames
  - POST /api/runs/{id}/[pause|resume|stop|approve|reject]: Run controls
  - POST /api/runs/batch: Ingests tabular CSV/XLSX and creates sequential runs
"""

from __future__ import annotations

import sys
import json
import base64
import asyncio
import subprocess
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from pydantic import BaseModel

from app.config import settings, PROJECT_ROOT
from app.database import get_db
from app.models.job import Run, RunStep
from app.services.data_ingest import DataIngestService

logger = logging.getLogger("forgex.api_runs")
router = APIRouter(prefix="/api/runs", tags=["Runs"])


class CreateRunRequest(BaseModel):
    task: Optional[str] = None
    subject_name: Optional[str] = None
    work_date: Optional[str] = None
    content_type: str = "Hero"
    workflow_name: str = "worldheroes"
    auto_publish: bool = False


class BatchRunRequest(BaseModel):
    file_path: str
    column_mapping: Dict[str, str]
    sheet_name: Optional[str] = None


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_run(req: CreateRunRequest, db: AsyncSession = Depends(get_db)):
    """
    Creates a new Run in database, spawns detached runner worker subprocess,
    and returns immediately without blocking.
    """
    subject = (req.subject_name or "").strip()
    date = (req.work_date or "").strip()
    content_type = (req.content_type or "Hero").strip()
    is_admin = getattr(settings, "WH_ADMIN_MODE", True)
    action_verb = "directly publish" if is_admin else "nominate"

    # Concurrency safety: mark any previous lingering active runs as STOPPED
    old_runs_stmt = select(Run).where(
        Run.status.in_(["RUNNING", "STARTING", "RESEARCHING", "SYNTHESIZING", "FILLING_FORM", "WAITING_APPROVAL", "WAITING_USER"])
    )
    lingering_runs = (await db.execute(old_runs_stmt)).scalars().all()
    for lr in lingering_runs:
        lr.status = "STOPPED"
        lr.command = "STOP"

    if req.task:
        task_str = req.task
    elif subject:
        task_str = f"Research and {action_verb} {content_type}: {subject}"
    elif date:
        task_str = f"Discover {content_type} for date {date} and {action_verb}"
    else:
        task_str = f"Discover and {action_verb} historic {content_type}"

    run = Run(
        task=task_str,
        subject_name=subject,
        work_date=date,
        content_type=content_type,
        workflow_name=req.workflow_name,
        status="QUEUED",
        created_at=datetime.utcnow(),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    # Spawn subprocess worker — visible console on Windows so Chrome is interactive
    cmd = [sys.executable, "-m", "app.agent.runner", "--run-id", str(run.id)]
    if req.auto_publish:
        cmd.append("--auto-publish")
    try:
        import os
        from app.config import get_admin_credentials
        env = os.environ.copy()
        # Ensure .env values propagate even if uvicorn didn't load them
        env.setdefault("BROWSER_HEADLESS", "false")
        env.setdefault("BROWSER_CHANNEL", "chrome")
        env.setdefault("CURSOR_ENABLED", "true")

        # Always push fresh admin credentials from SQLite into subprocess env
        # This ensures the runner has credentials even if os.environ wasn't updated
        try:
            admin_u, admin_p, admin_mode = get_admin_credentials()
            if admin_u:
                env["WH_ADMIN_USERNAME"] = admin_u
            if admin_p:
                env["WH_ADMIN_PASSWORD"] = admin_p
            env["WH_ADMIN_MODE"] = "true" if admin_mode else "false"
        except Exception as cred_err:
            logger.warning("Could not load admin credentials for subprocess env: %s", cred_err)


        if sys.platform == "win32":
            # CREATE_NEW_CONSOLE (0x10) opens a visible cmd window on Windows;
            # this keeps Chrome in the foreground of the new console process group.
            CREATE_NEW_CONSOLE = 0x00000010
            proc = subprocess.Popen(
                cmd,
                cwd=str(PROJECT_ROOT),
                env=env,
                creationflags=CREATE_NEW_CONSOLE,
            )
        else:
            proc = subprocess.Popen(
                cmd,
                cwd=str(PROJECT_ROOT),
                env=env,
                start_new_session=True,
            )
        logger.info("Spawned runner subprocess PID %s for Run %d", proc.pid, run.id)
    except Exception as exc:
        run.status = "FAILED"
        run.error = f"Failed to spawn runner process: {exc}"
        await db.commit()
        raise HTTPException(status_code=500, detail=str(exc))

    return {"run_id": run.id, "status": run.status, "subject_name": subject}


@router.get("/{run_id}")
async def get_run_details(run_id: int, db: AsyncSession = Depends(get_db)):
    """Fetches Run details and all executed steps."""
    res = await db.execute(select(Run).where(Run.id == run_id))
    run = res.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found.")

    steps_res = await db.execute(select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.step_no))
    steps = steps_res.scalars().all()

    return {
        "id": run.id,
        "task": run.task,
        "subject_name": run.subject_name,
        "work_date": run.work_date,
        "content_type": getattr(run, "content_type", "Hero") or "Hero",
        "workflow_name": run.workflow_name,
        "status": run.status,
        "result": run.get_result(),
        "error": run.error,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "steps": [
            {
                "step_no": s.step_no,
                "tab_id": s.tab_id,
                "thought": s.thought,
                "action": s.action,
                "args": s.get_args(),
                "url": s.url,
                "screenshot_path": s.screenshot_path,
                "status": s.status,
                "timestamp": s.timestamp.isoformat(),
            }
            for s in steps
        ],
    }


@router.get("/{run_id}/events")
async def stream_run_events(run_id: int, db: AsyncSession = Depends(get_db)):
    """
    Server-Sent Events (SSE) streaming live steps, status changes,
    and latest screencast frames every ~700 ms.
    """
    async def event_generator():
        last_step_no = 0
        last_status = None

        while True:
            # Query current run state
            r_res = await db.execute(select(Run).where(Run.id == run_id))
            run = r_res.scalar_one_or_none()
            if not run:
                yield f"event: error\ndata: {json.dumps({'error': 'Run not found'})}\n\n"
                break

            # Send state change
            if run.status != last_status:
                last_status = run.status
                state_data = {
                    "run_id": run.id,
                    "status": run.status,
                    "result": run.get_result(),
                    "error": run.error,
                }
                yield f"event: state\ndata: {json.dumps(state_data)}\n\n"

            # Query new steps
            s_res = await db.execute(
                select(RunStep)
                .where(RunStep.run_id == run_id, RunStep.step_no > last_step_no)
                .order_by(RunStep.step_no)
            )
            new_steps = s_res.scalars().all()

            for step in new_steps:
                last_step_no = step.step_no
                step_data = {
                    "step_no": step.step_no,
                    "tab_id": step.tab_id,
                    "thought": step.thought,
                    "action": step.action,
                    "args": step.get_args(),
                    "url": step.url,
                    "screenshot_path": step.screenshot_path,
                    "status": step.status,
                    "timestamp": step.timestamp.isoformat(),
                }
                yield f"event: step\ndata: {json.dumps(step_data)}\n\n"

            # Broadcast latest screenshot frame for live view
            screenshots_dir = settings.DEFAULT_DATA_DIR / "screenshots" / f"run_{run_id}"
            if screenshots_dir.exists():
                shots = sorted(screenshots_dir.glob("*.png"), key=lambda p: p.stat().st_mtime)
                if shots:
                    latest = shots[-1]
                    rel_url = f"/data/screenshots/run_{run_id}/{latest.name}"
                    yield f"event: frame\ndata: {json.dumps({'url': rel_url, 'timestamp': datetime.utcnow().isoformat()})}\n\n"

            # If finished, stop stream after sending completion
            if run.status in ("COMPLETED", "FAILED", "STOPPED", "REJECTED"):
                yield f"event: end\ndata: {json.dumps({'status': run.status})}\n\n"
                break

            await asyncio.sleep(0.7)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─────────────────────────────────────────────────────────────────────────
# Control Endpoints
# ─────────────────────────────────────────────────────────────────────────

@router.post("/{run_id}/pause")
async def pause_run(run_id: int, db: AsyncSession = Depends(get_db)):
    await db.execute(update(Run).where(Run.id == run_id).values(command="PAUSE"))
    await db.commit()
    return {"status": "PAUSE command sent"}


@router.post("/{run_id}/resume")
async def resume_run(run_id: int, db: AsyncSession = Depends(get_db)):
    await db.execute(update(Run).where(Run.id == run_id).values(command="RESUME"))
    await db.commit()
    return {"status": "RESUME command sent"}


@router.post("/{run_id}/stop")
async def stop_run(run_id: int, db: AsyncSession = Depends(get_db)):
    await db.execute(update(Run).where(Run.id == run_id).values(command="STOP", status="STOPPED"))
    await db.commit()
    return {"status": "STOP command sent"}


@router.post("/{run_id}/synthesize")
async def synthesize_run(run_id: int, db: AsyncSession = Depends(get_db)):
    """Triggers immediate local field synthesis fallback without waiting for ChatGPT web login."""
    await db.execute(update(Run).where(Run.id == run_id).values(command="SYNTHESIZE"))
    await db.commit()
    return {"status": "SYNTHESIZE command sent"}


@router.post("/{run_id}/approve")
async def approve_run(run_id: int, payload: Optional[Dict[str, Any]] = None, db: AsyncSession = Depends(get_db)):
    """Explicit human approval enabling final form submission."""
    run_res = await db.execute(select(Run).where(Run.id == run_id))
    run = run_res.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found.")

    if payload:
        res = run.get_result()
        res.update(payload)
        run.set_result(res)

    run.command = "APPROVE"
    run.status = "APPROVED"
    await db.commit()
    return {"status": "APPROVED command sent"}


@router.post("/{run_id}/reject")
async def reject_run(run_id: int, db: AsyncSession = Depends(get_db)):
    await db.execute(update(Run).where(Run.id == run_id).values(command="REJECT", status="REJECTED"))
    await db.commit()
    return {"status": "REJECT command sent"}


# ─────────────────────────────────────────────────────────────────────────
# Batch Ingestion & Sequential Execution
# ─────────────────────────────────────────────────────────────────────────

@router.post("/batch")
async def create_batch_runs(req: BatchRunRequest, db: AsyncSession = Depends(get_db)):
    """
    Parses CSV/XLSX and creates sequential Run records.
    """
    path = Path(req.file_path).resolve()
    parsed = DataIngestService.parse_and_validate(
        file_path=path,
        column_mapping=req.column_mapping,
        sheet_name=req.sheet_name,
    )

    created_runs = []
    for r in parsed["valid_rows"]:
        run = Run(
            task=f"Research and nominate {r['subject_name']}",
            subject_name=r["subject_name"],
            work_date=r.get("work_date", ""),
            status="QUEUED",
            created_at=datetime.utcnow(),
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        created_runs.append({"run_id": run.id, "subject_name": r["subject_name"]})

    return {
        "total_rows": parsed["total_rows"],
        "valid_count": parsed["valid_count"],
        "error_count": parsed["error_count"],
        "errors": parsed["errors"],
        "created_runs": created_runs,
    }

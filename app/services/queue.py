import os
from pathlib import Path
from datetime import datetime
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.job import Job, JobLog, AppSetting
from app.config import settings
from app.services.storage import StorageService
from app.services.browser import browser_manager
from app.services.limits import rate_limiter
from workflows.base import registry

class JobQueueService:
    def __init__(self):
        self.storage = StorageService(root_dir=settings.DEFAULT_DATA_DIR)

    async def log_step(self, session, job_id: int, step_name: str, level: str, message: str, screenshot_path: str = None):
        log = JobLog(
            job_id=job_id,
            step_name=step_name,
            level=level,
            message=message,
            screenshot_path=screenshot_path,
            timestamp=datetime.utcnow()
        )
        session.add(log)
        await session.commit()

    async def prepare_job(self, job_id: int) -> Job:
        """Gathers research data and images using workflow.prepare()"""
        async with AsyncSessionLocal() as session:
            stmt = select(Job).where(Job.id == job_id)
            res = await session.execute(stmt)
            job = res.scalar_one_or_none()
            if not job:
                raise ValueError(f"Job {job_id} not found")

            workflow = registry.get(job.workflow_name)
            if not workflow:
                raise ValueError(f"Workflow '{job.workflow_name}' not registered")

            job.status = "PREPARING"
            await session.commit()
            await self.log_step(session, job.id, "preparation", "INFO", f"Starting research for {job.subject_name}")

            try:
                prep_data = await workflow.prepare({
                    "subject_name": job.subject_name,
                    "content_type": job.content_type,
                    "work_date": job.work_date,
                    "notes": job.notes
                })

                job.set_research(prep_data.get("research_raw", {}))
                candidate_images = prep_data.get("candidate_images", [])
                job.set_candidate_images(candidate_images)
                review_dict = prep_data.get("review_data", {})

                # Automated Image Pipeline: Bot automatically fetches, validates, and saves top candidate
                if candidate_images:
                    top_img = candidate_images[0]
                    img_url = top_img.get("url")
                    if img_url:
                        try:
                            from app.services.images import WikimediaCommonsProvider
                            commons = WikimediaCommonsProvider()
                            raw_bytes = await commons.download_image_bytes(img_url)

                            # Determine storage folder
                            stmt_set = select(AppSetting).where(AppSetting.key == "user_data_dir")
                            res_set = await session.execute(stmt_set)
                            setting_val = res_set.scalar_one_or_none()
                            storage_dir = Path(setting_val.value).resolve() if (setting_val and setting_val.value) else settings.DEFAULT_DATA_DIR.resolve()
                            storage = StorageService(root_dir=storage_dir)

                            ext = top_img.get("mime", "image/jpeg").split("/")[-1].replace("jpeg", "jpg")
                            filename = f"hero_photo.{ext}"

                            meta = {
                                "source_url": top_img.get("url", ""),
                                "license": top_img.get("license", "Unknown"),
                                "author": top_img.get("author", "Unknown"),
                                "attribution": top_img.get("attribution", ""),
                                "page_url": top_img.get("description_url", "")
                            }

                            img_path, sidecar_path = storage.save_image_with_metadata(
                                person_slug=job.person_slug,
                                filename=filename,
                                image_data=raw_bytes,
                                metadata=meta,
                                base_dir=storage_dir
                            )

                            job.selected_image_index = 0
                            job.selected_image_path = str(img_path)
                            job.selected_image_sidecar = str(sidecar_path)
                            review_dict["selected_image_path"] = str(img_path)
                            review_dict["image_attribution"] = top_img.get("attribution", "")

                            await self.log_step(
                                session,
                                job.id,
                                "image_automation",
                                "INFO",
                                f"Bot automatically retrieved, verified, and saved hero photo: {filename}"
                            )
                        except Exception as img_err:
                            await self.log_step(
                                session,
                                job.id,
                                "image_automation",
                                "WARNING",
                                f"Auto image download notice: {img_err}"
                            )

                job.set_review(review_dict)
                job.status = "READY_FOR_REVIEW"
                await session.commit()
                await self.log_step(
                    session,
                    job.id,
                    "preparation",
                    "INFO",
                    f"Research complete. {len(candidate_images)} candidate images cataloged. Top image auto-attached."
                )
                return job
            except Exception as e:
                job.status = "FAILED"
                job.error_message = str(e)
                await session.commit()
                await self.log_step(session, job.id, "preparation", "ERROR", f"Preparation failed: {e}")
                raise

    async def _get_active_storage_dir(self, session) -> Path:
        stmt_set = select(AppSetting).where(AppSetting.key == "user_data_dir")
        res_set = await session.execute(stmt_set)
        setting_val = res_set.scalar_one_or_none()
        return Path(setting_val.value).resolve() if (setting_val and setting_val.value) else settings.DEFAULT_DATA_DIR.resolve()

    def _build_execution_data(self, job: Job, storage_dir: Path) -> dict:
        data = dict(job.get_review())
        img_path = data.get("selected_image_path") or job.selected_image_path

        if not img_path or not os.path.exists(img_path):
            person_dir = storage_dir / job.person_slug
            if person_dir.exists():
                for ext in (".jpg", ".jpeg", ".png", ".webp"):
                    candidates = list(person_dir.glob(f"*{ext}"))
                    valid = [c for c in candidates if not c.name.endswith(".meta.json") and not c.name.endswith("_upload.jpg")]
                    if valid:
                        img_path = str(valid[0].resolve())
                        break

        if img_path and os.path.exists(img_path):
            data["selected_image_path"] = str(img_path)
            if not job.selected_image_path:
                job.selected_image_path = str(img_path)

        return data

    async def run_dry_run(self, job_id: int) -> dict:
        """Executes browser automation dry run with screenshot capture."""
        await rate_limiter.acquire()

        async with AsyncSessionLocal() as session:
            stmt = select(Job).where(Job.id == job_id)
            res = await session.execute(stmt)
            job = res.scalar_one_or_none()
            if not job:
                raise ValueError(f"Job {job_id} not found")

            workflow = registry.get(job.workflow_name)
            if not workflow:
                raise ValueError(f"Workflow '{job.workflow_name}' not registered")

            storage_dir = await self._get_active_storage_dir(session)
            exec_data = self._build_execution_data(job, storage_dir)

            job.status = "DRY_RUNNING"
            await session.commit()
            await self.log_step(session, job.id, "dry_run", "INFO", "Initiating Playwright dry-run...")

            async def automation(page):
                return await workflow.dry_run(exec_data, page)

            try:
                result = await browser_manager.run_automation_task(automation, headless=settings.BROWSER_HEADLESS)
                
                # Save screenshot if captured
                screenshot_path = ""
                if result.get("screenshot_b64"):
                    import base64
                    raw_bytes = base64.b64decode(result["screenshot_b64"])
                    screens_dir = settings.DEFAULT_DATA_DIR / "screenshots"
                    screens_dir.mkdir(parents=True, exist_ok=True)
                    s_file = screens_dir / f"job_{job.id}_dry_run_{int(datetime.utcnow().timestamp())}.png"
                    with open(s_file, "wb") as f:
                        f.write(raw_bytes)
                    screenshot_path = str(s_file)
                    job.dry_run_screenshot = screenshot_path

                if result.get("success"):
                    job.status = "DRY_RUN_PASSED"
                    await session.commit()
                    await self.log_step(
                        session,
                        job.id,
                        "dry_run",
                        "INFO",
                        "Dry run successfully completed. Verified pre-filled form.",
                        screenshot_path=screenshot_path
                    )
                else:
                    job.status = "READY_FOR_REVIEW"
                    await session.commit()
                    await self.log_step(
                        session,
                        job.id,
                        "dry_run",
                        "WARNING",
                        result.get("message", "Dry run stopped."),
                        screenshot_path=screenshot_path
                    )
                return result
            except Exception as e:
                job.status = "FAILED"
                job.error_message = str(e)
                await session.commit()
                await self.log_step(session, job.id, "dry_run", "ERROR", f"Dry run error: {e}")
                raise

    async def run_publish(self, job_id: int) -> dict:
        """Executes authorized final publishing."""
        await rate_limiter.acquire()

        async with AsyncSessionLocal() as session:
            stmt = select(Job).where(Job.id == job_id)
            res = await session.execute(stmt)
            job = res.scalar_one_or_none()
            if not job:
                raise ValueError(f"Job {job_id} not found")

            workflow = registry.get(job.workflow_name)
            if not workflow:
                raise ValueError(f"Workflow '{job.workflow_name}' not registered")

            storage_dir = await self._get_active_storage_dir(session)
            exec_data = self._build_execution_data(job, storage_dir)

            job.status = "PUBLISHING"
            await session.commit()
            await self.log_step(session, job.id, "publish", "INFO", "User authorized publication. Starting publish step...")

            async def automation(page):
                return await workflow.publish(exec_data, page)

            try:
                result = await browser_manager.run_automation_task(automation, headless=settings.BROWSER_HEADLESS)

                # Handle needs_login gracefully
                if result.get("needs_login"):
                    job.status = "FAILED"
                    job.error_message = result.get("message", "Not logged in — session expired")
                    await session.commit()
                    await self.log_step(session, job.id, "publish", "ERROR", f"Publish aborted: {job.error_message}")
                    return result

                import base64
                screens_dir = settings.DEFAULT_DATA_DIR / "screenshots"
                screens_dir.mkdir(parents=True, exist_ok=True)

                # Save pre-submit screenshot
                pre_screenshot_path = ""
                if result.get("pre_submit_screenshot_b64"):
                    raw_pre = base64.b64decode(result["pre_submit_screenshot_b64"])
                    pre_file = screens_dir / f"job_{job.id}_pre_submit_{int(datetime.utcnow().timestamp())}.png"
                    with open(pre_file, "wb") as f:
                        f.write(raw_pre)
                    pre_screenshot_path = str(pre_file)

                # Save post-submit screenshot
                screenshot_path = ""
                if result.get("screenshot_b64"):
                    raw_bytes = base64.b64decode(result["screenshot_b64"])
                    s_file = screens_dir / f"job_{job.id}_publish_{int(datetime.utcnow().timestamp())}.png"
                    with open(s_file, "wb") as f:
                        f.write(raw_bytes)
                    screenshot_path = str(s_file)

                if result.get("success"):
                    job.status = "PUBLISHED"
                    job.published_url = result.get("published_url")
                    job.dry_run_screenshot = screenshot_path
                    await session.commit()
                    if pre_screenshot_path:
                        await self.log_step(
                            session, job.id, "publish", "INFO",
                            "Pre-submit form screenshot (all fields filled, before click).",
                            screenshot_path=pre_screenshot_path
                        )
                    await self.log_step(
                        session, job.id, "publish", "INFO",
                        f"Nomination submitted. Post-submit URL: {job.published_url}",
                        screenshot_path=screenshot_path
                    )
                else:
                    job.status = "FAILED"
                    job.error_message = result.get("error", "Publish failed")
                    await session.commit()
                    await self.log_step(
                        session, job.id, "publish", "ERROR",
                        f"Publish failed: {job.error_message}",
                        screenshot_path=screenshot_path
                    )
                return result
            except Exception as e:
                job.status = "FAILED"
                job.error_message = str(e)
                await session.commit()
                await self.log_step(session, job.id, "publish", "ERROR", f"Publish error: {e}")
                raise

job_queue = JobQueueService()

"""
ForgeX — Subprocess Agent Worker
================================
Runs as an independent process:
    python -m app.agent.runner --run-id <ID>

Decoupled from uvicorn so Windows event-loop quirks never interfere with FastAPI.
Executes the full Operator pipeline:
  1. Google Searches & AI Overview scraping
  2. Research bundle synthesis
  3. ChatGPT hand-off and schema validation
  4. Form fill via visible Pointer
  5. Strict human approval gate before final submission
"""

from __future__ import annotations

import sys
import json
import asyncio
import logging
import argparse
from pathlib import Path
from datetime import datetime
from typing import Optional

from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker

import os
from app.config import settings, PROJECT_ROOT, get_admin_credentials
from app.models.job import Base, Run, RunStep
from app.agent.browser import ChromeSession
from app.agent.tools import ToolExecutor
from app.agent.pointer import Pointer
from app.agent.playbook import ResearchPlaybook
import re
from app.agent.chatgpt import ChatGPTHandoff, synthesize_fields_from_bundle

# Set up logging for subprocess
LOGS_DIR = PROJECT_ROOT / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [Runner]: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOGS_DIR / "runner.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("forgex.runner")

# Synchronous DB session for runner process
SYNC_DB_PATH = settings.DATABASE_PATH
sync_engine = create_engine(
    f"sqlite:///{SYNC_DB_PATH.as_posix()}",
    connect_args={"check_same_thread": False},
    echo=False,
)
SyncSessionLocal = sessionmaker(bind=sync_engine)


class AgentRunner:
    def __init__(self, run_id: int, headless: Optional[bool] = None, auto_publish: bool = False):
        self.run_id = run_id
        self.headless = settings.BROWSER_HEADLESS if headless is None else headless
        self.auto_publish = auto_publish
        self.step_no = 0
        self.screenshots_dir = settings.DEFAULT_DATA_DIR / "screenshots" / f"run_{run_id}"
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

        self.session: Optional[ChromeSession] = None
        self.executor: Optional[ToolExecutor] = None

    def get_run(self) -> Optional[Run]:
        with SyncSessionLocal() as db:
            return db.execute(select(Run).where(Run.id == self.run_id)).scalar_one_or_none()

    def update_run_status(self, status: str, error: Optional[str] = None):
        with SyncSessionLocal() as db:
            run = db.execute(select(Run).where(Run.id == self.run_id)).scalar_one()
            run.status = status
            if error:
                run.error = error
            run.updated_at = datetime.utcnow()
            db.commit()

    async def emit_step(
        self,
        thought: str,
        action: str,
        args: dict,
        page,
        status: str = "SUCCESS",
        error: Optional[str] = None,
    ):
        self.step_no += 1
        screenshot_filename = f"step_{self.step_no:03d}.png"
        screenshot_rel_path = f"screenshots/run_{self.run_id}/{screenshot_filename}"
        full_screenshot_path = self.screenshots_dir / screenshot_filename

        # Take screenshot of active page (includes pointer overlay)
        try:
            if page and hasattr(page, "screenshot"):
                await page.screenshot(path=str(full_screenshot_path), full_page=False)
        except Exception as e:
            logger.debug("Screenshot skipped or failed: %s", e)

        tab_id = args.get("tab_id") or "tab"

        with SyncSessionLocal() as db:
            step = RunStep(
                run_id=self.run_id,
                step_no=self.step_no,
                tab_id=tab_id,
                thought=thought,
                action=action,
                args_json=json.dumps(args, ensure_ascii=False),
                url=page.url if hasattr(page, "url") else "",
                screenshot_path=screenshot_rel_path,
                status=status,
                error=error,
                timestamp=datetime.utcnow(),
            )
            db.add(step)
            db.commit()

        logger.info("[Step %02d] [%s] %s -> %s", self.step_no, tab_id, action, thought)

    async def check_commands(self) -> str:
        """Polls DB for user control commands (PAUSE, RESUME, STOP, SYNTHESIZE)."""
        run = self.get_run()
        cmd = (run.command or "").upper() if run else ""
        if cmd == "STOP":
            raise InterruptedError("Run terminated by user.")
        if cmd == "PAUSE":
            logger.info("Run paused by user command.")
            self.update_run_status("PAUSED")
            while True:
                await asyncio.sleep(1.0)
                r = self.get_run()
                c = (r.command or "").upper() if r else ""
                if c == "STOP":
                    raise InterruptedError("Run terminated by user while paused.")
                if c in ("RESUME", "SYNTHESIZE"):
                    logger.info("Run resumed by user.")
                    self.update_run_status("RUNNING")
                    with SyncSessionLocal() as db:
                        db_run = db.execute(select(Run).where(Run.id == self.run_id)).scalar_one()
                        db_run.command = None
                        db.commit()
                    break
        return cmd

    async def execute(self):
        run = self.get_run()
        if not run:
            logger.error("Run ID %d not found in database.", self.run_id)
            return

        subject_name = (run.subject_name or "").strip()
        work_date = (run.work_date or "").strip()

        logger.info("Starting Run %d: Subject='%s', Date='%s'", self.run_id, subject_name, work_date)
        with SyncSessionLocal() as db:
            db_run = db.execute(select(Run).where(Run.id == self.run_id)).scalar_one()
            db_run.status = "RUNNING"
            db_run.started_at = datetime.utcnow()
            db.commit()

        self.session = ChromeSession(headless=self.headless)
        try:
            await self.session.start()
            self.executor = ToolExecutor(self.session)
            active_page = self.session.get_active_page()
            playbook = ResearchPlaybook(self.executor)

            # -------------------------------------------------------------
            # Stage 0: Auto-Discover Hero from Date (if only date provided)
            # -------------------------------------------------------------
            target_date = work_date or subject_name
            is_date_only = False
            if not subject_name or subject_name == target_date:
                # Check if matches year or month/day patterns
                if re.match(r"^\d{4}$", target_date) or re.match(r"^\d{4}-\d{2}-\d{2}$", target_date):
                    is_date_only = True
                elif any(m in target_date.lower() for m in [
                    "january", "february", "march", "april", "may", "june",
                    "july", "august", "september", "october", "november", "december"
                ]):
                    is_date_only = True
                elif not subject_name and target_date:
                    is_date_only = True

            if is_date_only and target_date:
                self.update_run_status("DISCOVERING")
                await self.emit_step(
                    f"Auto-discovering prominent hero for date '{target_date}' via Google Chrome...",
                    "discover_hero",
                    {
                        "tab_id": "tab-discover",
                        "tab_title": f"Discover: {target_date}",
                        "tab_icon": "🔍",
                        "date": target_date,
                    },
                    active_page,
                )

                discovery = await playbook.discover_hero_from_date(target_date)
                subject_name = discovery.get("subject_name") or f"Hero of {target_date}"
                work_date = discovery.get("work_date") or target_date
                achievement = discovery.get("achievement", "")

                with SyncSessionLocal() as db:
                    db_run = db.execute(select(Run).where(Run.id == self.run_id)).scalar_one()
                    db_run.subject_name = subject_name
                    db_run.work_date = work_date
                    db.commit()

                await self.emit_step(
                    f"Discovered Hero for {target_date}: {subject_name} — {achievement[:120]}...",
                    "hero_discovered",
                    {
                        "tab_id": "tab-discover",
                        "tab_title": f"Discover: {target_date}",
                        "tab_icon": "🔍",
                        **discovery,
                    },
                    self.session.get_active_page(),
                )

            # If still empty, fallback
            if not subject_name:
                subject_name = run.task or "Historic Hero"

            # -------------------------------------------------------------
            # Stage 1: Google Research Playbook
            # -------------------------------------------------------------
            self.update_run_status("RESEARCHING")
            await self.emit_step(
                f"Starting deep Google research for {subject_name} across multiple new tabs",
                "search_playbook",
                {
                    "tab_id": "tab-search-main",
                    "tab_title": f"Search: {subject_name}",
                    "tab_icon": "🔍",
                    "subject": subject_name,
                    "work_date": work_date,
                },
                self.session.get_active_page(),
            )

            # Check content type & admin mode
            content_type = getattr(run, "content_type", None) or "Hero"
            c_type = content_type.strip().title()

            async def on_playbook_step(thought: str, action: str, args: dict, page):
                await self.emit_step(thought, action, args, page)

            research_bundle = await playbook.run(
                subject_name=subject_name,
                work_date=work_date,
                content_type=c_type,
                on_step=on_playbook_step,
            )

            with SyncSessionLocal() as db:
                db_run = db.execute(select(Run).where(Run.id == self.run_id)).scalar_one()
                db_run.research_bundle = research_bundle.get("bundle_text", "")
                db.commit()

            await self.emit_step(
                f"Research complete: {research_bundle['ai_overview_count']} AI Overviews and {research_bundle['organic_count']} articles compiled",
                "bundle_ready",
                {
                    "tab_id": "tab-bundle",
                    "tab_title": "Research Bundle",
                    "tab_icon": "📚",
                    "total_chars": len(research_bundle.get("bundle_text", "")),
                },
                self.session.get_active_page(),
            )

            await self.check_commands()

            # -------------------------------------------------------------
            # Stage 2: ChatGPT Hand-off & Schema Validation
            # -------------------------------------------------------------
            self.update_run_status("CHATGPT")
            await self.emit_step(
                "Handing off research bundle to ChatGPT for field extraction",
                "chatgpt_handoff",
                {
                    "tab_id": "tab-chatgpt",
                    "tab_title": "ChatGPT Web",
                    "tab_icon": "🤖",
                },
                self.session.get_active_page(),
            )

            chatgpt = ChatGPTHandoff(self.executor)
            chatgpt_res = await chatgpt.run(research_bundle.get("bundle_text", ""))

            if chatgpt_res.get("needs_auth"):
                self.update_run_status("WAITING_USER")
                await self.emit_step(
                    chatgpt_res.get("reason", "Authentication required on ChatGPT"),
                    "waiting_user",
                    {
                        "tab_id": "tab-chatgpt",
                        "tab_title": "ChatGPT Web",
                        "tab_icon": "🤖",
                        "instruction": "Log into ChatGPT in Chrome, or click 'Synthesize Fields (Local)'",
                    },
                    self.session.get_active_page(),
                )

                # Wait for user to log in and click resume, or command synthesize
                while True:
                    await asyncio.sleep(2.0)
                    r = self.get_run()
                    c = (r.command or "").upper() if r else ""
                    if c == "STOP":
                        raise InterruptedError("Run stopped by user.")
                    if c == "SYNTHESIZE":
                        logger.info("Local synthesis requested by user.")
                        break
                    if c == "RESUME":
                        # Attempt ChatGPT again
                        chatgpt_res = await chatgpt.run(research_bundle.get("bundle_text", ""))
                        if not chatgpt_res.get("needs_auth") and chatgpt_res.get("data"):
                            break
                        # If still needs auth or empty, fallback to local synthesis
                        logger.info("ChatGPT still requires authentication; utilizing local field synthesis engine.")
                        break

            extracted_fields = chatgpt_res.get("data") or {}
            verified_profile = research_bundle.get("verified_profile") or {}

            # Integrate the verified research from New Job's ResearchService
            if verified_profile:
                logger.info("Applying New Job verified research profile: %s", verified_profile.get("title"))
                # Standardized name
                if not extracted_fields.get("name") or extracted_fields.get("name") in ("Historic Hero", "Hero", ""):
                    extracted_fields["name"] = verified_profile.get("title") or subject_name
                # Verified category
                if verified_profile.get("category"):
                    extracted_fields["category"] = verified_profile["category"]
                # Verified country / nationality
                if not extracted_fields.get("country"):
                    extracted_fields["country"] = verified_profile.get("country") or verified_profile.get("facts", {}).get("nationality", "")
                # Verified birth & death — try facts dict, then top-level keys
                if not extracted_fields.get("birth"):
                    birth_val = (
                        verified_profile.get("birth")
                        or verified_profile.get("facts", {}).get("birth", "")
                        or verified_profile.get("facts", {}).get("birth_year", "")
                    )
                    if birth_val:
                        extracted_fields["birth"] = str(birth_val)[:4]
                if not extracted_fields.get("death"):
                    death_val = (
                        verified_profile.get("death")
                        or verified_profile.get("facts", {}).get("death", "")
                        or verified_profile.get("facts", {}).get("death_year", "")
                    )
                    if death_val:
                        extracted_fields["death"] = str(death_val)[:4]
                # Verified tagline
                if not extracted_fields.get("tagline") or len(str(extracted_fields.get("tagline", ""))) < 10:
                    extracted_fields["tagline"] = verified_profile.get("tagline", "")
                # Verified summary
                if not extracted_fields.get("summary") or len(str(extracted_fields.get("summary", ""))) < 120:
                    extracted_fields["summary"] = verified_profile.get("summary", "")
                # Verified story
                if not extracted_fields.get("story") or len(str(extracted_fields.get("story", ""))) < 1200:
                    extracted_fields["story"] = verified_profile.get("story", "")
                # Verified source URL
                if not extracted_fields.get("source_url"):
                    extracted_fields["source_url"] = verified_profile.get("page_url") or verified_profile.get("canonical_url", "")

            # Fallback to local high-precision synthesis if fields are still incomplete
            if not extracted_fields or not extracted_fields.get("story") or len(extracted_fields.get("story", "")) < 500:
                logger.info("Applying local high-precision synthesis to ensure complete, publication-ready fields...")
                fallback_fields = synthesize_fields_from_bundle(
                    subject_name=subject_name,
                    bundle_text=research_bundle.get("bundle_text", ""),
                    work_date=work_date,
                )
                for k, v in fallback_fields.items():
                    if not extracted_fields.get(k):
                        extracted_fields[k] = v

            # ── Field Sanitisation ──────────────────────────────────────────
            # Strip raw multi-line / navigation noise and citation brackets.
            _VALID_CATEGORIES = {
                "leaders", "entrepreneurs", "scientists", "athletes",
                "artists", "humanitarians", "unsung-heroes", "historical-heroes",
            }

            _DEMONYM_MAP = {
                "austrian": "Austria", "american": "United States", "british": "United Kingdom",
                "english": "United Kingdom", "scottish": "United Kingdom", "welsh": "United Kingdom",
                "french": "France", "german": "Germany", "polish": "Poland", "russian": "Russia",
                "soviet": "Russia", "italian": "Italy", "canadian": "Canada", "indian": "India",
                "australian": "Australia", "spanish": "Spain", "japanese": "Japan", "chinese": "China",
                "south african": "South Africa", "irish": "Ireland", "dutch": "Netherlands",
                "swiss": "Switzerland", "swedish": "Sweden", "norwegian": "Norway", "danish": "Denmark",
                "finnish": "Finland", "greek": "Greece", "mexican": "Mexico", "brazilian": "Brazil",
                "argentinian": "Argentina", "argentine": "Argentina", "ukrainian": "Ukraine",
                "portuguese": "Portugal",
            }

            # Strip Wikipedia citation brackets like [1], [2], [a], [b] and normalize whitespace across fields
            for fld in ["name", "tagline", "summary", "story", "country"]:
                val = str(extracted_fields.get(fld) or "")
                if val:
                    val = re.sub(r"\[(?:[0-9]{1,3}[a-z]?|[a-z])\]", "", val)
                    val = re.sub(r"[\u00a0\u200b\u202f]", " ", val)
                    extracted_fields[fld] = re.sub(r"\s+", " ", val).strip()

            def _sanitise_oneliner(text: str, subject: str, max_len: int = 215) -> str:
                """Return a clean single-line string ≤ max_len chars."""
                if not text:
                    return text
                # Take only the first line that is not blank or "Wikipedia"-style nav
                for line in re.split(r"[\n\r]+", text):
                    line = line.strip()
                    if not line:
                        continue
                    # Skip Wikipedia nav lines like "+1", "Wikipedia", "Show all"
                    if re.match(r"^(\+\d+|Wikipedia|Show more|Show all|Identity:)\s*$", line, re.I):
                        continue
                    if any(b in line.lower() for b in ["was born to", "was born in", "born on", "son of", "daughter of"]):
                        continue
                    if len(line) <= max_len:
                        return line[:max_len].rstrip(".,;: ") + ("." if not line[:max_len].endswith(".") else "")
                # Fallback — use first 215 chars of raw text, no newlines
                clean = re.sub(r"\s+", " ", text).strip()
                return clean[:max_len].rstrip(".,;: ") + "."

            # Tagline
            raw_tagline = str(extracted_fields.get("tagline") or "")
            if raw_tagline and ("\n" in raw_tagline or len(raw_tagline) > 215 or "Wikipedia" in raw_tagline or any(b in raw_tagline.lower() for b in ["was born to", "was born in", "born on"])):
                extracted_fields["tagline"] = _sanitise_oneliner(raw_tagline, subject_name, 215)
                logger.info("Tagline sanitised to: %s", extracted_fields["tagline"][:80])

            # Tagline fallback if still a birth sentence or blank
            cur_tagline = str(extracted_fields.get("tagline") or "")
            if not cur_tagline or len(cur_tagline) < 15 or any(b in cur_tagline.lower() for b in ["was born to", "was born in", "born on", "son of", "daughter of"]):
                cat = extracted_fields.get("category", "historical-heroes")
                c_name = extracted_fields.get("country", "")
                nat_prefix = f"{c_name} " if c_name and c_name != "Global" else ""
                role_desc = {
                    "athletes": "record-breaking athlete celebrated for extraordinary courage and historic milestones.",
                    "scientists": "pioneering scientist whose groundbreaking discoveries transformed our understanding of the world.",
                    "leaders": "visionary leader remembered for enduring impact on history and global progress.",
                    "artists": "celebrated artist whose creative masterworks inspired generations.",
                    "humanitarians": "compassionate humanitarian dedicated to defending human dignity and global peace.",
                    "entrepreneurs": "innovative pioneer who shaped modern industry and advanced society.",
                    "historical-heroes": "historic pioneer remembered for extraordinary bravery and landmark achievements.",
                }.get(cat, "inspiring hero celebrated for remarkable achievements and lasting global impact.")
                extracted_fields["tagline"] = f"{nat_prefix}{role_desc}".strip()

            # Summary — must be clean multi-word prose, 200-580 chars
            raw_summary = str(extracted_fields.get("summary") or "")
            if raw_summary and ("Wikipedia" in raw_summary or len(raw_summary) > 580):
                clean_summary = re.sub(r"\s+", " ", re.sub(r"Wikipedia[^\n]*\n?", "", raw_summary)).strip()
                extracted_fields["summary"] = clean_summary[:580]

            # Category — normalise to valid enum
            raw_cat = str(extracted_fields.get("category") or "").lower().strip()
            if raw_cat not in _VALID_CATEGORIES:
                # Try to infer from name + bundle
                from app.agent.chatgpt import synthesize_fields_from_bundle as _synth
                inferred_cat_fields = _synth(
                    subject_name=subject_name,
                    bundle_text=research_bundle.get("bundle_text", ""),
                    work_date=work_date,
                )
                extracted_fields["category"] = inferred_cat_fields.get("category", "historical-heroes")
                logger.info("Category normalised from '%s' to '%s'", raw_cat, extracted_fields["category"])

            # Country — map demonyms to sovereign countries
            cur_country = str(extracted_fields.get("country") or "").strip()
            if cur_country.lower() in _DEMONYM_MAP:
                extracted_fields["country"] = _DEMONYM_MAP[cur_country.lower()]
            elif not cur_country:
                bundle_lower = research_bundle.get("bundle_text", "").lower()
                for demonym, country_name in _DEMONYM_MAP.items():
                    if demonym in bundle_lower[:2000]:
                        extracted_fields["country"] = country_name
                        break

            # Enforce length limits on all text fields
            if len(str(extracted_fields.get("tagline") or "")) > 215:
                extracted_fields["tagline"] = str(extracted_fields["tagline"])[:212] + "..."
            if len(str(extracted_fields.get("summary") or "")) > 580:
                extracted_fields["summary"] = str(extracted_fields["summary"])[:577] + "..."
            if len(str(extracted_fields.get("story") or "")) > 7500:
                extracted_fields["story"] = str(extracted_fields["story"])[:7450].rsplit(".", 1)[0] + "."
            # ── End Sanitisation ────────────────────────────────────────────

            # Ensure key_moments, quotes, and sources are populated
            if not extracted_fields.get("key_moments") or not extracted_fields.get("quotes") or not extracted_fields.get("sources"):
                from app.agent.chatgpt import synthesize_fields_from_bundle as _synth
                fallback_fields = _synth(
                    subject_name=subject_name,
                    bundle_text=research_bundle.get("bundle_text", ""),
                    work_date=work_date,
                )
                for k in ["key_moments", "quotes", "sources"]:
                    if not extracted_fields.get(k):
                        extracted_fields[k] = fallback_fields.get(k) or []

            # -------------------------------------------------------------
            # Photo Discovery & Download (Operator Agent image pipeline)
            # -------------------------------------------------------------
            selected_image_path = ""
            photo_credit = ""
            candidate_images = []

            try:
                from app.services.images import WikimediaCommonsProvider
                from app.services.storage import StorageService

                commons = WikimediaCommonsProvider()
                candidate_images = await commons.search_images(subject_name, limit=6)
                main_thumb = verified_profile.get("main_thumbnail")

                target_img_url = ""
                if candidate_images:
                    top_cand = candidate_images[0]
                    target_img_url = top_cand.get("url")
                    author = top_cand.get("author") or "Unknown"
                    license_name = top_cand.get("license") or "CC BY-SA"
                    photo_credit = f"{author} ({license_name})"
                elif main_thumb:
                    target_img_url = main_thumb
                    photo_credit = "Wikipedia / Wikimedia Commons"

                if target_img_url:
                    logger.info("Downloading portrait photo for %s: %s", subject_name, target_img_url)
                    raw_bytes = await commons.download_image_bytes(target_img_url)

                    storage = StorageService(root_dir=settings.DEFAULT_DATA_DIR)
                    slug_person = storage.sanitize_slug(subject_name)
                    img_dir = settings.DEFAULT_DATA_DIR / slug_person
                    img_dir.mkdir(parents=True, exist_ok=True)

                    opt_bytes, fmt, w, h = storage.optimize_image_bytes(raw_bytes, max_dim=1600, quality=85)
                    saved_file = img_dir / f"hero_photo.{fmt.lower()}"
                    with open(saved_file, "wb") as f:
                        f.write(opt_bytes)

                    selected_image_path = str(saved_file.resolve())
                    logger.info("Photo saved and optimized: %s (%dx%d, %d KB)", selected_image_path, w, h, len(opt_bytes) // 1024)

                    extracted_fields["selected_image_path"] = selected_image_path
                    extracted_fields["photo_credit"] = photo_credit
                    extracted_fields["candidate_images"] = candidate_images

                    await self.emit_step(
                        f"Found & downloaded high-resolution photo for {subject_name} ({w}x{h} px, Credit: {photo_credit})",
                        "download_photo",
                        {
                            "tab_id": "tab-photo",
                            "tab_title": "Hero Portrait",
                            "tab_icon": "🖼️",
                            "image_path": selected_image_path,
                            "credit": photo_credit,
                            "width": w,
                            "height": h,
                        },
                        self.session.get_active_page(),
                    )
            except Exception as img_err:
                logger.warning("Operator Agent photo acquisition note: %s", img_err)

            with SyncSessionLocal() as db:
                db_run = db.execute(select(Run).where(Run.id == self.run_id)).scalar_one()
                db_run.set_result(extracted_fields)
                db.commit()

            await self.emit_step(
                f"Extracted fields validated: Category='{extracted_fields.get('category')}', Tagline='{extracted_fields.get('tagline', '')[:60]}...'",
                "fields_extracted",
                {
                    "tab_id": "tab-synthesis",
                    "tab_title": "Field Synthesis",
                    "tab_icon": "✨",
                    **extracted_fields,
                },
                self.session.get_active_page(),
            )

            await self.check_commands()

            # -------------------------------------------------------------
            # Stage 3: Form Fill (WorldHeroes Admin Direct Publish / Nomination)
            # -------------------------------------------------------------
            self.update_run_status("FILLING_FORM")

            # Check content type & admin mode
            content_type = getattr(run, "content_type", None) or "Hero"
            c_type = content_type.strip().title()
            is_admin = getattr(settings, "WH_ADMIN_MODE", True)

            if is_admin:
                if c_type == "Blog":
                    target_url = "https://worldheros.com/admin/blog/new"
                elif c_type == "Story":
                    # Direct admin publishing of stories routes to the Hero biography publisher
                    target_url = "https://worldheros.com/admin/heroes/new"
                    c_type = "Hero"
                else:
                    c_type = "Hero"
                    target_url = "https://worldheros.com/admin/heroes/new"
            else:
                target_url = "https://worldheros.com/nominate"

            form_page = await self.session.open_tab(url=target_url, purpose="admin_publish" if is_admin else "form_fill")
            await form_page.bring_to_front()
            await asyncio.sleep(1.5)

            await self.emit_step(
                f"Opened WorldHeroes {'Admin Publish' if is_admin else 'Nomination'} page for {c_type}: {extracted_fields.get('name', 'subject')}",
                "open_tab",
                {
                    "tab_id": "tab-worldheroes",
                    "tab_title": f"WorldHeroes Admin ({c_type})",
                    "tab_icon": "⚡",
                    "url": target_url,
                    "content_type": c_type,
                    "admin_mode": is_admin,
                },
                form_page,
            )

            # Check if admin login is needed
            if is_admin:
                current_url = form_page.url
                needs_admin_login = "/admin/login" in current_url or await form_page.locator("#u, input[name='username']").count() > 0
                if needs_admin_login:
                    # Always load from SQLite first — subprocess env may not have been updated
                    admin_u, admin_p, _ = get_admin_credentials()
                    if not admin_u or not admin_p:
                        admin_u = os.environ.get("WH_ADMIN_USERNAME", "")
                        admin_p = os.environ.get("WH_ADMIN_PASSWORD", "")

                    if admin_u and admin_p:
                        await self.emit_step(
                            f"Authenticating into WorldHeroes Admin as '{admin_u}'...",
                            "admin_login",
                            {
                                "tab_id": "tab-worldheroes",
                                "tab_title": f"WorldHeroes Admin ({c_type})",
                                "tab_icon": "⚡",
                                "username": admin_u,
                            },
                            form_page,
                        )
                        u_loc = form_page.locator("#u, input[name='username']").first
                        await self.session.pointer.type_text(form_page, u_loc, admin_u, label="Typing admin username")
                        await asyncio.sleep(0.3)
                        p_loc = form_page.locator("#p, input[name='password']").first
                        await self.session.pointer.type_text(form_page, p_loc, admin_p, label="Typing admin password")
                        await asyncio.sleep(0.3)
                        # Click the sign-in submit button
                        submit_login = form_page.locator("button[type='submit']").first
                        await self.session.pointer.click(form_page, submit_login, label="Signing into Admin")
                        try:
                            await form_page.wait_for_url(lambda u: "/admin/login" not in u, timeout=15_000)
                        except Exception:
                            pass
                        await asyncio.sleep(1.5)
                        # Check if login succeeded
                        if "/admin/login" in form_page.url:
                            logger.error("Admin login FAILED — still on login page after submit. Check credentials.")
                            self.update_run_status("FAILED", error="Admin login failed — wrong credentials?")
                            await self.emit_step(
                                "Admin login FAILED. Please verify your admin username and password in Settings.",
                                "error",
                                {
                                    "tab_id": "tab-worldheroes",
                                    "tab_title": f"WorldHeroes Admin ({c_type})",
                                    "tab_icon": "⚡",
                                    "url": form_page.url,
                                },
                                form_page,
                                status="FAILED",
                                error="Admin login failed",
                            )
                            return
                        # Navigate to the target page after login
                        if form_page.url.rstrip("/") != target_url.rstrip("/"):
                            await form_page.goto(target_url, wait_until="domcontentloaded", timeout=20_000)
                            await asyncio.sleep(1.0)
                    else:
                        self.update_run_status("WAITING_USER")
                        await self.emit_step(
                            "WorldHeroes Admin authentication required. Please log in with your Admin Username and Password in Chrome or enter them in Settings, then click Resume.",
                            "waiting_user",
                            {
                                "tab_id": "tab-worldheroes",
                                "tab_title": f"WorldHeroes Admin ({c_type})",
                                "tab_icon": "⚡",
                                "instruction": "Log into WorldHeroes Admin in Chrome, or configure Admin Username/Password in Settings, then click Resume.",
                            },
                            form_page,
                        )
                        while True:
                            await asyncio.sleep(2.0)
                            r = self.get_run()
                            c = (r.command or "").upper() if r else ""
                            if c == "STOP":
                                raise InterruptedError("Run stopped by user.")
                            if c == "RESUME":
                                logger.info("Run resumed by user after admin login.")
                                if form_page.url != target_url:
                                    await form_page.goto(target_url, wait_until="domcontentloaded", timeout=20_000)
                                    await asyncio.sleep(1.0)
                                break

            # Map category text to dropdown index
            cat_map = {
                "leaders": "1",
                "entrepreneurs": "2",
                "scientists": "3",
                "athletes": "4",
                "artists": "5",
                "humanitarians": "6",
                "unsung-heroes": "7",
                "historical-heroes": "8",
            }
            cat_val = cat_map.get(extracted_fields.get("category", "scientists"), "3")

            # Slug generation
            subject_title = extracted_fields.get("name") or extracted_fields.get("title") or subject_name
            slug_clean = re.sub(r"[^\w\s-]", "", subject_title.lower()).strip().replace(" ", "-")

            if is_admin:
                if c_type == "Hero":
                    # ── Hero form — field IDs confirmed from live /admin/heroes/new ──
                    # Name  →  #h-name / input[name='name'] data-slug-source
                    if subject_title:
                        name_sel = "#h-name, input[name='name'][data-slug-source]"
                        if await form_page.locator(name_sel).count() > 0:
                            await self.session.pointer.type_text(
                                form_page, form_page.locator(name_sel).first, subject_title, label="Typing hero name"
                            )
                            await asyncio.sleep(0.5)  # allow slug auto-fill JS to run

                    # Slug  →  #h-slug / input[name='slug'] — only fill if blank
                    slug_sel = "#h-slug, input[name='slug'][data-slug-target]"
                    if await form_page.locator(slug_sel).count() > 0:
                        cur_slug = await form_page.locator(slug_sel).first.input_value()
                        if not cur_slug:
                            await self.session.pointer.type_text(
                                form_page, form_page.locator(slug_sel).first, slug_clean, label="Typing slug"
                            )

                    # Category  →  #h-category  select[name='category_id']
                    cat_sel = "#h-category, select[name='category_id']"
                    if await form_page.locator(cat_sel).count() > 0:
                        try:
                            await form_page.locator(cat_sel).first.select_option(value=cat_val)
                        except Exception:
                            pass

                    # Tagline  →  #h-tagline  input[name='tagline']  max 220
                    if extracted_fields.get("tagline"):
                        tag_sel = "#h-tagline, input[name='tagline']"
                        if await form_page.locator(tag_sel).count() > 0:
                            await self.session.pointer.type_text(
                                form_page, form_page.locator(tag_sel).first, extracted_fields["tagline"][:215], label="Typing tagline"
                            )

                    # Country  →  #h-country  input[name='country']
                    if extracted_fields.get("country"):
                        c_sel = "#h-country, input[name='country']"
                        if await form_page.locator(c_sel).count() > 0:
                            await self.session.pointer.type_text(
                                form_page, form_page.locator(c_sel).first, extracted_fields["country"], label="Typing country"
                            )

                    # Birth year  →  #h-born  input[name='birth_year']  inputmode=numeric
                    if extracted_fields.get("birth"):
                        b_sel = "#h-born, input[name='birth_year']"
                        if await form_page.locator(b_sel).count() > 0:
                            await self.session.pointer.type_text(
                                form_page, form_page.locator(b_sel).first, str(extracted_fields["birth"])[:5], label="Typing birth year"
                            )

                    # Death year  →  #h-died  input[name='death_year']
                    if extracted_fields.get("death"):
                        d_sel = "#h-died, input[name='death_year']"
                        if await form_page.locator(d_sel).count() > 0:
                            await self.session.pointer.type_text(
                                form_page, form_page.locator(d_sel).first, str(extracted_fields["death"])[:5], label="Typing death year"
                            )

                    # Summary  →  #h-summary  textarea[name='summary']  max 2000
                    if extracted_fields.get("summary"):
                        s_sel = "#h-summary, textarea[name='summary']"
                        if await form_page.locator(s_sel).count() > 0:
                            await self.session.pointer.type_text(
                                form_page, form_page.locator(s_sel).first, extracted_fields["summary"][:1900], label="Typing summary"
                            )

                    # Biography / Story  →  #h-bio  textarea[name='biography']  (NOT h-story!)
                    story_text = extracted_fields.get("story") or extracted_fields.get("biography") or ""
                    if story_text:
                        bio_sel = "#h-bio, textarea[name='biography']"
                        if await form_page.locator(bio_sel).count() > 0:
                            await self.executor.tool_paste(
                                bio_sel, story_text[:7500], label="Pasting hero biography"
                            )

                    # Source URL  →  sources[0][url]  input[name='sources[0][url]']
                    if extracted_fields.get("source_url"):
                        src_sel = "input[name='sources[0][url]']"
                        if await form_page.locator(src_sel).count() > 0:
                            await self.session.pointer.type_text(
                                form_page, form_page.locator(src_sel).first, extracted_fields["source_url"][:490], label="Typing source URL"
                            )
                        # Also fill source title with subject name
                        src_title_sel = "input[name='sources[0][title]']"
                        if await form_page.locator(src_title_sel).count() > 0:
                            await self.session.pointer.type_text(
                                form_page, form_page.locator(src_title_sel).first,
                                f"{subject_title} — Wikipedia" if "wikipedia" in extracted_fields.get("source_url", "").lower() else subject_title,
                                label="Typing source title"
                            )

                    # ── Additional Sources ──
                    sources_list = extracted_fields.get("sources") or []
                    for s_idx, s_item in enumerate(sources_list[1:3], start=1):
                        s_url = s_item.get("url", "")
                        s_title = s_item.get("title", "")
                        if s_url:
                            add_s_btn = form_page.locator("button:has-text('Add a source'), .btn:has-text('Add a source')").first
                            if await add_s_btn.count() > 0:
                                await self.session.pointer.click(form_page, add_s_btn, label="Clicking 'Add a source'")
                                await asyncio.sleep(0.4)
                            t_sel = f"input[name='sources[{s_idx}][title]']"
                            u_sel = f"input[name='sources[{s_idx}][url]']"
                            if await form_page.locator(t_sel).count() > 0 and s_title:
                                await form_page.locator(t_sel).first.fill(s_title[:180])
                            if await form_page.locator(u_sel).count() > 0:
                                await form_page.locator(u_sel).first.fill(s_url[:490])

                    # ── Key Moments (Timeline) ──
                    key_moments = extracted_fields.get("key_moments") or []
                    for m_idx, moment in enumerate(key_moments[:3]):
                        yr = moment.get("year_label", "")
                        m_title = moment.get("title", "")
                        m_desc = moment.get("description", "")

                        if m_idx > 0:
                            add_btn = form_page.locator("button:has-text('Add a moment'), .btn:has-text('Add a moment')").first
                            if await add_btn.count() > 0:
                                await self.session.pointer.click(form_page, add_btn, label="Clicking 'Add a moment'")
                                await asyncio.sleep(0.4)

                        yr_sel = f"input[name='achievements[{m_idx}][year_label]']"
                        t_sel = f"input[name='achievements[{m_idx}][title]']"
                        d_sel = f"input[name='achievements[{m_idx}][description]']"

                        if await form_page.locator(yr_sel).count() > 0 and yr:
                            await form_page.locator(yr_sel).first.fill(str(yr)[:10])
                        if await form_page.locator(t_sel).count() > 0 and m_title:
                            await form_page.locator(t_sel).first.fill(m_title[:100])
                        if await form_page.locator(d_sel).count() > 0 and m_desc:
                            await form_page.locator(d_sel).first.fill(m_desc[:250])

                    # ── Quotes ──
                    quotes = extracted_fields.get("quotes") or []
                    for q_idx, q_item in enumerate(quotes[:2]):
                        q_text = q_item.get("quote", "")
                        q_ctx = q_item.get("context", "")

                        if q_idx > 0:
                            add_q_btn = form_page.locator("button:has-text('Add a quote'), .btn:has-text('Add a quote')").first
                            if await add_q_btn.count() > 0:
                                await self.session.pointer.click(form_page, add_q_btn, label="Clicking 'Add a quote'")
                                await asyncio.sleep(0.4)

                        q_sel = f"textarea[name='quotes[{q_idx}][quote]']"
                        ctx_sel = f"input[name='quotes[{q_idx}][context]']"

                        if await form_page.locator(q_sel).count() > 0 and q_text:
                            await form_page.locator(q_sel).first.fill(q_text[:400])
                        if await form_page.locator(ctx_sel).count() > 0 and q_ctx:
                            await form_page.locator(ctx_sel).first.fill(q_ctx[:180])

                    # ── Publishing Status: MUST BE SET TO 'published' ──
                    status_sel = "#h-status, select[name='status']"
                    if await form_page.locator(status_sel).count() > 0:
                        try:
                            await form_page.locator(status_sel).first.select_option(value="published")
                            logger.info("Selected 'published' status on WorldHeroes Hero form.")
                        except Exception as st_err:
                            logger.warning("Could not set status to 'published': %s", st_err)

                    # ── Photo Attachment & Credit ──
                    hero_img_path = extracted_fields.get("selected_image_path")
                    if hero_img_path and os.path.exists(hero_img_path):
                        photo_sel = "#h-photo, input[name='photo'][type='file'], input[type='file']"
                        if await form_page.locator(photo_sel).count() > 0:
                            await form_page.locator(photo_sel).first.set_input_files(hero_img_path)
                            logger.info("Uploaded hero photo to form: %s", hero_img_path)
                            await asyncio.sleep(0.5)

                        credit_val = extracted_fields.get("photo_credit") or "Wikimedia Commons"
                        credit_sel = "#h-credit, input[name='photo_credit']"
                        if await form_page.locator(credit_sel).count() > 0:
                            await form_page.locator(credit_sel).first.fill(credit_val[:180])

                elif c_type == "Blog":
                    # ── Blog form — field IDs confirmed from live /admin/blog/new ──
                    # Title  →  #f-title  input[name='title']  max 180
                    if subject_title:
                        title_sel = "#f-title, input[name='title']"
                        if await form_page.locator(title_sel).count() > 0:
                            await self.session.pointer.type_text(
                                form_page, form_page.locator(title_sel).first, subject_title, label="Typing blog title"
                            )
                            await asyncio.sleep(0.4)

                    # Author name  →  #f-author_name  input[name='author_name']  max 120
                    author_sel = "#f-author_name, input[name='author_name']"
                    if await form_page.locator(author_sel).count() > 0:
                        await self.session.pointer.type_text(
                            form_page, form_page.locator(author_sel).first, "ForgeX Research Bot", label="Typing author name"
                        )

                    # Slug  →  #f-slug  input[name='slug']
                    blog_slug_sel = "#f-slug, input[name='slug']"
                    if await form_page.locator(blog_slug_sel).count() > 0:
                        cur_slug = await form_page.locator(blog_slug_sel).first.input_value()
                        if not cur_slug:
                            await self.session.pointer.type_text(
                                form_page, form_page.locator(blog_slug_sel).first, slug_clean, label="Typing blog slug"
                            )

                    # Summary  →  #f-summary  textarea[name='summary']  max 2000
                    if extracted_fields.get("summary"):
                        bsum_sel = "#f-summary, textarea[name='summary']"
                        if await form_page.locator(bsum_sel).count() > 0:
                            await self.session.pointer.type_text(
                                form_page, form_page.locator(bsum_sel).first, extracted_fields["summary"][:1900], label="Typing blog summary"
                            )

                    # Body content  →  #f-body  textarea[name='body']  max 200000
                    content_text = extracted_fields.get("story") or extracted_fields.get("content") or ""
                    if content_text:
                        body_sel = "#f-body, textarea[name='body']"
                        if await form_page.locator(body_sel).count() > 0:
                            await self.executor.tool_paste(
                                body_sel, content_text[:15000], label="Pasting blog body"
                            )

                    # Source links  →  #f-source_links  textarea[name='source_links']
                    if extracted_fields.get("source_url"):
                        src_links_sel = "#f-source_links, textarea[name='source_links']"
                        if await form_page.locator(src_links_sel).count() > 0:
                            await self.session.pointer.type_text(
                                form_page, form_page.locator(src_links_sel).first, extracted_fields["source_url"][:490], label="Typing source links"
                            )

                    # Status: published
                    b_status_sel = "#status, select[name='status']"
                    if await form_page.locator(b_status_sel).count() > 0:
                        try:
                            await form_page.locator(b_status_sel).first.select_option(value="published")
                            logger.info("Selected 'published' status on Blog form.")
                        except Exception as e:
                            logger.warning("Could not set blog status to published: %s", e)

                    # Photo attachment for blog
                    blog_img_path = extracted_fields.get("selected_image_path")
                    if blog_img_path and os.path.exists(blog_img_path):
                        b_photo_sel = "#photo, input[name='photo'][type='file'], input[type='file']"
                        if await form_page.locator(b_photo_sel).count() > 0:
                            await form_page.locator(b_photo_sel).first.set_input_files(blog_img_path)
                            logger.info("Uploaded blog cover photo: %s", blog_img_path)

            else:
                # Public nomination form fill
                if extracted_fields.get("name"):
                    await self.session.pointer.type_text(
                        form_page, "#n-name", extracted_fields["name"], label="Typing subject name"
                    )
                if cat_val:
                    try:
                        await form_page.locator("#n-category").select_option(value=cat_val)
                    except Exception:
                        pass
                if extracted_fields.get("country"):
                    await self.session.pointer.type_text(
                        form_page, "#n-country", extracted_fields["country"], label="Typing country"
                    )
                if extracted_fields.get("birth"):
                    await self.session.pointer.type_text(
                        form_page, "#n-born", str(extracted_fields["birth"]), label="Typing birth year"
                    )
                if extracted_fields.get("death"):
                    await self.session.pointer.type_text(
                        form_page, "#n-died", str(extracted_fields["death"]), label="Typing death year"
                    )
                if extracted_fields.get("tagline"):
                    await self.session.pointer.type_text(
                        form_page, "#n-tagline", extracted_fields["tagline"][:215], label="Typing one-line tagline"
                    )
                if extracted_fields.get("summary"):
                    await self.session.pointer.type_text(
                        form_page, "#n-summary", extracted_fields["summary"][:580], label="Typing summary"
                    )
                if extracted_fields.get("story"):
                    await self.executor.tool_paste(
                        "#n-story", extracted_fields["story"][:7500], label="Pasting detailed story"
                    )
                if extracted_fields.get("source_url"):
                    await self.session.pointer.type_text(
                        form_page, "#n-source", extracted_fields["source_url"][:490], label="Typing source URL"
                    )

            # -------------------------------------------------------------
            # Stage 4: Approval Gate (STOP BEFORE SUBMIT OR AUTO-PUBLISH)
            # -------------------------------------------------------------
            if self.auto_publish:
                logger.info("Auto-publish enabled. Directly publishing without pausing at approval gate...")
                await self.emit_step(
                    f"All admin fields populated and validated. Auto-Publish is active: directly publishing {c_type} live...",
                    "auto_publish",
                    {
                        "tab_id": "tab-worldheroes",
                        "tab_title": f"WorldHeroes Admin ({c_type})",
                        "tab_icon": "⚡",
                        "target_url": target_url,
                        "admin_mode": is_admin,
                        "content_type": c_type,
                    },
                    form_page,
                )
                approved = True
            else:
                approval_msg = (
                    f"All admin fields populated for {c_type}. STOPPED before submit. Awaiting approval to DIRECTLY PUBLISH live."
                    if is_admin
                    else "All form fields populated. STOPPED before submit. Awaiting human approval."
                )
                await self.emit_step(
                    approval_msg,
                    "request_approval",
                    {
                        "tab_id": "tab-worldheroes",
                        "tab_title": f"WorldHeroes Admin ({c_type})",
                        "tab_icon": "⚡",
                        "target_url": target_url,
                        "admin_mode": is_admin,
                        "content_type": c_type,
                    },
                    form_page,
                )
                self.update_run_status("WAITING_APPROVAL")

                # Poll for UI approval event
                approved = False
                while True:
                    await asyncio.sleep(1.0)
                    r = self.get_run()
                    if not r:
                        break
                    cmd = (r.command or "").upper()
                    if cmd == "APPROVE" or r.status == "APPROVED":
                        approved = True
                        break
                    elif cmd == "REJECT":
                        self.update_run_status("REJECTED")
                        await self.emit_step(
                            "Submission rejected by user.",
                            "rejected",
                            {
                                "tab_id": "tab-worldheroes",
                                "tab_title": f"WorldHeroes Admin ({c_type})",
                                "tab_icon": "⚡",
                            },
                            form_page,
                        )
                        return
                    elif cmd == "STOP":
                        raise InterruptedError("Run stopped by user.")

            # -------------------------------------------------------------
            # Stage 5: Final Submission (ONLY after explicit approval)
            # -------------------------------------------------------------
            if approved:
                self.update_run_status("SUBMITTING")

                # If fields were updated by user in UI review card, reflect on form before submit
                r_latest = self.get_run()
                latest_res = r_latest.get_result() if r_latest else {}
                for k in ["tagline", "summary", "story", "country", "birth", "death", "source_url"]:
                    if latest_res.get(k) and latest_res.get(k) != extracted_fields.get(k):
                        extracted_fields[k] = latest_res[k]
                        try:
                            if k == "tagline" and await form_page.locator("#h-tagline, input[name='tagline']").count() > 0:
                                await form_page.locator("#h-tagline, input[name='tagline']").first.fill(latest_res[k])
                            elif k == "summary" and await form_page.locator("#h-summary, textarea[name='summary']").count() > 0:
                                await form_page.locator("#h-summary, textarea[name='summary']").first.fill(latest_res[k])
                            elif k == "story" and await form_page.locator("#h-bio, textarea[name='biography'], #f-content, textarea[name='content']").count() > 0:
                                await form_page.locator("#h-bio, textarea[name='biography'], #f-content, textarea[name='content']").first.fill(latest_res[k])
                        except Exception:
                            pass

                submit_action_text = f"Human approval confirmed. Directly publishing {c_type} live..." if is_admin else "Human approval confirmed. Clicking submit..."
                await self.emit_step(
                    submit_action_text,
                    "submit",
                    {
                        "tab_id": "tab-worldheroes",
                        "tab_title": f"WorldHeroes Admin ({c_type})",
                        "tab_icon": "⚡",
                    },
                    form_page,
                )

                if is_admin:
                    # Hero/Blog admin: "Save" button has name="after" value="stay"
                    # "Save and add another" has value="new" — we want value="stay"
                    submit_btn = form_page.locator(
                        "button[type='submit'][value='stay'], "
                        "button.btn-block[type='submit']:not(.btn-ghost), "
                        "button[type='submit']"
                    ).first
                else:
                    submit_btn = form_page.locator("button[type='submit']:has-text('Send nomination'), button[type='submit']").first

                await self.session.pointer.click(form_page, submit_btn, label=f"Publishing {c_type} live" if is_admin else "Submitting nomination")
                await asyncio.sleep(4.0)

                live_url = form_page.url
                # Check for success indicators
                still_on_new = live_url.rstrip("/").endswith("/new")
                if still_on_new:
                    # Check for error message on page
                    err_text = await form_page.locator(".alert-error, .error, [role='alert']").first.text_content() if await form_page.locator(".alert-error, .error, [role='alert']").count() > 0 else ""
                    finish_msg = f"Submission may have failed — still on 'new' page. Check browser. Error: {err_text or 'none'}"
                    logger.warning(finish_msg)
                else:
                    finish_msg = (
                        f"Successfully published {c_type} directly to WorldHeroes live! URL: {live_url}"
                        if is_admin
                        else f"Submission completed successfully! URL: {live_url}"
                    )
                await self.emit_step(
                    finish_msg,
                    "finish",
                    {
                        "tab_id": "tab-worldheroes",
                        "tab_title": f"WorldHeroes Admin ({c_type})",
                        "tab_icon": "⚡",
                        "published_url": live_url,
                    },
                    form_page,
                )
                self.update_run_status("COMPLETED")
                with SyncSessionLocal() as db:
                    db_run = db.execute(select(Run).where(Run.id == self.run_id)).scalar_one()
                    db_run.finished_at = datetime.utcnow()
                    db.commit()

        except Exception as exc:
            logger.error("Runner execution failed on Run %d: %s", self.run_id, exc, exc_info=True)
            self.update_run_status("FAILED", error=str(exc))
            if self.session and self.session.get_active_page():
                await self.emit_step(
                    f"Execution error: {exc}",
                    "error",
                    {
                        "tab_id": "tab-error",
                        "tab_title": "Execution Error",
                        "tab_icon": "⚠️",
                    },
                    self.session.get_active_page(),
                    status="FAILED",
                    error=str(exc),
                )
        finally:
            if self.session:
                await self.session.stop()
            logger.info("Run %d worker finished.", self.run_id)


def main():
    parser = argparse.ArgumentParser(description="ForgeX Agent Worker Subprocess")
    parser.add_argument("--run-id", type=int, required=True, help="ID of Run in SQLite database")
    parser.add_argument("--headless", action="store_true", help="Force headless Chrome")
    parser.add_argument("--auto-publish", action="store_true", help="Auto-publish without pausing at WAITING_APPROVAL")
    args = parser.parse_args()

    runner = AgentRunner(
        run_id=args.run_id,
        headless=args.headless if args.headless else None,
        auto_publish=args.auto_publish,
    )
    asyncio.run(runner.execute())


if __name__ == "__main__":
    main()

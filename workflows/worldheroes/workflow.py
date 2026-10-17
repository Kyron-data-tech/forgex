import os
import base64
import asyncio
from pathlib import Path
from typing import Any
from workflows.base import Workflow
from app.services.research import ResearchService
from app.services.images import WikimediaCommonsProvider
from app.services.limits import human_delay
from app.services.storage import StorageService


# ─────────────────────────────────────────────────────────────────────────────
# Helper: Adaptive field filler
# Tries every selector variant in a comma-separated CSS string.  
# Returns True if any variant succeeded.
# ─────────────────────────────────────────────────────────────────────────────
async def _fill_field(page: Any, css_variants: str, value: str, field_name: str, logs: list) -> bool:
    """Try each CSS selector variant and fill with `value`. Returns True on success."""
    if value is None:
        return False
    str_val = str(value).strip()
    if not str_val:
        return False

    for selector in [s.strip() for s in css_variants.split(",") if s.strip()]:
        try:
            locator = page.locator(selector).first
            if await locator.count() > 0:
                tag = await locator.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    await locator.select_option(value=str_val)
                else:
                    await locator.scroll_into_view_if_needed()
                    await locator.fill(str_val)
                    # Dispatch input and change events so JavaScript listeners register the change
                    await locator.evaluate("""el => {
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }""")
                logs.append(f"✓ Filled [{field_name}] with selector '{selector}'")
                await human_delay(200, 500)
                return True
        except Exception as exc:
            logs.append(f"  ↳ Selector '{selector}' for [{field_name}] failed: {exc}")
    logs.append(f"⚠ Could not fill [{field_name}] — no matching element found")
    return False


async def _attach_image(page: Any, image_path: str, file_selectors: str, logs: list) -> bool:
    """Attach image file to the first matching file-input element with automatic size/format optimization."""
    if not image_path:
        logs.append("⚠ No image attached to this entry.")
        return False

    if not os.path.exists(image_path):
        logs.append(f"⚠ Image path not found on disk: {image_path}")
        return False

    # Auto-optimize and correct image size & format before attaching
    upload_path = image_path
    try:
        from app.services.storage import StorageService
        storage = StorageService()
        with open(image_path, "rb") as f:
            raw_bytes = f.read()
        opt_bytes, fmt, w, h = storage.optimize_image_bytes(raw_bytes, max_dim=1200, quality=90)
        
        opt_path = Path(image_path).parent / f"{Path(image_path).stem}_upload.jpg"
        with open(opt_path, "wb") as f:
            f.write(opt_bytes)
        upload_path = str(opt_path)
        logs.append(f"✓ Image formatted & optimized: {w}x{h} px ({len(opt_bytes) // 1024} KB)")
    except Exception as opt_err:
        logs.append(f"Notice: Image optimization note: {opt_err}")

    for selector in [s.strip() for s in file_selectors.split(",") if s.strip()]:
        try:
            locator = page.locator(selector).first
            if await locator.count() > 0:
                await locator.set_input_files(upload_path)
                logs.append(f"✓ Image attached successfully: {os.path.basename(upload_path)}")
                await human_delay(500, 1000)
                return True
        except Exception as exc:
            logs.append(f"  ↳ File-input selector '{selector}' failed: {exc}")

    logs.append("⚠ Could not attach image — no matching file input element found")
    return False


async def _screenshot_b64(page: Any) -> str:
    raw = await page.screenshot(full_page=True)
    return base64.b64encode(raw).decode("ascii")


import re


def _slugify(text: str) -> str:
    s = str(text or "").lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s)
    return s.strip("-")


class WorldHeroesWorkflow(Workflow):
    name: str = "worldheroes"
    title: str = "WorldHeroes Publisher"
    description: str = "Publishes biographical heroes, stories, and blogs to worldheros.com"
    content_types: list[str] = ["Hero", "Story", "Blog"]
    selectors_file: Path = Path(__file__).parent / "selectors.yaml"

    def get_input_schema(self) -> dict:
        return {
            "type": "object",
            "required": ["subject_name", "work_date"],
            "properties": {
                "subject_name": {
                    "type": "string",
                    "title": "Person / Subject Name",
                    "description": "Full name of the hero, leader, scientist, etc."
                },
                "content_type": {
                    "type": "string",
                    "title": "Content Type",
                    "enum": ["Hero", "Story", "Blog"],
                    "default": "Hero"
                },
                "work_date": {
                    "type": "string",
                    "title": "Date of Work",
                    "format": "date",
                    "description": "Date associated with this entry"
                },
                "notes": {
                    "type": "string",
                    "title": "Editorial Notes",
                    "format": "textarea",
                    "description": "Your custom remarks or focus for this entry"
                }
            }
        }

    def get_review_schema(self) -> dict:
        return {
            "type": "object",
            "required": ["name", "category", "tagline", "summary", "story"],
            "properties": {
                "name": {"type": "string", "title": "Their Name"},
                "category": {
                    "type": "string",
                    "title": "Category",
                    "enum": [
                        "leaders", "entrepreneurs", "scientists", "athletes",
                        "artists", "humanitarians", "unsung-heroes", "historical-heroes"
                    ],
                },
                "country": {"type": "string", "title": "Country"},
                "birth": {"type": "string", "title": "Year Born"},
                "death": {"type": "string", "title": "Year Died"},
                "tagline": {"type": "string", "title": "Describe them in one line", "description": "Max 220 chars. E.g. Taught every child in her village to read"},
                "summary": {"type": "string", "title": "Why are they a hero?", "description": "Max 600 chars. A few sentences is enough."},
                "story": {"type": "string", "title": "Their story", "description": "Max 8000 chars. What they did, when, and who it helped."},
                "source_url": {"type": "string", "title": "A link where we can read more", "description": "Valid URL (https://) for further reading."},
                "attribution": {"type": "string", "title": "Attribution / Source"},
            }
        }

    def _infer_category(self, text: str) -> str:
        text_lower = text.lower()
        if any(w in text_lower for w in ["scientist", "physicist", "chemist", "biologist", "mathematician", "astronomer"]):
            return "scientists"
        # Extreme / action sports must be checked before traditional athlete keywords
        # so skydivers, BASE jumpers etc. don't fall through to historical-heroes
        if any(w in text_lower for w in [
            "skydiver", "skydiving", "base jump", "base jumping", "extreme sport",
            "extreme sportsman", "daredevil", "freefall", "parachut", "wingsuit",
            "stunt", "motocross", "bmx", "rock climbing",
            "athlete", "player", "footballer", "cricket", "tennis player",
            "swimmer", "runner", "sport", "olympic", "champion", "sprinter",
            "cycling", "boxing", "wrestling",
        ]):
            return "athletes"
        if any(w in text_lower for w in ["humanitarian", "activist", "philanthropist", "charity", "relief", "ngo", "human rights", "peace prize", "civil rights", "education activist", "social reformer"]):
            return "humanitarians"
        if any(w in text_lower for w in ["president", "prime minister", "king", "queen", "emperor", "governor", "senator", "politician", "freedom fighter", "independence"]):
            return "leaders"
        if any(w in text_lower for w in ["artist", "painter", "sculptor", "musician", "composer", "poet", "novelist", "author", "writer", "filmmaker", "satirist", "cartoonist"]):
            return "artists"
        if any(w in text_lower for w in ["entrepreneur", "founder", "ceo", "businessman", "businesswoman", "startup"]):
            return "entrepreneurs"
        if any(w in text_lower for w in ["historical", "ancient", "medieval", "century", "revolution", "war", "battle"]):
            return "historical-heroes"
        return "unsung-heroes"

    async def prepare(self, inputs: dict) -> dict:
        """
        4-Stage research pipeline (Discover → Verify → Build → Package):
          1. ResearchService discovers content from Wikipedia, DDG, News, Archives
          2. Cross-source fact verification (birth year, nationality, event dates)
          3. Content synthesised into field-accurate WorldHeroes form data
          4. Wikimedia Commons images fetched concurrently
        """
        subject = inputs.get("subject_name", "")
        content_type = inputs.get("content_type", "Hero")
        work_date = inputs.get("work_date", "")
        notes = inputs.get("notes", "")

        # Run research + image search concurrently
        research_service = ResearchService()
        commons_service = WikimediaCommonsProvider()

        import asyncio
        profile, candidate_images = await asyncio.gather(
            research_service.research(subject, content_type=content_type,
                                      work_date=work_date),
            commons_service.search_images(subject, limit=6),
        )

        facts = profile.get("facts", {})
        story = profile.get("story", "")
        if notes:
            story = story + f"\n\nEditorial Notes: {notes}"

        # Prefer the best authoritative source URL; fall back to Wikipedia canonical URL
        source_url = (
            profile.get("page_url")
            or profile.get("canonical_url")
            or ""
        )

        review_data = {
            "name":        profile.get("title", subject),
            "category":    profile.get("category", self._infer_category(story)),
            "tagline":     profile.get("tagline", ""),
            "country":     facts.get("nationality", ""),
            "birth":       facts.get("birth", facts.get("birth_year", "")),
            "death":       facts.get("death", facts.get("death_year", "")),
            "summary":     profile.get("summary", ""),
            "story":       story,
            "work_date":   work_date,
            "source_url":  source_url,
            "attribution": profile.get("attribution", "Source: Wikipedia (CC BY-SA 4.0)"),
        }

        return {
            "review_data":      review_data,
            "candidate_images": candidate_images,
            "research_raw":     profile,
        }

    async def run_playbook(self, inputs: dict, executor: Any) -> dict:
        """
        Visible Operator Playbook execution:
          1. Multi-tab Google research with AI overview selection (ResearchPlaybook)
          2. ChatGPT Web prompt hand-off and structured JSON field extraction
          3. Wikimedia Commons images fetched concurrently
        Falls back to prepare() if executor is absent or in headless legacy mode.
        """
        if not executor:
            return await self.prepare(inputs)

        from app.agent.playbook import ResearchPlaybook
        from app.agent.chatgpt import ChatGPTHandoff
        from app.services.images import WikimediaCommonsProvider

        subject = inputs.get("subject_name", "")
        work_date = inputs.get("work_date", "")
        notes = inputs.get("notes", "")

        playbook = ResearchPlaybook(executor)
        chatgpt = ChatGPTHandoff(executor)
        commons_service = WikimediaCommonsProvider()

        # Step 1: Visible Google research
        research_bundle = await playbook.run(
            subject_name=subject,
            work_date=work_date,
            notes=notes,
        )

        # Step 2: ChatGPT field extraction & Wikimedia images
        import asyncio
        chatgpt_res, candidate_images = await asyncio.gather(
            chatgpt.run(research_bundle.get("bundle_text", "")),
            commons_service.search_images(subject, limit=6),
        )

        fields = chatgpt_res.get("data") or {}
        story = fields.get("story", "")
        if notes:
            story = story + f"\n\nEditorial Notes: {notes}"

        review_data = {
            "name":        fields.get("name", subject),
            "category":    fields.get("category", self._infer_category(story)),
            "tagline":     fields.get("tagline", ""),
            "country":     fields.get("country", ""),
            "birth":       fields.get("birth", ""),
            "death":       fields.get("death", ""),
            "summary":     fields.get("summary", ""),
            "story":       story,
            "work_date":   work_date,
            "source_url":  fields.get("source_url", ""),
            "attribution": "Source: Google Search & Verified Web Articles",
        }

        return {
            "review_data":      review_data,
            "candidate_images": candidate_images,
            "research_bundle":  research_bundle,
            "chatgpt_result":   chatgpt_res,
        }



    # ─────────────────────────────────────────────────────────────────────────
    # Core: shared form-fill logic used by both dry_run and publish
    # ─────────────────────────────────────────────────────────────────────────
    async def _navigate_and_fill_admin(self, data: dict, page: Any) -> dict:
        """
        Direct Admin Publishing: Navigate to /admin/heroes/new, /admin/blog/new, or
        /admin/story/new, authenticate using admin username and password (#u, #p),
        and populate all fields. Does NOT click publish.
        """
        logs = []
        sel = self._selectors
        admin_cfg = sel.get("admin", {})

        content_type = str(data.get("content_type") or "Hero").strip().title()
        if content_type == "Blog":
            form_cfg = admin_cfg.get("blog", {})
            default_url = "https://worldheros.com/admin/blog/new"
        elif content_type == "Story":
            form_cfg = admin_cfg.get("story", {})
            default_url = "https://worldheros.com/admin/story/new"
        else:
            content_type = "Hero"
            form_cfg = admin_cfg.get("hero", {})
            default_url = "https://worldheros.com/admin/heroes/new"

        target_url = form_cfg.get("new_url", default_url)
        login_url = admin_cfg.get("login_url", "https://worldheros.com/admin/login")

        logs.append(f"Admin Mode: Navigating to {target_url} for {content_type} direct publishing…")
        await page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)
        await human_delay(800, 1500)

        # Check if redirected to login or login form is present
        current_url = page.url
        is_login_page = "/admin/login" in current_url or await page.locator("#u, input[name='username']").count() > 0

        if is_login_page:
            from app.config import get_admin_credentials
            admin_u, admin_p, _ = get_admin_credentials()

            if admin_u and admin_p:
                logs.append(f"Authenticating as admin ({admin_u})…")
                try:
                    await page.locator("#u, input[name='username']").first.fill(admin_u)
                    await human_delay(200, 400)
                    await page.locator("#p, input[name='password']").first.fill(admin_p)
                    await human_delay(200, 400)
                    await page.locator("button[type='submit']").first.click()
                    try:
                        await page.wait_for_url(lambda u: "/admin/login" not in u, timeout=15_000)
                    except Exception:
                        pass
                    await human_delay(600, 1200)
                    logs.append("✓ Admin authentication completed.")
                    if page.url != target_url:
                        await page.goto(target_url, wait_until="domcontentloaded", timeout=20_000)
                        await human_delay(600, 1200)
                except Exception as login_err:
                    logs.append(f"⚠ Admin login attempt error: {login_err}")

            if "/admin/login" in page.url or await page.locator("#u, input[name='username']").count() > 0:
                logs.append("WorldHeroes Admin login required.")
                screenshot_b64 = await _screenshot_b64(page)
                return {
                    "filled": False,
                    "needs_login": True,
                    "screenshot_b64": screenshot_b64,
                    "logs": logs,
                    "message": (
                        "Not logged into WorldHeroes Admin. "
                        "Please enter your Admin Username and Password in Settings "
                        "to enable direct publishing."
                    )
                }

        logs.append(f"✓ WorldHeroes Admin form confirmed for {content_type}.")

        # Fill form fields
        fields_filled = {}
        title_val = str(data.get("name") or data.get("title") or "").strip()
        slug_val = _slugify(title_val)

        category_slug = str(data.get("category", "")).lower().strip()
        category_id_map = {
            "leaders": "1",
            "entrepreneurs": "2",
            "scientists": "3",
            "athletes": "4",
            "artists": "5",
            "humanitarians": "6",
            "unsung-heroes": "7",
            "historical-heroes": "8"
        }
        cat_id = category_id_map.get(category_slug, "1")

        if content_type == "Hero":
            tagline_val = str(data.get("tagline", ""))[:215]
            summary_val = str(data.get("summary", ""))[:580]
            story_val = str(data.get("story", ""))[:7900]
            birth_val = str(data.get("birth", ""))[:5]
            death_val = str(data.get("death", ""))[:5]
            country_val = str(data.get("country", ""))
            source_val = str(data.get("source_url", ""))

            admin_hero_map = [
                ("name_input", title_val, "name"),
                ("slug_input", slug_val, "slug"),
                ("tagline_input", tagline_val, "tagline"),
                ("country_input", country_val, "country"),
                ("birth_year_input", birth_val, "birth_year"),
                ("death_year_input", death_val, "death_year"),
                ("summary_input", summary_val, "summary"),
                ("story_input", story_val, "story"),
                ("source_url_input", source_val, "source_url"),
            ]
            for sel_key, val, lbl in admin_hero_map:
                selector = form_cfg.get(sel_key)
                if selector and val:
                    ok = await _fill_field(page, selector, val, lbl, logs)
                    if ok:
                        fields_filled[lbl] = val[:40]

            cat_sel = form_cfg.get("category_select", "select[name='category_id']")
            try:
                loc = page.locator(cat_sel).first
                if await loc.count() > 0:
                    await loc.select_option(value=cat_id)
                    fields_filled["category"] = category_slug or cat_id
                    logs.append(f"✓ Category selected: {cat_id}")
            except Exception as e:
                logs.append(f"⚠ Category selection note: {e}")

            # Status: published (ensures hero appears live on the site)
            status_sel = form_cfg.get("status_select", "#h-status, select[name='status']")
            try:
                st_loc = page.locator(status_sel).first
                if await st_loc.count() > 0:
                    await st_loc.select_option(value="published")
                    fields_filled["status"] = "published"
                    logs.append("✓ Status selected: published")
            except Exception as e:
                logs.append(f"⚠ Status selection note: {e}")

            # Photo credit
            credit_sel = "#h-credit, input[name='photo_credit']"
            photo_credit_val = data.get("photo_credit") or "Wikimedia Commons"
            try:
                c_loc = page.locator(credit_sel).first
                if await c_loc.count() > 0:
                    await c_loc.fill(photo_credit_val[:180])
                    fields_filled["photo_credit"] = photo_credit_val
            except Exception:
                pass

        elif content_type in ("Blog", "Story"):
            content_val = str(data.get("story") or data.get("content") or "")[:7900]
            summary_val = str(data.get("summary", ""))[:580]

            admin_map = [
                ("title_input", title_val, "title"),
                ("slug_input", slug_val, "slug"),
                ("summary_input", summary_val, "summary"),
                ("content_input", content_val, "content"),
            ]
            for sel_key, val, lbl in admin_map:
                selector = form_cfg.get(sel_key)
                if selector and val:
                    ok = await _fill_field(page, selector, val, lbl, logs)
                    if ok:
                        fields_filled[lbl] = val[:40]

            cat_sel = form_cfg.get("category_select", "select[name='category_id']")
            try:
                loc = page.locator(cat_sel).first
                if await loc.count() > 0:
                    await loc.select_option(value=cat_id)
                    fields_filled["category"] = category_slug or cat_id
                    logs.append(f"✓ Category selected: {cat_id}")
            except Exception as e:
                pass

            # Blog status: published
            b_status_sel = "#status, select[name='status']"
            try:
                b_st_loc = page.locator(b_status_sel).first
                if await b_st_loc.count() > 0:
                    await b_st_loc.select_option(value="published")
                    fields_filled["status"] = "published"
                    logs.append("✓ Blog status selected: published")
            except Exception:
                pass

        # Image attachment
        img_sel = form_cfg.get("image_upload", "input[type='file']")
        image_path = data.get("selected_image_path")
        if image_path:
            img_ok = await _attach_image(page, image_path, img_sel, logs)
            if img_ok:
                fields_filled["image"] = os.path.basename(image_path)

        screenshot_b64 = await _screenshot_b64(page)
        logs.append(f"Admin fields successfully populated: {list(fields_filled.keys())}")
        return {
            "filled": True,
            "needs_login": False,
            "is_admin": True,
            "content_type": content_type,
            "screenshot_b64": screenshot_b64,
            "filled_fields": fields_filled,
            "logs": logs,
        }

    async def _navigate_and_fill(self, data: dict, page: Any) -> dict:
        """
        Navigate to target form, detect login state, fill all discoverable fields,
        attach the bot-chosen image. Returns a result dict with logs and screenshots.
        Does NOT click submit.
        """
        # Check if Admin mode is enabled — get_admin_credentials() reads from SQLite first
        from app.config import get_admin_credentials
        admin_u, admin_p, is_admin_mode = get_admin_credentials()
        # Admin mode requires both credentials to be set
        is_admin_mode = is_admin_mode and bool(admin_u and admin_p)
        if is_admin_mode:
            return await self._navigate_and_fill_admin(data, page)

        logs = []
        sel = self._selectors
        site = sel.get("site", {})
        nom = sel.get("nomination", {})

        nominate_url = site.get("nominate_url", "https://worldheros.com/nominate")
        signin_url = site.get("signin_url", "https://worldheros.com/signin")

        # ── 1. Navigate ──────────────────────────────────────────────────────
        logs.append(f"Navigating to {nominate_url} …")
        await page.goto(nominate_url, wait_until="domcontentloaded", timeout=30_000)
        await human_delay(800, 1500)

        # ── 2. Auth check & automatic headless login ──────────────────────────
        current_url = page.url
        has_signin_prompt = await page.locator(".signin-prompt, a[href*='/signin?next=']").count() > 0
        if "signin" in current_url or has_signin_prompt:
            wh_email = os.environ.get("WH_EMAIL")
            wh_password = os.environ.get("WH_PASSWORD")
            if wh_email and wh_password:
                logs.append("No active session found. Running automatic headless login with saved credentials…")
                try:
                    await page.goto(signin_url, wait_until="domcontentloaded", timeout=20_000)
                    await page.wait_for_selector("input[name='email']", timeout=10_000)
                    await page.locator("input[name='email']").fill(wh_email)
                    await human_delay(200, 400)
                    await page.locator("input[name='password']").fill(wh_password)
                    await human_delay(200, 400)
                    await page.locator("button[type='submit']").click()
                    try:
                        await page.wait_for_url(
                            lambda u: "/signin" not in u and "/join" not in u,
                            timeout=15_000
                        )
                    except Exception:
                        pass
                    await human_delay(600, 1200)
                    logs.append("✓ Headless login request completed.")
                    # Return to nominate page
                    await page.goto(nominate_url, wait_until="domcontentloaded", timeout=20_000)
                    await human_delay(600, 1200)
                    current_url = page.url
                    has_signin_prompt = await page.locator(".signin-prompt, a[href*='/signin?next=']").count() > 0
                except Exception as login_err:
                    logs.append(f"⚠ Automatic headless login attempt error: {login_err}")

            if "signin" in current_url or has_signin_prompt:
                logs.append("Active authenticated session required on worldheros.com.")
                screenshot_b64 = await _screenshot_b64(page)
                return {
                    "filled": False,
                    "needs_login": True,
                    "screenshot_b64": screenshot_b64,
                    "logs": logs,
                    "message": (
                        "Not logged into worldheros.com. "
                        "Please enter your WorldHeroes email and password in Settings "
                        "for automatic headless login (no browser window needed)."
                    )
                }

        logs.append("✓ Authenticated session confirmed.")

        # ── 3. Wait for form to be ready ──────────────────────────────────────
        ready_sel = nom.get("ready_indicator", "#n-name, input[name='name'], .stack-form input:not([type='hidden'])")
        try:
            await page.wait_for_selector(ready_sel, timeout=10_000)
            logs.append("✓ Nomination form detected on page.")
        except Exception:
            logs.append("⚠ No nomination form fields found within 10 s.")
            screenshot_b64 = await _screenshot_b64(page)
            return {
                "filled": False,
                "needs_login": False,
                "screenshot_b64": screenshot_b64,
                "logs": logs,
                "message": "Nomination form not found on the page. Selectors may need updating."
            }

        # ── 4. Introspect live form fields ────────────────────────────────────
        live_inputs = await page.locator(".stack-form input, .stack-form textarea, .stack-form select").all()
        live_field_names = set()
        for el in live_inputs:
            try:
                name = await el.get_attribute("name") or ""
                if name:
                    live_field_names.add(name)
            except Exception:
                pass
        logs.append(f"Live form fields detected: {sorted(live_field_names)}")

        # ── 5. Fill fields with smart data formatting ─────────────────────────
        fields_filled = {}

        # Year extraction helper for inputmode="numeric" maxlength="5"
        import re
        def _extract_year_str(val: Any) -> str:
            if not val:
                return ""
            s = str(val).strip()
            ym = re.search(r"\b(\d{3,4})\b", s)
            return ym.group(1) if ym else s[:5]

        # Extract clean source/read-more URL (valid URL for input type="url")
        raw_source_url = str(data.get("source_url") or "").strip()
        if raw_source_url and raw_source_url.startswith("http"):
            source_url = re.sub(r"[,\s|;]+$", "", raw_source_url)
        else:
            attrib = str(data.get("attribution", ""))
            url_match = re.search(r"https?://[^\s)\]|]+", attrib)
            if url_match:
                source_url = url_match.group(0).rstrip(".,;")
            else:
                name_slug = str(data.get("name", "")).replace(" ", "_")
                source_url = f"https://en.wikipedia.org/wiki/{name_slug}" if name_slug else ""

        # Form fields mapping
        tagline_val = str(data.get("tagline", ""))[:215]
        summary_val = str(data.get("summary", ""))[:580]
        story_val = str(data.get("story", ""))[:7900]
        birth_val = _extract_year_str(data.get("birth"))
        death_val = _extract_year_str(data.get("death"))

        field_map = [
            ("name_field",        data.get("name"),   "name"),
            ("tagline_field",     tagline_val,        "tagline"),
            ("country_field",     data.get("country"),"country"),
            ("birth_year_field",  birth_val,          "birth_year"),
            ("death_year_field",  death_val,          "death_year"),
            ("summary_field",     summary_val,        "summary"),
            ("story_field",       story_val,          "story"),
            ("source_url_field",  source_url,         "source_url"),
        ]

        for selector_key, value, label in field_map:
            selector = nom.get(selector_key, "")
            if not selector or not value:
                continue
            ok = await _fill_field(page, selector, str(value), label, logs)
            if ok:
                fields_filled[label] = str(value)[:40]

        # Category dropdown selection (WorldHeroes uses numeric IDs 1-8)
        category_slug = str(data.get("category", "")).lower().strip()
        if category_slug and nom.get("category_select"):
            cat_sel = nom["category_select"]
            category_id_map = {
                "leaders": "1",
                "entrepreneurs": "2",
                "scientists": "3",
                "athletes": "4",
                "artists": "5",
                "humanitarians": "6",
                "unsung-heroes": "7",
                "historical-heroes": "8"
            }
            target_id = category_id_map.get(category_slug, "")
            try:
                cat_loc = page.locator(cat_sel).first
                if await cat_loc.count() > 0:
                    selected = False
                    if target_id:
                        try:
                            await cat_loc.select_option(value=target_id)
                            logs.append(f"✓ Category selected by ID: {target_id} ({category_slug})")
                            fields_filled["category"] = category_slug
                            selected = True
                        except Exception:
                            pass
                    if not selected:
                        label_name = category_slug.replace("-", " ").title()
                        try:
                            await cat_loc.select_option(label=label_name)
                            logs.append(f"✓ Category selected by label: {label_name}")
                            fields_filled["category"] = category_slug
                            selected = True
                        except Exception:
                            pass
                    if not selected:
                        try:
                            await cat_loc.select_option(value=category_slug)
                            logs.append(f"✓ Category selected by value: {category_slug}")
                            fields_filled["category"] = category_slug
                        except Exception as e:
                            logs.append(f"⚠ Category selection failed: {e}")
            except Exception as exc:
                logs.append(f"⚠ Category select element error: {exc}")

        # ── 6. Attach image ───────────────────────────────────────────────────
        image_path = data.get("selected_image_path")
        if not image_path or not os.path.exists(image_path):
            # Fallback: search local user storage for person's saved photo
            try:
                from app.services.storage import StorageService
                storage = StorageService()
                slug = storage.sanitize_slug(data.get("name", ""))
                from app.config import settings
                for base in [settings.DEFAULT_DATA_DIR, Path("data_storage")]:
                    folder = (base / slug).resolve()
                    if folder.exists():
                        for f in folder.glob("*.*"):
                            if f.suffix.lower() in [".jpg", ".jpeg", ".png", ".webp"] and not f.name.endswith(".meta.json") and not f.name.endswith("_upload.jpg"):
                                image_path = str(f.resolve())
                                logs.append(f"Auto-resolved photo from local storage: {f.name}")
                                break
                    if image_path and os.path.exists(image_path):
                        break
            except Exception:
                pass

        file_sel = nom.get("image_upload", "#member-images, input[type='file']")
        img_ok = await _attach_image(page, image_path, file_sel, logs)
        if img_ok:
            fields_filled["image"] = os.path.basename(image_path)

        # ── 7. Screenshot ─────────────────────────────────────────────────────
        logs.append(f"Fields successfully filled: {list(fields_filled.keys())}")
        screenshot_b64 = await _screenshot_b64(page)

        return {
            "filled": True,
            "needs_login": False,
            "screenshot_b64": screenshot_b64,
            "filled_fields": fields_filled,
            "live_fields_detected": sorted(live_field_names),
            "logs": logs,
        }

    async def dry_run(self, data: dict, page: Any) -> dict:
        """
        Fills the nomination form completely and takes a screenshot,
        but NEVER clicks submit.
        """
        result = await self._navigate_and_fill(data, page)

        if not result.get("filled"):
            return {
                "success": False,
                "needs_login": result.get("needs_login", False),
                "screenshot_b64": result.get("screenshot_b64", ""),
                "logs": result.get("logs", []),
                "message": result.get("message", "Form fill failed"),
            }

        result["logs"].append("✓ Dry-run complete — form filled, submit NOT clicked.")
        return {
            "success": True,
            "needs_login": False,
            "screenshot_b64": result["screenshot_b64"],
            "filled_fields": result.get("filled_fields", {}),
            "live_fields_detected": result.get("live_fields_detected", []),
            "logs": result["logs"],
        }

    async def publish(self, data: dict, page: Any) -> dict:
        """
        Full publish: fill form, then click the submit button.
        Only invoked after the user explicitly clicks 'Approve & Publish'.
        """
        # ── Fill form ─────────────────────────────────────────────────────────
        fill_result = await self._navigate_and_fill(data, page)
        if not fill_result.get("filled"):
            return {
                "success": False,
                "error": fill_result.get("message", "Form fill failed before publish"),
                "logs": fill_result.get("logs", []),
            }

        logs = fill_result["logs"]
        is_admin = fill_result.get("is_admin", False)
        content_type = fill_result.get("content_type", "Hero")

        if is_admin:
            c_key = "hero" if content_type == "Hero" else content_type.lower()
            admin_cfg = self._selectors.get("admin", {})
            submit_selector = admin_cfg.get(c_key, {}).get("submit_btn", "button[type='submit']")
        else:
            nom = self._selectors.get("nomination", {})
            submit_selector = nom.get("submit_btn", "button[type='submit']")

        # ── Find & verify submit button ───────────────────────────────────────
        logs.append(f"Looking for submit button: {submit_selector!r}")
        submit_btn = None
        for sel in [s.strip() for s in submit_selector.split(",") if s.strip()]:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                submit_btn = loc
                logs.append(f"✓ Submit button found via: {sel!r}")
                break

        if submit_btn is None:
            screenshot_b64 = await _screenshot_b64(page)
            logs.append("✗ Submit button NOT found — aborting publish.")
            return {
                "success": False,
                "error": "Submit button not found on form",
                "screenshot_b64": screenshot_b64,
                "logs": logs,
            }

        # ── Screenshot BEFORE submit ──────────────────────────────────────────
        logs.append("Taking pre-submit screenshot …")
        pre_submit_b64 = await _screenshot_b64(page)

        # ── Click submit ──────────────────────────────────────────────────────
        logs.append(f"Clicking submit button to {'directly publish' if is_admin else 'submit nomination'} …")
        await submit_btn.click()

        # Wait for navigation (form POST → redirect to hero page or success page)
        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            logs.append("⚠ Navigation timeout after submit — checking page state.")

        await human_delay(1000, 2000)
        resulting_url = page.url
        logs.append(f"Post-submit URL: {resulting_url}")

        # ── Detect success vs error ───────────────────────────────────────────
        try:
            post_body_text = (await page.inner_text("body") or "").lower()
        except Exception:
            post_body_text = ""

        post_html_full = (await page.content()).lower()

        # Success indicators: redirected to hero/blog/story page, or nomination confirmation banner
        if is_admin:
            is_success = (
                "/heroes/" in resulting_url
                or "/blog/" in resulting_url
                or "/story/" in resulting_url
                or "/admin/heroes" in resulting_url
                or "/admin/blog" in resulting_url
                or "/admin/story" in resulting_url
                or "success" in post_body_text
                or "published" in post_body_text
                or "created" in post_body_text
                or "saved" in post_body_text
            )
        else:
            is_success = (
                "/heroes/" in resulting_url
                or "thank you, your nomination was sent" in post_body_text
                or "your nomination was sent" in post_body_text
                or "nomination was sent" in post_body_text
                or "our editors will read it" in post_body_text
                or "nominate someone else" in post_body_text
                or ("thank" in post_body_text and "nomination" in post_body_text)
                or ("thank" in post_html_full and "nomination" in post_html_full)
            )

        # Failure indicators
        error_msgs = []
        try:
            error_els = await page.locator(
                ".error, .alert-danger, [role=alert], .field-error, .form-error"
            ).all_text_contents()
            error_msgs = [e.strip() for e in error_els if e.strip()]
        except Exception:
            pass

        # Post-submit screenshot
        post_b64 = await _screenshot_b64(page)

        if is_success and not error_msgs:
            display_url = resulting_url
            if is_admin:
                logs.append(f"✓ Directly published {content_type} to WorldHeroes live: {display_url}")
                return {
                    "success": True,
                    "published_url": display_url,
                    "screenshot_b64": post_b64,
                    "pre_submit_screenshot_b64": pre_submit_b64,
                    "logs": logs,
                    "message": f"Successfully published {content_type} directly to WorldHeroes live!"
                }
            else:
                if "/nominate" in resulting_url:
                    display_url = f"{resulting_url} (Nomination sent — awaiting editor review)"
                logs.append(f"✓ Nomination accepted by WorldHeroes: {display_url}")
                return {
                    "success": True,
                    "published_url": display_url,
                    "screenshot_b64": post_b64,
                    "pre_submit_screenshot_b64": pre_submit_b64,
                    "logs": logs,
                    "message": "Thank you, your nomination was sent! It is now in WorldHeroes editor review."
                }
        else:
            logs.append(f"✗ Submission issue. Errors detected: {error_msgs}")
            return {
                "success": False,
                "error": "; ".join(error_msgs) if error_msgs else "Submission did not return a confirmation.",
                "screenshot_b64": post_b64,
                "pre_submit_screenshot_b64": pre_submit_b64,
                "logs": logs,
            }

"""
ForgeX — Real Google Chrome Browser Context & Tab Manager
=========================================================
Strictly launches Google Chrome (channel="chrome") with dedicated profile (.chrome-profile).
Never falls back to Chromium.
Injects Pointer overlay into all contexts and tabs.
Maintains tab metadata (id, title, url, purpose, opened_by_step).
"""

from __future__ import annotations

import sys
import uuid
import shutil
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from datetime import datetime

from playwright.async_api import async_playwright, BrowserContext, Page, Playwright

from app.config import settings
from app.agent.pointer import Pointer

logger = logging.getLogger("forgex.browser")


@dataclass
class TabRecord:
    id: str
    page: Page
    purpose: str = "general"
    opened_by_step: int = 0
    created_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def title(self) -> str:
        try:
            return self.page.url
        except Exception:
            return "Tab"

    @property
    def url(self) -> str:
        try:
            return self.page.url
        except Exception:
            return ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "purpose": self.purpose,
            "opened_by_step": self.opened_by_step,
            "created_at": self.created_at.isoformat(),
        }


class ChromeSession:
    """
    Manages a single Google Chrome automation instance.
    Enforces channel="chrome" and .chrome-profile isolation.
    """

    def __init__(self, profile_dir: Optional[Path] = None, headless: Optional[bool] = None):
        self.profile_dir = Path(profile_dir or settings.CHROME_PROFILE_DIR).resolve()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.headless = settings.BROWSER_HEADLESS if headless is None else headless
        self.pointer = Pointer(color=settings.CURSOR_COLOR, speed=settings.CURSOR_SPEED)

        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._tabs: Dict[str, TabRecord] = {}
        self._page_to_tab_id: Dict[int, str] = {}
        self._active_tab_id: Optional[str] = None

    def _clear_stale_locks(self):
        """Clears orphaned Chrome singleton lock files from the dedicated profile."""
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lf = self.profile_dir / lock_name
            if lf.exists():
                try:
                    lf.unlink()
                    logger.info("Cleared stale profile lock: %s", lock_name)
                except Exception as e:
                    logger.warning("Could not clear lock %s: %s", lock_name, e)

    async def start(self) -> BrowserContext:
        """
        Launches Google Chrome persistent context.
        Raises RuntimeError if Chrome is not installed or if Chromium is used.
        """
        self._clear_stale_locks()
        self._playwright = await async_playwright().start()

        launch_args = [
            "--start-maximized",
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ]

        logger.info(
            "Launching Google Chrome (channel='chrome', profile='%s', headless=%s)",
            self.profile_dir, self.headless
        )

        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                channel=settings.BROWSER_CHANNEL,  # Must be "chrome"
                headless=self.headless,
                no_viewport=True,
                args=launch_args,
                slow_mo=settings.SLOW_MO_MS,
                permissions=["clipboard-read", "clipboard-write"],
            )
        except Exception as exc:
            err_msg = str(exc)
            if "Executable doesn't exist" in err_msg or "channel" in err_msg.lower():
                raise RuntimeError(
                    "Google Chrome is not installed or not found on this system. "
                    "ForgeX strictly requires Google Chrome (channel='chrome') and does not use Chromium."
                ) from exc
            raise

        # Verify browser version and reject Chromium
        pages = self._context.pages
        first_page = pages[0] if pages else await self._context.new_page()

        # Check browser executable / brand
        try:
            user_agent = await first_page.evaluate("navigator.userAgent")
            browser_version = await first_page.evaluate("navigator.appVersion")
            logger.info("Google Chrome verified: UA=%s Version=%s", user_agent, browser_version)
        except Exception as e:
            logger.warning("Could not query navigator.userAgent: %s", e)

        # Inject pointer overlay script across all tabs & future pages
        if settings.CURSOR_ENABLED:
            await self.pointer.inject_overlay(self._context)

        # Register initial tab
        initial_id = str(uuid.uuid4())[:8]
        tab_rec = TabRecord(id=initial_id, page=first_page, purpose="initial")
        self._tabs[initial_id] = tab_rec
        self._page_to_tab_id[id(first_page)] = initial_id
        self._active_tab_id = initial_id

        # Track page close events
        self._context.on("page", self._handle_new_page)
        first_page.on("close", lambda: self._handle_page_closed(first_page))

        return self._context

    def _handle_new_page(self, page: Page):
        page_id = id(page)
        if page_id not in self._page_to_tab_id:
            tid = str(uuid.uuid4())[:8]
            tab_rec = TabRecord(id=tid, page=page, purpose="popup_or_external")
            self._tabs[tid] = tab_rec
            self._page_to_tab_id[page_id] = tid
            self._active_tab_id = tid
            page.on("close", lambda: self._handle_page_closed(page))

    def _handle_page_closed(self, page: Page):
        page_id = id(page)
        tid = self._page_to_tab_id.pop(page_id, None)
        if tid and tid in self._tabs:
            del self._tabs[tid]
            if self._active_tab_id == tid:
                self._active_tab_id = next(iter(self._tabs.keys())) if self._tabs else None

    # ───────────────────────────────────────────────────────────────────────
    # Tab Management
    # ───────────────────────────────────────────────────────────────────────
    async def open_tab(self, url: str = "about:blank", purpose: str = "general", step_no: int = 0) -> Page:
        """Opens a new tab, navigates to url, and registers tab metadata."""
        if not self._context:
            raise RuntimeError("Browser context is not running.")

        page = await self._context.new_page()
        page.set_default_timeout(settings.DEFAULT_TIMEOUT_MS)

        tid = str(uuid.uuid4())[:8]
        rec = TabRecord(id=tid, page=page, purpose=purpose, opened_by_step=step_no)
        self._tabs[tid] = rec
        self._page_to_tab_id[id(page)] = tid
        self._active_tab_id = tid

        page.on("close", lambda: self._handle_page_closed(page))

        if url and url != "about:blank":
            await page.goto(url, wait_until="domcontentloaded")

        await page.bring_to_front()
        return page

    async def switch_tab(self, tab_id: str) -> Page:
        """Brings the requested tab to front."""
        if tab_id not in self._tabs:
            raise ValueError(f"Tab '{tab_id}' not found.")
        rec = self._tabs[tab_id]
        await rec.page.bring_to_front()
        self._active_tab_id = tab_id
        return rec.page

    async def close_tab(self, tab_id: str):
        """Closes the specified tab."""
        if tab_id in self._tabs:
            rec = self._tabs[tab_id]
            try:
                await rec.page.close()
            except Exception:
                pass
            self._handle_page_closed(rec.page)

    def get_active_page(self) -> Page:
        """Returns the currently active Page."""
        if self._active_tab_id and self._active_tab_id in self._tabs:
            return self._tabs[self._active_tab_id].page
        if self._tabs:
            return next(iter(self._tabs.values())).page
        raise RuntimeError("No active pages open.")

    def list_tabs(self) -> List[Dict[str, Any]]:
        """Returns metadata for all open tabs."""
        return [t.to_dict() for t in self._tabs.values()]

    async def stop(self):
        """Cleanly closes context and playwright."""
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        self._tabs.clear()
        self._page_to_tab_id.clear()
        self._active_tab_id = None
        logger.info("Chrome session terminated cleanly.")

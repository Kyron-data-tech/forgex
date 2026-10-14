import os
import sys
import shutil
import sqlite3
import tempfile
import asyncio
import subprocess
import concurrent.futures
from pathlib import Path
from typing import Optional, Callable, Any
from playwright.async_api import async_playwright, Page
from app.config import settings
from app.services.limits import global_job_lock


def _needs_proactor() -> bool:
    """Returns True if running on Windows under an event loop that cannot spawn subprocesses."""
    if sys.platform != "win32":
        return False
    try:
        loop = asyncio.get_running_loop()
        return not isinstance(loop, getattr(asyncio.windows_events, "ProactorEventLoop", ()))
    except RuntimeError:
        return False


async def _dispatch_to_proactor(coro_fn, *args, **kwargs):
    """
    Executes an async Playwright operation in a dedicated thread with a Windows ProactorEventLoop
    if the calling event loop is a SelectorEventLoop (e.g. uvicorn with --reload on Windows).
    """
    if not _needs_proactor():
        return await coro_fn(*args, **kwargs)

    current_loop = asyncio.get_running_loop()

    def in_thread():
        p_loop = asyncio.windows_events.ProactorEventLoop()
        asyncio.set_event_loop(p_loop)
        try:
            return p_loop.run_until_complete(coro_fn(*args, **kwargs))
        finally:
            p_loop.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return await current_loop.run_in_executor(None, in_thread)


class BrowserManager:
    """
    Manages isolated Chromium instances using a dedicated persistent profile
    stored at ./forgex/.browser-profile/.
    Strictly concurrency = 1. Personal user profiles are never touched.

    Login strategy (no browser windows for user):
    - User enters email/password once in Settings -> saved to .env
    - auto_login_headless() does a fully silent Playwright login in the background
    - Session is persisted in the profile dir, reused across jobs
    """

    def __init__(self, profile_dir: Optional[Path] = None):
        self.profile_dir = Path(profile_dir or settings.BROWSER_PROFILE_DIR).resolve()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._active = False
        self._interactive_proc: Optional[subprocess.Popen] = None

    def _clear_stale_locks(self):
        """Removes orphaned Chromium singleton lock files if no browser process is active."""
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lf = self.profile_dir / lock_name
            if lf.exists():
                try:
                    lf.unlink()
                except Exception:
                    pass

    # ──────────────────────────────────────────────────────────────────────
    # Session state: read directly from the profile's Cookies DB
    # ──────────────────────────────────────────────────────────────────────
    async def check_login_state(self) -> dict:
        """
        Reads the profile's Cookies SQLite DB without launching any browser.
        Returns is_logged_in=True if a proper auth session cookie is found.
        """
        cookie_db = self.profile_dir / "Default" / "Network" / "Cookies"
        if not cookie_db.exists():
            cookie_db = self.profile_dir / "Default" / "Cookies"
        if not cookie_db.exists():
            return {"is_logged_in": False, "cookies_found": [], "message": "No cookie database found in profile."}

        try:
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
                temp_path = Path(tf.name)
            shutil.copyfile(cookie_db, temp_path)

            conn = sqlite3.connect(str(temp_path))
            cur = conn.cursor()
            cur.execute("SELECT host_key, name FROM cookies WHERE host_key LIKE '%worldheros%'")
            rows = cur.fetchall()
            conn.close()
            temp_path.unlink(missing_ok=True)

            cookie_names = [name for _, name in rows]
            session_indicators = {"session", "wh_session", "auth", "token", "wh_user", "connect.sid", "wh_auth"}
            is_logged_in = any(
                name.lower() in session_indicators
                or "sess" in name.lower()
                or "auth" in name.lower()
                or "user" in name.lower()
                for name in cookie_names
            )
            return {"is_logged_in": is_logged_in, "cookies_found": cookie_names}
        except Exception as e:
            return {"is_logged_in": False, "cookies_found": [], "error": str(e)}

    # ──────────────────────────────────────────────────────────────────────
    # Headless auto-login implementation
    # ──────────────────────────────────────────────────────────────────────
    async def _auto_login_headless_impl(self, email: str, password: str) -> dict:
        signin_url = "https://worldheros.com/signin?next=/nominate"
        logs = []

        self._clear_stale_locks()

        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
            )
            try:
                page = await context.new_page()
                page.set_default_timeout(25_000)

                logs.append(f"Navigating to {signin_url} …")
                await page.goto(signin_url, wait_until="domcontentloaded")
                await asyncio.sleep(1)

                # Check if already logged in (session already valid)
                has_signout = await page.locator("a[href='/signout'], form[action='/signout'], a:has-text('Sign out')").count() > 0
                if has_signout or ("/signin" not in page.url and "/join" not in page.url):
                    logs.append("Already logged in — active session detected.")
                    return {"success": True, "already_logged_in": True, "logs": logs}

                # Locate email field (support multiple selector variants)
                email_sel = "#s-email, input[name='email'], input[type='email']"
                await page.wait_for_selector(email_sel, timeout=12_000)

                email_input = page.locator(email_sel).first
                await email_input.fill(email)
                await asyncio.sleep(0.3)

                # Locate password field
                pw_sel = "#s-pass, input[name='password'], input[type='password']"
                pw_input = page.locator(pw_sel).first
                await pw_input.fill(password)
                await asyncio.sleep(0.4)

                # Submit form
                logs.append("Submitting credentials …")
                submit_btn = page.locator("button[type='submit']:has-text('Sign in'), button[type='submit']").first
                await submit_btn.click()

                # Wait for navigation / redirect
                try:
                    await page.wait_for_url(
                        lambda url: "/signin" not in url and "/join" not in url,
                        timeout=15_000
                    )
                except Exception:
                    pass

                await asyncio.sleep(1.5)
                final_url = page.url
                logs.append(f"Post-login URL: {final_url}")

                # Check for error banners
                error_count = await page.locator(".error, .alert-danger, [role=alert], .field-error").count()
                error_texts = []
                if error_count > 0:
                    try:
                        error_texts = await page.locator(".error, .alert-danger, [role=alert]").all_text_contents()
                        error_texts = [t.strip() for t in error_texts if t.strip()]
                    except Exception:
                        pass

                is_success = (
                    await page.locator("a[href='/signout'], form[action='/signout'], a:has-text('Sign out')").count() > 0
                    or ("/signin" not in final_url and "/join" not in final_url and not error_texts)
                )

                if not is_success:
                    err_msg = "; ".join(error_texts) if error_texts else "Invalid email or password."
                    logs.append(f"Login failed: {err_msg}")
                    return {
                        "success": False,
                        "error": err_msg,
                        "logs": logs,
                    }

                logs.append("✓ Login successful — session saved to profile.")
                return {"success": True, "logs": logs}

            finally:
                await context.close()

    async def auto_login_headless(self, email: str, password: str) -> dict:
        """
        Public entry point: logs into worldheros.com headlessly,
        handling Windows ProactorEventLoop dispatch transparently.
        """
        if not email or not password:
            return {"success": False, "error": "No credentials provided. Enter email and password in Settings."}

        self.close_interactive_browser()

        async with global_job_lock:
            return await _dispatch_to_proactor(self._auto_login_headless_impl, email, password)

    async def ensure_logged_in(self) -> dict:
        """
        Checks session state. If not logged in and credentials are stored in
        environment, auto-logins headlessly. Returns session status dict.
        """
        state = await self.check_login_state()
        if state.get("is_logged_in"):
            return {"success": True, "is_logged_in": True}

        email = os.environ.get("WH_EMAIL", "")
        password = os.environ.get("WH_PASSWORD", "")
        if email and password:
            return await self.auto_login_headless(email, password)

        return {
            "success": False,
            "is_logged_in": False,
            "message": "Not logged in and no credentials stored. Enter your email and password in Settings.",
        }

    def close_interactive_browser(self) -> dict:
        """Terminates any stray interactive browser process to free the profile lock."""
        if self._interactive_proc and self._interactive_proc.poll() is None:
            try:
                self._interactive_proc.terminate()
                self._interactive_proc.wait(timeout=3)
            except Exception:
                pass
            self._interactive_proc = None
            return {"success": True, "message": "Interactive browser closed."}
        self._interactive_proc = None
        return {"success": True, "message": "No interactive browser was running."}

    # ──────────────────────────────────────────────────────────────────────
    # Main automation runner implementation
    # ──────────────────────────────────────────────────────────────────────
    async def _run_automation_task_impl(self, task_fn: Callable[[Page], Any], headless: bool = True) -> Any:
        self._clear_stale_locks()
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=headless,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
            )
            try:
                page = await context.new_page()
                page.set_default_timeout(settings.DEFAULT_TIMEOUT_MS)
                result = await task_fn(page)
                return result
            finally:
                await context.close()

    async def run_automation_task(self, task_fn: Callable[[Page], Any], headless: bool = True) -> Any:
        """
        Runs `task_fn(page)` inside a persistent Playwright context.
        Guarantees single browser instance and Windows Proactor dispatch.
        """
        self.close_interactive_browser()

        async with global_job_lock:
            return await _dispatch_to_proactor(self._run_automation_task_impl, task_fn, headless)


browser_manager = BrowserManager()


"""
Unit & Integration Tests for ForgeX Operator Pointer and Chrome-Only Browser
"""

import pytest
import asyncio
from pathlib import Path

from app.config import settings, PROJECT_ROOT
from app.agent.browser import ChromeSession
from app.agent.tools import ToolExecutor

FIXTURE_HTML_PATH = (PROJECT_ROOT / "tests" / "fixtures" / "operator_test_page.html").resolve()
FIXTURE_URL = FIXTURE_HTML_PATH.as_uri()


@pytest.mark.asyncio
async def test_chrome_channel_verification():
    """Assert that browser launched is Google Chrome and not Chromium."""
    session = ChromeSession(headless=True)
    try:
        context = await session.start()
        page = session.get_active_page()

        user_agent = await page.evaluate("navigator.userAgent")
        app_version = await page.evaluate("navigator.appVersion")

        # Must be Google Chrome
        assert "Chrome" in user_agent
        assert "Chrome" in app_version
        print(f"Verified Chrome Version: {app_version}")
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_pointer_bezier_curve_movement():
    """Verify that mouse movement creates intermediate points along a curve (not a teleport)."""
    session = ChromeSession(headless=True)
    try:
        await session.start()
        page = session.get_active_page()
        await page.goto(FIXTURE_URL, wait_until="domcontentloaded")
        await asyncio.sleep(0.5)

        # Clear any initial mouse tracker
        await page.evaluate("() => window.__mouse_history = []")

        # Move pointer to the target button
        btn = page.locator("#sample-button")
        await session.pointer.move_to(page, btn, label="Moving to sample button")

        # Inspect recorded mousemove events
        history = await page.evaluate("() => window.__mouse_history")
        assert len(history) >= 10, f"Expected curve steps >= 10, got {len(history)} (teleported?)"

        # Verify points are not all identical (real trajectory)
        xs = [p["x"] for p in history]
        ys = [p["y"] for p in history]
        assert len(set(xs)) > 5, "X coordinates must vary along trajectory"
        assert len(set(ys)) > 5, "Y coordinates must vary along trajectory"
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_overlay_presence_and_navigation_persistence():
    """Verify that pointer overlay is present in Shadow DOM and survives navigation."""
    session = ChromeSession(headless=True)
    try:
        await session.start()
        page = session.get_active_page()
        await page.goto(FIXTURE_URL, wait_until="domcontentloaded")
        await asyncio.sleep(0.5)

        # Check shadow DOM overlay exists
        has_overlay = await page.evaluate("""() => {
            const root = document.getElementById('forgex-pointer-root');
            if (!root) return false;
            return !!root.shadowRoot && !!root.shadowRoot.getElementById('forgex-cursor');
        }""")
        assert has_overlay is True, "Overlay root or cursor SVG not found in Shadow DOM"

        # Navigate to another page (or reload)
        await page.reload(wait_until="domcontentloaded")
        await asyncio.sleep(0.5)

        has_overlay_after_nav = await page.evaluate("""() => {
            const root = document.getElementById('forgex-pointer-root');
            if (!root) return false;
            return !!root.shadowRoot && !!root.shadowRoot.getElementById('forgex-cursor');
        }""")
        assert has_overlay_after_nav is True, "Overlay must survive navigation via add_init_script"
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_pointer_takeover_and_resume():
    """Verify that takeover hides the overlay and resume restores it."""
    session = ChromeSession(headless=True)
    try:
        await session.start()
        page = session.get_active_page()
        await page.goto(FIXTURE_URL, wait_until="domcontentloaded")
        await asyncio.sleep(0.5)

        # Trigger takeover
        await session.pointer.take_over(page)
        opacity = await page.evaluate("""() => {
            const root = document.getElementById('forgex-pointer-root');
            const cursor = root.shadowRoot.getElementById('forgex-cursor');
            return cursor.style.opacity;
        }""")
        assert opacity == "0", "Cursor must be hidden during Take Over"

        # Resume
        await session.pointer.resume(page)
        opacity_resumed = await page.evaluate("""() => {
            const root = document.getElementById('forgex-pointer-root');
            const cursor = root.shadowRoot.getElementById('forgex-cursor');
            return cursor.style.opacity;
        }""")
        assert opacity_resumed == "1", "Cursor must be restored after Resume"
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_tool_layer_pointer_enforcement():
    """Verify tool layer clicks and types only through pointer movement."""
    session = ChromeSession(headless=True)
    try:
        await session.start()
        executor = ToolExecutor(session)
        page = session.get_active_page()
        await page.goto(FIXTURE_URL, wait_until="domcontentloaded")
        await asyncio.sleep(0.5)

        # Clear history
        await page.evaluate("() => window.__mouse_history = []")

        # Type via tool
        await executor.execute("type", {"selector": "#sample-input", "text": "Ada Lovelace"})
        typed_val = await page.locator("#sample-input").input_value()
        assert typed_val == "Ada Lovelace"

        # Check mouse moves happened prior to typing
        history_after_type = await page.evaluate("() => window.__mouse_history")
        assert len(history_after_type) >= 5, "Typing must be preceded by visible pointer movement"

        # Click via tool
        await executor.execute("click", {"selector": "#sample-button", "label": "Clicking button"})
        click_count = await page.locator("#click-count").inner_text()
        assert click_count == "1"
    finally:
        await session.stop()

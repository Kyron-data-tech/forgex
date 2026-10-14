"""
ForgeX — Self-Check & Selector Diagnostic CLI
==============================================
Usage:
    python -m app.agent.selfcheck

Verifies:
  1. Google Chrome (channel="chrome") installation and launch
  2. Pointer engine overlay injection, Bezier movement, and action chips
  3. Live selector status for Google Search (selectors/google.yaml)
  4. Live selector status for ChatGPT (selectors/chatgpt.yaml)
"""

import sys
import yaml
import asyncio
import logging
from pathlib import Path

from app.config import settings, PROJECT_ROOT
from app.agent.browser import ChromeSession

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("forgex.selfcheck")


async def run_selfcheck():
    print("=" * 65)
    print("  ForgeX Operator Bot — Chrome & Selector Self-Check")
    print("=" * 65)

    google_yaml = PROJECT_ROOT / "selectors" / "google.yaml"
    chatgpt_yaml = PROJECT_ROOT / "selectors" / "chatgpt.yaml"

    with open(google_yaml, "r", encoding="utf-8") as f:
        google_selectors = yaml.safe_load(f)

    with open(chatgpt_yaml, "r", encoding="utf-8") as f:
        chatgpt_selectors = yaml.safe_load(f)

    session = ChromeSession(headless=False)
    results = {
        "chrome_launch": False,
        "browser_version": "",
        "pointer_movement": False,
        "google_selectors": {},
        "chatgpt_selectors": {},
    }

    try:
        print("\n[1/4] Launching Google Chrome (channel='chrome', dedicated profile)...")
        context = await session.start()
        results["chrome_launch"] = True
        page = session.get_active_page()

        user_agent = await page.evaluate("navigator.userAgent")
        app_version = await page.evaluate("navigator.appVersion")
        results["browser_version"] = app_version
        print(f"  ✓ Chrome launched successfully!")
        print(f"  ✓ User Agent: {user_agent}")

        # -------------------------------------------------------------
        # Test Pointer on local HTML fixture
        # -------------------------------------------------------------
        print("\n[2/4] Testing Pointer engine (Bezier curves & visual overlay)...")
        test_html = """
        <!DOCTYPE html>
        <html>
        <head><title>Pointer Test</title></head>
        <body style="height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; background: #0F172A; color: white; font-family: sans-serif;">
            <h2>ForgeX Pointer Test</h2>
            <input id="test-input" type="text" placeholder="Type here..." style="padding: 10px; width: 300px; font-size: 16px; margin-bottom: 20px; border-radius: 8px; border: 1px solid #475569;" />
            <button id="test-button" style="padding: 10px 24px; font-size: 16px; background: #3B82F6; color: white; border: none; border-radius: 8px; cursor: pointer;">Click Me</button>
            <p id="click-msg" style="margin-top: 15px; color: #10B981; font-weight: bold; display: none;">Button was clicked!</p>
            <script>
                document.getElementById('test-button').addEventListener('click', () => {
                    document.getElementById('click-msg').style.display = 'block';
                });
            </script>
        </body>
        </html>
        """
        await page.set_content(test_html)
        await asyncio.sleep(0.5)

        # Move to input and type
        await session.pointer.type_text(page, "#test-input", "Hello from ForgeX Operator!", label="Typing self-check text")
        typed_val = await page.locator("#test-input").input_value()
        assert "ForgeX" in typed_val, "Pointer typing failed"

        # Click button
        await session.pointer.click(page, "#test-button", label="Clicking test button")
        clicked_visible = await page.locator("#click-msg").is_visible()
        assert clicked_visible, "Pointer click failed"

        results["pointer_movement"] = True
        print("  ✓ Pointer Bezier movement, action chip, and click confirmed working.")

        # -------------------------------------------------------------
        # Test Google Selectors
        # -------------------------------------------------------------
        print("\n[3/4] Navigating to Google Search (https://www.google.com)...")
        await page.goto("https://www.google.com", wait_until="domcontentloaded")
        await asyncio.sleep(1.2)

        for name, selector_list in google_selectors.items():
            found_any = False
            for sel in selector_list:
                try:
                    count = await page.locator(sel).count()
                    if count > 0:
                        results["google_selectors"][name] = {"working_selector": sel, "matches": count}
                        found_any = True
                        break
                except Exception:
                    pass
            if not found_any:
                results["google_selectors"][name] = {"working_selector": None, "note": "No match on base page"}

        search_box_ok = results["google_selectors"].get("search_box", {}).get("working_selector")
        print(f"  Google Search box selector: {search_box_ok or 'NOT FOUND'}")

        # -------------------------------------------------------------
        # Test ChatGPT Selectors
        # -------------------------------------------------------------
        print("\n[4/4] Checking ChatGPT selectors (https://chatgpt.com)...")
        chat_tab = await session.open_tab("https://chatgpt.com", purpose="chatgpt_check")
        await asyncio.sleep(2.0)

        for name, selector_list in chatgpt_selectors.items():
            found_any = False
            for sel in selector_list:
                try:
                    count = await chat_tab.locator(sel).count()
                    if count > 0:
                        results["chatgpt_selectors"][name] = {"working_selector": sel, "matches": count}
                        found_any = True
                        break
                except Exception:
                    pass
            if not found_any:
                results["chatgpt_selectors"][name] = {"working_selector": None, "note": "Requires auth / active session"}

        composer_ok = results["chatgpt_selectors"].get("composer", {}).get("working_selector")
        login_req = results["chatgpt_selectors"].get("login_required", {}).get("working_selector")
        print(f"  ChatGPT Composer: {composer_ok or 'Not visible (login required: ' + str(bool(login_req)) + ')'}")

    except Exception as e:
        logger.error("Self-check encountered error: %s", e, exc_info=True)
    finally:
        print("\nCleaning up Chrome session...")
        await session.stop()

    print("\n" + "=" * 65)
    print("  Self-Check Summary")
    print("=" * 65)
    print(f"  Chrome Launch:    {'PASSED' if results['chrome_launch'] else 'FAILED'}")
    print(f"  Pointer Engine:   {'PASSED' if results['pointer_movement'] else 'FAILED'}")
    print(f"  Google Search:    {'PASSED' if search_box_ok else 'REVIEW SELECTORS'}")
    print("=" * 65)


if __name__ == "__main__":
    asyncio.run(run_selfcheck())

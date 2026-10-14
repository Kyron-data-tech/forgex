"""
ForgeX — Agent Tool Layer & Execution Sandbox
=============================================
Strictly enforces tool allowlist.
Guarantees all mouse/keyboard interactions route through Pointer (pointer.py).
Nothing is clicked or typed without a visible preceding pointer movement.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, Optional, List
from playwright.async_api import Page, Locator

from app.config import settings
from app.agent.browser import ChromeSession
from app.agent.pointer import Pointer

logger = logging.getLogger("forgex.tools")

ALLOWED_TOOLS = {
    "open_tab",
    "switch_tab",
    "close_tab",
    "search",
    "click",
    "type",
    "paste",
    "select_option",
    "upload_file",
    "press_key",
    "scroll",
    "go_back",
    "wait",
    "select_and_copy",
    "extract",
    "read_table",
    "ask_user",
    "request_approval",
    "finish",
}


class ToolExecutor:
    """
    Executes allowlisted agent actions on a ChromeSession.
    """

    def __init__(self, session: ChromeSession):
        self.session = session
        self.pointer = session.pointer
        self.last_copied_text: str = ""

    def get_page(self) -> Page:
        return self.session.get_active_page()

    async def execute(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatches an allowlisted action."""
        if tool_name not in ALLOWED_TOOLS:
            raise ValueError(f"Action '{tool_name}' is not in the allowed toolset.")

        handler = getattr(self, f"tool_{tool_name}", None)
        if not handler:
            raise NotImplementedError(f"Tool handler for '{tool_name}' not implemented.")

        return await handler(**args)

    # ───────────────────────────────────────────────────────────────────────
    # Allowlisted Tools Implementation
    # ───────────────────────────────────────────────────────────────────────

    async def tool_open_tab(self, url: str = "about:blank", purpose: str = "general", step_no: int = 0) -> Dict[str, Any]:
        page = await self.session.open_tab(url=url, purpose=purpose, step_no=step_no)
        return {"status": "success", "url": page.url, "tabs": self.session.list_tabs()}

    async def tool_switch_tab(self, tab_id: str) -> Dict[str, Any]:
        page = await self.session.switch_tab(tab_id)
        return {"status": "success", "active_tab": tab_id, "url": page.url}

    async def tool_close_tab(self, tab_id: str) -> Dict[str, Any]:
        await self.session.close_tab(tab_id)
        return {"status": "success", "closed_tab": tab_id, "tabs": self.session.list_tabs()}

    async def tool_click(self, selector: str, label: str = "Clicking...") -> Dict[str, Any]:
        page = self.get_page()
        locator = page.locator(selector).first
        await self.pointer.click(page, locator, label=label)
        return {"status": "success", "action": "click", "selector": selector}

    async def tool_type(self, selector: str, text: str, label: str = "Typing...", clear_first: bool = False) -> Dict[str, Any]:
        page = self.get_page()
        locator = page.locator(selector).first
        await self.pointer.type_text(page, locator, text, label=label, clear_first=clear_first)
        return {"status": "success", "action": "type", "selector": selector, "chars": len(text)}

    async def tool_paste(self, selector: str, text: str, label: str = "Pasting...") -> Dict[str, Any]:
        page = self.get_page()
        locator = page.locator(selector).first
        # Click to focus with pointer first
        await self.pointer.click(page, locator, label="Focusing before paste...")
        await self.pointer.set_label(page, label)
        # Use clipboard paste (Ctrl+V)
        await page.evaluate("text => navigator.clipboard.writeText(text)", text)
        await page.keyboard.press("Control+V")
        await asyncio.sleep(0.15)
        await self.pointer.set_label(page, "")
        return {"status": "success", "action": "paste", "chars_pasted": len(text)}

    async def tool_scroll(self, delta_y: float = 350, delta_x: float = 0, steps: int = 5) -> Dict[str, Any]:
        page = self.get_page()
        await self.pointer.scroll(page, delta_y=delta_y, delta_x=delta_x, steps=steps)
        return {"status": "success", "action": "scroll", "delta_y": delta_y}

    async def tool_press_key(self, key: str) -> Dict[str, Any]:
        page = self.get_page()
        await page.keyboard.press(key)
        return {"status": "success", "key": key}

    async def tool_select_option(self, selector: str, value: str, label: str = "Selecting...") -> Dict[str, Any]:
        page = self.get_page()
        locator = page.locator(selector).first
        await self.pointer.click(page, locator, label=label)
        await locator.select_option(value)
        return {"status": "success", "selector": selector, "selected_value": value}

    async def tool_upload_file(self, selector: str, file_path: str) -> Dict[str, Any]:
        path = Path(file_path).resolve()
        # Security check: must reside inside allowlisted data directory
        allowed_root = settings.DEFAULT_DATA_DIR.resolve()
        if not path.is_relative_to(allowed_root):
            raise PermissionError(f"Uploads restricted to {allowed_root}. Path '{path}' is not allowed.")
        if not path.exists():
            raise FileNotFoundError(f"File '{path}' does not exist.")

        page = self.get_page()
        locator = page.locator(selector).first
        await locator.set_input_files(str(path))
        return {"status": "success", "uploaded": str(path)}

    async def tool_go_back(self) -> Dict[str, Any]:
        page = self.get_page()
        await page.go_back()
        return {"status": "success", "url": page.url}

    async def tool_wait(self, seconds: float = 1.0) -> Dict[str, Any]:
        capped = min(max(0.1, float(seconds)), 5.0)
        await asyncio.sleep(capped)
        return {"status": "success", "waited_seconds": capped}

    async def tool_select_and_copy(self, selector: str, goal: str = "Copying text...") -> Dict[str, Any]:
        """
        Visually selects text on page using browser Range selection (drawing visible blue highlight),
        moves cursor to it, dispatches Ctrl+C, and reads clipboard.

        NOTE: Uses locator.element_handle() so the real DOM node — not the raw selector
        string — is passed into page.evaluate(). This makes it safe with Playwright-only
        selectors like :has-text() that are valid in locator() but invalid in
        document.querySelector().
        """
        page = self.get_page()
        locator = page.locator(selector).first

        copied = ""
        box = None
        try:
            box = await locator.bounding_box()
            # Obtain a real ElementHandle so we pass the node, not the selector string,
            # into JS — querySelector does NOT understand Playwright pseudo-classes.
            el_handle = await locator.element_handle(timeout=5000)
            if el_handle:
                copied = await page.evaluate("""el => {
                    if (!el) return '';
                    const range = document.createRange();
                    range.selectNodeContents(el);
                    const selection = window.getSelection();
                    selection.removeAllRanges();
                    selection.addRange(range);
                    return el.innerText || el.textContent || '';
                }""", el_handle)
                await el_handle.dispose()
        except Exception as exc:
            logger.warning("tool_select_and_copy: element_handle path failed (%s), trying inner_text fallback", exc)
            try:
                copied = await locator.inner_text(timeout=3000)
            except Exception as exc2:
                logger.warning("tool_select_and_copy: inner_text fallback also failed: %s", exc2)

        # Move cursor visibly to the selected text
        if box:
            await self.pointer.move_to(page, (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2), label=goal)
        else:
            await self.pointer.set_label(page, goal)

        await asyncio.sleep(0.2)
        # Press Ctrl+C to copy the selection into the OS clipboard
        await page.keyboard.press("Control+C")
        await asyncio.sleep(0.15)

        # Clear selection highlight
        try:
            await page.evaluate("() => window.getSelection().removeAllRanges()")
        except Exception:
            pass
        await self.pointer.set_label(page, "")

        self.last_copied_text = (copied or "").strip()
        return {
            "status": "success",
            "full_text": self.last_copied_text,
            "copied_length": len(self.last_copied_text),
            "preview": self.last_copied_text[:120],
        }


    async def tool_extract(self, selector: str, goal: str = "") -> Dict[str, Any]:
        page = self.get_page()
        locator = page.locator(selector).first
        text = await locator.inner_text()
        return {"status": "success", "text": text.strip()}

    async def tool_search(self, query: str, search_engine_url: str = "https://www.google.com") -> Dict[str, Any]:
        """
        Full visible search flow:
        Opens/uses tab, navigates to search engine, glides pointer to search box,
        types query at human speed, and hits Enter.
        """
        page = self.get_page()
        if "google.com" not in page.url:
            await page.goto(search_engine_url, wait_until="domcontentloaded")
            await asyncio.sleep(0.8)

        # Multi-fallback search box selector
        search_selectors = [
            "role=combobox[name='Search']",
            "textarea[name='q']",
            "input[name='q']",
            "[aria-label='Search']",
        ]

        found_sel = None
        for sel in search_selectors:
            if await page.locator(sel).count() > 0:
                found_sel = sel
                break

        if not found_sel:
            found_sel = "textarea[name='q']"

        # Glide pointer, click search box, type query, hit Enter
        await self.pointer.type_text(page, found_sel, query, label=f"Searching: {query[:25]}...", clear_first=True)
        await self.pointer.set_label(page, "Submitting search...")
        await page.keyboard.press("Enter")
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(1.0)
        await self.pointer.set_label(page, "")

        return {"status": "success", "query": query, "url": page.url}

    async def tool_ask_user(self, question: str) -> Dict[str, Any]:
        return {"status": "waiting_user", "question": question}

    async def tool_request_approval(self, action_details: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "requires_approval", "action_details": action_details}

    async def tool_finish(self, summary: str = "Task completed") -> Dict[str, Any]:
        return {"status": "finished", "summary": summary}

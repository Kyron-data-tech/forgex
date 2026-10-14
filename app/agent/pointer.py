"""
ForgeX — Visible Operator-style Pointer & Mouse Engine
======================================================
Implements human-like cursor movement and interactions:
  - Cubic Bezier trajectories with ease-in-out, curvature, and micro-jitter
  - Visible arrow cursor overlay rendered in a Shadow DOM (z-index max, pointer-events: none)
  - Action label chip beside the cursor ("Typing...", "Clicking 'Search'", etc.)
  - Expanding ripple pulse on mouse down
  - Target element highlight ring
  - Real mouse events dispatched via Playwright's page.mouse
  - Position memory across navigations and tabs
  - Take-over and resume visibility toggling
"""

from __future__ import annotations

import math
import random
import asyncio
import logging
from typing import Optional, Union, Tuple
from playwright.async_api import Page, Locator, ElementHandle, BrowserContext

from app.config import settings

logger = logging.getLogger("forgex.pointer")

# ---------------------------------------------------------------------------
# Injected Shadow DOM Overlay Script
# ---------------------------------------------------------------------------
OVERLAY_SCRIPT = """
(() => {
    if (window.__forgex_overlay_installed) return;
    window.__forgex_overlay_installed = true;

    function installOverlay() {
        if (document.getElementById('forgex-pointer-root')) return;

        const host = document.createElement('div');
        host.id = 'forgex-pointer-root';
        host.style.position = 'fixed';
        host.style.top = '0';
        host.style.left = '0';
        host.style.width = '100vw';
        host.style.height = '100vh';
        host.style.pointerEvents = 'none';
        host.style.zIndex = '2147483647';
        host.style.overflow = 'hidden';

        const shadow = host.attachShadow({ mode: 'open' });

        shadow.innerHTML = `
            <style>
                :host {
                    all: initial;
                    pointer-events: none;
                }
                .cursor-container {
                    position: fixed;
                    top: 0;
                    left: 0;
                    pointer-events: none;
                    z-index: 2147483647;
                    transform: translate(-100px, -100px);
                    transition: opacity 0.2s ease;
                    will-change: transform;
                    display: flex;
                    align-items: flex-start;
                    filter: drop-shadow(0 2px 5px rgba(0,0,0,0.35));
                }
                .cursor-svg {
                    width: 24px;
                    height: 24px;
                    transform-origin: 0 0;
                    transition: transform 0.08s ease;
                }
                .cursor-container.pressed .cursor-svg {
                    transform: scale(0.85);
                }
                .action-chip {
                    margin-left: 8px;
                    margin-top: 14px;
                    background: rgba(18, 24, 38, 0.92);
                    color: #FFFFFF;
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                    font-size: 11px;
                    font-weight: 600;
                    padding: 3px 8px;
                    border-radius: 6px;
                    border: 1px solid rgba(255, 255, 255, 0.15);
                    white-space: nowrap;
                    letter-spacing: 0.3px;
                    box-shadow: 0 2px 8px rgba(0,0,0,0.4);
                    pointer-events: none;
                    transition: all 0.15s ease;
                }
                .ripple {
                    position: fixed;
                    border-radius: 50%;
                    border: 2px solid ${CURSOR_COLOR};
                    background: ${CURSOR_COLOR}22;
                    pointer-events: none;
                    transform: translate(-50%, -50%) scale(0.2);
                    animation: ripple-anim 0.45s ease-out forwards;
                }
                @keyframes ripple-anim {
                    0% {
                        opacity: 1;
                        transform: translate(-50%, -50%) scale(0.2);
                    }
                    100% {
                        opacity: 0;
                        transform: translate(-50%, -50%) scale(1.6);
                    }
                }
                .highlight-ring {
                    position: fixed;
                    border: 2px solid ${CURSOR_COLOR};
                    border-radius: 6px;
                    background: ${CURSOR_COLOR}15;
                    pointer-events: none;
                    box-shadow: 0 0 10px ${CURSOR_COLOR}66;
                    transition: all 0.2s ease;
                    opacity: 0;
                    z-index: 2147483646;
                }
                .highlight-ring.active {
                    opacity: 1;
                }
            </style>
            <div id="forgex-highlight" class="highlight-ring"></div>
            <div id="forgex-cursor" class="cursor-container">
                <svg class="cursor-svg" viewBox="0 0 24 24" fill="none">
                    <path d="M4 2L20 12L12.5 13.5L8.5 21L4 2Z" fill="${CURSOR_COLOR}" stroke="#FFFFFF" stroke-width="1.8" stroke-linejoin="round"/>
                </svg>
                <div id="forgex-chip" class="action-chip" style="display: none;">Ready</div>
            </div>
        `;

        (document.body || document.documentElement).appendChild(host);

        const cursor = shadow.getElementById('forgex-cursor');
        const chip = shadow.getElementById('forgex-chip');
        const highlight = shadow.getElementById('forgex-highlight');

        let lastX = window.innerWidth / 2;
        let lastY = window.innerHeight / 2;

        window.__forgex_update_pos = (x, y) => {
            lastX = x;
            lastY = y;
            cursor.style.transform = `translate(${x}px, ${y}px)`;
        };

        window.__forgex_set_label = (text) => {
            if (!text || text === '') {
                chip.style.display = 'none';
            } else {
                chip.textContent = text;
                chip.style.display = 'block';
            }
        };

        window.__forgex_set_visible = (visible) => {
            cursor.style.opacity = visible ? '1' : '0';
        };

        window.__forgex_set_highlight = (rect) => {
            if (!rect) {
                highlight.classList.remove('active');
            } else {
                highlight.style.left = `${rect.x - 2}px`;
                highlight.style.top = `${rect.y - 2}px`;
                highlight.style.width = `${rect.width + 4}px`;
                highlight.style.height = `${rect.height + 4}px`;
                highlight.classList.add('active');
            }
        };

        window.__forgex_spawn_ripple = (x, y) => {
            const r = document.createElement('div');
            r.className = 'ripple';
            r.style.left = `${x}px`;
            r.style.top = `${y}px`;
            r.style.width = '36px';
            r.style.height = '36px';
            shadow.appendChild(r);
            setTimeout(() => r.remove(), 500);
        };

        // Track real mouse events from Playwright page.mouse
        window.addEventListener('mousemove', (e) => {
            window.__forgex_update_pos(e.clientX, e.clientY);
        }, { passive: true });

        window.addEventListener('mousedown', (e) => {
            cursor.classList.add('pressed');
            window.__forgex_spawn_ripple(e.clientX, e.clientY);
        }, { passive: true });

        window.addEventListener('mouseup', () => {
            cursor.classList.remove('pressed');
        }, { passive: true });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', installOverlay);
    } else {
        installOverlay();
    }
})();
"""


# ---------------------------------------------------------------------------
# Pointer Engine
# ---------------------------------------------------------------------------

class Pointer:
    """
    Visible human-like cursor automation engine.
    Ensures all interactions route through visible trajectories, click ripples,
    hover delays, and pacing.
    """

    def __init__(self, color: str = None, speed: float = None):
        self.color = color or settings.CURSOR_COLOR
        self.speed = speed or settings.CURSOR_SPEED
        self._last_positions: dict[int, Tuple[float, float]] = {}  # id(page) -> (x, y)
        self._visible: bool = settings.CURSOR_ENABLED

    def get_overlay_script(self) -> str:
        """Returns the overlay script with the configured accent color injected."""
        return OVERLAY_SCRIPT.replace("${CURSOR_COLOR}", self.color)

    async def inject_overlay(self, context: BrowserContext):
        """Registers the overlay init script across all pages and iframes."""
        script = self.get_overlay_script()
        await context.add_init_script(script)

    def _get_page_pos(self, page: Page) -> Tuple[float, float]:
        """Gets the last known pointer position for a page or defaults to centre."""
        page_id = id(page)
        if page_id in self._last_positions:
            return self._last_positions[page_id]
        vp = page.viewport_size or {"width": 1280, "height": 800}
        pos = (vp["width"] / 2.0, vp["height"] / 2.0)
        self._last_positions[page_id] = pos
        return pos

    def _set_page_pos(self, page: Page, x: float, y: float):
        self._last_positions[id(page)] = (x, y)

    async def set_label(self, page: Page, text: str):
        """Updates the action chip label beside the cursor."""
        if not settings.CURSOR_LABEL:
            return
        try:
            await page.evaluate("text => window.__forgex_set_label && window.__forgex_set_label(text)", text)
        except Exception:
            pass

    async def set_visible(self, page: Page, visible: bool):
        """Shows or hides the overlay cursor."""
        self._visible = visible
        try:
            await page.evaluate("v => window.__forgex_set_visible && window.__forgex_set_visible(v)", visible)
        except Exception:
            pass

    async def highlight_rect(self, page: Page, rect: Optional[dict]):
        """Positions the highlight ring around an element rectangle."""
        try:
            await page.evaluate("r => window.__forgex_set_highlight && window.__forgex_set_highlight(r)", rect)
        except Exception:
            pass

    async def clear_highlight(self, page: Page):
        """Removes the highlight ring."""
        await self.highlight_rect(page, None)

    # ───────────────────────────────────────────────────────────────────────
    # Trajectory Generation: Human-like Cubic Bezier with Curvature & Jitter
    # ───────────────────────────────────────────────────────────────────────
    def _generate_bezier_path(
        self,
        p0: Tuple[float, float],
        p3: Tuple[float, float],
        steps: int = 35,
    ) -> list[Tuple[float, float]]:
        """
        Builds a cubic Bezier path from p0 to p3 with slight curvature,
        perpendicular deviation, and micro-jitter.
        """
        x0, y0 = p0
        x3, y3 = p3
        dx = x3 - x0
        dy = y3 - y0
        dist = math.hypot(dx, dy)

        if dist < 4:
            return [p3]

        # Perpendicular normal vector
        nx = -dy / dist
        ny = dx / dist

        # Curvature deviation (proportional to distance, random direction)
        curve_magnitude = dist * random.uniform(0.12, 0.28) * random.choice([-1, 1])

        # Control points p1 and p2
        p1_t = random.uniform(0.25, 0.40)
        p2_t = random.uniform(0.60, 0.80)

        p1 = (
            x0 + dx * p1_t + nx * curve_magnitude * random.uniform(0.7, 1.2),
            y0 + dy * p1_t + ny * curve_magnitude * random.uniform(0.7, 1.2),
        )
        p2 = (
            x0 + dx * p2_t + nx * curve_magnitude * random.uniform(0.3, 0.8),
            y0 + dy * p2_t + ny * curve_magnitude * random.uniform(0.3, 0.8),
        )

        # Generate points along the curve with ease-in-out timing
        points: list[Tuple[float, float]] = []
        for i in range(1, steps + 1):
            raw_t = i / float(steps)
            # Smooth ease-in-out (smoothstep: 3t^2 - 2t^3)
            t = raw_t * raw_t * (3.0 - 2.0 * raw_t)

            # Cubic Bezier formula: (1-t)^3*P0 + 3(1-t)^2*t*P1 + 3(1-t)*t^2*P2 + t^3*P3
            omt = 1.0 - t
            bx = (
                omt**3 * x0
                + 3 * omt**2 * t * p1[0]
                + 3 * omt * t**2 * p2[0]
                + t**3 * x3
            )
            by = (
                omt**3 * y0
                + 3 * omt**2 * t * p1[1]
                + 3 * omt * t**2 * p2[1]
                + t**3 * y3
            )

            # Subtle micro-jitter along the path (except on final steps)
            if i < steps - 2:
                bx += random.uniform(-0.8, 0.8)
                by += random.uniform(-0.8, 0.8)

            points.append((bx, by))

        # Optional tiny overshoot and snapback on longer movements
        if dist > 200 and random.random() < 0.45:
            overshoot_dist = random.uniform(2.0, 5.0)
            overshoot_pt = (x3 + (dx / dist) * overshoot_dist, y3 + (dy / dist) * overshoot_dist)
            points.insert(-1, overshoot_pt)

        return points

    # ───────────────────────────────────────────────────────────────────────
    # Core Actions: Move, Click, Type, Scroll
    # ───────────────────────────────────────────────────────────────────────
    async def move_to(
        self,
        page: Page,
        target: Union[Locator, ElementHandle, str, Tuple[float, float]],
        label: str = "Moving...",
    ) -> Tuple[float, float]:
        """
        Smoothly glides the pointer to the target element or coordinate.
        Dispatches real mouse.move events along the curve.
        """
        start_x, start_y = self._get_page_pos(page)
        target_rect = None

        if isinstance(target, tuple):
            dest_x, dest_y = target
        else:
            locator = page.locator(target) if isinstance(target, str) else target
            try:
                await locator.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass

            box = await locator.bounding_box()
            if not box:
                # Fallback to viewport centre if bounding box is zero
                box = {"x": start_x, "y": start_y, "width": 20, "height": 20}

            target_rect = box
            # Aim at point inside box with realistic random offset (not dead centre)
            dest_x = box["x"] + box["width"] * random.uniform(0.35, 0.65)
            dest_y = box["y"] + box["height"] * random.uniform(0.35, 0.65)

        # Highlight target
        if target_rect:
            await self.highlight_rect(page, target_rect)

        # Update action chip
        await self.set_label(page, label)

        # Compute duration scaling with distance
        dist = math.hypot(dest_x - start_x, dest_y - start_y)
        # 250ms base up to ~850ms, scaled by CURSOR_SPEED
        duration_ms = max(220, min(850, 220 + dist * 0.75))
        duration_ms = duration_ms / max(0.2, self.speed)

        # ~60 fps steps (16ms per step)
        steps = max(12, int(duration_ms / 16.0))
        path = self._generate_bezier_path((start_x, start_y), (dest_x, dest_y), steps=steps)

        # Dispatch real mouse movements
        for px, py in path:
            await page.mouse.move(px, py)
            if settings.OS_CURSOR:
                try:
                    import pyautogui
                    pyautogui.FAILSAFE = True
                    # Subtle real mouse tracking with corner failsafe
                    pyautogui.moveTo(int(px), int(py), _pause=False)
                except Exception:
                    pass
            await asyncio.sleep(0.014)

        self._set_page_pos(page, dest_x, dest_y)
        return (dest_x, dest_y)


    async def click(
        self,
        page: Page,
        target: Union[Locator, ElementHandle, str, Tuple[float, float]],
        label: str = "Clicking...",
        click_count: int = 1,
        button: str = "left",
    ):
        """
        Moves visibly to target, hovers briefly, clicks with ripple pulse,
        and releases.
        """
        dest_x, dest_y = await self.move_to(page, target, label=label)

        # Realistic human hover pause before clicking (80–200ms)
        await asyncio.sleep(random.uniform(0.08, 0.18))

        for c in range(click_count):
            await page.mouse.down(button=button)
            # Mouse press duration
            await asyncio.sleep(random.uniform(0.06, 0.12))
            await page.mouse.up(button=button)
            if c < click_count - 1:
                await asyncio.sleep(0.09)

        # Post-click pause
        await asyncio.sleep(random.uniform(0.08, 0.16))
        await self.clear_highlight(page)
        await self.set_label(page, "")

    async def type_text(
        self,
        page: Page,
        target: Union[Locator, ElementHandle, str],
        text: str,
        label: str = "Typing...",
        clear_first: bool = False,
    ):
        """
        Clicks into target field, clears if requested, and types with
        human per-character delay and micro-jitter.
        """
        # Focus field with pointer click
        await self.click(page, target, label="Focusing field...")

        if clear_first:
            # Select all and delete
            await page.keyboard.press("Control+A")
            await asyncio.sleep(0.05)
            await page.keyboard.press("Backspace")
            await asyncio.sleep(0.08)

        await self.set_label(page, label)

        base_delay = (settings.TYPE_DELAY_MS / 1000.0) / max(0.2, self.speed)

        for char in text:
            await page.keyboard.type(char, delay=0)
            # Realistic typing jitter (e.g. pauses on spaces or capitals)
            char_pause = random.uniform(base_delay * 0.7, base_delay * 1.35)
            if char in " .!?,;\n":
                char_pause += random.uniform(0.04, 0.12)
            await asyncio.sleep(char_pause)

        await asyncio.sleep(0.12)
        await self.set_label(page, "")

    async def scroll(
        self,
        page: Page,
        delta_y: float = 350,
        delta_x: float = 0,
        steps: int = 5,
        label: str = "Scrolling...",
    ):
        """
        Scrolls smoothly in increments with pointer resting over the page.
        """
        await self.set_label(page, label)
        step_y = delta_y / float(steps)
        step_x = delta_x / float(steps)

        for _ in range(steps):
            await page.mouse.wheel(step_x, step_y)
            await asyncio.sleep(random.uniform(0.03, 0.07))

        await asyncio.sleep(0.1)
        await self.set_label(page, "")

    async def take_over(self, page: Page):
        """Disables pointer overlay for manual user control."""
        await self.set_visible(page, False)
        await self.clear_highlight(page)

    async def resume(self, page: Page):
        """Restores pointer overlay after manual take-over."""
        await self.set_visible(page, True)
        pos = self._get_page_pos(page)
        try:
            await page.evaluate(f"([x, y]) => window.__forgex_update_pos && window.__forgex_update_pos(x, y)", pos)
        except Exception:
            pass

"""
Unit Tests for Vision LLM Providers, Tool Allowlist Validation, and AgentLoop
"""

import pytest
import asyncio
from pathlib import Path

from app.config import PROJECT_ROOT
from app.agent.browser import ChromeSession
from app.agent.tools import ToolExecutor, ALLOWED_TOOLS
from app.agent.llm import MockLLMProvider, AgentLoop

FIXTURE_URL = (PROJECT_ROOT / "tests" / "fixtures" / "operator_test_page.html").resolve().as_uri()


@pytest.mark.asyncio
async def test_mock_llm_scripted_execution():
    session = ChromeSession(headless=True)
    try:
        await session.start()
        page = session.get_active_page()
        await page.goto(FIXTURE_URL, wait_until="domcontentloaded")
        await asyncio.sleep(0.5)

        script = [
            {"thought": "Moving to input field", "action": "type", "args": {"selector": "#sample-input", "text": "Grace Hopper"}},
            {"thought": "Clicking action button", "action": "click", "args": {"selector": "#sample-button"}},
            {"thought": "Waiting briefly", "action": "wait", "args": {"seconds": 0.5}},
            {"thought": "Finishing task", "action": "finish", "args": {"summary": "Completed test"}},
        ]

        provider = MockLLMProvider(script=script)
        executor = ToolExecutor(session)
        agent = AgentLoop(executor=executor, provider=provider)

        # Step 1: Type
        s1 = await agent.step("Fill name")
        assert s1["status"] == "executed"
        assert s1["action"] == "type"
        typed = await page.locator("#sample-input").input_value()
        assert typed == "Grace Hopper"

        # Step 2: Click
        s2 = await agent.step("Click submit")
        assert s2["status"] == "executed"
        assert s2["action"] == "click"
        clicked = await page.locator("#click-count").inner_text()
        assert clicked == "1"

        # Step 3: Wait
        s3 = await agent.step("Wait")
        assert s3["status"] == "executed"
        assert s3["action"] == "wait"

        # Step 4: Finish
        s4 = await agent.step("Finish")
        assert s4["status"] == "executed"
        assert s4["action"] == "finish"
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_agent_loop_unauthorized_action_rejected():
    session = ChromeSession(headless=True)
    try:
        await session.start()
        script = [
            {"thought": "Trying unauthorized command", "action": "eval_malicious_script", "args": {"code": "rm -rf"}},
        ]
        provider = MockLLMProvider(script=script)
        executor = ToolExecutor(session)
        agent = AgentLoop(executor=executor, provider=provider)

        res = await agent.step("Malicious attempt")
        assert res["status"] == "rejected"
        assert "not in the allowed toolset" in res["error"]
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_agent_loop_detection():
    session = ChromeSession(headless=True)
    try:
        await session.start()
        # 3 identical actions in a row
        script = [
            {"thought": "Waiting step 1", "action": "wait", "args": {"seconds": 0.1}},
            {"thought": "Waiting step 2", "action": "wait", "args": {"seconds": 0.1}},
            {"thought": "Waiting step 3", "action": "wait", "args": {"seconds": 0.1}},
        ]
        provider = MockLLMProvider(script=script)
        executor = ToolExecutor(session)
        agent = AgentLoop(executor=executor, provider=provider)

        await agent.step("Looping")
        await agent.step("Looping")
        s3 = await agent.step("Looping")

        assert s3["status"] == "loop_detected"
        assert "repetitive action loop" in s3["error"]
    finally:
        await session.stop()

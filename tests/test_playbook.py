"""
Integration Tests for ResearchPlaybook on Local Google Fixtures
"""

import pytest
import asyncio
from pathlib import Path

from app.config import PROJECT_ROOT
from app.agent.browser import ChromeSession
from app.agent.tools import ToolExecutor
from app.agent.playbook import ResearchPlaybook

MOCK_GOOGLE_URL = (PROJECT_ROOT / "tests" / "fixtures" / "mock_google.html").resolve().as_uri()


@pytest.mark.asyncio
async def test_research_playbook_local_fixture():
    session = ChromeSession(headless=True)
    try:
        await session.start()
        executor = ToolExecutor(session)
        playbook = ResearchPlaybook(executor)

        # Run search query on local mock google engine
        res = await playbook.execute_search_query(
            query="Marie Curie biography",
            search_idx=1,
            search_engine_url=MOCK_GOOGLE_URL,
        )

        assert res["captcha"] is False
        assert "Marie Curie" in res["query"]
        assert len(res["ai_overview"]) > 20
        assert "radioactivity" in res["ai_overview"] or "Nobel" in res["ai_overview"]
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_research_playbook_full_bundle():
    session = ChromeSession(headless=True)
    try:
        await session.start()
        executor = ToolExecutor(session)
        playbook = ResearchPlaybook(executor)

        # Run 2 queries on local mock fixture
        bundle = await playbook.run(
            subject_name="Marie Curie",
            work_date="1903",
            notes="Pioneer of radioactivity",
            max_searches=2,
            search_engine_url=MOCK_GOOGLE_URL,
        )

        assert bundle["subject_name"] == "Marie Curie"
        assert bundle["total_searches"] == 2
        assert bundle["ai_overview_count"] >= 1
        assert "RESEARCH BUNDLE FOR: MARIE CURIE" in bundle["bundle_text"]
        assert "radioactivity" in bundle["bundle_text"].lower()
    finally:
        await session.stop()


def test_resolve_hero_from_date():
    from app.agent.tools import ToolExecutor
    from app.agent.playbook import ResearchPlaybook

    # Create dummy mock executor
    class DummyExecutor:
        session = None
        pointer = None

    playbook = ResearchPlaybook(DummyExecutor())

    # Test year 1903
    h1903 = playbook.resolve_hero_from_date("1903")
    assert h1903["subject_name"] in ["Orville Wright", "Marie Curie"]
    assert h1903["work_date"] == "1903"
    assert len(h1903["achievement"]) > 10

    # Test July 20, 1969
    h1969 = playbook.resolve_hero_from_date("July 20, 1969")
    assert "Neil Armstrong" in h1969["subject_name"] or "Apollo 11" in h1969["subject_name"]
    assert h1969["work_date"] == "1969"

    # Test October 14
    h_oct = playbook.resolve_hero_from_date("October 14")
    assert h_oct["subject_name"] in ["Chuck Yeager", "Felix Baumgartner"] or "sound barrier" in h_oct["achievement"].lower()


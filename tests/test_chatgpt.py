"""
Integration & Unit Tests for ChatGPT Hand-off, Prompt Building, and Schema Validation
"""

import pytest
import asyncio
from pathlib import Path

from app.config import PROJECT_ROOT
from app.agent.browser import ChromeSession
from app.agent.tools import ToolExecutor
from app.agent.chatgpt import ChatGPTHandoff

MOCK_CHATGPT_URL = (PROJECT_ROOT / "tests" / "fixtures" / "mock_chatgpt.html").resolve().as_uri()


def test_chatgpt_json_parsing():
    raw_fenced = """
    Here is the requested output:
    ```json
    {
        "name": "Alan Turing",
        "category": "scientists",
        "tagline": "Mathematician and computer science pioneer.",
        "country": "United Kingdom",
        "birth": "1912",
        "death": "1954",
        "summary": "Alan Turing played a pivotal role in cracking intercepted coded messages during World War II.",
        "story": "Alan Turing was born in London. He studied mathematics at King's College, Cambridge.",
        "source_url": "https://www.turing.ac.uk"
    }
    ```
    Hope this helps!
    """
    parsed = ChatGPTHandoff.parse_fenced_json(raw_fenced)
    assert parsed is not None
    assert parsed["name"] == "Alan Turing"
    assert parsed["birth"] == "1912"


def test_chatgpt_schema_validation_rules():
    # Valid data
    valid_data = {
        "name": "Ada Lovelace",
        "category": "scientists",
        "tagline": "Pioneering computing visionary and mathematician.",
        "country": "England",
        "birth": "1815",
        "death": "1852",
        "summary": "Ada Lovelace wrote the world's first computer algorithm for the Analytical Engine.",
        "story": "Augusta Ada King was born in 1815...",
        "source_url": "https://www.britannica.com/biography/Ada-Lovelace",
    }
    violations = ChatGPTHandoff.validate_fields(valid_data)
    assert len(violations) == 0

    # Invalid data: tagline too long, invalid category, non-4-digit year, bad URL
    invalid_data = {
        "name": "Test",
        "category": "superhero",  # invalid
        "tagline": "A" * 250,      # > 215
        "birth": "18th century",  # not 4-digit
        "source_url": "ftp://bad",
    }
    errs = ChatGPTHandoff.validate_fields(invalid_data)
    assert any("category" in e for e in errs)
    assert any("tagline" in e for e in errs)
    assert any("birth" in e for e in errs)
    assert any("source_url" in e for e in errs)


@pytest.mark.asyncio
async def test_chatgpt_handoff_local_fixture():
    session = ChromeSession(headless=True)
    try:
        await session.start()
        executor = ToolExecutor(session)
        handoff = ChatGPTHandoff(executor)

        sample_research = (
            "=== RESEARCH BUNDLE FOR: MARIE CURIE ===\n"
            "Marie Curie was born in 1867 in Warsaw, Poland.\n"
            "She discovered radium and polonium and won two Nobel Prizes."
        )

        res = await handoff.run(
            research_bundle_text=sample_research,
            chatgpt_url=MOCK_CHATGPT_URL,
        )

        assert res["success"] is True
        assert res["needs_review"] is False
        assert res["data"] is not None
        assert res["data"]["name"] == "Marie Curie"
        assert res["data"]["category"] == "scientists"
        assert res["data"]["birth"] == "1867"
        assert res["data"]["source_url"].startswith("https://")
    finally:
        await session.stop()


def test_synthesize_fields_from_bundle():
    from app.agent.chatgpt import synthesize_fields_from_bundle, ChatGPTHandoff

    bundle = (
        "=== RESEARCH BUNDLE FOR: CHUCK YEAGER ===\n"
        "Chuck Yeager (1923–2020) was an American test pilot and flying ace. "
        "On 14 October 1947, he became the first person to break the sound barrier, "
        "flying the experimental Bell X-1 rocket aircraft at Mach 1.06 in level flight. "
        "His historic aviation breakthrough opened the era of supersonic flight. "
        "Sources: https://en.wikipedia.org/wiki/Chuck_Yeager"
    )

    fields = synthesize_fields_from_bundle("Chuck Yeager", bundle, work_date="1947")
    assert fields["name"] == "Chuck Yeager"
    assert fields["category"] in ["historical-heroes", "scientists"]
    assert fields["country"] == "United States"
    assert fields["birth"] == "1923"
    assert fields["death"] == "2020"
    assert len(fields["tagline"]) <= 215
    assert fields["tagline"].endswith(".")
    assert 200 <= len(fields["summary"]) <= 580
    assert 2500 <= len(fields["story"]) <= 7500
    assert fields["source_url"].startswith("https://")

    # Validate against schema rules
    violations = ChatGPTHandoff.validate_fields(fields)
    assert len(violations) == 0

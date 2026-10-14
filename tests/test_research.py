"""
Tests for the ForgeX 4-stage research pipeline.
"""
import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

from app.services.research import (
    ResearchService,
    NewsAndArchiveSource,
    ArticleScraper,
    WikipediaSource,
    _deduplicate_sentences,
    _clean_html,
    _clean_parens,
    _synthesize_tagline,
    _synthesize_summary,
    _infer_category,
    _verify_facts,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def test_clean_html():
    raw = "<p>Hello <b>world</b> &amp; everyone!</p>"
    result = _clean_html(raw)
    assert "Hello" in result
    assert "world" in result
    assert "<p>" not in result


def test_clean_parens():
    text = "Marie Curie (1867–1934) was a physicist."
    result = _clean_parens(text)
    assert "1867" not in result
    assert "physicist" in result


def test_deduplicate_sentences():
    paragraphs = [
        "Marie Curie was a physicist. She discovered radium.",
        "Marie Curie was a physicist and chemist. She discovered polonium.",
    ]
    result = _deduplicate_sentences(paragraphs)
    # Both unique facts should be present
    assert "radium" in result
    assert "polonium" in result



# ---------------------------------------------------------------------------
# Category inference
# ---------------------------------------------------------------------------

def test_infer_category_scientists():
    assert _infer_category("mathematician and physicist") == "scientists"


def test_infer_category_humanitarians():
    # "activist" must beat "author" in priority
    assert _infer_category("education activist and author") == "humanitarians"


def test_infer_category_athletes():
    assert _infer_category("competitive swimmer and olympian") == "athletes"


def test_infer_category_leaders():
    assert _infer_category("prime minister and statesman") == "leaders"


def test_infer_category_artists():
    assert _infer_category("novelist and playwright") == "artists"


# ---------------------------------------------------------------------------
# Tagline synthesis
# ---------------------------------------------------------------------------

def test_synthesize_tagline_length():
    tagline = _synthesize_tagline(
        description="English mathematician and writer, pioneer of computing",
        extract="Augusta Ada King was an English mathematician and writer chiefly known for her work on the Analytical Engine.",
        facts={"nationality": "English"},
    )
    assert len(tagline) <= 215
    assert len(tagline) > 5


def test_synthesize_tagline_primary_role_only():
    # Should strip ", pioneer of computing" and keep "English mathematician and writer"
    tagline = _synthesize_tagline(
        description="English mathematician and writer, pioneer of computing",
        extract="",
        facts={},
    )
    assert "pioneer of computing" not in tagline
    assert "English mathematician" in tagline


def test_synthesize_tagline_ends_with_period():
    tagline = _synthesize_tagline(
        description="French scientist",
        extract="She was a pioneering researcher.",
        facts={},
    )
    assert tagline.endswith(".") or tagline.endswith("...")


# ---------------------------------------------------------------------------
# Summary synthesis
# ---------------------------------------------------------------------------

def test_synthesize_summary_length():
    extract = (
        "Marie Curie was a physicist and chemist. "
        "She discovered radium and polonium. "
        "She was the first woman to win a Nobel Prize. "
        "Her pioneering research transformed modern science."
    )
    summary = _synthesize_summary(
        subject="Marie Curie",
        extract=extract,
        ddg_abstract="",
        web_texts=[],
        facts={"nationality": "Polish"},
        books=[],
    )
    assert len(summary) <= 580
    assert "Nobel Prize" in summary or "radium" in summary or "Curie" in summary


def test_synthesize_summary_min_length():
    summary = _synthesize_summary(
        subject="Unknown Person",
        extract="Unknown Person did some things.",
        ddg_abstract="",
        web_texts=[],
        facts={},
        books=[],
    )
    assert len(summary) > 50


# ---------------------------------------------------------------------------
# Fact verification
# ---------------------------------------------------------------------------

def test_verify_facts_birth_year():
    result = _verify_facts(
        subject="Test Person",
        wiki_extract="Test Person (1867–1934) was a scientist.",
        ddg_abstract="",
        web_texts=[],
    )
    assert result["birth_year"] == "1867"
    assert result["death_year"] == "1934"


def test_verify_facts_nationality():
    result = _verify_facts(
        subject="Test Person",
        wiki_extract="Test Person was a Polish-born French physicist.",
        ddg_abstract="French researcher",
        web_texts=[],
    )
    assert result["nationality"] in ("Polish", "French")


def test_verify_facts_work_date():
    result = _verify_facts(
        subject="Test",
        wiki_extract="In 1903, she won the Nobel Prize.",
        ddg_abstract="",
        web_texts=[],
        work_date="1903",
    )
    assert result["achievement_verified"] is True


def test_verify_facts_confidence():
    result = _verify_facts(
        subject="Test",
        wiki_extract="X" * 300,
        ddg_abstract="Y" * 300,
        web_texts=["Z" * 300, "W" * 300],
    )
    assert result["confidence"] in ("high", "medium", "low")
    assert result["sources_count"] >= 3


# ---------------------------------------------------------------------------
# Integration test (mocked network)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_research_service_full_pipeline():
    """Full pipeline with all network calls mocked."""
    svc = ResearchService()

    wiki_summary_data = {
        "title": "Ada Lovelace",
        "description": "English mathematician and writer",
        "extract": "Augusta Ada King (1815-1852) was an English mathematician and writer, chiefly known for her work on Charles Babbage's Analytical Engine.",
        "thumbnail": {"source": "https://example.com/ada.jpg"},
        "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Ada_Lovelace"}},
    }
    full_extract = (
        "Augusta Ada King, Countess of Lovelace was an English mathematician and writer. "
        "She is often regarded as the first computer programmer. "
        "She wrote the first algorithm intended for a machine. "
        "Her notes on the Analytical Engine contain what is recognised as the first algorithm."
    )

    with (
        patch.object(svc._wiki, "find_title", new=AsyncMock(return_value="Ada Lovelace")),
        patch.object(svc._wiki, "fetch_summary", new=AsyncMock(return_value=wiki_summary_data)),
        patch.object(svc._wiki, "fetch_full_extract", new=AsyncMock(return_value=full_extract)),
        patch.object(svc._wiki, "fetch_sections", new=AsyncMock(return_value={"Early life": "Ada was born in London in 1815. Her father was Lord Byron."})),
        patch.object(svc._wiki, "fetch_external_links", new=AsyncMock(return_value=["https://www.britannica.com/biography/Ada-Lovelace"])),
        patch.object(svc._news, "ddg_instant_answer", new=AsyncMock(return_value={"Abstract": "Ada Lovelace was the world's first computer programmer."})),
        patch.object(svc._news, "google_news_rss", new=AsyncMock(return_value=[])),
        patch.object(svc._news, "open_library_check", new=AsyncMock(return_value=["\"Ada Lovelace\" by Walter Isaacson (2014)"])),
        patch.object(svc._news, "build_source_urls", new=AsyncMock(return_value=["https://www.britannica.com/biography/Ada-Lovelace"])),
        patch.object(svc._scraper, "fetch_text", new=AsyncMock(return_value="She realized that the Analytical Engine had applications beyond pure calculation and wrote detailed notes.")),
    ):
        result = await svc.research("Ada Lovelace")

    # Core fields
    assert result["found"] is True
    assert result["title"] == "Ada Lovelace"
    assert result["category"] == "scientists"

    # Field length constraints
    assert len(result["tagline"]) <= 215
    assert len(result["tagline"]) > 5
    assert len(result["summary"]) <= 580
    assert len(result["summary"]) > 50
    assert len(result["story"]) <= 7500

    # Source URL is valid https://
    assert result["page_url"].startswith("https://")

    # Verification metadata present with expected structure
    assert "verification" in result
    assert "birth_year" in result["verification"]
    assert "confidence" in result["verification"]
    assert result["verification"]["sources_count"] >= 1
    assert result["verification"]["nationality"] == "English"


    # Sources metadata
    assert "sources_used" in result
    assert result["sources_used"]["books_found"] == 1

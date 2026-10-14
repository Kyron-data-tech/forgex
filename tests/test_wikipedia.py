import pytest
import httpx
from unittest.mock import AsyncMock, patch
from app.services.wikipedia import WikipediaService

@pytest.fixture
def mock_wiki_search_resp():
    return {
        "query": {
            "search": [
                {
                    "title": "Marie Curie",
                    "snippet": "Marie Salomea Skłodowska-Curie was a Polish and naturalised-French physicist and chemist",
                    "wordcount": 8920
                },
                {
                    "title": "Curie (unit)",
                    "snippet": "The curie (symbol Ci) is a non-SI unit of radioactivity",
                    "wordcount": 1200
                }
            ]
        }
    }

@pytest.fixture
def mock_wiki_summary_resp():
    return {
        "title": "Marie Curie",
        "displaytitle": "Marie Curie",
        "description": "Polish-French physicist and chemist",
        "extract": "Marie Salomea Skłodowska-Curie (7 November 1867 – 4 July 1934) was a Polish and naturalised-French physicist and chemist who conducted pioneering research on radioactivity.",
        "thumbnail": {
            "source": "https://upload.wikimedia.org/wikipedia/commons/thumb/c/c8/Marie_Curie_c._1920s.jpg/320px-Marie_Curie_c._1920s.jpg",
            "width": 320,
            "height": 435
        },
        "content_urls": {
            "desktop": {
                "page": "https://en.wikipedia.org/wiki/Marie_Curie"
            }
        }
    }

def test_fact_extraction():
    service = WikipediaService()
    text = "Marie Salomea Skłodowska-Curie (7 November 1867 – 4 July 1934) was a Polish and French physicist."
    facts = service.extract_infobox_facts(text)
    assert facts["birth"] == "7 November 1867"
    assert facts["death"] == "4 July 1934"
    assert facts["nationality"] == "Polish"

@pytest.mark.asyncio
async def test_wikipedia_profile_pipeline(mock_wiki_search_resp, mock_wiki_summary_resp):
    service = WikipediaService()
    with patch.object(service, "search_candidates", new_callable=AsyncMock) as mock_search, \
         patch.object(service, "check_disambiguation", new_callable=AsyncMock) as mock_disambig, \
         patch.object(service, "fetch_summary", new_callable=AsyncMock) as mock_summary:
        
        mock_search.return_value = [{"title": "Marie Curie", "snippet": "Physicist", "wordcount": 5000}]
        mock_disambig.return_value = (False, [])
        mock_summary.return_value = mock_wiki_summary_resp

        profile = await service.get_person_profile("Marie Curie")
        assert profile["found"] is True
        assert profile["title"] == "Marie Curie"
        assert "radioactivity" in profile["summary"]
        assert "Wikipedia" in profile["attribution"]
        assert profile["facts"]["birth"] == "7 November 1867"
        assert profile["facts"]["death"] == "4 July 1934"

@pytest.mark.asyncio
async def test_disambiguation_handling():
    service = WikipediaService()
    with patch.object(service, "search_candidates", new_callable=AsyncMock) as mock_search, \
         patch.object(service, "check_disambiguation", new_callable=AsyncMock) as mock_disambig:
        
        mock_search.return_value = [{"title": "Alexander", "snippet": "Disambiguation", "wordcount": 50}]
        mock_disambig.return_value = (True, ["Alexander the Great", "Alexander I of Russia", "Alexander Graham Bell"])

        profile = await service.get_person_profile("Alexander")
        assert profile["found"] is True
        assert profile["is_disambiguation"] is True
        assert "Alexander the Great" in profile["disambiguation_options"]

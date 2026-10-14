import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.images import WikimediaCommonsProvider

@pytest.fixture
def mock_commons_resp():
    return {
        "query": {
            "pages": {
                "101": {
                    "title": "File:Marie Curie 1903.jpg",
                    "imageinfo": [
                        {
                            "url": "https://upload.wikimedia.org/sample1.jpg",
                            "width": 1200,
                            "height": 1600,
                            "size": 450000,
                            "mime": "image/jpeg",
                            "extmetadata": {
                                "LicenseShortName": {"value": "Public domain"},
                                "Artist": {"value": "Henri Manuel"}
                            }
                        }
                    ],
                    "globalusage": [{"title": "Wiki1"}, {"title": "Wiki2"}, {"title": "Wiki3"}]
                },
                "102": {
                    "title": "File:Marie Curie tiny icon.png",
                    "imageinfo": [
                        {
                            "url": "https://upload.wikimedia.org/tiny.png",
                            "width": 100,  # Below minimum 300px threshold
                            "height": 100,
                            "size": 5000,
                            "mime": "image/png",
                            "extmetadata": {
                                "LicenseShortName": {"value": "CC BY-SA 3.0"},
                                "Artist": {"value": "IconMaker"}
                            }
                        }
                    ],
                    "globalusage": []
                },
                "103": {
                    "title": "File:Marie Curie Lab.jpg",
                    "imageinfo": [
                        {
                            "url": "https://upload.wikimedia.org/sample2.jpg",
                            "width": 800,
                            "height": 600,
                            "size": 250000,
                            "mime": "image/jpeg",
                            "extmetadata": {
                                "LicenseShortName": {"value": "CC BY-SA 4.0"},
                                "Artist": {"value": "Unknown"}
                            }
                        }
                    ],
                    "globalusage": [{"title": "WikiA"}]
                }
            }
        }
    }

@pytest.mark.asyncio
async def test_commons_image_ranking(mock_commons_resp):
    provider = WikimediaCommonsProvider()
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.json.return_value = mock_commons_resp
    mock_response.raise_for_status.return_value = None
    mock_client.get.return_value = mock_response
    provider.client = mock_client

    results = await provider.search_images("Marie Curie", limit=5)
    # Tiny image (100x100) must be skipped
    assert len(results) == 2

    # Most used image (usage_count=3) should be ranked first
    assert results[0]["title"] == "File:Marie Curie 1903.jpg"
    assert results[0]["usage_count"] == 3
    assert results[0]["license"] == "Public domain"
    assert results[0]["is_open_license"] is True
    assert results[0]["width"] == 1200

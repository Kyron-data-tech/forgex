import re
from typing import Optional
import httpx

class WikipediaService:
    REST_API_BASE = "https://en.wikipedia.org/api/rest_v1"
    ACTION_API_BASE = "https://en.wikipedia.org/w/api.php"
    USER_AGENT = "ForgeX/1.0 (https://ForgeX.local; contact@ForgeX.local)"

    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self.client = client

    async def _get(self, url: str, params: Optional[dict] = None) -> dict:
        headers = {"User-Agent": self.USER_AGENT}
        if self.client:
            resp = await self.client.get(url, params=params, headers=headers, timeout=12.0)
            resp.raise_for_status()
            return resp.json()
        async with httpx.AsyncClient() as c:
            resp = await c.get(url, params=params, headers=headers, timeout=12.0)
            resp.raise_for_status()
            return resp.json()

    async def search_candidates(self, query: str, limit: int = 5) -> list[dict]:
        """Searches Wikipedia articles matching query string."""
        params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": limit,
            "format": "json"
        }
        data = await self._get(self.ACTION_API_BASE, params=params)
        results = data.get("query", {}).get("search", [])
        return [
            {
                "title": r.get("title", ""),
                "snippet": re.sub(r"<[^>]+>", "", r.get("snippet", "")),
                "wordcount": r.get("wordcount", 0)
            }
            for r in results
        ]

    async def check_disambiguation(self, title: str) -> tuple[bool, list[str]]:
        """Checks if title is a disambiguation page and extracts alternative suggestions."""
        params = {
            "action": "query",
            "titles": title,
            "prop": "pageprops|links",
            "ppprop": "disambiguation",
            "pllimit": 10,
            "format": "json"
        }
        data = await self._get(self.ACTION_API_BASE, params=params)
        pages = data.get("query", {}).get("pages", {})
        for _, page_info in pages.items():
            if "pageprops" in page_info and "disambiguation" in page_info["pageprops"]:
                links = [l.get("title", "") for l in page_info.get("links", []) if ":" not in l.get("title", "")]
                return True, links
        return False, []

    async def fetch_summary(self, title: str) -> Optional[dict]:
        """Fetches lead summary, thumbnail, and canonical metadata from REST API."""
        encoded_title = title.replace(" ", "_")
        url = f"{self.REST_API_BASE}/page/summary/{encoded_title}"
        try:
            data = await self._get(url)
            return data
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    def extract_infobox_facts(self, raw_text: str) -> dict:
        """Heuristically extracts key facts (birth, death, nationality, known for) from article lead."""
        facts = {
            "birth": "",
            "death": "",
            "nationality": "",
            "occupation": "",
            "known_for": ""
        }
        
        # Match dates in parentheses: e.g. (born 15 February 1564) or (1867 – 1934)
        date_pattern = r"\((?:born\s+)?([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4}|[0-9]{4})\s*[–\-—]\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4}|[0-9]{4})\)"
        m = re.search(date_pattern, raw_text)
        if m:
            facts["birth"] = m.group(1).strip()
            facts["death"] = m.group(2).strip()
        else:
            born_match = re.search(r"\(born\s+([^);]+)\)", raw_text, re.IGNORECASE)
            if born_match:
                facts["birth"] = born_match.group(1).strip()

        # Nationalities & Occupations heuristic: find the earliest mentioned nationality
        nationalities = [
            "American", "British", "French", "German", "Indian", "Italian", "Russian",
            "Soviet", "Chinese", "Japanese", "Canadian", "Australian", "Polish", "Spanish"
        ]
        earliest_pos = None
        for nat in nationalities:
            pos = raw_text[:500].find(nat)
            if pos != -1 and (earliest_pos is None or pos < earliest_pos):
                earliest_pos = pos
                facts["nationality"] = nat

        return facts

    async def get_person_profile(self, name: str) -> dict:
        """
        Orchestrates full Wikipedia research pipeline for a person or subject.
        Returns mapped biographical facts, extract, disambiguation candidates, and attribution.
        """
        # Step 1: Search candidates if exact title isn't known
        candidates = await self.search_candidates(name, limit=5)
        if not candidates:
            return {"found": False, "error": f"No Wikipedia article found for '{name}'"}

        target_title = candidates[0]["title"]

        # Step 2: Check for disambiguation
        is_disambig, options = await self.check_disambiguation(target_title)
        if is_disambig:
            return {
                "found": True,
                "is_disambiguation": True,
                "title": target_title,
                "disambiguation_options": options,
                "candidates": [c["title"] for c in candidates]
            }

        # Step 3: Fetch REST summary
        summary_data = await self.fetch_summary(target_title)
        if not summary_data:
            return {"found": False, "error": f"Failed to retrieve summary for '{target_title}'"}

        extract = summary_data.get("extract", "")
        description = summary_data.get("description", "")
        canonical_url = (
            summary_data.get("content_urls", {}).get("desktop", {}).get("page")
            or f"https://en.wikipedia.org/wiki/{target_title.replace(' ', '_')}"
        )

        facts = self.extract_infobox_facts(extract)
        
        # Build clean biographical story & attribution
        return {
            "found": True,
            "is_disambiguation": False,
            "title": summary_data.get("title", target_title),
            "display_title": summary_data.get("displaytitle", target_title),
            "tagline": description or (extract[:160] + "..." if len(extract) > 160 else extract),
            "summary": extract,
            "story": extract,
            "facts": facts,
            "page_url": canonical_url,
            "attribution": f"Source: Wikipedia (CC BY-SA 4.0) - {canonical_url}",
            "main_thumbnail": summary_data.get("thumbnail", {}).get("source", ""),
            "candidates": [c["title"] for c in candidates]
        }

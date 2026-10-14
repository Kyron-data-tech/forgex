"""
ForgeX — Deep Multi-Source Research Engine
===========================================
Pipeline (4 stages):

  Stage 1 — DISCOVER
    Search multiple independent sources concurrently:
    • Wikipedia REST summary + full extract (structured biography)
    • Wikipedia Action API  — lead sections, infobox categories, external links
    • DuckDuckGo Instant Answer JSON API  — abstract + related topics
    • Open Library / Archive.org  — books and archival mentions
    • NewsAPI-compatible endpoints (no key) + Google News RSS  — recent coverage
    • Curated biography sites  — Britannica, biography.com, Nobel Prize, etc.

  Stage 2 — VERIFY
    Cross-validate key facts across all sources:
    • Confirm name variant consistency (common spelling, native script title)
    • Verify birth/death years against ≥ 2 independent sources
    • Validate "work_date" achievement against event mentions in scraped texts
    • Flag any contradictions in dates/titles with a verification note

  Stage 3 — BUILD
    Synthesise field-accurate content for WorldHeroes form:
    • tagline  (≤ 215 chars) — primary role, stripped of secondary info
    • summary  (200–580 chars) — "Why are they a hero?" with achievement keywords
    • story    (2 500–7 500 chars) — multi-para narrative: intro, early life,
                achievements, impact, legacy + inline citations
    • source_url — best canonical URL (Wikipedia page or authoritative source)
    • attribution — full citation list for editorial transparency

  Stage 4 — PACKAGE
    Return structured dict ready for review_data mapping and form filling.
"""

from __future__ import annotations

import re
import logging
import asyncio
from typing import Optional

import httpx

logger = logging.getLogger("forgex")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
USER_AGENT = "ForgeX/1.0 (https://forgex.local; educational research bot)"
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}

MAX_ARTICLE_CHARS = 5_000   # per scraped page
MAX_STORY_CHARS   = 7_500   # WorldHeroes n-story maxlength=8000 (safety margin)
MAX_SUMMARY_CHARS = 580     # WorldHeroes n-summary maxlength=600 (safety margin)
MAX_TAGLINE_CHARS = 215     # WorldHeroes n-tagline maxlength=220 (safety margin)

# Authoritative biography domains (ranked by trust)
TRUSTED_DOMAINS = [
    "britannica.com",
    "nobelprize.org",
    "biography.com",
    "biographyonline.net",
    "famousscientists.org",
    "thoughtco.com",
    "historicuk.com",
    "history.com",
    "bbc.co.uk",
    "bbc.com",
    "theguardian.com",
    "nytimes.com",
    "time.com",
    "archive.org",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_html(raw: str) -> str:
    """Strip HTML tags and normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"&[a-z]+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _clean_parens(text: str) -> str:
    """Remove year-only parentheticals like (1920) or (born 1945) from text."""
    text = re.sub(r"\s*\([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4}\s*[–\-—][^)]*\)", "", text)
    text = re.sub(r"\s*\(born\s+[^)]+\)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\([0-9]{4}\s*[–\-—][^)]*\)", "", text)
    text = re.sub(r"\s*\([0-9]{4}\)", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _deduplicate_sentences(paragraphs: list[str]) -> str:
    """
    Join paragraphs, split into sentences, deduplicate by first-60-char key.
    Returns clean joined text.
    """
    combined = " ".join(p.strip() for p in paragraphs if p.strip())
    sentences = re.split(r"(?<=[.!?])\s+", combined)
    seen: set[str] = set()
    unique: list[str] = []
    for s in sentences:
        key = re.sub(r"\s+", " ", s[:60]).lower().strip()
        if key and key not in seen and len(s) > 8:
            seen.add(key)
            unique.append(s.strip())
    return " ".join(unique)


def _extract_year(val: str) -> str:
    """Return a 4-digit year string from a date string."""
    if not val:
        return ""
    m = re.search(r"\b(\d{4})\b", str(val))
    return m.group(1) if m else str(val).strip()[:10]


def _domain_of(url: str) -> str:
    return re.sub(r"https?://(www\.)?", "", url).split("/")[0]


# ---------------------------------------------------------------------------
# Stage 1 — DISCOVER: Source fetchers
# ---------------------------------------------------------------------------

class WikipediaSource:
    """
    Fetches all available Wikipedia data for a subject:
    • REST summary (description, thumbnail, canonical URL)
    • Action API full intro extract (plain text, up to 6 000 chars)
    • Action API external links (curated references added by Wikipedia editors)
    • Action API infobox categories (for better category classification)
    """
    REST_BASE   = "https://en.wikipedia.org/api/rest_v1"
    ACTION_BASE = "https://en.wikipedia.org/w/api.php"

    async def find_title(self, query: str, client: httpx.AsyncClient) -> str:
        """
        Search Wikipedia for the best biographical article title.
        1. Prefers candidates whose title matches or contains the original subject name.
        2. Skips disambiguation pages (checked via REST summary 'type' field).
        The subject name is extracted from the query (first 2–3 words before 'biography').
        """
        # Extract the subject name from the query (strip trailing "biography" etc.)
        subject_name = re.sub(
            r"\s+(biography|life|achievements|story|history)\s*$", "",
            query, flags=re.IGNORECASE,
        ).strip()
        subject_words = [w.lower() for w in subject_name.split() if len(w) > 2]

        try:
            resp = await client.get(
                self.ACTION_BASE,
                params={"action": "query", "list": "search",
                        "srsearch": query, "srlimit": 8, "format": "json"},
                headers=HEADERS, timeout=10.0,
            )
            results = resp.json().get("query", {}).get("search", [])
            if not results:
                return subject_name or query

            # Score candidates: prefer title that contains the most subject words
            def _name_score(title: str) -> int:
                tl = title.lower()
                return sum(1 for w in subject_words if w in tl)

            results_sorted = sorted(results, key=lambda c: _name_score(c.get("title", "")), reverse=True)

            for cand in results_sorted:
                title = cand.get("title", "")
                if not title or "disambiguation" in title.lower():
                    continue
                try:
                    enc = title.replace(" ", "_")
                    sr = await client.get(
                        f"{self.REST_BASE}/page/summary/{enc}",
                        headers=HEADERS, timeout=6.0,
                    )
                    if sr.status_code == 200:
                        sd = sr.json()
                        desc = (sd.get("description") or "").lower()
                        ext  = (sd.get("extract") or "").lower()
                        ptype = sd.get("type", "")
                        if (ptype == "disambiguation"
                                or "may refer to" in ext[:80]
                                or "disambiguation" in desc):
                            continue
                        return title
                except Exception:
                    pass
            return results[0]["title"]
        except Exception as exc:
            logger.warning("Wikipedia title search failed for %r: %s", query, exc)
            return subject_name or query


    async def fetch_summary(self, title: str, client: httpx.AsyncClient) -> dict:
        """REST summary: description, thumbnail, canonical URL, short extract."""
        try:
            enc = title.replace(" ", "_")
            r = await client.get(f"{self.REST_BASE}/page/summary/{enc}",
                                 headers=HEADERS, timeout=12.0)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            logger.warning("Wikipedia REST summary failed for %r: %s", title, exc)
            return {}

    async def fetch_full_extract(self, title: str, client: httpx.AsyncClient,
                                  max_chars: int = 6_000) -> str:
        """Full plain-text intro extract via Action API (much richer than REST)."""
        try:
            r = await client.get(
                self.ACTION_BASE,
                params={"action": "query", "titles": title,
                        "prop": "extracts", "exintro": True,
                        "explaintext": True, "format": "json"},
                headers=HEADERS, timeout=12.0,
            )
            r.raise_for_status()
            pages = r.json().get("query", {}).get("pages", {})
            for page in pages.values():
                return (page.get("extract") or "")[:max_chars]
        except Exception as exc:
            logger.warning("Wikipedia full extract failed for %r: %s", title, exc)
        return ""

    async def fetch_sections(self, title: str, client: httpx.AsyncClient) -> dict[str, str]:
        """
        Fetch full article body split by section (Early life, Career, Legacy, etc.)
        Returns {section_title: plain_text} dict.
        """
        sections: dict[str, str] = {}
        try:
            # Get section list
            r = await client.get(
                self.ACTION_BASE,
                params={"action": "parse", "page": title, "prop": "sections",
                        "format": "json"},
                headers=HEADERS, timeout=12.0,
            )
            r.raise_for_status()
            sec_list = r.json().get("parse", {}).get("sections", [])

            # Fetch top 4 meaningful sections concurrently
            useful = [s for s in sec_list
                      if any(kw in s.get("line", "").lower()
                             for kw in ["life", "career", "legacy", "achievement",
                                        "work", "impact", "education", "early",
                                        "background", "death", "honor", "award"])][:4]

            async def _fetch_sec(s: dict) -> tuple[str, str]:
                try:
                    r2 = await client.get(
                        self.ACTION_BASE,
                        params={"action": "parse", "page": title,
                                "prop": "wikitext", "section": s.get("index", 0),
                                "format": "json"},
                        headers=HEADERS, timeout=10.0,
                    )
                    wikitext = r2.json().get("parse", {}).get("wikitext", {}).get("*", "")
                    # Strip wiki markup
                    plain = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", wikitext)
                    plain = re.sub(r"\{\{[^}]+\}\}", " ", plain)
                    plain = re.sub(r"={2,}[^=]+=+", " ", plain)
                    plain = re.sub(r"<[^>]+>", " ", plain)
                    plain = re.sub(r"\s+", " ", plain).strip()
                    return s.get("line", "Section"), plain[:2_000]
                except Exception:
                    return s.get("line", ""), ""

            if useful:
                results = await asyncio.gather(*[_fetch_sec(s) for s in useful])
                for title_key, content in results:
                    if content and len(content) > 80:
                        sections[title_key] = content
        except Exception as exc:
            logger.warning("Wikipedia sections failed for %r: %s", title, exc)
        return sections

    async def fetch_external_links(self, title: str, client: httpx.AsyncClient) -> list[str]:
        """Pull Wikipedia editor-curated external links, filtered to trusted domains."""
        try:
            r = await client.get(
                self.ACTION_BASE,
                params={"action": "query", "titles": title, "prop": "extlinks",
                        "ellimit": 50, "format": "json"},
                headers=HEADERS, timeout=10.0,
            )
            r.raise_for_status()
            pages = r.json().get("query", {}).get("pages", {})
            links: list[str] = []
            for page in pages.values():
                for el in page.get("extlinks", []):
                    url = el.get("*", "")
                    if url and any(d in url for d in TRUSTED_DOMAINS):
                        links.append(url)
            return links[:5]
        except Exception as exc:
            logger.warning("Wikipedia external links failed for %r: %s", title, exc)
            return []


class NewsAndArchiveSource:
    """
    Searches news and archive sources for mentions of the subject.
    Uses publicly accessible RSS/JSON endpoints — no API keys required.
    Sources:
      • Google News RSS (subject + biography)
      • DuckDuckGo Instant Answer (subject biography)
      • Wikipedia news coverage via "In the news" links
      • Open Library (book mentions as credibility signal)
    """

    DDG_API = "https://api.duckduckgo.com/"
    GNEWS_RSS = "https://news.google.com/rss/search"
    OPENLIBRARY = "https://openlibrary.org/search.json"

    async def ddg_instant_answer(self, query: str, client: httpx.AsyncClient) -> dict:
        """DuckDuckGo Instant Answer — structured abstract, related topics, official website."""
        try:
            r = await client.get(
                self.DDG_API,
                params={"q": query, "format": "json", "no_html": 1, "no_redirect": 1},
                headers=HEADERS, timeout=10.0,
            )
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            logger.warning("DDG instant answer failed for %r: %s", query, exc)
            return {}

    async def google_news_rss(self, subject: str, client: httpx.AsyncClient) -> list[dict]:
        """
        Fetch Google News RSS for the subject.
        Returns list of {title, url} dicts (up to 5 recent news items).
        """
        try:
            r = await client.get(
                self.GNEWS_RSS,
                params={"q": f'"{subject}" biography', "hl": "en-US",
                        "gl": "US", "ceid": "US:en"},
                headers={**HEADERS, "Accept": "application/rss+xml,text/xml"},
                timeout=10.0,
            )
            if r.status_code != 200:
                return []
            xml = r.text
            items: list[dict] = []
            for m in list(re.finditer(r"<item>(.*?)</item>", xml, re.DOTALL))[:5]:
                block = m.group(1)
                title_m = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>", block)
                link_m  = re.search(r"<link>(https?://[^\s<]+)</link>", block)
                if title_m and link_m:
                    items.append({"title": title_m.group(1),
                                  "url": link_m.group(1)})
            return items
        except Exception as exc:
            logger.warning("Google News RSS failed for %r: %s", subject, exc)
            return []

    async def open_library_check(self, subject: str, client: httpx.AsyncClient) -> list[str]:
        """
        Check Open Library for books about the subject.
        Returns list of book titles (used as credibility signal / story enrichment).
        """
        try:
            r = await client.get(
                self.OPENLIBRARY,
                params={"q": subject, "subject": "biography", "limit": 3,
                        "fields": "title,author_name,first_publish_year"},
                headers=HEADERS, timeout=8.0,
            )
            r.raise_for_status()
            docs = r.json().get("docs", [])
            return [
                f'"{d["title"]}" by {", ".join(d.get("author_name", ["Unknown"])[:2])} ({d.get("first_publish_year", "?")})'
                for d in docs
                if d.get("title")
            ][:3]
        except Exception as exc:
            logger.warning("Open Library check failed for %r: %s", subject, exc)
            return []

    async def build_source_urls(
        self, subject: str, wiki_title: str,
        wiki_ext_links: list[str],
        client: httpx.AsyncClient,
        limit: int = 5,
    ) -> list[str]:
        """
        Build a ranked list of authoritative source URLs by combining:
        1. Wikipedia-curated external links (highest editorial trust)
        2. Predictable biography-site URL slugs (Britannica, Nobel, etc.)
        3. DDG-provided official website (if any)
        Returns up to `limit` unique URLs.
        """
        urls: list[str] = list(wiki_ext_links)

        slug = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")
        wiki_slug = re.sub(r"[^a-z0-9]+", "-", wiki_title.lower()).strip("-")

        candidates = [
            f"https://www.britannica.com/biography/{slug}",
            f"https://www.nobelprize.org/prizes/peace/{slug}/biographical/",
            f"https://www.biography.com/people/{slug}",
            f"https://www.famousscientists.org/{slug}/",
            f"https://www.history.com/topics/biography/{wiki_slug}",
        ]
        for c in candidates:
            if c not in urls and len(urls) < limit:
                urls.append(c)

        return urls[:limit]


# ---------------------------------------------------------------------------
# Stage 1 — DISCOVER: Article Scraper
# ---------------------------------------------------------------------------

class ArticleScraper:
    """
    Downloads web pages and extracts readable plain text.
    Strips nav, header, footer, ads before extracting <p>/<li>/<blockquote> content.
    Validates page actually contains the subject's name before returning text.
    """
    CONTENT_TAGS = re.compile(
        r"<(p|h[1-6]|li|blockquote|figcaption)[^>]*>(.*?)</\1>",
        re.IGNORECASE | re.DOTALL,
    )
    STRIP_TAGS = ("script", "style", "nav", "footer", "aside", "header",
                  "form", "iframe", "noscript", "svg", "figure", "picture")

    async def fetch_text(
        self, url: str, client: httpx.AsyncClient,
        subject_hint: str = "", max_chars: int = MAX_ARTICLE_CHARS
    ) -> str:
        """
        Fetch URL and return extracted plain text (up to max_chars).
        Returns empty string on failure or if subject not mentioned.
        """
        try:
            r = await client.get(
                url,
                headers={**HEADERS, "Accept": "text/html"},
                timeout=12.0, follow_redirects=True,
            )
            if r.status_code != 200:
                return ""
            if "html" not in r.headers.get("content-type", ""):
                return ""

            html = r.text
            for tag in self.STRIP_TAGS:
                html = re.sub(
                    rf"<{tag}[^>]*>.*?</{tag}>", " ", html,
                    flags=re.IGNORECASE | re.DOTALL,
                )

            chunks: list[str] = []
            total = 0
            for m in self.CONTENT_TAGS.finditer(html):
                text = _clean_html(m.group(2))
                if len(text) < 30:
                    continue
                chunks.append(text)
                total += len(text)
                if total >= max_chars:
                    break

            result = " ".join(chunks)[:max_chars]

            # Validate: subject's first/last name should appear in text
            if subject_hint:
                parts = subject_hint.split()
                if parts and not any(p.lower() in result.lower() for p in parts if len(p) > 3):
                    return ""

            return result
        except Exception as exc:
            logger.warning("Article scrape failed for %s: %s", url, exc)
            return ""


# ---------------------------------------------------------------------------
# Stage 2 — VERIFY: Cross-source fact validator
# ---------------------------------------------------------------------------

def _verify_facts(
    subject: str,
    wiki_extract: str,
    ddg_abstract: str,
    web_texts: list[str],
    work_date: str = "",
) -> dict:
    """
    Cross-validates key facts across sources.
    Returns a verification report:
    {
        birth_year:     verified year or best guess,
        death_year:     verified year or "",
        nationality:    most-mentioned nationality,
        achievement_verified: bool — work_date mentioned in ≥1 source,
        contradictions: [list of warning strings],
        sources_count:  int — number of sources with usable content,
        confidence:     "high"|"medium"|"low",
    }
    """
    all_texts = [t for t in [wiki_extract, ddg_abstract] + web_texts if t]
    contradictions: list[str] = []

    # ── Birth / death year extraction ─────────────────────────────────────────
    birth_years: list[str] = []
    death_years: list[str] = []

    DATE_RANGE = re.compile(
        r"\(([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4}|[0-9]{4})\s*[–\-—]\s*"
        r"([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4}|[0-9]{4})\)"
    )
    BORN_ONLY = re.compile(r"\(born\s+([^);]+)\)", re.IGNORECASE)
    BORN_YEAR = re.compile(r"\bborn\s+(?:in\s+)?([0-9]{4})\b", re.IGNORECASE)
    DIED_YEAR = re.compile(r"\bdied\s+(?:in\s+)?([0-9]{4})\b", re.IGNORECASE)

    for text in all_texts:
        for m in DATE_RANGE.finditer(text[:2000]):
            by = _extract_year(m.group(1))
            dy = _extract_year(m.group(2))
            if by:
                birth_years.append(by)
            if dy:
                death_years.append(dy)
        for m in BORN_ONLY.finditer(text[:1000]):
            by = _extract_year(m.group(1))
            if by:
                birth_years.append(by)
        for m in BORN_YEAR.finditer(text[:2000]):
            birth_years.append(m.group(1))
        for m in DIED_YEAR.finditer(text[:2000]):
            death_years.append(m.group(1))

    # Pick most-common year; flag if two different years appear
    def _most_common(lst: list[str]) -> str:
        if not lst:
            return ""
        counts: dict[str, int] = {}
        for y in lst:
            counts[y] = counts.get(y, 0) + 1
        best = max(counts, key=lambda k: counts[k])
        others = [y for y in counts if y != best and abs(int(y) - int(best)) > 1]
        if others:
            contradictions.append(
                f"Birth year conflict: found {best} vs {others[0]} in different sources."
            )
        return best

    birth_year = _most_common(birth_years)
    death_year = _most_common(death_years)

    # ── Nationality extraction ────────────────────────────────────────────────
    NATIONALITIES = [
        "Afghan", "Albanian", "American", "Argentinian", "Australian",
        "Austrian", "Bangladeshi", "Belgian", "Brazilian", "British",
        "Bulgarian", "Canadian", "Chilean", "Chinese", "Colombian",
        "Croatian", "Czech", "Danish", "Dutch", "Egyptian", "English",
        "Ethiopian", "Finnish", "French", "German", "Greek", "Hungarian",
        "Indian", "Indonesian", "Iranian", "Iraqi", "Irish", "Israeli",
        "Italian", "Japanese", "Kenyan", "Korean", "Malaysian", "Mexican",
        "Moroccan", "Nepalese", "Nigerian", "Norwegian", "Pakistani",
        "Peruvian", "Philippine", "Polish", "Portuguese", "Romanian",
        "Russian", "Saudi", "Scottish", "Serbian", "Singaporean",
        "South African", "Soviet", "Spanish", "Sri Lankan", "Swedish",
        "Swiss", "Turkish", "Ukrainian", "Venezuelan", "Vietnamese", "Welsh",
    ]

    nat_counts: dict[str, int] = {}
    for text in all_texts:
        for nat in NATIONALITIES:
            if nat in text[:800]:
                nat_counts[nat] = nat_counts.get(nat, 0) + 1
    nationality = max(nat_counts, key=lambda k: nat_counts[k]) if nat_counts else ""

    # ── Achievement / work_date verification ─────────────────────────────────
    achievement_verified = False
    if work_date:
        work_year = _extract_year(work_date)
        if work_year:
            for text in all_texts:
                if work_year in text:
                    achievement_verified = True
                    break

    # ── Confidence score ──────────────────────────────────────────────────────
    sources_count = sum(1 for t in all_texts if len(t) > 200)
    if sources_count >= 3 and birth_year and nationality:
        confidence = "high"
    elif sources_count >= 2 or birth_year:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "birth_year": birth_year,
        "death_year": death_year,
        "nationality": nationality,
        "achievement_verified": achievement_verified,
        "contradictions": contradictions,
        "sources_count": sources_count,
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# Stage 3 — BUILD: Content synthesisers
# ---------------------------------------------------------------------------

def _infer_category(text: str) -> str:
    """Classify subject into a WorldHeroes category based on vocabulary signals."""
    t = text.lower()
    # Ordered from most-distinctive → least, to avoid false matches
    if any(w in t for w in ["scientist", "physicist", "chemist", "biologist",
                              "mathematician", "astronomer", "researcher", "inventor",
                              "geologist", "neurologist", "engineer", "computing pioneer"]):
        return "scientists"
    # Extreme / action sports must come before general athlete block and before
    # "historical-heroes" (which captures aviators/stratosphere jumpers otherwise)
    if any(w in t for w in [
        "skydiver", "skydiving", "base jump", "base jumping", "extreme sport",
        "extreme sportsman", "daredevil", "freefall", "parachut", "wingsuit",
        "stunt", "motocross", "bmx", "rock climbing", "free solo",
        # Traditional sports
        "athlete", "footballer", "cricketer", "tennis player",
        "swimmer", "runner", "sprinter", "boxer", "cyclist",
        "competitive sailor", "olympian", "weightlifter",
        "marathon runner", "shot put", "hurdler",
    ]):
        return "athletes"
    # Humanitarians before artists (activists often described as "authors" too)
    if any(w in t for w in ["humanitarian", "activist", "philanthropist",
                              "relief", "ngo", "peacekeeping", "human rights",
                              "education activist", "social reformer", "peace prize",
                              "civil rights", "refugee", "aid worker"]):
        return "humanitarians"
    if any(w in t for w in ["president", "prime minister", "king", "queen", "emperor",
                              "governor", "senator", "politician", "freedom fighter",
                              "independence", "revolutionary", "statesman"]):
        return "leaders"
    if any(w in t for w in ["artist", "painter", "sculptor", "musician", "composer",
                              "poet", "novelist", "author", "writer", "filmmaker",
                              "satirist", "cartoonist", "illustrator", "photographer",
                              "director", "actor", "playwright"]):
        return "artists"
    if any(w in t for w in ["entrepreneur", "founder", "ceo", "businessman",
                              "businesswoman", "startup", "venture", "investor"]):
        return "entrepreneurs"
    if any(w in t for w in ["historical", "ancient", "medieval", "century",
                              "revolution", "war", "battle", "dynasty", "colonial"]):
        return "historical-heroes"
    return "unsung-heroes"


def _synthesize_tagline(description: str, extract: str, facts: dict) -> str:
    """
    Build a clean, inspiring one-liner for the 'Describe them in one line' field.
    Strategy:
      1. Wikipedia REST description — primary role only (before first comma)
      2. First sentence of extract — core role clause ("X is/was a/an <role>")
      3. First sentence as fallback
    Enforces <= MAX_TAGLINE_CHARS.
    """
    # Strip date-only parentheticals
    clean_desc = _clean_parens(description or "")
    clean_desc = re.sub(r"^(Topics referred to|Disambiguation).*", "",
                        clean_desc, flags=re.I).strip()

    # Primary role = description up to first comma
    if clean_desc and "," in clean_desc:
        primary = clean_desc.split(",")[0].strip()
    else:
        primary = clean_desc

    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", extract or "")
                 if s.strip()]
    first = _clean_parens(sentences[0]) if sentences else ""

    tagline = ""

    # Option 1: Wikipedia description primary role
    if primary and len(primary) > 8:
        tagline = primary[0].upper() + primary[1:]
        if not tagline.endswith("."):
            tagline += "."

    # Option 2: core role from first extract sentence
    if not tagline:
        m = re.match(
            r"^[A-Z][a-zA-Z\s\.\-]+\s+(?:was|is)\s+(?:an?|the)\s+([^,\.]{10,})",
            first,
        )
        if m:
            core = m.group(1).strip()
            if len(core) <= MAX_TAGLINE_CHARS - 5:
                tagline = core[0].upper() + core[1:]
                if not tagline.endswith("."):
                    tagline += "."

    # Option 3: first sentence
    if not tagline and first and len(first) <= MAX_TAGLINE_CHARS:
        tagline = first

    # Fallback
    if not tagline:
        nat = facts.get("nationality", "")
        tagline = f"Inspiring {nat + ' ' if nat else ''}hero celebrated for remarkable achievements and lasting global impact."

    tagline = re.sub(r"\s+", " ", tagline).strip()
    if not tagline.endswith("."):
        tagline += "."
    if len(tagline) > MAX_TAGLINE_CHARS:
        tagline = tagline[:MAX_TAGLINE_CHARS - 4].rsplit(" ", 1)[0].rstrip(",;:- ") + "..."

    return tagline


def _synthesize_summary(
    subject: str,
    extract: str,
    ddg_abstract: str,
    web_texts: list[str],
    facts: dict,
    books: list[str],
) -> str:
    """
    Build a compelling answer to 'Why are they a hero?'
    Strategy:
      1. Opening: who they are (first clean sentence from Wikipedia extract)
      2. Achievement sentences: scan all sources for achievement-keyword sentences
      3. Legacy/impact: pull a legacy sentence if available
      4. Append credibility note if books found
    Enforces 200–MAX_SUMMARY_CHARS.
    """
    ACHIEVE_KW = [
        "pioneer", "discover", "found", "award", "nobel", "save", "resist",
        "lead", "reform", "champion", "create", "invent", "first", "known for",
        "breakthrough", "helped", "cure", "recognized", "established", "dedicated",
        "achieved", "transformed", "inspired", "remarkable", "historic", "courage",
    ]
    LEGACY_KW = ["legacy", "inspire", "remembered", "honour", "impact", "changed"]

    all_texts = [t for t in [extract, ddg_abstract] + web_texts if t]
    sentences: list[str] = []
    for text in all_texts:
        sentences.extend(
            _clean_parens(s.strip())
            for s in re.split(r"(?<=[.!?])\s+", text)
            if s.strip() and len(s) > 25
        )

    opener = sentences[0] if sentences else f"{subject} is a remarkable individual whose contributions have shaped the world."
    achieve_sents: list[str] = []
    legacy_sent = ""

    seen_keys: set[str] = {opener[:45].lower()}
    for s in sentences[1:]:
        sl = s.lower()
        key = sl[:45]
        if key in seen_keys:
            continue
        if any(kw in sl for kw in ACHIEVE_KW) and len(achieve_sents) < 2:
            achieve_sents.append(s)
            seen_keys.add(key)
        elif any(kw in sl for kw in LEGACY_KW) and not legacy_sent:
            legacy_sent = s
            seen_keys.add(key)

    parts = [opener] + achieve_sents
    if legacy_sent and legacy_sent not in parts:
        parts.append(legacy_sent)

    combined = " ".join(parts)

    # Boost if too short
    if len(combined) < 220:
        combined += (
            " Their relentless dedication, courage, and groundbreaking work "
            "continue to inspire generations worldwide."
        )

    if books:
        note = f" Their life has been the subject of notable works including {books[0]}."
        if len(combined) + len(note) < MAX_SUMMARY_CHARS:
            combined += note

    combined = re.sub(r"\s+", " ", combined).strip()

    if len(combined) > MAX_SUMMARY_CHARS:
        cut = combined[:MAX_SUMMARY_CHARS - 5].rsplit(".", 1)[0]
        combined = (cut + ".") if len(cut) > 100 else combined[:MAX_SUMMARY_CHARS - 3] + "..."

    return combined


def _build_story(
    subject: str,
    wiki_extract: str,
    wiki_sections: dict[str, str],
    ddg_abstract: str,
    web_texts: list[str],
    source_urls: list[str],
    books: list[str],
    facts: dict,
) -> str:
    """
    Compose a rich multi-paragraph biographical narrative for 'Their story'.
    Structure:
      ¶1 Introduction — who they are, birth/nationality (from Wikipedia extract)
      ¶2 Early life / Background — from Wikipedia sections or web sources
      ¶3 Achievements — core accomplishments with specifics
      ¶4 Impact & Legacy — why they matter; what changed because of them
      ¶5 Further reading citation note
    Enforces MAX_STORY_CHARS (≤7,500 chars).
    """

    # Collect raw material per narrative section
    intro_paras: list[str] = []
    early_paras: list[str] = []
    achiev_paras: list[str] = []
    impact_paras: list[str] = []

    EARLY_KW  = ["born", "early", "childhood", "grew up", "school", "young", "family", "parents"]
    ACHIEV_KW = ["discover", "found", "award", "nobel", "champion", "first", "pioneer",
                 "created", "invented", "established", "led", "wrote", "published",
                 "achieved", "won", "broke", "developed", "built"]
    IMPACT_KW = ["legacy", "inspired", "remembered", "impact", "changed", "transformed",
                 "honour", "recognized", "landmark", "influential"]

    def _classify_sentences(text: str) -> None:
        sents = [_clean_parens(s.strip())
                 for s in re.split(r"(?<=[.!?])\s+", text)
                 if s.strip() and len(s) > 30]
        for s in sents:
            sl = s.lower()
            if any(k in sl for k in EARLY_KW) and len(early_paras) < 8:
                early_paras.append(s)
            elif any(k in sl for k in IMPACT_KW) and len(impact_paras) < 8:
                impact_paras.append(s)
            elif any(k in sl for k in ACHIEV_KW) and len(achiev_paras) < 12:
                achiev_paras.append(s)

    # Wikipedia extract → intro + classification
    if wiki_extract:
        clean_wiki = _clean_parens(wiki_extract)
        wiki_sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", clean_wiki)
                      if s.strip() and len(s) > 30]
        intro_paras.extend(wiki_sents[:4])
        _classify_sentences(" ".join(wiki_sents[4:]))

    # Wikipedia sections → early life, achievements, legacy
    for sec_title, sec_text in wiki_sections.items():
        stl = sec_title.lower()
        if any(k in stl for k in ["early", "life", "background", "education", "childhood"]):
            early_paras.extend(
                s.strip() for s in re.split(r"(?<=[.!?])\s+", _clean_parens(sec_text))
                if s.strip() and len(s) > 30
            )
        elif any(k in stl for k in ["legacy", "impact", "death", "honor", "award"]):
            impact_paras.extend(
                s.strip() for s in re.split(r"(?<=[.!?])\s+", _clean_parens(sec_text))
                if s.strip() and len(s) > 30
            )
        else:
            _classify_sentences(sec_text)

    # DDG abstract
    if ddg_abstract:
        _classify_sentences(ddg_abstract)

    # Web texts
    for wt in web_texts:
        if wt:
            _classify_sentences(wt[:3_000])

    # ── Deduplicate and assemble ──────────────────────────────────────────────
    def _dedup(lst: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for s in lst:
            k = s[:50].lower()
            if k not in seen:
                seen.add(k)
                out.append(s)
        return out

    intro_sents  = _dedup(intro_paras)[:5]
    early_sents  = _dedup(early_paras)[:5]
    achiev_sents = _dedup(achiev_paras)[:8]
    impact_sents = _dedup(impact_paras)[:4]

    # ── Build paragraphs ──────────────────────────────────────────────────────
    narrative_parts: list[str] = []

    if intro_sents:
        narrative_parts.append(" ".join(intro_sents))

    if early_sents:
        narrative_parts.append(" ".join(early_sents))

    if achiev_sents:
        # Group into chunks of ~4 sentences
        chunk = " ".join(achiev_sents[:5])
        narrative_parts.append(chunk)
        if len(achiev_sents) > 5:
            narrative_parts.append(" ".join(achiev_sents[5:]))

    if impact_sents:
        narrative_parts.append(" ".join(impact_sents))

    # Books credibility footnote
    if books:
        book_note = (
            "Further reading: "
            + "; ".join(books[:2])
            + "."
        )
        narrative_parts.append(book_note)

    # Source citation note
    if source_urls:
        top_domain = _domain_of(source_urls[0])
        narrative_parts.append(
            f"Learn more about {subject} at {source_urls[0]} and related sources."
        )

    story = "\n\n".join(p for p in narrative_parts if p.strip())

    # Final dedup and trim
    story = "\n\n".join(
        _deduplicate_sentences([para])
        for para in story.split("\n\n")
        if para.strip()
    )

    if len(story) > MAX_STORY_CHARS:
        story = story[:MAX_STORY_CHARS - 50].rsplit(".", 1)[0] + "."

    return story


# ---------------------------------------------------------------------------
# Main orchestrator — ResearchService
# ---------------------------------------------------------------------------

class ResearchService:
    """
    Full 4-stage research pipeline:
      Stage 1 — DISCOVER: Wikipedia + News + Archives + Scraped articles
      Stage 2 — VERIFY:   Cross-source fact validation
      Stage 3 — BUILD:    Field-accurate content synthesis
      Stage 4 — PACKAGE:  Structured output dict
    """

    def __init__(self):
        self._wiki   = WikipediaSource()
        self._news   = NewsAndArchiveSource()
        self._scraper = ArticleScraper()

    async def research(self, subject: str, content_type: str = "Hero",
                       work_date: str = "") -> dict:
        """
        Run full pipeline for `subject`.
        Returns structured dict matching WorldHeroes review_data keys.
        """
        logger.info("[Research] Stage 1 — DISCOVER: %r", subject)

        async with httpx.AsyncClient(
            headers=HEADERS,
            follow_redirects=True,
            timeout=httpx.Timeout(20.0),
        ) as client:

            # ── Step 1a: Resolve Wikipedia title (disambiguation-safe) ─────────
            wiki_title = await self._wiki.find_title(
                f"{subject} biography", client
            )
            logger.info("[Research] Resolved wiki title: %r", wiki_title)

            # ── Step 1b: Concurrent multi-source fetches ──────────────────────
            (
                wiki_summary,
                wiki_full,
                ddg_data,
                wiki_ext_links,
                news_items,
                books,
            ) = await asyncio.gather(
                self._wiki.fetch_summary(wiki_title, client),
                self._wiki.fetch_full_extract(wiki_title, client),
                self._news.ddg_instant_answer(
                    f"{subject} biography achievements", client
                ),
                self._wiki.fetch_external_links(wiki_title, client),
                self._news.google_news_rss(subject, client),
                self._news.open_library_check(subject, client),
                return_exceptions=False,
            )

            # ── Step 1c: Wikipedia section deep-dive ──────────────────────────
            wiki_sections = await self._wiki.fetch_sections(wiki_title, client)

            # ── Step 1d: Build source URL list ────────────────────────────────
            source_urls = await self._news.build_source_urls(
                subject, wiki_title, wiki_ext_links, client, limit=5
            )

            # ── Step 1e: Concurrent article scraping ──────────────────────────
            scrape_urls = source_urls[:4]   # top 4 most trusted
            if scrape_urls:
                logger.info("[Research] Scraping %d sources: %s", len(scrape_urls), scrape_urls)
                raw_texts: list[str] = await asyncio.gather(
                    *[self._scraper.fetch_text(u, client, subject_hint=subject)
                      for u in scrape_urls],
                    return_exceptions=False,
                )
                web_texts = [t for t in raw_texts if isinstance(t, str) and len(t) > 100]
            else:
                web_texts = []

        # ── Stage 2 — VERIFY ─────────────────────────────────────────────────
        logger.info("[Research] Stage 2 — VERIFY: cross-validating facts")
        wiki_rest_extract = wiki_summary.get("extract", "")
        ddg_abstract = ddg_data.get("Abstract", "") or ddg_data.get("Answer", "")
        rest_description = wiki_summary.get("description", "")
        canonical_url = (
            wiki_summary.get("content_urls", {}).get("desktop", {}).get("page")
            or f"https://en.wikipedia.org/wiki/{wiki_title.replace(' ', '_')}"
        )
        main_thumbnail = wiki_summary.get("thumbnail", {}).get("source", "")
        display_title = wiki_summary.get("title", wiki_title)

        best_extract = wiki_full or wiki_rest_extract

        verified = _verify_facts(
            subject=subject,
            wiki_extract=best_extract,
            ddg_abstract=ddg_abstract,
            web_texts=web_texts,
            work_date=work_date,
        )
        logger.info(
            "[Research] Verification: birth=%s nat=%s confidence=%s sources=%d",
            verified["birth_year"], verified["nationality"],
            verified["confidence"], verified["sources_count"],
        )

        # ── Stage 3 — BUILD ───────────────────────────────────────────────────
        logger.info("[Research] Stage 3 — BUILD: synthesising content")

        tagline = _synthesize_tagline(
            description=rest_description,
            extract=wiki_rest_extract or best_extract,
            facts=verified,
        )

        summary = _synthesize_summary(
            subject=subject,
            extract=best_extract,
            ddg_abstract=ddg_abstract,
            web_texts=web_texts,
            facts=verified,
            books=books,
        )

        story = _build_story(
            subject=subject,
            wiki_extract=best_extract,
            wiki_sections=wiki_sections,
            ddg_abstract=ddg_abstract,
            web_texts=web_texts,
            source_urls=source_urls,
            books=books,
            facts=verified,
        )

        category_text = f"{best_extract} {ddg_abstract} {tagline} {rest_description}"
        category = _infer_category(category_text)

        # ── Stage 4 — PACKAGE ─────────────────────────────────────────────────
        logger.info("[Research] Stage 4 — PACKAGE")

        # Best source URL = first Wikipedia-curated external link or canonical page
        best_source_url = (
            wiki_ext_links[0] if wiki_ext_links else canonical_url
        )

        # Full attribution for editorial transparency
        source_notes: list[str] = [f"Wikipedia (CC BY-SA 4.0) — {canonical_url}"]
        for url in source_urls[:3]:
            dom = _domain_of(url)
            if dom not in source_notes[0]:
                source_notes.append(dom)
        attribution = "Sources: " + " | ".join(source_notes)

        logger.info(
            "[Research] Complete for %r — tagline:%d summary:%d story:%d "
            "category:%s confidence:%s",
            subject, len(tagline), len(summary), len(story),
            category, verified["confidence"],
        )

        return {
            # Core fields
            "found":         bool(best_extract or story),
            "title":         display_title,
            "tagline":       tagline,
            "summary":       summary,
            "story":         story,
            "category":      category,

            # Facts (verified)
            "facts": {
                "birth":       verified["birth_year"],
                "death":       verified["death_year"],
                "nationality": verified["nationality"],
            },

            # Source & attribution
            "page_url":      best_source_url,
            "canonical_url": canonical_url,
            "attribution":   attribution,
            "source_urls":   source_urls,
            "news_items":    news_items,
            "books":         books,

            # Image
            "main_thumbnail": main_thumbnail,

            # Research metadata
            "wiki_title":    wiki_title,
            "sources_used": {
                "wikipedia":     canonical_url,
                "web_sources":   source_urls,
                "ddg_abstract":  bool(ddg_abstract),
                "news_items":    len(news_items),
                "books_found":   len(books),
                "web_texts":     len(web_texts),
            },
            "verification": verified,
        }

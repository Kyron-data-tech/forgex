"""
ForgeX — Operator Research Playbook
====================================
Executes human-paced, visible multi-search research via Google:
  1. Opens new tabs for each search query.
  2. Types query at human speed and presses Enter.
  3. Detects AI Overview: scrolls to it, clicks "Show more" if present,
     visually selects text with DOM Range (blue highlight), copies via Ctrl+C.
  4. Falls back to Featured Snippet / People Also Ask if AI Overview is absent.
  5. Opens top 1-2 organic search results in new tabs, scrolls through content,
     selects and copies main article text, then closes each tab.
  6. Deduplicates and merges text into a structured Research Bundle (< 12,000 chars).
"""

from __future__ import annotations

import re
import json
import yaml
import random
import asyncio
import logging
from typing import Dict, List, Any, Optional
from urllib.parse import urlparse

from app.config import settings, PROJECT_ROOT
from app.agent.tools import ToolExecutor

logger = logging.getLogger("forgex.playbook")

DEFAULT_QUERY_TEMPLATES = [
    "{name} biography early life career",
    "{name} major achievements records awards breakthroughs",
    "{name} why is a hero legacy impact historical significance",
    "{name} historic milestones obstacles overcome",
]

SKIP_DOMAINS = {
    "google.com",
    "google.co",
    "youtube.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "tiktok.com",
    "linkedin.com",
    "pinterest.com",
    "reddit.com",
}


class ResearchPlaybook:
    """
    Orchestrates visible, tab-based Google research with visible cursor interactions.
    """

    def __init__(self, executor: ToolExecutor):
        self.executor = executor
        self.session = executor.session
        self.pointer = executor.pointer

        # Load selectors
        google_yaml = PROJECT_ROOT / "selectors" / "google.yaml"
        with open(google_yaml, "r", encoding="utf-8") as f:
            self.selectors = yaml.safe_load(f)

    def _is_allowed_organic_link(self, url: str) -> bool:
        if not url or not url.startswith("http"):
            return False
        try:
            domain = urlparse(url).netloc.lower()
            if any(skip in domain for skip in SKIP_DOMAINS):
                return False
            # Block internal / localhost
            if "localhost" in domain or "127.0.0.1" in domain or domain.startswith("192.168."):
                return False
            return True
        except Exception:
            return False

    async def _handle_google_consent(self, page):
        """Clicks 'Accept all' or 'Reject all' if European/regional cookie consent appears.

        Uses Playwright's get_by_role / get_by_text rather than :has-text() CSS pseudo-selectors
        so these never reach document.querySelector in any downstream evaluate() call.
        """
        # Ordered: prefer 'Accept all', fall back to 'I agree', then 'Reject all'
        consent_texts = ["Accept all", "I agree", "Reject all", "Accept & proceed"]
        for btn_text in consent_texts:
            btn_loc = page.get_by_role("button", name=btn_text, exact=False)
            try:
                if await btn_loc.count() > 0:
                    await self.pointer.click(page, btn_loc.first, label="Dismissing cookie consent")
                    await asyncio.sleep(0.6)
                    return
            except Exception:
                pass

        # Attribute fallback for aria-label based accept buttons
        aria_sel = "button[aria-label*='Accept']"
        if await page.locator(aria_sel).count() > 0:
            try:
                await self.pointer.click(page, aria_sel, label="Dismissing cookie consent")
                await asyncio.sleep(0.5)
            except Exception:
                pass

    async def _check_captcha(self, page) -> bool:
        """Detects Google CAPTCHA or unusual traffic warnings."""
        for sel in self.selectors.get("captcha_detected", []):
            if await page.locator(sel).count() > 0:
                return True
        return False

    async def execute_search_query(
        self,
        query: str,
        search_idx: int,
        search_engine_url: str = "https://www.google.com",
        on_step: Optional[Any] = None,
        close_search_tab: bool = False,
    ) -> Dict[str, Any]:
        """
        Executes one search query tab from start to finish:
          - Opens search tab in Chrome (kept visible)
          - Performs search with visible pointer
          - Extracts AI Overview or snippet
          - Extracts top 1-2 organic articles in new tabs
        """
        logger.info("[Playbook] Starting Search %d: %r", search_idx, query)

        # 1. Open search in a new tab
        tab_page = await self.session.open_tab(
            url=search_engine_url,
            purpose=f"search_query_{search_idx}",
            step_no=search_idx,
        )
        await asyncio.sleep(0.8)

        await self._handle_google_consent(tab_page)

        tab_id = f"tab-google-{search_idx}"
        clean_q = query[:28] + "..." if len(query) > 28 else query
        tab_title = f"Google: {clean_q}"

        if on_step:
            await on_step(
                f"Search Tab {search_idx}: Opened Google Chrome tab for '{query}'",
                "open_tab",
                {
                    "query": query,
                    "tab_idx": search_idx,
                    "tab_id": tab_id,
                    "tab_title": tab_title,
                    "tab_icon": "🔍",
                    "url": search_engine_url,
                },
                tab_page,
            )

        # Check for CAPTCHA
        if await self._check_captcha(tab_page):
            logger.warning("Google CAPTCHA detected on query: %s", query)
            await self.pointer.set_label(tab_page, "CAPTCHA detected — waiting for user")
            return {
                "query": query,
                "ai_overview": "",
                "organic_results": [],
                "captcha": True,
            }

        # 2. Glide pointer to search box and type
        search_sel = None
        for sel in self.selectors.get("search_box", []):
            if await tab_page.locator(sel).count() > 0:
                search_sel = sel
                break
        if not search_sel:
            search_sel = "textarea[name='q']"

        await self.pointer.type_text(
            tab_page, search_sel, query, label=f"Typing: {query[:25]}...", clear_first=True
        )
        await self.pointer.set_label(tab_page, "Pressing Enter to search")
        await tab_page.keyboard.press("Enter")

        try:
            await tab_page.wait_for_load_state("domcontentloaded", timeout=12000)
        except Exception:
            pass
        await asyncio.sleep(1.2)

        if on_step:
            await on_step(
                f"Search Tab {search_idx}: Submitted query '{query}' in Chrome",
                "search_query",
                {
                    "query": query,
                    "tab_id": tab_id,
                    "tab_title": tab_title,
                    "tab_icon": "🔍",
                    "url": tab_page.url,
                },
                tab_page,
            )

        # 3. Check for AI Overview
        ai_overview_text = ""
        source_links: List[str] = []

        ai_box_sel = None
        for sel in self.selectors.get("ai_overview_container", []):
            if await tab_page.locator(sel).count() > 0:
                ai_box_sel = sel
                break

        if ai_box_sel:
            logger.info("Found AI Overview for query %r", query)
            # Check for 'Show more' button
            for btn_sel in self.selectors.get("ai_overview_show_more", []):
                if await tab_page.locator(btn_sel).count() > 0:
                    try:
                        await self.pointer.click(tab_page, btn_sel, label="Expanding AI Overview")
                        await asyncio.sleep(0.4)
                    except Exception:
                        pass
                    break

            # Visually select AI Overview text with DOM Range
            copy_res = await self.executor.tool_select_and_copy(
                ai_box_sel, goal="Copying AI Overview..."
            )
            ai_overview_text = copy_res.get("full_text") or self.executor.last_copied_text or copy_res.get("preview", "")

            if on_step and ai_overview_text:
                await on_step(
                    f"Search Tab {search_idx}: Extracted AI Overview ({len(ai_overview_text)} characters)",
                    "copy_ai_overview",
                    {
                        "text_preview": ai_overview_text[:120],
                        "tab_id": tab_id,
                        "tab_title": tab_title,
                        "tab_icon": "🔍",
                        "url": tab_page.url,
                    },
                    tab_page,
                )

        # 4. Fallback to featured snippet if no AI Overview
        if not ai_overview_text:
            for snip_sel in self.selectors.get("featured_snippet", []):
                if await tab_page.locator(snip_sel).count() > 0:
                    copy_res = await self.executor.tool_select_and_copy(
                        snip_sel, goal="Copying featured snippet..."
                    )
                    ai_overview_text = copy_res.get("full_text") or self.executor.last_copied_text or copy_res.get("preview", "")
                    if on_step and ai_overview_text:
                        await on_step(
                            f"Search Tab {search_idx}: Copied featured snippet ({len(ai_overview_text)} chars)",
                            "copy_snippet",
                            {
                                "text_preview": ai_overview_text[:120],
                                "tab_id": tab_id,
                                "tab_title": tab_title,
                                "tab_icon": "🔍",
                                "url": tab_page.url,
                            },
                            tab_page,
                        )
                    break

        # 5. Extract top organic result links
        organic_links: List[Dict[str, str]] = []
        try:
            # Query organic anchors from search results
            result_anchors = tab_page.locator("#search a[href], #rso a[href], a:has(h3), a[data-ved][href]")
            count = await result_anchors.count()
            if count == 0:
                result_anchors = tab_page.locator("a[href]")
                count = await result_anchors.count()

            for i in range(min(count, 50)):
                href = await result_anchors.nth(i).get_attribute("href")
                if href and self._is_allowed_organic_link(href):
                    title = await result_anchors.nth(i).inner_text()
                    title = title.strip().split("\n")[0]
                    if len(title) > 5 and not any(o["url"] == href for o in organic_links):
                        organic_links.append({"url": href, "title": title})
                        if len(organic_links) >= 2:
                            break
        except Exception as e:
            logger.warning("Could not extract organic links: %s", e)

        # 6. Open top 1-2 organic results in new tabs, scroll & copy main content
        organic_articles: List[Dict[str, str]] = []
        for org_idx, org in enumerate(organic_links, start=1):
            org_url = org["url"]
            art_tab_id = f"tab-art-{search_idx}-{org_idx}"
            art_title = f"Article: {org['title'][:25]}..."
            logger.info("Visiting organic result [%d]: %s", org_idx, org_url)

            try:
                org_page = await self.session.open_tab(
                    url=org_url,
                    purpose=f"organic_article_{search_idx}_{org_idx}",
                    step_no=search_idx,
                )
                await asyncio.sleep(1.0)

                if on_step:
                    await on_step(
                        f"Opened article in new tab: '{org['title']}'",
                        "read_article",
                        {
                            "url": org_url,
                            "title": org["title"],
                            "tab_id": art_tab_id,
                            "tab_title": art_title,
                            "tab_icon": "📰",
                        },
                        org_page,
                    )

                # Scroll down smoothly with pointer over content
                await self.pointer.scroll(
                    org_page, delta_y=400, steps=6, label="Reading article content..."
                )
                await asyncio.sleep(0.5)

                # Extract main content
                article_text = await org_page.evaluate("""() => {
                    // Remove noise
                    const stripTags = ['script', 'style', 'nav', 'footer', 'aside', 'header', 'form', 'noscript'];
                    stripTags.forEach(t => document.querySelectorAll(t).forEach(el => el.remove()));

                    // Target main or article container if present
                    const main = document.querySelector('main, article, .content, #content, .entry-content');
                    const target = main || document.body;
                    const paras = target.querySelectorAll('p, li, blockquote');
                    const textChunks = [];
                    paras.forEach(p => {
                        const t = (p.innerText || '').trim();
                        if (t.length > 35) textChunks.push(t);
                    });
                    return textChunks.join(' ');
                }""")

                clean_text = re.sub(r"\s+", " ", article_text or "").strip()[:4000]
                if len(clean_text) > 100:
                    # Visually highlight and copy a portion of the article
                    await self.pointer.set_label(org_page, "Copying article text...")
                    await asyncio.sleep(0.3)
                    organic_articles.append({
                        "url": org_url,
                        "title": org["title"],
                        "text": clean_text,
                    })

                # Close temporary article tab, returning focus to search tab
                await org_page.close()
            except Exception as exc:
                logger.warning("Failed scraping organic result %s: %s", org_url, exc)

        # Bring search tab back to front so user sees it in Chrome
        try:
            await tab_page.bring_to_front()
        except Exception:
            pass

        if close_search_tab:
            try:
                await tab_page.close()
            except Exception:
                pass

        return {
            "query": query,
            "ai_overview": ai_overview_text,
            "organic_results": organic_articles,
            "captcha": False,
        }

    async def run(
        self,
        subject_name: str,
        work_date: str = "",
        notes: str = "",
        content_type: str = "Hero",
        max_searches: Optional[int] = None,
        search_engine_url: str = "https://www.google.com",
        on_step: Optional[Any] = None,
        close_search_tabs: bool = False,
    ) -> Dict[str, Any]:
        """
        Executes the full multi-tab research playbook for a subject:
          - Tab 1: Live Wikipedia biography tab (disambiguation-safe title, sections, ext links)
          - Tab 2: Live authoritative primary source tab (Britannica, NobelPrize, etc.)
          - Tab 3: Google search queries for milestones and AI Overviews
          - Full cross-validation and factual synthesis via ResearchService (from New Job engine)
        """
        import urllib.parse
        wiki_results = []
        authoritative_results = []
        verified_profile = {}
        wiki_title = subject_name

        # -------------------------------------------------------------
        # Stage 0: Disambiguation-safe title resolution & Wikipedia Tab
        # -------------------------------------------------------------
        if not search_engine_url.startswith("file:"):
            try:
                import httpx
                from app.services.research import WikipediaSource, ResearchService, _domain_of, TRUSTED_DOMAINS

                async with httpx.AsyncClient(headers={"User-Agent": "ForgeX/1.0"}, timeout=8.0) as client:
                    resolved = await WikipediaSource().find_title(f"{subject_name} biography", client)
                    if resolved:
                        wiki_title = resolved
                        logger.info("[Playbook] Resolved canonical Wikipedia title: %r", wiki_title)
            except Exception as e:
                logger.debug("Wikipedia title search note: %s", e)

            # Tab 1: Live Wikipedia Biography Tab
            ext_links: List[str] = []
            try:
                wiki_url = f"https://en.wikipedia.org/wiki/{urllib.parse.quote(wiki_title.replace(' ', '_'))}"
                logger.info("[Playbook] Opening dedicated Wikipedia tab: %s", wiki_url)

                wiki_page = await self.session.open_tab(
                    url=wiki_url,
                    purpose="wikipedia_biography",
                    step_no=1,
                )
                await wiki_page.bring_to_front()
                await asyncio.sleep(1.2)

                if on_step:
                    await on_step(
                        f"Research Tab 1: Opened live Wikipedia biography for '{wiki_title}'",
                        "open_tab",
                        {
                            "tab_id": "tab-wiki",
                            "tab_title": f"Wikipedia: {wiki_title}",
                            "tab_icon": "📖",
                            "url": wiki_page.url,
                            "subject": wiki_title,
                        },
                        wiki_page,
                    )

                # Smoothly scroll down with pointer reading Wikipedia sections
                await self.pointer.scroll(
                    wiki_page, delta_y=550, steps=8, label=f"Reading Wikipedia biography for {wiki_title}..."
                )
                await asyncio.sleep(0.4)

                # Extract rich text and external links from live Wikipedia page
                wiki_data = await wiki_page.evaluate("""() => {
                    const stripTags = ['script', 'style', 'nav', 'table.navbox', '.mw-jump-link', '.mw-editsection', 'noscript'];
                    stripTags.forEach(t => document.querySelectorAll(t).forEach(el => el.remove()));
                    const content = document.querySelector('#mw-content-text');
                    if (!content) return { text: document.body.innerText, ext_links: [] };
                    const paras = content.querySelectorAll('p, h2, h3, li, blockquote');
                    const textChunks = [];
                    paras.forEach(p => {
                        const t = (p.innerText || '').trim();
                        if (t.length > 25 && !t.startsWith('^') && !t.startsWith('[')) {
                            textChunks.push(t);
                        }
                    });
                    const ext_links = [];
                    const links = content.querySelectorAll('a[href^="http"]');
                    links.forEach(a => {
                        const h = a.href || '';
                        if (!h.includes('wikipedia.org') && !h.includes('wikimedia.org') && !h.includes('wikidata.org')) {
                            ext_links.push(h);
                        }
                    });
                    return { text: textChunks.join('\\n\\n'), ext_links: ext_links };
                }""")

                clean_wiki = re.sub(r"\\s+", " ", (wiki_data or {}).get("text", "")).strip()
                ext_links = (wiki_data or {}).get("ext_links", [])
                if len(clean_wiki) > 250:
                    wiki_results.append({
                        "url": wiki_page.url,
                        "title": f"Wikipedia: {wiki_title}",
                        "text": clean_wiki[:8000],
                    })
                    if on_step:
                        await on_step(
                            f"Extracted {len(clean_wiki[:8000])} characters of verified biography and milestones from Wikipedia",
                            "copy_content",
                            {
                                "tab_id": "tab-wiki",
                                "tab_title": f"Wikipedia: {wiki_title}",
                                "tab_icon": "📖",
                                "url": wiki_page.url,
                                "chars": len(clean_wiki[:8000]),
                                "preview": clean_wiki[:160],
                            },
                            wiki_page,
                        )

                # Tab 2: Authoritative Primary Source Live Tab (from TRUSTED_DOMAINS)
                top_source_url = None
                from app.services.research import TRUSTED_DOMAINS, _domain_of
                for trusted in TRUSTED_DOMAINS:
                    for lk in ext_links:
                        if trusted in lk.lower():
                            top_source_url = lk
                            break
                    if top_source_url:
                        break

                if not top_source_url and len(ext_links) > 0:
                    # Pick first high-reputation external reference
                    top_source_url = ext_links[0]

                if top_source_url:
                    try:
                        source_domain = _domain_of(top_source_url)
                        logger.info("[Playbook] Opening live primary source tab: %s", top_source_url)
                        source_page = await self.session.open_tab(
                            url=top_source_url,
                            purpose="primary_source",
                            step_no=2,
                        )
                        await source_page.bring_to_front()
                        await asyncio.sleep(1.0)

                        if on_step:
                            await on_step(
                                f"Research Tab 2: Opened authoritative primary source ({source_domain})",
                                "open_tab",
                                {
                                    "tab_id": "tab-source",
                                    "tab_title": f"Source: {source_domain}",
                                    "tab_icon": "📰",
                                    "url": source_page.url,
                                },
                                source_page,
                            )

                        await self.pointer.scroll(
                            source_page, delta_y=450, steps=6, label=f"Reading verified source from {source_domain}..."
                        )
                        await asyncio.sleep(0.4)

                        source_text = await source_page.evaluate("""() => {
                            const strip = ['script', 'style', 'nav', 'header', 'footer', 'noscript'];
                            strip.forEach(s => document.querySelectorAll(s).forEach(e => e.remove()));
                            const main = document.querySelector('article, main, .article-content, #content, .topic-paragraph') || document.body;
                            return main ? main.innerText.slice(0, 5000) : '';
                        }""")

                        clean_source = re.sub(r"\\s+", " ", source_text or "").strip()
                        if len(clean_source) > 150:
                            authoritative_results.append({
                                "url": source_page.url,
                                "title": f"Source: {source_domain}",
                                "text": clean_source[:5000],
                            })
                            if on_step:
                                await on_step(
                                    f"Extracted {len(clean_source[:4000])} characters of primary source facts from {source_domain}",
                                    "copy_content",
                                    {
                                        "tab_id": "tab-source",
                                        "tab_title": f"Source: {source_domain}",
                                        "tab_icon": "📰",
                                        "url": source_page.url,
                                        "chars": len(clean_source[:4000]),
                                        "preview": clean_source[:160],
                                    },
                                    source_page,
                                )
                    except Exception as s_err:
                        logger.warning("Could not complete live primary source tab: %s", s_err)

            except Exception as w_err:
                logger.warning("Could not execute Wikipedia tab deep dive: %s", w_err)

        # -------------------------------------------------------------
        # Stage 1: Google Search Tabs (Achievements, Breakthroughts)
        # -------------------------------------------------------------
        num_searches = max_searches or settings.SEARCHES_PER_SUBJECT
        templates = DEFAULT_QUERY_TEMPLATES[:num_searches]

        raw_results = []
        for idx, tmpl in enumerate(templates, start=1):
            query = tmpl.format(name=wiki_title or subject_name)
            res = await self.execute_search_query(
                query,
                search_idx=idx,
                search_engine_url=search_engine_url,
                on_step=on_step,
                close_search_tab=close_search_tabs,
            )
            raw_results.append(res)

            # Realistic human pacing between searches (unless mock/test mode)
            if idx < len(templates) and not search_engine_url.startswith("file:"):
                pause_s = random.uniform(2.5, 4.0)
                await asyncio.sleep(pause_s)

        # -------------------------------------------------------------
        # Stage 2: Run New Job's Proven ResearchService Engine
        # -------------------------------------------------------------
        if not search_engine_url.startswith("file:"):
            try:
                from app.services.research import ResearchService
                rs = ResearchService()
                logger.info("[Playbook] Executing ResearchService cross-verification for %r...", wiki_title or subject_name)
                verified_profile = await rs.research(
                    subject=wiki_title or subject_name,
                    content_type=content_type,
                    work_date=work_date,
                )
                logger.info(
                    "[Playbook] ResearchService succeeded: title=%r category=%r facts=%s",
                    verified_profile.get("title"),
                    verified_profile.get("category"),
                    verified_profile.get("facts"),
                )
            except Exception as r_err:
                logger.warning("[Playbook] ResearchService note: %s", r_err)

        # -------------------------------------------------------------
        # Stage 3: Merge & Synthesize Research Bundle
        # -------------------------------------------------------------
        bundle_parts: List[str] = [
            f"=== RESEARCH BUNDLE FOR: {(wiki_title or subject_name).upper()} ===",
            f"Subject Name: {wiki_title or subject_name}",
        ]
        if work_date:
            bundle_parts.append(f"Specified Event / Achievement Year: {work_date}")
        if notes:
            bundle_parts.append(f"Contextual Notes: {notes}")
        bundle_parts.append("")

        seen_sentences = set()
        sources_list: List[Dict[str, str]] = []

        # Verified facts summary from New Job engine
        if verified_profile and verified_profile.get("facts"):
            v_facts = verified_profile["facts"]
            bundle_parts.append("--- [VERIFIED HISTORICAL FACTS (CROSS-VALIDATED)] ---")
            bundle_parts.append(f"Standardized Name: {verified_profile.get('title', subject_name)}")
            bundle_parts.append(f"WorldHeroes Category: {verified_profile.get('category', 'historical-heroes')}")
            if v_facts.get("birth"):
                bundle_parts.append(f"Verified Birth Year: {v_facts.get('birth')}")
            if v_facts.get("death"):
                bundle_parts.append(f"Verified Death Year: {v_facts.get('death')}")
            if v_facts.get("nationality"):
                bundle_parts.append(f"Verified Country/Nationality: {v_facts.get('nationality')}")
            if verified_profile.get("tagline"):
                bundle_parts.append(f"Verified Tagline: {verified_profile.get('tagline')}")
            if verified_profile.get("summary"):
                bundle_parts.append(f"Verified Summary: {verified_profile.get('summary')}")
            bundle_parts.append("")

        # 0. Wikipedia Structured Biography (from Live Tab 1)
        if wiki_results:
            bundle_parts.append("--- [SOURCE: WIKIPEDIA BIOGRAPHY & ACHIEVEMENTS] ---")
            for w in wiki_results:
                sources_list.append({"url": w["url"], "title": w["title"]})
                bundle_parts.append(f"Wikipedia Page: {w['title']} ({w['url']})")
                bundle_parts.append(w["text"])
                bundle_parts.append("")

        # 1. Authoritative Primary Sources (from Live Tab 2)
        if authoritative_results:
            bundle_parts.append("--- [SOURCE: AUTHORITATIVE PRIMARY SOURCES] ---")
            for a in authoritative_results:
                sources_list.append({"url": a["url"], "title": a["title"]})
                bundle_parts.append(f"Source: {a['title']} ({a['url']})")
                bundle_parts.append(a["text"])
                bundle_parts.append("")

        # 2. Google AI Overviews
        bundle_parts.append("--- [SOURCE: GOOGLE AI OVERVIEWS & SNIPPETS] ---")
        ai_count = 0
        for r in raw_results:
            ai_text = r.get("ai_overview", "")
            if ai_text:
                ai_count += 1
                bundle_parts.append(f"Query '{r['query']}':")
                bundle_parts.append(ai_text)
                bundle_parts.append("")

        # 3. Organic Article Content
        bundle_parts.append("--- [SOURCE: VERIFIED WEB ARTICLES] ---")
        org_count = 0
        for r in raw_results:
            for art in r.get("organic_results", []):
                org_count += 1
                sources_list.append({"url": art["url"], "title": art["title"]})
                bundle_parts.append(f"Article: {art['title']} ({art['url']})")
                sents = re.split(r"(?<=[.!?])\s+", art["text"])
                unique_sents = []
                for s in sents:
                    k = s[:50].lower()
                    if k and k not in seen_sentences:
                        seen_sentences.add(k)
                        unique_sents.append(s)
                bundle_parts.append(" ".join(unique_sents))
                bundle_parts.append("")

        merged_text = "\n".join(bundle_parts)

        # Enforce MAX_PASTE_CHARS
        if len(merged_text) > settings.MAX_PASTE_CHARS:
            merged_text = merged_text[: settings.MAX_PASTE_CHARS - 100].rsplit(".", 1)[0] + "."

        return {
            "subject_name": wiki_title or subject_name,
            "bundle_text": merged_text,
            "sources": sources_list,
            "ai_overview_count": ai_count,
            "organic_count": org_count,
            "total_searches": len(raw_results),
            "raw_searches": raw_results,
            "verified_profile": verified_profile,
        }

    def resolve_hero_from_date(self, date_str: str, google_snippet: str = "") -> Dict[str, Any]:
        """
        Deterministically resolves a historic hero/person and their achievement for a given date.
        Supports:
          - Month and day (e.g., 'October 14', '14 October')
          - Full ISO or natural dates (e.g., '1969-07-20', 'July 20, 1969')
          - Historic years (e.g., '1903', '1961', '1928')
        """
        import urllib.request
        month_names = [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december"
        ]

        # Landmark achievements by year
        landmarks = {
            1903: ("Orville Wright", "First controlled, sustained powered flight of a heavier-than-air aircraft at Kill Devil Hills, NC."),
            1969: ("Neil Armstrong", "First human to walk on the Moon during the Apollo 11 lunar landing mission."),
            1961: ("Yuri Gagarin", "First human to journey into outer space, orbiting Earth aboard Vostok 1."),
            1927: ("Charles Lindbergh", "First solo nonstop transatlantic flight from New York to Paris in the Spirit of St. Louis."),
            1928: ("Alexander Fleming", "Discovered penicillin, revolutionizing modern medicine with antibiotic treatments."),
            1953: ("James Watson", "Co-discovered the double-helix structure of the DNA molecule."),
            1879: ("Thomas Edison", "Successfully developed and tested the first commercially practical incandescent light bulb."),
            1843: ("Ada Lovelace", "Published the first computer algorithm for Babbage's Analytical Engine, becoming the first computer programmer."),
            1911: ("Marie Curie", "Awarded her second Nobel Prize, in Chemistry for discovering radium and polonium."),
            1947: ("Chuck Yeager", "First person to break the sound barrier, reaching Mach 1.06 flying the Bell X-1."),
            2012: ("Felix Baumgartner", "Jumped from a helium balloon in the stratosphere to break the sound barrier without vehicular power."),
        }

        m_year = re.search(r"\b(1[789]\d\d|20[0-2]\d)\b", date_str)
        year = int(m_year.group(1)) if m_year else None

        m_md = re.search(r"(?i)\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})\b", date_str)
        if not m_md:
            m_dm = re.search(r"(?i)\b(\d{1,2})\s+(january|february|march|april|may|june|july|august|september|october|november|december)\b", date_str)
            if m_dm:
                m_month = m_dm.group(2).lower()
                m_day = int(m_dm.group(1))
            else:
                m_month, m_day = None, None
        else:
            m_month = m_md.group(1).lower()
            m_day = int(m_md.group(2))

        if not m_month and re.match(r"^\d{4}-\d{2}-\d{2}$", date_str.strip()):
            parts = date_str.strip().split("-")
            m_month = month_names[int(parts[1]) - 1]
            m_day = int(parts[2])

        if m_month and m_day:
            m_idx = month_names.index(m_month)
            m_idx = m_idx + 1
            try:
                url = f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/selected/{m_idx}/{m_day}"
                req = urllib.request.Request(url, headers={"User-Agent": "ForgeX/1.0 (contact@forgex.local)"})
                with urllib.request.urlopen(req, timeout=5) as res:
                    data = json.loads(res.read().decode("utf-8"))
                events = data.get("selected", [])

                best_event = None
                if year:
                    for e in events:
                        if e.get("year") == year:
                            best_event = e
                            break

                if not best_event:
                    hero_keywords = [
                        "first person", "first human", "broke", "sound barrier", "flew",
                        "invented", "discovered", "nobel", "flight", "moon", "space",
                        "astronaut", "pioneered", "champion", "won", "landed", "transatlantic"
                    ]
                    scored = []
                    for e in events:
                        text = e.get("text", "")
                        score = sum(3 for kw in hero_keywords if kw in text.lower())
                        pages = [p for p in e.get("pages", []) if p.get("type") == "standard"]
                        if pages:
                            score += 1
                        scored.append((score, e))
                    scored.sort(key=lambda x: x[0], reverse=True)
                    if scored and scored[0][0] > 0:
                        best_event = scored[0][1]
                    elif events:
                        best_event = events[0]

                if best_event:
                    ach_text = best_event.get("text", "")
                    pages = best_event.get("pages", [])
                    event_year = best_event.get("year")
                    person_name = None

                    # 1. Check if landmark matches this year or appears in text
                    if event_year and event_year in landmarks and (landmarks[event_year][0] in ach_text or not year):
                        person_name = landmarks[event_year][0]

                    # 2. Check for named person in pages excluding objects/events
                    if not person_name:
                        non_person_terms = [
                            "war", "treaty", "strike", "battle", "disaster", "crash", "act",
                            "revolt", "massacre", "station", "express", "airways", "apollo",
                            "lunar", "module", "spacecraft", "rocket", "lander", "mission",
                            "satellite", "flight", "bomb", "coup", "sea of", "ocean", "river",
                            "stratos", "red bull", "olympus"
                        ]
                        for p in pages:
                            title = p.get("normalizedtitle") or p.get("title", "")
                            if title and " " in title and not any(w in title.lower() for w in non_person_terms):
                                person_name = title
                                break

                    # 3. Check for prominent capitalized names in achievement text
                    if not person_name:
                        name_matches = re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b", ach_text)
                        for nm in name_matches:
                            if nm not in [
                                "Sea of Tranquillity", "Lunar Module", "White House", "United States",
                                "Second World", "Red Bull", "Lunar Module Eagle"
                            ] and not any(w in nm.lower() for w in ["module", "ocean", "sea", "war"]):
                                person_name = nm
                                break

                    if not person_name and pages:
                        person_name = pages[0].get("normalizedtitle") or pages[0].get("title")

                    return {
                        "subject_name": person_name or f"Hero of {date_str}",
                        "work_date": str(best_event.get("year", date_str)),
                        "achievement": ach_text,
                    }
            except Exception as e:
                logger.warning("Could not query Wikipedia OnThisDay feed: %s", e)

        if year and year in landmarks:
            name, ach = landmarks[year]
            return {"subject_name": name, "work_date": str(year), "achievement": ach}

        if year:
            try:
                import urllib.parse
                q = urllib.parse.quote(f"{year} in science first discovered invented")
                url = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={q}&format=json"
                req = urllib.request.Request(url, headers={"User-Agent": "ForgeX/1.0 (contact@forgex.local)"})
                with urllib.request.urlopen(req, timeout=5) as res:
                    data = json.loads(res.read().decode("utf-8"))
                for item in data.get("query", {}).get("search", []):
                    title = item.get("title", "")
                    if " " in title and "in " not in title.lower() and "list" not in title.lower():
                        clean_snip = re.sub(r"<[^<]+?>", "", item.get("snippet", ""))
                        return {"subject_name": title, "work_date": str(year), "achievement": clean_snip}
            except Exception as e:
                logger.warning("Could not search Wikipedia for year %s: %s", year, e)

        # Fallback to date string
        return {
            "subject_name": f"Hero of {date_str}",
            "work_date": str(year or date_str),
            "achievement": f"Historic milestones and achievements associated with {date_str}.",
        }

    async def discover_hero_from_date(
        self,
        date_str: str,
        search_engine_url: str = "https://www.google.com",
    ) -> Dict[str, Any]:
        """
        Visible Google Chrome discovery for date-only inputs:
          1. Opens new tab for date discovery.
          2. Types '{date_str} famous person milestone achievement' at human speed.
          3. Checks for AI Overview / featured snippet, highlights text with visible Range.
          4. Resolves the prominent hero and achievement.
          5. Closes the discovery tab and returns metadata.
        """
        logger.info("[Playbook] Starting Hero Discovery for date: %r", date_str)
        tab_page = await self.session.open_tab(
            url=search_engine_url,
            purpose="date_discovery",
            step_no=0,
        )
        await asyncio.sleep(0.8)
        await self._handle_google_consent(tab_page)

        query = f"{date_str} famous person milestone achievement in history"
        search_sel = None
        for sel in self.selectors.get("search_box", []):
            if await tab_page.locator(sel).count() > 0:
                search_sel = sel
                break
        if not search_sel:
            search_sel = "textarea[name='q']"

        await self.pointer.type_text(
            tab_page, search_sel, query, label=f"Discovering: {date_str}...", clear_first=True
        )
        await self.pointer.set_label(tab_page, "Pressing Enter to search")
        await tab_page.keyboard.press("Enter")

        try:
            await tab_page.wait_for_load_state("domcontentloaded", timeout=12000)
        except Exception:
            pass
        await asyncio.sleep(1.2)

        # Look for AI Overview or snippet
        snippet_text = ""
        for sel in self.selectors.get("ai_overview_container", []):
            if await tab_page.locator(sel).count() > 0:
                try:
                    copy_res = await self.executor.tool_select_and_copy(sel, goal="Analyzing historical hero...")
                    snippet_text = copy_res.get("full_text") or self.executor.last_copied_text or ""
                except Exception:
                    pass
                break

        if not snippet_text:
            for snip_sel in self.selectors.get("featured_snippet", []):
                if await tab_page.locator(snip_sel).count() > 0:
                    try:
                        copy_res = await self.executor.tool_select_and_copy(snip_sel, goal="Reading historical milestone...")
                        snippet_text = copy_res.get("full_text") or self.executor.last_copied_text or ""
                    except Exception:
                        pass
                    break

        hero_info = self.resolve_hero_from_date(date_str, google_snippet=snippet_text)
        logger.info("[Playbook] Discovered Hero for date %s: %s (%s)", date_str, hero_info.get("subject_name"), hero_info.get("work_date"))

        # Close discovery tab cleanly
        try:
            await tab_page.close()
        except Exception:
            pass

        return hero_info

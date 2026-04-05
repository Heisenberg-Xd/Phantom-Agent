"""
Crawler agent — Phase 1.
Discovers all pages, builds an interaction graph, and uses Gemini 2.5 Pro
to build a structured app model (journeys, edge cases, risk areas).
"""

import asyncio
import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

from google import genai
from google.genai import types

from browser.runner import PhantomBrowser

logger = logging.getLogger("phantom")

APP_MODEL_PATH = Path("memory/app_model.json")

from urllib.parse import urlparse


def is_same_domain(base_url: str, candidate_url: str) -> bool:
    base_netloc = urlparse(base_url).netloc
    candidate_netloc = urlparse(candidate_url).netloc
    return base_netloc == candidate_netloc


# ── Gemini client ─────────────────────────────────────────────────────────────

def _make_client() -> genai.Client:
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])

MODEL = "gemini-1.5-flash"
WAIT_TIMES = [10, 30, 60, 60, 60]  # exponential backoff seconds for free tier


class CrawlerAgent:
    """Recursively discovers app structure and builds a Gemini-powered app model."""

    def __init__(
        self,
        browser: PhantomBrowser,
        base_url: str,
        description: str,
        max_pages: int = 30,
        progress_callback=None,
    ):
        self.browser = browser
        self.base_url = base_url
        self.base_domain = urlparse(base_url).netloc
        self.description = description
        self.max_pages = max_pages
        self.progress_callback = progress_callback
        self._client = _make_client()

        self.visited_urls: set[str] = set()
        self.interaction_graph: dict = {"nodes": [], "edges": []}
        self.page_screenshots: dict[str, str] = {}

    # ── URL helpers ───────────────────────────────────────────────────────────

    def _normalize_url(self, url: str, current_url: str) -> Optional[str]:
        try:
            full_url = urljoin(current_url, url)
            parsed = urlparse(full_url)
            normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            if parsed.query:
                normalized += f"?{parsed.query}"
            return normalized if parsed.scheme in ("http", "https") else None
        except Exception:
            return None

    # ── Crawl ─────────────────────────────────────────────────────────────────

    async def crawl(self) -> dict:
        """BFS crawl over all reachable pages."""
        logger.info(f"Starting crawl of {self.base_url}")
        queue = deque([self.base_url])

        while queue and len(self.visited_urls) < self.max_pages:
            url = queue.popleft()
            if url in self.visited_urls:
                continue
            self.visited_urls.add(url)

            if self.progress_callback:
                self.progress_callback(f"Crawling: {url[:70]}")

            try:
                capture = await self.browser.navigate(url, timeout=20000)
                self.page_screenshots[url] = capture.screenshot_path

                node = {
                    "url": capture.url,
                    "title": capture.title,
                    "screenshot": capture.screenshot_path,
                    "load_time": capture.load_time_seconds,
                    "forms": await self.browser.get_all_forms(),
                    "interactive_elements": await self.browser.get_interactive_elements(),
                }
                self.interaction_graph["nodes"].append(node)

                links = await self.browser.get_all_links()
                for link in links:
                    normalized = self._normalize_url(link, capture.url)
                    if normalized:
                        # Hard skip if not on the same domain
                        if not is_same_domain(self.base_url, normalized):
                            continue
                        
                        if normalized not in self.visited_urls:
                            queue.append(normalized)
                            self.interaction_graph["edges"].append({
                                "from": capture.url,
                                "to": normalized,
                                "type": "navigation",
                            })

                await asyncio.sleep(0.3)

            except Exception as e:
                logger.error(f"Crawl error on {url}: {e}")

        logger.info(
            f"Crawl complete. Visited {len(self.visited_urls)} pages, "
            f"found {len(self.interaction_graph['edges'])} edges."
        )
        return self.interaction_graph

    # ── App model ─────────────────────────────────────────────────────────────

    async def build_app_model(self) -> dict:
        """Ask Gemini to analyse the graph and return a structured app model."""
        logger.info("Building app model with Gemini 2.5 Pro...")

        graph_summary = {
            "total_pages": len(self.interaction_graph["nodes"]),
            "total_links": len(self.interaction_graph["edges"]),
            "pages": [
                {
                    "url": n["url"],
                    "title": n["title"],
                    "forms_count": len(n.get("forms", [])),
                    "buttons_count": len(n.get("interactive_elements", [])),
                    "forms": n.get("forms", [])[:3],
                    "buttons": [
                        e["text"] for e in n.get("interactive_elements", [])[:10]
                    ],
                }
                for n in self.interaction_graph["nodes"]
            ],
        }

        prompt = f"""This is a {self.description}. Based on these pages 
and interactive elements found: {json.dumps(graph_summary, indent=2)}

Generate 3-5 concrete user journeys as step-by-step 
Playwright actions. Each journey must include:
- journey name
- steps: list of {{action, selector, value}} dicts
- success_criteria: what must be true at the end

Return JSON only. Use CSS selectors or visible text 
to identify elements. For a todo app example:
{{
  "journeys": [
    {{
      "name": "add and complete a task",
      "steps": [
        {{"action": "fill", "selector": ".new-todo", "value": "Buy groceries"}},
        {{"action": "press", "selector": ".new-todo", "value": "Enter"}},
        {{"action": "click", "selector": ".toggle"}}
      ],
      "success_criteria": "task marked as completed",
      "pages_involved": ["url1"],
      "priority": "high"
    }}
  ],
  "edge_cases": [
    {{
      "journey": "add and complete a task",
      "case": "Empty task",
      "category": "boundary",
      "severity": "medium"
    }}
  ],
  "risk_areas": [
    {{
      "area": "task input",
      "description": "injection through input",
      "url": "url1",
      "type": "form"
    }}
  ]
}}"""

        app_model = await self._call_gemini_with_retry(prompt)

        app_model["interaction_graph"] = self.interaction_graph
        app_model["crawled_urls"] = list(self.visited_urls)
        app_model["base_url"] = self.base_url
        app_model["description"] = self.description

        APP_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        APP_MODEL_PATH.write_text(json.dumps(app_model, indent=2))
        logger.info(f"App model saved to {APP_MODEL_PATH}")
        return app_model

    # ── Gemini helper ─────────────────────────────────────────────────────────

    async def _call_gemini_with_retry(self, prompt: str, max_retries: int = 5) -> dict:
        """Call Gemini with exponential backoff. SDK is sync → run in executor."""
        for attempt in range(max_retries):
            try:
                logger.info(f"Gemini call (attempt {attempt + 1}/{max_retries})...")

                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(
                    None,
                    lambda p=prompt: self._client.models.generate_content(
                        model=MODEL,
                        contents=p,
                    ),
                )

                text = response.text.strip()
                # Strip markdown fences if present
                if "```" in text:
                    for part in text.split("```"):
                        part = part.strip().lstrip("json").strip()
                        try:
                            return json.loads(part)
                        except json.JSONDecodeError:
                            continue
                else:
                    return json.loads(text)

            except json.JSONDecodeError as e:
                logger.warning(f"Gemini invalid JSON (attempt {attempt + 1}): {e}")
                if attempt == max_retries - 1:
                    logger.error("Returning empty app model after JSON parse failures.")
                    return {"journeys": [], "edge_cases": [], "risk_areas": []}

            except Exception as e:
                wait = WAIT_TIMES[min(attempt, len(WAIT_TIMES) - 1)]
                logger.warning(
                    f"Gemini API error (attempt {attempt + 1}/{max_retries}): {e}. "
                    f"Retrying in {wait}s..."
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(wait)
                else:
                    logger.error("Gemini failed after all retries.")
                    return {"journeys": [], "edge_cases": [], "risk_areas": []}

            # Rate-limit buffer between API calls
            await asyncio.sleep(4)

        return {"journeys": [], "edge_cases": [], "risk_areas": []}

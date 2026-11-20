# dealbot/scrapers/base.py
"""Base class and common utilities for pluggable deal scrapers and feed parsers."""
from __future__ import annotations

import abc
import logging
import random
import time
from typing import Any, Dict, List, Optional
import requests

from dealbot.models import Deal

logger = logging.getLogger("dealbot.scrapers.base")

# Modern Chrome and Mobile User-Agents for clean rotating scraping
CHROME_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36",
]

DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
    "Cache-Control": "max-age=0",
    "Upgrade-Insecure-Requests": "1",
}


class BaseDealScraper(abc.ABC):
    """Abstract base class for all pluggable deal scrapers."""

    def __init__(self, name: str, enabled: bool = True):
        self.name = name
        self.enabled = enabled
        self.logger = logging.getLogger(f"dealbot.scrapers.{name}")

    @abc.abstractmethod
    def scrape(self, min_discount: int = 20) -> List[Deal]:
        """Scrape or fetch candidate deals. Must return a list of Deal instances."""
        raise NotImplementedError

    def safe_scrape(self, min_discount: int = 20) -> List[Deal]:
        """Wrap scrape execution with error isolation and logging."""
        if not self.enabled:
            self.logger.debug(f"Scraper '{self.name}' is disabled; skipping.")
            return []

        start_time = time.time()
        try:
            deals = self.scrape(min_discount=min_discount)
            valid_deals = [d for d in deals if isinstance(d, Deal) and d.is_valid()]
            elapsed = time.time() - start_time
            self.logger.info(
                f"[{self.name}] Ingested {len(valid_deals)} valid deals in {elapsed:.2f}s"
            )
            return valid_deals
        except Exception as exc:
            elapsed = time.time() - start_time
            self.logger.error(
                f"[{self.name}] Error during scraping after {elapsed:.2f}s: {exc}",
                exc_info=True,
            )
            return []

    def fetch_url(
        self,
        url: str,
        timeout: int = 10,
        max_retries: int = 2,
        extra_headers: Optional[Dict[str, str]] = None,
        session: Optional[requests.Session] = None,
    ) -> Optional[requests.Response]:
        """Fetch URL with rotating User-Agent, exponential backoff, and error isolation."""
        s = session or requests.Session()
        headers = dict(DEFAULT_HEADERS)
        headers["User-Agent"] = random.choice(CHROME_USER_AGENTS)
        if extra_headers:
            headers.update(extra_headers)

        backoff = 1.0
        for attempt in range(max_retries + 1):
            try:
                resp = s.get(url, headers=headers, timeout=timeout, allow_redirects=True)
                if resp.status_code == 200:
                    return resp
                if resp.status_code in (429, 503):
                    self.logger.warning(
                        f"[{self.name}] Received status {resp.status_code} for {url}. Retrying in {backoff:.1f}s..."
                    )
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue
                self.logger.debug(f"[{self.name}] URL {url} returned HTTP {resp.status_code}")
                return resp
            except requests.RequestException as e:
                if attempt < max_retries:
                    time.sleep(backoff)
                    backoff *= 2.0
                else:
                    self.logger.debug(f"[{self.name}] Failed request to {url}: {e}")
                    return None
        return None

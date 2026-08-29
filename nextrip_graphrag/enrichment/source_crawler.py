from __future__ import annotations

import hashlib
import time
import urllib.robotparser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from ..config import ENRICHMENT_HTTP_TIMEOUT_SECONDS, SOURCE_CRAWL_DELAY_SECONDS
from .catalog import build_source_catalog, load_verified_places
from .io import cache_path, read_json, utc_now, write_json, write_jsonl


DEFAULT_USER_AGENT = "NexTripAI-KB/0.1 (+https://github.com/hieuxinloi/NexTripAI-KB)"
MIN_DOCUMENT_CHARS = 500


def extract_page_text(html: str) -> tuple[str | None, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else None
    for element in soup.select("script, style, nav, footer, form, noscript, svg, aside"):
        element.decompose()
    container = soup.find("article") or soup.find("main") or soup.body or soup
    blocks = [block.get_text(" ", strip=True) for block in container.select("h1, h2, h3, p, li")]
    text = "\n".join(dict.fromkeys(block for block in blocks if block))
    if not text:
        text = container.get_text("\n", strip=True)
    return title, text


class SourceCrawler:
    def __init__(
        self,
        workspace: str | Path,
        delay: float = SOURCE_CRAWL_DELAY_SECONDS,
        timeout: float = ENRICHMENT_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self.workspace = Path(workspace)
        self.cache_dir = self.workspace / "cache" / "source_pages"
        self.delay = delay
        self.client = httpx.Client(
            headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            follow_redirects=True,
            timeout=timeout,
        )
        self.robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.last_request_at: dict[str, float] = {}

    def close(self) -> None:
        self.client.close()

    def _origin(self, url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))

    def _wait(self, origin: str) -> None:
        elapsed = time.monotonic() - self.last_request_at.get(origin, 0.0)
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

    def _get(self, url: str) -> httpx.Response:
        origin = self._origin(url)
        self._wait(origin)
        try:
            response = self.client.get(url)
        finally:
            self.last_request_at[origin] = time.monotonic()
        if response.status_code in {429, 500, 502, 503, 504}:
            retry_after = response.headers.get("Retry-After")
            wait_seconds = min(float(retry_after), 10.0) if retry_after and retry_after.isdigit() else 2.0
            time.sleep(wait_seconds)
            self._wait(origin)
            try:
                response = self.client.get(url)
            finally:
                self.last_request_at[origin] = time.monotonic()
        return response

    def _robots_parser(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        origin = self._origin(url)
        if origin in self.robots:
            return self.robots[origin]
        robots_url = f"{origin}/robots.txt"
        try:
            response = self._get(robots_url)
            if response.status_code in {401, 403}:
                self.robots[origin] = None
                return None
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(robots_url)
            parser.parse(response.text.splitlines() if response.is_success else [])
            self.robots[origin] = parser
            return parser
        except httpx.HTTPError:
            self.robots[origin] = None
            return None

    def crawl(self, entry: dict[str, Any], refresh: bool = False) -> dict[str, Any]:
        path = cache_path(self.cache_dir, entry["url"])
        if path.exists() and not refresh:
            return read_json(path)

        result = {
            **entry,
            "fetched_at": utc_now(),
            "final_url": None,
            "http_status": None,
            "robots_allowed": False,
            "content_type": None,
            "title": None,
            "text": "",
            "content_hash": None,
            "error": None,
            "attribution": {"source_url": entry["url"], "source_name": entry.get("source_name")},
        }
        parser = self._robots_parser(entry["url"])
        if parser is None:
            result["error"] = "robots_unavailable_or_forbidden"
            write_json(path, result)
            return result
        if not parser.can_fetch(DEFAULT_USER_AGENT, entry["url"]):
            result["error"] = "blocked_by_robots"
            write_json(path, result)
            return result

        result["robots_allowed"] = True
        try:
            response = self._get(entry["url"])
            result["http_status"] = response.status_code
            result["final_url"] = str(response.url)
            result["content_type"] = response.headers.get("content-type")
            response.raise_for_status()
            if "html" not in (result["content_type"] or "").lower():
                result["error"] = "unsupported_content_type"
            else:
                title, text = extract_page_text(response.text)
                result["title"] = title
                result["text"] = text
                result["content_hash"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if not text:
                    result["error"] = "empty_page_text"
        except httpx.HTTPError as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        write_json(path, result)
        return result


def crawl_source_documents(
    data_dir: str | Path,
    workspace: str | Path,
    *,
    limit: int | None = None,
    refresh: bool = False,
    delay: float = 0.75,
) -> dict[str, Any]:
    catalog = build_source_catalog(load_verified_places(data_dir))
    if limit is not None:
        catalog = catalog[:limit]
    crawler = SourceCrawler(workspace, delay=delay)
    try:
        documents = [crawler.crawl(entry, refresh=refresh) for entry in catalog]
    finally:
        crawler.close()

    output_dir = Path(workspace) / "output"
    documents_path = output_dir / "source_documents.jsonl"
    for document in documents:
        if not document.get("error") and len(document.get("text") or "") < MIN_DOCUMENT_CHARS:
            document["error"] = "insufficient_article_text"
    write_jsonl(documents_path, documents)
    successful = sum(1 for row in documents if not row.get("error"))
    report = {
        "generated_at": utc_now(),
        "requested": len(catalog),
        "successful": successful,
        "failed": len(documents) - successful,
        "robots_blocked": sum(1 for row in documents if row.get("error") == "blocked_by_robots"),
        "documents_path": str(documents_path.resolve()),
        "failures": [
            {"url": row["url"], "status": row.get("http_status"), "error": row.get("error")}
            for row in documents
            if row.get("error")
        ],
    }
    write_json(output_dir / "crawl_report.json", report)
    return report

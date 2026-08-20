from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4


class BrowserDependencyError(RuntimeError):
    """Raised when Playwright or its Chromium browser is unavailable."""


class CrawlBlockedError(RuntimeError):
    """Raised when a page presents CAPTCHA or automated-traffic blocking."""


@dataclass(frozen=True, slots=True)
class BrowserSnapshot:
    requested_url: str
    final_url: str
    title: str
    html: str
    http_status: int | None
    structured_data: dict[str, object] | None = None


class BrowserClient(Protocol):
    def capture(
        self,
        url: str,
        *,
        wait_selector: str | None = None,
        timeout_seconds: float = 30,
    ) -> BrowserSnapshot: ...


class PlaywrightBrowserClient:
    """Small synchronous Playwright runtime shared by browser crawlers.

    It deliberately does not implement CAPTCHA bypassing or stealth plugins.
    Blocked responses are persisted as diagnostic artifacts and stop the crawl.
    """

    BLOCK_PATTERNS = (
        re.compile(r"captcha", re.IGNORECASE),
        re.compile(r"access denied", re.IGNORECASE),
        re.compile(r"unusual traffic", re.IGNORECASE),
        re.compile(r"automated (?:queries|traffic)", re.IGNORECASE),
        re.compile(r"verify (?:that )?you are human", re.IGNORECASE),
    )

    def __init__(
        self,
        *,
        headless: bool = True,
        locale: str = "vi-VN",
        artifact_directory: str | Path = "data/crawl_artifacts",
        minimum_delay_seconds: float = 1.0,
    ) -> None:
        if minimum_delay_seconds < 0:
            raise ValueError("minimum_delay_seconds cannot be negative")
        self.headless = headless
        self.locale = locale
        self.artifact_directory = Path(artifact_directory)
        self.minimum_delay_seconds = minimum_delay_seconds
        self._last_request_at = 0.0

    def capture(
        self,
        url: str,
        *,
        wait_selector: str | None = None,
        timeout_seconds: float = 30,
    ) -> BrowserSnapshot:
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise BrowserDependencyError(
                'Install browser support with: pip install -e ".[crawl]" '
                "and then run: playwright install chromium"
            ) from error

        self._respect_minimum_delay()
        timeout_ms = int(timeout_seconds * 1000)
        page = None
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=self.headless)
                context = browser.new_context(locale=self.locale)
                page = context.new_page()
                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                if wait_selector:
                    page.wait_for_selector(wait_selector, timeout=timeout_ms)
                page.wait_for_timeout(1000)
                snapshot = BrowserSnapshot(
                    requested_url=url,
                    final_url=page.url,
                    title=page.title(),
                    html=page.content(),
                    http_status=response.status if response else None,
                )
                if self._is_blocked(snapshot):
                    self._save_artifacts(page, snapshot, "blocked")
                    raise CrawlBlockedError(
                        f"Browser crawl was blocked at {snapshot.final_url}"
                    )
                context.close()
                browser.close()
                return snapshot
        except CrawlBlockedError:
            raise
        except PlaywrightError as error:
            if page is not None:
                try:
                    snapshot = BrowserSnapshot(url, page.url, "", page.content(), None)
                    self._save_artifacts(page, snapshot, "error")
                except PlaywrightError:
                    # The Playwright context may already be closed after a
                    # navigation failure. Preserve the original error instead
                    # of masking it while collecting diagnostics.
                    pass
            raise RuntimeError(
                f"Playwright capture failed for {url}: {error}"
            ) from error

    def capture_google_maps_place(
        self,
        url: str,
        *,
        timeout_seconds: float = 45,
    ) -> BrowserSnapshot:
        """Capture a Maps page after expanding its weekly-hours panel."""
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise BrowserDependencyError(
                'Install browser support with: pip install -e ".[crawl]" '
                "and then run: playwright install chromium"
            ) from error

        self._respect_minimum_delay()
        timeout_ms = int(timeout_seconds * 1000)
        page = None
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=self.headless)
                context = browser.new_context(locale=self.locale)
                page = context.new_page()
                response = page.goto(
                    url, wait_until="domcontentloaded", timeout=timeout_ms
                )
                page.wait_for_timeout(1500)
                selected_detail_url: str | None = None
                if "/maps/place/" not in page.url:
                    first_result = page.locator(
                        "a.hfpxzc, [role='feed'] a[href*='/maps/place/'], "
                        "a[href*='/maps/place/']"
                    ).first
                    if first_result.count():
                        detail_url = first_result.get_attribute("href", timeout=3000)
                        if detail_url:
                            selected_detail_url = detail_url
                            detail_response = page.goto(
                                detail_url,
                                wait_until="domcontentloaded",
                                timeout=timeout_ms,
                            )
                            response = detail_response or response
                        else:
                            first_result.click(timeout=5000, no_wait_after=True)
                        page.wait_for_timeout(1200)
                hours_toggle = page.locator(
                    "[data-item-id='oh'], [aria-label='Show open hours for the week']"
                ).first
                if hours_toggle.count():
                    try:
                        hours_toggle.click(timeout=3000)
                        page.wait_for_timeout(500)
                    except PlaywrightError:
                        pass

                def text_of(selector: str) -> str | None:
                    locator = page.locator(selector).first
                    if not locator.count():
                        return None
                    value = locator.inner_text(timeout=2000).strip()
                    return value or None

                def attribute_of(selector: str, name: str) -> str | None:
                    locator = page.locator(selector).first
                    if not locator.count():
                        return None
                    value = locator.get_attribute(name, timeout=2000)
                    return value.strip() if value and value.strip() else None

                try:
                    page.locator("h1").first.wait_for(
                        state="visible", timeout=min(timeout_ms, 5000)
                    )
                except PlaywrightError:
                    pass
                resolved_name = text_of("h1")
                resolved_title = page.title().strip()
                if not resolved_name or resolved_title.casefold() == "google maps":
                    unresolved_snapshot = BrowserSnapshot(
                        requested_url=url,
                        final_url=page.url,
                        title=resolved_title,
                        html=page.content(),
                        http_status=response.status if response else None,
                    )
                    self._save_artifacts(page, unresolved_snapshot, "unresolved")
                    raise RuntimeError(
                        "Google Maps search did not resolve to a place detail page"
                    )

                aria_labels = page.locator("[aria-label]").evaluate_all(
                    "els => els.map(el => el.getAttribute('aria-label')).filter(Boolean)"
                )
                image_urls = page.locator("img[src]").evaluate_all(
                    "els => [...new Set(els.map(el => el.src).filter(src => "
                    "src.includes('googleusercontent') || src.includes('streetviewpixels')))]"
                )
                detail_data: dict[str, object] = {
                    "name": resolved_name,
                    "category": text_of("button.DkEaL"),
                    "address": text_of("button[data-item-id='address'] .Io6YTe")
                    or attribute_of("button[data-item-id='address']", "aria-label"),
                    "phone": text_of("button[data-item-id^='phone'] .Io6YTe"),
                    "website_url": attribute_of("a[data-item-id='authority']", "href"),
                    "menu_url": attribute_of("a[data-item-id='menu']", "href"),
                    "price_text": attribute_of(
                        "span[aria-label^='Price'], span[aria-label^='Mức giá'], "
                        "span[aria-label^='Giá']",
                        "aria-label",
                    ),
                }
                canonical_url = selected_detail_url or page.url
                detail_title = page.title()
                detail_html = page.content()
                menu_image_urls: list[str] = []
                menu_tabs = page.locator("[role='tab']").filter(
                    has_text=re.compile(r"^Menu$", re.IGNORECASE)
                )
                if menu_tabs.count():
                    try:
                        menu_tabs.first.click(timeout=3000, no_wait_after=True)
                        page.wait_for_timeout(1500)
                        menu_image_urls = page.locator(
                            "img[src*='googleusercontent']"
                        ).evaluate_all(
                            "els => {"
                            "const nodes = [...document.querySelectorAll('h1,h2,h3,div,span')];"
                            "const marker = nodes.find(el => "
                            "el.textContent.trim() === 'Highlights');"
                            "const limit = marker ? marker.getBoundingClientRect().top : Infinity;"
                            "return [...new Set(els.filter(el => {"
                            "const r = el.getBoundingClientRect();"
                            "return r.width > 0 && r.height > 0 && r.top >= 0 && r.top < limit;"
                            "}).map(el => el.src))];"
                            "}"
                        )
                    except PlaywrightError:
                        pass
                if not detail_data["menu_url"] and not menu_image_urls:
                    photo_button = page.locator(
                        "button[aria-label^='Photo of'], "
                        "button[aria-label^='Photos of']"
                    ).first
                    if photo_button.count():
                        try:
                            photo_button.click(timeout=3000, no_wait_after=True)
                            page.wait_for_timeout(1500)
                            menu_filters = page.locator("button, [role='tab']").filter(
                                has_text=re.compile(r"^Menu$", re.IGNORECASE)
                            )
                            if menu_filters.count():
                                menu_filters.first.wait_for(
                                    state="visible", timeout=5000
                                )
                                menu_filters.first.click(
                                    timeout=3000, no_wait_after=True
                                )
                                page.wait_for_timeout(1500)
                                menu_image_urls = page.locator("img[src]").evaluate_all(
                                    "els => [...new Set(els.map(el => el.src).filter(src => "
                                    "src.includes('googleusercontent')))]"
                                )
                        except PlaywrightError:
                            pass
                structured_data: dict[str, object] = {
                    **detail_data,
                    "detail_url": selected_detail_url,
                    "aria_labels": aria_labels,
                    "image_urls": image_urls[:20],
                    "menu_image_urls": menu_image_urls[:20],
                }
                snapshot = BrowserSnapshot(
                    requested_url=url,
                    final_url=canonical_url,
                    title=detail_title,
                    html=detail_html,
                    http_status=response.status if response else None,
                    structured_data=structured_data,
                )
                if self._is_blocked(snapshot):
                    self._save_artifacts(page, snapshot, "blocked")
                    raise CrawlBlockedError(
                        f"Browser crawl was blocked at {snapshot.final_url}"
                    )
                context.close()
                browser.close()
                return snapshot
        except CrawlBlockedError:
            raise
        except PlaywrightError as error:
            if page is not None:
                try:
                    snapshot = BrowserSnapshot(url, page.url, "", page.content(), None)
                    self._save_artifacts(page, snapshot, "error")
                except PlaywrightError:
                    pass
            raise RuntimeError(
                f"Playwright Google Maps capture failed for {url}: {error}"
            ) from error

    def capture_google_maps_search_results(
        self,
        url: str,
        *,
        timeout_seconds: float = 45,
        result_limit: int = 30,
        scroll_rounds: int = 8,
    ) -> BrowserSnapshot:
        """Capture a bounded Google Maps result feed for place discovery.

        This method only collects links and text already rendered by the public
        search page.  It deliberately does not bypass CAPTCHA, log in, or open
        every result.  Candidate detail pages are fetched later through the
        normal audited place adapter so discovery and identity confirmation
        remain separate pipeline steps.
        """

        if result_limit < 1:
            raise ValueError("result_limit must be positive")
        if scroll_rounds < 0:
            raise ValueError("scroll_rounds cannot be negative")
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise BrowserDependencyError(
                'Install browser support with: pip install -e ".[crawl]" '
                "and then run: playwright install chromium"
            ) from error

        self._respect_minimum_delay()
        timeout_ms = int(timeout_seconds * 1000)
        page = None
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=self.headless)
                context = browser.new_context(locale=self.locale)
                page = context.new_page()
                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                page.wait_for_timeout(1500)

                feed = page.locator("[role='feed']").first
                previous_count = -1
                unchanged_rounds = 0
                for _ in range(scroll_rounds):
                    links = page.locator(
                        "a.hfpxzc, [role='feed'] a[href*='/maps/place/']"
                    )
                    count = links.count()
                    if count >= result_limit:
                        break
                    if count == previous_count:
                        unchanged_rounds += 1
                        if unchanged_rounds >= 2:
                            break
                    else:
                        unchanged_rounds = 0
                    previous_count = count
                    if not feed.count():
                        break
                    feed.evaluate("element => element.scrollTo(0, element.scrollHeight)")
                    page.wait_for_timeout(750)

                rendered = page.locator(
                    "a.hfpxzc, [role='feed'] a[href*='/maps/place/']"
                ).evaluate_all(
                    """
                    elements => elements.map(element => {
                      const card = element.closest('[role="article"]')
                        || element.parentElement?.parentElement
                        || element.parentElement;
                      return {
                        name: element.getAttribute('aria-label')
                          || element.textContent?.trim()
                          || null,
                        url: element.href || element.getAttribute('href'),
                        card_text: card?.innerText?.trim() || null,
                      };
                    })
                    """
                )
                results: list[dict[str, object]] = []
                seen_urls: set[str] = set()
                for value in rendered:
                    if not isinstance(value, dict):
                        continue
                    candidate_url = value.get("url")
                    if (
                        not isinstance(candidate_url, str)
                        or "/maps/place/" not in candidate_url
                        or candidate_url in seen_urls
                    ):
                        continue
                    seen_urls.add(candidate_url)
                    results.append(
                        {
                            "name": value.get("name"),
                            "url": candidate_url,
                            "card_text": value.get("card_text"),
                            "position": len(results) + 1,
                        }
                    )
                    if len(results) >= result_limit:
                        break

                snapshot = BrowserSnapshot(
                    requested_url=url,
                    final_url=page.url,
                    title=page.title(),
                    html=page.content(),
                    http_status=response.status if response else None,
                    structured_data={"search_results": results},
                )
                if self._is_blocked(snapshot):
                    self._save_artifacts(page, snapshot, "blocked")
                    raise CrawlBlockedError(
                        f"Browser crawl was blocked at {snapshot.final_url}"
                    )
                context.close()
                browser.close()
                return snapshot
        except CrawlBlockedError:
            raise
        except PlaywrightError as error:
            if page is not None:
                try:
                    snapshot = BrowserSnapshot(url, page.url, "", page.content(), None)
                    self._save_artifacts(page, snapshot, "error")
                except PlaywrightError:
                    pass
            raise RuntimeError(
                f"Playwright Google Maps search capture failed for {url}: {error}"
            ) from error

    def _respect_minimum_delay(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.minimum_delay_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _is_blocked(self, snapshot: BrowserSnapshot) -> bool:
        if snapshot.http_status in {401, 403, 429}:
            return True
        sample = f"{snapshot.title}\n{snapshot.html[:200_000]}"
        return any(pattern.search(sample) for pattern in self.BLOCK_PATTERNS)

    def _save_artifacts(
        self, page: object, snapshot: BrowserSnapshot, kind: str
    ) -> None:
        self.artifact_directory.mkdir(parents=True, exist_ok=True)
        artifact_id = uuid4().hex
        html_path = self.artifact_directory / f"{kind}-{artifact_id}.html"
        screenshot_path = self.artifact_directory / f"{kind}-{artifact_id}.png"
        html_path.write_text(snapshot.html, encoding="utf-8")
        page.screenshot(path=str(screenshot_path), full_page=True)

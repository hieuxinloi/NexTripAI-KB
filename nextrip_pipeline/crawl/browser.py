from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from nextrip_pipeline.google_maps_identity import (
    google_maps_official_share_url,
    google_maps_stable_place_url,
)
from nextrip_pipeline.google_maps_price import google_maps_price_evidence_text
from nextrip_pipeline.google_maps_plus_code import google_maps_plus_code_from_text


_GOOGLE_MAPS_WEEKDAYS = (
    "thứ hai",
    "thứ ba",
    "thứ tư",
    "thứ năm",
    "thứ sáu",
    "thứ bảy",
    "chủ nhật",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def _google_maps_weekday_aria_label_count(values: object) -> int:
    """Count distinct weekdays represented by Maps aria-label evidence."""

    if not isinstance(values, (list, tuple)):
        return 0
    weekdays: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        label = " ".join(value.strip().casefold().split())
        for weekday in _GOOGLE_MAPS_WEEKDAYS:
            if label == weekday or label.startswith(f"{weekday},"):
                weekdays.add(weekday)
                break
    return len(weekdays)


def _first_official_google_maps_share_url(values: object) -> str | None:
    """Select the first Google-owned share URL from rendered dialog values."""

    if not isinstance(values, (list, tuple)):
        return None
    for value in values:
        if share_url := google_maps_official_share_url(value):
            return share_url
    return None


def _google_maps_price_text_from_scoped_labels(values: object) -> str | None:
    """Return explicit price evidence from the place header/range panel only.

    Callers must pass labels already scoped to the selected place. Requiring a
    currency symbol or a provider price-level word keeps nearby hotel cards,
    sponsored tours, and labels such as ``Giá phòng cho ...`` out of the
    observation.
    """

    if not isinstance(values, (list, tuple)):
        return None
    for value in values:
        if not isinstance(value, str):
            continue
        if evidence := google_maps_price_evidence_text(value):
            return evidence
    return None


_GOOGLE_SEARCH_STOP_WORDS = {
    "at",
    "da",
    "dia",
    "duong",
    "nam",
    "phuong",
    "quan",
    "tai",
    "thanh",
    "tinh",
    "viet",
}


def _google_maps_search_query(url: str) -> str | None:
    """Extract the human query from a public Maps ``/search/`` URL."""

    path = unquote(urlsplit(url).path)
    marker = "/maps/search/"
    if marker not in path:
        return None
    query = path.split(marker, 1)[1].split("/@", 1)[0].strip("/")
    cleaned = " ".join(query.replace("+", " ").split())
    return cleaned or None


def _google_maps_identity_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKD", value.casefold()).replace("đ", "d")
    ascii_like = "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )
    return " ".join(re.findall(r"[a-z0-9]+", ascii_like))


def _google_maps_place_name_from_url(value: object) -> str | None:
    """Read the visible place slug when Maps omits an anchor aria-label."""

    if not isinstance(value, str):
        return None
    path = unquote(urlsplit(value).path)
    marker = "/maps/place/"
    if marker not in path:
        return None
    slug = path.split(marker, 1)[1].split("/", 1)[0]
    name = " ".join(slug.replace("+", " ").split())
    return name or None


def _google_maps_search_result_score(
    query: str,
    *,
    name: object,
    card_text: object,
) -> float:
    """Rank rendered Maps results without treating the first card as truth."""

    expected_name = _google_maps_identity_text(query.split(",", 1)[0])
    observed_name = _google_maps_identity_text(name)
    if not expected_name or not observed_name:
        return 0.0
    name_similarity = SequenceMatcher(None, expected_name, observed_name).ratio()
    expected_name_tokens = set(expected_name.split())
    observed_name_tokens = set(observed_name.split())
    name_recall = len(expected_name_tokens & observed_name_tokens) / max(
        1, len(expected_name_tokens)
    )

    query_tokens = {
        token
        for token in _google_maps_identity_text(query).split()
        if len(token) > 1 and token not in _GOOGLE_SEARCH_STOP_WORDS
    }
    card_tokens = set(_google_maps_identity_text(card_text).split())
    card_recall = (
        len(query_tokens & card_tokens) / len(query_tokens) if query_tokens else 0.0
    )
    return round(
        0.55 * name_similarity + 0.30 * name_recall + 0.15 * card_recall,
        6,
    )


def _select_google_maps_search_result(
    requested_url: str,
    values: object,
    *,
    minimum_score: float = 0.60,
) -> dict[str, object] | None:
    """Choose the best rendered listing or fail closed when none matches.

    Google Maps can put a sponsored or merely nearby card first. Selection is
    based on the requested place name plus card evidence. The caller keeps the
    search page when no candidate clears the threshold, allowing downstream
    quality gates to receive fallback evidence instead of a false identity.
    """

    if not 0 <= minimum_score <= 1:
        raise ValueError("minimum_score must be between zero and one")
    query = _google_maps_search_query(requested_url)
    if query is None or not isinstance(values, (list, tuple)):
        return None
    candidates: list[tuple[float, int, str, dict[str, object]]] = []
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            continue
        candidate_url = value.get("url")
        if not isinstance(candidate_url, str) or "/maps/place/" not in candidate_url:
            continue
        candidate_name = value.get("name") or _google_maps_place_name_from_url(
            candidate_url
        )
        score = _google_maps_search_result_score(
            query,
            name=candidate_name,
            card_text=value.get("card_text"),
        )
        candidates.append(
            (
                score,
                -index,
                candidate_url,
                {**value, "name": candidate_name},
            )
        )
    if not candidates:
        return None
    score, _, _, selected = max(candidates)
    if score < minimum_score:
        return None
    return {
        **selected,
        "selection_score": score,
        "selection_query": query,
    }


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

    @staticmethod
    def _resolve_google_maps_share_place_url(
        context,  # type: ignore[no-untyped-def]
        page,  # type: ignore[no-untyped-def]
        *,
        timeout_ms: int,
    ) -> tuple[str | None, str | None]:
        """Resolve the rendered Share link without navigating the detail page."""

        try:
            from playwright.sync_api import Error as PlaywrightError
        except ImportError:
            return None, None

        share_url: str | None = None
        share_dialog_opened = False
        resolver_page = None
        try:
            share_button = page.locator(
                "[role='button'][jsaction*='share' i]:visible, "
                "button[jsaction*='share' i]:visible, "
                "button[data-value='Share']:visible, "
                "[role='button'][aria-label*='Share' i]:visible, "
                "[role='button'][aria-label*='Chia sẻ' i]:visible, "
                "button[aria-label*='Share' i]:visible, "
                "button[aria-label*='Chia sẻ' i]:visible"
            ).first
            if not share_button.count():
                return None, None
            share_button.scroll_into_view_if_needed(timeout=2000)
            share_button.click(timeout=3000, no_wait_after=True)
            share_dialog_opened = True
            dialog = page.locator("[role='dialog']").last
            try:
                dialog.wait_for(state="visible", timeout=min(timeout_ms, 5000))
            except PlaywrightError:
                # Some Maps builds ignore a synthetic pointer event on the
                # action-row button. Keyboard activation is a public-UI retry.
                share_button.press("Enter", timeout=2000)
                dialog.wait_for(state="visible", timeout=min(timeout_ms, 5000))
            page.wait_for_timeout(500)
            candidates = dialog.locator("input, a[href]").evaluate_all(
                "els => els.flatMap(el => [el.value, el.href]).filter(Boolean)"
            )
            share_url = _first_official_google_maps_share_url(candidates)
            if share_url is None:
                return None, None
            if stable_url := google_maps_stable_place_url(share_url):
                return share_url, stable_url

            # A Maps share dialog normally exposes maps.app.goo.gl. Resolve it
            # in a separate page so the already-captured detail DOM and menu
            # state are never replaced by the redirect navigation.
            resolver_page = context.new_page()
            resolver_page.goto(
                share_url,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            resolver_page.wait_for_timeout(500)
            return share_url, google_maps_stable_place_url(resolver_page.url)
        except PlaywrightError:
            return share_url, None
        finally:
            if resolver_page is not None:
                try:
                    resolver_page.close()
                except PlaywrightError:
                    pass
            if share_dialog_opened:
                try:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(250)
                except PlaywrightError:
                    pass

    def capture_google_maps_place(
        self,
        url: str,
        *,
        timeout_seconds: float = 45,
        include_menu: bool = False,
    ) -> BrowserSnapshot:
        """Capture one Maps place and optionally inspect its Menu tab.

        Daily place refreshes intentionally leave ``include_menu`` disabled.
        Menu evidence has a separate human-reviewed workflow and must not make
        an otherwise valid place/status crawl slower or less reliable.
        """
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
                selected_detail_score: float | None = None
                rendered_results: list[dict[str, object]] = []
                if "/maps/place/" not in page.url:
                    rendered_values = page.locator(
                        "a.hfpxzc, [role='feed'] a[href*='/maps/place/'], "
                        "a[href*='/maps/place/']"
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
                    rendered_results = [
                        value
                        for value in rendered_values
                        if isinstance(value, dict)
                        and isinstance(value.get("url"), str)
                        and "/maps/place/" in str(value["url"])
                    ][:20]
                    selected_result = _select_google_maps_search_result(
                        url,
                        rendered_results,
                    )
                    if selected_result is not None:
                        detail_url = selected_result.get("url")
                        if isinstance(detail_url, str):
                            selected_detail_url = detail_url
                            selected_detail_score = float(
                                selected_result["selection_score"]
                            )
                            detail_response = page.goto(
                                detail_url,
                                wait_until="domcontentloaded",
                                timeout=timeout_ms,
                            )
                            response = detail_response or response
                        page.wait_for_timeout(1200)

                def pre_expansion_text(selector: str) -> str | None:
                    locator = page.locator(selector).first
                    if not locator.count():
                        return None
                    value = locator.inner_text(timeout=2000).strip()
                    return value or None

                def pre_expansion_attribute(selector: str, name: str) -> str | None:
                    locator = page.locator(selector).first
                    if not locator.count():
                        return None
                    value = locator.get_attribute(name, timeout=2000)
                    return value.strip() if value and value.strip() else None

                try:
                    page.locator("h1.DUwDvf, h1").first.wait_for(
                        state="visible", timeout=min(timeout_ms, 5000)
                    )
                except PlaywrightError:
                    pass
                pre_expansion_detail = {
                    "name": pre_expansion_text("h1.DUwDvf") or pre_expansion_text("h1"),
                    "category": pre_expansion_text("button.DkEaL"),
                    "address": pre_expansion_text(
                        "button[data-item-id='address'] .Io6YTe"
                    )
                    or pre_expansion_attribute(
                        "button[data-item-id='address']", "aria-label"
                    ),
                    "phone": pre_expansion_text(
                        "button[data-item-id^='phone'] .Io6YTe"
                    ),
                    "website_url": pre_expansion_attribute(
                        "a[data-item-id='authority']", "href"
                    ),
                    # ``oloc`` is scoped to the selected place panel. Capture
                    # it before expanding hours because Maps can replace parts
                    # of the detail DOM during that interaction.
                    "plus_code": google_maps_plus_code_from_text(
                        pre_expansion_text("button[data-item-id='oloc'] .Io6YTe")
                        or pre_expansion_attribute(
                            "button[data-item-id='oloc']", "aria-label"
                        )
                    ),
                }
                pre_expansion_title = page.title().strip()
                # Prefer the focusable open-hours control. The localized
                # aria-label is commonly attached to a child icon; clicking
                # that icon does not consistently expand the seven-day table.
                hours_expander = page.locator(
                    "[aria-label*='giờ mở cửa trong tuần' i]:visible, "
                    "[aria-label*='open hours for the week' i]:visible"
                ).first
                hours_control = page.locator(
                    "[role='button'][jsaction*='openhours']:visible"
                ).first
                hours_toggle = (
                    hours_control
                    if hours_control.count()
                    else hours_expander
                    if hours_expander.count()
                    else page.locator("[data-item-id='oh']:visible").first
                )

                def expanded_weekday_count() -> int:
                    labels = page.locator("[aria-label]").evaluate_all(
                        "els => els.map(el => el.getAttribute('aria-label'))"
                        ".filter(Boolean)"
                    )
                    rows = page.locator("table.eK4R0e tr.y0skZc:visible").count()
                    return max(rows, _google_maps_weekday_aria_label_count(labels))

                def wait_for_expanded_weekdays() -> int:
                    try:
                        page.wait_for_function(
                            "() => document.querySelectorAll("
                            "'table.eK4R0e tr.y0skZc').length > 1",
                            timeout=min(timeout_ms, 3500),
                        )
                    except PlaywrightError:
                        pass
                    return expanded_weekday_count()

                if hours_toggle.count():
                    try:
                        hours_toggle.scroll_into_view_if_needed(timeout=2000)
                        hours_toggle.click(timeout=3000)
                        if wait_for_expanded_weekdays() <= 1:
                            # Retry on the fresh focusable container. A child
                            # icon can consume the first pointer event without
                            # toggling the delegated open-hours action.
                            retry_control = page.locator(
                                "[role='button'][jsaction*='openhours']:visible"
                            ).first
                            if retry_control.count():
                                retry_control.evaluate("element => element.click()")
                                wait_for_expanded_weekdays()
                        if expanded_weekday_count() <= 1:
                            # Some Maps builds attach the delegated action to a
                            # focusable div and ignore a synthetic pointer click.
                            # Keyboard activation exercises the same public UI.
                            retry_control = page.locator(
                                "[role='button'][jsaction*='openhours']:visible"
                            ).first
                            retry_control.press("Enter", timeout=2000)
                            wait_for_expanded_weekdays()
                    except PlaywrightError:
                        # A place can legitimately publish only today's hours.
                        # Preserve that evidence and let completeness mark the
                        # weekly schedule as not listed/incomplete.
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

                def scoped_status_text() -> str | None:
                    selectors = (
                        "[role='main'] [aria-label*='Permanently closed'], "
                        "[role='main'] [aria-label*='Đã đóng cửa vĩnh viễn'], "
                        "[role='main'] [aria-label*='Temporarily closed'], "
                        "[role='main'] [aria-label*='Tạm thời đóng cửa'], "
                        "[role='main'] [aria-label*='Open now'], "
                        "[role='main'] [aria-label*='Đang mở cửa'], "
                        "[role='main'] [aria-label*='Closed now'], "
                        "[role='main'] [aria-label*='Đã đóng cửa']"
                    )
                    locator = page.locator(selectors).first
                    if not locator.count():
                        return None
                    aria_label = locator.get_attribute("aria-label", timeout=2000)
                    if aria_label and aria_label.strip():
                        return aria_label.strip()
                    value = locator.inner_text(timeout=2000).strip()
                    return value or None

                def scoped_price_text() -> str | None:
                    """Read only the selected place's header/range evidence."""

                    try:
                        heading = page.locator("h1.DUwDvf, h1").first
                        if not heading.count():
                            return None
                        candidates: list[str] = []
                        # The first sibling after the h1 wrapper is Maps' place
                        # summary (rating, review count, price level/range, and
                        # category). This deliberately excludes hotel cards,
                        # ads, reviews, and menu text farther down the panel.
                        summary = heading.locator(
                            "xpath=parent::*/following-sibling::*[1]"
                        )
                        if summary.count():
                            candidates.extend(
                                summary.locator(
                                    "[role='img'][aria-label]"
                                ).evaluate_all(
                                    "els => els.map(el => el.getAttribute('aria-label'))"
                                    ".filter(Boolean)"
                                )
                            )
                        # Some Maps layouts omit the compact header price but
                        # expose an explicit price-range control in the same
                        # place-information region.
                        region = heading.locator("xpath=ancestor::*[@role='region'][1]")
                        if region.count():
                            candidates.extend(
                                region.locator(
                                    "[role='button'][aria-label^='Price range' i], "
                                    "[role='button'][aria-label^='Khoảng giá' i], "
                                    "[role='button'][aria-label^='Mức giá' i]"
                                ).evaluate_all(
                                    "els => els.map(el => el.getAttribute('aria-label'))"
                                    ".filter(Boolean)"
                                )
                            )
                        return _google_maps_price_text_from_scoped_labels(candidates)
                    except PlaywrightError:
                        return None

                try:
                    page.locator("h1").first.wait_for(
                        state="visible", timeout=min(timeout_ms, 5000)
                    )
                except PlaywrightError:
                    pass
                resolved_name = (
                    pre_expansion_detail["name"]
                    or text_of("h1.DUwDvf")
                    or text_of("h1")
                )
                resolved_title = pre_expansion_title or page.title().strip()
                if (
                    not resolved_name
                    or resolved_name.casefold() == "google maps"
                    or resolved_title.casefold() == "google maps"
                ):
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
                    "category": pre_expansion_detail["category"]
                    or text_of("button.DkEaL"),
                    "address": pre_expansion_detail["address"]
                    or text_of("button[data-item-id='address'] .Io6YTe")
                    or attribute_of("button[data-item-id='address']", "aria-label"),
                    "phone": pre_expansion_detail["phone"]
                    or text_of("button[data-item-id^='phone'] .Io6YTe"),
                    "website_url": pre_expansion_detail["website_url"]
                    or attribute_of("a[data-item-id='authority']", "href"),
                    "plus_code": pre_expansion_detail["plus_code"]
                    or google_maps_plus_code_from_text(
                        text_of("button[data-item-id='oloc'] .Io6YTe")
                        or attribute_of("button[data-item-id='oloc']", "aria-label")
                    ),
                    "menu_url": (
                        attribute_of("a[data-item-id='menu']", "href")
                        if include_menu
                        else None
                    ),
                    "price_text": scoped_price_text(),
                }
                business_status_text = scoped_status_text()
                captured_detail_url = selected_detail_url or page.url
                stable_detail_url = google_maps_stable_place_url(captured_detail_url)
                detail_title = page.title()
                detail_html = page.content()
                official_share_url: str | None = None
                if stable_detail_url is None:
                    official_share_url, stable_detail_url = (
                        self._resolve_google_maps_share_place_url(
                            context,
                            page,
                            timeout_ms=timeout_ms,
                        )
                    )
                canonical_url = stable_detail_url or captured_detail_url
                menu_image_urls: list[str] = []
                menu_tabs = page.locator("[role='tab']").filter(
                    has_text=re.compile(r"^Menu$", re.IGNORECASE)
                )
                if include_menu and menu_tabs.count():
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
                if include_menu and not detail_data["menu_url"] and not menu_image_urls:
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
                    "detail_url": stable_detail_url or selected_detail_url,
                    "selected_detail_score": selected_detail_score,
                    "search_result_candidates": rendered_results,
                    "official_share_url": official_share_url,
                    "business_status_text": business_status_text,
                    "status_evidence_scoped": True,
                    "menu_capture_enabled": include_menu,
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
                    feed.evaluate(
                        "element => element.scrollTo(0, element.scrollHeight)"
                    )
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

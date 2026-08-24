from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import uuid4

from nextrip_pipeline.canonical.models import EntityCityVacancy
from nextrip_pipeline.crawl.browser import BrowserClient
from nextrip_pipeline.crawl.raw_writer import compute_content_hash
from nextrip_pipeline.schemas import EntityType, RecordSubjectType, SourceRecord


MAX_GOOGLE_MAPS_DISCOVERY_RESULTS = 50
DEFAULT_GOOGLE_MAPS_DISCOVERY_RESULTS = 20

_DEFAULT_CITY_NAMES = {
    "city_da_nang": "Đà Nẵng",
    "da_nang": "Đà Nẵng",
    "da nang": "Đà Nẵng",
    "dn": "Đà Nẵng",
    "city_quy_nhon": "Quy Nhơn",
    "quy_nhon": "Quy Nhơn",
    "quy nhon": "Quy Nhơn",
    "qn": "Quy Nhơn",
}
_DEFAULT_ENTITY_SEARCH_TERMS = {
    EntityType.ATTRACTION: "địa điểm tham quan",
    EntityType.CAFE: "quán cà phê",
    EntityType.HOTEL: "khách sạn",
    EntityType.NIGHTLIFE: "bar pub nightlife",
    EntityType.RESTAURANT: "nhà hàng",
}


class GoogleMapsDiscoveryConfigurationError(ValueError):
    """Raised before a search when its bounded query cannot be constructed."""


class GoogleMapsCandidateDiscoveryAdapter:
    """Capture a bounded Google Maps result feed for one canonical vacancy.

    This adapter creates raw evidence only. It never mutates master data,
    validates distinctness, approves a candidate, or allocates a place ID.
    """

    def __init__(
        self,
        browser: BrowserClient,
        *,
        base_url: str = "https://www.google.com/maps/search",
        source_id: str = "google-maps-web",
        parser_version: str = "1.0.0",
        timeout_seconds: float = 45,
        city_names: Mapping[str, str] | None = None,
        entity_search_terms: Mapping[EntityType, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise GoogleMapsDiscoveryConfigurationError(
                "timeout_seconds must be positive"
            )
        self.browser = browser
        self.base_url = base_url.rstrip("/")
        self.source_id = source_id
        self.parser_version = parser_version
        self.timeout_seconds = timeout_seconds
        self.city_names = {
            _city_key(key): value.strip()
            for key, value in (city_names or _DEFAULT_CITY_NAMES).items()
            if value.strip()
        }
        self.entity_search_terms = dict(
            entity_search_terms or _DEFAULT_ENTITY_SEARCH_TERMS
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def query_for(
        self,
        vacancy: EntityCityVacancy,
        *,
        search_term: str | None = None,
        candidate_entity_type: EntityType | None = None,
    ) -> str:
        try:
            city_name = self.city_names[_city_key(vacancy.city_id)]
        except KeyError as error:
            raise GoogleMapsDiscoveryConfigurationError(
                f"unsupported canonical city: {vacancy.city_id!r}"
            ) from error
        if search_term is None:
            entity_type = candidate_entity_type or vacancy.entity_type
            try:
                entity_term = self.entity_search_terms[entity_type].strip()
            except KeyError as error:
                raise GoogleMapsDiscoveryConfigurationError(
                    "missing search term for entity type: "
                    f"{entity_type.value}"
                ) from error
        else:
            entity_term = search_term.strip()
        if not entity_term:
            raise GoogleMapsDiscoveryConfigurationError(
                "blank search term for entity type: "
                f"{(candidate_entity_type or vacancy.entity_type).value}"
            )
        return f"{entity_term} tại {city_name}"

    def fetch(
        self,
        vacancy: EntityCityVacancy,
        *,
        run_id: str,
        result_limit: int = DEFAULT_GOOGLE_MAPS_DISCOVERY_RESULTS,
        search_term: str | None = None,
        candidate_entity_type: EntityType | None = None,
    ) -> SourceRecord:
        _validate_result_limit(result_limit)
        effective_entity_type = candidate_entity_type or vacancy.entity_type
        if (
            effective_entity_type is not vacancy.entity_type
            and not (
                vacancy.entity_type is EntityType.NIGHTLIFE
                and effective_entity_type
                in {EntityType.CAFE, EntityType.RESTAURANT}
            )
        ):
            raise GoogleMapsDiscoveryConfigurationError(
                "cross-type discovery supports only cafe/restaurant "
                "candidates for nightlife vacancies"
            )
        query = self.query_for(
            vacancy,
            search_term=search_term,
            candidate_entity_type=candidate_entity_type,
        )
        requested_url = f"{self.base_url}/{quote(query, safe='')}?hl=vi"
        search_capture = getattr(
            self.browser,
            "capture_google_maps_search_results",
            None,
        )
        if not callable(search_capture):
            raise GoogleMapsDiscoveryConfigurationError(
                "browser does not support Google Maps result-feed capture"
            )
        snapshot = search_capture(
            requested_url,
            timeout_seconds=self.timeout_seconds,
            result_limit=result_limit,
        )
        raw_payload = {
            "request": {
                "vacancy_id": vacancy.vacancy_id,
                "retired_place_id": vacancy.retired_place_id,
                "entity_type": vacancy.entity_type.value,
                "city_id": vacancy.city_id,
                "query": query,
                "result_limit": result_limit,
            },
            "page": {
                "requested_url": snapshot.requested_url,
                "final_url": snapshot.final_url,
                "title": snapshot.title,
                "html": snapshot.html,
                "structured_data": snapshot.structured_data or {},
            },
        }
        if effective_entity_type is not vacancy.entity_type:
            raw_payload["request"]["candidate_entity_type"] = (
                effective_entity_type.value
            )
        return SourceRecord(
            source_record_id=f"google-maps-discovery-{uuid4().hex}",
            run_id=run_id,
            source_id=self.source_id,
            entity_type=effective_entity_type,
            subject_type=RecordSubjectType.PLACE,
            subject_id=vacancy.vacancy_id,
            crawled_at=self.clock(),
            raw_payload=raw_payload,
            content_hash=compute_content_hash(raw_payload),
            parser_version=self.parser_version,
            source_url=snapshot.final_url,
            http_status=snapshot.http_status,
            content_type="text/html",
        )


def _city_key(value: str) -> str:
    return " ".join(value.strip().casefold().replace("-", " ").split())


def _validate_result_limit(value: int) -> None:
    if isinstance(value, bool) or not 1 <= value <= MAX_GOOGLE_MAPS_DISCOVERY_RESULTS:
        raise GoogleMapsDiscoveryConfigurationError(
            "result_limit must be between 1 and "
            f"{MAX_GOOGLE_MAPS_DISCOVERY_RESULTS}"
        )

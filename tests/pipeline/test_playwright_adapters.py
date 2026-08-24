from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from nextrip_pipeline.crawl import BrowserSnapshot, RawJsonWriter
from nextrip_pipeline.crawl.adapters import (
    GoogleMapsPlaceAdapter,
    TrivagoPriceAdapter,
    TrivagoPriceRequest,
)
from nextrip_pipeline.jobs import GoogleMapsOpeningJob, TrivagoHotelPriceJob
from nextrip_pipeline.schemas import (
    EntityType,
    ExternalEntityMapping,
    MappingStatus,
    Occupancy,
    RecordSubjectType,
)


NOW = datetime(2026, 8, 18, 5, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures"


class FixtureBrowser:
    def __init__(self, fixture_name: str) -> None:
        self.html = (FIXTURES / fixture_name).read_text(encoding="utf-8")
        self.requested_urls: list[str] = []

    def capture(
        self,
        url: str,
        *,
        wait_selector: str | None = None,
        timeout_seconds: float = 30,
    ) -> BrowserSnapshot:
        self.requested_urls.append(url)
        return BrowserSnapshot(
            requested_url=url,
            final_url=f"{url}&captured=1",
            title="fixture",
            html=self.html,
            http_status=200,
        )


def _mapping(
    source_id: str,
    entity_type: EntityType,
    *,
    external_id: str = "external-1",
) -> ExternalEntityMapping:
    return ExternalEntityMapping(
        mapping_id=f"mapping-{source_id}",
        entity_id="place-1",
        entity_type=entity_type,
        source_id=source_id,
        external_id=external_id,
        status=MappingStatus.CONFIRMED,
        matched_at=NOW,
        verified_at=NOW,
    )


def test_trivago_adapter_captures_html_and_json_ld() -> None:
    browser = FixtureBrowser("trivago_hotel.html")
    adapter = TrivagoPriceAdapter(browser, clock=lambda: NOW)
    record = adapter.fetch(
        _mapping("trivago-web", EntityType.HOTEL),
        TrivagoPriceRequest(
            hotel_name="Khách sạn mẫu",
            destination="Quy Nhơn",
            check_in=date(2026, 8, 20),
            check_out=date(2026, 8, 21),
            occupancy=Occupancy(adults=2, rooms=1),
        ),
        run_id="price-run",
    )

    assert record.subject_type is RecordSubjectType.HOTEL_PRICE
    assert record.raw_payload["page"]["json_ld"][0]["@type"] == "Hotel"
    assert "checkin=2026-08-20" in browser.requested_urls[0]


def test_google_maps_adapter_uses_mapping_search_query() -> None:
    browser = FixtureBrowser("google_maps_place.html")
    adapter = GoogleMapsPlaceAdapter(browser, clock=lambda: NOW)
    mapping = _mapping(
        "google-maps-web",
        EntityType.RESTAURANT,
        external_id="Nhà hàng mẫu Quy Nhơn",
    )
    record = adapter.fetch(mapping, run_id="opening-run")

    assert record.subject_type is RecordSubjectType.OPENING_STATUS
    assert record.raw_payload["page"]["json_ld"][0]["geo"]["latitude"] == 13.78
    assert "Nh%C3%A0%20h%C3%A0ng" in browser.requested_urls[0]
    assert parse_qs(urlsplit(browser.requested_urls[0]).query)["hl"] == ["vi"]


def test_google_maps_adapter_uses_search_url_with_master_coordinates() -> None:
    browser = FixtureBrowser("google_maps_place.html")
    adapter = GoogleMapsPlaceAdapter(browser, clock=lambda: NOW)
    mapping = _mapping(
        "google-maps-web",
        EntityType.CAFE,
        external_id="Xom Meo Coffee Da Nang",
    ).model_copy(
        update={
            "attributes": {
                "search_query": "Xom Meo Coffee Da Nang",
                "master_latitude": 16.0462401,
                "master_longitude": 108.2371618,
            }
        }
    )

    record = adapter.fetch(mapping, run_id="opening-run")

    requested_url = browser.requested_urls[0]
    assert "/maps/search/" in requested_url
    assert "/maps/place/" not in requested_url
    assert "@16.0462401,108.2371618,17z" in requested_url
    assert parse_qs(urlsplit(requested_url).query)["hl"] == ["vi"]
    assert record.raw_payload["page"]["used_master_coordinates_for_viewport"] is True


def test_google_maps_adapter_forces_vi_on_external_url_and_retains_query() -> None:
    browser = FixtureBrowser("google_maps_place.html")
    adapter = GoogleMapsPlaceAdapter(browser, clock=lambda: NOW)
    mapping = _mapping(
        "google-maps-web",
        EntityType.CAFE,
        external_id="stable-google-id",
    ).model_copy(
        update={
            "external_url": (
                "https://www.google.com/maps/place/Test?api=1&entry=ttu&"
                "query_place_id=ChIJabc%2B123&hl=en"
            )
        }
    )

    adapter.fetch(mapping, run_id="opening-run")

    requested_url = browser.requested_urls[0]
    query = parse_qs(urlsplit(requested_url).query)
    assert query == {
        "api": ["1"],
        "entry": ["ttu"],
        "query_place_id": ["ChIJabc+123"],
        "hl": ["vi"],
    }
    assert "hl=en" not in requested_url


def test_browser_jobs_write_separate_raw_partitions(tmp_path: Path) -> None:
    hotel_mapping = _mapping("trivago-web", EntityType.HOTEL)
    hotel_job = TrivagoHotelPriceJob(
        TrivagoPriceAdapter(FixtureBrowser("trivago_hotel.html"), clock=lambda: NOW),
        RawJsonWriter(tmp_path),
        clock=lambda: NOW,
    )
    hotel_result = hotel_job.run(
        [
            (
                hotel_mapping,
                TrivagoPriceRequest(
                    hotel_name="Khách sạn mẫu",
                    destination="Quy Nhơn",
                    check_in=date(2026, 8, 20),
                    check_out=date(2026, 8, 21),
                    occupancy=Occupancy(adults=2, rooms=1),
                ),
            )
        ],
        run_id="price-run",
    )
    maps_job = GoogleMapsOpeningJob(
        GoogleMapsPlaceAdapter(
            FixtureBrowser("google_maps_place.html"), clock=lambda: NOW
        ),
        RawJsonWriter(tmp_path),
        clock=lambda: NOW,
    )
    maps_result = maps_job.run(
        [_mapping("google-maps-web", EntityType.CAFE)],
        run_id="opening-run",
    )

    assert hotel_result.succeeded
    assert maps_result.succeeded
    assert "entity=hotel_price" in hotel_result.written_paths[0].as_posix()
    assert "entity=opening_status" in maps_result.written_paths[0].as_posix()

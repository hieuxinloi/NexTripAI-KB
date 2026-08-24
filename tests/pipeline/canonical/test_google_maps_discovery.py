from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from nextrip_pipeline.canonical.discovery import (
    CandidateStageAlreadyExistsError,
    GoogleMapsCandidateDiscovery,
    GoogleMapsCandidateStageWriter,
    GoogleMapsDiscoveryPayloadError,
    stage_google_maps_candidates,
)
from nextrip_pipeline.canonical.models import EntityCityVacancy, stable_identifier
from nextrip_pipeline.crawl.adapters.google_maps_discovery import (
    GoogleMapsCandidateDiscoveryAdapter,
    GoogleMapsDiscoveryConfigurationError,
)
from nextrip_pipeline.crawl.browser import BrowserSnapshot
from nextrip_pipeline.crawl.raw_writer import RawJsonWriter
from nextrip_pipeline.schemas import EntityType


NOW = datetime(2026, 8, 20, 10, tzinfo=timezone.utc)


def _vacancy() -> EntityCityVacancy:
    vacancy_id = stable_identifier(
        "vacancy",
        "city_da_nang",
        EntityType.CAFE.value,
        "cafe_dn_008",
    )
    return EntityCityVacancy(
        vacancy_id=vacancy_id,
        retired_place_id="cafe_dn_008",
        city_id="city_da_nang",
        entity_type=EntityType.CAFE,
    )


def _nightlife_vacancy() -> EntityCityVacancy:
    retired_place_id = "night_dn_008"
    return EntityCityVacancy(
        vacancy_id=stable_identifier(
            "vacancy",
            "city_da_nang",
            EntityType.NIGHTLIFE.value,
            retired_place_id,
        ),
        retired_place_id=retired_place_id,
        city_id="city_da_nang",
        entity_type=EntityType.NIGHTLIFE,
    )


class FakeSearchBrowser:
    def __init__(self, results: object) -> None:
        self.results = results
        self.calls: list[tuple[str, float, int]] = []

    def capture_google_maps_search_results(
        self,
        url: str,
        *,
        timeout_seconds: float,
        result_limit: int,
    ) -> BrowserSnapshot:
        self.calls.append((url, timeout_seconds, result_limit))
        return BrowserSnapshot(
            requested_url=url,
            final_url=url,
            title="Google Maps cafe results",
            html="<html>audited search result</html>",
            http_status=200,
            structured_data={"search_results": self.results},
        )


def _valid_results() -> list[dict[str, object]]:
    return [
        {
            "name": "Cafe Mới Một",
            "url": "https://www.google.com/maps/place/Cafe+Moi/data=!4m2!3m1!1s0x123:0x456?hl=vi",
            "card_text": "Cafe Mới Một\n4.8 stars",
            "position": 1,
        },
        {
            "name": "Duplicate rendered card",
            "url": "https://www.google.com/maps/place/Changed+Slug/@16.1,108.2,17z/data=!4m2!3m1!1s0x123:0x456?entry=ttu",
            "card_text": "same place",
            "position": 2,
        },
        {
            "name": "Cafe Mới Hai",
            "url": "https://www.google.com/maps/place/Cafe+Hai/data=!4m2!3m1!1sChIJdefABC",
            "card_text": None,
            "position": 3,
        },
    ]


def test_adapter_builds_vacancy_query_and_raw_source_record() -> None:
    browser = FakeSearchBrowser(_valid_results())
    adapter = GoogleMapsCandidateDiscoveryAdapter(
        browser,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    record = adapter.fetch(_vacancy(), run_id="discovery-run-1", result_limit=5)

    assert browser.calls[0][2] == 5
    assert "qu%C3%A1n%20c%C3%A0%20ph%C3%AA%20t%E1%BA%A1i%20%C4%90%C3%A0%20N%E1%BA%B5ng" in browser.calls[0][0]
    assert record.subject_id == _vacancy().vacancy_id
    assert record.entity_type is EntityType.CAFE
    assert record.raw_payload["request"]["query"] == "quán cà phê tại Đà Nẵng"
    assert record.raw_payload["page"]["html"] == "<html>audited search result</html>"


def test_stage_parses_unique_candidates_with_stable_key_and_exact_maps_url() -> None:
    browser = FakeSearchBrowser(_valid_results())
    record = GoogleMapsCandidateDiscoveryAdapter(
        browser,  # type: ignore[arg-type]
        clock=lambda: NOW,
    ).fetch(_vacancy(), run_id="discovery-run-1", result_limit=5)

    first = stage_google_maps_candidates(record, _vacancy())
    second = stage_google_maps_candidates(record, _vacancy())

    assert len(first.candidates) == 2
    assert first.candidates[0].candidate.candidate_key == (
        second.candidates[0].candidate.candidate_key
    )
    assert first.candidates[0].card_text == "Cafe Mới Một\n4.8 stars"
    source_url = _valid_results()[0]["url"]
    assert str(first.candidates[0].source_url) == source_url
    assert str(
        first.candidates[0].candidate.external_identities[0].external_url
    ) == source_url
    assert (
        first.candidates[0].candidate.external_identities[0].external_id
        == "0x123:0x456"
    )
    assert first.candidates[0].status.value == "discovered"


def test_service_writes_immutable_raw_evidence_then_candidate_stage(tmp_path) -> None:
    browser = FakeSearchBrowser(_valid_results())
    service = GoogleMapsCandidateDiscovery(
        GoogleMapsCandidateDiscoveryAdapter(
            browser,  # type: ignore[arg-type]
            clock=lambda: NOW,
        ),
        RawJsonWriter(tmp_path / "raw"),
        GoogleMapsCandidateStageWriter(tmp_path / "staging"),
    )

    result = service.run(
        _vacancy(),
        run_id="discovery-run-1",
        result_limit=5,
    )

    assert result.raw_path.is_file()
    assert result.stage_path.is_file()
    persisted = json.loads(result.stage_path.read_text(encoding="utf-8"))
    assert persisted["vacancy"]["retired_place_id"] == "cafe_dn_008"
    assert persisted["candidates"][0]["status"] == "discovered"
    with pytest.raises(CandidateStageAlreadyExistsError, match="immutable"):
        GoogleMapsCandidateStageWriter(tmp_path / "staging").write(result.stage)


def test_cross_type_discovery_preserves_target_vacancy_and_actual_type(
    tmp_path,
) -> None:
    browser = FakeSearchBrowser(_valid_results())
    service = GoogleMapsCandidateDiscovery(
        GoogleMapsCandidateDiscoveryAdapter(
            browser,  # type: ignore[arg-type]
            clock=lambda: NOW,
        ),
        RawJsonWriter(tmp_path / "raw"),
        GoogleMapsCandidateStageWriter(tmp_path / "staging"),
    )

    result = service.run(
        _nightlife_vacancy(),
        run_id="cross-type-discovery-run",
        result_limit=5,
        candidate_entity_type=EntityType.CAFE,
    )

    request = result.source_record.raw_payload["request"]
    assert result.source_record.entity_type is EntityType.CAFE
    assert request["entity_type"] == EntityType.NIGHTLIFE.value
    assert request["candidate_entity_type"] == EntityType.CAFE.value
    assert result.stage.vacancy.retired_place_id == "night_dn_008"
    assert result.stage.candidate_entity_type is EntityType.CAFE
    assert all(
        item.candidate.entity_type is EntityType.CAFE
        for item in result.stage.candidates
    )
    assert "qu%C3%A1n%20c%C3%A0%20ph%C3%AA" in browser.calls[0][0]


def test_malformed_result_keeps_raw_audit_but_creates_no_stage(tmp_path) -> None:
    browser = FakeSearchBrowser(
        [
            {
                "name": "",
                "url": "https://www.google.com/maps/place/NoName",
                "card_text": "missing required name",
                "position": 1,
            }
        ]
    )
    service = GoogleMapsCandidateDiscovery(
        GoogleMapsCandidateDiscoveryAdapter(
            browser,  # type: ignore[arg-type]
            clock=lambda: NOW,
        ),
        RawJsonWriter(tmp_path / "raw"),
        GoogleMapsCandidateStageWriter(tmp_path / "staging"),
    )

    with pytest.raises(GoogleMapsDiscoveryPayloadError, match="name"):
        service.run(_vacancy(), run_id="malformed-run", result_limit=5)

    assert len(list((tmp_path / "raw").rglob("*.json"))) == 1
    assert list((tmp_path / "staging").rglob("*.json")) == []


@pytest.mark.parametrize("result_limit", [0, 51, True])
def test_result_limit_is_strictly_bounded(result_limit) -> None:
    browser = FakeSearchBrowser([])
    adapter = GoogleMapsCandidateDiscoveryAdapter(
        browser,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    with pytest.raises(GoogleMapsDiscoveryConfigurationError, match="between 1 and 50"):
        adapter.fetch(
            _vacancy(),
            run_id="discovery-run-1",
            result_limit=result_limit,
        )
    assert browser.calls == []


def test_non_place_google_url_fails_closed() -> None:
    browser = FakeSearchBrowser(
        [
            {
                "name": "Not a place detail",
                "url": "https://www.google.com/maps/search/cafe",
                "card_text": None,
                "position": 1,
            }
        ]
    )
    record = GoogleMapsCandidateDiscoveryAdapter(
        browser,  # type: ignore[arg-type]
        clock=lambda: NOW,
    ).fetch(_vacancy(), run_id="discovery-run-1", result_limit=5)

    with pytest.raises(GoogleMapsDiscoveryPayloadError, match="place URL"):
        stage_google_maps_candidates(record, _vacancy())

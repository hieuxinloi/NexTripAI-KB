from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from nextrip_pipeline.canonical.candidate import (
    CandidateDisposition,
    CandidateExternalIdentity,
    CandidateReasonCode,
    CanonicalReplacementCandidate,
    ExistingCanonicalIdentity,
)
from nextrip_pipeline.canonical.detail import (
    CandidateDetailAlreadyExistsError,
    CandidateDetailStageWriter,
    GoogleMapsCandidateDetail,
)
from nextrip_pipeline.canonical.discovery import StagedGoogleMapsCandidate
from nextrip_pipeline.canonical.models import EntityCityVacancy, stable_identifier
from nextrip_pipeline.crawl.adapters.google_maps import GoogleMapsPlaceAdapter
from nextrip_pipeline.crawl.browser import BrowserSnapshot
from nextrip_pipeline.crawl.raw_writer import RawJsonWriter
from nextrip_pipeline.preprocessing.google_maps import GoogleMapsNormalizationError
from nextrip_pipeline.schemas import EntityType


NOW = datetime(2026, 8, 20, 11, tzinfo=timezone.utc)
STABLE_ID = "0x314219c8f123:0xabc456"


def _vacancy() -> EntityCityVacancy:
    return EntityCityVacancy(
        vacancy_id=stable_identifier(
            "vacancy",
            "city_da_nang",
            EntityType.CAFE.value,
            "cafe_dn_008",
        ),
        retired_place_id="cafe_dn_008",
        city_id="city_da_nang",
        entity_type=EntityType.CAFE,
    )


def _maps_url(*, token: bool = True, coordinates: bool = True) -> str:
    viewport = "/@16.0471,108.2062,17z" if coordinates else ""
    data = f"/data=!4m2!3m1!1s{STABLE_ID}" if token else ""
    return f"https://www.google.com/maps/place/Cafe+Moi{viewport}{data}"


def _staged(*, url: str | None = None) -> StagedGoogleMapsCandidate:
    source_url = url or _maps_url()
    external_id = STABLE_ID if STABLE_ID in source_url else None
    candidate = CanonicalReplacementCandidate(
        candidate_key=stable_identifier("candidate", source_url),
        entity_type=EntityType.CAFE,
        city_id="city_da_nang",
        name="Search-result name",
        external_identities=[
            CandidateExternalIdentity(
                source_id="google-maps-web",
                external_id=external_id,
                external_url=source_url,
            )
        ],
    )
    return StagedGoogleMapsCandidate(
        candidate=candidate,
        source_url=source_url,
        result_position=1,
        card_text="Search card",
    )


class FakeDetailBrowser:
    def __init__(self, final_url: str, *, name: str = "Tên chính thức Google") -> None:
        self.final_url = final_url
        self.name = name
        self.requested_urls: list[str] = []

    def capture_google_maps_place(
        self,
        url: str,
        *,
        timeout_seconds: float,
    ) -> BrowserSnapshot:
        self.requested_urls.append(url)
        return BrowserSnapshot(
            requested_url=url,
            final_url=self.final_url,
            title=f"{self.name} - Google Maps",
            html="<html>Open now</html>",
            http_status=200,
            structured_data={
                "name": self.name,
                "category": "Coffee shop",
                "address": "12 Example Street, Da Nang",
                "phone": "0905 123 456",
                "website_url": "https://cafe-moi.example/menu",
                "detail_url": self.final_url,
            },
        )


def _service(tmp_path, final_url: str) -> tuple[GoogleMapsCandidateDetail, FakeDetailBrowser]:
    browser = FakeDetailBrowser(final_url)
    adapter = GoogleMapsPlaceAdapter(
        browser,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    service = GoogleMapsCandidateDetail(
        adapter,
        RawJsonWriter(tmp_path / "raw"),
        CandidateDetailStageWriter(tmp_path / "detail"),
        clock=lambda: NOW,
    )
    return service, browser


def test_fetches_exact_url_and_projects_full_google_candidate(tmp_path) -> None:
    staged = _staged()
    service, browser = _service(tmp_path, _maps_url())

    result = service.run(
        staged,
        _vacancy(),
        [],
        run_id="candidate-detail-run-1",
    )

    assert browser.requested_urls == [str(staged.source_url)]
    assert result.mapping.status.value == "auto_matched"
    assert result.mapping.attributes.get("master_latitude") is None
    candidate = result.detail.candidate
    assert candidate.name == "Tên chính thức Google"
    assert candidate.phone == "0905 123 456"
    assert candidate.provider_category == "Coffee shop"
    assert candidate.resolved_address == "12 Example Street, Da Nang"
    assert candidate.business_status is not None
    assert candidate.business_status.value == "active"
    assert str(candidate.website_url) == "https://cafe-moi.example/menu"
    assert candidate.location is not None
    assert candidate.location.latitude == 16.0471
    assert candidate.location.source == "google-maps-web"
    assert candidate.external_identities[0].external_id == STABLE_ID
    assert str(candidate.external_identities[0].external_url) == _maps_url()
    assert result.detail.validation.status is CandidateDisposition.PASS
    assert CandidateReasonCode.GOOGLE_PROVIDER_CATEGORY_COMPATIBLE in (
        result.detail.validation.reason_codes
    )
    assert result.raw_path.is_file()
    assert result.detail_path.is_file()


def test_detail_validation_rejects_existing_google_identity(tmp_path) -> None:
    service, _ = _service(tmp_path, _maps_url())
    existing = ExistingCanonicalIdentity(
        place_id="cafe_dn_099",
        entity_type=EntityType.CAFE,
        city_id="city_da_nang",
        name="Old canonical name",
        external_identities=[
            CandidateExternalIdentity(
                source_id="google-maps-web",
                external_id=STABLE_ID,
            )
        ],
    )

    result = service.run(
        _staged(),
        _vacancy(),
        [existing],
        run_id="duplicate-detail-run",
    )

    assert result.detail.validation.status is CandidateDisposition.REJECT
    assert CandidateReasonCode.EXTERNAL_ID_MATCH in (
        result.detail.validation.reason_codes
    )


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        (
            _maps_url(token=False),
            CandidateReasonCode.GOOGLE_STABLE_EXTERNAL_ID_MISSING,
        ),
        (
            _maps_url(coordinates=False),
            CandidateReasonCode.GOOGLE_SOURCED_COORDINATES_MISSING,
        ),
    ],
)
def test_missing_required_google_identity_evidence_forces_review(
    tmp_path,
    url: str,
    reason: CandidateReasonCode,
) -> None:
    service, _ = _service(tmp_path, url)

    result = service.run(
        _staged(url=url),
        _vacancy(),
        [],
        run_id=f"review-{reason.value}",
    )

    assert result.detail.validation.status is CandidateDisposition.REVIEW
    assert reason in result.detail.validation.reason_codes


def test_detail_file_is_immutable(tmp_path) -> None:
    service, _ = _service(tmp_path, _maps_url())
    result = service.run(
        _staged(),
        _vacancy(),
        [],
        run_id="immutable-detail-run",
    )
    persisted = json.loads(result.detail_path.read_text(encoding="utf-8"))
    assert persisted["candidate"]["name"] == "Tên chính thức Google"

    with pytest.raises(CandidateDetailAlreadyExistsError, match="immutable"):
        CandidateDetailStageWriter(tmp_path / "detail").write(result.detail)


def test_normalization_failure_preserves_raw_and_writes_no_detail(tmp_path) -> None:
    browser = FakeDetailBrowser(_maps_url(), name="Google Maps")
    service = GoogleMapsCandidateDetail(
        GoogleMapsPlaceAdapter(
            browser,  # type: ignore[arg-type]
            clock=lambda: NOW,
        ),
        RawJsonWriter(tmp_path / "raw"),
        CandidateDetailStageWriter(tmp_path / "detail"),
        clock=lambda: NOW,
    )

    with pytest.raises(GoogleMapsNormalizationError, match="not resolved"):
        service.run(
            _staged(),
            _vacancy(),
            [],
            run_id="malformed-detail-run",
        )

    assert len(list((tmp_path / "raw").rglob("*.json"))) == 1
    assert list((tmp_path / "detail").rglob("*.json")) == []

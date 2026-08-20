from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from nextrip_pipeline.canonical.evidence import (
    DuplicateEvidenceAuditor,
    DuplicateEvidenceReason,
    DuplicateEvidenceStatus,
    MasterIdentityProjection,
    extract_google_place_token,
    load_duplicate_candidate_groups,
    load_latest_google_maps_observations,
    load_master_identity_projections,
)
from nextrip_pipeline.schemas import (
    DailyOpeningStatus,
    EntityType,
    GeoPoint,
    GoogleMapsPlaceObservation,
    OpeningStatusObservation,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 20, 8, tzinfo=UTC)


def _master(
    place_id: str,
    *,
    name: str = "Xóm Mèo Coffee",
    city: str = "Đà Nẵng",
    latitude: float = 16.0462,
    longitude: float = 108.2371,
) -> MasterIdentityProjection:
    from nextrip_pipeline.canonical.evidence import normalize_identity_text

    return MasterIdentityProjection(
        place_id=place_id,
        entity_type=EntityType.CAFE,
        name=name,
        normalized_name=normalize_identity_text(name),
        city=city,
        normalized_city=normalize_identity_text(city),
        location=GeoPoint(latitude=latitude, longitude=longitude),
    )


def _observation(
    place_id: str,
    *,
    token: str = "0x3142192f:0xf873e96f",
    name: str = "Xóm Mèo Coffee",
    latitude: float = 16.0462,
    longitude: float = 108.2371,
    observed_at: datetime = NOW,
) -> GoogleMapsPlaceObservation:
    observation_id = f"obs-{place_id}-{observed_at.hour}"
    return GoogleMapsPlaceObservation(
        observation_id=observation_id,
        run_id="run-evidence",
        place_id=place_id,
        source_record_id=f"source-{observation_id}",
        source_id="google-maps-web",
        source_url=f"https://www.google.com/maps/place/Test/data=!4m2!1s{token}!8m2",
        name=name,
        location=GeoPoint(latitude=latitude, longitude=longitude),
        opening=OpeningStatusObservation(
            observation_id=f"opening-{observation_id}",
            run_id="run-evidence",
            place_id=place_id,
            source_record_ids=[f"source-{observation_id}"],
            local_date=date(2026, 8, 20),
            status=DailyOpeningStatus.UNKNOWN,
            observed_at=observed_at,
        ),
        observed_at=observed_at,
    )


def test_shared_google_token_confirms_duplicate_and_is_deterministic() -> None:
    auditor = DuplicateEvidenceAuditor()
    masters = [_master("cafe_dn_001"), _master("night_dn_001")]
    observations = [_observation(item.place_id) for item in masters]

    first = auditor.audit(
        [["night_dn_001", "cafe_dn_001"]], masters, observations
    )
    second = auditor.audit(
        [["cafe_dn_001", "night_dn_001"]], reversed(masters), observations
    )

    assert first.audit_id == second.audit_id
    assert first.audit_hash == second.audit_hash
    assert first.groups[0].group_id == second.groups[0].group_id
    assert first.groups[0].status is DuplicateEvidenceStatus.CONFIRMED
    assert (
        DuplicateEvidenceReason.SHARED_GOOGLE_PLACE_TOKEN
        in first.groups[0].reason_codes
    )


def test_observed_name_and_near_google_positions_confirm_without_token() -> None:
    observations = [
        _observation("cafe_dn_001", token="/g/cafe-one"),
        _observation(
            "night_dn_001",
            token="/g/cafe-two",
            latitude=16.04625,
            longitude=108.23715,
        ),
    ]
    # Different strong tokens conflict with the near/name signal, so remove tokens
    # using stable but token-less external URLs.
    observations = [
        item.model_copy(
            update={
                "source_url": f"https://maps.google.com/maps/place/shared-{index}"
            }
        )
        for index, item in enumerate(observations)
    ]
    result = DuplicateEvidenceAuditor().audit(
        [["cafe_dn_001", "night_dn_001"]],
        [_master("cafe_dn_001"), _master("night_dn_001")],
        observations,
    )

    assert result.groups[0].status is DuplicateEvidenceStatus.CONFIRMED
    assert result.groups[0].pair_evidence[0].observed_distance_meters < 50
    assert (
        DuplicateEvidenceReason.OBSERVED_NAME_AND_LOCATION_MATCH
        in result.groups[0].reason_codes
    )


def test_name_only_and_master_distance_never_auto_confirm() -> None:
    result = DuplicateEvidenceAuditor().audit(
        [["cafe_dn_001", "night_dn_001"]],
        [
            _master("cafe_dn_001"),
            _master(
                "night_dn_001", latitude=16.04621, longitude=108.23711
            ),
        ],
        [],
    )

    group = result.groups[0]
    assert group.status is DuplicateEvidenceStatus.REVIEW
    assert group.missing_google_observation_place_ids == [
        "cafe_dn_001",
        "night_dn_001",
    ]
    assert DuplicateEvidenceReason.MASTER_NAME_CITY_MATCH_ONLY in group.reason_codes
    assert result.missing_google_observation_place_ids == group.place_ids


def test_same_google_search_url_is_not_treated_as_external_place_identity() -> None:
    shared_search_url = "https://google.com/maps/search/Same+name?hl=vi"
    observations = [
        _observation("cafe_dn_001", token="", name="First").model_copy(
            update={"source_url": shared_search_url, "location": None}
        ),
        _observation("night_dn_001", token="", name="Second").model_copy(
            update={"source_url": shared_search_url, "location": None}
        ),
    ]
    result = DuplicateEvidenceAuditor().audit(
        [["cafe_dn_001", "night_dn_001"]],
        [_master("cafe_dn_001"), _master("night_dn_001")],
        observations,
    )

    assert result.groups[0].status is DuplicateEvidenceStatus.REVIEW
    assert (
        DuplicateEvidenceReason.SHARED_GOOGLE_EXTERNAL_URL
        not in result.groups[0].reason_codes
    )


def test_master_coordinate_fallback_is_not_observed_location_evidence() -> None:
    observations = [
        _observation("cafe_dn_001", token="", name="Same").model_copy(
            update={
                "source_url": "https://google.com/maps/search/first",
                "location": GeoPoint(
                    latitude=16.0462,
                    longitude=108.2371,
                    accuracy="verified_master_fallback",
                    source="verified-master-data",
                ),
            }
        ),
        _observation("night_dn_001", token="", name="Same").model_copy(
            update={
                "source_url": "https://google.com/maps/search/second",
                "location": GeoPoint(
                    latitude=16.04621,
                    longitude=108.23711,
                    accuracy="verified_master_fallback",
                    source="verified-master-data",
                ),
            }
        ),
    ]
    result = DuplicateEvidenceAuditor().audit(
        [["cafe_dn_001", "night_dn_001"]],
        [_master("cafe_dn_001"), _master("night_dn_001")],
        observations,
    )

    pair = result.groups[0].pair_evidence[0]
    assert pair.status is DuplicateEvidenceStatus.REVIEW
    assert pair.observed_distance_meters is None
    assert (
        DuplicateEvidenceReason.OBSERVED_NAME_AND_LOCATION_MATCH
        not in pair.reason_codes
    )


def test_conflicting_tokens_or_far_observed_positions_are_distinct() -> None:
    masters = [_master("cafe_dn_001"), _master("night_dn_001")]
    conflicting_tokens = DuplicateEvidenceAuditor().audit(
        [["cafe_dn_001", "night_dn_001"]],
        masters,
        [
            _observation("cafe_dn_001", token="0x1:0x1", name="First"),
            _observation("night_dn_001", token="0x2:0x2", name="Second"),
        ],
    )
    far_observations = [
        _observation("cafe_dn_001", token="", name="First").model_copy(
            update={"source_url": "https://maps.google.com/maps/place/first"}
        ),
        _observation(
            "night_dn_001",
            token="",
            name="Second",
            latitude=16.06,
            longitude=108.25,
        ).model_copy(
            update={"source_url": "https://maps.google.com/maps/place/second"}
        ),
    ]
    far_positions = DuplicateEvidenceAuditor().audit(
        [["cafe_dn_001", "night_dn_001"]],
        masters,
        far_observations,
    )

    assert conflicting_tokens.groups[0].status is DuplicateEvidenceStatus.DISTINCT
    assert (
        DuplicateEvidenceReason.CONFLICTING_GOOGLE_PLACE_TOKEN
        in conflicting_tokens.groups[0].reason_codes
    )
    assert far_positions.groups[0].status is DuplicateEvidenceStatus.DISTINCT
    assert (
        DuplicateEvidenceReason.OBSERVED_LOCATIONS_FAR_APART
        in far_positions.groups[0].reason_codes
    )


def test_conflicting_strong_signals_fail_closed_to_review() -> None:
    result = DuplicateEvidenceAuditor().audit(
        [["cafe_dn_001", "night_dn_001"]],
        [_master("cafe_dn_001"), _master("night_dn_001")],
        [
            _observation("cafe_dn_001", token="0x1:0x1"),
            _observation("night_dn_001", token="0x2:0x2"),
        ],
    )

    assert result.groups[0].status is DuplicateEvidenceStatus.REVIEW
    assert (
        DuplicateEvidenceReason.CONFLICTING_STRONG_EVIDENCE
        in result.groups[0].reason_codes
    )


def test_path_loaders_read_groups_master_and_only_latest_google_observation(
    tmp_path: Path,
) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {"duplicate_identity_candidates": [["night_dn_001", "cafe_dn_001"]]}
        ),
        encoding="utf-8",
    )
    master = tmp_path / "cafe_final.json"
    master.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "id": "cafe_dn_001",
                        "entity_type": "cafe",
                        "name": "Xóm Mèo Coffee",
                        "city": "Đà Nẵng",
                        "address": "1 Test",
                        "coordinates": {"lat": 16.04, "lng": 108.23},
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    normalized = tmp_path / "normalized"
    normalized.mkdir()
    older = _observation("cafe_dn_001", observed_at=NOW - timedelta(hours=1))
    newer = _observation("cafe_dn_001", observed_at=NOW)
    (normalized / "older.json").write_text(
        older.model_dump_json(), encoding="utf-8"
    )
    (normalized / "newer.json").write_text(
        newer.model_dump_json(), encoding="utf-8"
    )
    (normalized / "unrelated.json").write_text(
        json.dumps({"source_id": "trivago-mcp"}), encoding="utf-8"
    )

    assert load_duplicate_candidate_groups(report) == [
        ["cafe_dn_001", "night_dn_001"]
    ]
    loaded_master = load_master_identity_projections(master)
    assert loaded_master[0].normalized_name == "xom meo coffee"
    loaded_google = load_latest_google_maps_observations(normalized)
    assert [item.observation_id for item in loaded_google] == [newer.observation_id]
    assert extract_google_place_token(str(newer.source_url)) == "0x3142192f:0xf873e96f"

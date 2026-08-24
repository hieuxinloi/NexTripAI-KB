from __future__ import annotations

from datetime import datetime, timezone

from nextrip_pipeline.crawl import compute_content_hash
from nextrip_pipeline.crawl.trivago_registry import TrivagoHotelRegistryEntry
from nextrip_pipeline.crawl.trivago_registry import TrivagoRegistryStatus
from nextrip_pipeline.quality import (
    TrivagoDiscoveryResolution,
    TrivagoDiscoveryResolver,
    TrivagoDiscoveryStatus,
    apply_trivago_resolution,
)
from nextrip_pipeline.schemas import EntityType, RecordSubjectType, SourceRecord


NOW = datetime(2026, 8, 19, 10, tzinfo=timezone.utc)


def _entry() -> TrivagoHotelRegistryEntry:
    return TrivagoHotelRegistryEntry(
        entity_id="hotel-dn-1",
        master_name="NexTrip Riverside Hotel",
        city="Đà Nẵng",
        address="1 Bạch Đằng",
        search_query="NexTrip Riverside Hotel, 1 Bạch Đằng, Đà Nẵng, Việt Nam",
    )


def _record(
    accommodations: object = None,
    *,
    review_target_hash: str | None = None,
) -> SourceRecord:
    structured = (
        {"accommodations": accommodations}
        if accommodations is not None
        else {"content": "No structured accommodation identity"}
    )
    request = {"entity_id": "hotel-dn-1", "arguments": {}}
    if review_target_hash is not None:
        request["review_target_hash"] = review_target_hash
    payload = {
        "request": request,
        "response": {"result": {"structuredContent": structured}},
    }
    return SourceRecord(
        source_record_id="source-1",
        run_id="run-1",
        source_id="trivago-mcp",
        entity_type=EntityType.HOTEL,
        subject_type=RecordSubjectType.HOTEL_PRICE,
        subject_id="hotel-dn-1",
        crawled_at=NOW,
        raw_payload=payload,
        content_hash=compute_content_hash(payload),
        parser_version="test",
    )


def test_resolver_confirms_unique_name_and_city_then_builds_real_mapping() -> None:
    resolution = TrivagoDiscoveryResolver(clock=lambda: NOW).resolve(
        _entry(),
        _record(
            [
                {
                    "accommodation_id": "real-id-123",
                    "accommodation_name": "NexTrip Riverside Hotel",
                    "city": "Đà Nẵng",
                    "accommodation_url": "https://www.trivago.vn/hotel/real-id-123",
                }
            ]
        ),
    )

    assert resolution.status is TrivagoDiscoveryStatus.CONFIRMED
    assert resolution.selected_external_id == "real-id-123"
    mapping = apply_trivago_resolution(_entry(), resolution).to_mapping()
    assert mapping.external_id == "real-id-123"
    assert mapping.source_record_ids == ["source-1"]


def test_confirmed_candidate_persists_trivago_name_without_changing_master() -> None:
    entry = _entry().model_copy(
        update={
            "master_name": "NexTrip Riverside Hotel (Legacy Master Name)",
            "search_query": (
                "NexTrip Riverside Hotel (Legacy Master Name), ÄÃ  Náºµng, Viá»‡t Nam"
            ),
        }
    )
    resolution = TrivagoDiscoveryResolver(clock=lambda: NOW).resolve(
        entry,
        _record(
            [
                {
                    "accommodation_id": "real-id-123",
                    "accommodation_name": "NexTrip Riverside Hotel",
                    "city": entry.city,
                }
            ]
        ),
    )

    resolved = apply_trivago_resolution(entry, resolution)
    mapping = resolved.to_mapping()

    assert resolution.status is TrivagoDiscoveryStatus.CONFIRMED
    assert resolution.selected_name == "NexTrip Riverside Hotel"
    assert resolved.entity_id == entry.entity_id
    assert resolved.master_name == "NexTrip Riverside Hotel (Legacy Master Name)"
    assert resolved.trivago_name == "NexTrip Riverside Hotel"
    assert mapping.entity_id == entry.entity_id
    assert mapping.attributes["master_name"] == resolved.master_name
    assert mapping.attributes["trivago_name"] == resolved.trivago_name
    assert mapping.attributes["hotel_name"] == resolved.master_name


def test_different_name_is_not_confirmed_by_unique_close_geo_identity() -> None:
    entry = _entry().model_copy(update={"latitude": 16.06778, "longitude": 108.22083})
    resolution = TrivagoDiscoveryResolver(clock=lambda: NOW).resolve(
        entry,
        _record(
            [
                {
                    "accommodation_id": "rebranded-id",
                    "accommodation_name": "Entirely New Riverside Brand",
                    "city": entry.city,
                    "latitude": 16.06788,
                    "longitude": 108.22083,
                },
                {
                    "accommodation_id": "far-id",
                    "accommodation_name": "Another Hotel",
                    "city": entry.city,
                    "latitude": 16.06978,
                    "longitude": 108.22083,
                },
            ]
        ),
    )

    assert resolution.status is TrivagoDiscoveryStatus.REVIEW
    assert resolution.selected_external_id == "rebranded-id"
    assert resolution.selected_name == "Entirely New Riverside Brand"
    assert "geo_identity_requires_review" in resolution.reason_codes
    selected = next(
        item for item in resolution.candidates if item.external_id == "rebranded-id"
    )
    assert selected.latitude == 16.06788
    assert selected.longitude == 108.22083
    assert selected.distance_from_master_m is not None
    assert selected.distance_from_master_m < 50
    resolved = apply_trivago_resolution(entry, resolution)
    assert resolved.trivago_name is None
    assert resolved.master_name == entry.master_name
    assert resolved.entity_id == entry.entity_id


def test_unique_distinctive_provider_name_confirms_with_city_and_geo() -> None:
    entry = _entry().model_copy(
        update={
            "master_name": "Khách sạn Hương Việt",
            "city": "Quy Nhơn",
            "latitude": 13.77,
            "longitude": 109.24,
        }
    )
    resolution = TrivagoDiscoveryResolver(clock=lambda: NOW).resolve(
        entry,
        _record(
            [
                {
                    "accommodation_id": "huong-viet-id",
                    "accommodation_name": ("Huong Viet Hotel Quy Nhon - Beachfront"),
                    "country_city": "Quy Nhơn, Việt Nam",
                    "latitude": 13.7702,
                    "longitude": 109.2401,
                }
            ]
        ),
    )

    assert resolution.status is TrivagoDiscoveryStatus.CONFIRMED
    assert resolution.selected_external_id == "huong-viet-id"
    assert resolution.reason_codes == ["unique_distinctive_name", "city_match"]


def test_rebrand_with_ambiguous_colocated_candidates_requires_review() -> None:
    entry = _entry().model_copy(update={"latitude": 16.06778, "longitude": 108.22083})
    resolution = TrivagoDiscoveryResolver(clock=lambda: NOW).resolve(
        entry,
        _record(
            [
                {
                    "accommodation_id": "near-a",
                    "accommodation_name": "New Brand A",
                    "city": entry.city,
                    "latitude": 16.06788,
                    "longitude": 108.22083,
                },
                {
                    "accommodation_id": "near-b",
                    "accommodation_name": "New Brand B",
                    "city": entry.city,
                    "latitude": 16.0679,
                    "longitude": 108.22083,
                },
            ]
        ),
    )

    assert resolution.status is TrivagoDiscoveryStatus.REVIEW
    assert "nearby_geocoded_candidates_ambiguous" in resolution.reason_codes
    assert all(
        item.distance_from_master_m is not None and item.distance_from_master_m < 50
        for item in resolution.candidates
    )
    unresolved = apply_trivago_resolution(entry, resolution)
    assert unresolved.trivago_name is None
    assert unresolved.master_name == entry.master_name
    assert unresolved.entity_id == entry.entity_id


def test_name_similarity_without_exact_match_or_geo_requires_review() -> None:
    entry = _entry()
    resolution = TrivagoDiscoveryResolver(clock=lambda: NOW).resolve(
        entry,
        _record(
            [
                {
                    "accommodation_id": "similar-id",
                    "accommodation_name": "NexTrip Riverside Hotels",
                    "city": entry.city,
                }
            ]
        ),
    )

    assert resolution.candidates[0].name_score >= 0.94
    assert resolution.status is TrivagoDiscoveryStatus.REVIEW
    assert apply_trivago_resolution(entry, resolution).trivago_name is None


def test_returned_confirmed_external_id_can_refresh_trivago_name() -> None:
    entry = TrivagoHotelRegistryEntry.model_validate(
        {
            **_entry().model_dump(mode="python"),
            "status": TrivagoRegistryStatus.CONFIRMED,
            "external_id": "verified-id",
            "trivago_name": "Previous Trivago Brand",
            "confidence": 1,
            "matched_at": NOW,
            "verified_at": NOW,
        }
    )
    resolution = TrivagoDiscoveryResolver(clock=lambda: NOW).resolve(
        entry,
        _record(
            [
                {
                    "accommodation_id": "verified-id",
                    "accommodation_name": "Current Trivago Brand",
                    "city": entry.city,
                }
            ]
        ),
    )

    resolved = apply_trivago_resolution(entry, resolution)

    assert resolution.status is TrivagoDiscoveryStatus.CONFIRMED
    assert resolved.entity_id == entry.entity_id
    assert resolved.external_id == "verified-id"
    assert resolved.master_name == entry.master_name
    assert resolved.trivago_name == "Current Trivago Brand"


def test_resolver_recognizes_radius_search_country_city() -> None:
    resolution = TrivagoDiscoveryResolver(clock=lambda: NOW).resolve(
        _entry(),
        _record(
            [
                {
                    "accommodation_id": "radius-result-id",
                    "accommodation_name": "NexTrip Riverside Hotel",
                    "country_city": "Vietnam, Đà Nẵng",
                }
            ]
        ),
    )

    assert resolution.status is TrivagoDiscoveryStatus.CONFIRMED
    assert resolution.selected_external_id == "radius-result-id"
    assert resolution.candidates[0].location_text == "Vietnam, Đà Nẵng"
    assert resolution.candidates[0].city_evidence == "match"


def test_resolver_keeps_exact_name_without_city_for_review() -> None:
    resolution = TrivagoDiscoveryResolver(clock=lambda: NOW).resolve(
        _entry(),
        _record(
            [
                {
                    "accommodation_id": "possible-id",
                    "accommodation_name": "NexTrip Riverside Hotel",
                }
            ]
        ),
    )

    assert resolution.status is TrivagoDiscoveryStatus.REVIEW
    assert resolution.selected_external_id == "possible-id"
    assert "city_evidence_missing" in resolution.reason_codes


def test_resolver_preserves_generic_mcp_response_as_missing() -> None:
    resolution = TrivagoDiscoveryResolver(clock=lambda: NOW).resolve(
        _entry(), _record()
    )

    assert resolution.status is TrivagoDiscoveryStatus.MISSING
    assert resolution.selected_external_id is None
    assert resolution.reason_codes == ["accommodations_missing_from_response"]


def test_resolver_treats_structured_no_accommodations_error_as_missing() -> None:
    payload = {
        "request": {"entity_id": "hotel-dn-1", "arguments": {}},
        "response": {
            "result": {"structuredContent": {"error": "No accommodations found"}}
        },
    }
    record = SourceRecord(
        source_record_id="source-no-accommodations",
        run_id="run-1",
        source_id="trivago-mcp",
        entity_type=EntityType.HOTEL,
        subject_type=RecordSubjectType.HOTEL_PRICE,
        subject_id="hotel-dn-1",
        crawled_at=NOW,
        raw_payload=payload,
        content_hash=compute_content_hash(payload),
        parser_version="test",
    )

    resolution = TrivagoDiscoveryResolver(clock=lambda: NOW).resolve(_entry(), record)

    assert resolution.status is TrivagoDiscoveryStatus.MISSING
    assert resolution.returned_candidate_count == 0
    assert resolution.reason_codes == ["accommodations_missing_from_response"]


def test_resolver_never_treats_ranked_candidates_as_an_external_id_change() -> None:
    confirmed = TrivagoHotelRegistryEntry.model_validate(
        {
            **_entry().model_dump(mode="python"),
            "status": TrivagoRegistryStatus.CONFIRMED,
            "external_id": "verified-id",
            "trivago_name": "Verified Trivago Name",
            "confidence": 1,
            "matched_at": NOW,
            "verified_at": NOW,
        }
    )

    resolution = TrivagoDiscoveryResolver(clock=lambda: NOW).resolve(
        confirmed,
        _record(
            [
                {
                    "accommodation_id": "different-id",
                    "accommodation_name": "NexTrip Riverside Hotel",
                    "city": "Đà Nẵng",
                }
            ]
        ),
    )

    assert resolution.status is TrivagoDiscoveryStatus.MISSING
    assert resolution.reason_codes == [
        "confirmed_external_id_not_returned",
        "returned_candidates_do_not_prove_identity_change",
    ]
    resolved = apply_trivago_resolution(confirmed, resolution)
    assert resolved.external_id == "verified-id"
    assert resolved.entity_id == confirmed.entity_id
    assert resolution.selected_external_id is None
    assert resolution.selected_name is None
    assert resolved.trivago_name == "Verified Trivago Name"


def test_resolver_requires_review_for_same_property_with_a_new_external_id() -> None:
    confirmed = TrivagoHotelRegistryEntry.model_validate(
        {
            **_entry().model_dump(mode="python"),
            "status": TrivagoRegistryStatus.CONFIRMED,
            "external_id": "verified-id",
            "external_url": ("https://www.trivago.vn/vi/lm/verified?search=100-123456"),
            "trivago_name": "Verified Trivago Name",
            "confidence": 1,
            "matched_at": NOW,
            "verified_at": NOW,
        }
    )
    resolution = TrivagoDiscoveryResolver(clock=lambda: NOW).resolve(
        confirmed,
        _record(
            [
                {
                    "accommodation_id": "new-external-id",
                    "accommodation_name": "Verified Trivago Name",
                    "city": "Đà Nẵng",
                    "accommodation_url": (
                        "https://www.trivago.vn/vi/lm/verified"
                        "?search=100-123456;dr-20260824-20260825"
                    ),
                }
            ]
        ),
    )

    assert resolution.status is TrivagoDiscoveryStatus.REVIEW
    assert resolution.selected_external_id == "new-external-id"
    assert "stable_property_id_supports_external_id_change" in (resolution.reason_codes)
    assert "external_id_change_requires_review" in resolution.reason_codes
    resolved = apply_trivago_resolution(confirmed, resolution)
    assert resolved.external_id == "verified-id"


def _review_target_entry() -> TrivagoHotelRegistryEntry:
    return TrivagoHotelRegistryEntry(
        entity_id="hotel-dn-1",
        master_name="Legacy Daisy Property",
        city="ÄÃ  Náºµng",
        address="67 Che Lan Vien",
        latitude=16.0407,
        longitude=108.2466,
        search_query="Legacy Daisy Property, Da Nang",
        search_aliases=["Daisy Boutique Hotel"],
        search_queries=[
            "Legacy Daisy Property, Da Nang",
            "Daisy Boutique Hotel, Da Nang",
        ],
        review_target_external_id="reviewed-daisy-id",
        review_target_property_id="19017974",
        review_target_name="Daisy Boutique Hotel",
        review_target_reviewer="Oanhh-approved-agent-review",
        review_target_reviewed_at=NOW,
        review_target_reason="reviewed exact name, address, and coordinates",
        review_target_hash="a" * 64,
        review_target_evidence=[{"path": "evidence.json", "file_sha256": "b" * 64}],
    )


def _reviewed_candidate(**updates) -> dict[str, object]:
    candidate: dict[str, object] = {
        "accommodation_id": "reviewed-daisy-id",
        "accommodation_name": "Daisy Boutique Hotel",
        "country_city": "ÄÃ  Náºµng, Viá»‡t Nam",
        "accommodation_url": (
            "https://www.trivago.vn/vi/lm/daisy?currencyCode=VND"
            "&search=100-19017974;dr-20260823-20260824"
        ),
        "latitude": 16.0407,
        "longitude": 108.2466,
    }
    candidate.update(updates)
    return candidate


def test_review_target_confirms_fresh_exact_identity_and_deduplicates_offers() -> None:
    entry = _review_target_entry()
    candidate = _reviewed_candidate()
    resolution = TrivagoDiscoveryResolver(clock=lambda: NOW).resolve(
        entry,
        _record(
            [candidate, {**candidate, "advertisers": "Booking.com"}],
            review_target_hash=entry.review_target_hash,
        ),
    )

    assert resolution.status is TrivagoDiscoveryStatus.CONFIRMED
    assert resolution.selected_external_id == "reviewed-daisy-id"
    assert resolution.resolver_version == "1.4.0"
    assert resolution.review_target_hash == entry.review_target_hash
    assert "reviewed_search_target_returned" in resolution.reason_codes
    mapping = apply_trivago_resolution(entry, resolution).to_mapping()
    assert mapping.attributes["review_target_hash"] == entry.review_target_hash


def test_review_target_requires_current_registry_hash_in_raw_request() -> None:
    entry = _review_target_entry()

    for stale_hash in (None, "c" * 64):
        resolution = TrivagoDiscoveryResolver(clock=lambda: NOW).resolve(
            entry,
            _record(
                [_reviewed_candidate()],
                review_target_hash=stale_hash,
            ),
        )

        assert resolution.status is TrivagoDiscoveryStatus.REVIEW
        assert resolution.selected_external_id == entry.review_target_external_id
        assert "review_target_request_hash_mismatch" in resolution.reason_codes
        assert "fresh_review_target_evidence_required" in resolution.reason_codes


def test_review_target_mismatch_never_confirms() -> None:
    entry = _review_target_entry()
    mismatches = [
        {"accommodation_id": "another-id"},
        {"accommodation_url": ("https://www.trivago.vn/vi/lm/daisy?search=100-999999")},
        {"accommodation_name": "Another Daisy Hotel"},
        {"country_city": "Hanoi, Vietnam"},
        {"latitude": 16.09, "longitude": 108.2466},
    ]

    for mismatch in mismatches:
        resolution = TrivagoDiscoveryResolver(clock=lambda: NOW).resolve(
            entry,
            _record([_reviewed_candidate(**mismatch)]),
        )
        assert resolution.status is not TrivagoDiscoveryStatus.CONFIRMED


def test_review_and_rejected_candidate_names_never_update_registry_name() -> None:
    review = TrivagoDiscoveryResolver(clock=lambda: NOW).resolve(
        _entry(),
        _record(
            [
                {
                    "accommodation_id": "possible-id",
                    "accommodation_name": "NexTrip Riverside Hotel",
                }
            ]
        ),
    )
    assert review.status is TrivagoDiscoveryStatus.REVIEW
    assert review.selected_name == "NexTrip Riverside Hotel"

    reviewed_entry = apply_trivago_resolution(_entry(), review)
    rejected_entry = apply_trivago_resolution(
        _entry(),
        review.model_copy(
            update={
                "status": TrivagoDiscoveryStatus.REJECTED,
                "reason_codes": ["rejected_for_test"],
            }
        ),
    )

    assert reviewed_entry.trivago_name is None
    assert rejected_entry.trivago_name is None
    assert reviewed_entry.master_name == _entry().master_name
    assert rejected_entry.master_name == _entry().master_name


def test_pre_canonical_name_resolution_and_registry_json_remain_readable() -> None:
    resolution = TrivagoDiscoveryResolver(clock=lambda: NOW).resolve(
        _entry(), _record()
    )
    legacy_resolution = resolution.model_dump(mode="json", exclude={"selected_name"})
    legacy_entry = _entry().model_dump(mode="json", exclude={"trivago_name"})

    assert (
        TrivagoDiscoveryResolution.model_validate(legacy_resolution).selected_name
        is None
    )
    assert TrivagoHotelRegistryEntry.model_validate(legacy_entry).trivago_name is None

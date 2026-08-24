from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from nextrip_pipeline.canonical.completeness import (
    ArtifactInputDigest,
    CanonicalCompletenessAudit,
    CanonicalHotelStayContext,
    CompletenessArtifactKind,
    CompletenessField,
    CompletenessSeverity,
    CompletenessStatus,
    build_canonical_completeness_audit,
)
from nextrip_pipeline.canonical.dataset import materialize_canonical_active_dataset
from nextrip_pipeline.crawl.trivago_registry import (
    TrivagoHotelRegistryEntry,
    TrivagoRegistryStatus,
    TrivagoSearchReviewEvidence,
)
from nextrip_pipeline.canonical.evidence import DuplicateEvidenceAuditor
from nextrip_pipeline.canonical.master import CanonicalMasterLoad, MasterRawRecord
from nextrip_pipeline.canonical.models import (
    DuplicateIdentityDecision,
    LegacyPlaceSlot,
    stable_sha256,
)
from nextrip_pipeline.canonical.readiness import (
    evaluate_canonical_dataset_readiness,
)
from nextrip_pipeline.canonical.resolver import (
    CanonicalIdentityResolver,
    build_canonical_identity_manifest,
)
from nextrip_pipeline.publishing import (
    CurrentHotelAvailabilitySnapshot,
    CurrentHotelPriceSnapshot,
    GoogleMapsMenuSourceEntry,
)
from nextrip_pipeline.schemas import (
    EntityType,
    ExternalEntityMapping,
    HotelAvailabilityObservation,
    HotelAvailabilityReason,
    HotelAvailabilityStatus,
    HotelPriceObservation,
    MappingStatus,
    NormalizedMenu,
    NormalizedMenuItem,
    OfferAvailability,
    OpeningStatusObservation,
    VerificationStatus,
)


UTC = timezone.utc
AS_OF = datetime(2026, 8, 21, 5, tzinfo=UTC)
CHECK_IN = date(2026, 8, 22)
CHECK_OUT = date(2026, 8, 23)
CAFE_ID = "cafe_dn_001"
HOTEL_ID = "hotel_dn_001"


def _dataset_and_readiness(
    *,
    cafe_address: str | None = "1 Bach Dang",
    include_cafe_category: bool = True,
    include_google_refresh: bool = False,
    include_reference_price: bool = True,
    google_price_observed: bool = True,
    open_vacancy: bool = False,
):
    slots = [
        LegacyPlaceSlot(
            legacy_place_id=CAFE_ID,
            city_id="city_da_nang",
            primary_type=EntityType.CAFE,
        ),
        LegacyPlaceSlot(
            legacy_place_id=HOTEL_ID,
            city_id="city_da_nang",
            primary_type=EntityType.HOTEL,
        ),
    ]
    if open_vacancy:
        slots.append(
            LegacyPlaceSlot(
                legacy_place_id="night_dn_001",
                city_id="city_da_nang",
                primary_type=EntityType.NIGHTLIFE,
            )
        )
    cafe = {
        "id": CAFE_ID,
        "entity_type": "cafe",
        "name": "Canonical Cafe",
        "city": "Da Nang",
        "address": cafe_address,
        "coordinates": {"lat": 16.06, "lng": 108.22},
        "description": "Verified cafe description",
        "cover_image_url": "https://images.example/cafe.jpg",
        "opening_hours": {"monday": "07:00-22:00"},
        "tags": [],
    }
    if include_reference_price:
        cafe["price_range"] = "20,000-50,000 VND"
    if include_google_refresh:
        observed_at = AS_OF - timedelta(minutes=10)
        opening = OpeningStatusObservation(
            observation_id="opening-cafe-1",
            run_id="google-run-1",
            place_id=CAFE_ID,
            source_record_ids=["google-source-cafe-1"],
            local_date=AS_OF.date(),
            status="open_today",
            open_now=True,
            observed_at=observed_at,
            verification_status=VerificationStatus.AUTO_VERIFIED,
        )
        cafe.update(
            {
                "business_status": "active",
                "opening_status": opening.model_dump(mode="json"),
                "google_maps_schedule_status": "observed",
                "google_maps_price": {
                    "status": "observed" if google_price_observed else "not_listed",
                    "level": 2 if google_price_observed else None,
                    "raw_text": "₫₫" if google_price_observed else None,
                    "observed_at": observed_at.isoformat(),
                    "source": "google-maps-web",
                },
                "google_maps_refresh": {
                    "refresh_hash": "a" * 64,
                    "source_id": "google-maps-web",
                    "source_record_id": "google-source-cafe-1",
                    "observation_id": "google-place-observation-1",
                    "run_id": "google-run-1",
                    "source_url": "https://www.google.com/maps/place/cafe-1",
                    "observed_at": observed_at.isoformat(),
                },
            }
        )
    if include_cafe_category:
        cafe["category"] = "Coffee shop"
    hotel = {
        "id": HOTEL_ID,
        "entity_type": "hotel",
        "name": "Canonical Hotel",
        "city": "Da Nang",
        "address": "2 Bach Dang",
        "coordinates": {"lat": 16.061, "lng": 108.221},
        "category": "Hotel",
        "description": "Verified hotel description",
        "cover_image_url": "https://images.example/hotel.jpg",
        "amenities": ["wifi", "pool"],
        "tags": [],
    }
    raw_records = [
        MasterRawRecord(
            place_id=CAFE_ID,
            source_filename="cafe_final.json",
            record_index=0,
            raw_record=cafe,
        ),
        MasterRawRecord(
            place_id=HOTEL_ID,
            source_filename="hotel_final.json",
            record_index=0,
            raw_record=hotel,
        ),
    ]
    if open_vacancy:
        raw_records.append(
            MasterRawRecord(
                place_id="night_dn_001",
                source_filename="nightlife_final.json",
                record_index=0,
                raw_record={
                    "id": "night_dn_001",
                    "entity_type": "nightlife",
                    "name": "Canonical Cafe Night Alias",
                    "city": "Da Nang",
                    "address": cafe_address,
                    "coordinates": {"lat": 16.06, "lng": 108.22},
                    "category": "Coffee shop",
                    "tags": [],
                },
            )
        )
    master = CanonicalMasterLoad(
        slots=slots,
        raw_records_by_id={item.place_id: item for item in raw_records},
        source_files=[],
        quota_counts=[],
        explicit_duplicate_decisions=[],
    )
    duplicate_decisions = (
        [
            DuplicateIdentityDecision(
                keeper_legacy_place_id=CAFE_ID,
                duplicate_legacy_place_ids=["night_dn_001"],
                reason="reviewed physical duplicate",
            )
        ]
        if open_vacancy
        else []
    )
    manifest = build_canonical_identity_manifest(
        slots,
        duplicate_decisions=duplicate_decisions,
        generated_at=AS_OF,
    )
    dataset = materialize_canonical_active_dataset(master, manifest)
    evidence = DuplicateEvidenceAuditor().audit([], [], [])
    readiness = evaluate_canonical_dataset_readiness(
        dataset,
        evidence,
        CanonicalIdentityResolver(manifest),
    )
    return dataset, readiness


def _stay_context(
    check_in: date = CHECK_IN,
    check_out: date = CHECK_OUT,
) -> CanonicalHotelStayContext:
    return CanonicalHotelStayContext(check_in=check_in, check_out=check_out)


def _google_mapping() -> ExternalEntityMapping:
    return ExternalEntityMapping(
        mapping_id=f"google-maps-web-{CAFE_ID}",
        entity_id=CAFE_ID,
        entity_type=EntityType.CAFE,
        source_id="google-maps-web",
        external_id="google-place-cafe-1",
        external_url="https://www.google.com/maps/place/cafe-1",
        status=MappingStatus.CONFIRMED,
        confidence=1,
        matched_at=AS_OF - timedelta(days=1),
        verified_at=AS_OF - timedelta(days=1),
        last_checked_at=AS_OF - timedelta(minutes=10),
    )


def _trivago_mapping() -> ExternalEntityMapping:
    return ExternalEntityMapping(
        mapping_id=f"trivago-mcp-{HOTEL_ID}",
        entity_id=HOTEL_ID,
        entity_type=EntityType.HOTEL,
        source_id="trivago-mcp",
        external_id="trivago-hotel-1",
        external_url="https://www.trivago.vn/hotel-1",
        status=MappingStatus.CONFIRMED,
        confidence=1,
        matched_at=AS_OF - timedelta(days=1),
        verified_at=AS_OF - timedelta(days=1),
        last_checked_at=AS_OF - timedelta(minutes=10),
    )


def _trivago_registry_entry(
    status: TrivagoRegistryStatus,
) -> TrivagoHotelRegistryEntry:
    return TrivagoHotelRegistryEntry(
        entity_id=HOTEL_ID,
        master_name="Canonical Hotel",
        city="Da Nang",
        search_query="Canonical Hotel, Da Nang, Vietnam",
        status=status,
        review_target_reviewer="test-reviewer",
        review_target_reviewed_at=AS_OF,
        review_target_reason="terminal outcome confirmed in test evidence",
        review_target_hash="a" * 64,
        review_target_evidence=[
            TrivagoSearchReviewEvidence(
                path="evidence/hotel_dn_001.json",
                file_sha256="b" * 64,
            )
        ],
    )


def _hotel_availability(
    *,
    check_in: date = CHECK_IN,
    check_out: date = CHECK_OUT,
    requested_check_in: date | None = None,
    fallback_offset_days: int = 0,
) -> CurrentHotelAvailabilitySnapshot:
    observation = HotelAvailabilityObservation(
        observation_id=f"availability-{check_in.isoformat()}",
        run_id="trivago-run-1",
        hotel_id=HOTEL_ID,
        source_record_id="trivago-source-1",
        source_id="trivago-mcp",
        mapping_id=f"trivago-mcp-{HOTEL_ID}",
        external_id="trivago-hotel-1",
        requested_check_in=requested_check_in or check_in,
        fallback_offset_days=fallback_offset_days,
        check_in=check_in,
        check_out=check_out,
        nights=(check_out - check_in).days,
        status=HotelAvailabilityStatus.AVAILABLE,
        reason=HotelAvailabilityReason.OFFER_FOUND,
        offer_count=1,
        price_observation_ids=[f"price-{check_in.isoformat()}"],
        observed_at=AS_OF - timedelta(minutes=10),
        verification_status=VerificationStatus.AUTO_VERIFIED,
    )
    return CurrentHotelAvailabilitySnapshot(
        hotel_id=HOTEL_ID,
        observation_id=observation.observation_id,
        observation=observation,
        updated_at=AS_OF - timedelta(minutes=5),
        stale_after=AS_OF + timedelta(hours=4),
    )


def _hotel_price(
    *,
    check_in: date = CHECK_IN,
    check_out: date = CHECK_OUT,
) -> CurrentHotelPriceSnapshot:
    observation = HotelPriceObservation(
        observation_id=f"price-{check_in.isoformat()}",
        run_id="trivago-run-1",
        hotel_id=HOTEL_ID,
        offer_key="standard-room",
        source_record_id="trivago-source-1",
        source_id="trivago-mcp",
        mapping_id=f"trivago-mcp-{HOTEL_ID}",
        external_id="trivago-hotel-1",
        room_type="Standard room",
        check_in=check_in,
        check_out=check_out,
        currency="VND",
        total_amount=Decimal("1000000"),
        availability=OfferAvailability.AVAILABLE,
        observed_at=AS_OF - timedelta(minutes=10),
        verification_status=VerificationStatus.AUTO_VERIFIED,
    )
    return CurrentHotelPriceSnapshot(
        hotel_id=HOTEL_ID,
        observation_id=observation.observation_id,
        decision_id=f"decision-{observation.observation_id}",
        observation=observation,
        updated_at=AS_OF - timedelta(minutes=5),
        stale_after=AS_OF + timedelta(hours=4),
    )


def _verified_menu() -> NormalizedMenu:
    return NormalizedMenu(
        place_id=CAFE_ID,
        items=[
            NormalizedMenuItem(
                name="Vietnamese coffee",
                section="Coffee",
                currency="VND",
                amount=25000,
            )
        ],
    )


def test_audit_is_deterministic_and_rejects_a_tampered_hash() -> None:
    dataset, readiness = _dataset_and_readiness()
    digests = [
        ArtifactInputDigest(
            kind=CompletenessArtifactKind.HOTEL_PRICE,
            root_label="prices",
            file_count=1,
            input_hash="b" * 64,
        ),
        ArtifactInputDigest(
            kind=CompletenessArtifactKind.GOOGLE_REGISTRY,
            root_label="google_registry",
            file_count=1,
            input_hash="a" * 64,
        ),
    ]

    first = build_canonical_completeness_audit(
        dataset,
        readiness,
        as_of=AS_OF,
        stay_context=_stay_context(),
        input_digests=digests,
    )
    second = build_canonical_completeness_audit(
        dataset,
        readiness,
        as_of=AS_OF,
        stay_context=_stay_context(),
        input_digests=reversed(digests),
    )

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.audit_id == f"canonical-completeness-{first.audit_hash[:20]}"

    tampered = first.model_dump(mode="json")
    tampered["audit_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="audit_hash does not match"):
        CanonicalCompletenessAudit.model_validate(tampered)


def test_open_vacancy_blocks_identity_publish_without_review_place_ids() -> None:
    dataset, readiness = _dataset_and_readiness(open_vacancy=True)

    assert readiness.schema_version == "1.3.0"
    assert readiness.open_vacancy_count == 1
    assert readiness.unresolved_groups == []
    assert readiness.publish_ready is False

    audit = build_canonical_completeness_audit(
        dataset,
        readiness,
        as_of=AS_OF,
        stay_context=_stay_context(),
    )

    assert audit.schema_version == "1.1.0"
    assert audit.identity_review_place_ids == []
    assert audit.open_vacancy_count == 1
    assert audit.identity_publish_ready is False
    assert (
        CanonicalCompletenessAudit.model_validate_json(audit.model_dump_json()) == audit
    )

    tampered = audit.model_dump(mode="json")
    tampered["identity_publish_ready"] = True
    with pytest.raises(
        ValidationError,
        match="unresolved identity places and open vacancies",
    ):
        CanonicalCompletenessAudit.model_validate(tampered)


def test_schema_1_0_completeness_artifact_remains_valid() -> None:
    dataset, readiness = _dataset_and_readiness()
    current = build_canonical_completeness_audit(
        dataset,
        readiness,
        as_of=AS_OF,
        stay_context=_stay_context(),
    )
    legacy_payload = current.model_dump(
        mode="json",
        exclude={"audit_id", "audit_hash", "open_vacancy_count"},
    )
    legacy_payload["schema_version"] = "1.0.0"
    legacy_hash = stable_sha256(legacy_payload)

    parsed = CanonicalCompletenessAudit.model_validate(
        {
            **legacy_payload,
            "audit_id": f"canonical-completeness-{legacy_hash[:20]}",
            "audit_hash": legacy_hash,
        }
    )

    assert parsed.schema_version == "1.0.0"
    assert parsed.open_vacancy_count is None
    assert parsed.identity_publish_ready is True


def test_static_operational_and_deferred_readiness_are_independent() -> None:
    dataset, readiness = _dataset_and_readiness()

    empty = build_canonical_completeness_audit(
        dataset,
        readiness,
        as_of=AS_OF,
        stay_context=_stay_context(),
    )

    assert empty.static_ingest_ready is True
    assert empty.operational_fresh is False
    assert empty.deferred_complete is False
    assert {
        (gap.place_id, gap.field)
        for gap in empty.gaps
        if gap.severity is CompletenessSeverity.OPERATIONAL
    } == {
        (CAFE_ID, CompletenessField.BUSINESS_STATUS),
        (CAFE_ID, CompletenessField.DAILY_OPENING),
        (HOTEL_ID, CompletenessField.HOTEL_AVAILABILITY),
        (HOTEL_ID, CompletenessField.HOTEL_PRICE),
    }
    assert any(
        gap.place_id == HOTEL_ID
        and gap.field is CompletenessField.HOTEL_MAPPING
        and gap.severity is CompletenessSeverity.BACKFILL
        for gap in empty.gaps
    )
    assert any(
        gap.place_id == CAFE_ID
        and gap.field is CompletenessField.VERIFIED_MENU
        and gap.status is CompletenessStatus.MISSING
        for gap in empty.gaps
    )

    refreshed_dataset, refreshed_readiness = _dataset_and_readiness(
        include_google_refresh=True
    )
    complete = build_canonical_completeness_audit(
        refreshed_dataset,
        refreshed_readiness,
        as_of=AS_OF,
        stay_context=_stay_context(),
        google_mappings=[_google_mapping()],
        trivago_mappings=[_trivago_mapping()],
        hotel_availability=[_hotel_availability()],
        hotel_prices=[_hotel_price()],
        current_menus=[_verified_menu()],
    )

    assert complete.gaps == []
    assert complete.static_ingest_ready is True
    assert complete.operational_fresh is True
    assert complete.deferred_complete is True


def test_hotel_observations_must_match_the_exact_stay_context() -> None:
    dataset, readiness = _dataset_and_readiness()
    wrong_check_in = CHECK_IN + timedelta(days=1)
    wrong_check_out = CHECK_OUT + timedelta(days=1)

    wrong_context = build_canonical_completeness_audit(
        dataset,
        readiness,
        as_of=AS_OF,
        stay_context=_stay_context(),
        trivago_mappings=[_trivago_mapping()],
        hotel_availability=[
            _hotel_availability(
                check_in=wrong_check_in,
                check_out=wrong_check_out,
            )
        ],
        hotel_prices=[_hotel_price(check_in=wrong_check_in, check_out=wrong_check_out)],
    )
    hotel_dynamic = {
        gap.field: gap.status
        for gap in wrong_context.gaps
        if gap.place_id == HOTEL_ID
        and gap.field
        in {
            CompletenessField.HOTEL_AVAILABILITY,
            CompletenessField.HOTEL_PRICE,
        }
    }
    assert hotel_dynamic == {
        CompletenessField.HOTEL_AVAILABILITY: CompletenessStatus.MISSING,
        CompletenessField.HOTEL_PRICE: CompletenessStatus.MISSING,
    }

    exact_context = build_canonical_completeness_audit(
        dataset,
        readiness,
        as_of=AS_OF,
        stay_context=_stay_context(),
        trivago_mappings=[_trivago_mapping()],
        hotel_availability=[_hotel_availability()],
        hotel_prices=[_hotel_price()],
    )
    assert not any(
        gap.place_id == HOTEL_ID
        and gap.field
        in {
            CompletenessField.HOTEL_AVAILABILITY,
            CompletenessField.HOTEL_PRICE,
        }
        for gap in exact_context.gaps
    )

    fallback_context = build_canonical_completeness_audit(
        dataset,
        readiness,
        as_of=AS_OF,
        stay_context=_stay_context(),
        trivago_mappings=[_trivago_mapping()],
        hotel_availability=[
            _hotel_availability(
                check_in=wrong_check_in,
                check_out=wrong_check_out,
                requested_check_in=CHECK_IN,
                fallback_offset_days=1,
            )
        ],
        hotel_prices=[_hotel_price(check_in=wrong_check_in, check_out=wrong_check_out)],
    )
    fallback_gaps = {
        gap.field: gap.status
        for gap in fallback_context.gaps
        if gap.place_id == HOTEL_ID
        and gap.field
        in {
            CompletenessField.HOTEL_AVAILABILITY,
            CompletenessField.HOTEL_PRICE,
        }
    }
    assert fallback_gaps == {
        CompletenessField.HOTEL_AVAILABILITY: CompletenessStatus.MISSING,
        CompletenessField.HOTEL_PRICE: CompletenessStatus.MISSING,
    }


def test_google_daily_evidence_is_read_from_and_aged_inside_canonical_data() -> None:
    dataset, readiness = _dataset_and_readiness(include_google_refresh=True)

    fresh = build_canonical_completeness_audit(
        dataset,
        readiness,
        as_of=AS_OF,
        stay_context=_stay_context(),
    )
    assert not any(
        gap.place_id == CAFE_ID
        and gap.field
        in {
            CompletenessField.BUSINESS_STATUS,
            CompletenessField.DAILY_OPENING,
            CompletenessField.OPENING_SCHEDULE,
            CompletenessField.REFERENCE_PRICE,
        }
        for gap in fresh.gaps
    )

    stale = build_canonical_completeness_audit(
        dataset,
        readiness,
        as_of=AS_OF + timedelta(days=2),
        stay_context=_stay_context(),
    )
    stale_google = {
        gap.field: gap
        for gap in stale.gaps
        if gap.place_id == CAFE_ID
        and gap.field
        in {
            CompletenessField.BUSINESS_STATUS,
            CompletenessField.DAILY_OPENING,
        }
    }
    assert set(stale_google) == {
        CompletenessField.BUSINESS_STATUS,
        CompletenessField.DAILY_OPENING,
    }
    assert all(
        gap.status is CompletenessStatus.STALE
        and gap.evidence[0].kind is CompletenessArtifactKind.CANONICAL_GOOGLE_REFRESH
        for gap in stale_google.values()
    )


def test_google_price_not_listed_is_not_treated_as_a_reference_price() -> None:
    dataset, readiness = _dataset_and_readiness(
        include_google_refresh=True,
        include_reference_price=False,
        google_price_observed=False,
    )

    audit = build_canonical_completeness_audit(
        dataset,
        readiness,
        as_of=AS_OF,
        stay_context=_stay_context(),
    )

    gap = next(
        item
        for item in audit.gaps
        if item.place_id == CAFE_ID and item.field is CompletenessField.REFERENCE_PRICE
    )
    assert gap.status is CompletenessStatus.MISSING
    assert gap.evidence[0].kind is CompletenessArtifactKind.CANONICAL_GOOGLE_REFRESH

    canonical_price_dataset, canonical_price_readiness = _dataset_and_readiness()
    canonical_price_audit = build_canonical_completeness_audit(
        canonical_price_dataset,
        canonical_price_readiness,
        as_of=AS_OF,
        stay_context=_stay_context(),
    )
    assert not any(
        item.place_id == CAFE_ID and item.field is CompletenessField.REFERENCE_PRICE
        for item in canonical_price_audit.gaps
    )


@pytest.mark.parametrize(
    ("status", "expected_reason"),
    [
        (
            TrivagoRegistryStatus.PROVIDER_NOT_LISTED,
            "reviewed hotel has no confirmed Trivago provider listing",
        ),
        (
            TrivagoRegistryStatus.IDENTITY_REVERIFY,
            "Trivago hotel identity requires re-verification",
        ),
    ],
)
def test_terminal_trivago_registry_state_overrides_stale_confirmed_mapping(
    status: TrivagoRegistryStatus,
    expected_reason: str,
) -> None:
    dataset, readiness = _dataset_and_readiness()

    audit = build_canonical_completeness_audit(
        dataset,
        readiness,
        as_of=AS_OF,
        stay_context=_stay_context(),
        trivago_mappings=[_trivago_mapping()],
        trivago_entries=[_trivago_registry_entry(status)],
    )

    gap = next(
        item
        for item in audit.gaps
        if item.place_id == HOTEL_ID and item.field is CompletenessField.HOTEL_MAPPING
    )
    assert gap.status is CompletenessStatus.UNRESOLVED
    assert gap.reason == expected_reason
    assert any(
        evidence.kind is CompletenessArtifactKind.TRIVAGO_REGISTRY
        and evidence.semantic_status == status.value
        for evidence in gap.evidence
    )


def test_blocking_static_gaps_and_menu_source_only_are_explicit() -> None:
    dataset, readiness = _dataset_and_readiness(
        cafe_address=None,
        include_cafe_category=False,
    )
    menu_source = GoogleMapsMenuSourceEntry(
        place_id=CAFE_ID,
        source_record_id="google-menu-source-1",
        image_url="https://images.example/menu.jpg",
        observed_at=AS_OF - timedelta(hours=1),
    )

    audit = build_canonical_completeness_audit(
        dataset,
        readiness,
        as_of=AS_OF,
        stay_context=_stay_context(),
        menu_sources=[menu_source],
    )

    assert audit.static_ingest_ready is False
    blocking_fields = {
        gap.field
        for gap in audit.gaps
        if gap.place_id == CAFE_ID and gap.severity is CompletenessSeverity.BLOCKING
    }
    assert blocking_fields == {
        CompletenessField.ADDRESS,
        CompletenessField.CATEGORY,
    }
    menu_gap = next(
        gap
        for gap in audit.gaps
        if gap.place_id == CAFE_ID and gap.field is CompletenessField.VERIFIED_MENU
    )
    assert menu_gap.status is CompletenessStatus.SOURCE_ONLY
    assert menu_gap.evidence[0].artifact_id == "google-menu-source-1"

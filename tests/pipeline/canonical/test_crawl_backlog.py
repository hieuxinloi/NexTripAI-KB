from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from nextrip_pipeline.canonical.completeness import (
    ArtifactInputDigest,
    CanonicalHotelStayContext,
    CanonicalSourcePolicy,
    CompletenessArtifactKind,
    build_canonical_completeness_audit,
)
from nextrip_pipeline.canonical.crawl_backlog import (
    CanonicalCrawlBacklog,
    CanonicalCrawlBlockedReason,
    CanonicalCrawlExecutionClaimWriter,
    CanonicalCrawlJob,
    CanonicalCrawlReason,
    CanonicalCrawlRequiredField,
    build_canonical_crawl_backlog,
    build_canonical_crawl_execution_claim,
)
from nextrip_pipeline.canonical.dataset import materialize_canonical_active_dataset
from nextrip_pipeline.crawl.trivago_registry import (
    TrivagoHotelRegistryEntry,
    TrivagoRegistryStatus,
    TrivagoSearchReviewEvidence,
)
from nextrip_pipeline.canonical.evidence import DuplicateEvidenceAuditor
from nextrip_pipeline.canonical.master import CanonicalMasterLoad, MasterRawRecord
from nextrip_pipeline.canonical.models import LegacyPlaceSlot
from nextrip_pipeline.canonical.readiness import (
    evaluate_canonical_dataset_readiness,
)
from nextrip_pipeline.canonical.resolver import (
    CanonicalIdentityResolver,
    build_canonical_identity_manifest,
)
from nextrip_pipeline.publishing import GoogleMapsMenuSourceEntry
from nextrip_pipeline.schemas import EntityType


UTC = timezone.utc
AS_OF = datetime(2026, 8, 21, 5, tzinfo=UTC)
CHECK_IN = date(2026, 8, 22)
CHECK_OUT = date(2026, 8, 23)
CAFE_ID = "cafe_dn_001"
HOTEL_ID = "hotel_dn_001"


def _audit_with_coalescible_gaps(
    trivago_status: TrivagoRegistryStatus | None = None,
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
    # The cafe deliberately lacks a cover and weekly opening schedule. These
    # static gaps must share one Google crawl with the daily-opening gap.
    cafe = MasterRawRecord(
        place_id=CAFE_ID,
        source_filename="cafe_final.json",
        record_index=0,
        raw_record={
            "id": CAFE_ID,
            "entity_type": "cafe",
            "name": "Canonical Cafe",
            "city": "Da Nang",
            "address": "1 Bach Dang",
            "coordinates": {"lat": 16.06, "lng": 108.22},
            "category": "Coffee shop",
            "description": "Verified cafe description",
            "price_range": "20,000-50,000 VND",
            "tags": [],
        },
    )
    hotel = MasterRawRecord(
        place_id=HOTEL_ID,
        source_filename="hotel_final.json",
        record_index=0,
        raw_record={
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
        },
    )
    master = CanonicalMasterLoad(
        slots=slots,
        raw_records_by_id={CAFE_ID: cafe, HOTEL_ID: hotel},
        source_files=[],
        quota_counts=[],
        explicit_duplicate_decisions=[],
    )
    manifest = build_canonical_identity_manifest(slots, generated_at=AS_OF)
    dataset = materialize_canonical_active_dataset(master, manifest)
    evidence = DuplicateEvidenceAuditor().audit([], [], [])
    readiness = evaluate_canonical_dataset_readiness(
        dataset,
        evidence,
        CanonicalIdentityResolver(manifest),
    )
    menu_source = GoogleMapsMenuSourceEntry(
        place_id=CAFE_ID,
        source_record_id="google-menu-source-1",
        image_url="https://images.example/menu.jpg",
        observed_at=AS_OF - timedelta(hours=1),
    )
    trivago_entries = (
        [
            TrivagoHotelRegistryEntry(
                entity_id=HOTEL_ID,
                master_name="Canonical Hotel",
                city="Da Nang",
                search_query="Canonical Hotel, Da Nang, Vietnam",
                status=trivago_status,
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
        ]
        if trivago_status is not None
        else []
    )
    return build_canonical_completeness_audit(
        dataset,
        readiness,
        as_of=AS_OF,
        stay_context=CanonicalHotelStayContext(
            check_in=CHECK_IN,
            check_out=CHECK_OUT,
        ),
        trivago_entries=trivago_entries,
        menu_sources=[menu_source],
        input_digests=[
            ArtifactInputDigest(
                kind=CompletenessArtifactKind.GOOGLE_REGISTRY,
                root_label="google_registry",
                file_count=1,
                input_hash="a" * 64,
            )
        ],
    )


def _source_policies() -> list[CanonicalSourcePolicy]:
    return [
        CanonicalSourcePolicy(
            source_id="google-maps-web",
            enabled=False,
            schedule_interval_minutes=1440,
            parser_version="google-parser-v1",
        ),
        CanonicalSourcePolicy(
            source_id="trivago-mcp",
            enabled=True,
            schedule_interval_minutes=300,
            parser_version="trivago-parser-v1",
        ),
    ]


def test_backlog_coalesces_provider_work_and_preserves_exact_context() -> None:
    audit = _audit_with_coalescible_gaps()

    backlog = build_canonical_crawl_backlog(audit, _source_policies())

    assert backlog.task_count == 3
    assert backlog.input_digests == audit.input_digests
    assert backlog.job_counts == {
        CanonicalCrawlJob.GOOGLE_MAPS_PLACE.value: 1,
        CanonicalCrawlJob.MENU_HUMAN_REVIEW.value: 1,
        CanonicalCrawlJob.TRIVAGO_AVAILABILITY.value: 1,
    }
    google_task = next(
        task
        for task in backlog.tasks
        if task.job is CanonicalCrawlJob.GOOGLE_MAPS_PLACE
    )
    assert google_task.place_id == CAFE_ID
    assert set(google_task.required_fields) == {
        CanonicalCrawlRequiredField.BUSINESS_STATUS,
        CanonicalCrawlRequiredField.COVER_IMAGE,
        CanonicalCrawlRequiredField.OPENING_SCHEDULE,
        CanonicalCrawlRequiredField.DAILY_OPENING,
    }
    assert set(google_task.reason_codes) == {
        CanonicalCrawlReason.BUSINESS_STATUS_MISSING,
        CanonicalCrawlReason.DAILY_OPENING_MISSING,
        CanonicalCrawlReason.STATIC_BACKFILL,
    }
    assert len(google_task.completeness_gap_hashes) == 4

    hotel_task = next(
        task
        for task in backlog.tasks
        if task.job is CanonicalCrawlJob.TRIVAGO_AVAILABILITY
    )
    assert hotel_task.place_id == HOTEL_ID
    assert set(hotel_task.required_fields) == {
        CanonicalCrawlRequiredField.HOTEL_MAPPING,
        CanonicalCrawlRequiredField.HOTEL_AVAILABILITY,
        CanonicalCrawlRequiredField.HOTEL_PRICE,
    }
    assert len(hotel_task.completeness_gap_hashes) == 3
    assert hotel_task.context == {
        "check_in": CHECK_IN.isoformat(),
        "check_out": CHECK_OUT.isoformat(),
        "occupancy": {"adults": 2, "children": 0, "rooms": 1},
        "children_ages": [],
        "currency": "VND",
        "lookahead_days": 1,
    }
    assert hotel_task.schedule_interval_minutes == 300


def test_disabled_google_and_manual_menu_tasks_are_blocked_independently() -> None:
    backlog = build_canonical_crawl_backlog(
        _audit_with_coalescible_gaps(),
        _source_policies(),
    )

    google_task = next(
        task
        for task in backlog.tasks
        if task.job is CanonicalCrawlJob.GOOGLE_MAPS_PLACE
    )
    menu_task = next(
        task
        for task in backlog.tasks
        if task.job is CanonicalCrawlJob.MENU_HUMAN_REVIEW
    )
    hotel_task = next(
        task
        for task in backlog.tasks
        if task.job is CanonicalCrawlJob.TRIVAGO_AVAILABILITY
    )

    assert google_task.automatic is False
    assert google_task.blocked_reasons == [CanonicalCrawlBlockedReason.SOURCE_DISABLED]
    assert menu_task.automatic is False
    assert menu_task.blocked_reasons == [
        CanonicalCrawlBlockedReason.MANUAL_MENU_VERIFICATION
    ]
    assert menu_task.source_id == "human-review"
    assert menu_task.context == {
        "image_url": "https://images.example/menu.jpg",
        "source_record_id": "google-menu-source-1",
    }
    assert hotel_task.automatic is True
    assert hotel_task.blocked_reasons == []
    assert backlog.automatic_count == 1
    assert backlog.blocked_count == 2
    assert backlog.manual_count == 1


@pytest.mark.parametrize(
    ("status", "blocked_reason", "reason_code"),
    [
        (
            TrivagoRegistryStatus.PROVIDER_NOT_LISTED,
            CanonicalCrawlBlockedReason.PROVIDER_NOT_LISTED,
            CanonicalCrawlReason.PROVIDER_NOT_LISTED,
        ),
        (
            TrivagoRegistryStatus.IDENTITY_REVERIFY,
            CanonicalCrawlBlockedReason.IDENTITY_REVERIFY,
            CanonicalCrawlReason.IDENTITY_REVERIFY,
        ),
    ],
)
def test_terminal_trivago_outcome_blocks_automatic_backlog_retry(
    status: TrivagoRegistryStatus,
    blocked_reason: CanonicalCrawlBlockedReason,
    reason_code: CanonicalCrawlReason,
) -> None:
    backlog = build_canonical_crawl_backlog(
        _audit_with_coalescible_gaps(status),
        _source_policies(),
    )

    hotel_task = next(
        task
        for task in backlog.tasks
        if task.job is CanonicalCrawlJob.TRIVAGO_AVAILABILITY
    )
    assert hotel_task.automatic is False
    assert blocked_reason in hotel_task.blocked_reasons
    assert reason_code in hotel_task.reason_codes


def test_backlog_is_deterministic_and_rejects_a_tampered_hash() -> None:
    audit = _audit_with_coalescible_gaps()
    policies = _source_policies()

    first = build_canonical_crawl_backlog(audit, policies)
    second = build_canonical_crawl_backlog(audit, list(reversed(policies)))

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.backlog_id == f"canonical-crawl-backlog-{first.backlog_hash[:20]}"

    tampered = first.model_dump(mode="json")
    tampered["backlog_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="backlog_hash does not match"):
        CanonicalCrawlBacklog.model_validate(tampered)


def test_execution_claim_is_atomic_and_idempotent(tmp_path: Path) -> None:
    backlog = build_canonical_crawl_backlog(
        _audit_with_coalescible_gaps(),
        _source_policies(),
    )
    hotel_task = next(
        task
        for task in backlog.tasks
        if task.job is CanonicalCrawlJob.TRIVAGO_AVAILABILITY
    )
    claim = build_canonical_crawl_execution_claim(
        backlog,
        job=CanonicalCrawlJob.TRIVAGO_AVAILABILITY,
        task_ids=[hotel_task.task_id],
        selection_context={"max_requests": 1, "offset": 0},
        claimed_at=AS_OF,
    )
    writer = CanonicalCrawlExecutionClaimWriter(tmp_path)

    first_path, first_created = writer.claim(claim)
    second_path, second_created = writer.claim(claim)

    assert first_path == second_path
    assert first_created is True
    assert second_created is False
    assert claim.run_id.startswith("canonical-trivago_availability-")

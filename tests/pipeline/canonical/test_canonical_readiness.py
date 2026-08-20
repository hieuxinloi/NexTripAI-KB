from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nextrip_pipeline.canonical.dataset import materialize_canonical_active_dataset
from nextrip_pipeline.canonical.evidence import (
    DuplicateEvidenceAudit,
    DuplicateEvidenceReason,
    DuplicateEvidenceStatus,
    DuplicateGroupEvidence,
    DuplicatePairEvidence,
    MasterIdentityProjection,
)
from nextrip_pipeline.canonical.master import CanonicalMasterLoad, MasterRawRecord
from nextrip_pipeline.canonical.models import (
    DistinctIdentityDecision,
    DuplicateIdentityDecision,
    LegacyPlaceSlot,
    stable_identifier,
    stable_sha256,
)
from nextrip_pipeline.canonical.readiness import (
    CanonicalDatasetReadinessWriter,
    CanonicalDatasetReadinessReport,
    CanonicalDatasetNotReadyError,
    CanonicalReadinessInputError,
    ReadinessReason,
    evaluate_canonical_dataset_readiness,
    read_canonical_dataset_readiness,
    require_canonical_dataset_publish_ready,
)
from nextrip_pipeline.canonical.resolver import (
    CanonicalIdentityResolver,
    build_canonical_identity_manifest,
)
from nextrip_pipeline.schemas import EntityType, GeoPoint


UTC = timezone.utc
GENERATED_AT = datetime(2026, 8, 20, 5, tzinfo=UTC)
PLACE_IDS = ["cafe_dn_001", "night_dn_001"]


def _state(*, merged: bool):
    slots = [
        LegacyPlaceSlot(
            legacy_place_id="cafe_dn_001",
            city_id="city_da_nang",
            primary_type=EntityType.CAFE,
        ),
        LegacyPlaceSlot(
            legacy_place_id="night_dn_001",
            city_id="city_da_nang",
            primary_type=EntityType.NIGHTLIFE,
        ),
    ]
    raws = {
        slot.legacy_place_id: MasterRawRecord(
            place_id=slot.legacy_place_id,
            source_filename=(
                "cafe_final.json"
                if slot.primary_type is EntityType.CAFE
                else "nightlife_final.json"
            ),
            record_index=index,
            raw_record={
                "id": slot.legacy_place_id,
                "entity_type": slot.primary_type.value,
                "name": "One Physical Venue",
                "city": "Đà Nẵng",
                "address": "1 Test Street",
                "coordinates": {
                    "lat": 16.05 + index * 0.00001,
                    "lng": 108.2,
                },
                "tags": [],
            },
        )
        for index, slot in enumerate(slots)
    }
    master = CanonicalMasterLoad(
        slots=slots,
        raw_records_by_id=raws,
        source_files=[],
        quota_counts=[],
        explicit_duplicate_decisions=[],
    )
    decisions = (
        [
            DuplicateIdentityDecision(
                keeper_legacy_place_id="cafe_dn_001",
                duplicate_legacy_place_ids=["night_dn_001"],
                reason="reviewed explicit merge",
            )
        ]
        if merged
        else []
    )
    manifest = build_canonical_identity_manifest(
        slots,
        duplicate_decisions=decisions,
        generated_at=GENERATED_AT,
    )
    dataset = materialize_canonical_active_dataset(master, manifest)
    return dataset, CanonicalIdentityResolver(manifest)


def _audit(
    status: DuplicateEvidenceStatus,
    *,
    place_ids: list[str] | None = None,
) -> DuplicateEvidenceAudit:
    ids = sorted(place_ids or PLACE_IDS)
    reason_by_status = {
        DuplicateEvidenceStatus.CONFIRMED: (
            DuplicateEvidenceReason.SHARED_GOOGLE_PLACE_TOKEN
        ),
        DuplicateEvidenceStatus.DISTINCT: (
            DuplicateEvidenceReason.CONFLICTING_GOOGLE_PLACE_TOKEN
        ),
        DuplicateEvidenceStatus.REVIEW: (
            DuplicateEvidenceReason.INSUFFICIENT_STRONG_EVIDENCE
        ),
    }
    reason = reason_by_status[status]
    masters = [
        MasterIdentityProjection(
            place_id=place_id,
            entity_type=(
                EntityType.CAFE
                if place_id.startswith("cafe_")
                else EntityType.NIGHTLIFE
            ),
            name="One Physical Venue",
            normalized_name="one physical venue",
            city="Đà Nẵng",
            normalized_city="da nang",
            address="1 Test Street",
            location=GeoPoint(latitude=16.05, longitude=108.2),
        )
        for place_id in ids
    ]
    pair = DuplicatePairEvidence(
        left_place_id=ids[0],
        right_place_id=ids[1],
        status=status,
        reason_codes=[reason],
        same_master_name=True,
        same_master_city=True,
    )
    group = DuplicateGroupEvidence(
        group_id=stable_identifier("duplicate_group", *ids),
        place_ids=ids,
        status=status,
        reason_codes=[reason],
        master_identities=masters,
        google_observations=[],
        missing_google_observation_place_ids=ids,
        pair_evidence=[pair],
    )
    groups = [group]
    payload = {
        "schema_version": "1.0.0",
        "groups": [item.model_dump(mode="json") for item in groups],
    }
    audit_hash = stable_sha256(payload)
    counts = Counter(item.status.value for item in groups)
    return DuplicateEvidenceAudit(
        audit_id=f"duplicate_evidence_{audit_hash[:20]}",
        audit_hash=audit_hash,
        group_count=1,
        status_counts={
            item.value: counts[item.value] for item in DuplicateEvidenceStatus
        },
        missing_google_observation_place_ids=ids,
        groups=groups,
    )


@pytest.mark.parametrize(
    ("status", "merged", "result_field", "reason"),
    [
        (
            DuplicateEvidenceStatus.CONFIRMED,
            True,
            "resolved_merge",
            ReadinessReason.CONFIRMED_MERGED,
        ),
        (
            DuplicateEvidenceStatus.DISTINCT,
            False,
            "resolved_distinct",
            ReadinessReason.DISTINCT_SEPARATE,
        ),
    ],
)
def test_consistent_confirmed_and_distinct_groups_are_publish_ready(
    status: DuplicateEvidenceStatus,
    merged: bool,
    result_field: str,
    reason: ReadinessReason,
) -> None:
    dataset, resolver = _state(merged=merged)

    report = evaluate_canonical_dataset_readiness(
        dataset,
        _audit(status),
        resolver,
    )

    assert report.publish_ready is True
    assert report.unresolved_groups == []
    result = getattr(report, result_field)
    assert len(result) == 1
    assert result[0].reason is reason


@pytest.mark.parametrize(
    ("status", "merged", "reason"),
    [
        (
            DuplicateEvidenceStatus.CONFIRMED,
            False,
            ReadinessReason.CONFIRMED_NOT_MERGED,
        ),
        (
            DuplicateEvidenceStatus.DISTINCT,
            True,
            ReadinessReason.DISTINCT_COLLAPSED,
        ),
    ],
)
def test_evidence_manifest_conflicts_block_publish(
    status: DuplicateEvidenceStatus,
    merged: bool,
    reason: ReadinessReason,
) -> None:
    dataset, resolver = _state(merged=merged)

    report = evaluate_canonical_dataset_readiness(
        dataset,
        _audit(status),
        resolver,
    )

    assert report.publish_ready is False
    assert report.unresolved_groups[0].reason is reason


def test_review_blocks_distinct_active_ids_but_explicit_merge_resolves_it() -> None:
    separate_dataset, separate_resolver = _state(merged=False)
    merged_dataset, merged_resolver = _state(merged=True)
    evidence = _audit(DuplicateEvidenceStatus.REVIEW)

    blocked = evaluate_canonical_dataset_readiness(
        separate_dataset,
        evidence,
        separate_resolver,
    )
    resolved = evaluate_canonical_dataset_readiness(
        merged_dataset,
        evidence,
        merged_resolver,
    )

    assert blocked.publish_ready is False
    assert blocked.unresolved_groups[0].reason is (
        ReadinessReason.REVIEW_ACTIVE_DISTINCT
    )
    with pytest.raises(CanonicalDatasetNotReadyError, match="1 unresolved"):
        require_canonical_dataset_publish_ready(blocked)
    assert resolved.publish_ready is True
    assert resolved.resolved_merge[0].reason is (
        ReadinessReason.REVIEW_EXPLICITLY_MERGED
    )
    assert resolved.resolved_merge[0].active_canonical_ids == ["cafe_dn_001"]
    assert require_canonical_dataset_publish_ready(resolved) == resolved


def test_review_is_resolved_by_explicit_distinct_decision_with_provenance() -> None:
    dataset, resolver = _state(merged=False)
    evidence = _audit(DuplicateEvidenceStatus.REVIEW)
    decision = DistinctIdentityDecision(
        place_ids=["night_dn_001", "cafe_dn_001"],
        reason="human_review_verified_different_google_branches",
    )

    report = evaluate_canonical_dataset_readiness(
        dataset,
        evidence,
        resolver,
        distinct_decisions=[decision],
    )

    assert report.publish_ready is True
    assert report.schema_version == "1.1.0"
    assert report.explicit_distinct_decisions == [decision]
    assert report.resolved_distinct[0].reason is (
        ReadinessReason.REVIEW_EXPLICITLY_DISTINCT
    )
    without_decision = evaluate_canonical_dataset_readiness(
        dataset,
        evidence,
        resolver,
    )
    assert report.readiness_hash != without_decision.readiness_hash


def test_explicit_distinct_decision_fails_if_manifest_collapsed_ids() -> None:
    dataset, resolver = _state(merged=True)
    decision = DistinctIdentityDecision(place_ids=PLACE_IDS)

    report = evaluate_canonical_dataset_readiness(
        dataset,
        _audit(DuplicateEvidenceStatus.REVIEW),
        resolver,
        distinct_decisions=[decision],
    )

    assert report.publish_ready is False
    assert report.unresolved_groups[0].reason is (
        ReadinessReason.REVIEW_DISTINCT_COLLAPSED
    )


def test_unknown_distinct_decision_group_is_rejected() -> None:
    dataset, resolver = _state(merged=False)
    decision = DistinctIdentityDecision(
        place_ids=["cafe_dn_001", "night_dn_999"]
    )

    with pytest.raises(CanonicalReadinessInputError, match="unknown evidence groups"):
        evaluate_canonical_dataset_readiness(
            dataset,
            _audit(DuplicateEvidenceStatus.REVIEW),
            resolver,
            distinct_decisions=[decision],
        )


def test_distinct_decision_conflicting_with_confirmed_evidence_is_rejected() -> None:
    dataset, resolver = _state(merged=False)

    with pytest.raises(CanonicalReadinessInputError, match="confirmed duplicate"):
        evaluate_canonical_dataset_readiness(
            dataset,
            _audit(DuplicateEvidenceStatus.CONFIRMED),
            resolver,
            distinct_decisions=[DistinctIdentityDecision(place_ids=PLACE_IDS)],
        )


def test_duplicate_distinct_decisions_are_rejected() -> None:
    dataset, resolver = _state(merged=False)
    decisions = [
        DistinctIdentityDecision(place_ids=PLACE_IDS, reason="first review"),
        DistinctIdentityDecision(place_ids=PLACE_IDS, reason="second review"),
    ]

    with pytest.raises(CanonicalReadinessInputError, match="one distinct decision"):
        evaluate_canonical_dataset_readiness(
            dataset,
            _audit(DuplicateEvidenceStatus.REVIEW),
            resolver,
            distinct_decisions=decisions,
        )


def test_unknown_evidence_identity_fails_closed() -> None:
    dataset, resolver = _state(merged=False)
    evidence = _audit(
        DuplicateEvidenceStatus.REVIEW,
        place_ids=["cafe_dn_001", "night_dn_999"],
    )

    report = evaluate_canonical_dataset_readiness(dataset, evidence, resolver)

    assert report.publish_ready is False
    assert report.unresolved_groups[0].reason is ReadinessReason.UNKNOWN_IDENTITY
    assert report.unresolved_groups[0].unknown_place_ids == ["night_dn_999"]


def test_report_hash_and_writer_are_deterministic_and_immutable(
    tmp_path: Path,
) -> None:
    dataset, resolver = _state(merged=True)
    evidence = _audit(DuplicateEvidenceStatus.CONFIRMED)
    first = evaluate_canonical_dataset_readiness(dataset, evidence, resolver)
    retry = evaluate_canonical_dataset_readiness(dataset, evidence, resolver)
    writer = CanonicalDatasetReadinessWriter(tmp_path / "readiness")

    assert retry.readiness_hash == first.readiness_hash
    path = writer.write(first)
    original = path.read_bytes()
    assert writer.write(retry) == path
    assert path.read_bytes() == original
    assert read_canonical_dataset_readiness(path) == first


def test_schema_1_0_readiness_artifact_remains_valid() -> None:
    dataset, resolver = _state(merged=True)
    current = evaluate_canonical_dataset_readiness(
        dataset,
        _audit(DuplicateEvidenceStatus.CONFIRMED),
        resolver,
    )
    legacy_payload = current.model_dump(
        mode="json",
        exclude={
            "readiness_id",
            "readiness_hash",
            "explicit_distinct_decisions",
        },
    )
    legacy_payload["schema_version"] = "1.0.0"
    legacy_hash = stable_sha256(legacy_payload)

    parsed = CanonicalDatasetReadinessReport.model_validate(
        {
            **legacy_payload,
            "readiness_id": f"canonical-readiness-{legacy_hash[:20]}",
            "readiness_hash": legacy_hash,
        }
    )

    assert parsed.schema_version == "1.0.0"
    assert parsed.explicit_distinct_decisions == []


def test_dataset_and_resolver_must_use_the_same_manifest() -> None:
    dataset, _ = _state(merged=False)
    _, other_resolver = _state(merged=True)

    with pytest.raises(CanonicalReadinessInputError, match="different manifests"):
        evaluate_canonical_dataset_readiness(
            dataset,
            _audit(DuplicateEvidenceStatus.REVIEW),
            other_resolver,
        )


def test_tampered_evidence_hash_is_rejected() -> None:
    dataset, resolver = _state(merged=True)
    evidence = _audit(DuplicateEvidenceStatus.CONFIRMED).model_copy(
        update={"audit_hash": "0" * 64}
    )

    with pytest.raises(CanonicalReadinessInputError, match="audit_hash"):
        evaluate_canonical_dataset_readiness(dataset, evidence, resolver)

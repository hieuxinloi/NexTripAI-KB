from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nextrip_pipeline.canonical.candidate import (
    CandidateDisposition,
    CandidateExternalIdentity,
    CandidateReasonCode,
    CandidateValidationResult,
    CanonicalReplacementCandidate,
)
from nextrip_pipeline.canonical.detail import (
    CandidateDetailStage,
    candidate_detail_payload,
)
from nextrip_pipeline.canonical.master import (
    CanonicalMasterLoad,
    MasterRawRecord,
)
from nextrip_pipeline.canonical.models import (
    DuplicateIdentityDecision,
    EntityCityVacancy,
    LegacyPlaceSlot,
    VacancyReplacementDecision,
    stable_identifier,
    stable_sha256,
)
from nextrip_pipeline.canonical.projection import (
    ApprovedReplacement,
    ApprovedReplacementWriter,
    ExistingIdentityProjectionError,
    ProjectionIssueCode,
    ReplacementApprovalMethod,
    ReplacementApprovalError,
    ReplacementWriteConflictError,
    build_existing_identity_projection,
    load_approved_replacement_slots,
    load_approved_replacements,
    materialize_master_record,
)
from nextrip_pipeline.canonical.resolver import build_canonical_identity_manifest
from nextrip_pipeline.schemas import (
    EntityType,
    ExternalEntityMapping,
    GeoPoint,
    MappingStatus,
)
from nextrip_pipeline.schemas.current_place import (
    CurrentPlaceProvenance,
    CurrentPlaceSnapshot,
)


UTC = timezone.utc
OBSERVED_AT = datetime(2026, 8, 20, 3, tzinfo=UTC)
APPROVED_AT = OBSERVED_AT + timedelta(hours=1)
GOOGLE_TOKEN = "0x314219caaa000001:0x1000000000000001"


def _maps_url(token: str = GOOGLE_TOKEN) -> str:
    return (
        "https://www.google.com/maps/place/New+Cafe/"
        f"@16.051,108.202,17z/data=!3m1!4b1!4m6!3m5!1s{token}!8m2"
    )


def _master_and_manifest():
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
    raw_records = {
        "cafe_dn_001": MasterRawRecord(
            place_id="cafe_dn_001",
            source_filename="cafe_final.json",
            record_index=0,
            raw_record={
                "id": "cafe_dn_001",
                "entity_type": "cafe",
                "name": "Master Cafe",
                "city": "Đà Nẵng",
                "address": "1 Master Street",
                "phone": "0905000001",
                "website_url": "https://master-cafe.example",
                "coordinates": {"lat": 16.05, "lng": 108.2},
            },
        ),
        "night_dn_001": MasterRawRecord(
            place_id="night_dn_001",
            source_filename="nightlife_final.json",
            record_index=0,
            raw_record={
                "id": "night_dn_001",
                "entity_type": "nightlife",
                "name": "Legacy duplicate name",
                "city": "Đà Nẵng",
                "coordinates": {"lat": 16.0501, "lng": 108.2001},
            },
        ),
    }
    master = CanonicalMasterLoad(
        slots=slots,
        raw_records_by_id=raw_records,
        source_files=[],
        quota_counts=[],
        explicit_duplicate_decisions=[],
    )
    manifest = build_canonical_identity_manifest(
        slots,
        duplicate_decisions=[
            DuplicateIdentityDecision(
                keeper_legacy_place_id="cafe_dn_001",
                duplicate_legacy_place_ids=["night_dn_001"],
                reason="reviewed duplicate",
            )
        ],
        generated_at=OBSERVED_AT,
    )
    return master, manifest


def _write_current_place(path: Path) -> None:
    snapshot = CurrentPlaceSnapshot(
        place_id="cafe_dn_001",
        entity_type=EntityType.CAFE,
        city="Đà Nẵng",
        city_id="city_da_nang",
        name="Official Google Name",
        phone="0905111111",
        website_url="https://official.example",
        location=GeoPoint(
            latitude=16.051,
            longitude=108.202,
            source="google-maps-web",
        ),
        provenance=CurrentPlaceProvenance(
            mapping_id="google-maps-cafe_dn_001",
            run_id="maps-run",
            source_record_id="maps-source-record",
            source_id="google-maps-web",
            source_url=_maps_url(),
        ),
        updated_at=OBSERVED_AT,
    )
    path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")


def _write_confirmed_mapping(path: Path) -> None:
    mapping = ExternalEntityMapping(
        mapping_id="google-maps-cafe_dn_001",
        entity_id="cafe_dn_001",
        entity_type=EntityType.CAFE,
        source_id="google-maps-web",
        # Generated registries historically used the name here. Projection
        # derives the stable token from the confirmed final place URL.
        external_id="Official Google Name",
        external_url=_maps_url(),
        status=MappingStatus.CONFIRMED,
        matched_at=OBSERVED_AT,
        verified_at=OBSERVED_AT,
        attributes={"city_id": "city_da_nang"},
    )
    path.write_text(mapping.model_dump_json(indent=2), encoding="utf-8")


def test_projects_active_and_retired_with_current_enrichment_and_google_token(
    tmp_path: Path,
) -> None:
    master, manifest = _master_and_manifest()
    current_dir = tmp_path / "current"
    mapping_dir = tmp_path / "mappings"
    current_dir.mkdir()
    mapping_dir.mkdir()
    _write_current_place(current_dir / "cafe_dn_001.json")
    _write_confirmed_mapping(mapping_dir / "cafe_dn_001.json")
    (current_dir / "unrelated-broken.json").write_text("{broken", encoding="utf-8")
    (mapping_dir / "unrelated-broken.json").write_text("[]", encoding="utf-8")

    projection = build_existing_identity_projection(
        master,
        manifest,
        current_place_directory=current_dir,
        current_google_mapping_directory=mapping_dir,
    )

    assert [(item.place_id, item.retired) for item in projection.identities] == [
        ("cafe_dn_001", False),
        ("night_dn_001", True),
    ]
    active, retired = projection.identities
    assert active.name == "Official Google Name"
    assert active.location is not None
    assert active.location.source == "google-maps-web"
    assert retired.name == "Legacy duplicate name"
    assert retired.location is not None
    assert retired.location.source == "verified-master-data"
    # Both aliases protect the same known physical place from being selected
    # again as a replacement.
    for identity in (active, retired):
        assert identity.external_identities[0].external_id == GOOGLE_TOKEN
        assert str(identity.external_identities[0].external_url) == _maps_url()
    assert len(projection.quarantined_inputs) == 2
    assert {item.code for item in projection.quarantined_inputs} == {
        ProjectionIssueCode.MALFORMED_INPUT
    }


def test_missing_current_directories_fall_back_to_verified_master(tmp_path: Path) -> None:
    master, manifest = _master_and_manifest()

    projection = build_existing_identity_projection(
        master,
        manifest,
        current_place_directory=tmp_path / "missing-current",
        current_google_mapping_directory=tmp_path / "missing-mappings",
    )

    active = projection.identities[0]
    assert active.name == "Master Cafe"
    assert active.phone == "0905000001"
    assert str(active.website_url) == "https://master-cafe.example/"
    assert active.external_identities == []
    assert projection.quarantined_inputs == []


def _filled_replacement_projection_inputs():
    slots = [
        LegacyPlaceSlot(
            legacy_place_id="cafe_dn_001",
            city_id="city_da_nang",
            primary_type=EntityType.CAFE,
        ),
        LegacyPlaceSlot(
            legacy_place_id="cafe_dn_002",
            city_id="city_da_nang",
            primary_type=EntityType.CAFE,
        ),
    ]
    master = CanonicalMasterLoad(
        slots=slots,
        raw_records_by_id={
            "cafe_dn_001": MasterRawRecord(
                place_id="cafe_dn_001",
                source_filename="cafe_final.json",
                record_index=0,
                raw_record={
                    "id": "cafe_dn_001",
                    "entity_type": "cafe",
                    "name": "Keeper Cafe",
                    "city": "Đà Nẵng",
                    "address": "1 Keeper Street",
                    "coordinates": {"lat": 16.04, "lng": 108.20},
                },
            ),
            "cafe_dn_002": MasterRawRecord(
                place_id="cafe_dn_002",
                source_filename="cafe_final.json",
                record_index=1,
                raw_record={
                    "id": "cafe_dn_002",
                    "entity_type": "cafe",
                    "name": "Retired Alias",
                    "city": "Đà Nẵng",
                    "address": "1 Keeper Street",
                    "coordinates": {"lat": 16.0401, "lng": 108.2001},
                },
            ),
        },
        source_files=[],
        quota_counts=[],
        explicit_duplicate_decisions=[],
    )
    duplicate = DuplicateIdentityDecision(
        keeper_legacy_place_id="cafe_dn_001",
        duplicate_legacy_place_ids=["cafe_dn_002"],
        reason="reviewed duplicate",
    )
    initial = build_canonical_identity_manifest(
        slots,
        duplicate_decisions=[duplicate],
        generated_at=OBSERVED_AT,
    )
    approval = _approval()
    replacement = materialize_master_record(approval).model_copy(
        update={"address": "100 Google Replacement Street"}
    )
    filled = build_canonical_identity_manifest(
        [*slots, replacement.to_legacy_place_slot()],
        duplicate_decisions=[duplicate],
        replacement_decisions=[
            VacancyReplacementDecision(
                retired_place_id="cafe_dn_002",
                replacement_place_id="cafe_dn_100",
            )
        ],
        previous_manifest=initial,
        generated_at=APPROVED_AT,
    )
    return master, initial, filled, replacement


def _filled_cross_type_projection_inputs():
    master, initial = _master_and_manifest()
    approval = ApprovedReplacement.from_candidate_detail(
        _detail(
            candidate_key="candidate-cafe-for-nightlife-capacity",
            retired_place_id="night_dn_001",
            vacancy_entity_type=EntityType.NIGHTLIFE,
            candidate_entity_type=EntityType.CAFE,
        ),
        allocated_place_id="cafe_dn_100",
        reviewer="reviewer@example.com",
        approved_at=APPROVED_AT,
    )
    replacement = materialize_master_record(approval)
    filled = build_canonical_identity_manifest(
        [*master.slots, replacement.to_legacy_place_slot()],
        duplicate_decisions=[
            DuplicateIdentityDecision(
                keeper_legacy_place_id="cafe_dn_001",
                duplicate_legacy_place_ids=["night_dn_001"],
                reason="reviewed duplicate",
            )
        ],
        replacement_decisions=[
            VacancyReplacementDecision(
                retired_place_id="night_dn_001",
                replacement_place_id="cafe_dn_100",
            )
        ],
        previous_manifest=initial,
        generated_at=APPROVED_AT,
    )
    return master, filled, replacement


def test_projection_reloads_filled_replacement_overlay_without_master_rewrite() -> None:
    master, _, filled, replacement = _filled_replacement_projection_inputs()

    projection = build_existing_identity_projection(
        master,
        filled,
        approved_replacements=[replacement],
    )

    assert [(item.place_id, item.retired) for item in projection.identities] == [
        ("cafe_dn_001", False),
        ("cafe_dn_002", True),
        ("cafe_dn_100", False),
    ]
    projected = projection.identities[2]
    assert projected.name == "New Distinct Cafe"
    assert projected.address == "100 Google Replacement Street"
    assert projected.phone == "0905222222"
    assert str(projected.website_url) == "https://new-cafe.example/"
    assert projected.location is not None
    assert projected.location.source == "google-maps-web"
    assert projected.external_identities[0].external_id == GOOGLE_TOKEN


def test_projection_accepts_actual_type_overlay_for_cross_type_city_fill() -> None:
    master, filled, replacement = _filled_cross_type_projection_inputs()

    projection = build_existing_identity_projection(
        master,
        filled,
        approved_replacements=[replacement],
    )

    vacancy = next(
        item
        for item in filled.vacancies
        if item.retired_place_id == "night_dn_001"
    )
    projected = next(
        item for item in projection.identities if item.place_id == "cafe_dn_100"
    )
    assert vacancy.entity_type is EntityType.NIGHTLIFE
    assert vacancy.replacement_place_id == "cafe_dn_100"
    assert projected.entity_type is EntityType.CAFE
    assert projected.city_id == vacancy.city_id


def test_projection_rejects_unrepresented_or_duplicate_overlay() -> None:
    master, initial, filled, replacement = _filled_replacement_projection_inputs()

    with pytest.raises(ExistingIdentityProjectionError, match="active matching"):
        build_existing_identity_projection(
            master,
            initial,
            approved_replacements=[replacement],
        )
    with pytest.raises(ExistingIdentityProjectionError, match="duplicate"):
        build_existing_identity_projection(
            master,
            filled,
            approved_replacements=[replacement, replacement],
        )


def test_projection_rejects_overlay_token_owned_by_existing_identity(
    tmp_path: Path,
) -> None:
    master, _, filled, replacement = _filled_replacement_projection_inputs()
    mapping_dir = tmp_path / "mappings"
    mapping_dir.mkdir()
    _write_confirmed_mapping(mapping_dir / "cafe_dn_001.json")

    with pytest.raises(ExistingIdentityProjectionError, match="already belongs"):
        build_existing_identity_projection(
            master,
            filled,
            current_google_mapping_directory=mapping_dir,
            approved_replacements=[replacement],
        )


def _detail(
    *,
    candidate_key: str = "candidate-new-cafe",
    retired_place_id: str = "cafe_dn_002",
    vacancy_entity_type: EntityType = EntityType.CAFE,
    candidate_entity_type: EntityType = EntityType.CAFE,
    token: str = GOOGLE_TOKEN,
    url_token: str | None = None,
    allocated_location_source: str = "google-maps-web",
    disposition: CandidateDisposition = CandidateDisposition.PASS,
) -> CandidateDetailStage:
    vacancy = EntityCityVacancy(
        vacancy_id=stable_identifier(
            "vacancy",
            "city_da_nang",
            vacancy_entity_type.value,
            retired_place_id,
        ),
        retired_place_id=retired_place_id,
        city_id="city_da_nang",
        entity_type=vacancy_entity_type,
    )
    candidate = CanonicalReplacementCandidate(
        candidate_key=candidate_key,
        entity_type=candidate_entity_type,
        city_id="city_da_nang",
        name="New Distinct Cafe",
        phone="0905222222",
        website_url="https://new-cafe.example",
        location=GeoPoint(
            latitude=16.051,
            longitude=108.202,
            source=allocated_location_source,
        ),
        external_identities=[
            CandidateExternalIdentity(
                source_id="google-maps-web",
                external_id=token,
                external_url=_maps_url(url_token or token),
            )
        ],
    )
    validation = CandidateValidationResult(
        candidate_key=candidate_key,
        status=disposition,
        reason_codes=(
            [CandidateReasonCode.NO_DUPLICATE_SIGNAL]
            if disposition is CandidateDisposition.PASS
            else [CandidateReasonCode.EXACT_NAME_CITY_DISTANCE_UNKNOWN]
        ),
    )
    detail_id = stable_identifier("candidate-detail", candidate_key)
    payload = candidate_detail_payload(
        schema_version="1.0.0",
        detail_id=detail_id,
        run_id="candidate-detail-run",
        source_record_id=f"source-{candidate_key}",
        observation_id=f"observation-{candidate_key}",
        vacancy=vacancy,
        candidate=candidate,
        validation=validation,
    )
    return CandidateDetailStage(
        detail_id=detail_id,
        detail_hash=stable_sha256(payload),
        run_id="candidate-detail-run",
        source_record_id=f"source-{candidate_key}",
        observation_id=f"observation-{candidate_key}",
        observed_at=OBSERVED_AT,
        vacancy=vacancy,
        candidate=candidate,
        validation=validation,
    )


def _approval(
    *,
    candidate_key: str = "candidate-new-cafe",
    retired_place_id: str = "cafe_dn_002",
    token: str = GOOGLE_TOKEN,
    allocated_place_id: str = "cafe_dn_100",
) -> ApprovedReplacement:
    return ApprovedReplacement.from_candidate_detail(
        _detail(
            candidate_key=candidate_key,
            retired_place_id=retired_place_id,
            token=token,
        ),
        allocated_place_id=allocated_place_id,
        reviewer="reviewer@example.com",
        approved_at=APPROVED_AT,
    )


def test_approval_and_materialization_are_strict_traceable_and_non_mutating() -> None:
    detail = _detail()
    before = detail.model_dump(mode="json")

    approval = ApprovedReplacement.from_candidate_detail(
        detail,
        allocated_place_id="cafe_dn_100",
        reviewer="reviewer@example.com",
        approved_at=APPROVED_AT,
    )
    record = materialize_master_record(approval)

    assert detail.model_dump(mode="json") == before
    assert record.id == "cafe_dn_100"
    assert record.city == "Đà Nẵng"
    assert record.coordinates.model_dump() == {"lat": 16.051, "lng": 108.202}
    assert record.verification_status.value == "human_verified"
    assert record.provenance.candidate_detail_id == detail.detail_id
    assert record.provenance.candidate_detail_hash == detail.detail_hash
    assert record.provenance.source_record_ids == [detail.source_record_id]
    assert record.provenance.observation_ids == [detail.observation_id]
    assert record.provenance.replacement_of == "cafe_dn_002"
    assert record.provenance.google_external_id == GOOGLE_TOKEN
    assert approval.approval_method is ReplacementApprovalMethod.HUMAN
    assert record.provenance.approval_method is ReplacementApprovalMethod.HUMAN


def test_cross_type_approval_uses_actual_candidate_type_and_source_vacancy() -> None:
    detail = _detail(
        candidate_key="candidate-cafe-for-nightlife-capacity",
        retired_place_id="night_dn_001",
        vacancy_entity_type=EntityType.NIGHTLIFE,
        candidate_entity_type=EntityType.CAFE,
    )

    approval = ApprovedReplacement.from_candidate_detail(
        detail,
        allocated_place_id="cafe_dn_100",
        reviewer="reviewer@example.com",
        approved_at=APPROVED_AT,
    )
    record = materialize_master_record(approval)

    assert approval.replacement_of == "night_dn_001"
    assert record.id == "cafe_dn_100"
    assert record.entity_type is EntityType.CAFE
    assert record.primary_type is EntityType.CAFE
    assert record.provenance.vacancy_id == detail.vacancy.vacancy_id
    assert record.provenance.replacement_of == "night_dn_001"

    with pytest.raises(ValueError, match="entity/city slot"):
        ApprovedReplacement.from_candidate_detail(
            detail,
            allocated_place_id="night_dn_100",
            reviewer="reviewer@example.com",
            approved_at=APPROVED_AT,
        )


def test_deterministic_pass_approval_is_traceable_and_auto_verified() -> None:
    detail = _detail()
    deterministic = ApprovedReplacement.from_candidate_detail(
        detail,
        allocated_place_id="cafe_dn_100",
        reviewer="canonical-distinct-gate-v1",
        approved_at=APPROVED_AT,
        approval_method=ReplacementApprovalMethod.DETERMINISTIC,
    )
    human = ApprovedReplacement.from_candidate_detail(
        detail,
        allocated_place_id="cafe_dn_100",
        reviewer="canonical-distinct-gate-v1",
        approved_at=APPROVED_AT,
    )

    record = materialize_master_record(deterministic)

    assert deterministic.approval_method is ReplacementApprovalMethod.DETERMINISTIC
    assert deterministic.approval_hash != human.approval_hash
    assert deterministic.approval_id != human.approval_id
    assert record.verification_status.value == "auto_verified"
    assert (
        record.provenance.approval_method
        is ReplacementApprovalMethod.DETERMINISTIC
    )


def test_deterministic_approval_does_not_bypass_review() -> None:
    with pytest.raises(ValueError, match="PASS"):
        ApprovedReplacement.from_candidate_detail(
            _detail(disposition=CandidateDisposition.REVIEW),
            allocated_place_id="cafe_dn_100",
            reviewer="canonical-distinct-gate-v1",
            approved_at=APPROVED_AT,
            approval_method=ReplacementApprovalMethod.DETERMINISTIC,
        )


@pytest.mark.parametrize(
    ("detail", "allocated_place_id", "message"),
    [
        (
            _detail(disposition=CandidateDisposition.REVIEW),
            "cafe_dn_100",
            "PASS",
        ),
        (
            _detail(
                token="0x314219caaa000002:0x1000000000000002",
                url_token=GOOGLE_TOKEN,
            ),
            "cafe_dn_100",
            "stable Google token",
        ),
        (
            _detail(allocated_location_source="verified-master-data"),
            "cafe_dn_100",
            "Google Maps sourced coordinates",
        ),
        (
            _detail(),
            "rest_dn_100",
            "entity/city slot",
        ),
        (
            _detail(),
            "cafe_dn_002",
            "never be reused",
        ),
    ],
)
def test_approval_rejects_ineligible_detail_or_allocated_identity(
    detail: CandidateDetailStage,
    allocated_place_id: str,
    message: str,
) -> None:
    with pytest.raises((ValueError, ReplacementApprovalError), match=message):
        ApprovedReplacement.from_candidate_detail(
            detail,
            allocated_place_id=allocated_place_id,
            reviewer="reviewer@example.com",
            approved_at=APPROVED_AT,
        )


def test_writer_is_deterministic_and_rejects_duplicate_ids_or_tokens(
    tmp_path: Path,
) -> None:
    writer = ApprovedReplacementWriter(tmp_path / "approved")
    first = _approval()

    path = writer.write(first)
    first_bytes = path.read_bytes()
    assert writer.write(first) == path
    assert path.read_bytes() == first_bytes
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["provenance"]["replacement_of"] == "cafe_dn_002"
    assert load_approved_replacements(tmp_path / "approved") == [
        materialize_master_record(first)
    ]
    slots = load_approved_replacement_slots(tmp_path / "approved")
    assert [item.model_dump(mode="json") for item in slots] == [
        {
            "legacy_place_id": "cafe_dn_100",
            "city_id": "city_da_nang",
            "primary_type": "cafe",
            "secondary_types": [],
            "tags": ["canonical_replacement"],
        }
    ]

    duplicate_token = _approval(
        candidate_key="candidate-other",
        retired_place_id="cafe_dn_003",
        allocated_place_id="cafe_dn_101",
    )
    with pytest.raises(ReplacementWriteConflictError, match="external token"):
        writer.write(duplicate_token)

    duplicate_id = _approval(
        candidate_key="candidate-third",
        retired_place_id="cafe_dn_004",
        token="0x314219caaa000003:0x1000000000000003",
        allocated_place_id="cafe_dn_100",
    )
    with pytest.raises(ReplacementWriteConflictError, match="place ID"):
        writer.write(duplicate_id)


def test_writer_rejects_duplicate_ids_and_tokens_before_partial_batch_write(
    tmp_path: Path,
) -> None:
    writer = ApprovedReplacementWriter(tmp_path / "approved")
    first = _approval()
    conflicting = _approval(
        candidate_key="candidate-other",
        retired_place_id="cafe_dn_003",
        token="0x314219caaa000003:0x1000000000000003",
        allocated_place_id="cafe_dn_100",
    )

    with pytest.raises(ReplacementWriteConflictError, match="batch"):
        writer.write_many([first, conflicting])

    assert not writer.records_root.exists()

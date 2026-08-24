from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from nextrip_pipeline.canonical.invalidation import (
    ApprovedCanonicalInvalidation,
    CanonicalInvalidationInputError,
    CanonicalInvalidationReason,
    CanonicalInvalidationWriteConflictError,
    CanonicalInvalidationWriter,
    approve_canonical_invalidation,
    load_approved_invalidations,
)
from nextrip_pipeline.canonical.models import (
    DuplicateIdentityDecision,
    LegacyPlaceSlot,
    VacancyReplacementDecision,
    VacancyStatus,
)
from nextrip_pipeline.canonical.resolver import (
    CanonicalIdentityError,
    CanonicalIdentityResolver,
    build_canonical_identity_manifest,
)
from nextrip_pipeline.cli import main
from nextrip_pipeline.schemas import (
    BusinessStatus,
    DailyOpeningStatus,
    EntityType,
    GoogleMapsPlaceObservation,
    OpeningStatusObservation,
    VerificationStatus,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 22, 8, tzinfo=UTC)


def _slots() -> list[LegacyPlaceSlot]:
    return [
        LegacyPlaceSlot(
            legacy_place_id="cafe_dn_001",
            city_id="city_da_nang",
            primary_type=EntityType.CAFE,
            tags=["instagrammable"],
        ),
        LegacyPlaceSlot(
            legacy_place_id="night_dn_001",
            city_id="city_da_nang",
            primary_type=EntityType.NIGHTLIFE,
            tags=["late_night_cafe"],
        ),
        LegacyPlaceSlot(
            legacy_place_id="cafe_dn_100",
            city_id="city_da_nang",
            primary_type=EntityType.CAFE,
        ),
    ]


def _duplicate() -> DuplicateIdentityDecision:
    return DuplicateIdentityDecision(
        keeper_legacy_place_id="cafe_dn_001",
        duplicate_legacy_place_ids=["night_dn_001"],
        reason="reviewed physical duplicate",
    )


def _initial_manifest():
    return build_canonical_identity_manifest(
        _slots(),
        duplicate_decisions=[_duplicate()],
        replacement_decisions=[
            VacancyReplacementDecision(
                retired_place_id="night_dn_001",
                replacement_place_id="cafe_dn_100",
            )
        ],
        generated_at=NOW,
    )


def _observation(
    *,
    category: str = "Advertising agency",
) -> GoogleMapsPlaceObservation:
    return GoogleMapsPlaceObservation(
        observation_id="google-observation-ador",
        run_id="google-run-ador",
        place_id="cafe_dn_001",
        source_record_id="google-source-record-ador",
        source_id="google-maps-web",
        source_url="https://www.google.com/maps/place/ADOR",
        name="ADOR",
        category=category,
        address="80A Le Dinh Duong, Da Nang",
        phone="+84 979 723 464",
        website_url="https://ador.vn/",
        location=None,
        business_status=BusinessStatus.ACTIVE,
        opening=OpeningStatusObservation(
            observation_id="google-observation-ador:opening",
            run_id="google-run-ador",
            place_id="cafe_dn_001",
            source_record_ids=["google-source-record-ador"],
            local_date=date(2026, 8, 22),
            timezone="Asia/Ho_Chi_Minh",
            status=DailyOpeningStatus.UNKNOWN,
            observed_at=NOW,
            verification_status=VerificationStatus.PENDING_REVIEW,
        ),
        observed_at=NOW,
        verification_status=VerificationStatus.PENDING_REVIEW,
    )


def _identity_source(
    tmp_path: Path,
    *,
    place_id: str = "cafe_dn_001",
    name: str = "ADOR",
    address: str = "80A Le Dinh Duong, Da Nang",
) -> Path:
    path = tmp_path / f"master-{place_id}.json"
    path.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "id": place_id,
                        "name": name,
                        "address": address,
                        "coordinates": {"lat": 16.0617, "lng": 108.2195},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _approval(tmp_path: Path) -> ApprovedCanonicalInvalidation:
    observation_path = tmp_path / "ador-observation.json"
    observation_path.write_text(
        _observation().model_dump_json(indent=2),
        encoding="utf-8",
    )
    return approve_canonical_invalidation(
        _initial_manifest(),
        place_id="cafe_dn_001",
        observation_path=observation_path,
        reason=CanonicalInvalidationReason.ENTITY_TYPE_INELIGIBLE,
        reviewer="Oanhh",
        identity_source_path=_identity_source(tmp_path),
        approved_at=NOW + timedelta(hours=1),
    )


def test_invalidation_archives_group_and_opens_only_the_active_primary_slot(
    tmp_path: Path,
) -> None:
    initial = _initial_manifest()
    approval = _approval(tmp_path)

    rebuilt = build_canonical_identity_manifest(
        _slots(),
        duplicate_decisions=[_duplicate()],
        previous_manifest=initial,
        approved_invalidations=[approval],
        generated_at=NOW + timedelta(hours=2),
    )

    assert rebuilt.schema_version == "1.1.0"
    assert [item.canonical_place_id for item in rebuilt.identities] == [
        "cafe_dn_100"
    ]
    assert len(rebuilt.quarantined_identities) == 1
    quarantine = rebuilt.quarantined_identities[0]
    assert quarantine.identity.canonical_place_id == "cafe_dn_001"
    assert quarantine.identity.legacy_place_ids == [
        "cafe_dn_001",
        "night_dn_001",
    ]
    assert quarantine.invalidation_approval_id == approval.approval_id

    vacancy_by_source = {
        item.retired_place_id: item for item in rebuilt.vacancies
    }
    assert vacancy_by_source["night_dn_001"].status is VacancyStatus.FILLED
    assert (
        vacancy_by_source["night_dn_001"].replacement_place_id
        == "cafe_dn_100"
    )
    assert vacancy_by_source["cafe_dn_001"].status is VacancyStatus.VACANT
    assert vacancy_by_source["cafe_dn_001"].entity_type is EntityType.CAFE
    assert len(rebuilt.retired_place_ids) == 1

    cafe_quota = next(
        item
        for item in rebuilt.quotas
        if item.city_id == "city_da_nang"
        and item.entity_type is EntityType.CAFE
    )
    assert (
        cafe_quota.target_count,
        cafe_quota.active_count,
        cafe_quota.vacancy_count,
    ) == (2, 1, 1)

    resolver = CanonicalIdentityResolver(rebuilt)
    assert resolver.resolve("cafe_dn_001") is None
    assert resolver.resolve("night_dn_001") is None
    assert resolver.quarantine_for("cafe_dn_001") == "cafe_dn_001"
    assert resolver.quarantine_for("night_dn_001") == "cafe_dn_001"
    assert resolver.resolve("cafe_dn_100") == "cafe_dn_100"


def test_quarantine_is_carried_without_requiring_the_approval_again(
    tmp_path: Path,
) -> None:
    initial = _initial_manifest()
    invalidated = build_canonical_identity_manifest(
        _slots(),
        duplicate_decisions=[_duplicate()],
        previous_manifest=initial,
        approved_invalidations=[_approval(tmp_path)],
        generated_at=NOW + timedelta(hours=2),
    )

    rebuilt = build_canonical_identity_manifest(
        [_slots()[-1]],
        previous_manifest=invalidated,
        generated_at=NOW + timedelta(days=1),
    )

    assert rebuilt.manifest_hash == invalidated.manifest_hash
    assert rebuilt.quarantined_identities == invalidated.quarantined_identities
    assert rebuilt.vacancies == invalidated.vacancies


def test_quarantined_vacancy_fill_preserves_capacity_without_id_reuse(
    tmp_path: Path,
) -> None:
    invalidated = build_canonical_identity_manifest(
        _slots(),
        duplicate_decisions=[_duplicate()],
        previous_manifest=_initial_manifest(),
        approved_invalidations=[_approval(tmp_path)],
        generated_at=NOW + timedelta(hours=2),
    )
    replacement = LegacyPlaceSlot(
        legacy_place_id="cafe_dn_101",
        city_id="city_da_nang",
        primary_type=EntityType.CAFE,
    )

    filled = build_canonical_identity_manifest(
        [*_slots(), replacement],
        duplicate_decisions=[_duplicate()],
        replacement_decisions=[
            VacancyReplacementDecision(
                retired_place_id="cafe_dn_001",
                replacement_place_id="cafe_dn_101",
            )
        ],
        previous_manifest=invalidated,
        generated_at=NOW + timedelta(hours=3),
    )

    vacancy = next(
        item
        for item in filled.vacancies
        if item.retired_place_id == "cafe_dn_001"
    )
    assert vacancy.status is VacancyStatus.FILLED
    assert vacancy.replacement_place_id == "cafe_dn_101"
    assert {item.canonical_place_id for item in filled.identities} == {
        "cafe_dn_100",
        "cafe_dn_101",
    }
    assert "cafe_dn_001" not in {
        item.canonical_place_id for item in filled.identities
    }
    assert sum(item.target_count for item in invalidated.quotas) == 2
    assert sum(item.target_count for item in filled.quotas) == 2
    assert sum(item.active_count for item in filled.quotas) == 2
    assert sum(item.vacancy_count for item in filled.quotas) == 0


def test_replacement_cli_authorizes_and_blocks_all_quarantined_legacy_ids(
    tmp_path: Path,
    monkeypatch,
) -> None:
    invalidated = build_canonical_identity_manifest(
        _slots(),
        duplicate_decisions=[_duplicate()],
        previous_manifest=_initial_manifest(),
        approved_invalidations=[_approval(tmp_path)],
        generated_at=NOW + timedelta(hours=2),
    )
    captured_allocator: dict[str, object] = {}

    monkeypatch.setattr(
        "nextrip_pipeline.cli.read_canonical_identity_manifest",
        lambda _: invalidated,
    )
    monkeypatch.setattr(
        "nextrip_pipeline.cli.load_verified_master",
        lambda _: SimpleNamespace(slots=_slots()),
    )
    monkeypatch.setattr(
        "nextrip_pipeline.cli.load_approved_replacements",
        lambda _: [],
    )
    monkeypatch.setattr(
        "nextrip_pipeline.cli.build_existing_identity_projection",
        lambda *args, **kwargs: SimpleNamespace(
            identities=[],
            projection_hash="p" * 64,
        ),
    )
    monkeypatch.setattr(
        "nextrip_pipeline.cli.PlaywrightBrowserClient",
        lambda *args, **kwargs: object(),
    )

    def capture_allocator(**kwargs):
        captured_allocator.update(kwargs)
        return object()

    class FakeRunner:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def run(self, *args, **kwargs):
            return (
                SimpleNamespace(
                    vacant_count=1,
                    unique_search_count=0,
                    proposed_count=0,
                    unresolved_count=1,
                    failed_count=0,
                    results=[],
                ),
                tmp_path / "summary.json",
            )

    monkeypatch.setattr(
        "nextrip_pipeline.cli.MonotonicPlaceIdAllocator",
        capture_allocator,
    )
    monkeypatch.setattr(
        "nextrip_pipeline.cli.CanonicalReplacementProposalBatchRunner",
        FakeRunner,
    )
    vacancy = next(
        item
        for item in invalidated.vacancies
        if item.retired_place_id == "cafe_dn_001"
    )

    exit_code = main(
        [
            "propose-canonical-replacements",
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--master-dir",
            str(tmp_path / "master"),
            "--summary-dir",
            str(tmp_path / "summaries"),
            "--vacancy-id",
            vacancy.vacancy_id,
        ]
    )

    assert exit_code == 0
    assert captured_allocator["retired_ids"] == {"night_dn_001"}
    assert captured_allocator["quarantined_ids"] == {
        "cafe_dn_001",
        "night_dn_001",
    }


def test_invalidation_gate_rejects_compatible_or_ambiguous_category(
    tmp_path: Path,
) -> None:
    for category, message in [
        ("Coffee shop", "compatible"),
        ("Business", "ambiguous"),
    ]:
        path = tmp_path / f"{category}.json"
        path.write_text(
            _observation(category=category).model_dump_json(),
            encoding="utf-8",
        )
        with pytest.raises(CanonicalInvalidationInputError, match=message):
            approve_canonical_invalidation(
                _initial_manifest(),
                place_id="cafe_dn_001",
                observation_path=path,
                reason=CanonicalInvalidationReason.ENTITY_TYPE_INELIGIBLE,
                reviewer="Oanhh",
                approved_at=NOW,
            )


def test_invalidation_gate_rejects_unknown_or_mistyped_category(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unknown-category.json"
    path.write_text(
        _observation(category="Advertising agncy").model_dump_json(),
        encoding="utf-8",
    )

    with pytest.raises(CanonicalInvalidationInputError, match="unknown or mistyped"):
        approve_canonical_invalidation(
            _initial_manifest(),
            place_id="cafe_dn_001",
            observation_path=path,
            identity_source_path=_identity_source(tmp_path),
            reason=CanonicalInvalidationReason.ENTITY_TYPE_INELIGIBLE,
            reviewer="Oanhh",
            approved_at=NOW,
        )


def test_invalidation_requires_strong_physical_identity_corroboration(
    tmp_path: Path,
) -> None:
    path = tmp_path / "observation.json"
    path.write_text(_observation().model_dump_json(), encoding="utf-8")

    with pytest.raises(CanonicalInvalidationInputError, match="pinned identity source"):
        approve_canonical_invalidation(
            _initial_manifest(),
            place_id="cafe_dn_001",
            observation_path=path,
            reason=CanonicalInvalidationReason.ENTITY_TYPE_INELIGIBLE,
            reviewer="Oanhh",
            approved_at=NOW,
        )
    with pytest.raises(CanonicalInvalidationInputError, match="strongly corroborated"):
        approve_canonical_invalidation(
            _initial_manifest(),
            place_id="cafe_dn_001",
            observation_path=path,
            identity_source_path=_identity_source(
                tmp_path,
                name="Another Business",
                address="99 Different Street",
            ),
            reason=CanonicalInvalidationReason.ENTITY_TYPE_INELIGIBLE,
            reviewer="Oanhh",
            approved_at=NOW,
        )

    approval = approve_canonical_invalidation(
        _initial_manifest(),
        place_id="cafe_dn_001",
        observation_path=path,
        identity_source_path=_identity_source(tmp_path),
        reason=CanonicalInvalidationReason.ENTITY_TYPE_INELIGIBLE,
        reviewer="Oanhh",
        approved_at=NOW,
    )
    assert approval.schema_version == "1.1.0"
    corroboration = approval.evidence[0].identity_corroboration
    assert corroboration is not None
    assert corroboration.matched_signals == ["address", "name"]


def test_invalidation_writer_is_idempotent_and_detects_tampering(
    tmp_path: Path,
) -> None:
    approval = _approval(tmp_path)
    output = tmp_path / "invalidations"
    writer = CanonicalInvalidationWriter(output)

    path = writer.write(approval)
    assert writer.write(approval) == path
    assert load_approved_invalidations(output) == [approval]

    tampered = approval.model_dump(mode="json")
    tampered["approval_hash"] = "0" * 64
    path.write_text(
        json.dumps(tampered, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(
        CanonicalInvalidationWriteConflictError,
        match="cannot validate existing invalidation",
    ):
        writer.write(approval)

    with pytest.raises(ValidationError, match="approval_hash"):
        ApprovedCanonicalInvalidation.model_validate(tampered)


def test_approved_replacement_cannot_be_invalidated_without_retraction(
    tmp_path: Path,
) -> None:
    initial = _initial_manifest()
    replacement_observation = _observation().model_copy(
        update={"place_id": "cafe_dn_100"},
        deep=True,
    )
    path = tmp_path / "replacement-observation.json"
    path.write_text(replacement_observation.model_dump_json(), encoding="utf-8")
    approval = approve_canonical_invalidation(
        initial,
        place_id="cafe_dn_100",
        observation_path=path,
        reason=CanonicalInvalidationReason.ENTITY_TYPE_INELIGIBLE,
        reviewer="Oanhh",
        identity_source_path=_identity_source(
            tmp_path,
            place_id="cafe_dn_100",
        ),
        approved_at=NOW + timedelta(hours=1),
    )

    with pytest.raises(CanonicalIdentityError, match="retraction workflow"):
        build_canonical_identity_manifest(
            _slots(),
            duplicate_decisions=[_duplicate()],
            previous_manifest=initial,
            approved_invalidations=[approval],
            generated_at=NOW + timedelta(hours=2),
        )

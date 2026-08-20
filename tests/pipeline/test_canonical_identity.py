from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from nextrip_pipeline.canonical import (
    CanonicalIdentityError,
    CanonicalIdentityManifest,
    CanonicalIdentityManifestWriter,
    CanonicalIdentityResolver,
    DuplicateIdentityDecision,
    LegacyPlaceSlot,
    UnknownPlaceIdentityError,
    VacancyReplacementDecision,
    VacancyStatus,
    build_canonical_identity_manifest,
    read_canonical_identity_manifest,
)
from nextrip_pipeline.schemas import EntityType


UTC = timezone.utc
GENERATED_AT = datetime(2026, 8, 20, 9, tzinfo=UTC)


def _slots() -> list[LegacyPlaceSlot]:
    return [
        LegacyPlaceSlot(
            legacy_place_id="cafe_qn_001",
            city_id="city_quy_nhon",
            primary_type=EntityType.CAFE,
            secondary_types=[EntityType.RESTAURANT],
            tags=[" Pet Friendly ", "WiFi"],
        ),
        LegacyPlaceSlot(
            legacy_place_id="night_qn_032",
            city_id="city_quy_nhon",
            primary_type=EntityType.NIGHTLIFE,
            tags=["wifi", "Live Music"],
        ),
        LegacyPlaceSlot(
            legacy_place_id="rest_qn_001",
            city_id="city_quy_nhon",
            primary_type=EntityType.RESTAURANT,
            tags=["local food"],
        ),
    ]


def _decision(
    *, keeper: str = "cafe_qn_001", duplicate: str = "night_qn_032"
) -> DuplicateIdentityDecision:
    return DuplicateIdentityDecision(
        keeper_legacy_place_id=keeper,
        duplicate_legacy_place_ids=[duplicate],
        reason="human_verified_duplicate",
    )


def _manifest(*, generated_at: datetime = GENERATED_AT):
    return build_canonical_identity_manifest(
        _slots(),
        duplicate_decisions=[_decision()],
        generated_at=generated_at,
    )


def _replacement_slot(
    place_id: str = "night_qn_097",
    *,
    city_id: str = "city_quy_nhon",
    entity_type: EntityType = EntityType.NIGHTLIFE,
) -> LegacyPlaceSlot:
    return LegacyPlaceSlot(
        legacy_place_id=place_id,
        city_id=city_id,
        primary_type=entity_type,
        tags=["replacement"],
    )


def _replacement_decision(
    place_id: str = "night_qn_097",
) -> VacancyReplacementDecision:
    return VacancyReplacementDecision(
        retired_place_id="night_qn_032",
        replacement_place_id=place_id,
        reason="human_verified_distinct_replacement",
    )


def test_duplicate_resolution_produces_one_place_alias_tombstone_and_vacancy() -> None:
    manifest = _manifest()

    assert [item.canonical_place_id for item in manifest.identities] == [
        "cafe_qn_001",
        "rest_qn_001",
    ]
    merged = manifest.identities[0]
    assert merged.active_legacy_place_id == "cafe_qn_001"
    assert merged.legacy_place_ids == ["cafe_qn_001", "night_qn_032"]
    assert merged.primary_type is EntityType.CAFE
    assert merged.secondary_types == [
        EntityType.NIGHTLIFE,
        EntityType.RESTAURANT,
    ]
    assert merged.place_types == [
        EntityType.CAFE,
        EntityType.NIGHTLIFE,
        EntityType.RESTAURANT,
    ]
    assert merged.tags == ["live music", "pet friendly", "wifi"]

    retirement = manifest.retired_place_ids[0]
    assert retirement.retired_place_id == "night_qn_032"
    assert retirement.canonical_place_id == "cafe_qn_001"
    assert retirement.entity_type is EntityType.NIGHTLIFE
    assert manifest.vacancies[0].retired_place_id == "night_qn_032"
    assert manifest.vacancies[0].entity_type is EntityType.NIGHTLIFE

    quotas = {
        (item.city_id, item.entity_type): (
            item.target_count,
            item.active_count,
            item.vacancy_count,
        )
        for item in manifest.quotas
    }
    assert quotas[("city_quy_nhon", EntityType.CAFE)] == (1, 1, 0)
    assert quotas[("city_quy_nhon", EntityType.NIGHTLIFE)] == (1, 0, 1)
    assert quotas[("city_quy_nhon", EntityType.RESTAURANT)] == (1, 1, 0)


def test_resolver_accepts_active_and_retired_ids_but_not_unknown_ids() -> None:
    resolver = CanonicalIdentityResolver(_manifest())

    assert resolver.resolve("cafe_qn_001") == "cafe_qn_001"
    assert resolver.resolve("night_qn_032") == "cafe_qn_001"
    assert resolver.identity_for("night_qn_032").canonical_place_id == "cafe_qn_001"
    assert resolver.is_retired("night_qn_032") is True
    assert resolver.is_retired("cafe_qn_001") is False
    assert resolver.resolve("unknown_qn_999") is None
    with pytest.raises(UnknownPlaceIdentityError):
        resolver.require("unknown_qn_999")


def test_replacement_fills_vacancy_without_changing_retired_alias_or_quota() -> None:
    manifest = build_canonical_identity_manifest(
        [*_slots(), _replacement_slot()],
        duplicate_decisions=[_decision()],
        replacement_decisions=[_replacement_decision()],
        generated_at=GENERATED_AT,
    )

    vacancy = manifest.vacancies[0]
    assert vacancy.status is VacancyStatus.FILLED
    assert vacancy.replacement_place_id == "night_qn_097"
    assert len(manifest.vacancies) == 1

    resolver = CanonicalIdentityResolver(manifest)
    assert resolver.resolve("night_qn_032") == "cafe_qn_001"
    assert resolver.resolve("night_qn_097") == "night_qn_097"
    replacement = resolver.identity_for("night_qn_097")
    assert replacement is not None
    assert replacement.legacy_place_ids == ["night_qn_097"]

    nightlife_quota = next(
        item
        for item in manifest.quotas
        if item.entity_type is EntityType.NIGHTLIFE
    )
    assert (
        nightlife_quota.target_count,
        nightlife_quota.active_count,
        nightlife_quota.vacancy_count,
    ) == (1, 1, 0)


def test_filled_assignment_persists_and_cannot_be_changed_or_reopened() -> None:
    previous = build_canonical_identity_manifest(
        [*_slots(), _replacement_slot()],
        duplicate_decisions=[_decision()],
        replacement_decisions=[_replacement_decision()],
        generated_at=GENERATED_AT,
    )
    active_slots = [
        item for item in [*_slots(), _replacement_slot()]
        if item.legacy_place_id != "night_qn_032"
    ]

    rebuilt = build_canonical_identity_manifest(
        active_slots,
        previous_manifest=previous,
        generated_at=GENERATED_AT + timedelta(days=1),
    )
    assert rebuilt.vacancies == previous.vacancies
    assert rebuilt.manifest_hash == previous.manifest_hash

    changed_slots = [*active_slots, _replacement_slot("night_qn_098")]
    with pytest.raises(
        CanonicalIdentityError,
        match="filled vacancy replacement cannot be changed",
    ):
        build_canonical_identity_manifest(
            changed_slots,
            replacement_decisions=[_replacement_decision("night_qn_098")],
            previous_manifest=previous,
            generated_at=GENERATED_AT + timedelta(days=1),
        )


@pytest.mark.parametrize(
    ("replacement_slot", "message"),
    [
        (
            _replacement_slot(
                "night_dn_101",
                city_id="city_da_nang",
            ),
            "preserve the vacancy entity/city slot",
        ),
        (
            _replacement_slot(
                "rest_qn_107",
                entity_type=EntityType.RESTAURANT,
            ),
            "preserve the vacancy entity/city slot",
        ),
    ],
)
def test_replacement_must_preserve_city_and_primary_entity_slot(
    replacement_slot: LegacyPlaceSlot,
    message: str,
) -> None:
    with pytest.raises(CanonicalIdentityError, match=message):
        build_canonical_identity_manifest(
            [*_slots(), replacement_slot],
            duplicate_decisions=[_decision()],
            replacement_decisions=[
                _replacement_decision(replacement_slot.legacy_place_id)
            ],
            generated_at=GENERATED_AT,
        )


def test_replacement_assignments_are_one_to_one() -> None:
    second_duplicate = LegacyPlaceSlot(
        legacy_place_id="night_qn_033",
        city_id="city_quy_nhon",
        primary_type=EntityType.NIGHTLIFE,
    )
    duplicate_decision = DuplicateIdentityDecision(
        keeper_legacy_place_id="cafe_qn_001",
        duplicate_legacy_place_ids=["night_qn_032", "night_qn_033"],
    )
    with pytest.raises(
        CanonicalIdentityError,
        match="one vacancy is allowed per replacement place",
    ):
        build_canonical_identity_manifest(
            [*_slots(), second_duplicate, _replacement_slot()],
            duplicate_decisions=[duplicate_decision],
            replacement_decisions=[
                _replacement_decision(),
                VacancyReplacementDecision(
                    retired_place_id="night_qn_033",
                    replacement_place_id="night_qn_097",
                ),
            ],
            generated_at=GENERATED_AT,
        )


def test_existing_active_identity_cannot_be_repurposed_as_a_new_vacancy_fill() -> None:
    previous = build_canonical_identity_manifest(
        [*_slots(), _replacement_slot()],
        duplicate_decisions=[_decision()],
        generated_at=GENERATED_AT,
    )
    active_slots = [
        item for item in [*_slots(), _replacement_slot()]
        if item.legacy_place_id != "night_qn_032"
    ]

    with pytest.raises(
        CanonicalIdentityError,
        match="requires a newly allocated place ID",
    ):
        build_canonical_identity_manifest(
            active_slots,
            replacement_decisions=[_replacement_decision()],
            previous_manifest=previous,
            generated_at=GENERATED_AT + timedelta(days=1),
        )


def test_manifest_hash_ids_and_order_are_stable_across_retries() -> None:
    first = _manifest()
    second = build_canonical_identity_manifest(
        list(reversed(_slots())),
        duplicate_decisions=[_decision()],
        generated_at=GENERATED_AT + timedelta(days=1),
    )

    assert second.generated_at != first.generated_at
    assert second.manifest_hash == first.manifest_hash
    assert second.manifest_id == first.manifest_id
    assert second.identities == first.identities
    assert second.retired_place_ids == first.retired_place_ids
    assert second.vacancies == first.vacancies
    assert second.quotas == first.quotas


def test_previous_tombstone_survives_master_cleanup_without_hash_churn() -> None:
    previous = _manifest()
    active_only = [
        item for item in _slots() if item.legacy_place_id != "night_qn_032"
    ]

    rebuilt = build_canonical_identity_manifest(
        active_only,
        previous_manifest=previous,
        generated_at=GENERATED_AT + timedelta(days=2),
    )

    assert rebuilt.manifest_hash == previous.manifest_hash
    resolver = CanonicalIdentityResolver(rebuilt)
    assert resolver.resolve("night_qn_032") == "cafe_qn_001"
    assert resolver.is_retired("night_qn_032")
    assert rebuilt.vacancies == previous.vacancies


def test_retired_id_cannot_become_singleton_keeper_or_another_places_alias() -> None:
    previous = _manifest()

    with pytest.raises(
        CanonicalIdentityError,
        match="retired legacy ID cannot become an active singleton",
    ):
        build_canonical_identity_manifest(
            _slots(),
            previous_manifest=previous,
            generated_at=GENERATED_AT + timedelta(days=1),
        )

    with pytest.raises(
        CanonicalIdentityError,
        match="retired legacy ID cannot be reused as keeper",
    ):
        build_canonical_identity_manifest(
            _slots(),
            duplicate_decisions=[
                _decision(keeper="night_qn_032", duplicate="cafe_qn_001")
            ],
            previous_manifest=previous,
            generated_at=GENERATED_AT + timedelta(days=1),
        )

    with pytest.raises(
        CanonicalIdentityError,
        match="cannot be reassigned to another canonical place",
    ):
        build_canonical_identity_manifest(
            _slots(),
            duplicate_decisions=[
                _decision(keeper="rest_qn_001", duplicate="night_qn_032")
            ],
            previous_manifest=previous,
            generated_at=GENERATED_AT + timedelta(days=1),
        )


def test_resolver_rejects_cross_city_and_overlapping_duplicate_decisions() -> None:
    slots = [
        *_slots(),
        LegacyPlaceSlot(
            legacy_place_id="cafe_dn_001",
            city_id="city_da_nang",
            primary_type=EntityType.CAFE,
        ),
    ]
    with pytest.raises(CanonicalIdentityError, match="same city"):
        build_canonical_identity_manifest(
            slots,
            duplicate_decisions=[
                _decision(duplicate="cafe_dn_001"),
            ],
            generated_at=GENERATED_AT,
        )

    with pytest.raises(CanonicalIdentityError, match="multiple duplicate decisions"):
        build_canonical_identity_manifest(
            _slots(),
            duplicate_decisions=[
                _decision(),
                _decision(keeper="rest_qn_001", duplicate="night_qn_032"),
            ],
            generated_at=GENERATED_AT,
        )


def test_manifest_writer_round_trips_and_skips_semantic_noop(tmp_path) -> None:
    destination = tmp_path / "canonical-identity.json"
    writer = CanonicalIdentityManifestWriter(destination)
    first = _manifest()
    retry = _manifest(generated_at=GENERATED_AT + timedelta(hours=5))

    assert writer.write(first) == destination
    original_bytes = destination.read_bytes()
    assert writer.write(retry) == destination
    assert destination.read_bytes() == original_bytes
    assert writer.read() == first
    assert read_canonical_identity_manifest(destination) == first


def test_manifest_rejects_tampered_hash_and_reused_retired_id() -> None:
    manifest = _manifest()
    tampered = manifest.model_dump(mode="json")
    tampered["manifest_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="manifest_hash"):
        CanonicalIdentityManifest.model_validate(tampered)

    reused = manifest.model_dump(mode="json")
    reused["identities"][0]["canonical_place_id"] = "night_qn_032"
    identity_content = {
        key: reused["identities"][0][key]
        for key in (
            "canonical_place_id",
            "active_legacy_place_id",
            "legacy_place_ids",
            "city_id",
            "primary_type",
            "secondary_types",
            "tags",
        )
    }
    # Ensure validation reaches the retired-ID invariant instead of stopping at
    # the deliberately changed identity hash.
    from nextrip_pipeline.canonical import stable_sha256

    reused["identities"][0]["identity_hash"] = stable_sha256(identity_content)
    with pytest.raises(ValidationError, match="never be reused as active"):
        CanonicalIdentityManifest.model_validate(reused)


def test_manifest_json_is_standard_json_not_a_python_specific_encoding() -> None:
    payload = json.loads(_manifest().model_dump_json())

    assert payload["schema_version"] == "1.0.0"
    assert payload["identities"][0]["primary_type"] == "cafe"
    assert payload["vacancies"][0]["status"] == "vacant"

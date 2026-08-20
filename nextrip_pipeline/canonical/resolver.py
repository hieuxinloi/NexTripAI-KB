from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timezone

from .models import (
    CanonicalIdentityManifest,
    CanonicalPlaceIdentity,
    DuplicateIdentityDecision,
    EntityCityQuota,
    EntityCityVacancy,
    LegacyPlaceSlot,
    RetiredPlaceId,
    VacancyReplacementDecision,
    VacancyStatus,
    canonical_identity_payload,
    canonical_manifest_payload,
    stable_identifier,
    stable_sha256,
)


class CanonicalIdentityError(ValueError):
    """Raised when an identity snapshot would violate a permanent invariant."""


class UnknownPlaceIdentityError(KeyError):
    """Raised when a caller requires an ID not present in the manifest."""


class CanonicalIdentityResolver:
    """Resolve active or retired legacy IDs without consulting mutable master data."""

    def __init__(self, manifest: CanonicalIdentityManifest) -> None:
        self.manifest = manifest
        self._by_canonical_id = {
            item.canonical_place_id: item for item in manifest.identities
        }
        self._owner_by_id = {
            legacy_id: item.canonical_place_id
            for item in manifest.identities
            for legacy_id in item.legacy_place_ids
        }
        self._retired_ids = {
            item.retired_place_id for item in manifest.retired_place_ids
        }

    def resolve(self, place_id: str) -> str | None:
        """Return the canonical ID for an active ID or a retired legacy alias."""

        return self._owner_by_id.get(place_id)

    def require(self, place_id: str) -> str:
        canonical_id = self.resolve(place_id)
        if canonical_id is None:
            raise UnknownPlaceIdentityError(place_id)
        return canonical_id

    def identity_for(self, place_id: str) -> CanonicalPlaceIdentity | None:
        canonical_id = self.resolve(place_id)
        return self._by_canonical_id.get(canonical_id) if canonical_id else None

    def is_retired(self, place_id: str) -> bool:
        return place_id in self._retired_ids

    @classmethod
    def build_manifest(
        cls,
        slots: Sequence[LegacyPlaceSlot],
        *,
        duplicate_decisions: Sequence[DuplicateIdentityDecision] = (),
        replacement_decisions: Sequence[VacancyReplacementDecision] = (),
        previous_manifest: CanonicalIdentityManifest | None = None,
        generated_at: datetime | None = None,
    ) -> CanonicalIdentityManifest:
        return build_canonical_identity_manifest(
            slots,
            duplicate_decisions=duplicate_decisions,
            replacement_decisions=replacement_decisions,
            previous_manifest=previous_manifest,
            generated_at=generated_at,
        )


def build_canonical_identity_manifest(
    slots: Sequence[LegacyPlaceSlot],
    *,
    duplicate_decisions: Sequence[DuplicateIdentityDecision] = (),
    replacement_decisions: Sequence[VacancyReplacementDecision] = (),
    previous_manifest: CanonicalIdentityManifest | None = None,
    generated_at: datetime | None = None,
) -> CanonicalIdentityManifest:
    """Build a full canonical identity snapshot from explicit duplicate decisions.

    ``slots`` is a full active-source snapshot. Previous active identities are
    retained if a caller accidentally omits them, because silently forgetting
    an identity would make its ID reusable. Previous tombstones are permanent.
    No fuzzy duplicate inference occurs here; a reviewed decision is required.
    """

    generated_at = generated_at or datetime.now(timezone.utc)
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise CanonicalIdentityError("generated_at must be timezone-aware")

    slot_by_id: dict[str, LegacyPlaceSlot] = {}
    for slot in slots:
        if slot.legacy_place_id in slot_by_id:
            raise CanonicalIdentityError(
                f"duplicate legacy place slot: {slot.legacy_place_id}"
            )
        slot_by_id[slot.legacy_place_id] = slot
    supplied_slot_ids = set(slot_by_id)

    previous_retired = (
        {item.retired_place_id: item for item in previous_manifest.retired_place_ids}
        if previous_manifest
        else {}
    )
    if previous_manifest is not None:
        # Identity is independent of whether an entity is currently publishable.
        # Carry an omitted active identity rather than freeing its ID by accident.
        for identity in previous_manifest.identities:
            active_id = identity.active_legacy_place_id
            if active_id not in slot_by_id:
                slot_by_id[active_id] = LegacyPlaceSlot(
                    legacy_place_id=active_id,
                    city_id=identity.city_id,
                    primary_type=identity.primary_type,
                    secondary_types=identity.secondary_types,
                    tags=identity.tags,
                )

    groups: dict[str, set[str]] = {}
    decision_reason: dict[str, str] = {}
    mentioned: set[str] = set()
    for decision in duplicate_decisions:
        ids = {
            decision.keeper_legacy_place_id,
            *decision.duplicate_legacy_place_ids,
        }
        overlap = mentioned & ids
        if overlap:
            raise CanonicalIdentityError(
                "legacy IDs cannot appear in multiple duplicate decisions: "
                + ", ".join(sorted(overlap))
            )
        missing = ids - set(slot_by_id)
        if missing:
            raise CanonicalIdentityError(
                "duplicate decision references unknown legacy IDs: "
                + ", ".join(sorted(missing))
            )
        if decision.keeper_legacy_place_id in previous_retired:
            raise CanonicalIdentityError(
                "retired legacy ID cannot be reused as keeper: "
                f"{decision.keeper_legacy_place_id}"
            )
        cities = {slot_by_id[item].city_id for item in ids}
        if len(cities) != 1:
            raise CanonicalIdentityError(
                "duplicate identities must belong to the same city: "
                + ", ".join(sorted(ids))
            )
        groups[decision.keeper_legacy_place_id] = ids
        for retired_id in decision.duplicate_legacy_place_ids:
            decision_reason[retired_id] = decision.reason
        mentioned.update(ids)

    for legacy_id in sorted(set(slot_by_id) - mentioned):
        if legacy_id in previous_retired:
            raise CanonicalIdentityError(
                "retired legacy ID cannot become an active singleton: " + legacy_id
            )
        groups[legacy_id] = {legacy_id}

    owner_by_id = {
        legacy_id: keeper_id
        for keeper_id, member_ids in groups.items()
        for legacy_id in member_ids
    }

    identity_parts: dict[str, dict[str, object]] = {}
    retirement_parts: dict[str, dict[str, object]] = {}
    for keeper_id, member_ids in groups.items():
        keeper = slot_by_id[keeper_id]
        all_types = {
            item_type
            for member_id in member_ids
            for item_type in (
                slot_by_id[member_id].primary_type,
                *slot_by_id[member_id].secondary_types,
            )
        }
        tags = {
            tag
            for member_id in member_ids
            for tag in slot_by_id[member_id].tags
        }
        identity_parts[keeper_id] = {
            "active_id": keeper_id,
            "legacy_ids": set(member_ids),
            "city_id": keeper.city_id,
            "primary_type": keeper.primary_type,
            "all_types": all_types,
            "tags": tags,
        }
        for retired_id in member_ids - {keeper_id}:
            source = slot_by_id[retired_id]
            retirement_parts[retired_id] = {
                "canonical_id": keeper_id,
                "city_id": source.city_id,
                "entity_type": source.primary_type,
                "reason": decision_reason[retired_id],
            }

    if previous_manifest is not None:
        _merge_previous_manifest(
            previous_manifest=previous_manifest,
            owner_by_id=owner_by_id,
            identity_parts=identity_parts,
            retirement_parts=retirement_parts,
            previous_retired=previous_retired,
        )

    identities = [_build_identity(value) for value in identity_parts.values()]
    identities.sort(key=lambda item: item.canonical_place_id)

    retirements = [
        RetiredPlaceId(
            retirement_id=stable_identifier("retirement", retired_id),
            retired_place_id=retired_id,
            canonical_place_id=str(values["canonical_id"]),
            city_id=str(values["city_id"]),
            entity_type=values["entity_type"],
            reason=str(values["reason"]),
        )
        for retired_id, values in retirement_parts.items()
    ]
    retirements.sort(key=lambda item: item.retired_place_id)

    replacement_by_retired = _resolve_replacement_assignments(
        replacement_decisions=replacement_decisions,
        previous_manifest=previous_manifest,
        identities=identities,
        retirements=retirements,
        supplied_slot_ids=supplied_slot_ids,
    )

    vacancies = [
        EntityCityVacancy(
            vacancy_id=stable_identifier(
                "vacancy",
                item.city_id,
                item.entity_type.value,
                item.retired_place_id,
            ),
            retired_place_id=item.retired_place_id,
            city_id=item.city_id,
            entity_type=item.entity_type,
            status=(
                VacancyStatus.FILLED
                if item.retired_place_id in replacement_by_retired
                else VacancyStatus.VACANT
            ),
            replacement_place_id=replacement_by_retired.get(item.retired_place_id),
        )
        for item in retirements
    ]
    vacancies.sort(key=lambda item: (item.city_id, item.entity_type.value, item.vacancy_id))

    active_counts = Counter(
        (identity.city_id, identity.primary_type) for identity in identities
    )
    vacancy_counts = Counter(
        (vacancy.city_id, vacancy.entity_type)
        for vacancy in vacancies
        if vacancy.status is VacancyStatus.VACANT
    )
    quotas = [
        EntityCityQuota(
            city_id=city_id,
            entity_type=entity_type,
            active_count=active_counts[(city_id, entity_type)],
            vacancy_count=vacancy_counts[(city_id, entity_type)],
            target_count=(
                active_counts[(city_id, entity_type)]
                + vacancy_counts[(city_id, entity_type)]
            ),
        )
        for city_id, entity_type in sorted(
            set(active_counts) | set(vacancy_counts),
            key=lambda item: (item[0], item[1].value),
        )
    ]

    payload = canonical_manifest_payload(
        schema_version="1.0.0",
        identities=identities,
        retired_place_ids=retirements,
        vacancies=vacancies,
        quotas=quotas,
    )
    manifest_hash = stable_sha256(payload)
    return CanonicalIdentityManifest(
        manifest_id=f"canonical_identity_{manifest_hash[:20]}",
        manifest_hash=manifest_hash,
        generated_at=generated_at,
        identities=identities,
        retired_place_ids=retirements,
        vacancies=vacancies,
        quotas=quotas,
    )


def _resolve_replacement_assignments(
    *,
    replacement_decisions: Sequence[VacancyReplacementDecision],
    previous_manifest: CanonicalIdentityManifest | None,
    identities: Sequence[CanonicalPlaceIdentity],
    retirements: Sequence[RetiredPlaceId],
    supplied_slot_ids: set[str],
) -> dict[str, str]:
    """Validate new fills and retain every previously committed assignment."""

    retirement_by_id = {item.retired_place_id: item for item in retirements}
    identity_by_id = {item.canonical_place_id: item for item in identities}
    retired_ids = set(retirement_by_id)
    previous_identity_ids = (
        {
            legacy_id
            for identity in previous_manifest.identities
            for legacy_id in identity.legacy_place_ids
        }
        if previous_manifest is not None
        else set()
    )
    previous_assignments = (
        {
            vacancy.retired_place_id: vacancy.replacement_place_id
            for vacancy in previous_manifest.vacancies
            if vacancy.status is VacancyStatus.FILLED
        }
        if previous_manifest is not None
        else {}
    )
    assignments: dict[str, str] = {
        retired_id: replacement_id
        for retired_id, replacement_id in previous_assignments.items()
        if replacement_id is not None
    }

    decision_by_retired: dict[str, VacancyReplacementDecision] = {}
    decision_by_replacement: dict[str, VacancyReplacementDecision] = {}
    for decision in replacement_decisions:
        if decision.retired_place_id in decision_by_retired:
            raise CanonicalIdentityError(
                "one replacement decision is allowed per vacancy: "
                + decision.retired_place_id
            )
        if decision.replacement_place_id in decision_by_replacement:
            raise CanonicalIdentityError(
                "one vacancy is allowed per replacement place: "
                + decision.replacement_place_id
            )
        decision_by_retired[decision.retired_place_id] = decision
        decision_by_replacement[decision.replacement_place_id] = decision

    for retired_id, decision in decision_by_retired.items():
        if retired_id not in retirement_by_id:
            raise CanonicalIdentityError(
                "replacement decision references an unknown vacancy: " + retired_id
            )
        previous_replacement = assignments.get(retired_id)
        if (
            previous_replacement is not None
            and previous_replacement != decision.replacement_place_id
        ):
            raise CanonicalIdentityError(
                "a filled vacancy replacement cannot be changed: " + retired_id
            )
        if (
            previous_replacement is None
            and decision.replacement_place_id in previous_identity_ids
        ):
            raise CanonicalIdentityError(
                "a new vacancy fill requires a newly allocated place ID: "
                + decision.replacement_place_id
            )
        if (
            previous_replacement is None
            and decision.replacement_place_id not in supplied_slot_ids
        ):
            raise CanonicalIdentityError(
                "replacement place must be supplied as a new active slot: "
                + decision.replacement_place_id
            )
        assignments[retired_id] = decision.replacement_place_id

    replacement_ids = list(assignments.values())
    if len(replacement_ids) != len(set(replacement_ids)):
        raise CanonicalIdentityError("one replacement place can fill only one vacancy")

    for retired_id, replacement_id in assignments.items():
        retirement = retirement_by_id.get(retired_id)
        if retirement is None:
            raise CanonicalIdentityError(
                "historical filled vacancy no longer exists: " + retired_id
            )
        if replacement_id in retired_ids:
            raise CanonicalIdentityError(
                "retired place ID cannot be reused as a replacement: "
                + replacement_id
            )
        replacement = identity_by_id.get(replacement_id)
        if replacement is None:
            raise CanonicalIdentityError(
                "replacement must remain an active canonical identity: "
                + replacement_id
            )
        if replacement.legacy_place_ids != [replacement_id]:
            raise CanonicalIdentityError(
                "replacement must remain a distinct singleton: " + replacement_id
            )
        if (replacement.city_id, replacement.primary_type) != (
            retirement.city_id,
            retirement.entity_type,
        ):
            raise CanonicalIdentityError(
                "replacement must preserve the vacancy entity/city slot: "
                + replacement_id
            )

    return assignments


def _merge_previous_manifest(
    *,
    previous_manifest: CanonicalIdentityManifest,
    owner_by_id: dict[str, str],
    identity_parts: dict[str, dict[str, object]],
    retirement_parts: dict[str, dict[str, object]],
    previous_retired: dict[str, RetiredPlaceId],
) -> None:
    # A previously active identity can explicitly be merged into a new keeper.
    # Its aliases and classification metadata follow it to the final owner.
    for previous in previous_manifest.identities:
        target = owner_by_id[previous.active_legacy_place_id]
        part = identity_parts[target]
        if str(part["city_id"]) != previous.city_id:
            raise CanonicalIdentityError(
                "existing canonical identity cannot move cities: "
                + previous.canonical_place_id
            )
        if (
            target == previous.canonical_place_id
            and part["primary_type"] != previous.primary_type
        ):
            raise CanonicalIdentityError(
                "existing canonical identity cannot change its primary quota slot: "
                + previous.canonical_place_id
            )
        part["legacy_ids"].update(previous.legacy_place_ids)  # type: ignore[union-attr]
        part["all_types"].update(previous.place_types)  # type: ignore[union-attr]
        part["tags"].update(previous.tags)  # type: ignore[union-attr]

    for retired_id, previous in previous_retired.items():
        expected_target = owner_by_id[previous.canonical_place_id]
        current_target = owner_by_id.get(retired_id)
        if current_target is not None and current_target != expected_target:
            raise CanonicalIdentityError(
                "retired legacy ID cannot be reassigned to another canonical place: "
                + retired_id
            )
        part = identity_parts[expected_target]
        part["legacy_ids"].add(retired_id)  # type: ignore[union-attr]
        existing = retirement_parts.get(retired_id)
        if existing is not None and str(existing["canonical_id"]) != expected_target:
            raise CanonicalIdentityError(
                "retired legacy ID has conflicting canonical owners: " + retired_id
            )
        retirement_parts[retired_id] = {
            "canonical_id": expected_target,
            "city_id": previous.city_id,
            "entity_type": previous.entity_type,
            "reason": previous.reason,
        }


def _build_identity(values: dict[str, object]) -> CanonicalPlaceIdentity:
    active_id = str(values["active_id"])
    legacy_ids = [active_id] + sorted(
        set(values["legacy_ids"]) - {active_id}  # type: ignore[arg-type]
    )
    primary_type = values["primary_type"]
    secondary_types = sorted(
        set(values["all_types"]) - {primary_type},  # type: ignore[arg-type]
        key=lambda item: item.value,
    )
    tags = sorted(set(values["tags"]))  # type: ignore[arg-type]
    payload = canonical_identity_payload(
        canonical_place_id=active_id,
        active_legacy_place_id=active_id,
        legacy_place_ids=legacy_ids,
        city_id=str(values["city_id"]),
        primary_type=primary_type,
        secondary_types=secondary_types,
        tags=tags,
    )
    return CanonicalPlaceIdentity(
        **payload,
        identity_hash=stable_sha256(payload),
    )

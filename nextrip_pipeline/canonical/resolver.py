from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timezone

from nextrip_pipeline.schemas import EntityType

from .invalidation import ApprovedCanonicalInvalidation
from .models import (
    CanonicalIdentityManifest,
    CanonicalPlaceIdentity,
    DuplicateIdentityDecision,
    EntityCityQuota,
    EntityCityVacancy,
    LegacyPlaceSlot,
    QuarantinedCanonicalIdentity,
    RetiredPlaceId,
    VacancyReplacementDecision,
    VacancySourceSubtype,
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
        self._quarantine_by_canonical_id = {
            item.identity.canonical_place_id: item
            for item in manifest.quarantined_identities
        }
        self._quarantine_owner_by_id = {
            legacy_id: item.identity.canonical_place_id
            for item in manifest.quarantined_identities
            for legacy_id in item.identity.legacy_place_ids
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

    def is_quarantined(self, place_id: str) -> bool:
        """Return whether an ID belongs to a quarantined canonical identity."""

        return place_id in self._quarantine_owner_by_id

    def quarantine_for(self, place_id: str) -> str | None:
        """Return the quarantined canonical owner for one historical ID."""

        return self._quarantine_owner_by_id.get(place_id)

    def quarantined_identity_for(
        self, place_id: str
    ) -> QuarantinedCanonicalIdentity | None:
        canonical_id = self.quarantine_for(place_id)
        return (
            self._quarantine_by_canonical_id.get(canonical_id)
            if canonical_id is not None
            else None
        )

    @classmethod
    def build_manifest(
        cls,
        slots: Sequence[LegacyPlaceSlot],
        *,
        duplicate_decisions: Sequence[DuplicateIdentityDecision] = (),
        replacement_decisions: Sequence[VacancyReplacementDecision] = (),
        approved_invalidations: Sequence[ApprovedCanonicalInvalidation] = (),
        previous_manifest: CanonicalIdentityManifest | None = None,
        generated_at: datetime | None = None,
    ) -> CanonicalIdentityManifest:
        return build_canonical_identity_manifest(
            slots,
            duplicate_decisions=duplicate_decisions,
            replacement_decisions=replacement_decisions,
            approved_invalidations=approved_invalidations,
            previous_manifest=previous_manifest,
            generated_at=generated_at,
        )


def build_canonical_identity_manifest(
    slots: Sequence[LegacyPlaceSlot],
    *,
    duplicate_decisions: Sequence[DuplicateIdentityDecision] = (),
    replacement_decisions: Sequence[VacancyReplacementDecision] = (),
    approved_invalidations: Sequence[ApprovedCanonicalInvalidation] = (),
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
        previous_identity_snapshots = [
            *previous_manifest.identities,
            *(
                item.identity
                for item in previous_manifest.quarantined_identities
            ),
        ]
        for identity in previous_identity_snapshots:
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

    identity_snapshots = [
        _build_identity(value) for value in identity_parts.values()
    ]
    identity_snapshots.sort(key=lambda item: item.canonical_place_id)
    quarantined_identities = _resolve_quarantined_identities(
        identities=identity_snapshots,
        approved_invalidations=approved_invalidations,
        previous_manifest=previous_manifest,
    )
    quarantined_ids = {
        item.identity.canonical_place_id for item in quarantined_identities
    }
    identities = [
        item
        for item in identity_snapshots
        if item.canonical_place_id not in quarantined_ids
    ]

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

    vacancy_sources: dict[str, tuple[str, EntityType]] = {
        item.retired_place_id: (item.city_id, item.entity_type)
        for item in retirements
    }
    vacancy_sources.update(
        {
            item.identity.canonical_place_id: (
                item.identity.city_id,
                item.identity.primary_type,
            )
            for item in quarantined_identities
        }
    )
    quarantined_legacy_ids = {
        legacy_id
        for item in quarantined_identities
        for legacy_id in item.identity.legacy_place_ids
    }
    replacement_by_retired = _resolve_replacement_assignments(
        replacement_decisions=replacement_decisions,
        previous_manifest=previous_manifest,
        identities=identities,
        vacancy_sources=vacancy_sources,
        forbidden_replacement_ids=(
            {item.retired_place_id for item in retirements}
            | quarantined_legacy_ids
        ),
        supplied_slot_ids=supplied_slot_ids,
    )
    previous_vacancies = (
        {
            item.retired_place_id: item
            for item in previous_manifest.vacancies
        }
        if previous_manifest is not None
        else {}
    )

    vacancies = [
        EntityCityVacancy(
            vacancy_id=stable_identifier(
                "vacancy",
                city_id,
                entity_type.value,
                source_place_id,
            ),
            retired_place_id=source_place_id,
            city_id=city_id,
            entity_type=entity_type,
            source_subtype=_resolve_vacancy_source_subtype(
                retired_place_id=source_place_id,
                entity_type=entity_type,
                slot_by_id=slot_by_id,
                previous_vacancies=previous_vacancies,
            ),
            status=(
                VacancyStatus.FILLED
                if source_place_id in replacement_by_retired
                else VacancyStatus.VACANT
            ),
            replacement_place_id=replacement_by_retired.get(source_place_id),
        )
        for source_place_id, (city_id, entity_type) in vacancy_sources.items()
    ]
    vacancies.sort(key=lambda item: (item.city_id, item.entity_type.value, item.vacancy_id))

    # Quota rows describe the current type distribution, not a frozen target.
    # A cross-type fill is counted under the replacement identity's type while
    # an unfilled vacancy remains counted under its retired historical type.
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

    schema_version = (
        "1.1.0"
        if quarantined_identities
        or (
            previous_manifest is not None
            and previous_manifest.schema_version == "1.1.0"
        )
        else "1.0.0"
    )
    payload = canonical_manifest_payload(
        schema_version=schema_version,
        identities=identities,
        quarantined_identities=quarantined_identities,
        retired_place_ids=retirements,
        vacancies=vacancies,
        quotas=quotas,
    )
    manifest_hash = stable_sha256(payload)
    return CanonicalIdentityManifest(
        manifest_id=f"canonical_identity_{manifest_hash[:20]}",
        manifest_hash=manifest_hash,
        generated_at=generated_at,
        schema_version=schema_version,
        identities=identities,
        quarantined_identities=quarantined_identities,
        retired_place_ids=retirements,
        vacancies=vacancies,
        quotas=quotas,
    )


def _resolve_quarantined_identities(
    *,
    identities: Sequence[CanonicalPlaceIdentity],
    approved_invalidations: Sequence[ApprovedCanonicalInvalidation],
    previous_manifest: CanonicalIdentityManifest | None,
) -> list[QuarantinedCanonicalIdentity]:
    """Apply new approvals and retain every previous quarantine permanently."""

    identity_by_id = {item.canonical_place_id: item for item in identities}
    previous_by_id = (
        {
            item.identity.canonical_place_id: item
            for item in previous_manifest.quarantined_identities
        }
        if previous_manifest is not None
        else {}
    )
    approvals_by_id: dict[str, ApprovedCanonicalInvalidation] = {}
    for approval in approved_invalidations:
        previous = approvals_by_id.get(approval.canonical_place_id)
        if previous is not None and previous != approval:
            raise CanonicalIdentityError(
                "one invalidation approval is allowed per canonical identity: "
                + approval.canonical_place_id
            )
        approvals_by_id[approval.canonical_place_id] = approval

    quarantined: list[QuarantinedCanonicalIdentity] = []
    for canonical_id, previous in sorted(previous_by_id.items()):
        current = identity_by_id.get(canonical_id)
        if current is None:
            raise CanonicalIdentityError(
                "quarantined canonical identity cannot change keeper or disappear: "
                + canonical_id
            )
        if current != previous.identity:
            raise CanonicalIdentityError(
                "quarantined canonical identity metadata cannot be changed: "
                + canonical_id
            )
        supplied = approvals_by_id.get(canonical_id)
        if supplied is not None and (
            supplied.approval_id != previous.invalidation_approval_id
            or supplied.approval_hash != previous.invalidation_approval_hash
        ):
            raise CanonicalIdentityError(
                "quarantined identity cannot receive another invalidation approval: "
                + canonical_id
            )
        quarantined.append(previous)

    previous_filled_replacements = (
        {
            item.replacement_place_id
            for item in previous_manifest.vacancies
            if item.status is VacancyStatus.FILLED
            and item.replacement_place_id is not None
        }
        if previous_manifest is not None
        else set()
    )
    for canonical_id, approval in sorted(approvals_by_id.items()):
        if canonical_id in previous_by_id:
            continue
        if approval.schema_version == "1.0.0":
            raise CanonicalIdentityError(
                "legacy invalidation approval cannot create a new quarantine; "
                "reissue it with pinned physical-identity corroboration: "
                + canonical_id
            )
        if previous_manifest is None:
            raise CanonicalIdentityError(
                "a new invalidation approval requires its source previous manifest"
            )
        if (
            approval.source_manifest_id != previous_manifest.manifest_id
            or approval.source_manifest_hash != previous_manifest.manifest_hash
        ):
            raise CanonicalIdentityError(
                "invalidation approval belongs to another source manifest: "
                + canonical_id
            )
        source_identity = next(
            (
                item
                for item in previous_manifest.identities
                if item.canonical_place_id == canonical_id
            ),
            None,
        )
        if source_identity is None:
            raise CanonicalIdentityError(
                "invalidation approval does not target an active identity: "
                + canonical_id
            )
        current = identity_by_id.get(canonical_id)
        if current is None or current != source_identity:
            raise CanonicalIdentityError(
                "invalidation target changed after approval: " + canonical_id
            )
        if approval.identity_hash != current.identity_hash:
            raise CanonicalIdentityError(
                "invalidation approval identity hash is stale: " + canonical_id
            )
        if canonical_id in previous_filled_replacements:
            raise CanonicalIdentityError(
                "approved replacement invalidation requires a retraction workflow: "
                + canonical_id
            )
        quarantined.append(
            QuarantinedCanonicalIdentity(
                quarantine_id=stable_identifier("quarantine", canonical_id),
                identity=current,
                source_manifest_id=approval.source_manifest_id,
                source_manifest_hash=approval.source_manifest_hash,
                invalidation_approval_id=approval.approval_id,
                invalidation_approval_hash=approval.approval_hash,
                reason=approval.reason.value,
                reviewer=approval.reviewer,
                invalidated_at=approval.approved_at,
                evidence_hashes=sorted(
                    item.artifact_sha256 for item in approval.evidence
                ),
            )
        )
    return sorted(
        quarantined,
        key=lambda item: item.identity.canonical_place_id,
    )


def _resolve_vacancy_source_subtype(
    *,
    retired_place_id: str,
    entity_type: EntityType,
    slot_by_id: dict[str, LegacyPlaceSlot],
    previous_vacancies: dict[str, EntityCityVacancy],
) -> VacancySourceSubtype | None:
    if entity_type is not EntityType.NIGHTLIFE:
        return None
    slot = slot_by_id.get(retired_place_id)
    current = _source_subtype_from_tags(slot.tags) if slot is not None else None
    previous_vacancy = previous_vacancies.get(retired_place_id)
    previous = (
        previous_vacancy.source_subtype
        if previous_vacancy is not None
        else None
    )
    if current is not None and previous is not None and current is not previous:
        raise CanonicalIdentityError(
            "retired vacancy source subtype cannot be changed: "
            f"{retired_place_id}"
        )
    return current or previous


def _source_subtype_from_tags(
    tags: Sequence[str],
) -> VacancySourceSubtype | None:
    normalized = {tag.casefold() for tag in tags}
    matches: set[VacancySourceSubtype] = set()
    if "late_night_cafe" in normalized:
        matches.add(VacancySourceSubtype.LATE_NIGHT_CAFE)
    if {"late_night_dining", "late_night_restaurant"} & normalized:
        matches.add(VacancySourceSubtype.LATE_NIGHT_DINING)
    if len(matches) > 1:
        raise CanonicalIdentityError(
            "retired nightlife slot has conflicting late-night subtype tags"
        )
    return next(iter(matches), None)


def _resolve_replacement_assignments(
    *,
    replacement_decisions: Sequence[VacancyReplacementDecision],
    previous_manifest: CanonicalIdentityManifest | None,
    identities: Sequence[CanonicalPlaceIdentity],
    vacancy_sources: dict[str, tuple[str, EntityType]],
    forbidden_replacement_ids: set[str],
    supplied_slot_ids: set[str],
) -> dict[str, str]:
    """Validate new fills and retain every previously committed assignment."""

    identity_by_id = {item.canonical_place_id: item for item in identities}
    previous_identity_ids = (
        {
            legacy_id
            for identity in [
                *previous_manifest.identities,
                *(
                    item.identity
                    for item in previous_manifest.quarantined_identities
                ),
            ]
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
        if retired_id not in vacancy_sources:
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
        source_slot = vacancy_sources.get(retired_id)
        if source_slot is None:
            raise CanonicalIdentityError(
                "historical filled vacancy no longer exists: " + retired_id
            )
        if replacement_id in forbidden_replacement_ids:
            raise CanonicalIdentityError(
                "retired or quarantined place ID cannot be reused as a replacement: "
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
        source_city_id, _ = source_slot
        if replacement.city_id != source_city_id:
            raise CanonicalIdentityError(
                "replacement must preserve the vacancy city slot: "
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
    quarantined_ids = {
        item.identity.canonical_place_id
        for item in previous_manifest.quarantined_identities
    }
    previous_identity_snapshots = [
        *previous_manifest.identities,
        *(item.identity for item in previous_manifest.quarantined_identities),
    ]
    for previous in previous_identity_snapshots:
        target = owner_by_id[previous.active_legacy_place_id]
        if (
            previous.canonical_place_id in quarantined_ids
            and target != previous.canonical_place_id
        ):
            raise CanonicalIdentityError(
                "quarantined identity cannot be merged into another keeper: "
                + previous.canonical_place_id
            )
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

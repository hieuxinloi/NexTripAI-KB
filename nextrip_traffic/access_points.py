from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from threading import RLock
from typing import Any, Literal

from pydantic import ValidationError

from nextrip_pipeline.canonical.dataset import read_canonical_active_dataset
from nextrip_pipeline.canonical.place_projection import (
    project_canonical_dataset_places,
)
from nextrip_pipeline.schemas import (
    AccessPointRecord,
    AccessPointType,
    CurrentPlaceSnapshot,
    GeoPoint,
    TransportMode,
    VerificationStatus,
)

from .errors import AccessPointNotFoundError


_VERIFIED_STATUSES = frozenset(
    {
        VerificationStatus.LEGACY_VERIFIED,
        VerificationStatus.AUTO_VERIFIED,
        VerificationStatus.AGENT_VERIFIED,
        VerificationStatus.HUMAN_VERIFIED,
    }
)
_ROAD_ROUTING_MODES = [
    TransportMode.WALK,
    TransportMode.BICYCLE,
    TransportMode.TWO_WHEELER,
    TransportMode.DRIVE,
]


@dataclass(frozen=True, slots=True)
class RegistryIssue:
    """One non-fatal problem found while rebuilding the registry."""

    code: str
    message: str
    path: str | None = None
    record_id: str | None = None
    severity: Literal["warning", "error"] = "error"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RegistryLoadReport:
    """Audit summary for one atomic registry reload."""

    files_seen: int
    place_access_points_loaded: int
    city_centers_created: int
    override_access_points_loaded: int
    skipped_records: int
    issues: tuple[RegistryIssue, ...]

    @property
    def error_count(self) -> int:
        return sum(issue.severity == "error" for issue in self.issues)

    @property
    def warning_count(self) -> int:
        return sum(issue.severity == "warning" for issue in self.issues)

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["error_count"] = self.error_count
        payload["warning_count"] = self.warning_count
        return payload


class AccessPointRegistry:
    """In-memory routing endpoints derived from canonical place data.

    Reloads are built in temporary dictionaries and swapped under a lock so
    callers never observe a half-built registry.

    An optional overrides file may be either a JSON list of access-point
    records or an object with ``access_points`` and ``aliases`` keys. Each
    access-point object may include a registry-only ``aliases`` list. Curated
    overrides replace a generated record with the same access-point ID.
    """

    def __init__(
        self,
        *,
        canonical_dataset_path: str | Path,
        overrides_path: str | Path | None = None,
        auto_reload: bool = True,
    ) -> None:
        self.canonical_dataset_path = Path(canonical_dataset_path)
        self.overrides_path = Path(overrides_path) if overrides_path else None
        self._lock = RLock()
        self._records: dict[str, AccessPointRecord] = {}
        self._aliases: dict[str, str] = {}
        self._origins: dict[str, Literal["place", "city", "override"]] = {}
        self.last_report = RegistryLoadReport(0, 0, 0, 0, 0, ())
        if auto_reload:
            self.reload()

    def reload(self) -> RegistryLoadReport:
        """Rebuild and atomically publish all access points."""

        records: dict[str, AccessPointRecord] = {}
        aliases: dict[str, str] = {}
        origins: dict[str, Literal["place", "city", "override"]] = {}
        issues: list[RegistryIssue] = []
        verified_by_city: dict[str, list[CurrentPlaceSnapshot]] = defaultdict(list)

        files_seen = 0
        place_count = 0
        city_count = 0
        override_count = 0
        skipped = 0

        snapshot_inputs: list[tuple[CurrentPlaceSnapshot, Path | None]] = []
        try:
            dataset = read_canonical_active_dataset(self.canonical_dataset_path)
            projected = project_canonical_dataset_places(dataset)
            snapshot_inputs = [
                (projected[place_id], self.canonical_dataset_path)
                for place_id in sorted(projected)
            ]
            files_seen = len(snapshot_inputs)
        except (OSError, UnicodeError, ValidationError, ValueError) as error:
            issues.append(
                RegistryIssue(
                    code="invalid_canonical_dataset",
                    message=str(error),
                    path=str(self.canonical_dataset_path),
                )
            )
            skipped += 1

        seen_place_ids: set[str] = set()
        for snapshot, path in snapshot_inputs:
            if snapshot.place_id in seen_place_ids:
                issues.append(
                    RegistryIssue(
                        code="duplicate_place_id",
                        message=f"duplicate place_id {snapshot.place_id!r}; record skipped",
                        path=str(path) if path is not None else None,
                        record_id=snapshot.place_id,
                    )
                )
                skipped += 1
                continue
            seen_place_ids.add(snapshot.place_id)

            if snapshot.location is None:
                issues.append(
                    RegistryIssue(
                        code="missing_location",
                        message="place has no routable location",
                        path=str(path) if path is not None else None,
                        record_id=snapshot.place_id,
                        severity="warning",
                    )
                )
                skipped += 1
                continue

            access_point_id = f"place:{snapshot.place_id}:main"
            record = AccessPointRecord(
                access_point_id=access_point_id,
                owner_entity_id=snapshot.place_id,
                access_type=AccessPointType.MAIN_ENTRANCE,
                name=snapshot.name,
                location=snapshot.location,
                supported_modes=_ROAD_ROUTING_MODES,
                source_record_ids=[snapshot.provenance.source_record_id],
                verification_status=snapshot.provenance.verification_status,
                updated_at=snapshot.updated_at,
            )
            if not self._add_generated_record(
                record,
                record_aliases=(snapshot.place_id,),
                origin="place",
                records=records,
                aliases=aliases,
                origins=origins,
                issues=issues,
                path=path,
            ):
                skipped += 1
                continue
            place_count += 1

            if (
                snapshot.city_id
                and snapshot.provenance.verification_status in _VERIFIED_STATUSES
            ):
                verified_by_city[snapshot.city_id].append(snapshot)

        for city_id in sorted(verified_by_city):
            snapshots = verified_by_city[city_id]
            if not snapshots:
                continue
            latest_update = max(snapshot.updated_at for snapshot in snapshots)
            latest_verified = max(
                (
                    snapshot.location.verified_at
                    for snapshot in snapshots
                    if snapshot.location is not None
                    and snapshot.location.verified_at is not None
                ),
                default=latest_update,
            )
            city_names = Counter(snapshot.city for snapshot in snapshots)
            city_name = min(
                city_names,
                key=lambda value: (-city_names[value], value),
            )
            city_record = AccessPointRecord(
                access_point_id=f"city:{city_id}:center",
                owner_entity_id=city_id,
                access_type=AccessPointType.CITY_CENTER,
                name=f"{city_name} center",
                location=GeoPoint(
                    latitude=median(
                        snapshot.location.latitude
                        for snapshot in snapshots
                        if snapshot.location is not None
                    ),
                    longitude=median(
                        snapshot.location.longitude
                        for snapshot in snapshots
                        if snapshot.location is not None
                    ),
                    accuracy="derived_city_center_median",
                    source="derived:verified-canonical-place-median",
                    verified_at=latest_verified,
                ),
                supported_modes=_ROAD_ROUTING_MODES,
                # Keep provenance bounded: the marker states the deterministic
                # method and population size without copying hundreds of IDs.
                source_record_ids=[
                    f"derived:median:canonical-place:{city_id}:count={len(snapshots)}"
                ],
                verification_status=VerificationStatus.AUTO_VERIFIED,
                updated_at=latest_update,
            )
            if self._add_generated_record(
                city_record,
                record_aliases=(city_id,),
                origin="city",
                records=records,
                aliases=aliases,
                origins=origins,
                issues=issues,
                path=None,
            ):
                city_count += 1
            else:
                skipped += 1

        if self.overrides_path is not None:
            loaded, override_skipped = self._load_overrides(
                records=records,
                aliases=aliases,
                origins=origins,
                issues=issues,
            )
            override_count += loaded
            skipped += override_skipped

        report = RegistryLoadReport(
            files_seen=files_seen,
            place_access_points_loaded=place_count,
            city_centers_created=city_count,
            override_access_points_loaded=override_count,
            skipped_records=skipped,
            issues=tuple(issues),
        )
        with self._lock:
            self._records = records
            self._aliases = aliases
            self._origins = origins
            self.last_report = report
        return report

    def get(self, access_point_id: str) -> AccessPointRecord:
        """Return an access point by canonical ID (aliases are not consulted)."""

        with self._lock:
            record = self._records.get(access_point_id)
            if record is None:
                raise AccessPointNotFoundError(access_point_id)
            return record.model_copy(deep=True)

    def resolve(self, identifier: str) -> AccessPointRecord:
        """Resolve either a canonical access-point ID or a registered alias."""

        with self._lock:
            access_point_id = (
                identifier
                if identifier in self._records
                else self._aliases.get(identifier)
            )
            if access_point_id is None:
                raise AccessPointNotFoundError(identifier)
            return self._records[access_point_id].model_copy(deep=True)

    def list(
        self,
        *,
        access_type: AccessPointType | None = None,
        owner_entity_id: str | None = None,
    ) -> list[AccessPointRecord]:
        """Return a stable, optionally filtered snapshot of the registry."""

        with self._lock:
            values = (
                record
                for _, record in sorted(self._records.items())
                if (access_type is None or record.access_type == access_type)
                and (
                    owner_entity_id is None or record.owner_entity_id == owner_entity_id
                )
            )
            return [record.model_copy(deep=True) for record in values]

    def stats(self) -> dict[str, object]:
        """Return JSON-ready registry counts for health and operations APIs."""

        with self._lock:
            by_type = Counter(
                record.access_type.value for record in self._records.values()
            )
            by_status = Counter(
                record.verification_status.value for record in self._records.values()
            )
            by_origin = Counter(self._origins.values())
            return {
                "total_access_points": len(self._records),
                "aliases": len(self._aliases),
                "by_access_type": dict(sorted(by_type.items())),
                "by_verification_status": dict(sorted(by_status.items())),
                "by_origin": dict(sorted(by_origin.items())),
                "load": self.last_report.as_dict(),
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    def __contains__(self, identifier: object) -> bool:
        if not isinstance(identifier, str):
            return False
        with self._lock:
            return identifier in self._records or identifier in self._aliases

    @staticmethod
    def _add_generated_record(
        record: AccessPointRecord,
        *,
        record_aliases: tuple[str, ...],
        origin: Literal["place", "city"],
        records: dict[str, AccessPointRecord],
        aliases: dict[str, str],
        origins: dict[str, Literal["place", "city", "override"]],
        issues: list[RegistryIssue],
        path: Path | None,
    ) -> bool:
        access_point_id = record.access_point_id
        if access_point_id in records:
            issues.append(
                RegistryIssue(
                    code="duplicate_access_point_id",
                    message=f"duplicate access-point ID {access_point_id!r}; record skipped",
                    path=str(path) if path else None,
                    record_id=access_point_id,
                )
            )
            return False

        candidate_aliases = (access_point_id, *record_aliases)
        conflicts = [
            alias
            for alias in candidate_aliases
            if alias in aliases and aliases[alias] != access_point_id
        ]
        if conflicts:
            issues.append(
                RegistryIssue(
                    code="duplicate_alias",
                    message=f"aliases already belong to another access point: {conflicts}",
                    path=str(path) if path else None,
                    record_id=access_point_id,
                )
            )
            return False

        records[access_point_id] = record
        origins[access_point_id] = origin
        for alias in candidate_aliases:
            aliases[alias] = access_point_id
        return True

    def _load_overrides(
        self,
        *,
        records: dict[str, AccessPointRecord],
        aliases: dict[str, str],
        origins: dict[str, Literal["place", "city", "override"]],
        issues: list[RegistryIssue],
    ) -> tuple[int, int]:
        assert self.overrides_path is not None
        path = self.overrides_path
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            issues.append(
                RegistryIssue(
                    code="invalid_overrides_file",
                    message=str(error),
                    path=str(path),
                )
            )
            return 0, 1

        if isinstance(raw, list):
            entries = raw
            top_aliases: Any = {}
        elif isinstance(raw, dict):
            entries = raw.get("access_points", [])
            top_aliases = raw.get("aliases", {})
        else:
            issues.append(
                RegistryIssue(
                    code="invalid_overrides_shape",
                    message="overrides must be a list or an object",
                    path=str(path),
                )
            )
            return 0, 1

        if not isinstance(entries, list):
            issues.append(
                RegistryIssue(
                    code="invalid_overrides_shape",
                    message="access_points must be a list",
                    path=str(path),
                )
            )
            return 0, 1

        loaded = 0
        skipped = 0
        seen_override_ids: set[str] = set()
        local_aliases_by_id: dict[str, list[str]] = {}
        for index, value in enumerate(entries):
            if not isinstance(value, dict):
                issues.append(
                    RegistryIssue(
                        code="invalid_override_access_point",
                        message=f"access_points[{index}] must be an object",
                        path=str(path),
                    )
                )
                skipped += 1
                continue
            payload = dict(value)
            local_aliases = payload.pop("aliases", [])
            if not isinstance(local_aliases, list) or not all(
                isinstance(alias, str) and alias for alias in local_aliases
            ):
                issues.append(
                    RegistryIssue(
                        code="invalid_override_aliases",
                        message=f"access_points[{index}].aliases must contain strings",
                        path=str(path),
                    )
                )
                skipped += 1
                continue
            try:
                record = AccessPointRecord.model_validate(payload)
            except ValidationError as error:
                issues.append(
                    RegistryIssue(
                        code="invalid_override_access_point",
                        message=str(error),
                        path=str(path),
                        record_id=str(payload.get("access_point_id") or "") or None,
                    )
                )
                skipped += 1
                continue
            if record.access_point_id in seen_override_ids:
                issues.append(
                    RegistryIssue(
                        code="duplicate_override_access_point_id",
                        message=(
                            f"duplicate override ID {record.access_point_id!r}; "
                            "later record skipped"
                        ),
                        path=str(path),
                        record_id=record.access_point_id,
                    )
                )
                skipped += 1
                continue
            seen_override_ids.add(record.access_point_id)

            previous = records.get(record.access_point_id)
            if previous is not None:
                issues.append(
                    RegistryIssue(
                        code="generated_access_point_overridden",
                        message=f"curated override replaced {record.access_point_id!r}",
                        path=str(path),
                        record_id=record.access_point_id,
                        severity="warning",
                    )
                )
                # Existing aliases still identify the same canonical endpoint.
                # Retaining them means an override can refine coordinates or
                # access type without breaking place_id/city_id callers.

            records[record.access_point_id] = record
            origins[record.access_point_id] = "override"
            aliases[record.access_point_id] = record.access_point_id
            local_aliases_by_id[record.access_point_id] = local_aliases
            loaded += 1

        if not isinstance(top_aliases, dict):
            issues.append(
                RegistryIssue(
                    code="invalid_overrides_alias_map",
                    message="aliases must be an object mapping alias to access-point ID",
                    path=str(path),
                )
            )
            skipped += 1
            top_aliases = {}

        requested_aliases: list[tuple[str, str]] = []
        for access_point_id, values in local_aliases_by_id.items():
            requested_aliases.extend((alias, access_point_id) for alias in values)
        requested_aliases.extend(
            (str(alias), str(access_point_id))
            for alias, access_point_id in top_aliases.items()
        )

        for alias, access_point_id in requested_aliases:
            if not alias:
                issues.append(
                    RegistryIssue(
                        code="invalid_override_alias",
                        message="override alias cannot be empty",
                        path=str(path),
                    )
                )
                skipped += 1
                continue
            if access_point_id not in records:
                issues.append(
                    RegistryIssue(
                        code="unknown_override_alias_target",
                        message=(
                            f"alias {alias!r} references unknown access point "
                            f"{access_point_id!r}"
                        ),
                        path=str(path),
                        record_id=access_point_id,
                    )
                )
                skipped += 1
                continue
            existing = aliases.get(alias)
            if existing is not None and existing != access_point_id:
                issues.append(
                    RegistryIssue(
                        code="override_alias_reassigned",
                        message=(
                            f"curated alias {alias!r} moved from {existing!r} "
                            f"to {access_point_id!r}"
                        ),
                        path=str(path),
                        record_id=access_point_id,
                        severity="warning",
                    )
                )
            aliases[alias] = access_point_id

        return loaded, skipped

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from pydantic import AwareDatetime, Field, HttpUrl, TypeAdapter, ValidationError

from nextrip_pipeline.schemas import (
    BusinessStatus,
    CurrentPlaceOpeningHours,
    CurrentPlaceProvenance,
    CurrentPlaceSnapshot,
    EntityType,
    GeoPoint,
    NexTripModel,
    VerificationStatus,
)


MASTER_PLACE_FILES: Mapping[EntityType, str] = {
    EntityType.ATTRACTION: "attraction_final.json",
    EntityType.CAFE: "cafe_final.json",
    EntityType.HOTEL: "hotel_final.json",
    EntityType.NIGHTLIFE: "nightlife_final.json",
    EntityType.RESTAURANT: "restaurant_final.json",
}

_COVERAGE_FIELDS = (
    "name",
    "city",
    "address",
    "location",
    "opening_hours",
    "phone",
    "website_url",
    "cover_image_url",
    "price_level",
)
_HTTP_URL = TypeAdapter(HttpUrl)


class MasterPlaceSeedDisposition(StrEnum):
    SEEDED = "seeded"
    SKIPPED_EXISTING = "skipped_existing"


class MasterPlaceBootstrapError(NexTripModel):
    entity_type: EntityType
    source_file: str = Field(min_length=1)
    record_index: int = Field(ge=0)
    place_id: str | None = None
    error_type: str = Field(min_length=1)
    message: str = Field(min_length=1)


class MasterPlaceEntityCoverage(NexTripModel):
    entity_type: EntityType
    source_file: str = Field(min_length=1)
    source_record_count: int = Field(ge=0)
    seeded_count: int = Field(ge=0)
    skipped_existing_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    present_field_counts: dict[str, int] = Field(default_factory=dict)
    missing_field_counts: dict[str, int] = Field(default_factory=dict)


class MasterPlaceBootstrapSummary(NexTripModel):
    run_id: str = Field(min_length=1)
    source_id: str = "verified-master-data"
    source_directory: str = Field(min_length=1)
    current_directory: str = Field(min_length=1)
    started_at: AwareDatetime
    finished_at: AwareDatetime
    source_file_count: int = Field(ge=0)
    source_record_count: int = Field(ge=0)
    seeded_count: int = Field(ge=0)
    skipped_existing_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    seeded_place_ids: list[str] = Field(default_factory=list)
    skipped_existing_place_ids: list[str] = Field(default_factory=list)
    entity_coverage: list[MasterPlaceEntityCoverage] = Field(default_factory=list)
    errors: list[MasterPlaceBootstrapError] = Field(default_factory=list)


class MasterPlaceBootstrapSummaryWriter:
    """Atomically write an immutable coverage report for one bootstrap run."""

    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def path_for(self, run_id: str) -> Path:
        return self.root_directory / f"run={quote(run_id, safe='-_.')}.json"

    def write(self, summary: MasterPlaceBootstrapSummary) -> Path:
        destination = self.path_for(summary.run_id)
        _publish_new_file(destination, summary.model_dump_json(indent=2) + "\n")
        return destination


class VerifiedMasterCurrentPlaceWriter:
    """Seed a current-place baseline without replacing any existing snapshot.

    In particular, a Google Maps PASS already stored at the destination always
    wins. The no-overwrite guarantee is enforced at filesystem publication
    time, so concurrent seed workers cannot replace one another either.
    """

    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def path_for(self, place_id: str) -> Path:
        return self.root_directory / f"{quote(place_id, safe='-_.')}.json"

    def get(self, place_id: str) -> CurrentPlaceSnapshot | None:
        destination = self.path_for(place_id)
        if not destination.exists():
            return None
        return CurrentPlaceSnapshot.model_validate_json(
            destination.read_text(encoding="utf-8")
        )

    def seed(
        self, snapshot: CurrentPlaceSnapshot
    ) -> tuple[MasterPlaceSeedDisposition, Path]:
        destination = self.path_for(snapshot.place_id)
        existing = self.get(snapshot.place_id)
        if existing is not None:
            self._require_same_identity(existing, snapshot)
            return MasterPlaceSeedDisposition.SKIPPED_EXISTING, destination

        try:
            _publish_new_file(
                destination,
                snapshot.model_dump_json(indent=2) + "\n",
            )
        except FileExistsError:
            # Another writer won after the initial read. Validate what won and
            # retain it instead of turning the seed operation into an overwrite.
            existing = self.get(snapshot.place_id)
            if existing is None:  # pragma: no cover - defensive filesystem race
                raise
            self._require_same_identity(existing, snapshot)
            return MasterPlaceSeedDisposition.SKIPPED_EXISTING, destination
        return MasterPlaceSeedDisposition.SEEDED, destination

    @staticmethod
    def _require_same_identity(
        existing: CurrentPlaceSnapshot,
        proposed: CurrentPlaceSnapshot,
    ) -> None:
        current_identity = (
            existing.place_id,
            existing.entity_type,
            existing.city,
            existing.city_id,
        )
        proposed_identity = (
            proposed.place_id,
            proposed.entity_type,
            proposed.city,
            proposed.city_id,
        )
        if current_identity != proposed_identity:
            raise ValueError(
                "existing current-place identity conflicts with verified master data"
            )


class VerifiedMasterPlaceBootstrapper:
    """Create seed-only current-place projections from five verified datasets."""

    source_id = "verified-master-data"

    def __init__(
        self,
        current_writer: VerifiedMasterCurrentPlaceWriter,
        summary_writer: MasterPlaceBootstrapSummaryWriter,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.current_writer = current_writer
        self.summary_writer = summary_writer
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run(
        self,
        source_directory: str | Path,
        *,
        run_id: str | None = None,
    ) -> tuple[MasterPlaceBootstrapSummary, Path]:
        source_root = Path(source_directory)
        started_at = self._aware_now()
        run_id = run_id or self._run_id(started_at)
        datasets = self._load_all(source_root)

        seeded_ids: list[str] = []
        skipped_ids: list[str] = []
        errors: list[MasterPlaceBootstrapError] = []
        coverage: list[MasterPlaceEntityCoverage] = []
        seen_place_ids: set[str] = set()

        for entity_type, source_file, records in datasets:
            present = {field_name: 0 for field_name in _COVERAGE_FIELDS}
            seeded_count = 0
            skipped_count = 0
            failed_count = 0

            for index, record in enumerate(records):
                place_id = _clean_string(record.get("id"))
                try:
                    if place_id and place_id in seen_place_ids:
                        raise ValueError(f"duplicate master place id: {place_id}")
                    snapshot = self._to_snapshot(
                        record,
                        entity_type=entity_type,
                        source_file=source_file,
                        run_id=run_id,
                        projected_at=started_at,
                    )
                    seen_place_ids.add(snapshot.place_id)
                    for field_name in _COVERAGE_FIELDS:
                        if _has_value(getattr(snapshot, field_name)):
                            present[field_name] += 1
                    disposition, _ = self.current_writer.seed(snapshot)
                    if disposition is MasterPlaceSeedDisposition.SEEDED:
                        seeded_count += 1
                        seeded_ids.append(snapshot.place_id)
                    else:
                        skipped_count += 1
                        skipped_ids.append(snapshot.place_id)
                except (OSError, TypeError, ValueError, ValidationError) as error:
                    failed_count += 1
                    errors.append(
                        MasterPlaceBootstrapError(
                            entity_type=entity_type,
                            source_file=source_file,
                            record_index=index,
                            place_id=place_id,
                            error_type=type(error).__name__,
                            message=str(error),
                        )
                    )

            total = len(records)
            coverage.append(
                MasterPlaceEntityCoverage(
                    entity_type=entity_type,
                    source_file=source_file,
                    source_record_count=total,
                    seeded_count=seeded_count,
                    skipped_existing_count=skipped_count,
                    failed_count=failed_count,
                    present_field_counts=present,
                    missing_field_counts={
                        field_name: total - count
                        for field_name, count in present.items()
                    },
                )
            )

        summary = MasterPlaceBootstrapSummary(
            run_id=run_id,
            source_directory=str(source_root),
            current_directory=str(self.current_writer.root_directory),
            started_at=started_at,
            finished_at=self._aware_now(),
            source_file_count=len(datasets),
            source_record_count=sum(len(records) for _, _, records in datasets),
            seeded_count=len(seeded_ids),
            skipped_existing_count=len(skipped_ids),
            failed_count=len(errors),
            seeded_place_ids=seeded_ids,
            skipped_existing_place_ids=skipped_ids,
            entity_coverage=coverage,
            errors=errors,
        )
        return summary, self.summary_writer.write(summary)

    def _to_snapshot(
        self,
        record: Mapping[str, object],
        *,
        entity_type: EntityType,
        source_file: str,
        run_id: str,
        projected_at: datetime,
    ) -> CurrentPlaceSnapshot:
        place_id = _required_string(record, "id")
        record_type = EntityType(_required_string(record, "entity_type"))
        if record_type is not entity_type:
            raise ValueError(
                f"record entity_type {record_type.value!r} does not match "
                f"{entity_type.value!r} dataset"
            )

        name = _required_string(record, "name")
        city = _required_string(record, "city")
        verification = self._verification_status(record)
        observed_at = self._record_timestamp(record)
        location = self._location(record.get("coordinates"), observed_at)
        opening_hours = self._opening_hours(record.get("opening_hours"))
        source = record.get("source")
        source_url = self._url(source.get("url")) if isinstance(source, Mapping) else None
        cover_image_url = self._cover_image(record.get("images"))
        website_url = self._website(record)
        price_level = record.get("price_level")
        if not isinstance(price_level, int) or isinstance(price_level, bool):
            price_level = None
        if isinstance(price_level, int) and not 1 <= price_level <= 4:
            price_level = None

        values: dict[str, object | None] = {
            "name": name,
            "city": city,
            "address": _clean_string(record.get("address")),
            "location": location,
            "opening_hours": opening_hours,
            "phone": self._phone(record),
            "website_url": website_url,
            "cover_image_url": cover_image_url,
            "price_level": price_level,
            "category": _clean_string(record.get("category")),
            "business_status": self._business_status(record.get("business_status")),
        }
        field_sources = {
            field_name: self.source_id
            for field_name, value in values.items()
            if _has_value(value)
        }

        return CurrentPlaceSnapshot(
            place_id=place_id,
            entity_type=entity_type,
            city=city,
            city_id=self._city_id(city),
            name=name,
            category=values["category"],
            address=values["address"],
            phone=values["phone"],
            website_url=website_url,
            location=location,
            business_status=values["business_status"],
            opening=None,
            weekly_opening=None,
            opening_hours=opening_hours,
            cover_image_url=cover_image_url,
            price_level=price_level,
            field_sources=field_sources,
            provenance=CurrentPlaceProvenance(
                run_id=run_id,
                source_record_id=place_id,
                source_id=self.source_id,
                source_url=source_url,
                source_file=source_file,
                verification_status=verification,
                observed_at=observed_at,
            ),
            updated_at=projected_at,
            stale_after=None,
        )

    @staticmethod
    def _load_all(
        source_root: Path,
    ) -> list[tuple[EntityType, str, list[Mapping[str, object]]]]:
        missing = [
            filename
            for filename in MASTER_PLACE_FILES.values()
            if not (source_root / filename).is_file()
        ]
        if missing:
            raise FileNotFoundError(
                "verified master datasets are missing: " + ", ".join(missing)
            )

        datasets: list[tuple[EntityType, str, list[Mapping[str, object]]]] = []
        for entity_type, filename in MASTER_PLACE_FILES.items():
            payload = json.loads((source_root / filename).read_text(encoding="utf-8-sig"))
            records = payload.get("data") if isinstance(payload, Mapping) else None
            if not isinstance(records, list):
                raise ValueError(f"{filename} must contain a data array")
            if not all(isinstance(record, Mapping) for record in records):
                raise ValueError(f"{filename} data entries must be JSON objects")
            datasets.append((entity_type, filename, records))
        return datasets

    @staticmethod
    def _verification_status(record: Mapping[str, object]) -> VerificationStatus:
        if _clean_string(record.get("last_verified")) or record.get(
            "verified_sources"
        ):
            return VerificationStatus.HUMAN_VERIFIED
        raw_status = (_clean_string(record.get("verification_status")) or "").lower()
        if raw_status.startswith(("reviewed", "human_verified", "verified_")):
            return VerificationStatus.HUMAN_VERIFIED
        return VerificationStatus.LEGACY_VERIFIED

    @staticmethod
    def _record_timestamp(record: Mapping[str, object]) -> datetime | None:
        source = record.get("source")
        candidates: Sequence[object] = (
            record.get("last_verified"),
            record.get("last_updated"),
            source.get("crawled_at") if isinstance(source, Mapping) else None,
        )
        for candidate in candidates:
            parsed = _parse_datetime(candidate)
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def _location(value: object, verified_at: datetime | None) -> GeoPoint | None:
        if not isinstance(value, Mapping):
            return None
        latitude = value.get("lat", value.get("latitude"))
        longitude = value.get("lng", value.get("longitude"))
        if not isinstance(latitude, (int, float)) or isinstance(latitude, bool):
            return None
        if not isinstance(longitude, (int, float)) or isinstance(longitude, bool):
            return None
        return GeoPoint(
            latitude=float(latitude),
            longitude=float(longitude),
            accuracy="verified_master",
            source="verified-master-data",
            verified_at=verified_at,
        )

    @staticmethod
    def _opening_hours(value: object) -> CurrentPlaceOpeningHours | None:
        if not isinstance(value, Mapping) or not value:
            return None
        opens_at = _clean_string(value.get("open"))
        closes_at = _clean_string(value.get("close"))
        closed_days = value.get("closed_days", [])
        if not isinstance(closed_days, (list, str)):
            closed_days = []
        if isinstance(closed_days, list):
            closed_days = [
                cleaned
                for item in closed_days
                if (cleaned := _clean_string(item)) is not None
            ]
        note = _clean_string(value.get("note"))
        if not any((opens_at, closes_at, closed_days, note)):
            return None
        return CurrentPlaceOpeningHours(
            opens_at=opens_at,
            closes_at=closes_at,
            closed_days=closed_days,
            note=note,
        )

    @staticmethod
    def _phone(record: Mapping[str, object]) -> str | None:
        direct = _clean_string(record.get("phone")) or _clean_string(
            record.get("phone_number")
        )
        if direct:
            return direct
        contact = record.get("contact")
        return _clean_string(contact.get("phone")) if isinstance(contact, Mapping) else None

    @classmethod
    def _website(cls, record: Mapping[str, object]) -> HttpUrl | None:
        direct = record.get("website_url", record.get("website"))
        if direct is None and isinstance(record.get("contact"), Mapping):
            direct = record["contact"].get("website")
        return cls._url(direct)

    @classmethod
    def _cover_image(cls, value: object) -> HttpUrl | None:
        if not isinstance(value, list):
            return None
        for image in value:
            candidate = image.get("url") if isinstance(image, Mapping) else image
            parsed = cls._url(candidate)
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def _url(value: object) -> HttpUrl | None:
        candidate = _clean_string(value)
        if not candidate:
            return None
        try:
            return _HTTP_URL.validate_python(candidate)
        except ValidationError:
            return None

    @staticmethod
    def _business_status(value: object) -> BusinessStatus | None:
        candidate = _clean_string(value)
        if candidate is None:
            return None
        try:
            return BusinessStatus(candidate.lower())
        except ValueError:
            return None

    @staticmethod
    def _city_id(city: str) -> str | None:
        normalized = city.casefold()
        if normalized == "đà nẵng".casefold():
            return "city_da_nang"
        if normalized == "quy nhơn".casefold():
            return "city_quy_nhon"
        return None

    def _aware_now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("bootstrap clock must return an aware datetime")
        return value

    @staticmethod
    def _run_id(at: datetime) -> str:
        timestamp = at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"master-place-bootstrap-{timestamp}-{uuid4().hex[:8]}"


def _required_string(record: Mapping[str, object], field_name: str) -> str:
    value = _clean_string(record.get(field_name))
    if not value:
        raise ValueError(f"master record requires {field_name}")
    return value


def _clean_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _has_value(value: object) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _parse_datetime(value: object) -> datetime | None:
    candidate = _clean_string(value)
    if not candidate:
        return None
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _publish_new_file(destination: Path, content: str) -> None:
    """Atomically publish complete content and never replace an existing path."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        # A hard link publishes the already-fsynced inode atomically and fails
        # if destination exists. It therefore provides both complete reads and
        # seed-only semantics without a check-then-replace race.
        os.link(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()

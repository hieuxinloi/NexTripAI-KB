from __future__ import annotations

import json
import math
import os
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from nextrip_pipeline.publishing._file_lock import destination_file_lock
from nextrip_pipeline.schemas import (
    BusinessStatus,
    GeoPoint,
    GoogleMapsPlaceObservation,
    NexTripModel,
)

from .dataset import (
    CanonicalActiveDataset,
    CanonicalActivePlaceRecord,
    CanonicalCoordinates,
)
from .models import stable_sha256
from .resolver import CanonicalIdentityResolver


class ReviewCorrectionInputError(ValueError):
    """Raised when a review overlay cannot be built without inventing data."""


class ReviewCorrectionAlreadyExistsError(FileExistsError):
    """Raised rather than replacing an immutable correction overlay."""


class ReviewCorrectionSkipReason(StrEnum):
    MISSING_GOOGLE_OBSERVATION = "missing_google_observation"
    INVALID_OBSERVED_IDENTITY = "invalid_observed_identity"
    NO_SOURCE_BACKED_FIELDS = "no_source_backed_fields"


_FIELD_ORDER = (
    "name",
    "address",
    "category",
    "business_status",
    "phone",
    "website_url",
    "location",
)
_PLACEHOLDER_NAMES = {
    "about",
    "directions",
    "google maps",
    "hours",
    "menu",
    "overview",
    "photos",
    "reviews",
}


def _observation_payload_hash(observation: GoogleMapsPlaceObservation) -> str:
    return stable_sha256(observation.model_dump(mode="json"))


class GoogleMapsCorrectionProvenance(NexTripModel):
    source_id: str = Field(pattern=r"^google-maps-web$")
    source_record_id: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    source_url: HttpUrl
    observed_at: AwareDatetime
    source_observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def _correction_payload(
    *,
    group_id: str,
    place_id: str,
    corrected_fields: list[str],
    name: str | None,
    address: str | None,
    category: str | None,
    business_status: BusinessStatus | None,
    phone: str | None,
    website_url: HttpUrl | None,
    location: GeoPoint | None,
    provenance: GoogleMapsCorrectionProvenance,
) -> dict[str, object]:
    return {
        "group_id": group_id,
        "place_id": place_id,
        "identity_status": "review",
        "corrected_fields": corrected_fields,
        "name": name,
        "address": address,
        "category": category,
        "business_status": (
            business_status.value if business_status is not None else None
        ),
        "phone": phone,
        "website_url": str(website_url) if website_url is not None else None,
        "location": location.model_dump(mode="json") if location else None,
        "provenance": provenance.model_dump(mode="json"),
    }


class CanonicalReviewPlaceCorrection(NexTripModel):
    """A source-backed field overlay that does not resolve canonical identity."""

    correction_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    group_id: str = Field(min_length=1)
    place_id: str = Field(min_length=1)
    identity_status: str = Field(default="review", pattern=r"^review$")
    corrected_fields: list[str] = Field(min_length=1)
    name: str | None = None
    address: str | None = None
    category: str | None = None
    business_status: BusinessStatus | None = None
    phone: str | None = None
    website_url: HttpUrl | None = None
    location: GeoPoint | None = None
    provenance: GoogleMapsCorrectionProvenance

    @model_validator(mode="after")
    def validate_correction(self) -> CanonicalReviewPlaceCorrection:
        present = {
            "name": self.name is not None,
            "address": self.address is not None,
            "category": self.category is not None,
            "business_status": self.business_status is not None,
            "phone": self.phone is not None,
            "website_url": self.website_url is not None,
            "location": self.location is not None,
        }
        expected_fields = [field for field in _FIELD_ORDER if present[field]]
        if self.corrected_fields != expected_fields:
            raise ValueError(
                "corrected_fields must exactly describe source-backed values"
            )
        if self.location is not None and not _is_google_sourced_location(
            self.location
        ):
            raise ValueError("correction location must be sourced by google-maps-web")
        payload = _correction_payload(
            group_id=self.group_id,
            place_id=self.place_id,
            corrected_fields=self.corrected_fields,
            name=self.name,
            address=self.address,
            category=self.category,
            business_status=self.business_status,
            phone=self.phone,
            website_url=self.website_url,
            location=self.location,
            provenance=self.provenance,
        )
        if self.correction_hash != stable_sha256(payload):
            raise ValueError("correction_hash does not match correction content")
        return self


class CanonicalReviewCorrectionSkip(NexTripModel):
    place_id: str = Field(min_length=1)
    reason: ReviewCorrectionSkipReason
    observation_id: str | None = Field(default=None, min_length=1)


class CanonicalReviewCorrectionGroup(NexTripModel):
    group_id: str = Field(min_length=1)
    place_ids: list[str] = Field(min_length=2)
    identity_status: str = Field(default="review", pattern=r"^review$")
    corrections: list[CanonicalReviewPlaceCorrection] = Field(default_factory=list)
    skips: list[CanonicalReviewCorrectionSkip] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_group(self) -> CanonicalReviewCorrectionGroup:
        if self.place_ids != sorted(set(self.place_ids)):
            raise ValueError("review group place_ids must be unique and sorted")
        if self.corrections != sorted(
            self.corrections, key=lambda item: item.place_id
        ):
            raise ValueError("review group corrections must be sorted")
        if self.skips != sorted(self.skips, key=lambda item: item.place_id):
            raise ValueError("review group skips must be sorted")
        outcomes = [item.place_id for item in self.corrections] + [
            item.place_id for item in self.skips
        ]
        if sorted(outcomes) != self.place_ids or len(outcomes) != len(set(outcomes)):
            raise ValueError("every review place must have exactly one outcome")
        if any(item.group_id != self.group_id for item in self.corrections):
            raise ValueError("correction belongs to another review group")
        return self


def _overlay_payload(
    groups: list[CanonicalReviewCorrectionGroup],
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "identity_status": "review",
        "groups": [item.model_dump(mode="json") for item in groups],
    }


class CanonicalReviewCorrectionOverlay(NexTripModel):
    """Immutable corrections for unresolved groups; never a merge decision."""

    schema_version: str = "1.0.0"
    overlay_id: str = Field(min_length=1)
    overlay_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    identity_status: str = Field(default="review", pattern=r"^review$")
    group_count: int = Field(ge=0)
    correction_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    groups: list[CanonicalReviewCorrectionGroup]

    @model_validator(mode="after")
    def validate_overlay(self) -> CanonicalReviewCorrectionOverlay:
        if self.groups != sorted(self.groups, key=lambda item: item.group_id):
            raise ValueError("review correction groups must be sorted")
        if len({item.group_id for item in self.groups}) != len(self.groups):
            raise ValueError("review correction group IDs must be unique")
        place_ids = [place_id for item in self.groups for place_id in item.place_ids]
        if len(place_ids) != len(set(place_ids)):
            raise ValueError("a place cannot occur in multiple review groups")
        correction_count = sum(len(item.corrections) for item in self.groups)
        skipped_count = sum(len(item.skips) for item in self.groups)
        if (
            self.group_count != len(self.groups)
            or self.correction_count != correction_count
            or self.skipped_count != skipped_count
        ):
            raise ValueError("review correction overlay counts do not match groups")
        payload = _overlay_payload(self.groups)
        expected_hash = stable_sha256(payload)
        if self.overlay_hash != expected_hash:
            raise ValueError("overlay_hash does not match overlay content")
        if self.overlay_id != f"canonical-review-corrections-{expected_hash[:20]}":
            raise ValueError("overlay_id does not match overlay_hash")
        return self


def build_canonical_review_correction_overlay(
    review_groups: Mapping[str, Sequence[str]],
    google_observations: Iterable[GoogleMapsPlaceObservation],
) -> CanonicalReviewCorrectionOverlay:
    """Project the latest clean Google observation for every review-place ID.

    This builder deliberately does not mutate master data and does not change
    any group's canonical decision. A malformed Maps capture is skipped as a
    whole. Master/fallback coordinates are omitted even when other fields from
    the same observation are retained.
    """

    normalized_groups = {
        str(group_id).strip(): sorted({str(value).strip() for value in place_ids})
        for group_id, place_ids in review_groups.items()
    }
    if any(not group_id for group_id in normalized_groups):
        raise ReviewCorrectionInputError("review group IDs must not be blank")
    for group_id, place_ids in normalized_groups.items():
        if len(place_ids) < 2 or any(not place_id for place_id in place_ids):
            raise ReviewCorrectionInputError(
                f"{group_id}: review group requires at least two non-empty IDs"
            )
    all_place_ids = [value for values in normalized_groups.values() for value in values]
    if len(all_place_ids) != len(set(all_place_ids)):
        raise ReviewCorrectionInputError("place IDs must be unique across groups")

    latest = _latest_observations(google_observations)
    groups: list[CanonicalReviewCorrectionGroup] = []
    for group_id in sorted(normalized_groups):
        corrections: list[CanonicalReviewPlaceCorrection] = []
        skips: list[CanonicalReviewCorrectionSkip] = []
        for place_id in normalized_groups[group_id]:
            observation = latest.get(place_id)
            if observation is None:
                skips.append(
                    CanonicalReviewCorrectionSkip(
                        place_id=place_id,
                        reason=ReviewCorrectionSkipReason.MISSING_GOOGLE_OBSERVATION,
                    )
                )
                continue
            if not _has_clean_observed_identity(observation):
                skips.append(
                    CanonicalReviewCorrectionSkip(
                        place_id=place_id,
                        reason=ReviewCorrectionSkipReason.INVALID_OBSERVED_IDENTITY,
                        observation_id=observation.observation_id,
                    )
                )
                continue
            correction = _build_place_correction(group_id, observation)
            if correction is None:
                skips.append(
                    CanonicalReviewCorrectionSkip(
                        place_id=place_id,
                        reason=ReviewCorrectionSkipReason.NO_SOURCE_BACKED_FIELDS,
                        observation_id=observation.observation_id,
                    )
                )
            else:
                corrections.append(correction)
        groups.append(
            CanonicalReviewCorrectionGroup(
                group_id=group_id,
                place_ids=normalized_groups[group_id],
                corrections=sorted(corrections, key=lambda item: item.place_id),
                skips=sorted(skips, key=lambda item: item.place_id),
            )
        )

    payload = _overlay_payload(groups)
    overlay_hash = stable_sha256(payload)
    return CanonicalReviewCorrectionOverlay(
        overlay_id=f"canonical-review-corrections-{overlay_hash[:20]}",
        overlay_hash=overlay_hash,
        group_count=len(groups),
        correction_count=sum(len(item.corrections) for item in groups),
        skipped_count=sum(len(item.skips) for item in groups),
        groups=groups,
    )


def apply_review_corrections(
    dataset: CanonicalActiveDataset,
    overlay: CanonicalReviewCorrectionOverlay,
    resolver: CanonicalIdentityResolver | None = None,
) -> CanonicalActiveDataset:
    """Return a new canonical dataset with a correction overlay applied.

    Neither input is mutated. Review identity remains unresolved: this helper
    updates source-backed place fields only and records overlay provenance in
    ``data.review_correction``. Skipped observations leave their records intact.
    Record and dataset hashes are recomputed from the resulting content.
    """

    if resolver is None:
        corrections = {
            correction.place_id: correction
            for group in overlay.groups
            for correction in group.corrections
        }
        source_legacy_ids: dict[str, str] = {}
    else:
        corrections, source_legacy_ids = _resolve_corrections(overlay, resolver)
    record_ids = {record.place_id for record in dataset.records}
    missing = sorted(set(corrections) - record_ids)
    if missing:
        raise ReviewCorrectionInputError(
            "correction overlay references missing canonical records: "
            + ", ".join(missing)
        )

    records = [
        _apply_place_correction(
            record,
            corrections[record.place_id],
            source_legacy_place_id=source_legacy_ids.get(record.place_id),
        )
        if record.place_id in corrections
        else CanonicalActivePlaceRecord.model_validate_json(record.model_dump_json())
        for record in dataset.records
    ]
    dataset_payload = {
        "schema_version": dataset.schema_version,
        "manifest_id": dataset.manifest_id,
        "manifest_hash": dataset.manifest_hash,
        "records": [item.model_dump(mode="json") for item in records],
        "report": dataset.report.model_dump(mode="json"),
    }
    dataset_hash = stable_sha256(dataset_payload)
    return CanonicalActiveDataset(
        schema_version=dataset.schema_version,
        dataset_id=f"canonical-active-{dataset_hash[:20]}",
        dataset_hash=dataset_hash,
        manifest_id=dataset.manifest_id,
        manifest_hash=dataset.manifest_hash,
        records=records,
        report=dataset.report,
    )


def _apply_place_correction(
    record: CanonicalActivePlaceRecord,
    correction: CanonicalReviewPlaceCorrection,
    *,
    source_legacy_place_id: str | None = None,
) -> CanonicalActivePlaceRecord:
    data = deepcopy(record.data)
    name = correction.name or record.name
    aliases = list(record.aliases)
    if correction.name is not None and _identity_key(record.name) != _identity_key(name):
        aliases.append(record.name)
    aliases = _canonical_aliases(aliases, name)
    address = correction.address or record.address
    phone = correction.phone or record.phone
    website_url = correction.website_url or record.website_url
    coordinates = record.coordinates
    if correction.location is not None:
        coordinates = CanonicalCoordinates(
            lat=correction.location.latitude,
            lng=correction.location.longitude,
            source="google-maps-web",
        )

    data["name"] = name
    data["aliases"] = aliases
    if correction.address is not None:
        data["address"] = correction.address
    if correction.category is not None:
        # Keep the source taxonomy separate from the canonical domain category.
        data["google_maps_category"] = correction.category
    if correction.business_status is not None:
        data["business_status"] = correction.business_status.value
    if correction.phone is not None:
        data["phone"] = correction.phone
    if correction.website_url is not None:
        data["website_url"] = str(correction.website_url)
    if correction.location is not None:
        data["coordinates"] = {
            "lat": correction.location.latitude,
            "lng": correction.location.longitude,
            "source": "google-maps-web",
        }
    data["review_correction"] = {
        "identity_status": correction.identity_status,
        "group_id": correction.group_id,
        "correction_hash": correction.correction_hash,
        "corrected_fields": correction.corrected_fields,
        "source_id": correction.provenance.source_id,
        "source_record_id": correction.provenance.source_record_id,
        "observation_id": correction.provenance.observation_id,
        "run_id": correction.provenance.run_id,
        "source_url": str(correction.provenance.source_url),
        "observed_at": correction.provenance.observed_at.isoformat(),
        "source_observation_hash": correction.provenance.source_observation_hash,
    }
    if source_legacy_place_id is not None:
        data["review_correction"]["source_legacy_place_id"] = source_legacy_place_id

    values = record.model_dump(mode="json")
    values.update(
        {
            "name": name,
            "aliases": aliases,
            "address": address,
            "coordinates": coordinates.model_dump(mode="json"),
            "phone": phone,
            "website_url": (
                str(website_url) if website_url is not None else None
            ),
            "data": data,
        }
    )
    values.pop("record_hash")
    return CanonicalActivePlaceRecord(
        record_hash=stable_sha256(values),
        **values,
    )


def _resolve_corrections(
    overlay: CanonicalReviewCorrectionOverlay,
    resolver: CanonicalIdentityResolver,
) -> tuple[dict[str, CanonicalReviewPlaceCorrection], dict[str, str]]:
    by_canonical_id: dict[str, list[CanonicalReviewPlaceCorrection]] = defaultdict(list)
    unknown_ids: list[str] = []
    for group in overlay.groups:
        for correction in group.corrections:
            canonical_id = resolver.resolve(correction.place_id)
            if canonical_id is None:
                if resolver.is_quarantined(correction.place_id):
                    # The immutable correction remains valid evidence for the
                    # archived duplicate corpus, but it must not be applied to
                    # an active-only dataset.
                    continue
                unknown_ids.append(correction.place_id)
                continue
            by_canonical_id[canonical_id].append(correction)
    if unknown_ids:
        raise ReviewCorrectionInputError(
            "correction overlay references identities missing from the resolver: "
            + ", ".join(sorted(unknown_ids))
        )

    selected: dict[str, CanonicalReviewPlaceCorrection] = {}
    source_legacy_ids: dict[str, str] = {}
    for canonical_id in sorted(by_canonical_id):
        candidates = by_canonical_id[canonical_id]
        _reject_conflicting_corrections(canonical_id, candidates)
        correction = min(
            candidates,
            key=lambda item: (
                -len(item.corrected_fields),
                item.place_id != canonical_id,
                -item.provenance.observed_at.timestamp(),
                item.correction_hash,
            ),
        )
        selected[canonical_id] = correction
        source_legacy_ids[canonical_id] = correction.place_id
    return selected, source_legacy_ids


def _reject_conflicting_corrections(
    canonical_id: str,
    corrections: Sequence[CanonicalReviewPlaceCorrection],
) -> None:
    conflicts: set[str] = set()
    for index, left in enumerate(corrections):
        for right in corrections[index + 1 :]:
            common_fields = set(left.corrected_fields) & set(right.corrected_fields)
            conflicts.update(
                field
                for field in common_fields
                if not _materially_equal(
                    field,
                    getattr(left, field),
                    getattr(right, field),
                )
            )
    if conflicts:
        source_ids = ", ".join(sorted(item.place_id for item in corrections))
        raise ReviewCorrectionInputError(
            f"corrections resolving to {canonical_id} conflict on "
            f"{', '.join(sorted(conflicts))}: {source_ids}"
        )


def _materially_equal(field: str, left: object, right: object) -> bool:
    if field in {"name", "address", "category"}:
        return _comparison_text(str(left)) == _comparison_text(str(right))
    if field == "phone":
        return "".join(character for character in str(left) if character.isdigit()) == (
            "".join(character for character in str(right) if character.isdigit())
        )
    if field == "website_url":
        return str(left).rstrip("/") == str(right).rstrip("/")
    if field == "location":
        assert isinstance(left, GeoPoint)
        assert isinstance(right, GeoPoint)
        return _location_distance_metres(left, right) <= 25
    return left == right


def _comparison_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(
        character
        for character in normalized
        if not unicodedata.combining(character) and character.isalnum()
    )


def _location_distance_metres(left: GeoPoint, right: GeoPoint) -> float:
    latitude_1 = math.radians(left.latitude)
    latitude_2 = math.radians(right.latitude)
    latitude_delta = latitude_2 - latitude_1
    longitude_delta = math.radians(right.longitude - left.longitude)
    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(latitude_1)
        * math.cos(latitude_2)
        * math.sin(longitude_delta / 2) ** 2
    )
    return 6_371_000 * 2 * math.asin(min(1.0, math.sqrt(haversine)))


def _canonical_aliases(values: Sequence[str], name: str) -> list[str]:
    by_key: dict[str, str] = {}
    name_key = _identity_key(name)
    for value in values:
        cleaned = " ".join(value.split())
        key = _identity_key(cleaned)
        if cleaned and key != name_key:
            by_key.setdefault(key, cleaned)
    return [by_key[key] for key in sorted(by_key)]


def _latest_observations(
    observations: Iterable[GoogleMapsPlaceObservation],
) -> dict[str, GoogleMapsPlaceObservation]:
    latest: dict[str, GoogleMapsPlaceObservation] = {}
    for observation in observations:
        if observation.source_id != "google-maps-web":
            continue
        current = latest.get(observation.place_id)
        if current is None or (
            observation.observed_at,
            observation.observation_id,
        ) > (current.observed_at, current.observation_id):
            latest[observation.place_id] = observation
    return latest


def _has_clean_observed_identity(observation: GoogleMapsPlaceObservation) -> bool:
    name = _optional_text(observation.name)
    if name is None or _identity_key(name) in _PLACEHOLDER_NAMES:
        return False
    return bool(_optional_text(observation.address) or _optional_text(observation.category))


def _build_place_correction(
    group_id: str,
    observation: GoogleMapsPlaceObservation,
) -> CanonicalReviewPlaceCorrection | None:
    name = _optional_text(observation.name)
    address = _optional_text(observation.address)
    category = _optional_text(observation.category)
    phone = _optional_text(observation.phone)
    website_url = observation.website_url
    business_status = (
        observation.business_status
        if observation.business_status is not BusinessStatus.UNKNOWN
        else None
    )
    location = (
        observation.location
        if observation.location is not None
        and _is_google_sourced_location(observation.location)
        else None
    )
    values = {
        "name": name,
        "address": address,
        "category": category,
        "business_status": business_status,
        "phone": phone,
        "website_url": website_url,
        "location": location,
    }
    corrected_fields = [field for field in _FIELD_ORDER if values[field] is not None]
    if not corrected_fields:
        return None
    provenance = GoogleMapsCorrectionProvenance(
        source_id=observation.source_id,
        source_record_id=observation.source_record_id,
        observation_id=observation.observation_id,
        run_id=observation.run_id,
        source_url=observation.source_url,
        observed_at=observation.observed_at,
        source_observation_hash=_observation_payload_hash(observation),
    )
    payload = _correction_payload(
        group_id=group_id,
        place_id=observation.place_id,
        corrected_fields=corrected_fields,
        name=name,
        address=address,
        category=category,
        business_status=business_status,
        phone=phone,
        website_url=website_url,
        location=location,
        provenance=provenance,
    )
    return CanonicalReviewPlaceCorrection(
        correction_hash=stable_sha256(payload),
        **payload,
    )


def _is_google_sourced_location(location: GeoPoint) -> bool:
    source = (location.source or "").strip().casefold()
    accuracy = (location.accuracy or "").strip().casefold()
    return source == "google-maps-web" and "fallback" not in accuracy


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _identity_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(
        character for character in normalized if not unicodedata.combining(character)
    ).strip()


class CanonicalReviewCorrectionWriter:
    """Write one content-addressed correction overlay without replacement."""

    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root)

    def destination_for(self, overlay: CanonicalReviewCorrectionOverlay) -> Path:
        return (
            self.output_root
            / f"overlay={quote(overlay.overlay_id, safe='-_.')}"
            / "canonical-review-corrections.json"
        )

    def write(self, overlay: CanonicalReviewCorrectionOverlay) -> Path:
        validated = CanonicalReviewCorrectionOverlay.model_validate_json(
            overlay.model_dump_json()
        )
        destination = self.destination_for(validated)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination_file_lock(destination):
            if destination.exists():
                try:
                    existing = CanonicalReviewCorrectionOverlay.model_validate_json(
                        destination.read_bytes()
                    )
                except (OSError, TypeError, ValueError) as error:
                    raise ReviewCorrectionAlreadyExistsError(
                        f"immutable correction overlay is invalid: {destination}"
                    ) from error
                if existing.overlay_hash == validated.overlay_hash:
                    return destination
                raise ReviewCorrectionAlreadyExistsError(
                    f"immutable correction overlay already exists: {destination}"
                )
            serialized = (
                json.dumps(
                    validated.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                file.write(serialized)
                file.flush()
                os.fsync(file.fileno())
        return destination


def read_canonical_review_correction_overlay(
    path: str | Path,
) -> CanonicalReviewCorrectionOverlay:
    return CanonicalReviewCorrectionOverlay.model_validate_json(Path(path).read_bytes())

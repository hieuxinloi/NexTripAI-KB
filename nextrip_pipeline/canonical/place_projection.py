from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import HttpUrl, TypeAdapter, ValidationError

from nextrip_pipeline.canonical.dataset import (
    CanonicalActiveDataset,
    CanonicalActivePlaceRecord,
    CanonicalRecordSource,
)
from nextrip_pipeline.schemas import (
    BusinessStatus,
    CurrentPlaceOpeningHours,
    CurrentPlaceProvenance,
    CurrentPlaceSnapshot,
    GeoPoint,
    OpeningStatusObservation,
    VerificationStatus,
    WeeklyOpeningScheduleObservation,
)


CANONICAL_PLACE_SOURCE_ID = "canonical-active-dataset"
_URL_ADAPTER = TypeAdapter(HttpUrl)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def project_canonical_dataset_places(
    dataset: CanonicalActiveDataset,
) -> dict[str, CurrentPlaceSnapshot]:
    """Project an immutable canonical dataset into the existing place contract.

    ``CurrentPlaceSnapshot`` remains the public HTTP/MCP compatibility model, but
    the returned values are derived exclusively from the content-addressed
    canonical dataset. Time-dependent price, availability, and daily-opening
    observations are deliberately not merged here.
    """

    projected = {
        record.place_id: project_canonical_place(
            record,
            dataset_id=dataset.dataset_id,
        )
        for record in dataset.records
    }
    if len(projected) != len(dataset.records):  # defensive; dataset also validates it
        raise ValueError("canonical dataset contains duplicate place IDs")
    return projected


def project_canonical_place(
    record: CanonicalActivePlaceRecord,
    *,
    dataset_id: str,
) -> CurrentPlaceSnapshot:
    """Convert one canonical record without consulting mutable local projections."""

    data = record.data
    observed_at = _record_timestamp(data)
    google_refresh = _google_refresh(data)
    source_id = (
        _clean_string(google_refresh.get("source_id"))
        if google_refresh is not None
        else None
    ) or CANONICAL_PLACE_SOURCE_ID
    source_label = f"{source_id}:{dataset_id}"
    opening_hours = _opening_hours(data.get("opening_hours"))
    opening = _daily_opening(data)
    weekly_opening = _weekly_opening(data)
    category = _clean_string(data.get("google_maps_category")) or _clean_string(
        data.get("category")
    )
    business_status = _business_status(data.get("business_status"))
    cover_image_url = _cover_image_url(data)
    price_level = _google_price_level(data)
    source_url = _source_url(data)
    location = GeoPoint(
        latitude=record.coordinates.lat,
        longitude=record.coordinates.lng,
        accuracy="canonical_active_dataset",
        source=record.coordinates.source,
        verified_at=observed_at,
    )
    values: dict[str, object | None] = {
        "name": record.name,
        "category": category,
        "address": record.address,
        "phone": record.phone,
        "website_url": record.website_url,
        "location": location,
        "business_status": business_status,
        "opening": opening,
        "weekly_opening": weekly_opening,
        "opening_hours": opening_hours,
        "cover_image_url": cover_image_url,
        "price_level": price_level,
    }
    return CurrentPlaceSnapshot(
        place_id=record.place_id,
        entity_type=record.primary_type,
        city=record.city,
        city_id=record.city_id,
        name=record.name,
        category=category,
        address=record.address,
        phone=record.phone,
        website_url=record.website_url,
        location=location,
        business_status=business_status,
        opening=opening,
        weekly_opening=weekly_opening,
        opening_hours=opening_hours,
        cover_image_url=cover_image_url,
        price_level=price_level,
        field_sources={
            field_name: source_label
            for field_name, value in values.items()
            if _has_value(value)
        },
        provenance=CurrentPlaceProvenance(
            mapping_id=None,
            observation_id=(
                _clean_string(google_refresh.get("observation_id"))
                if google_refresh is not None
                else None
            ),
            decision_id=(
                _clean_string(google_refresh.get("decision_id"))
                if google_refresh is not None
                else None
            ),
            run_id=(
                _clean_string(google_refresh.get("run_id"))
                if google_refresh is not None
                else None
            )
            or dataset_id,
            source_record_id=(
                _clean_string(google_refresh.get("source_record_id"))
                if google_refresh is not None
                else None
            )
            or f"canonical-record:{record.record_hash}",
            source_id=source_id,
            source_url=source_url,
            source_file=f"canonical-dataset:{dataset_id}",
            verification_status=(
                opening.verification_status
                if opening is not None and google_refresh is not None
                else _verification_status(record, data)
            ),
            observed_at=observed_at,
        ),
        updated_at=observed_at or _EPOCH,
        stale_after=(
            observed_at + timedelta(days=1)
            if opening is not None and google_refresh is not None and observed_at
            else None
        ),
    )


def _record_timestamp(data: Mapping[str, Any]) -> datetime | None:
    source = data.get("source")
    candidates = (
        data.get("last_verified"),
        data.get("last_updated"),
        source.get("crawled_at") if isinstance(source, Mapping) else None,
    )
    for candidate in candidates:
        parsed = _parse_datetime(candidate)
        if parsed is not None:
            return parsed
    return None


def _parse_datetime(value: object) -> datetime | None:
    candidate = _clean_string(value)
    if candidate is None:
        return None
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _opening_hours(value: object) -> CurrentPlaceOpeningHours | None:
    if not isinstance(value, Mapping) or not value:
        return None
    opens_at = _clean_string(value.get("opens_at")) or _clean_string(value.get("open"))
    closes_at = _clean_string(value.get("closes_at")) or _clean_string(
        value.get("close")
    )
    raw_closed_days = value.get("closed_days", [])
    if isinstance(raw_closed_days, str):
        closed_days: list[str] | str = raw_closed_days.strip()
    elif isinstance(raw_closed_days, list):
        closed_days = [
            cleaned
            for item in raw_closed_days
            if (cleaned := _clean_string(item)) is not None
        ]
    else:
        closed_days = []
    note = _clean_string(value.get("note"))
    if not any((opens_at, closes_at, closed_days, note)):
        return None
    return CurrentPlaceOpeningHours(
        opens_at=opens_at,
        closes_at=closes_at,
        closed_days=closed_days,
        note=note,
    )


def _weekly_opening(data: Mapping[str, Any]) -> WeeklyOpeningScheduleObservation | None:
    value = (
        data.get("google_maps_weekly_opening")
        or data.get("weekly_opening")
        or data.get("weekly_opening_schedule")
    )
    if not isinstance(value, Mapping):
        return None
    try:
        return WeeklyOpeningScheduleObservation.model_validate(value)
    except (TypeError, ValidationError, ValueError):
        return None


def _daily_opening(data: Mapping[str, Any]) -> OpeningStatusObservation | None:
    value = data.get("opening_status")
    if not isinstance(value, Mapping):
        return None
    try:
        return OpeningStatusObservation.model_validate(value)
    except (TypeError, ValidationError, ValueError):
        return None


def _business_status(value: object) -> BusinessStatus | None:
    candidate = _clean_string(value)
    if candidate is None:
        return None
    try:
        return BusinessStatus(candidate.casefold())
    except ValueError:
        return None


def _price_level(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= 4 else None


def _google_price_level(data: Mapping[str, Any]) -> int | None:
    evidence = data.get("google_maps_price")
    if isinstance(evidence, Mapping):
        observed = _price_level(evidence.get("level"))
        if observed is not None:
            return observed
    return _price_level(data.get("price_level"))


def _cover_image_url(data: Mapping[str, Any]) -> HttpUrl | None:
    direct = _url(data.get("cover_image_url"))
    if direct is not None:
        return direct
    images = data.get("images")
    if not isinstance(images, list):
        return None
    for image in images:
        candidate = None
        if isinstance(image, Mapping):
            candidate = image.get("url") or image.get("image_url")
        elif isinstance(image, str):
            candidate = image
        parsed = _url(candidate)
        if parsed is not None:
            return parsed
    return None


def _source_url(data: Mapping[str, Any]) -> HttpUrl | None:
    google_refresh = data.get("google_maps_refresh")
    if isinstance(google_refresh, Mapping):
        parsed = _url(google_refresh.get("source_url"))
        if parsed is not None:
            return parsed
    source = data.get("source")
    if not isinstance(source, Mapping):
        return None
    return _url(source.get("url"))


def _google_refresh(data: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = data.get("google_maps_refresh")
    return value if isinstance(value, Mapping) else None


def _url(value: object) -> HttpUrl | None:
    if value is None:
        return None
    try:
        return _URL_ADAPTER.validate_python(value)
    except (TypeError, ValidationError, ValueError):
        return None


def _verification_status(
    record: CanonicalActivePlaceRecord,
    data: Mapping[str, Any],
) -> VerificationStatus:
    raw = _clean_string(data.get("verification_status"))
    if raw is not None:
        try:
            status = VerificationStatus(raw.casefold())
        except ValueError:
            status = None
        if status in {
            VerificationStatus.LEGACY_VERIFIED,
            VerificationStatus.AUTO_VERIFIED,
            VerificationStatus.AGENT_VERIFIED,
            VerificationStatus.HUMAN_VERIFIED,
        }:
            return status
    if record.provenance.source_kind is CanonicalRecordSource.APPROVED_REPLACEMENT:
        return VerificationStatus.HUMAN_VERIFIED
    return VerificationStatus.LEGACY_VERIFIED


def _clean_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _has_value(value: object) -> bool:
    return value is not None and value != "" and value != [] and value != {}

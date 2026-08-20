from __future__ import annotations

import json
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

from nextrip_pipeline.schemas import (
    ExternalEntityMapping,
    HotelPriceObservation,
    MappingStatus,
    Occupancy,
    OfferAvailability,
    RecordSubjectType,
    SourceRecord,
    VerificationStatus,
)


class HotelPriceNormalizationError(ValueError):
    """Base error for deterministic hotel-price normalization."""


class AccommodationNotMatchedError(HotelPriceNormalizationError):
    """Raised when the confirmed external accommodation is absent."""


class InvalidPriceError(HotelPriceNormalizationError):
    """Raised when a returned price cannot be parsed safely."""


class NormalizedRecordAlreadyExistsError(FileExistsError):
    """Raised when an immutable normalized observation already exists."""


def parse_price_amount(value: object, currency: str) -> Decimal:
    if not isinstance(value, str) or not value.strip():
        raise InvalidPriceError("price must be a non-empty formatted string")

    normalized_currency = currency.upper()
    if normalized_currency == "VND":
        digits = re.sub(r"[^0-9]", "", value)
        if not digits:
            raise InvalidPriceError(f"cannot parse VND price: {value!r}")
        amount = Decimal(digits)
    else:
        numeric = re.sub(r"[^0-9,.-]", "", value)
        if not numeric:
            raise InvalidPriceError(f"cannot parse price: {value!r}")
        if numeric.count(",") == 1 and numeric.count(".") == 0:
            numeric = numeric.replace(",", ".")
        elif numeric.count(".") > 1 and "," not in numeric:
            numeric = numeric.replace(".", "")
        else:
            numeric = numeric.replace(",", "")
        try:
            amount = Decimal(numeric)
        except InvalidOperation as error:
            raise InvalidPriceError(f"cannot parse price: {value!r}") from error

    if amount <= 0:
        raise InvalidPriceError("price must be greater than zero")
    return amount


class TrivagoMcpPriceNormalizer:
    """Converts official Trivago MCP structured output into observations."""

    source_id = "trivago-mcp"

    def normalize(
        self,
        record: SourceRecord,
        mapping: ExternalEntityMapping,
    ) -> list[HotelPriceObservation]:
        self._validate_provenance(record, mapping)
        request = self._require_dict(record.raw_payload.get("request"), "request")
        arguments = self._require_dict(request.get("arguments"), "request.arguments")
        accommodations = self._accommodations(record)
        matches = self._deduplicate_exact_rows(
            [
                item
                for item in accommodations
                if str(item.get("accommodation_id", "")) == mapping.external_id
            ]
        )
        if not matches:
            raise AccommodationNotMatchedError(
                f"Trivago accommodation {mapping.external_id!r} was not returned "
                f"for NexTrip hotel {mapping.entity_id!r}"
            )

        occupancy = Occupancy(
            adults=arguments.get("adults", 2),
            children=arguments.get("children", 0),
            rooms=arguments.get("rooms", 1),
        )
        children_ages = self._children_ages(arguments.get("children_ages"))
        if len(children_ages) != occupancy.children:
            raise HotelPriceNormalizationError(
                "request.arguments.children_ages must match children"
            )
        normalized = [
            self._normalize_offer(
                record,
                mapping,
                arguments,
                occupancy,
                children_ages,
                item,
            )
            for item in matches
        ]
        return self._select_best_seller_offers(normalized)

    def _validate_provenance(
        self,
        record: SourceRecord,
        mapping: ExternalEntityMapping,
    ) -> None:
        if record.source_id != self.source_id:
            raise HotelPriceNormalizationError("source record is not from Trivago MCP")
        if record.subject_type is not RecordSubjectType.HOTEL_PRICE:
            raise HotelPriceNormalizationError("source record is not a hotel price")
        if record.subject_id != mapping.entity_id:
            raise HotelPriceNormalizationError(
                "source record and mapping entity differ"
            )
        if mapping.source_id != self.source_id:
            raise HotelPriceNormalizationError("mapping is not for Trivago MCP")
        if mapping.status not in {MappingStatus.CONFIRMED, MappingStatus.AUTO_MATCHED}:
            raise HotelPriceNormalizationError(
                "mapping is not eligible for normalization"
            )

    def _accommodations(self, record: SourceRecord) -> list[dict[str, object]]:
        response = self._require_dict(record.raw_payload.get("response"), "response")
        result = self._require_dict(response.get("result"), "response.result")
        structured = self._require_dict(
            result.get("structuredContent"),
            "response.result.structuredContent",
        )
        raw_accommodations = structured.get("accommodations")
        if not isinstance(raw_accommodations, list):
            raise HotelPriceNormalizationError("accommodations must be a list")
        return [
            self._require_dict(item, "accommodations item")
            for item in raw_accommodations
        ]

    def _normalize_offer(
        self,
        record: SourceRecord,
        mapping: ExternalEntityMapping,
        arguments: dict[str, object],
        occupancy: Occupancy,
        children_ages: list[int],
        item: dict[str, object],
    ) -> HotelPriceObservation:
        external_id = self._required_text(item, "accommodation_id")
        currency = self._required_text(item, "currency").upper()
        nightly_text = self._required_text(item, "price_per_night")
        total_text = self._required_text(item, "price_per_stay")
        seller = self._required_text(item, "advertisers")
        nightly_amount = parse_price_amount(nightly_text, currency)
        total_amount = parse_price_amount(total_text, currency)
        seller_key = re.sub(r"[^a-z0-9]+", "-", seller.casefold()).strip("-")
        booking_url = str(item.get("accommodation_url") or "").replace("\\u0026", "&")

        return HotelPriceObservation(
            observation_id=(
                f"{record.source_record_id}:{external_id}:{seller_key or 'unknown'}"
            ),
            run_id=record.run_id,
            hotel_id=mapping.entity_id,
            offer_key=(
                f"{external_id}|{seller_key or 'unknown'}|"
                f"{occupancy.rooms}r-{occupancy.adults}a-{occupancy.children}c"
            ),
            source_record_id=record.source_record_id,
            source_id=record.source_id,
            mapping_id=mapping.mapping_id,
            external_id=external_id,
            seller=seller,
            room_type="unspecified",
            check_in=self._required_text(arguments, "arrival"),
            check_out=self._required_text(arguments, "departure"),
            occupancy=occupancy,
            children_ages=children_ages,
            currency=currency,
            amount=nightly_amount,
            nightly_amount=nightly_amount,
            total_amount=total_amount,
            booking_url=booking_url or None,
            availability=OfferAvailability.AVAILABLE,
            raw_text=f"{nightly_text} per night; {total_text} per stay",
            observed_at=record.crawled_at,
            verification_status=(
                VerificationStatus.AUTO_VERIFIED
                if mapping.status is MappingStatus.CONFIRMED
                else VerificationStatus.PENDING_REVIEW
            ),
        )

    @staticmethod
    def _deduplicate_exact_rows(
        rows: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        """Drop byte-equivalent provider duplicates without merging real offers."""

        unique: list[dict[str, object]] = []
        seen: set[str] = set()
        for row in rows:
            key = json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(row)
        return unique

    @classmethod
    def _select_best_seller_offers(
        cls,
        observations: list[HotelPriceObservation],
    ) -> list[HotelPriceObservation]:
        """Keep one deterministic lowest-total offer per seller identity.

        Trivago occasionally emits conflicting rows for the same accommodation
        and advertiser while exposing no room identifier that could safely make
        them separate offers. The immutable raw response retains every row; the
        normalized/current projection selects the cheapest complete row.
        """

        selected: dict[tuple[str | None, str, str], HotelPriceObservation] = {}
        for observation in observations:
            identity = (
                observation.external_id,
                " ".join((observation.seller or "").split()).casefold(),
                observation.currency,
            )
            current = selected.get(identity)
            candidate_key = (
                observation.total_amount or Decimal("Infinity"),
                observation.nightly_amount or Decimal("Infinity"),
                str(observation.booking_url or ""),
            )
            if current is None:
                selected[identity] = observation
                continue
            current_key = (
                current.total_amount or Decimal("Infinity"),
                current.nightly_amount or Decimal("Infinity"),
                str(current.booking_url or ""),
            )
            if candidate_key < current_key:
                selected[identity] = observation
        return list(selected.values())

    def _children_ages(self, value: object) -> list[int]:
        if value is None or value == "":
            return []

        raw_ages: list[object]
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            if not re.fullmatch(r"\d+(?:\s*[-,]\s*\d+)*", text):
                raise HotelPriceNormalizationError(
                    "request.arguments.children_ages has invalid format"
                )
            raw_ages = re.split(r"\s*[-,]\s*", text)
        elif isinstance(value, list):
            raw_ages = value
        else:
            raise HotelPriceNormalizationError(
                "request.arguments.children_ages must be a string or list"
            )

        children_ages: list[int] = []
        for raw_age in raw_ages:
            if isinstance(raw_age, bool):
                raise HotelPriceNormalizationError(
                    "request.arguments.children_ages must contain integers"
                )
            if isinstance(raw_age, int):
                age = raw_age
            elif isinstance(raw_age, str) and re.fullmatch(r"\d+", raw_age.strip()):
                age = int(raw_age)
            else:
                raise HotelPriceNormalizationError(
                    "request.arguments.children_ages must contain integers"
                )
            if age < 0 or age > 17:
                raise HotelPriceNormalizationError(
                    "children ages must be between 0 and 17"
                )
            children_ages.append(age)
        return sorted(children_ages)

    def _require_dict(self, value: object, field: str) -> dict[str, object]:
        if not isinstance(value, dict):
            raise HotelPriceNormalizationError(f"{field} must be an object")
        return value

    def _required_text(self, value: dict[str, object], field: str) -> str:
        item = value.get(field)
        if not isinstance(item, str) or not item.strip():
            raise HotelPriceNormalizationError(f"{field} must be a non-empty string")
        return item.strip()


class NormalizedHotelPriceWriter:
    """Writes normalized observations once using deterministic partitions."""

    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def destination_for(self, observation: HotelPriceObservation) -> Path:
        observed_date = observation.observed_at.date().isoformat()
        source_id = observation.source_id or "unknown"
        return (
            self.root_directory
            / "entity=hotel_price"
            / f"source={quote(source_id, safe='-_.')}"
            / f"date={observed_date}"
            / f"run={quote(observation.run_id, safe='-_.')}"
            / f"observation={quote(observation.observation_id, safe='-_.')}.json"
        )

    def write(self, observation: HotelPriceObservation) -> Path:
        destination = self.destination_for(observation)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = observation.model_dump_json(indent=2) + "\n"
        try:
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            )
        except FileExistsError as error:
            raise NormalizedRecordAlreadyExistsError(
                f"Normalized observation already exists: {destination}"
            ) from error
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        return destination

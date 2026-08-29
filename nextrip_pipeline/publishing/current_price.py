from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from pydantic import AwareDatetime, Field

from nextrip_pipeline.decision_gate import (
    HotelPriceDecision,
    HotelPriceDecisionStatus,
)
from nextrip_pipeline.schemas import HotelPriceObservation, NexTripModel, Occupancy

from ._file_lock import destination_file_lock


class OlderPriceObservationError(ValueError):
    """Raised when an older observation attempts to replace current price."""


class CurrentHotelPriceSnapshot(NexTripModel):
    hotel_id: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    observation: HotelPriceObservation
    updated_at: AwareDatetime
    stale_after: AwareDatetime

    def is_stale(self, at: datetime) -> bool:
        return at > self.stale_after


class CurrentHotelPriceWriter:
    """Publishes the latest PASS price for each exact hotel-offer context.

    Current prices are isolated by hotel, stay dates, occupancy, child ages, and
    a stable source/seller/offer identity. A new search therefore cannot
    overwrite a price for another stay or another seller. Legacy snapshots are
    still readable, but are never moved or deleted automatically.
    """

    def __init__(
        self,
        root_directory: str | Path,
        *,
        ttl: timedelta = timedelta(hours=5),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("hotel price TTL must be greater than zero")
        self.root_directory = Path(root_directory)
        self.ttl = ttl
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def publish(
        self,
        observation: HotelPriceObservation,
        decision: HotelPriceDecision,
    ) -> Path:
        if decision.status is not HotelPriceDecisionStatus.PASS:
            raise ValueError("only PASS hotel prices can become current")
        if (
            decision.observation_id != observation.observation_id
            or decision.hotel_id != observation.hotel_id
            or decision.run_id != observation.run_id
        ):
            raise ValueError("decision and observation do not match")

        destination = self.path_for(observation)
        with destination_file_lock(destination):
            current = self._read(destination)
            current_path = destination
            if current is None:
                pre_currency_path = self._pre_currency_context_path_for(observation)
                pre_currency = self._read(pre_currency_path)
                if pre_currency is not None and self._same_offer_context(
                    pre_currency.observation, observation
                ):
                    current = pre_currency
                    current_path = pre_currency_path
            if current is None:
                legacy_context_path = self._legacy_context_path_for(observation)
                legacy_context = self._read(legacy_context_path)
                if legacy_context is not None and self._same_offer_context(
                    legacy_context.observation, observation
                ):
                    current = legacy_context
                    current_path = legacy_context_path
            if current is None:
                legacy_path = self.legacy_path_for(observation.hotel_id)
                legacy = self._read(legacy_path)
                if legacy is not None and self._same_offer_context(
                    legacy.observation, observation
                ):
                    current = legacy
                    current_path = legacy_path
            if current is not None:
                if current.observation_id == observation.observation_id:
                    return current_path
                if current.observation.observed_at >= observation.observed_at:
                    raise OlderPriceObservationError(
                        "older hotel price cannot replace the current snapshot"
                    )

            snapshot = CurrentHotelPriceSnapshot(
                hotel_id=observation.hotel_id,
                observation_id=observation.observation_id,
                decision_id=decision.decision_id,
                observation=observation,
                updated_at=self.clock(),
                stale_after=observation.observed_at + self.ttl,
            )
            self._replace(destination, snapshot.model_dump_json(indent=2) + "\n")
            return destination

    def path_for(self, observation: HotelPriceObservation) -> Path:
        """Return the deterministic destination for an observation."""

        return self._context_path(
            hotel_id=observation.hotel_id,
            check_in=observation.check_in,
            check_out=observation.check_out,
            occupancy=observation.occupancy,
            children_ages=observation.children_ages,
            currency=observation.currency,
            source_id=observation.source_id,
            seller=observation.seller,
            offer_key=observation.offer_key,
        )

    def legacy_path_for(self, hotel_id: str) -> Path:
        """Return the path used by the original single-file writer."""

        return self.root_directory / f"hotel={quote(hotel_id, safe='-_.')}.json"

    def get_for(
        self,
        observation: HotelPriceObservation,
        *,
        include_legacy: bool = True,
    ) -> CurrentHotelPriceSnapshot | None:
        """Read the exact current offer represented by ``observation``."""

        snapshot = self._read(self.path_for(observation))
        if snapshot is not None or not include_legacy:
            return snapshot
        legacy_paths = (
            self._pre_currency_context_path_for(observation),
            self._legacy_context_path_for(observation),
            self.legacy_path_for(observation.hotel_id),
        )
        for legacy_path in legacy_paths:
            legacy = self._read(legacy_path)
            if legacy is not None and self._same_offer_context(
                legacy.observation, observation
            ):
                return legacy
        return None

    def get_latest(
        self,
        hotel_id: str,
        *,
        check_in: date | None = None,
        check_out: date | None = None,
        occupancy: Occupancy | None = None,
        children_ages: list[int] | None = None,
        currency: str | None = None,
        seller: str | None = None,
        offer_key: str | None = None,
        include_stale: bool = True,
        at: datetime | None = None,
    ) -> CurrentHotelPriceSnapshot | None:
        """Return the newest matching snapshot, including legacy data if present."""

        snapshots = self.list_snapshots(
            hotel_id=hotel_id,
            check_in=check_in,
            check_out=check_out,
            occupancy=occupancy,
            children_ages=children_ages,
            currency=currency,
            seller=seller,
            offer_key=offer_key,
            include_stale=include_stale,
            at=at,
        )
        return snapshots[0] if snapshots else None

    def list_snapshots(
        self,
        *,
        hotel_id: str | None = None,
        check_in: date | None = None,
        check_out: date | None = None,
        occupancy: Occupancy | None = None,
        children_ages: list[int] | None = None,
        currency: str | None = None,
        seller: str | None = None,
        offer_key: str | None = None,
        include_stale: bool = True,
        at: datetime | None = None,
    ) -> list[CurrentHotelPriceSnapshot]:
        """List current offers newest-first with optional downstream filters."""

        if not self.root_directory.exists():
            return []
        evaluated_at = at or self.clock()
        snapshots: dict[tuple[object, ...], CurrentHotelPriceSnapshot] = {}
        for path in self.root_directory.rglob("*.json"):
            if path.name.startswith("."):
                continue
            snapshot = self._read(path)
            if snapshot is None or not self._matches(
                snapshot,
                hotel_id=hotel_id,
                check_in=check_in,
                check_out=check_out,
                occupancy=occupancy,
                children_ages=children_ages,
                currency=currency,
                seller=seller,
                offer_key=offer_key,
            ):
                continue
            if not include_stale and snapshot.is_stale(evaluated_at):
                continue

            # Legacy and contextual copies can coexist during a non-destructive
            # transition. Expose only the newest observation for each exact offer.
            identity = self._context_identity(snapshot.observation)
            existing = snapshots.get(identity)
            if existing is None or (
                snapshot.observation.observed_at,
                snapshot.updated_at,
            ) > (
                existing.observation.observed_at,
                existing.updated_at,
            ):
                snapshots[identity] = snapshot

        return sorted(
            snapshots.values(),
            key=lambda item: (item.observation.observed_at, item.updated_at),
            reverse=True,
        )

    @classmethod
    def offer_identity_key(
        cls,
        *,
        source_id: str | None,
        seller: str | None,
        offer_key: str,
        currency: str | None = None,
    ) -> str:
        """Build a filesystem-safe, stable identity for one seller offer."""

        normalized_seller = cls._normalized_text(seller) or "unknown"
        normalized_source = cls._normalized_text(source_id) or "unknown"
        identity = {
            "source_id": normalized_source,
            "seller": normalized_seller,
            "offer_key": offer_key.strip(),
        }
        if currency is not None:
            identity["currency"] = currency.upper()
        canonical = json.dumps(
            identity,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        seller_slug = cls._slug(seller or "unknown")
        return f"{seller_slug}--{digest}"

    @staticmethod
    def occupancy_key(occupancy: Occupancy) -> str:
        return f"{occupancy.adults}a-{occupancy.children}c-{occupancy.rooms}r"

    @staticmethod
    def children_ages_key(children_ages: list[int]) -> str:
        if not children_ages:
            return "none"
        return "-".join(str(age) for age in sorted(children_ages))

    def _context_path(
        self,
        *,
        hotel_id: str,
        check_in: date,
        check_out: date,
        occupancy: Occupancy,
        children_ages: list[int] | None,
        currency: str | None,
        source_id: str | None,
        seller: str | None,
        offer_key: str,
    ) -> Path:
        identity = self.offer_identity_key(
            source_id=source_id,
            seller=seller,
            offer_key=offer_key,
            currency=currency,
        )
        context_directory = (
            self.root_directory
            / f"hotel={quote(hotel_id, safe='-_.')}"
            / f"checkin={check_in.isoformat()}"
            / f"checkout={check_out.isoformat()}"
            / f"occupancy={self.occupancy_key(occupancy)}"
        )
        if children_ages is not None:
            context_directory /= (
                f"children_ages={self.children_ages_key(children_ages)}"
            )
        if currency is not None:
            context_directory /= f"currency={quote(currency.upper(), safe='-_.')}"
        return context_directory / f"offer={identity}.json"

    def _pre_currency_context_path_for(
        self,
        observation: HotelPriceObservation,
    ) -> Path:
        """Return the child-aware contextual path used before currency isolation."""

        return self._context_path(
            hotel_id=observation.hotel_id,
            check_in=observation.check_in,
            check_out=observation.check_out,
            occupancy=observation.occupancy,
            children_ages=observation.children_ages,
            currency=None,
            source_id=observation.source_id,
            seller=observation.seller,
            offer_key=observation.offer_key,
        )

    def _legacy_context_path_for(
        self,
        observation: HotelPriceObservation,
    ) -> Path:
        """Return the pre-child-age contextual path for a read-only fallback."""

        return self._context_path(
            hotel_id=observation.hotel_id,
            check_in=observation.check_in,
            check_out=observation.check_out,
            occupancy=observation.occupancy,
            children_ages=None,
            currency=None,
            source_id=observation.source_id,
            seller=observation.seller,
            offer_key=observation.offer_key,
        )

    @classmethod
    def _same_offer_context(
        cls,
        left: HotelPriceObservation,
        right: HotelPriceObservation,
    ) -> bool:
        return (
            left.hotel_id == right.hotel_id
            and left.check_in == right.check_in
            and left.check_out == right.check_out
            and left.occupancy == right.occupancy
            and left.children_ages == right.children_ages
            and left.currency == right.currency
            and left.offer_key == right.offer_key
            and cls._normalized_text(left.seller) == cls._normalized_text(right.seller)
            and cls._normalized_text(left.source_id)
            == cls._normalized_text(right.source_id)
        )

    @classmethod
    def _context_identity(
        cls,
        observation: HotelPriceObservation,
    ) -> tuple[object, ...]:
        return (
            observation.hotel_id,
            observation.check_in,
            observation.check_out,
            observation.occupancy.adults,
            observation.occupancy.children,
            observation.occupancy.rooms,
            tuple(observation.children_ages),
            observation.currency,
            cls._normalized_text(observation.source_id),
            cls._normalized_text(observation.seller),
            observation.offer_key,
        )

    @classmethod
    def _matches(
        cls,
        snapshot: CurrentHotelPriceSnapshot,
        *,
        hotel_id: str | None,
        check_in: date | None,
        check_out: date | None,
        occupancy: Occupancy | None,
        children_ages: list[int] | None,
        currency: str | None,
        seller: str | None,
        offer_key: str | None,
    ) -> bool:
        observation = snapshot.observation
        return not (
            (hotel_id is not None and snapshot.hotel_id != hotel_id)
            or (check_in is not None and observation.check_in != check_in)
            or (check_out is not None and observation.check_out != check_out)
            or (occupancy is not None and observation.occupancy != occupancy)
            or (
                children_ages is not None
                and observation.children_ages != sorted(children_ages)
            )
            or (currency is not None and observation.currency != currency.upper())
            or (
                seller is not None
                and cls._normalized_text(observation.seller)
                != cls._normalized_text(seller)
            )
            or (offer_key is not None and observation.offer_key != offer_key)
        )

    @staticmethod
    def _normalized_text(value: str | None) -> str:
        return " ".join((value or "").split()).casefold()

    @staticmethod
    def _slug(value: str) -> str:
        ascii_value = (
            unicodedata.normalize("NFKD", value)
            .encode("ascii", "ignore")
            .decode("ascii")
        )
        slug = re.sub(r"[^a-z0-9]+", "-", ascii_value.casefold()).strip("-")
        return (slug or "unknown")[:48]

    @staticmethod
    def _read(destination: Path) -> CurrentHotelPriceSnapshot | None:
        if not destination.exists():
            return None
        return CurrentHotelPriceSnapshot.model_validate_json(
            destination.read_text(encoding="utf-8")
        )

    @staticmethod
    def _replace(destination: Path, content: str) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from pydantic import AwareDatetime, Field

from nextrip_pipeline.schemas import (
    HotelAvailabilityObservation,
    HotelAvailabilityStatus,
    NexTripModel,
    Occupancy,
    VerificationStatus,
)

from ._file_lock import destination_file_lock


class OlderAvailabilityObservationError(ValueError):
    """Raised when older evidence attempts to replace current availability."""


class CurrentHotelAvailabilitySnapshot(NexTripModel):
    hotel_id: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    observation: HotelAvailabilityObservation
    updated_at: AwareDatetime
    stale_after: AwareDatetime

    def is_stale(self, at: datetime) -> bool:
        return at > self.stale_after


class CurrentHotelAvailabilityWriter:
    """Stores the latest verified result for each exact hotel-search context."""

    _PUBLISHABLE_STATUSES = {
        VerificationStatus.LEGACY_VERIFIED,
        VerificationStatus.AUTO_VERIFIED,
        VerificationStatus.AGENT_VERIFIED,
        VerificationStatus.HUMAN_VERIFIED,
    }

    def __init__(
        self,
        root_directory: str | Path,
        *,
        ttl: timedelta = timedelta(hours=5),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("hotel availability TTL must be greater than zero")
        self.root_directory = Path(root_directory)
        self.ttl = ttl
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def publish(self, observation: HotelAvailabilityObservation) -> Path:
        if observation.verification_status not in self._PUBLISHABLE_STATUSES:
            raise ValueError("only verified hotel availability can become current")

        destination = self.path_for(observation)
        with destination_file_lock(destination):
            current = self._read(destination)
            if current is not None:
                if current.observation_id == observation.observation_id:
                    return destination
                if current.observation.observed_at >= observation.observed_at:
                    raise OlderAvailabilityObservationError(
                        "older hotel availability cannot replace the current snapshot"
                    )

            snapshot = CurrentHotelAvailabilitySnapshot(
                hotel_id=observation.hotel_id,
                observation_id=observation.observation_id,
                observation=observation,
                updated_at=self.clock(),
                stale_after=observation.observed_at + self.ttl,
            )
            self._replace(destination, snapshot.model_dump_json(indent=2) + "\n")
            return destination

    def path_for(self, observation: HotelAvailabilityObservation) -> Path:
        return (
            self.root_directory
            / f"hotel={quote(observation.hotel_id, safe='-_.')}"
            / f"checkin={observation.check_in.isoformat()}"
            / f"checkout={observation.check_out.isoformat()}"
            / f"nights={observation.nights}"
            / f"occupancy={self.occupancy_key(observation.occupancy)}"
            / f"children_ages={self.children_ages_key(observation.children_ages)}"
            / f"currency={observation.currency}"
            / f"source={quote(observation.source_id, safe='-_.')}.json"
        )

    def get_for(
        self, observation: HotelAvailabilityObservation
    ) -> CurrentHotelAvailabilitySnapshot | None:
        return self._read(self.path_for(observation))

    def get_latest(
        self,
        hotel_id: str,
        *,
        check_in: date | None = None,
        check_out: date | None = None,
        nights: int | None = None,
        occupancy: Occupancy | None = None,
        children_ages: list[int] | None = None,
        currency: str | None = None,
        source_id: str | None = None,
        status: HotelAvailabilityStatus | None = None,
        include_stale: bool = True,
        at: datetime | None = None,
    ) -> CurrentHotelAvailabilitySnapshot | None:
        snapshots = self.list_snapshots(
            hotel_id=hotel_id,
            check_in=check_in,
            check_out=check_out,
            nights=nights,
            occupancy=occupancy,
            children_ages=children_ages,
            currency=currency,
            source_id=source_id,
            status=status,
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
        nights: int | None = None,
        occupancy: Occupancy | None = None,
        children_ages: list[int] | None = None,
        currency: str | None = None,
        source_id: str | None = None,
        status: HotelAvailabilityStatus | None = None,
        include_stale: bool = True,
        at: datetime | None = None,
    ) -> list[CurrentHotelAvailabilitySnapshot]:
        if not self.root_directory.exists():
            return []

        evaluated_at = at or self.clock()
        snapshots: list[CurrentHotelAvailabilitySnapshot] = []
        for path in self.root_directory.rglob("*.json"):
            if path.name.startswith("."):
                continue
            snapshot = self._read(path)
            if snapshot is None or not self._matches(
                snapshot,
                hotel_id=hotel_id,
                check_in=check_in,
                check_out=check_out,
                nights=nights,
                occupancy=occupancy,
                children_ages=children_ages,
                currency=currency,
                source_id=source_id,
                status=status,
            ):
                continue
            if not include_stale and snapshot.is_stale(evaluated_at):
                continue
            snapshots.append(snapshot)

        return sorted(
            snapshots,
            key=lambda item: (item.observation.observed_at, item.updated_at),
            reverse=True,
        )

    @staticmethod
    def occupancy_key(occupancy: Occupancy) -> str:
        return f"{occupancy.adults}a-{occupancy.children}c-{occupancy.rooms}r"

    @staticmethod
    def children_ages_key(children_ages: list[int]) -> str:
        if not children_ages:
            return "none"
        return "-".join(str(age) for age in sorted(children_ages))

    @classmethod
    def _matches(
        cls,
        snapshot: CurrentHotelAvailabilitySnapshot,
        *,
        hotel_id: str | None,
        check_in: date | None,
        check_out: date | None,
        nights: int | None,
        occupancy: Occupancy | None,
        children_ages: list[int] | None,
        currency: str | None,
        source_id: str | None,
        status: HotelAvailabilityStatus | None,
    ) -> bool:
        observation = snapshot.observation
        return not (
            (hotel_id is not None and snapshot.hotel_id != hotel_id)
            or (check_in is not None and observation.check_in != check_in)
            or (check_out is not None and observation.check_out != check_out)
            or (nights is not None and observation.nights != nights)
            or (occupancy is not None and observation.occupancy != occupancy)
            or (
                children_ages is not None
                and observation.children_ages != sorted(children_ages)
            )
            or (
                currency is not None
                and observation.currency != currency.strip().upper()
            )
            or (
                source_id is not None
                and cls._normalized_text(observation.source_id)
                != cls._normalized_text(source_id)
            )
            or (status is not None and observation.status is not status)
        )

    @staticmethod
    def _normalized_text(value: str) -> str:
        return " ".join(value.split()).casefold()

    @staticmethod
    def _read(destination: Path) -> CurrentHotelAvailabilitySnapshot | None:
        if not destination.exists():
            return None
        return CurrentHotelAvailabilitySnapshot.model_validate_json(
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

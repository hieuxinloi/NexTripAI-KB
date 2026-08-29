from __future__ import annotations

import hashlib
import json
import os
from datetime import timezone
from enum import StrEnum
from pathlib import Path
from typing import Literal, TypeAlias
from urllib.parse import quote

from pydantic import Field, model_validator

from nextrip_pipeline.schemas import (
    GoogleMapsPlaceObservation,
    DailyOpeningStatus,
    HotelAvailabilityObservation,
    HotelAvailabilityStatus,
    HotelPriceObservation,
    NexTripModel,
    OpeningStatusObservation,
    BusinessStatus,
    VerificationStatus,
)

from ._file_lock import destination_file_lock


class AcceptedObservationError(ValueError):
    """Base error raised when an observation cannot enter the accepted store."""


class UnverifiedObservationError(AcceptedObservationError):
    """Raised when an observation still requires review or was rejected."""


class AcceptedObservationConflictError(FileExistsError):
    """Raised when an immutable content-addressed destination is invalid."""


class AcceptedObservationType(StrEnum):
    HOTEL_PRICE = "hotel_price"
    HOTEL_AVAILABILITY = "hotel_availability"
    GOOGLE_OPENING_STATUS = "google_opening_status"
    GOOGLE_PLACE = "google_place"


AcceptedObservation: TypeAlias = (
    HotelPriceObservation
    | HotelAvailabilityObservation
    | OpeningStatusObservation
    | GoogleMapsPlaceObservation
)


ACCEPTED_VERIFICATION_STATUSES = frozenset(
    {
        VerificationStatus.LEGACY_VERIFIED,
        VerificationStatus.AUTO_VERIFIED,
        VerificationStatus.AGENT_VERIFIED,
        VerificationStatus.HUMAN_VERIFIED,
    }
)


class AcceptedObservationArtifact(NexTripModel):
    """Self-verifying envelope persisted by :class:`AcceptedObservationStore`."""

    schema_version: Literal[1] = 1
    observation_type: AcceptedObservationType
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation: AcceptedObservation

    @model_validator(mode="after")
    def validate_identity_and_acceptance(self) -> AcceptedObservationArtifact:
        actual_type = observation_type_for(self.observation)
        if self.observation_type is not actual_type:
            raise ValueError(
                "observation_type does not match the embedded observation model"
            )
        require_accepted_verification(self.observation)
        expected_hash = accepted_observation_hash(self.observation)
        if self.content_hash != expected_hash:
            raise ValueError("content_hash does not match accepted observation content")
        return self


def observation_type_for(
    observation: AcceptedObservation,
) -> AcceptedObservationType:
    """Return the stable storage type for one supported observation model."""

    if isinstance(observation, HotelPriceObservation):
        return AcceptedObservationType.HOTEL_PRICE
    if isinstance(observation, HotelAvailabilityObservation):
        return AcceptedObservationType.HOTEL_AVAILABILITY
    if isinstance(observation, OpeningStatusObservation):
        return AcceptedObservationType.GOOGLE_OPENING_STATUS
    if isinstance(observation, GoogleMapsPlaceObservation):
        return AcceptedObservationType.GOOGLE_PLACE
    raise TypeError(
        "accepted observation store supports hotel price, hotel availability, "
        "Google opening status, and Google place observations"
    )


def require_accepted_verification(observation: AcceptedObservation) -> None:
    """Reject pending, quarantined, or rejected evidence at the store boundary."""

    components: list[tuple[str, VerificationStatus]] = [
        ("observation", observation.verification_status)
    ]
    if isinstance(observation, GoogleMapsPlaceObservation):
        components.append(("opening", observation.opening.verification_status))
        if observation.weekly_opening is not None:
            components.append(
                ("weekly_opening", observation.weekly_opening.verification_status)
            )
        if observation.menu_source is not None:
            components.append(
                ("menu_source", observation.menu_source.verification_status)
            )
        if observation.media is not None:
            components.append(("media", observation.media.verification_status))

    unverified = [
        f"{name}={status.value}"
        for name, status in components
        if status not in ACCEPTED_VERIFICATION_STATUSES
    ]
    if unverified:
        raise UnverifiedObservationError(
            "accepted observation contains unverified evidence: "
            + ", ".join(unverified)
        )

    semantic_unknowns: list[str] = []
    if (
        isinstance(observation, HotelAvailabilityObservation)
        and observation.status is HotelAvailabilityStatus.UNKNOWN
    ):
        semantic_unknowns.append("availability=unknown")
    if (
        isinstance(observation, OpeningStatusObservation)
        and observation.status is DailyOpeningStatus.UNKNOWN
    ):
        semantic_unknowns.append("opening=unknown")
    if isinstance(observation, GoogleMapsPlaceObservation):
        if observation.business_status is BusinessStatus.UNKNOWN:
            semantic_unknowns.append("business_status=unknown")
        if observation.opening.status is DailyOpeningStatus.UNKNOWN:
            semantic_unknowns.append("opening=unknown")
    if semantic_unknowns:
        raise UnverifiedObservationError(
            "accepted observation contains unknown evidence: "
            + ", ".join(semantic_unknowns)
        )


def accepted_observation_hash(observation: AcceptedObservation) -> str:
    """Return a deterministic digest over schema, type, and validated payload."""

    observation_type = observation_type_for(observation)
    canonical = json.dumps(
        {
            "schema_version": 1,
            "observation_type": observation_type.value,
            "observation": observation.model_dump(mode="json"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_accepted_observation_artifact(
    observation: AcceptedObservation,
) -> AcceptedObservationArtifact:
    """Validate and wrap an accepted observation without writing it."""

    observation_type = observation_type_for(observation)
    require_accepted_verification(observation)
    return AcceptedObservationArtifact(
        observation_type=observation_type,
        content_hash=accepted_observation_hash(observation),
        observation=observation,
    )


def read_accepted_observation(path: str | Path) -> AcceptedObservationArtifact:
    """Read and verify one immutable accepted-observation artifact."""

    return AcceptedObservationArtifact.model_validate_json(Path(path).read_bytes())


class AcceptedObservationStore:
    """Append-only, content-addressed storage for accepted observations.

    The default root is intentionally independent from ``data/current``. A
    repeated write of identical validated content is an idempotent no-op, while
    every content change receives a new SHA-256 path and preserves the prior
    observation.
    """

    def __init__(self, root_directory: str | Path = "data/observations") -> None:
        self.root_directory = Path(root_directory)

    def destination_for(self, observation: AcceptedObservation) -> Path:
        artifact = build_accepted_observation_artifact(observation)
        subject_id = _subject_id(artifact.observation)
        observed_date = (
            artifact.observation.observed_at.astimezone(timezone.utc)
            .date()
            .isoformat()
        )
        return (
            self.root_directory
            / f"type={artifact.observation_type.value}"
            / f"date={observed_date}"
            / f"subject={quote(subject_id, safe='-_.')}"
            / f"observation={artifact.content_hash}.json"
        )

    def write(self, observation: AcceptedObservation) -> Path:
        artifact = build_accepted_observation_artifact(observation)
        destination = self.destination_for(artifact.observation)
        serialized = (
            json.dumps(
                artifact.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

        with destination_file_lock(destination):
            if destination.exists():
                try:
                    existing = read_accepted_observation(destination)
                except (OSError, TypeError, ValueError) as error:
                    raise AcceptedObservationConflictError(
                        "immutable accepted observation is invalid: "
                        f"{destination}"
                    ) from error
                if existing == artifact:
                    return destination
                raise AcceptedObservationConflictError(
                    "immutable accepted observation already exists with "
                    f"different content: {destination}"
                )

            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o666,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                file.write(serialized)
                file.flush()
                os.fsync(file.fileno())
        return destination

    def write_many(
        self,
        observations: list[AcceptedObservation],
    ) -> list[Path]:
        return [self.write(observation) for observation in observations]

    @staticmethod
    def read(path: str | Path) -> AcceptedObservationArtifact:
        return read_accepted_observation(path)


def _subject_id(observation: AcceptedObservation) -> str:
    if isinstance(observation, (HotelPriceObservation, HotelAvailabilityObservation)):
        return observation.hotel_id
    if isinstance(observation, (OpeningStatusObservation, GoogleMapsPlaceObservation)):
        return observation.place_id
    raise TypeError("unsupported accepted observation model")

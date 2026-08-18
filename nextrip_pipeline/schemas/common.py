from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class NexTripModel(BaseModel):
    """Strict base contract shared by pipeline and traffic services."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class EntityType(StrEnum):
    ATTRACTION = "attraction"
    CAFE = "cafe"
    HOTEL = "hotel"
    NIGHTLIFE = "nightlife"
    RESTAURANT = "restaurant"


class VerificationStatus(StrEnum):
    LEGACY_VERIFIED = "legacy_verified"
    AUTO_VERIFIED = "auto_verified"
    AGENT_VERIFIED = "agent_verified"
    HUMAN_VERIFIED = "human_verified"
    PENDING_REVIEW = "pending_review"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"


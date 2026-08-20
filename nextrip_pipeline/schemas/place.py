from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from .common import EntityType, NexTripModel, VerificationStatus


class BusinessStatus(StrEnum):
    ACTIVE = "active"
    TEMPORARILY_CLOSED = "temporarily_closed"
    PERMANENTLY_CLOSED = "permanently_closed"
    UNKNOWN = "unknown"


class Address(NexTripModel):
    formatted: str | None = None
    street: str | None = None
    ward: str | None = None
    district: str | None = None
    province: str | None = None
    country_code: str = Field(default="VN", pattern=r"^[A-Z]{2}$")


class GeoPoint(NexTripModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy: str | None = None
    source: str | None = None
    verified_at: AwareDatetime | None = None


class Contact(NexTripModel):
    phone: str | None = None
    website: HttpUrl | None = None


class ImageReference(NexTripModel):
    url: HttpUrl
    source_url: HttpUrl | None = None
    alt_text: str | None = None


class PlaceRecord(NexTripModel):
    """Canonical normalized place linked back to captured source records."""

    place_id: str = Field(min_length=1)
    # ``entity_type`` remains as a compatibility projection. Canonical data
    # uses primary_type + place_types so one physical venue can be both a cafe
    # and a nightlife venue without being duplicated.
    entity_type: EntityType | None = None
    primary_type: EntityType | None = None
    place_types: list[EntityType] = Field(default_factory=list)
    name: str = Field(min_length=1)
    normalized_name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    city_id: str | None = None
    categories: list[str] = Field(default_factory=list)
    subcategories: list[str] = Field(default_factory=list)
    address: Address | None = None
    location: GeoPoint | None = None
    contact: Contact = Field(default_factory=Contact)
    phone: str | None = None  # legacy projection of contact.phone
    website: HttpUrl | None = None  # legacy projection of contact.website
    description: str | None = None
    highlights: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    suitable_for: list[str] = Field(default_factory=list)
    images: list[ImageReference] = Field(default_factory=list)
    business_status: BusinessStatus = BusinessStatus.UNKNOWN
    external_mapping_ids: list[str] = Field(default_factory=list)
    source_record_ids: list[str] = Field(min_length=1)
    verification_status: VerificationStatus = VerificationStatus.PENDING_REVIEW
    normalized_at: AwareDatetime
    created_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None
    last_verified_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def normalize_place_types(self) -> PlaceRecord:
        primary = self.primary_type or self.entity_type
        if primary is None:
            raise ValueError("primary_type or entity_type is required")

        values = list(dict.fromkeys(self.place_types or [primary]))
        if primary not in values:
            values.insert(0, primary)

        object.__setattr__(self, "primary_type", primary)
        object.__setattr__(self, "entity_type", primary)
        object.__setattr__(self, "place_types", values)

        if self.contact.phone is None and self.phone is not None:
            object.__setattr__(
                self,
                "contact",
                self.contact.model_copy(update={"phone": self.phone}),
            )
        if self.contact.website is None and self.website is not None:
            object.__setattr__(
                self,
                "contact",
                self.contact.model_copy(update={"website": self.website}),
            )
        return self

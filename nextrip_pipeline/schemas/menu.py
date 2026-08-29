from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from .common import NexTripModel, VerificationStatus


class MenuSourceType(StrEnum):
    GOOGLE_MAPS_LINK = "google_maps_link"
    OFFICIAL_WEBSITE = "official_website"
    HTML = "html"
    PDF = "pdf"
    IMAGE = "image"


class MenuSourceObservation(NexTripModel):
    observation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    place_id: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    source_type: MenuSourceType
    menu_url: HttpUrl | None = None
    menu_image_urls: list[HttpUrl] = Field(default_factory=list)
    observed_at: AwareDatetime
    verification_status: VerificationStatus = VerificationStatus.PENDING_REVIEW

    @model_validator(mode="after")
    def require_menu_evidence(self) -> MenuSourceObservation:
        if self.menu_url is None and not self.menu_image_urls:
            raise ValueError("menu source requires a URL or at least one menu image")
        return self


class MenuItem(NexTripModel):
    item_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    section: str | None = None
    description: str | None = None
    currency: str = Field(default="VND", pattern=r"^[A-Z]{3}$")
    amount: Decimal | None = Field(default=None, ge=0)
    min_amount: Decimal | None = Field(default=None, ge=0)
    max_amount: Decimal | None = Field(default=None, ge=0)
    image_url: HttpUrl | None = None
    available: bool | None = None
    ocr_confidence: float | None = Field(default=None, ge=0, le=1)


class NormalizedMenuItem(NexTripModel):
    """Stable menu item contract consumed by the knowledge base."""

    name: str = Field(min_length=1)
    section: str | None = None
    currency: str = Field(default="VND", pattern=r"^[A-Z]{3}$")
    amount: int = Field(ge=0)


class NormalizedMenu(NexTripModel):
    """Minimal menu payload published by the normalization stage."""

    place_id: str = Field(min_length=1)
    items: list[NormalizedMenuItem] = Field(default_factory=list)


class MenuOcrLine(NexTripModel):
    text: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    bounding_box: list[list[float]] = Field(default_factory=list)


class MenuObservation(NexTripModel):
    observation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    place_id: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    source_url: HttpUrl
    image_content_hash: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    ocr_engine: str = Field(min_length=1)
    ocr_engine_version: str = Field(min_length=1)
    raw_ocr_text: str = ""
    ocr_lines: list[MenuOcrLine] = Field(default_factory=list)
    valid_on: date | None = None
    items: list[MenuItem] = Field(default_factory=list)
    observed_at: AwareDatetime
    verification_status: VerificationStatus = VerificationStatus.PENDING_REVIEW

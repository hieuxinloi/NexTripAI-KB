from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from nextrip_pipeline.decision_gate.google_maps import (
    GoogleMapsDecision,
    GoogleMapsDecisionStatus,
)
from nextrip_pipeline.schemas.current_place import (
    CurrentPlaceProvenance,
    CurrentPlaceSnapshot,
)
from nextrip_pipeline.schemas.external_mapping import ExternalEntityMapping
from nextrip_pipeline.schemas.google_maps import GoogleMapsPlaceObservation
from nextrip_pipeline.schemas.media import PlaceMediaRole
from nextrip_pipeline.schemas.common import VerificationStatus


class OlderPlaceObservationError(ValueError):
    """Raised when stale evidence attempts to replace a current place."""


class CurrentPlaceIdentityError(ValueError):
    """Raised when a publish attempt would change master-owned identity."""


class CurrentPlaceWriter:
    """Atomically publishes only PASS observations as current place state."""

    def __init__(
        self,
        root_directory: str | Path,
        *,
        ttl: timedelta = timedelta(days=1),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root_directory = Path(root_directory)
        self.ttl = ttl
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def publish(
        self,
        observation: GoogleMapsPlaceObservation,
        decision: GoogleMapsDecision,
        mapping: ExternalEntityMapping,
    ) -> Path:
        self._validate_publish_request(observation, decision, mapping)
        city, city_id = self._master_city(mapping)
        destination = self.path_for(observation.place_id)

        current = self._read(destination)
        if current is not None:
            self._validate_stable_identity(current, mapping, city, city_id)
            if current.provenance.observation_id == observation.observation_id:
                return destination
            current_observed_at = current.provenance.observed_at
            if current_observed_at is not None and (
                current_observed_at > observation.observed_at
                or (
                    current_observed_at == observation.observed_at
                    and current.provenance.source_id == observation.source_id
                    and current.provenance.source_record_id
                    != observation.source_record_id
                )
            ):
                raise OlderPlaceObservationError(
                    "older place observation cannot replace the current snapshot"
                )

        name = observation.name.strip() if observation.name else ""
        if not name:
            raise ValueError("a current place requires the Google Maps name")

        cover_image_url = self._cover_image_url(observation)
        field_sources = dict(current.field_sources) if current is not None else {}
        google_source = observation.source_id
        for field_name, value in {
            "name": name,
            "category": observation.category,
            "address": observation.address,
            "phone": observation.phone,
            "website_url": observation.website_url,
            "location": observation.location,
            "business_status": observation.business_status,
            "opening": observation.opening,
            "weekly_opening": observation.weekly_opening,
            "cover_image_url": cover_image_url,
            "price_level": observation.price_level,
        }.items():
            if value is not None:
                field_sources[field_name] = google_source

        snapshot = CurrentPlaceSnapshot(
            place_id=mapping.entity_id,
            entity_type=mapping.entity_type,
            city=city,
            city_id=city_id,
            name=name,
            category=self._prefer_observed(observation.category, current, "category"),
            address=self._prefer_observed(observation.address, current, "address"),
            phone=self._prefer_observed(observation.phone, current, "phone"),
            website_url=self._prefer_observed(
                observation.website_url, current, "website_url"
            ),
            location=self._prefer_observed(observation.location, current, "location"),
            business_status=observation.business_status,
            opening=observation.opening.model_copy(
                update={"verification_status": VerificationStatus.AUTO_VERIFIED}
            ),
            weekly_opening=(
                observation.weekly_opening.model_copy(
                    update={"verification_status": VerificationStatus.AUTO_VERIFIED}
                )
                if observation.weekly_opening is not None
                else (current.weekly_opening if current is not None else None)
            ),
            opening_hours=current.opening_hours if current is not None else None,
            cover_image_url=self._prefer_observed(
                cover_image_url, current, "cover_image_url"
            ),
            price_level=self._prefer_observed(
                observation.price_level, current, "price_level"
            ),
            field_sources=field_sources,
            provenance=CurrentPlaceProvenance(
                mapping_id=mapping.mapping_id,
                observation_id=observation.observation_id,
                decision_id=decision.decision_id,
                validation_ids=decision.validation_ids,
                run_id=observation.run_id,
                source_record_id=observation.source_record_id,
                source_id=observation.source_id,
                source_url=observation.source_url,
                verification_status=VerificationStatus.AUTO_VERIFIED,
                observed_at=observation.observed_at,
                decided_at=decision.decided_at,
            ),
            updated_at=self.clock(),
            stale_after=observation.observed_at + self.ttl,
        )
        self._replace(destination, snapshot.model_dump_json(indent=2) + "\n")
        return destination

    @staticmethod
    def _prefer_observed(
        observed: object | None,
        current: CurrentPlaceSnapshot | None,
        field_name: str,
    ) -> object | None:
        if observed is not None:
            return observed
        if current is None:
            return None
        return getattr(current, field_name)

    def get(self, place_id: str) -> CurrentPlaceSnapshot | None:
        return self._read(self.path_for(place_id))

    def path_for(self, place_id: str) -> Path:
        return self.root_directory / f"{quote(place_id, safe='-_.')}.json"

    @staticmethod
    def _validate_publish_request(
        observation: GoogleMapsPlaceObservation,
        decision: GoogleMapsDecision,
        mapping: ExternalEntityMapping,
    ) -> None:
        if decision.status is not GoogleMapsDecisionStatus.PASS:
            raise ValueError("only PASS Google Maps observations can become current")
        if (
            decision.observation_id != observation.observation_id
            or decision.place_id != observation.place_id
            or decision.run_id != observation.run_id
        ):
            raise ValueError("decision and observation do not match")
        if (
            mapping.entity_id != observation.place_id
            or mapping.entity_type is None
            or mapping.source_id != observation.source_id
        ):
            raise ValueError("mapping and observation do not match")
        if (
            observation.opening.place_id != observation.place_id
            or observation.source_record_id
            not in observation.opening.source_record_ids
        ):
            raise ValueError("opening status and observation do not match")

    @staticmethod
    def _master_city(mapping: ExternalEntityMapping) -> tuple[str, str | None]:
        raw_city = mapping.attributes.get("master_city")
        raw_city_id = mapping.attributes.get("city_id")
        city = raw_city.strip() if isinstance(raw_city, str) else ""
        city_id = raw_city_id.strip() if isinstance(raw_city_id, str) else ""
        if not city:
            city = city_id
        if not city:
            raise ValueError("mapping requires master_city or city_id")
        return city, city_id or None

    @staticmethod
    def _validate_stable_identity(
        current: CurrentPlaceSnapshot,
        mapping: ExternalEntityMapping,
        city: str,
        city_id: str | None,
    ) -> None:
        identity = (
            current.place_id,
            current.entity_type,
            current.city,
            current.city_id,
        )
        proposed = (mapping.entity_id, mapping.entity_type, city, city_id)
        if identity != proposed:
            raise CurrentPlaceIdentityError(
                "master-owned place identity cannot change during status publish"
            )

    @staticmethod
    def _cover_image_url(
        observation: GoogleMapsPlaceObservation,
    ) -> object | None:
        if observation.media is None:
            return None
        return next(
            (
                asset.url
                for asset in observation.media.assets
                if asset.role is PlaceMediaRole.COVER
            ),
            None,
        )

    @staticmethod
    def _read(destination: Path) -> CurrentPlaceSnapshot | None:
        if not destination.exists():
            return None
        return CurrentPlaceSnapshot.model_validate_json(
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

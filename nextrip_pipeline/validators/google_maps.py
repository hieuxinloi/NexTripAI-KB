from __future__ import annotations

import math
import os
import re
import unicodedata
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from nextrip_pipeline.schemas import (
    ExternalEntityMapping,
    GoogleMapsPlaceObservation,
    MappingStatus,
    SuggestedAction,
    ValidationEvidence,
    ValidationResult,
    ValidationStatus,
)
from nextrip_pipeline.quality import (
    GoogleMapsMappingResolution,
    MappingResolutionStatus,
)


class GoogleMapsValidatorOrchestrator:
    validator_version = "1.0.0"

    def __init__(
        self,
        *,
        max_age: timedelta = timedelta(days=1),
        pass_distance_m: float = 500,
        fail_distance_m: float = 3000,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.max_age = max_age
        self.pass_distance_m = pass_distance_m
        self.fail_distance_m = fail_distance_m
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def validate(
        self,
        observation: GoogleMapsPlaceObservation,
        mapping: ExternalEntityMapping,
        *,
        mapping_resolution: GoogleMapsMappingResolution | None = None,
    ) -> list[ValidationResult]:
        now = self.clock()
        return [
            (
                self._mapping_resolution(
                    observation,
                    mapping,
                    mapping_resolution,
                    now,
                )
                if mapping_resolution is not None
                else self._mapping(observation, mapping, now)
            ),
            self._identity(observation, mapping, now, mapping_resolution),
            self._coordinates(observation, mapping, now),
            self._detail_integrity(observation, now),
            self._freshness(observation, now),
        ]

    def _mapping_resolution(self, observation, mapping, resolution, now):
        if (
            resolution.mapping_id != mapping.mapping_id
            or resolution.place_id != observation.place_id
            or resolution.observation_id != observation.observation_id
            or resolution.source_record_id != observation.source_record_id
        ):
            return self._result(
                observation,
                "GoogleMapsMappingResolverValidator",
                ValidationStatus.FAIL,
                now,
                reason_code="INVALID_MAPPING_RESOLUTION",
                score=0.0,
            )
        if resolution.status is MappingResolutionStatus.REJECT:
            status = ValidationStatus.FAIL
            reasons = resolution.reason_codes
        elif (
            resolution.status is MappingResolutionStatus.REVIEW
            and mapping.status is not MappingStatus.CONFIRMED
        ):
            status = ValidationStatus.WARN
            reasons = resolution.reason_codes
        else:
            status = ValidationStatus.PASS
            reasons = ()
        action = (
            SuggestedAction.AUTO_ACCEPT
            if status is ValidationStatus.PASS
            else SuggestedAction.HUMAN_REVIEW
            if status is ValidationStatus.WARN
            else SuggestedAction.QUARANTINE
        )
        return ValidationResult(
            validation_id=(
                f"{observation.observation_id}:GoogleMapsMappingResolverValidator"
            ),
            run_id=observation.run_id,
            record_id=observation.observation_id,
            validator="GoogleMapsMappingResolverValidator",
            validator_version=resolution.resolver_version,
            status=status,
            score=resolution.score,
            reason_codes=list(reasons),
            evidence=[
                ValidationEvidence(
                    code="MAPPING_RESOLUTION",
                    source_record_id=observation.source_record_id,
                    observed_value={
                        "status": resolution.status.value,
                        "evidence_hash": resolution.evidence_hash,
                    },
                    expected_value={"mapping_id": mapping.mapping_id},
                )
            ],
            suggested_action=action,
            requires_semantic_review=status is ValidationStatus.WARN,
            requires_human_review=False,
            validated_at=now,
        )

    def _result(
        self,
        observation: GoogleMapsPlaceObservation,
        validator: str,
        status: ValidationStatus,
        now: datetime,
        *,
        score: float,
        reason_code: str | None = None,
        evidence: list[ValidationEvidence] | None = None,
    ) -> ValidationResult:
        review = status is ValidationStatus.WARN
        action = (
            SuggestedAction.AUTO_ACCEPT
            if status is ValidationStatus.PASS
            else SuggestedAction.HUMAN_REVIEW
            if review
            else SuggestedAction.QUARANTINE
        )
        return ValidationResult(
            validation_id=f"{observation.observation_id}:{validator}",
            run_id=observation.run_id,
            record_id=observation.observation_id,
            validator=validator,
            validator_version=self.validator_version,
            status=status,
            score=score,
            reason_codes=[reason_code] if reason_code else [],
            evidence=evidence or [],
            suggested_action=action,
            requires_human_review=review,
            validated_at=now,
        )

    def _mapping(self, observation, mapping, now):
        correct = (
            mapping.entity_id == observation.place_id
            and mapping.source_id == observation.source_id
        )
        if not correct or mapping.status in {
            MappingStatus.REJECTED,
            MappingStatus.PENDING_REVIEW,
        }:
            values = ValidationStatus.FAIL, "INVALID_MAPPING", 0.0
        elif mapping.status is MappingStatus.AUTO_MATCHED:
            values = ValidationStatus.WARN, "MAPPING_NEEDS_CONFIRMATION", 0.8
        else:
            values = ValidationStatus.PASS, None, 1.0
        return self._result(
            observation,
            "GoogleMapsMappingValidator",
            values[0],
            now,
            reason_code=values[1],
            score=values[2],
        )

    def _identity(self, observation, mapping, now, mapping_resolution=None):
        expected = str(mapping.attributes.get("master_name") or mapping.external_id)
        observed = observation.name or ""
        similarity = self._token_similarity(observed, expected)
        if (
            mapping_resolution is not None
            and mapping_resolution.status is MappingResolutionStatus.AUTO_CONFIRM
        ):
            # The resolver has already accepted the name together with city,
            # category and Google coordinates. Do not let this older,
            # name-only Jaccard check contradict that stronger decision.
            status, reason = ValidationStatus.PASS, None
        elif similarity >= 0.6:
            status, reason = ValidationStatus.PASS, None
        elif similarity >= 0.3:
            status, reason = ValidationStatus.WARN, "PLACE_NAME_AMBIGUOUS"
        else:
            status, reason = ValidationStatus.FAIL, "PLACE_NAME_MISMATCH"
        evidence = [
            ValidationEvidence(
                code="PLACE_NAME",
                source_record_id=observation.source_record_id,
                observed_value=observed,
                expected_value=expected,
            )
        ]
        return self._result(
            observation,
            "GoogleMapsIdentityValidator",
            status,
            now,
            reason_code=reason,
            score=similarity,
            evidence=evidence,
        )

    def _coordinates(self, observation, mapping, now):
        location = observation.location
        latitude = mapping.attributes.get("master_latitude")
        longitude = mapping.attributes.get("master_longitude")
        if location is None:
            status, reason, score, distance = (
                ValidationStatus.WARN,
                "COORDINATES_MISSING_REVIEW",
                0.4,
                None,
            )
        elif not isinstance(latitude, (int, float)) or not isinstance(
            longitude, (int, float)
        ):
            status, reason, score, distance = (
                ValidationStatus.WARN,
                "MASTER_COORDINATES_MISSING",
                0.5,
                None,
            )
        else:
            distance = self._distance_m(
                location.latitude, location.longitude, float(latitude), float(longitude)
            )
            if distance <= self.pass_distance_m:
                status, reason, score = ValidationStatus.PASS, None, 1.0
            elif distance <= self.fail_distance_m:
                status, reason, score = (
                    ValidationStatus.WARN,
                    "COORDINATES_NEED_REVIEW",
                    0.6,
                )
            else:
                status, reason, score = (
                    ValidationStatus.FAIL,
                    "COORDINATES_MISMATCH",
                    0.0,
                )
        evidence = [
            ValidationEvidence(
                code="COORDINATE_DISTANCE_METERS",
                source_record_id=observation.source_record_id,
                observed_value=distance,
                expected_value={
                    "pass_max": self.pass_distance_m,
                    "fail_above": self.fail_distance_m,
                },
            )
        ]
        return self._result(
            observation,
            "GoogleMapsCoordinateValidator",
            status,
            now,
            reason_code=reason,
            score=score,
            evidence=evidence,
        )

    def _freshness(self, observation, now):
        age = now - observation.observed_at
        passed = timedelta(0) <= age <= self.max_age
        return self._result(
            observation,
            "GoogleMapsFreshnessValidator",
            ValidationStatus.PASS if passed else ValidationStatus.FAIL,
            now,
            reason_code=None if passed else "STALE_OR_FUTURE_PLACE_STATUS",
            score=1.0 if passed else 0.0,
        )

    def _detail_integrity(self, observation, now):
        nested = [
            item
            for item in (
                observation.weekly_opening,
                observation.menu_source,
                observation.media,
            )
            if item is not None
        ]
        passed = all(
            item.place_id == observation.place_id
            and observation.source_record_id
            in (
                item.source_record_ids
                if hasattr(item, "source_record_ids")
                else [item.source_record_id]
            )
            for item in nested
        )
        return self._result(
            observation,
            "GoogleMapsDetailIntegrityValidator",
            ValidationStatus.PASS if passed else ValidationStatus.FAIL,
            now,
            reason_code=None if passed else "DETAIL_PROVENANCE_MISMATCH",
            score=1.0 if passed else 0.0,
        )

    @staticmethod
    def _token_similarity(left: str, right: str) -> float:
        def tokens(value: str) -> set[str]:
            value = unicodedata.normalize("NFKD", value.casefold())
            value = "".join(char for char in value if not unicodedata.combining(char))
            return set(re.findall(r"[a-z0-9]+", value))

        left_tokens, right_tokens = tokens(left), tokens(right)
        union = left_tokens | right_tokens
        if right_tokens and right_tokens <= left_tokens:
            return 1.0
        if len(left_tokens) >= 2 and left_tokens <= right_tokens:
            return 0.9
        return len(left_tokens & right_tokens) / len(union) if union else 0.0

    @staticmethod
    def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        p1, p2 = math.radians(lat1), math.radians(lat2)
        d_lat, d_lon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
        value = (
            math.sin(d_lat / 2) ** 2
            + math.cos(p1) * math.cos(p2) * math.sin(d_lon / 2) ** 2
        )
        return 6371008.8 * 2 * math.asin(math.sqrt(value))


class GoogleMapsValidationWriter:
    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def write(self, result: ValidationResult) -> Path:
        destination = (
            self.root_directory
            / "entity=opening_status"
            / f"run={quote(result.run_id, safe='-_.')}"
            / f"validation={quote(result.validation_id, safe='-_.')}.json"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError as error:
            raise FileExistsError(
                f"Validation result already exists: {destination}"
            ) from error
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(result.model_dump_json(indent=2) + "\n")
            file.flush()
            os.fsync(file.fileno())
        return destination

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from nextrip_pipeline.schemas import (
    AccessPointRecord,
    RouteMatrixResult,
    RouteObservation,
    SuggestedAction,
    TrafficBasis,
    TransportMode,
    ValidationEvidence,
    ValidationResult,
    ValidationStatus,
)


def _haversine_meters(left: AccessPointRecord, right: AccessPointRecord) -> float:
    lat1 = math.radians(left.location.latitude)
    lon1 = math.radians(left.location.longitude)
    lat2 = math.radians(right.location.latitude)
    lon2 = math.radians(right.location.longitude)
    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 6_371_008.8 * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


class TrafficQualityValidator:
    """Deterministic checks applied before a provider result enters the cache."""

    version = "1.0.0"

    def __init__(self, *, clock=None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def validate_route(
        self,
        route: RouteObservation,
        origin: AccessPointRecord,
        destination: AccessPointRecord,
        *,
        run_id: str,
    ) -> list[ValidationResult]:
        now = self._clock()
        reasons: list[str] = []
        evidence: list[ValidationEvidence] = []
        status = ValidationStatus.PASS

        if route.origin_access_point_id != origin.access_point_id or (
            route.destination_access_point_id != destination.access_point_id
        ):
            status = ValidationStatus.FAIL
            reasons.append("ROUTE_ENDPOINT_BINDING_MISMATCH")

        direct_distance = _haversine_meters(origin, destination)
        evidence.append(
            ValidationEvidence(
                code="DIRECT_DISTANCE_METERS",
                observed_value=round(direct_distance),
                expected_value="route distance should not be materially shorter",
            )
        )
        if direct_distance >= 100 and route.distance_meters < direct_distance * 0.75:
            status = ValidationStatus.FAIL
            reasons.append("ROUTE_DISTANCE_BELOW_GEODESIC_BOUND")

        # 70 m/s (~252 km/h) is intentionally only a corruption guard, not a
        # claim about a legal road-speed limit.
        if route.duration_seconds < route.distance_meters / 70:
            status = ValidationStatus.FAIL
            reasons.append("ROUTE_DURATION_IMPLAUSIBLY_SHORT")

        if route.expires_at <= now:
            status = ValidationStatus.FAIL
            reasons.append("ROUTE_ALREADY_EXPIRED")
        if route.observed_at > now + timedelta(minutes=5):
            status = ValidationStatus.FAIL
            reasons.append("ROUTE_OBSERVED_IN_FUTURE")

        if (
            route.mode in {TransportMode.DRIVE, TransportMode.TWO_WHEELER}
            and route.traffic_basis == TrafficBasis.UNKNOWN
            and status == ValidationStatus.PASS
        ):
            status = ValidationStatus.WARN
            reasons.append("TRAFFIC_BASIS_UNKNOWN")

        return [
            ValidationResult(
                validation_id=f"route-validation-{uuid4().hex}",
                run_id=run_id,
                record_id=route.observation_id,
                validator="traffic_route_validator",
                validator_version=self.version,
                status=status,
                score=(1.0 if status == ValidationStatus.PASS else 0.75)
                if status != ValidationStatus.FAIL
                else 0.0,
                reason_codes=reasons,
                evidence=evidence,
                suggested_action=(
                    SuggestedAction.QUARANTINE
                    if status == ValidationStatus.FAIL
                    else SuggestedAction.AUTO_ACCEPT
                ),
                validated_at=now,
            )
        ]

    def validate_matrix(
        self,
        matrix: RouteMatrixResult,
        *,
        expected_origin_ids: list[str],
        expected_destination_ids: list[str],
        run_id: str,
    ) -> list[ValidationResult]:
        reasons: list[str] = []
        status = ValidationStatus.PASS
        if matrix.origin_access_point_ids != expected_origin_ids or (
            matrix.destination_access_point_ids != expected_destination_ids
        ):
            status = ValidationStatus.FAIL
            reasons.append("MATRIX_ENDPOINT_BINDING_MISMATCH")
        expected_cells = len(expected_origin_ids) * len(expected_destination_ids)
        if len(matrix.cells) != expected_cells:
            status = ValidationStatus.FAIL
            reasons.append("MATRIX_INCOMPLETE")
        if (
            not any(cell.reachable for cell in matrix.cells)
            and status == ValidationStatus.PASS
        ):
            status = ValidationStatus.WARN
            reasons.append("MATRIX_NO_REACHABLE_CELLS")

        return [
            ValidationResult(
                validation_id=f"matrix-validation-{uuid4().hex}",
                run_id=run_id,
                record_id=matrix.matrix_id,
                validator="traffic_matrix_validator",
                validator_version=self.version,
                status=status,
                score=1.0 if status == ValidationStatus.PASS else 0.0,
                reason_codes=reasons,
                suggested_action=(
                    SuggestedAction.QUARANTINE
                    if status == ValidationStatus.FAIL
                    else SuggestedAction.AUTO_ACCEPT
                ),
                validated_at=self._clock(),
            )
        ]

    def validate_cross_provider_route(
        self,
        selected: RouteObservation,
        baseline: RouteObservation,
        *,
        run_id: str,
    ) -> list[ValidationResult]:
        """Warn on large provider divergence without rejecting live traffic.

        Valhalla and HERE can legitimately choose different roads. This check
        is therefore an auditable warning, not an automatic quarantine gate.
        """

        distance_ratio = abs(selected.distance_meters - baseline.distance_meters) / max(
            selected.distance_meters, baseline.distance_meters
        )
        duration_ratio = abs(
            selected.duration_seconds - baseline.duration_seconds
        ) / max(selected.duration_seconds, baseline.duration_seconds)
        reasons: list[str] = []
        if distance_ratio > 0.35:
            reasons.append("CROSS_PROVIDER_DISTANCE_DIVERGENCE")
        if duration_ratio > 0.60:
            reasons.append("CROSS_PROVIDER_DURATION_DIVERGENCE")
        status = ValidationStatus.WARN if reasons else ValidationStatus.PASS
        return [
            ValidationResult(
                validation_id=f"cross-provider-validation-{uuid4().hex}",
                run_id=run_id,
                record_id=selected.observation_id,
                validator="traffic_cross_provider_validator",
                validator_version=self.version,
                status=status,
                score=0.7 if reasons else 1.0,
                reason_codes=reasons,
                evidence=[
                    ValidationEvidence(
                        code="CROSS_PROVIDER_COMPARISON",
                        observed_value={
                            "selected_provider": selected.provider.value,
                            "baseline_provider": baseline.provider.value,
                            "distance_ratio": round(distance_ratio, 4),
                            "duration_ratio": round(duration_ratio, 4),
                        },
                    )
                ],
                suggested_action=SuggestedAction.AUTO_ACCEPT,
                validated_at=self._clock(),
            )
        ]

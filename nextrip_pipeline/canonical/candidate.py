from __future__ import annotations

from enum import StrEnum

from pydantic import Field, HttpUrl, model_validator

from nextrip_pipeline.schemas import BusinessStatus, EntityType, GeoPoint, NexTripModel


class CandidateDisposition(StrEnum):
    """Decision produced before a candidate may receive a canonical ID."""

    PASS = "pass"
    REVIEW = "review"
    REJECT = "reject"


class CandidateReasonCode(StrEnum):
    NO_DUPLICATE_SIGNAL = "no_duplicate_signal"
    EXTERNAL_ID_MATCH = "external_id_match"
    EXTERNAL_URL_MATCH = "external_url_match"
    PHONE_MATCH = "phone_match"
    WEBSITE_MATCH = "website_match"
    EXACT_NAME_CITY_NEAR = "exact_name_city_near"
    EXACT_NAME_CITY_DISTANCE_UNKNOWN = "exact_name_city_distance_unknown"
    EXACT_NAME_CITY_DISTANT = "exact_name_city_distant"
    FUZZY_NAME_CITY_NEAR = "fuzzy_name_city_near"
    FUZZY_NAME_CITY_DISTANCE_UNKNOWN = "fuzzy_name_city_distance_unknown"
    CONTACT_AND_NAME_MATCH = "contact_and_name_match"
    MULTIPLE_EXISTING_MATCHES = "multiple_existing_matches"
    GOOGLE_STABLE_EXTERNAL_ID_MISSING = "google_stable_external_id_missing"
    GOOGLE_SOURCED_COORDINATES_MISSING = "google_sourced_coordinates_missing"
    GOOGLE_PROVIDER_CATEGORY_COMPATIBLE = "google_provider_category_compatible"
    GOOGLE_PROVIDER_CATEGORY_MISSING = "google_provider_category_missing"
    GOOGLE_PROVIDER_CATEGORY_AMBIGUOUS = "google_provider_category_ambiguous"
    GOOGLE_PROVIDER_CATEGORY_INCOMPATIBLE = "google_provider_category_incompatible"
    GOOGLE_LATE_NIGHT_SUBTYPE_COMPATIBLE = (
        "google_late_night_subtype_compatible"
    )
    GOOGLE_LATE_NIGHT_SUBTYPE_MISMATCH = "google_late_night_subtype_mismatch"
    GOOGLE_LATE_NIGHT_HOURS_COMPATIBLE = "google_late_night_hours_compatible"
    GOOGLE_LATE_NIGHT_HOURS_UNPROVEN = "google_late_night_hours_unproven"
    GOOGLE_PERMANENTLY_CLOSED = "google_permanently_closed"
    GOOGLE_CITY_BOUNDARY_MISMATCH = "google_city_boundary_mismatch"
    GOOGLE_CITY_BOUNDARY_UNAVAILABLE = "google_city_boundary_unavailable"
    ENTITY_NAME_INCOMPATIBLE_KEYWORD = "entity_name_incompatible_keyword"
    SAME_TYPE_NEARBY_IDENTITY_UNKNOWN = "same_type_nearby_identity_unknown"


class CandidateExternalIdentity(NexTripModel):
    source_id: str = Field(min_length=1)
    external_id: str | None = Field(default=None, min_length=1)
    external_url: HttpUrl | None = None

    @model_validator(mode="after")
    def require_external_evidence(self) -> CandidateExternalIdentity:
        if self.external_id is None and self.external_url is None:
            raise ValueError("external identity requires external_id or external_url")
        return self


class CanonicalReplacementCandidate(NexTripModel):
    """Proposed place identity; validation never mutates a master record."""

    candidate_key: str = Field(min_length=1)
    entity_type: EntityType
    city_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    resolved_address: str | None = None
    provider_category: str | None = None
    business_status: BusinessStatus | None = None
    phone: str | None = None
    website_url: HttpUrl | None = None
    location: GeoPoint | None = None
    external_identities: list[CandidateExternalIdentity] = Field(default_factory=list)


class ExistingCanonicalIdentity(NexTripModel):
    """Minimum identity projection needed for offline duplicate detection."""

    place_id: str = Field(min_length=1)
    entity_type: EntityType
    city_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    phone: str | None = None
    website_url: HttpUrl | None = None
    location: GeoPoint | None = None
    external_identities: list[CandidateExternalIdentity] = Field(default_factory=list)
    retired: bool = False


class CandidateMatchEvidence(NexTripModel):
    place_id: str = Field(min_length=1)
    retired: bool = False
    reason_codes: list[CandidateReasonCode] = Field(min_length=1)
    name_similarity: float | None = Field(default=None, ge=0, le=1)
    distance_meters: float | None = Field(default=None, ge=0)
    definite_duplicate: bool = False


class CandidateValidationResult(NexTripModel):
    candidate_key: str = Field(min_length=1)
    status: CandidateDisposition
    reason_codes: list[CandidateReasonCode] = Field(min_length=1)
    matches: list[CandidateMatchEvidence] = Field(default_factory=list)

    @property
    def disposition(self) -> CandidateDisposition:
        """Compatibility alias for callers that describe the result as a disposition."""

        return self.status

    @model_validator(mode="after")
    def validate_disposition(self) -> CandidateValidationResult:
        definite = any(item.definite_duplicate for item in self.matches)
        eligibility_reject = bool(
            {
                CandidateReasonCode.GOOGLE_PERMANENTLY_CLOSED,
                CandidateReasonCode.GOOGLE_CITY_BOUNDARY_MISMATCH,
            }
            & set(self.reason_codes)
        )
        if (
            self.status is CandidateDisposition.REJECT
            and not definite
            and not eligibility_reject
        ):
            raise ValueError("reject requires definite duplicate evidence")
        if self.status is CandidateDisposition.PASS and self.matches:
            raise ValueError("pass cannot contain duplicate matches")
        if self.status is CandidateDisposition.PASS:
            reasons = set(self.reason_codes)
            allowed = {
                CandidateReasonCode.NO_DUPLICATE_SIGNAL,
                CandidateReasonCode.GOOGLE_PROVIDER_CATEGORY_COMPATIBLE,
                CandidateReasonCode.GOOGLE_LATE_NIGHT_SUBTYPE_COMPATIBLE,
                CandidateReasonCode.GOOGLE_LATE_NIGHT_HOURS_COMPATIBLE,
            }
            if CandidateReasonCode.NO_DUPLICATE_SIGNAL not in reasons:
                raise ValueError("pass requires no_duplicate_signal")
            if not reasons <= allowed:
                raise ValueError("pass contains a non-pass reason code")
        if eligibility_reject and self.status is not CandidateDisposition.REJECT:
            raise ValueError("ineligible candidates must be rejected")
        return self

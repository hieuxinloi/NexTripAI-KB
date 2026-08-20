from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from .candidate import (
    CandidateDisposition,
    CandidateExternalIdentity,
    CandidateMatchEvidence,
    CandidateReasonCode,
    CandidateValidationResult,
    CanonicalReplacementCandidate,
    ExistingCanonicalIdentity,
)


@dataclass(frozen=True, slots=True)
class DistinctCandidatePolicy:
    exact_name_near_meters: float = 150.0
    fuzzy_near_meters: float = 500.0
    fuzzy_reject_similarity: float = 0.94
    fuzzy_review_similarity: float = 0.84
    contact_name_similarity: float = 0.88
    same_type_nearby_identity_unknown_meters: float = 75.0

    def __post_init__(self) -> None:
        if self.exact_name_near_meters <= 0 or self.fuzzy_near_meters <= 0:
            raise ValueError("distance thresholds must be positive")
        if self.same_type_nearby_identity_unknown_meters <= 0:
            raise ValueError("same-type nearby threshold must be positive")
        if self.fuzzy_near_meters < self.exact_name_near_meters:
            raise ValueError("fuzzy distance cannot be below exact-name distance")
        thresholds = (
            self.fuzzy_reject_similarity,
            self.fuzzy_review_similarity,
            self.contact_name_similarity,
        )
        if any(value < 0 or value > 1 for value in thresholds):
            raise ValueError("similarity thresholds must be between zero and one")
        if self.fuzzy_reject_similarity < self.fuzzy_review_similarity:
            raise ValueError("reject similarity cannot be below review similarity")


class DistinctCandidateValidator:
    """Offline, deterministic duplicate gate for a proposed canonical place."""

    def __init__(self, policy: DistinctCandidatePolicy | None = None) -> None:
        self.policy = policy or DistinctCandidatePolicy()

    def validate(
        self,
        candidate: CanonicalReplacementCandidate,
        existing: Iterable[ExistingCanonicalIdentity],
    ) -> CandidateValidationResult:
        matches = [
            match
            for record in existing
            if (match := self._match(candidate, record)) is not None
        ]
        if not matches:
            return CandidateValidationResult(
                candidate_key=candidate.candidate_key,
                status=CandidateDisposition.PASS,
                reason_codes=[CandidateReasonCode.NO_DUPLICATE_SIGNAL],
            )

        definite = any(item.definite_duplicate for item in matches)
        reasons = {reason for item in matches for reason in item.reason_codes}
        if len(matches) > 1:
            reasons.add(CandidateReasonCode.MULTIPLE_EXISTING_MATCHES)
        return CandidateValidationResult(
            candidate_key=candidate.candidate_key,
            status=(
                CandidateDisposition.REJECT if definite else CandidateDisposition.REVIEW
            ),
            reason_codes=sorted(reasons, key=lambda item: item.value),
            matches=sorted(matches, key=lambda item: item.place_id),
        )

    def _match(
        self,
        candidate: CanonicalReplacementCandidate,
        existing: ExistingCanonicalIdentity,
    ) -> CandidateMatchEvidence | None:
        reasons: set[CandidateReasonCode] = set()
        definite = False

        candidate_external_ids = _external_id_keys(candidate.external_identities)
        existing_external_ids = _external_id_keys(existing.external_identities)
        if candidate_external_ids & existing_external_ids:
            reasons.add(CandidateReasonCode.EXTERNAL_ID_MATCH)
            definite = True

        candidate_external_urls = _external_url_keys(candidate.external_identities)
        existing_external_urls = _external_url_keys(existing.external_identities)
        if candidate_external_urls & existing_external_urls:
            reasons.add(CandidateReasonCode.EXTERNAL_URL_MATCH)
            definite = True

        phone_match = bool(
            (candidate_phone := _phone_key(candidate.phone))
            and candidate_phone == _phone_key(existing.phone)
        )
        website_match = bool(
            (candidate_website := _website_key(candidate.website_url))
            and candidate_website == _website_key(existing.website_url)
        )
        if phone_match:
            reasons.add(CandidateReasonCode.PHONE_MATCH)
        if website_match:
            reasons.add(CandidateReasonCode.WEBSITE_MATCH)

        same_city = _text_key(candidate.city_id) == _text_key(existing.city_id)
        candidate_name = _name_key(candidate.name)
        existing_name = _name_key(existing.name)
        names_comparable = bool(candidate_name and existing_name)
        similarity = (
            SequenceMatcher(None, candidate_name, existing_name).ratio()
            if names_comparable
            else 0.0
        )
        distance = _distance_meters(candidate.location, existing.location)

        if same_city and names_comparable and candidate_name == existing_name:
            if distance is None:
                reasons.add(CandidateReasonCode.EXACT_NAME_CITY_DISTANCE_UNKNOWN)
            elif distance <= self.policy.exact_name_near_meters:
                reasons.add(CandidateReasonCode.EXACT_NAME_CITY_NEAR)
                definite = True
            else:
                reasons.add(CandidateReasonCode.EXACT_NAME_CITY_DISTANT)
        elif (
            same_city
            and names_comparable
            and similarity >= self.policy.fuzzy_review_similarity
        ):
            if distance is None:
                if similarity >= self.policy.fuzzy_reject_similarity:
                    reasons.add(CandidateReasonCode.FUZZY_NAME_CITY_DISTANCE_UNKNOWN)
            elif distance <= self.policy.fuzzy_near_meters:
                reasons.add(CandidateReasonCode.FUZZY_NAME_CITY_NEAR)
                if (
                    similarity >= self.policy.fuzzy_reject_similarity
                    and distance <= self.policy.exact_name_near_meters
                ):
                    definite = True

        if (
            same_city
            and (phone_match or website_match)
            and similarity >= self.policy.contact_name_similarity
        ):
            reasons.add(CandidateReasonCode.CONTACT_AND_NAME_MATCH)
            definite = True
        if phone_match and website_match:
            definite = True

        # Translated or rebranded names can hide a duplicate. Without stronger
        # identity evidence, fail closed when a same-type place is very near.
        # Different entity types may legitimately share one building.
        if (
            not reasons
            and same_city
            and candidate.entity_type is existing.entity_type
            and distance is not None
            and distance <= self.policy.same_type_nearby_identity_unknown_meters
        ):
            reasons.add(CandidateReasonCode.SAME_TYPE_NEARBY_IDENTITY_UNKNOWN)

        if not reasons:
            return None
        return CandidateMatchEvidence(
            place_id=existing.place_id,
            retired=existing.retired,
            reason_codes=sorted(reasons, key=lambda item: item.value),
            name_similarity=round(similarity, 6),
            distance_meters=round(distance, 2) if distance is not None else None,
            definite_duplicate=definite,
        )


def _text_key(value: str) -> str:
    return " ".join(value.casefold().split())


def _name_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    ascii_like = "".join(
        char for char in normalized if not unicodedata.combining(char)
    ).replace("đ", "d")
    return " ".join(re.findall(r"[a-z0-9]+", ascii_like))


def _phone_key(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if digits.startswith("0084"):
        digits = digits[2:]
    if digits.startswith("84") and len(digits) in {11, 12}:
        digits = "0" + digits[2:]
    return digits if len(digits) >= 8 else None


def _external_id_keys(
    identities: Iterable[CandidateExternalIdentity],
) -> set[tuple[str, str]]:
    return {
        (_text_key(item.source_id), _text_key(item.external_id))
        for item in identities
        if item.external_id
    }


def _external_url_keys(
    identities: Iterable[CandidateExternalIdentity],
) -> set[str]:
    return {
        key
        for item in identities
        if item.external_url and (key := _url_key(item.external_url, keep_query=True))
    }


def _website_key(value: object | None) -> str | None:
    return _url_key(value, keep_query=False)


def _url_key(value: object | None, *, keep_query: bool) -> str | None:
    if value is None:
        return None
    parsed = urlsplit(str(value).strip())
    host = (parsed.hostname or "").casefold()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    port = parsed.port
    if port and not (
        (parsed.scheme.casefold() == "http" and port == 80)
        or (parsed.scheme.casefold() == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", unquote(parsed.path or "/")).rstrip("/")
    path = path or "/"
    if not keep_query:
        return host + path
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_")
        and key.casefold() not in {"fbclid", "gclid"}
    ]
    normalized_query = urlencode(sorted(query))
    return host + path + (f"?{normalized_query}" if normalized_query else "")


def _distance_meters(first: object | None, second: object | None) -> float | None:
    if first is None or second is None:
        return None
    latitude_1 = math.radians(first.latitude)
    latitude_2 = math.radians(second.latitude)
    delta_latitude = latitude_2 - latitude_1
    delta_longitude = math.radians(second.longitude - first.longitude)
    haversine = (
        math.sin(delta_latitude / 2) ** 2
        + math.cos(latitude_1)
        * math.cos(latitude_2)
        * math.sin(delta_longitude / 2) ** 2
    )
    return 6_371_000 * 2 * math.asin(min(1.0, math.sqrt(haversine)))

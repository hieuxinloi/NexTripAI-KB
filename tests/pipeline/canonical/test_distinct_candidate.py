from __future__ import annotations

from nextrip_pipeline.canonical.candidate import (
    CandidateDisposition,
    CandidateExternalIdentity,
    CandidateReasonCode,
    CanonicalReplacementCandidate,
    ExistingCanonicalIdentity,
)
from nextrip_pipeline.canonical.distinct import DistinctCandidateValidator
from nextrip_pipeline.schemas import EntityType, GeoPoint


def _candidate(**updates) -> CanonicalReplacementCandidate:
    values = {
        "candidate_key": "candidate-1",
        "entity_type": EntityType.CAFE,
        "city_id": "city_da_nang",
        "name": "Xóm Mèo Coffee & Petshop Đà Nẵng",
        "location": GeoPoint(latitude=16.04624, longitude=108.23716),
    }
    values.update(updates)
    return CanonicalReplacementCandidate(**values)


def _existing(**updates) -> ExistingCanonicalIdentity:
    values = {
        "place_id": "cafe_dn_062",
        "entity_type": EntityType.CAFE,
        "city_id": "city_da_nang",
        "name": "Xóm Mèo Coffee & Petshop Đà Nẵng",
        "location": GeoPoint(latitude=16.04625, longitude=108.23717),
    }
    values.update(updates)
    return ExistingCanonicalIdentity(**values)


def test_rejects_matching_external_id() -> None:
    identity = CandidateExternalIdentity(
        source_id="google-maps-web",
        external_id="ChIJ-123",
    )
    result = DistinctCandidateValidator().validate(
        _candidate(
            name="Different name",
            location=None,
            external_identities=[identity],
        ),
        [_existing(external_identities=[identity])],
    )

    assert result.disposition is CandidateDisposition.REJECT
    assert CandidateReasonCode.EXTERNAL_ID_MATCH in result.reason_codes


def test_rejects_canonical_equivalent_external_url() -> None:
    result = DistinctCandidateValidator().validate(
        _candidate(
            name="Different name",
            location=None,
            external_identities=[
                CandidateExternalIdentity(
                    source_id="google-maps-web",
                    external_url="https://www.google.com/maps/place/x/?q=1&utm_source=test",
                )
            ],
        ),
        [
            _existing(
                external_identities=[
                    CandidateExternalIdentity(
                        source_id="google-maps-web",
                        external_url="http://google.com/maps/place/x?q=1",
                    )
                ]
            )
        ],
    )

    assert result.disposition is CandidateDisposition.REJECT
    assert CandidateReasonCode.EXTERNAL_URL_MATCH in result.reason_codes


def test_contact_only_match_requires_review() -> None:
    result = DistinctCandidateValidator().validate(
        _candidate(name="Unrelated branch", location=None, phone="+84 905 123 456"),
        [_existing(name="Another venue", location=None, phone="0905 123 456")],
    )

    assert result.disposition is CandidateDisposition.REVIEW
    assert result.reason_codes == [CandidateReasonCode.PHONE_MATCH]


def test_website_only_match_requires_review() -> None:
    result = DistinctCandidateValidator().validate(
        _candidate(
            name="Unrelated branch",
            location=None,
            website_url="https://www.example.vn/cafe/?campaign=summer",
        ),
        [
            _existing(
                name="Another venue",
                location=None,
                website_url="http://example.vn/cafe",
            )
        ],
    )

    assert result.status is CandidateDisposition.REVIEW
    assert result.reason_codes == [CandidateReasonCode.WEBSITE_MATCH]


def test_exact_normalized_name_same_city_and_near_is_duplicate() -> None:
    result = DistinctCandidateValidator().validate(
        _candidate(name="Xom Meo Coffee Petshop Da Nang"),
        [_existing()],
    )

    assert result.status is CandidateDisposition.REJECT
    assert CandidateReasonCode.EXACT_NAME_CITY_NEAR in result.reason_codes
    assert result.matches[0].distance_meters is not None


def test_similar_name_near_requires_review_below_reject_threshold() -> None:
    result = DistinctCandidateValidator().validate(
        _candidate(name="Xóm Mèo Coffee Pet Đà Nẵng"),
        [_existing()],
    )

    assert result.status is CandidateDisposition.REVIEW
    assert CandidateReasonCode.FUZZY_NAME_CITY_NEAR in result.reason_codes


def test_no_identity_signal_passes() -> None:
    result = DistinctCandidateValidator().validate(
        _candidate(name="Completely New Cafe", location=None),
        [_existing()],
    )

    assert result.status is CandidateDisposition.PASS
    assert result.reason_codes == [CandidateReasonCode.NO_DUPLICATE_SIGNAL]
    assert result.matches == []


def test_same_type_nearby_with_unrelated_translated_name_requires_review() -> None:
    result = DistinctCandidateValidator().validate(
        _candidate(
            entity_type=EntityType.ATTRACTION,
            name="Marble Mountains",
        ),
        [
            _existing(
                entity_type=EntityType.ATTRACTION,
                name="Ngu Hanh Son",
            )
        ],
    )

    assert result.status is CandidateDisposition.REVIEW
    assert result.reason_codes == [
        CandidateReasonCode.SAME_TYPE_NEARBY_IDENTITY_UNKNOWN
    ]


def test_different_entity_type_may_share_coordinates_without_identity_signal() -> None:
    result = DistinctCandidateValidator().validate(
        _candidate(name="Independent cafe inside a landmark"),
        [
            _existing(
                entity_type=EntityType.ATTRACTION,
                name="Unrelated landmark",
            )
        ],
    )

    assert result.status is CandidateDisposition.PASS
    assert result.reason_codes == [CandidateReasonCode.NO_DUPLICATE_SIGNAL]

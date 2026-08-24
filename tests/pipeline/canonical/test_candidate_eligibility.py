from __future__ import annotations

import unicodedata
from datetime import date, datetime, time, timezone

import pytest

from nextrip_pipeline.canonical.candidate import (
    CandidateDisposition,
    CandidateMatchEvidence,
    CandidateReasonCode,
    CandidateValidationResult,
    CanonicalReplacementCandidate,
)
from nextrip_pipeline.canonical.eligibility import (
    CandidateEntityEligibilityPolicy,
    CandidateEntityEligibilityValidator,
)
from nextrip_pipeline.canonical.models import (
    EntityCityVacancy,
    VacancySourceSubtype,
    stable_identifier,
)
from nextrip_pipeline.schemas import (
    BusinessStatus,
    DailyOpeningSchedule,
    DailyOpeningStatus,
    EntityType,
    GeoPoint,
    GoogleMapsPlaceObservation,
    OpeningInterval,
    OpeningStatusObservation,
    VerificationStatus,
    Weekday,
    WeeklyOpeningScheduleObservation,
)


NOW = datetime(2026, 8, 21, 9, tzinfo=timezone.utc)


def _candidate(
    entity_type: EntityType,
    category: str | None,
    *,
    business_status: BusinessStatus = BusinessStatus.ACTIVE,
    location: GeoPoint | None = None,
    city_id: str = "city_da_nang",
    name: str = "Candidate place",
) -> CanonicalReplacementCandidate:
    return CanonicalReplacementCandidate(
        candidate_key="candidate-1",
        entity_type=entity_type,
        city_id=city_id,
        name=name,
        provider_category=category,
        business_status=business_status,
        location=location,
    )


def _pass() -> CandidateValidationResult:
    return CandidateValidationResult(
        candidate_key="candidate-1",
        status=CandidateDisposition.PASS,
        reason_codes=[CandidateReasonCode.NO_DUPLICATE_SIGNAL],
    )


def _nightlife_vacancy(
    subtype: VacancySourceSubtype | None,
) -> EntityCityVacancy:
    retired_place_id = "night_dn_008"
    return EntityCityVacancy(
        vacancy_id=stable_identifier(
            "vacancy",
            "city_da_nang",
            EntityType.NIGHTLIFE.value,
            retired_place_id,
        ),
        retired_place_id=retired_place_id,
        city_id="city_da_nang",
        entity_type=EntityType.NIGHTLIFE,
        source_subtype=subtype,
    )


def _google_observation(
    days: list[DailyOpeningSchedule] | None,
    *,
    current_is_24_hours: bool = False,
) -> GoogleMapsPlaceObservation:
    weekly = (
        WeeklyOpeningScheduleObservation(
            observation_id="weekly-1",
            run_id="run-1",
            place_id="candidate-1",
            source_record_ids=["source-1"],
            days=days,
            observed_at=NOW,
            verification_status=VerificationStatus.PENDING_REVIEW,
        )
        if days is not None
        else None
    )
    return GoogleMapsPlaceObservation(
        observation_id="observation-1",
        run_id="run-1",
        place_id="candidate-1",
        source_record_id="source-1",
        source_id="google-maps-web",
        source_url="https://www.google.com/maps/place/example",
        name="Candidate place",
        opening=OpeningStatusObservation(
            observation_id="opening-1",
            run_id="run-1",
            place_id="candidate-1",
            source_record_ids=["source-1"],
            local_date=date(2026, 8, 21),
            status=(
                DailyOpeningStatus.OPEN_TODAY
                if current_is_24_hours
                else DailyOpeningStatus.UNKNOWN
            ),
            is_24_hours=current_is_24_hours,
            observed_at=NOW,
        ),
        weekly_opening=weekly,
        observed_at=NOW,
    )


def _schedule(
    *,
    closes_at: time = time(22),
    closes_next_day: bool = False,
    open_24_hours: bool = False,
) -> DailyOpeningSchedule:
    if open_24_hours:
        return DailyOpeningSchedule(day=Weekday.FRIDAY, open_24_hours=True)
    return DailyOpeningSchedule(
        day=Weekday.FRIDAY,
        intervals=[
            OpeningInterval(
                opens_at=time(18),
                closes_at=closes_at,
                closes_next_day=closes_next_day,
            )
        ],
    )


@pytest.mark.parametrize(
    ("entity_type", "category"),
    [
        (EntityType.ATTRACTION, "Tourist attraction"),
        (EntityType.CAFE, "Coffee shop"),
        (EntityType.HOTEL, "Resort hotel"),
        (EntityType.NIGHTLIFE, "Cocktail bar"),
        (EntityType.RESTAURANT, "Vietnamese restaurant"),
    ],
)
def test_explicit_google_category_keeps_distinct_candidate_pass(
    entity_type: EntityType,
    category: str,
) -> None:
    result = CandidateEntityEligibilityValidator().validate(
        _candidate(entity_type, category),
        _pass(),
    )

    assert result.status is CandidateDisposition.PASS
    assert CandidateReasonCode.GOOGLE_PROVIDER_CATEGORY_COMPATIBLE in (
        result.reason_codes
    )


@pytest.mark.parametrize(
    ("entity_type", "category"),
    [
        (EntityType.ATTRACTION, "\u0110i\u1ec3m thu h\u00fat kh\u00e1ch du l\u1ecbch"),
        (EntityType.ATTRACTION, "Th\u1eafng c\u1ea3nh"),
        (EntityType.ATTRACTION, "B\u1ea3o t\u00e0ng"),
        (EntityType.ATTRACTION, "B\u00e3i bi\u1ec3n"),
        (EntityType.ATTRACTION, "C\u00f4ng vi\u00ean"),
        (EntityType.ATTRACTION, "\u0110\u1ecba danh l\u1ecbch s\u1eed"),
        (EntityType.ATTRACTION, "Ch\u00f9a"),
        (EntityType.ATTRACTION, "\u0110\u1ec1n"),
        (EntityType.ATTRACTION, "Th\u00e1c n\u01b0\u1edbc"),
        (EntityType.CAFE, "Qu\u00e1n c\u00e0 ph\u00ea"),
        (EntityType.CAFE, "Ti\u1ec7m c\u00e0 ph\u00ea"),
        (EntityType.HOTEL, "Kh\u00e1ch s\u1ea1n"),
        (EntityType.HOTEL, "Khu ngh\u1ec9 d\u01b0\u1ee1ng"),
        (EntityType.HOTEL, "Nh\u00e0 ngh\u1ec9"),
        (EntityType.NIGHTLIFE, "Qu\u00e1n bar"),
        (EntityType.NIGHTLIFE, "Qu\u00e1n r\u01b0\u1ee3u"),
        (EntityType.NIGHTLIFE, "C\u00e2u l\u1ea1c b\u1ed9 \u0111\u00eam"),
        (EntityType.NIGHTLIFE, "H\u1ed9p \u0111\u00eam"),
        (EntityType.RESTAURANT, "Nh\u00e0 h\u00e0ng"),
        (EntityType.RESTAURANT, "Nh\u00e0 h\u00e0ng Vi\u1ec7t Nam"),
        (EntityType.RESTAURANT, "Nh\u00e0 h\u00e0ng h\u1ea3i s\u1ea3n"),
        (EntityType.RESTAURANT, "Qu\u00e1n \u0103n"),
    ],
)
def test_localized_google_category_is_explicitly_compatible(
    entity_type: EntityType,
    category: str,
) -> None:
    result = CandidateEntityEligibilityValidator().validate(
        _candidate(entity_type, category),
        _pass(),
    )

    assert result.status is CandidateDisposition.PASS
    assert CandidateReasonCode.GOOGLE_PROVIDER_CATEGORY_COMPATIBLE in (
        result.reason_codes
    )


@pytest.mark.parametrize(
    "category",
    ["C\u1ea7u", "C\u1eeda h\u00e0ng", "Doanh nghi\u1ec7p", "Bridge", "Store", "Business"],
)
def test_generic_provider_category_never_passes(
    category: str,
) -> None:
    result = CandidateEntityEligibilityValidator().validate(
        _candidate(EntityType.ATTRACTION, category),
        _pass(),
    )

    assert result.status is CandidateDisposition.REVIEW
    assert CandidateReasonCode.GOOGLE_PROVIDER_CATEGORY_AMBIGUOUS in (
        result.reason_codes
    )
    assert CandidateReasonCode.GOOGLE_PROVIDER_CATEGORY_COMPATIBLE not in (
        result.reason_codes
    )


@pytest.mark.parametrize(
    ("category", "reason"),
    [
        (None, CandidateReasonCode.GOOGLE_PROVIDER_CATEGORY_MISSING),
        (
            "Point of interest",
            CandidateReasonCode.GOOGLE_PROVIDER_CATEGORY_AMBIGUOUS,
        ),
        (
            "Coffee shop and restaurant",
            CandidateReasonCode.GOOGLE_PROVIDER_CATEGORY_AMBIGUOUS,
        ),
        ("Hotel", CandidateReasonCode.GOOGLE_PROVIDER_CATEGORY_INCOMPATIBLE),
    ],
)
def test_non_explicit_category_downgrades_pass_to_review(
    category: str | None,
    reason: CandidateReasonCode,
) -> None:
    result = CandidateEntityEligibilityValidator().validate(
        _candidate(EntityType.CAFE, category),
        _pass(),
    )

    assert result.status is CandidateDisposition.REVIEW
    assert reason in result.reason_codes


def test_permanently_closed_candidate_is_rejected() -> None:
    result = CandidateEntityEligibilityValidator().validate(
        _candidate(
            EntityType.CAFE,
            "Coffee shop",
            business_status=BusinessStatus.PERMANENTLY_CLOSED,
        ),
        _pass(),
    )

    assert result.status is CandidateDisposition.REJECT
    assert CandidateReasonCode.GOOGLE_PERMANENTLY_CLOSED in result.reason_codes


@pytest.mark.parametrize(
    "name",
    [
        "THU\u00ca LOA K\u00c9O QUY NH\u01a0N - D\u1ecaCH V\u1ee4 LOA K\u00c9O",
        "Cho thu\u00ea loa k\u00e9o s\u1ef1 ki\u1ec7n",
        "D\u1ecbch v\u1ee5 loa chuy\u00ean nghi\u1ec7p",
        "Speaker rental Quy Nhon",
        "Karaoke equipment rental",
        unicodedata.normalize("NFD", "Thu\u00ea loa k\u00e9o Quy Nh\u01a1n"),
    ],
)
def test_nightlife_rental_or_speaker_service_name_requires_review(
    name: str,
) -> None:
    result = CandidateEntityEligibilityValidator().validate(
        _candidate(
            EntityType.NIGHTLIFE,
            "Qu\u00e1n bar karaoke",
            name=name,
        ),
        _pass(),
    )

    assert result.status is CandidateDisposition.REVIEW
    assert CandidateReasonCode.ENTITY_NAME_INCOMPATIBLE_KEYWORD in (
        result.reason_codes
    )
    assert CandidateReasonCode.GOOGLE_PROVIDER_CATEGORY_COMPATIBLE in (
        result.reason_codes
    )


@pytest.mark.parametrize(
    ("name", "category"),
    [
        ("The 69 Cocktail Bar", "Cocktail bar"),
        ("T.O.P Pub", "Pub"),
        ("New Phuong Dong Nightclub", "Night club"),
        ("Karaoke Kingdom", "Qu\u00e1n karaoke"),
    ],
)
def test_valid_nightlife_names_are_not_affected(
    name: str,
    category: str,
) -> None:
    result = CandidateEntityEligibilityValidator().validate(
        _candidate(EntityType.NIGHTLIFE, category, name=name),
        _pass(),
    )

    assert result.status is CandidateDisposition.PASS
    assert CandidateReasonCode.ENTITY_NAME_INCOMPATIBLE_KEYWORD not in (
        result.reason_codes
    )


@pytest.mark.parametrize(
    ("category", "subtype", "schedule"),
    [
        (
            "Coffee shop",
            VacancySourceSubtype.LATE_NIGHT_CAFE,
            _schedule(closes_at=time(22)),
        ),
        (
            "Restaurant",
            VacancySourceSubtype.LATE_NIGHT_DINING,
            _schedule(closes_at=time(1), closes_next_day=True),
        ),
        (
            "Restaurant",
            VacancySourceSubtype.LATE_NIGHT_DINING,
            _schedule(open_24_hours=True),
        ),
    ],
)
def test_matching_late_night_subtype_and_google_weekly_hours_can_pass(
    category: str,
    subtype: VacancySourceSubtype,
    schedule: DailyOpeningSchedule,
) -> None:
    result = CandidateEntityEligibilityValidator().validate(
        _candidate(EntityType.NIGHTLIFE, category),
        _pass(),
        vacancy=_nightlife_vacancy(subtype),
        google_observation=_google_observation([schedule]),
    )

    assert result.status is CandidateDisposition.PASS
    assert CandidateReasonCode.GOOGLE_PROVIDER_CATEGORY_COMPATIBLE in (
        result.reason_codes
    )
    assert CandidateReasonCode.GOOGLE_LATE_NIGHT_SUBTYPE_COMPATIBLE in (
        result.reason_codes
    )
    assert CandidateReasonCode.GOOGLE_LATE_NIGHT_HOURS_COMPATIBLE in (
        result.reason_codes
    )


def test_late_night_cross_category_requires_matching_retired_subtype() -> None:
    result = CandidateEntityEligibilityValidator().validate(
        _candidate(EntityType.NIGHTLIFE, "Coffee shop"),
        _pass(),
        vacancy=_nightlife_vacancy(VacancySourceSubtype.LATE_NIGHT_DINING),
        google_observation=_google_observation([_schedule()]),
    )

    assert result.status is CandidateDisposition.REVIEW
    assert CandidateReasonCode.GOOGLE_LATE_NIGHT_SUBTYPE_MISMATCH in (
        result.reason_codes
    )
    assert CandidateReasonCode.GOOGLE_PROVIDER_CATEGORY_INCOMPATIBLE in (
        result.reason_codes
    )


@pytest.mark.parametrize(
    "observation",
    [
        _google_observation([_schedule(closes_at=time(21, 59))]),
        _google_observation(None),
        # A current-day 24-hour flag alone is not the required parsed weekly proof.
        _google_observation(None, current_is_24_hours=True),
    ],
)
def test_missing_or_insufficient_weekly_hours_remains_review(
    observation: GoogleMapsPlaceObservation,
) -> None:
    result = CandidateEntityEligibilityValidator().validate(
        _candidate(EntityType.NIGHTLIFE, "Coffee shop"),
        _pass(),
        vacancy=_nightlife_vacancy(VacancySourceSubtype.LATE_NIGHT_CAFE),
        google_observation=observation,
    )

    assert result.status is CandidateDisposition.REVIEW
    assert CandidateReasonCode.GOOGLE_LATE_NIGHT_SUBTYPE_COMPATIBLE in (
        result.reason_codes
    )
    assert CandidateReasonCode.GOOGLE_LATE_NIGHT_HOURS_UNPROVEN in (
        result.reason_codes
    )
    assert CandidateReasonCode.GOOGLE_PROVIDER_CATEGORY_INCOMPATIBLE in (
        result.reason_codes
    )


def test_existing_nightlife_category_does_not_require_subtype_or_hours() -> None:
    result = CandidateEntityEligibilityValidator().validate(
        _candidate(EntityType.NIGHTLIFE, "Cocktail bar"),
        _pass(),
        vacancy=_nightlife_vacancy(VacancySourceSubtype.LATE_NIGHT_CAFE),
    )

    assert result.status is CandidateDisposition.PASS
    assert not {
        CandidateReasonCode.GOOGLE_LATE_NIGHT_SUBTYPE_COMPATIBLE,
        CandidateReasonCode.GOOGLE_LATE_NIGHT_HOURS_COMPATIBLE,
    } & set(result.reason_codes)


def test_incompatible_name_keywords_are_configurable_per_entity() -> None:
    validator = CandidateEntityEligibilityValidator(
        CandidateEntityEligibilityPolicy(
            incompatible_name_keywords={
                EntityType.CAFE: ("coffee machine rental",),
            }
        )
    )

    result = validator.validate(
        _candidate(
            EntityType.CAFE,
            "Coffee shop",
            name="Da Nang coffee machine rental",
        ),
        _pass(),
    )

    assert result.status is CandidateDisposition.REVIEW
    assert CandidateReasonCode.ENTITY_NAME_INCOMPATIBLE_KEYWORD in (
        result.reason_codes
    )


@pytest.mark.parametrize(
    ("city_id", "latitude", "longitude"),
    [
        ("city_da_nang", 15.997, 107.988),  # Ba Na Hills
        ("city_da_nang", 16.047, 108.206),  # Da Nang urban centre
        ("city_quy_nhon", 13.769, 109.223),
    ],
)
def test_google_coordinates_inside_target_city_can_remain_pass(
    city_id: str,
    latitude: float,
    longitude: float,
) -> None:
    result = CandidateEntityEligibilityValidator().validate(
        _candidate(
            EntityType.CAFE,
            "Coffee shop",
            city_id=city_id,
            location=GeoPoint(
                latitude=latitude,
                longitude=longitude,
                source="google-maps-web",
            ),
        ),
        _pass(),
    )

    assert result.status is CandidateDisposition.PASS


@pytest.mark.parametrize(
    ("city_id", "latitude", "longitude"),
    [
        ("city_da_nang", 15.8801, 108.3380),  # Hoi An
        ("city_quy_nhon", 16.0471, 108.2062),  # Da Nang
    ],
)
def test_google_coordinates_outside_target_city_are_rejected(
    city_id: str,
    latitude: float,
    longitude: float,
) -> None:
    result = CandidateEntityEligibilityValidator().validate(
        _candidate(
            EntityType.CAFE,
            "Coffee shop",
            city_id=city_id,
            location=GeoPoint(
                latitude=latitude,
                longitude=longitude,
                source="google-maps-web",
            ),
        ),
        _pass(),
    )

    assert result.status is CandidateDisposition.REJECT
    assert CandidateReasonCode.GOOGLE_CITY_BOUNDARY_MISMATCH in result.reason_codes


def test_existing_duplicate_reject_is_preserved_exactly() -> None:
    duplicate = CandidateValidationResult(
        candidate_key="candidate-1",
        status=CandidateDisposition.REJECT,
        reason_codes=[CandidateReasonCode.EXTERNAL_ID_MATCH],
        matches=[
            CandidateMatchEvidence(
                place_id="cafe_dn_001",
                reason_codes=[CandidateReasonCode.EXTERNAL_ID_MATCH],
                definite_duplicate=True,
            )
        ],
    )

    result = CandidateEntityEligibilityValidator().validate(
        _candidate(
            EntityType.CAFE,
            None,
            business_status=BusinessStatus.PERMANENTLY_CLOSED,
        ),
        duplicate,
    )

    assert result == duplicate


def test_category_policy_is_configurable_without_inventing_entity_type() -> None:
    policy = CandidateEntityEligibilityPolicy(
        exact_categories={EntityType.ATTRACTION: ("heritage garden",)},
        keyword_categories={EntityType.ATTRACTION: ("heritage garden",)},
        ambiguous_categories=("place",),
    )
    validator = CandidateEntityEligibilityValidator(policy)

    accepted = validator.validate(
        _candidate(EntityType.ATTRACTION, "National heritage garden"),
        _pass(),
    )
    not_invented = validator.validate(
        _candidate(EntityType.CAFE, "National heritage garden"),
        _pass(),
    )

    assert accepted.status is CandidateDisposition.PASS
    assert not_invented.status is CandidateDisposition.REVIEW
    assert CandidateReasonCode.GOOGLE_PROVIDER_CATEGORY_INCOMPATIBLE in (
        not_invented.reason_codes
    )

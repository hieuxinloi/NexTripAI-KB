from __future__ import annotations

import unicodedata
from datetime import date, datetime, time, timedelta, timezone

import pytest

from nextrip_pipeline.canonical.dataset import materialize_canonical_active_dataset
from nextrip_pipeline.canonical.google_maps_refresh import (
    CanonicalGoogleMapsPatchWriter,
    CanonicalGoogleMapsRefreshError,
    CanonicalMenuCollectionBacklogWriter,
    GoogleMapsPatchDisposition,
    GoogleMapsPriceEvidenceStatus,
    apply_google_maps_canonical_refresh_patch,
    build_canonical_menu_collection_backlog,
    build_google_maps_canonical_refresh_patch,
)
from nextrip_pipeline.canonical.master import CanonicalMasterLoad, MasterRawRecord
from nextrip_pipeline.canonical.models import LegacyPlaceSlot
from nextrip_pipeline.canonical.place_projection import project_canonical_place
from nextrip_pipeline.canonical.resolver import build_canonical_identity_manifest
from nextrip_pipeline.decision_gate.google_maps import (
    GoogleMapsDecision,
    GoogleMapsDecisionStatus,
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
from nextrip_graphrag.versions.v8.observation_publisher import (
    build_v8_observation_plan_for_dataset,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 24, 2, tzinfo=UTC)
GOOGLE_TOKEN = "0x314219caaa000001:0x1000000000000001"
GOOGLE_URL = (
    f"https://www.google.com/maps/place/Google+Cafe/data=!4m7!3m6!1s{GOOGLE_TOKEN}!8m2"
)


def _dataset(*, names: dict[str, str] | None = None):
    selected_names = names or {}
    slots = [
        LegacyPlaceSlot(
            legacy_place_id="attr_dn_001",
            city_id="city_da_nang",
            primary_type=EntityType.ATTRACTION,
        ),
        LegacyPlaceSlot(
            legacy_place_id="cafe_dn_001",
            city_id="city_da_nang",
            primary_type=EntityType.CAFE,
        ),
    ]
    raw_records = [
        MasterRawRecord(
            place_id=slot.legacy_place_id,
            source_filename=f"{slot.primary_type.value}_final.json",
            record_index=index,
            raw_record={
                "id": slot.legacy_place_id,
                "entity_type": slot.primary_type.value,
                "name": selected_names.get(
                    slot.legacy_place_id, f"Master {slot.primary_type.value}"
                ),
                "aliases": [],
                "city": "Da Nang",
                "address": f"{index + 1} Master Street",
                "coordinates": {"lat": 16.05 + index / 100, "lng": 108.2},
                "category": slot.primary_type.value,
                "tags": [],
                "price_range": {"min": 20_000, "max": 50_000, "currency": "VND"},
                "menu_url": "https://legacy.example/menu.jpg",
            },
        )
        for index, slot in enumerate(slots)
    ]
    master = CanonicalMasterLoad(
        slots=slots,
        raw_records_by_id={item.place_id: item for item in raw_records},
        source_files=[],
        quota_counts=[],
        explicit_duplicate_decisions=[],
    )
    manifest = build_canonical_identity_manifest(slots, generated_at=NOW)
    return materialize_canonical_active_dataset(master, manifest)


def _weekly(place_id: str, observed_at: datetime):
    return WeeklyOpeningScheduleObservation(
        observation_id=f"weekly-{place_id}-{observed_at.timestamp()}",
        run_id="google-cycle-1",
        place_id=place_id,
        source_record_ids=[f"source-{place_id}"],
        days=[
            DailyOpeningSchedule(
                day=weekday,
                intervals=[OpeningInterval(opens_at=time(7), closes_at=time(22))],
            )
            for weekday in Weekday
        ],
        observed_at=observed_at,
        verification_status=VerificationStatus.AUTO_VERIFIED,
    )


def _observation(
    *,
    observed_at: datetime = NOW,
    location: GeoPoint | None = None,
) -> GoogleMapsPlaceObservation:
    observation_id = f"google-cafe-{int(observed_at.timestamp())}"
    return GoogleMapsPlaceObservation(
        observation_id=observation_id,
        run_id="google-cycle-1",
        place_id="cafe_dn_001",
        source_record_id="source-cafe_dn_001",
        source_id="google-maps-web",
        source_url=GOOGLE_URL,
        name="Google Cafe Official",
        category="Coffee shop",
        address="10 Google Street, Da Nang",
        phone="0905000000",
        website_url="https://google-cafe.example",
        location=location,
        business_status=BusinessStatus.ACTIVE,
        opening=OpeningStatusObservation(
            observation_id=f"opening-{observation_id}",
            run_id="google-cycle-1",
            place_id="cafe_dn_001",
            source_record_ids=["source-cafe_dn_001"],
            local_date=date(2026, 8, 24),
            status=DailyOpeningStatus.OPEN_TODAY,
            opening_intervals=[OpeningInterval(opens_at=time(7), closes_at=time(22))],
            observed_at=observed_at,
            verification_status=VerificationStatus.AUTO_VERIFIED,
        ),
        weekly_opening=_weekly("cafe_dn_001", observed_at),
        observed_at=observed_at,
        verification_status=VerificationStatus.AUTO_VERIFIED,
    )


def _write_evidence(
    root,
    observation: GoogleMapsPlaceObservation,
    *,
    status: GoogleMapsDecisionStatus = GoogleMapsDecisionStatus.PASS,
) -> None:
    artifact_stem = f"{observation.run_id}-{observation.observation_id}"
    normalized = root / "normalized" / f"{artifact_stem}.json"
    normalized.parent.mkdir(parents=True, exist_ok=True)
    normalized.write_text(observation.model_dump_json(indent=2), encoding="utf-8")
    decision = GoogleMapsDecision(
        decision_id=f"{observation.observation_id}:decision",
        run_id=observation.run_id,
        observation_id=observation.observation_id,
        place_id=observation.place_id,
        status=status,
        validation_ids=["validation-1"],
        reason_codes=([] if status is GoogleMapsDecisionStatus.PASS else ["TEST_GATE"]),
        decided_at=observation.observed_at + timedelta(minutes=1),
    )
    decision_path = root / "decisions" / f"{artifact_stem}-{status.value}.json"
    decision_path.parent.mkdir(parents=True, exist_ok=True)
    decision_path.write_text(decision.model_dump_json(indent=2), encoding="utf-8")


def _observation_for_run(
    *,
    run_id: str,
    observed_at: datetime,
    name: str,
) -> GoogleMapsPlaceObservation:
    observation = _observation(observed_at=observed_at)
    return observation.model_copy(
        update={
            "run_id": run_id,
            "name": name,
            "opening": observation.opening.model_copy(update={"run_id": run_id}),
            "weekly_opening": observation.weekly_opening.model_copy(
                update={"run_id": run_id}
            ),
        }
    )


def test_refresh_creates_new_canonical_version_and_preserves_reference_price(
    tmp_path,
) -> None:
    dataset = _dataset()
    observation = _observation(
        location=GeoPoint(
            latitude=16.061,
            longitude=108.221,
            source="google-maps-web",
            accuracy="google_maps_place_page",
        )
    )
    _write_evidence(tmp_path, observation)

    patch = build_google_maps_canonical_refresh_patch(
        dataset,
        tmp_path / "normalized",
        tmp_path / "decisions",
        generated_at=NOW + timedelta(minutes=2),
    )
    refreshed = apply_google_maps_canonical_refresh_patch(dataset, patch)

    assert len(patch.records) == 1
    assert patch.deferred[0].place_id == "attr_dn_001"
    assert patch.deferred[0].disposition is GoogleMapsPatchDisposition.MISSING
    assert refreshed.dataset_id != dataset.dataset_id
    original = {item.place_id: item for item in dataset.records}["cafe_dn_001"]
    updated = {item.place_id: item for item in refreshed.records}["cafe_dn_001"]
    assert original.name == "Master cafe"
    assert updated.name == "Google Cafe Official"
    assert updated.aliases == ["Master cafe"]
    assert updated.coordinates.source == "google-maps-web"
    assert updated.data["price_range"] == original.data["price_range"]
    assert updated.data["google_maps_price"]["status"] == (
        GoogleMapsPriceEvidenceStatus.NOT_LISTED.value
    )
    assert updated.data["google_maps_weekly_opening"] is not None
    assert updated.data["menu"] is None
    assert "menu_url" not in updated.data
    assert updated.external_identities[-1]["external_id"] == GOOGLE_TOKEN
    served = project_canonical_place(updated, dataset_id=refreshed.dataset_id)
    assert served.opening is not None
    assert served.weekly_opening is not None
    assert served.business_status is BusinessStatus.ACTIVE
    assert str(served.provenance.source_url) == GOOGLE_URL

    price_root = tmp_path / "prices"
    availability_root = tmp_path / "availability"
    price_root.mkdir()
    availability_root.mkdir()
    observation_plan = build_v8_observation_plan_for_dataset(
        refreshed,
        hotel_price_root=price_root,
        hotel_availability_root=availability_root,
        current_menu_root=None,
        built_at=NOW,
    )
    assert observation_plan.counts["opening_status"] == 1
    assert all(
        artifact.family != "current_place"
        for artifact in observation_plan.input_artifacts
    )


def test_latest_not_listed_price_supersedes_older_false_positive(tmp_path) -> None:
    dataset = _dataset()
    older = _observation(observed_at=NOW - timedelta(minutes=5)).model_copy(
        update={"raw_price_text": "Giá phòng cho Google Cafe Official"}
    )
    newer = _observation(observed_at=NOW)
    _write_evidence(tmp_path / "older", older)
    _write_evidence(tmp_path / "newer", newer)

    patch = build_google_maps_canonical_refresh_patch(
        dataset,
        tmp_path,
        tmp_path,
        entity_types=[EntityType.CAFE],
        generated_at=NOW + timedelta(minutes=2),
    )

    assert len(patch.records) == 1
    assert patch.records[0].observation_id == newer.observation_id
    assert patch.records[0].price_status is GoogleMapsPriceEvidenceStatus.NOT_LISTED
    refreshed = apply_google_maps_canonical_refresh_patch(dataset, patch)
    original = {item.place_id: item for item in dataset.records}["cafe_dn_001"]
    updated = {item.place_id: item for item in refreshed.records}["cafe_dn_001"]
    assert updated.data["price_range"] == original.data["price_range"]
    assert updated.data["google_maps_price"]["status"] == (
        GoogleMapsPriceEvidenceStatus.NOT_LISTED.value
    )


@pytest.mark.parametrize(
    "false_price_text",
    [
        "Giá phòng cho Google Cafe Official",
        "Giáo - Hội Tăng - Già Khất - Sĩ Việt",
        "Giá tour Ngũ Hành Sơn 466.726 ₫",
    ],
)
def test_patch_sanitizes_legacy_false_price_from_pass_observation(
    tmp_path,
    false_price_text: str,
) -> None:
    dataset = _dataset()
    legacy = _observation().model_copy(
        update={"raw_price_text": false_price_text, "price_level": 1}
    )
    _write_evidence(tmp_path, legacy)

    patch = build_google_maps_canonical_refresh_patch(
        dataset,
        tmp_path / "normalized",
        tmp_path / "decisions",
        entity_types=[EntityType.CAFE],
        generated_at=NOW + timedelta(minutes=2),
    )

    assert len(patch.records) == 1
    assert patch.records[0].raw_price_text is None
    assert patch.records[0].price_level is None
    assert patch.records[0].price_status is GoogleMapsPriceEvidenceStatus.NOT_LISTED
    refreshed = apply_google_maps_canonical_refresh_patch(dataset, patch)
    updated = {item.place_id: item for item in refreshed.records}["cafe_dn_001"]
    assert updated.data["price_range"] == dataset.records[1].data["price_range"]
    assert updated.data["google_maps_price"]["status"] == (
        GoogleMapsPriceEvidenceStatus.NOT_LISTED.value
    )


def test_patch_keeps_valid_exact_vnd_but_recomputes_legacy_tier(tmp_path) -> None:
    dataset = _dataset()
    legacy = _observation().model_copy(
        update={"raw_price_text": "1.000.000 ₫ trở lên", "price_level": 1}
    )
    _write_evidence(tmp_path, legacy)

    patch = build_google_maps_canonical_refresh_patch(
        dataset,
        tmp_path / "normalized",
        tmp_path / "decisions",
        entity_types=[EntityType.CAFE],
        generated_at=NOW + timedelta(minutes=2),
    )

    assert patch.records[0].raw_price_text == "1.000.000 ₫ trở lên"
    assert patch.records[0].price_level is None
    assert patch.records[0].price_status is GoogleMapsPriceEvidenceStatus.OBSERVED


def test_fallback_coordinates_do_not_replace_canonical_and_stale_patch_fails(
    tmp_path,
) -> None:
    dataset = _dataset()
    observation = _observation(
        location=GeoPoint(
            latitude=16.06,
            longitude=108.2,
            source="verified-master-data",
            accuracy="verified_master_fallback",
        )
    )
    _write_evidence(tmp_path, observation)
    patch = build_google_maps_canonical_refresh_patch(
        dataset,
        tmp_path / "normalized",
        tmp_path / "decisions",
        entity_types=[EntityType.CAFE],
        generated_at=NOW + timedelta(minutes=2),
    )
    refreshed = apply_google_maps_canonical_refresh_patch(dataset, patch)
    updated = {item.place_id: item for item in refreshed.records}["cafe_dn_001"]
    original = {item.place_id: item for item in dataset.records}["cafe_dn_001"]
    assert updated.coordinates == original.coordinates

    older_root = tmp_path / "older"
    older = _observation(observed_at=NOW - timedelta(days=1))
    _write_evidence(older_root, older)
    older_patch = build_google_maps_canonical_refresh_patch(
        refreshed,
        older_root / "normalized",
        older_root / "decisions",
        entity_types=[EntityType.CAFE],
        generated_at=NOW + timedelta(minutes=3),
    )
    with pytest.raises(CanonicalGoogleMapsRefreshError, match="is stale"):
        apply_google_maps_canonical_refresh_patch(refreshed, older_patch)


def test_patch_and_menu_backlog_are_immutable_and_content_addressed(tmp_path) -> None:
    dataset = _dataset()
    _write_evidence(tmp_path, _observation())
    patch = build_google_maps_canonical_refresh_patch(
        dataset,
        tmp_path / "normalized",
        tmp_path / "decisions",
        generated_at=NOW + timedelta(minutes=2),
    )
    patch_writer = CanonicalGoogleMapsPatchWriter(tmp_path / "patches")
    assert patch_writer.write(patch) == patch_writer.write(patch)

    refreshed = apply_google_maps_canonical_refresh_patch(dataset, patch)
    backlog = build_canonical_menu_collection_backlog(
        refreshed,
        generated_at=NOW + timedelta(minutes=2),
    )
    assert [item.place_id for item in backlog.tasks] == ["cafe_dn_001"]
    backlog_writer = CanonicalMenuCollectionBacklogWriter(tmp_path / "menu")
    assert backlog_writer.write(backlog) == backlog_writer.write(backlog)


def test_pass_decision_with_unverified_observation_is_deferred_not_published(
    tmp_path,
) -> None:
    dataset = _dataset()
    observation = _observation().model_copy(
        update={"verification_status": VerificationStatus.PENDING_REVIEW}
    )
    _write_evidence(tmp_path, observation)

    patch = build_google_maps_canonical_refresh_patch(
        dataset,
        tmp_path / "normalized",
        tmp_path / "decisions",
        entity_types=[EntityType.CAFE],
        generated_at=NOW + timedelta(minutes=2),
    )

    assert patch.records == []
    assert patch.deferred[0].disposition is GoogleMapsPatchDisposition.REVIEW
    assert patch.deferred[0].reason_codes == ["GOOGLE_OBSERVATION_NOT_VERIFIED"]


def test_later_non_pass_does_not_shadow_earlier_pass_across_allowed_runs(
    tmp_path,
) -> None:
    dataset = _dataset()
    earlier = _observation_for_run(
        run_id="google-cycle-earlier",
        observed_at=NOW,
        name="Earlier accepted Google name",
    )
    later = _observation_for_run(
        run_id="google-cycle-later",
        observed_at=NOW + timedelta(hours=1),
        name="Later review Google name",
    )
    _write_evidence(tmp_path, earlier)
    _write_evidence(tmp_path, later, status=GoogleMapsDecisionStatus.REVIEW)

    patch = build_google_maps_canonical_refresh_patch(
        dataset,
        tmp_path / "normalized",
        tmp_path / "decisions",
        entity_types=[EntityType.CAFE],
        source_run_ids=[earlier.run_id, later.run_id],
        generated_at=NOW + timedelta(hours=2),
    )

    assert patch.deferred == []
    assert len(patch.records) == 1
    assert patch.records[0].observation_id == earlier.observation_id
    assert patch.records[0].run_id == earlier.run_id
    assert patch.records[0].name == "Earlier accepted Google name"
    refreshed = apply_google_maps_canonical_refresh_patch(dataset, patch)
    updated = {item.place_id: item for item in refreshed.records}["cafe_dn_001"]
    assert updated.name == "Earlier accepted Google name"

    repeated_patch = build_google_maps_canonical_refresh_patch(
        refreshed,
        tmp_path / "normalized",
        tmp_path / "decisions",
        entity_types=[EntityType.CAFE],
        source_run_ids=[earlier.run_id, later.run_id],
        generated_at=NOW + timedelta(hours=3),
    )
    repeated = apply_google_maps_canonical_refresh_patch(refreshed, repeated_patch)
    assert repeated.dataset_id == refreshed.dataset_id
    assert repeated.dataset_hash == refreshed.dataset_hash


def test_later_pass_supersedes_earlier_pass_across_allowed_runs(tmp_path) -> None:
    dataset = _dataset()
    earlier = _observation_for_run(
        run_id="google-cycle-earlier",
        observed_at=NOW,
        name="Earlier accepted Google name",
    )
    later = _observation_for_run(
        run_id="google-cycle-later",
        observed_at=NOW + timedelta(hours=1),
        name="Latest accepted Google name",
    )
    _write_evidence(tmp_path, earlier)
    _write_evidence(tmp_path, later)

    patch = build_google_maps_canonical_refresh_patch(
        dataset,
        tmp_path / "normalized",
        tmp_path / "decisions",
        entity_types=[EntityType.CAFE],
        source_run_ids=[earlier.run_id, later.run_id],
        generated_at=NOW + timedelta(hours=2),
    )

    assert patch.deferred == []
    assert len(patch.records) == 1
    assert patch.records[0].observation_id == later.observation_id
    assert patch.records[0].run_id == later.run_id
    assert patch.records[0].name == "Latest accepted Google name"


def test_google_name_unicode_normalization_does_not_create_a_duplicate_alias(
    tmp_path,
) -> None:
    canonical_name = "Café Mèo"
    dataset = _dataset(names={"cafe_dn_001": canonical_name})
    observation = _observation().model_copy(
        update={"name": unicodedata.normalize("NFD", canonical_name)}
    )
    _write_evidence(tmp_path, observation)

    patch = build_google_maps_canonical_refresh_patch(
        dataset,
        tmp_path / "normalized",
        tmp_path / "decisions",
        entity_types=[EntityType.CAFE],
        generated_at=NOW + timedelta(minutes=2),
    )
    refreshed = apply_google_maps_canonical_refresh_patch(dataset, patch)
    updated = {item.place_id: item for item in refreshed.records}["cafe_dn_001"]

    assert updated.name == canonical_name
    assert updated.aliases == []

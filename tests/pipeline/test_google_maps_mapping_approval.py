from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from nextrip_pipeline import cli as cli_module
from nextrip_pipeline.canonical.dataset import (
    CanonicalActiveDataset,
    CanonicalActiveDatasetReport,
    CanonicalActivePlaceRecord,
    CanonicalCoordinates,
    CanonicalEntityCityCount,
    CanonicalMasterRecordReference,
    CanonicalRecordProvenance,
    CanonicalRecordSource,
    _dataset_payload,
    _record_payload,
    _report_payload,
)
from nextrip_pipeline.canonical.google_maps_refresh import (
    CanonicalGoogleMapsEvidence,
    apply_google_maps_canonical_refresh_patch,
    build_google_maps_canonical_refresh_patch,
)
from nextrip_pipeline.canonical.models import EntityCityQuota, stable_sha256
from nextrip_pipeline.decision_gate.google_maps import (
    GoogleMapsDecision,
    GoogleMapsDecisionStatus,
)
from nextrip_pipeline.quality import (
    CurrentGoogleMapsMappingWriter,
    GoogleMapsMappingApproval,
    GoogleMapsMappingApprovalError,
    GoogleMapsMappingApprovalWriter,
    GoogleMapsMappingResolver,
    approve_google_maps_mapping,
)
from nextrip_pipeline.schemas import (
    DailyOpeningStatus,
    EntityType,
    ExternalEntityMapping,
    GeoPoint,
    GoogleMapsPlaceObservation,
    MappingStatus,
    OpeningStatusObservation,
)


NOW = datetime(2026, 8, 24, 8, tzinfo=timezone.utc)
MANIFEST_ID = "canonical-manifest-test"
MANIFEST_HASH = "1" * 64
GOOGLE_TOKEN = "0x31421b00723d9291:0x46d9f1c4fa5c9f78"
GOOGLE_URL = (
    "https://www.google.com/maps/place/google-reference/"
    f"data=!4m7!3m6!1s{GOOGLE_TOKEN}!8m2"
)


def _record(
    place_id: str,
    *,
    name: str,
    external_identities: list[dict[str, object]] | None = None,
) -> CanonicalActivePlaceRecord:
    provenance = CanonicalRecordProvenance(
        manifest_id=MANIFEST_ID,
        manifest_hash=MANIFEST_HASH,
        identity_hash="2" * 64,
        source_kind=CanonicalRecordSource.VERIFIED_MASTER,
        canonical_place_id=place_id,
        active_legacy_place_id=place_id,
        legacy_place_ids=[place_id],
        master_records=[
            CanonicalMasterRecordReference(
                legacy_place_id=place_id,
                source_filename="cafe_final.json",
                record_index=0,
                source_record_hash="3" * 64,
            )
        ],
    )
    data = {
        "id": place_id,
        "entity_type": "cafe",
        "primary_type": "cafe",
        "place_types": ["cafe"],
        "aliases": [],
        "name": name,
        "tags": [],
    }
    values = {
        "place_id": place_id,
        "primary_type": EntityType.CAFE,
        "secondary_types": [],
        "place_types": [EntityType.CAFE],
        "name": name,
        "aliases": [],
        "tags": [],
        "city_id": "city_da_nang",
        "city": "Đà Nẵng",
        "address": "Địa chỉ master cũ, Đà Nẵng",
        "coordinates": CanonicalCoordinates(
            lat=16.50,
            lng=108.50,
            source="verified_master",
        ),
        "phone": None,
        "website_url": None,
        "external_identities": external_identities or [],
        "data": data,
        "provenance": provenance,
    }
    return CanonicalActivePlaceRecord(
        record_hash=stable_sha256(_record_payload(**values)),
        **values,
    )


def _dataset(*records: CanonicalActivePlaceRecord) -> CanonicalActiveDataset:
    ordered = sorted(records, key=lambda item: item.place_id)
    counts = [
        CanonicalEntityCityCount(
            city_id="city_da_nang",
            entity_type=EntityType.CAFE,
            count=len(ordered),
        )
    ]
    quotas = [
        EntityCityQuota(
            city_id="city_da_nang",
            entity_type=EntityType.CAFE,
            target_count=len(ordered),
            active_count=len(ordered),
            vacancy_count=0,
        )
    ]
    report_values = {
        "manifest_id": MANIFEST_ID,
        "manifest_hash": MANIFEST_HASH,
        "master_source_record_count": len(ordered),
        "approved_replacement_count": 0,
        "canonical_record_count": len(ordered),
        "master_materialized_count": len(ordered),
        "replacement_materialized_count": 0,
        "retired_duplicate_count": 0,
        "open_vacancy_count": 0,
        "filled_vacancy_count": 0,
        "entity_city_counts": counts,
        "quotas": quotas,
    }
    report = CanonicalActiveDatasetReport(
        report_hash=stable_sha256(_report_payload(**report_values)),
        **report_values,
    )
    dataset_values = {
        "manifest_id": MANIFEST_ID,
        "manifest_hash": MANIFEST_HASH,
        "records": ordered,
        "report": report,
    }
    dataset_hash = stable_sha256(_dataset_payload(**dataset_values))
    return CanonicalActiveDataset(
        dataset_id=f"canonical-active-{dataset_hash[:20]}",
        dataset_hash=dataset_hash,
        **dataset_values,
    )


def _observation(
    *,
    place_id: str = "cafe_dn_001",
    location_source: str = "google-maps-web",
    accuracy: str = "google_maps_place_page",
) -> GoogleMapsPlaceObservation:
    opening = OpeningStatusObservation(
        observation_id="source-1:opening",
        run_id="google-run-1",
        place_id=place_id,
        source_record_ids=["source-1"],
        local_date=date(2026, 8, 24),
        status=DailyOpeningStatus.UNKNOWN,
        observed_at=NOW,
    )
    return GoogleMapsPlaceObservation(
        observation_id="source-1:place-status",
        run_id="google-run-1",
        place_id=place_id,
        source_record_id="source-1",
        source_id="google-maps-web",
        source_url=GOOGLE_URL,
        name="Tên chính xác trên Google Maps",
        category="Quán cà phê",
        address="123 Bạch Đằng, Hải Châu, Đà Nẵng, Việt Nam",
        location=GeoPoint(
            latitude=16.061,
            longitude=108.224,
            source=location_source,
            accuracy=accuracy,
        ),
        opening=opening,
        observed_at=NOW,
    )


def _mapping(place_id: str = "cafe_dn_001") -> ExternalEntityMapping:
    return ExternalEntityMapping(
        mapping_id=f"google-maps-{place_id}",
        entity_id=place_id,
        entity_type=EntityType.CAFE,
        source_id="google-maps-web",
        external_id=f"search:{place_id}",
        status=MappingStatus.AUTO_MATCHED,
        confidence=0.7,
        matched_at=NOW,
        attributes={
            "master_name": "Tên master cũ",
            "master_address": "Địa chỉ master cũ, Đà Nẵng",
            "master_city": "Đà Nẵng",
            "master_latitude": 16.50,
            "master_longitude": 108.50,
        },
    )


def _resolution(observation: GoogleMapsPlaceObservation):
    return GoogleMapsMappingResolver(
        clock=lambda: NOW + timedelta(hours=1)
    ).resolve(_mapping(observation.place_id), observation)


def _decision(observation: GoogleMapsPlaceObservation) -> GoogleMapsDecision:
    return GoogleMapsDecision(
        decision_id=f"{observation.observation_id}:decision",
        run_id=observation.run_id,
        observation_id=observation.observation_id,
        place_id=observation.place_id,
        status=GoogleMapsDecisionStatus.QUARANTINE,
        validation_ids=["validation-1"],
        reason_codes=["COORDINATE_CONFLICT", "NAME_MISMATCH"],
        decided_at=NOW + timedelta(hours=2),
    )


def _write_inputs(
    root: Path,
    dataset: CanonicalActiveDataset,
    observation: GoogleMapsPlaceObservation,
):
    resolution = _resolution(observation)
    decision = _decision(observation)
    paths = {
        "dataset": root / "dataset.json",
        "observation": root / "observation.json",
        "resolution": root / "resolution.json",
        "decision": root / "decision.json",
    }
    values = {
        paths["dataset"]: dataset,
        paths["observation"]: observation,
        paths["resolution"]: resolution,
        paths["decision"]: decision,
    }
    for path, value in values.items():
        path.write_text(value.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return paths


def _approve(
    tmp_path: Path,
    dataset: CanonicalActiveDataset,
    observation: GoogleMapsPlaceObservation,
    *,
    active_mappings: list[ExternalEntityMapping] | None = None,
):
    paths = _write_inputs(tmp_path, dataset, observation)
    mapping_writer = CurrentGoogleMapsMappingWriter(tmp_path / "mappings")
    result = approve_google_maps_mapping(
        paths["dataset"],
        paths["observation"],
        paths["resolution"],
        paths["decision"],
        reviewer="Oanhh",
        approval_writer=GoogleMapsMappingApprovalWriter(tmp_path / "approvals"),
        mapping_writer=mapping_writer,
        active_mappings=active_mappings or [],
        approved_at=NOW + timedelta(hours=3),
    )
    return result, paths, mapping_writer


def test_approval_uses_google_name_and_coordinate_but_preserves_service_region(
    tmp_path: Path,
) -> None:
    dataset = _dataset(_record("cafe_dn_001", name="Tên master cũ"))
    (approval, mapping, approval_path, mapping_path), paths, writer = _approve(
        tmp_path,
        dataset,
        _observation(),
    )

    stored = GoogleMapsMappingApproval.model_validate_json(
        approval_path.read_bytes()
    )
    assert stored == approval
    assert mapping.status is MappingStatus.CONFIRMED
    assert mapping.entity_id == "cafe_dn_001"
    assert mapping.external_id == GOOGLE_TOKEN
    assert mapping.attributes["google_place_name"] == "Tên chính xác trên Google Maps"
    assert mapping.attributes["google_latitude"] == 16.061
    assert mapping.attributes["google_longitude"] == 108.224
    assert mapping.attributes["service_region_city_id"] == "city_da_nang"
    assert mapping.attributes["service_region_city"] == "Đà Nẵng"
    assert mapping.attributes["human_mapping_approval_hash"] == approval.approval_hash
    assert approval.observation_file_sha256 == hashlib.sha256(
        paths["observation"].read_bytes()
    ).hexdigest()
    assert approval.canonical_dataset_id == dataset.dataset_id
    assert approval.canonical_dataset_hash == dataset.dataset_hash
    assert mapping_path == writer.path_for("cafe_dn_001")


def test_approval_rejects_verified_master_coordinate_fallback(tmp_path: Path) -> None:
    dataset = _dataset(_record("cafe_dn_001", name="Tên master cũ"))

    with pytest.raises(GoogleMapsMappingApprovalError, match="fallback coordinates"):
        _approve(
            tmp_path,
            dataset,
            _observation(accuracy="verified_master_fallback"),
        )


def test_approval_rejects_viewport_coordinate_when_detail_url_has_place_coordinate(
    tmp_path: Path,
) -> None:
    dataset = _dataset(_record("cafe_dn_001", name="TÃªn master cÅ©"))
    source_url = (
        "https://www.google.com/maps/place/google-reference/"
        "@16.061,108.224,17z/"
        f"data=!4m7!3m6!1s{GOOGLE_TOKEN}!8m2!3d16.062!4d108.225"
    )
    observation = GoogleMapsPlaceObservation.model_validate(
        {**_observation().model_dump(), "source_url": source_url}
    )

    with pytest.raises(
        GoogleMapsMappingApprovalError,
        match="do not match Google place coordinates",
    ):
        _approve(tmp_path, dataset, observation)


def test_approval_rejects_token_owned_by_another_active_mapping(
    tmp_path: Path,
) -> None:
    dataset = _dataset(
        _record("cafe_dn_001", name="Tên master cũ"),
        _record("cafe_dn_002", name="Địa điểm khác"),
    )
    owner = ExternalEntityMapping(
        mapping_id="google-maps-cafe_dn_002",
        entity_id="cafe_dn_002",
        entity_type=EntityType.CAFE,
        source_id="google-maps-web",
        external_id=GOOGLE_TOKEN,
        external_url=GOOGLE_URL,
        status=MappingStatus.CONFIRMED,
        confidence=1.0,
        matched_at=NOW,
        verified_at=NOW,
        last_checked_at=NOW,
    )

    with pytest.raises(GoogleMapsMappingApprovalError, match="another active mapping"):
        _approve(
            tmp_path,
            dataset,
            _observation(),
            active_mappings=[owner],
        )


def test_approval_rejects_resolution_bound_to_another_place(tmp_path: Path) -> None:
    dataset = _dataset(_record("cafe_dn_001", name="Tên master cũ"))
    observation = _observation()
    paths = _write_inputs(tmp_path, dataset, observation)
    resolution = _resolution(observation).model_copy(
        update={"place_id": "cafe_dn_999"}
    )
    paths["resolution"].write_text(
        resolution.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(GoogleMapsMappingApprovalError, match="place binding"):
        approve_google_maps_mapping(
            paths["dataset"],
            paths["observation"],
            paths["resolution"],
            paths["decision"],
            reviewer="Oanhh",
            approval_writer=GoogleMapsMappingApprovalWriter(tmp_path / "approvals"),
            mapping_writer=CurrentGoogleMapsMappingWriter(tmp_path / "mappings"),
            approved_at=NOW + timedelta(hours=3),
        )


def test_approval_accepts_auto_confirm_identity_when_place_decision_needs_review(
    tmp_path: Path,
) -> None:
    observation = _observation()
    dataset = _dataset(_record("cafe_dn_001", name="TÃªn master cÅ©"))
    mapping = _mapping().model_copy(
        update={
            "attributes": {
                **_mapping().attributes,
                "master_name": observation.name,
                "master_address": observation.address,
                "master_latitude": observation.location.latitude,
                "master_longitude": observation.location.longitude,
            }
        }
    )
    resolution = GoogleMapsMappingResolver(
        clock=lambda: NOW + timedelta(hours=1)
    ).resolve(mapping, observation, canonical_url=str(observation.source_url))
    assert resolution.status.value == "auto_confirm"
    decision = _decision(observation).model_copy(
        update={
            "status": GoogleMapsDecisionStatus.REVIEW,
            "reason_codes": ["OPENING_STATUS_UNAVAILABLE"],
        }
    )
    paths = {
        "dataset": tmp_path / "dataset.json",
        "observation": tmp_path / "observation.json",
        "resolution": tmp_path / "resolution.json",
        "decision": tmp_path / "decision.json",
    }
    for key, value in (
        ("dataset", dataset),
        ("observation", observation),
        ("resolution", resolution),
        ("decision", decision),
    ):
        paths[key].write_text(value.model_dump_json(indent=2) + "\n", encoding="utf-8")

    approval, published, _, _ = approve_google_maps_mapping(
        paths["dataset"],
        paths["observation"],
        paths["resolution"],
        paths["decision"],
        reviewer="Oanhh",
        approval_writer=GoogleMapsMappingApprovalWriter(tmp_path / "approvals"),
        mapping_writer=CurrentGoogleMapsMappingWriter(tmp_path / "mappings"),
        approved_at=NOW + timedelta(hours=3),
    )

    assert approval.resolution_status.value == "auto_confirm"
    assert approval.decision_status is GoogleMapsDecisionStatus.REVIEW
    assert published.status is MappingStatus.CONFIRMED


def test_approve_google_maps_mapping_cli_uses_default_output_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dataset = _dataset(_record("cafe_dn_001", name="Tên master cũ"))
    paths = _write_inputs(tmp_path, dataset, _observation())
    monkeypatch.chdir(tmp_path)

    exit_code = cli_module.main(
        [
            "approve-google-maps-mapping",
            "--canonical-dataset",
            str(paths["dataset"]),
            "--observation",
            str(paths["observation"]),
            "--resolution",
            str(paths["resolution"]),
            "--decision",
            str(paths["decision"]),
            "--reviewer",
            "Oanhh",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "place_id=cafe_dn_001" in output
    assert f"external_id={GOOGLE_TOKEN}" in output
    assert f"approval={Path('data/approvals/google_maps_mapping')}" in output
    assert f"mapping={Path('data/current/google_maps_mappings')}" in output
    assert (tmp_path / "data/current/google_maps_mappings/cafe_dn_001.json").exists()


def test_approve_google_maps_mapping_cli_returns_two_for_unsafe_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dataset = _dataset(_record("cafe_dn_001", name="Tên master cũ"))
    paths = _write_inputs(
        tmp_path,
        dataset,
        _observation(accuracy="verified_master_fallback"),
    )

    exit_code = cli_module.main(
        [
            "approve-google-maps-mapping",
            "--canonical-dataset",
            str(paths["dataset"]),
            "--observation",
            str(paths["observation"]),
            "--resolution",
            str(paths["resolution"]),
            "--decision",
            str(paths["decision"]),
            "--reviewer",
            "Oanhh",
            "--approval-output-dir",
            str(tmp_path / "approvals"),
            "--current-mapping-dir",
            str(tmp_path / "mappings"),
        ]
    )

    error = capsys.readouterr().err
    assert exit_code == 2
    assert "Cannot approve Google Maps mapping" in error
    assert "fallback coordinates" in error


def test_exact_mapping_approval_publishes_quarantined_evidence(
    tmp_path: Path,
) -> None:
    dataset = _dataset(_record("cafe_dn_001", name="Tên master cũ"))
    (approval, _, approval_path, _), _, _ = _approve(
        tmp_path,
        dataset,
        _observation(),
    )

    patch = build_google_maps_canonical_refresh_patch(
        dataset,
        tmp_path,
        tmp_path,
        entity_types=[EntityType.CAFE],
        mapping_approval_root=tmp_path / "approvals",
        generated_at=NOW + timedelta(hours=4),
    )

    assert patch.deferred == []
    assert patch.human_approved_count == 1
    assert len(patch.records) == 1
    evidence = patch.records[0].evidence
    assert evidence.mapping_approval_id == approval.approval_id
    assert evidence.mapping_approval_hash == approval.approval_hash
    assert evidence.mapping_approval_reviewer == "Oanhh"
    assert evidence.mapping_approval_approved_at == approval.approved_at
    assert evidence.mapping_approval_file_sha256 == hashlib.sha256(
        approval_path.read_bytes()
    ).hexdigest()
    assert evidence.mapping_approval_relative_path.startswith(
        "place=cafe_dn_001/approval="
    )

    refreshed = apply_google_maps_canonical_refresh_patch(dataset, patch)
    updated = refreshed.records[0]
    assert updated.name == "Tên chính xác trên Google Maps"
    assert updated.coordinates.source == "google-maps-web"
    assert (
        updated.data["google_maps_refresh"]["mapping_approval_id"]
        == approval.approval_id
    )


def test_reviewed_complete_weekly_schedule_is_source_and_approval_pinned(
    tmp_path: Path,
) -> None:
    dataset = _dataset(_record("cafe_dn_001", name="TÃªn master cÅ©"))
    observation = _observation()
    (approval, _, _, _), _, _ = _approve(tmp_path, dataset, observation)
    days = [
        {
            "day": day,
            "intervals": [
                {
                    "opens_at": "14:00:00",
                    "closes_at": "22:00:00",
                    "closes_next_day": False,
                }
            ],
            "closed": False,
            "open_24_hours": False,
        }
        for day in (
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        )
    ]
    schedule_path = tmp_path / "reviewed-schedule.json"
    schedule_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "reviewer": "Oanhh",
                "reviewed_at": (NOW + timedelta(hours=3)).isoformat(),
                "records": [
                    {
                        "place_id": observation.place_id,
                        "observation_id": observation.observation_id,
                        "timezone": "Asia/Ho_Chi_Minh",
                        "days": days,
                        "reason": "reviewed Google weekly schedule",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    patch = build_google_maps_canonical_refresh_patch(
        dataset,
        tmp_path,
        tmp_path,
        entity_types=[EntityType.CAFE],
        mapping_approval_root=tmp_path / "approvals",
        weekly_schedule_review_path=schedule_path,
        generated_at=NOW + timedelta(hours=4),
    )

    record = patch.records[0]
    assert record.weekly_opening is not None
    assert len(record.weekly_opening.days) == 7
    assert record.weekly_opening.verification_status.value == "human_verified"
    assert record.evidence.mapping_approval_id == approval.approval_id
    assert record.evidence.schedule_review_reviewer == "Oanhh"
    assert record.evidence.schedule_review_file_sha256 == hashlib.sha256(
        schedule_path.read_bytes()
    ).hexdigest()
    refreshed = apply_google_maps_canonical_refresh_patch(dataset, patch)
    assert refreshed.records[0].data["opening_hours"]["open"] == "14:00"
    assert refreshed.records[0].data["opening_hours"]["close"] == "22:00"


def test_mapping_approval_for_another_dataset_is_not_accepted(
    tmp_path: Path,
) -> None:
    approved_dataset = _dataset(_record("cafe_dn_001", name="Tên master cũ"))
    _approve(tmp_path, approved_dataset, _observation())
    current_dataset = _dataset(
        _record("cafe_dn_001", name="Tên canonical đã thay đổi")
    )

    patch = build_google_maps_canonical_refresh_patch(
        current_dataset,
        tmp_path,
        tmp_path,
        entity_types=[EntityType.CAFE],
        mapping_approval_root=tmp_path / "approvals",
        generated_at=NOW + timedelta(hours=4),
    )

    assert patch.records == []
    assert patch.human_approved_count == 0
    assert patch.deferred[0].disposition.value == "quarantine"


def test_mapping_approval_provenance_is_all_or_none(tmp_path: Path) -> None:
    dataset = _dataset(_record("cafe_dn_001", name="Tên master cũ"))
    _approve(tmp_path, dataset, _observation())
    patch = build_google_maps_canonical_refresh_patch(
        dataset,
        tmp_path,
        tmp_path,
        entity_types=[EntityType.CAFE],
        mapping_approval_root=tmp_path / "approvals",
        generated_at=NOW + timedelta(hours=4),
    )
    values = patch.records[0].evidence.model_dump(mode="json")
    values["mapping_approval_hash"] = None

    with pytest.raises(ValueError, match="must be supplied together"):
        CanonicalGoogleMapsEvidence.model_validate(values)


@pytest.mark.parametrize("mutated_artifact", ["observation", "decision", "approval"])
def test_mapping_approval_is_not_accepted_when_pinned_bytes_change(
    tmp_path: Path,
    mutated_artifact: str,
) -> None:
    dataset = _dataset(_record("cafe_dn_001", name="Tên master cũ"))
    (_, _, approval_path, _), paths, _ = _approve(
        tmp_path,
        dataset,
        _observation(),
    )
    artifact_path = approval_path if mutated_artifact == "approval" else paths[
        mutated_artifact
    ]
    artifact_path.write_bytes(artifact_path.read_bytes() + b"\n")

    patch = build_google_maps_canonical_refresh_patch(
        dataset,
        tmp_path,
        tmp_path,
        entity_types=[EntityType.CAFE],
        mapping_approval_root=tmp_path / "approvals",
        generated_at=NOW + timedelta(hours=4),
    )

    assert patch.records == []
    assert patch.human_approved_count == 0
    assert patch.deferred[0].disposition.value == "quarantine"


def test_apply_google_maps_refresh_cli_accepts_mapping_approval_root() -> None:
    arguments = cli_module.build_parser().parse_args(
        [
            "apply-google-maps-canonical-refresh",
            "--canonical-dataset",
            "canonical.json",
            "--mapping-approval-root",
            "data/approvals/google_maps_mapping",
        ]
    )

    assert arguments.mapping_approval_root == Path(
        "data/approvals/google_maps_mapping"
    )

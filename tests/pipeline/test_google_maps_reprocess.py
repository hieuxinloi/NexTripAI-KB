from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from nextrip_pipeline.decision_gate import (
    GoogleMapsDecisionGate,
    GoogleMapsDecisionStatus,
    GoogleMapsDecisionWriter,
)
from nextrip_pipeline import cli as cli_module
from nextrip_pipeline.jobs.google_maps_reprocess import (
    GoogleMapsQualityReprocessor,
    GoogleMapsReprocessItemStatus,
    GoogleMapsReprocessSummaryWriter,
)
from nextrip_pipeline.preprocessing import NormalizedGoogleMapsWriter
from nextrip_pipeline.publishing import CurrentPlaceWriter
from nextrip_pipeline.quality import (
    CurrentGoogleMapsMappingWriter,
    GoogleMapsMappingResolutionWriter,
    GoogleMapsMappingResolver,
    LLMReviewQueueConfig,
    LLMReviewQueueDisposition,
    LLMReviewRequestWriter,
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
from nextrip_pipeline.validators import (
    GoogleMapsValidationWriter,
    GoogleMapsValidatorOrchestrator,
)


NOW = datetime(2026, 8, 19, 8, tzinfo=timezone.utc)


def _mapping(
    place_id: str,
    name: str,
    *,
    status: MappingStatus = MappingStatus.CONFIRMED,
) -> ExternalEntityMapping:
    return ExternalEntityMapping(
        mapping_id=f"maps-{place_id}",
        entity_id=place_id,
        entity_type=EntityType.CAFE,
        source_id="google-maps-web",
        external_id=name,
        status=status,
        confidence=0.9 if status is MappingStatus.CONFIRMED else 0.7,
        matched_at=NOW - timedelta(days=1),
        verified_at=(
            NOW - timedelta(hours=2)
            if status is MappingStatus.CONFIRMED
            else None
        ),
        attributes={
            "master_name": name,
            "master_address": "1 Bach Dang, Da Nang, Vietnam",
            "master_city": "Da Nang",
            "city_id": "city_da_nang",
            "master_latitude": 16.0601,
            "master_longitude": 108.2201,
        },
    )


def _observation(
    place_id: str,
    name: str,
    *,
    source_record_id: str,
    observed_at: datetime = NOW,
    location: GeoPoint | None = None,
) -> GoogleMapsPlaceObservation:
    opening = OpeningStatusObservation(
        observation_id=f"{source_record_id}:opening",
        run_id=f"crawl-{source_record_id}",
        place_id=place_id,
        source_record_ids=[source_record_id],
        local_date=date(2026, 8, 19),
        status=DailyOpeningStatus.UNKNOWN,
        observed_at=observed_at,
    )
    return GoogleMapsPlaceObservation(
        observation_id=f"{source_record_id}:place-status",
        run_id=f"crawl-{source_record_id}",
        place_id=place_id,
        source_record_id=source_record_id,
        source_id="google-maps-web",
        source_url=f"https://www.google.com/maps/place/{place_id}",
        name=name,
        category="Cafe",
        address="1 Bach Dang, Da Nang, Vietnam",
        location=location,
        opening=opening,
        observed_at=observed_at,
    )


def _processor(tmp_path, *, llm_max_requests: int = 5):
    return GoogleMapsQualityReprocessor(
        NormalizedGoogleMapsWriter(tmp_path / "output-normalized"),
        GoogleMapsMappingResolutionWriter(tmp_path / "resolution"),
        GoogleMapsValidationWriter(tmp_path / "validation"),
        GoogleMapsDecisionWriter(tmp_path / "decision"),
        CurrentGoogleMapsMappingWriter(tmp_path / "current-mapping"),
        CurrentPlaceWriter(tmp_path / "current-place", clock=lambda: NOW),
        GoogleMapsReprocessSummaryWriter(tmp_path / "reports"),
        resolver=GoogleMapsMappingResolver(clock=lambda: NOW),
        validator=GoogleMapsValidatorOrchestrator(clock=lambda: NOW),
        decision_gate=GoogleMapsDecisionGate(clock=lambda: NOW),
        llm_review_writer=LLMReviewRequestWriter(
            LLMReviewQueueConfig(
                root_directory=tmp_path / "llm-review",
                max_requests_per_run=llm_max_requests,
                max_tokens_per_run=10_000,
            ),
            clock=lambda: NOW,
        ),
        clock=lambda: NOW,
    )


def test_reprocess_selects_latest_and_publishes_confirmed_with_fallback(
    tmp_path,
) -> None:
    input_writer = NormalizedGoogleMapsWriter(tmp_path / "input-normalized")
    old = _observation(
        "cafe-1",
        "Cafe Latest",
        source_record_id="source-old",
        observed_at=NOW - timedelta(hours=1),
        location=GeoPoint(
            latitude=16.0602,
            longitude=108.2202,
            source="google-maps-web",
            accuracy="google_maps_place_page",
        ),
    )
    latest = _observation(
        "cafe-1", "Cafe Latest", source_record_id="source-latest"
    )
    orphan = _observation(
        "cafe-orphan", "Cafe Orphan", source_record_id="source-orphan"
    )
    input_writer.write(old)
    input_writer.write(latest)
    input_writer.write(orphan)

    result = _processor(tmp_path).run(
        [
            _mapping("cafe-1", "Cafe Latest"),
            _mapping("cafe-without-observation", "Cafe Missing"),
        ],
        observation_root=tmp_path / "input-normalized",
        run_id="quality-reprocess-1",
    )

    summary = result.summary
    assert summary.observation_candidate_count == 3
    assert summary.selected_observation_count == 2
    assert summary.processed_count == 1
    assert summary.succeeded_count == 1
    assert summary.coordinate_fallback_count == 1
    assert summary.published_mapping_count == 0
    assert summary.published_place_count == 1
    assert summary.missing_mapping_place_ids == ["cafe-orphan"]
    assert summary.missing_observation_place_ids == ["cafe-without-observation"]
    assert summary.resolution_status_counts == {"review": 1}
    assert summary.decision_status_counts == {"pass": 1}

    item = summary.items[0]
    assert item.status is GoogleMapsReprocessItemStatus.SUCCEEDED
    assert item.source_observation_id == latest.observation_id
    assert item.source_record_id == latest.source_record_id
    assert item.observed_at == latest.observed_at
    assert item.derived_observation_id != latest.observation_id
    assert item.current_place_published is True

    assert item.normalized_path is not None
    derived = GoogleMapsPlaceObservation.model_validate_json(
        Path(item.normalized_path).read_text(encoding="utf-8")
    )
    assert derived.run_id == "quality-reprocess-1"
    assert derived.source_record_id == latest.source_record_id
    assert derived.observed_at == latest.observed_at
    assert derived.opening.observed_at == latest.opening.observed_at
    assert derived.location is not None
    assert derived.location.source == "verified-master-data"
    assert derived.location.accuracy == "verified_master_fallback"
    assert result.summary_path.exists()

    with pytest.raises(FileExistsError, match="run already exists"):
        _processor(tmp_path).run(
            [_mapping("cafe-1", "Cafe Latest")],
            observations=[latest],
            run_id="quality-reprocess-1",
        )


def test_reprocess_ignores_output_from_an_earlier_reprocess_run(tmp_path) -> None:
    original = _observation(
        "cafe-1",
        "Cafe Original",
        source_record_id="source-original",
        observed_at=NOW - timedelta(hours=1),
    )
    derived = _observation(
        "cafe-1",
        "Cafe Derived Must Not Become Evidence",
        source_record_id="source-original",
        observed_at=NOW,
    ).model_copy(
        update={
            "observation_id": (
                f"{original.observation_id}:quality-reprocess:previous"
            )
        }
    )

    summary = _processor(tmp_path).run(
        [_mapping("cafe-1", "Cafe Original")],
        observations=[original, derived],
        run_id="quality-reprocess-no-recursion",
    ).summary

    assert summary.selected_observation_count == 1
    assert summary.items[0].source_observation_id == original.observation_id


def test_reprocess_migrates_legacy_string_menu_source_in_memory(tmp_path) -> None:
    observation = _observation(
        "cafe-legacy",
        "Cafe Legacy",
        source_record_id="source-legacy",
    )
    document = observation.model_dump(mode="json")
    document["menu_source"] = "https://maps.app.goo.gl/not-a-menu-image"
    path = tmp_path / "observation=legacy.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    summary = _processor(tmp_path).run(
        [_mapping("cafe-legacy", "Cafe Legacy")],
        observation_paths=[path],
        run_id="quality-reprocess-legacy-menu-source",
    ).summary

    assert summary.succeeded_count == 1
    assert summary.errors == []
    derived = GoogleMapsPlaceObservation.model_validate_json(
        Path(summary.items[0].normalized_path).read_text(encoding="utf-8")
    )
    assert derived.menu_source is None


@pytest.mark.parametrize("status", [MappingStatus.PENDING_REVIEW, MappingStatus.REJECTED])
def test_explicit_reprocess_reevaluates_non_active_mapping_states(
    tmp_path, status: MappingStatus
) -> None:
    mapping = _mapping("cafe-retry", "Cafe Retry", status=status)
    observation = _observation(
        "cafe-retry",
        "Cafe Retry",
        source_record_id="source-retry",
        location=GeoPoint(
            latitude=16.0602,
            longitude=108.2202,
            source="google-maps-web",
            accuracy="google_maps_place_page",
        ),
    )

    summary = _processor(tmp_path).run(
        [mapping],
        observations=[observation],
        run_id=f"quality-reprocess-{status.value}",
    ).summary

    assert summary.processed_count == 1
    assert summary.resolution_status_counts == {"auto_confirm": 1}
    assert summary.published_mapping_count == 1


def test_auto_matched_reviews_use_bounded_llm_queue_and_do_not_publish(
    tmp_path,
) -> None:
    mappings = [
        _mapping("cafe-1", "Cafe One", status=MappingStatus.AUTO_MATCHED),
        _mapping("cafe-2", "Cafe Two", status=MappingStatus.AUTO_MATCHED),
    ]
    observations = [
        _observation("cafe-1", "Cafe One", source_record_id="source-1"),
        _observation("cafe-2", "Cafe Two", source_record_id="source-2"),
    ]

    summary = _processor(tmp_path, llm_max_requests=1).run(
        mappings,
        observations=observations,
        run_id="quality-reprocess-llm",
    ).summary

    assert summary.succeeded_count == 2
    assert summary.resolution_status_counts == {"review": 2}
    assert summary.decision_status_counts == {"review": 2}
    assert summary.published_mapping_count == 0
    assert summary.published_place_count == 0
    assert summary.llm_queued_count == 1
    assert summary.llm_queue_status_counts == {
        "queued": 1,
        "run_request_cap_reached": 1,
    }
    assert {
        item.llm_queue_disposition for item in summary.items
    } == {
        LLMReviewQueueDisposition.QUEUED,
        LLMReviewQueueDisposition.RUN_REQUEST_CAP_REACHED,
    }
    assert all(
        item.decision_status is GoogleMapsDecisionStatus.REVIEW
        for item in summary.items
    )


def test_auto_confirmed_mapping_and_pass_place_are_published(tmp_path) -> None:
    mapping = _mapping("cafe-strong", "Cafe Strong", status=MappingStatus.AUTO_MATCHED)
    observation = _observation(
        "cafe-strong",
        "Cafe Strong",
        source_record_id="source-strong",
        location=GeoPoint(
            latitude=16.0602,
            longitude=108.2202,
            source="google-maps-web",
            accuracy="google_maps_place_page",
        ),
    )

    summary = _processor(tmp_path).run(
        [mapping],
        observations=[observation],
        run_id="quality-reprocess-strong",
    ).summary

    assert summary.resolution_status_counts == {"auto_confirm": 1}
    assert summary.decision_status_counts == {"pass": 1}
    assert summary.published_mapping_count == 1
    assert summary.published_place_count == 1
    assert summary.llm_queued_count == 0
    assert summary.items[0].current_mapping_path is not None
    assert summary.items[0].current_place_path is not None


def test_hard_identity_conflict_is_persisted_and_never_publishes_place(
    tmp_path,
) -> None:
    mapping = _mapping(
        "cafe-wrong-city",
        "Cafe Strong",
        status=MappingStatus.AUTO_MATCHED,
    )
    observation = _observation(
        "cafe-wrong-city",
        "Cafe Strong",
        source_record_id="source-wrong-city",
        location=GeoPoint(
            latitude=21.0285,
            longitude=105.8542,
            source="google-maps-web",
            accuracy="google_maps_place_page",
        ),
    ).model_copy(update={"address": "1 Trang Tien, Ha Noi, Vietnam"})

    processor = _processor(tmp_path)
    summary = processor.run(
        [mapping],
        observations=[observation],
        run_id="quality-reprocess-reject",
    ).summary

    assert summary.resolution_status_counts == {"reject": 1}
    assert summary.decision_status_counts == {"quarantine": 1}
    assert summary.published_mapping_count == 1
    assert summary.published_place_count == 0
    assert summary.items[0].current_mapping_path is not None
    rejected = CurrentGoogleMapsMappingWriter(
        tmp_path / "current-mapping"
    ).get("cafe-wrong-city")
    assert rejected is not None
    assert rejected.status is MappingStatus.REJECTED


def test_invalid_observations_and_item_failures_are_reported_without_stopping(
    tmp_path,
) -> None:
    invalid_path = tmp_path / "observation=broken.json"
    invalid_path.write_text("not-json", encoding="utf-8")
    generic_shell = _observation(
        "cafe-shell", "Google Maps", source_record_id="source-shell"
    )
    good = _observation("cafe-good", "Cafe Good", source_record_id="source-good")

    summary = _processor(tmp_path).run(
        [
            _mapping("cafe-shell", "Cafe Shell"),
            _mapping("cafe-good", "Cafe Good"),
        ],
        observations=[generic_shell, good],
        observation_paths=[invalid_path],
        run_id="quality-reprocess-errors",
    ).summary

    assert summary.processed_count == 1
    assert summary.succeeded_count == 1
    assert summary.missing_observation_place_ids == ["cafe-shell"]
    assert {error.stage for error in summary.errors} == {
        "load_observation",
        "validate_observation",
    }
    assert summary.items[0].status is GoogleMapsReprocessItemStatus.SUCCEEDED


def test_reprocess_cli_parser_has_local_only_defaults() -> None:
    arguments = cli_module.build_parser().parse_args(
        ["reprocess-google-maps", "--manifest", "manifest.json"]
    )

    assert arguments.observation_root == Path("data/normalized")
    assert arguments.normalized_dir == Path("data/normalized")
    assert arguments.quality_dir == Path("data/quality/google_maps_mapping")
    assert arguments.validation_dir == Path("data/validation")
    assert arguments.decision_dir == Path("data/decisions")
    assert arguments.current_mapping_dir == Path(
        "data/current/google_maps_mappings"
    )
    assert arguments.current_place_dir == Path("data/current/place")
    assert arguments.summary_dir == Path("data/runs/google_maps_reprocess")
    assert arguments.entity_type == []
    assert arguments.disable_llm_review_queue is False
    assert not hasattr(arguments, "headed")
    assert not hasattr(arguments, "raw_dir")


def test_reprocess_cli_filters_default_entities_and_fails_on_item_failure(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    mappings = [
        _mapping("cafe-1", "Cafe One"),
        ExternalEntityMapping(
            mapping_id="maps-hotel-1",
            entity_id="hotel-1",
            entity_type=EntityType.HOTEL,
            source_id="google-maps-web",
            external_id="Hotel One",
            status=MappingStatus.AUTO_MATCHED,
            matched_at=NOW,
        ),
    ]
    captured = {}

    class FakeReprocessor:
        def __init__(self, *args, **kwargs) -> None:
            captured["constructor_args"] = args
            captured["constructor_kwargs"] = kwargs

        def run(self, received_mappings, **kwargs):
            captured["mappings"] = received_mappings
            captured["run_kwargs"] = kwargs
            summary = SimpleNamespace(
                selected_observation_count=1,
                processed_count=1,
                succeeded_count=0,
                failed_count=1,
                coordinate_fallback_count=0,
                published_mapping_count=0,
                published_place_count=0,
                llm_queued_count=0,
                missing_mapping_place_ids=[],
                missing_observation_place_ids=[],
                errors=[],
                items=[
                    SimpleNamespace(
                        status=GoogleMapsReprocessItemStatus.FAILED,
                        place_id="cafe-1",
                        error="test failure",
                    )
                ],
            )
            return SimpleNamespace(summary=summary, summary_path=Path("report.json"))

    monkeypatch.setattr(cli_module, "load_google_maps_manifest", lambda path: mappings)
    monkeypatch.setattr(cli_module, "GoogleMapsQualityReprocessor", FakeReprocessor)
    monkeypatch.setattr(
        cli_module,
        "PlaywrightBrowserClient",
        lambda *args, **kwargs: pytest.fail("reprocess command must not use a browser"),
    )

    exit_code = cli_module.main(
        ["reprocess-google-maps", "--manifest", "manifest.json"]
    )

    captured_output = capsys.readouterr()
    assert exit_code == 1
    assert [mapping.entity_id for mapping in captured["mappings"]] == ["cafe-1"]
    assert captured["run_kwargs"]["observation_root"] == Path("data/normalized")
    assert "selected=1 processed=1 succeeded=0 failed=1" in captured_output.out
    assert "failed place_id=cafe-1" in captured_output.err


def test_reprocess_cli_can_disable_llm_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    class FakeReprocessor:
        def __init__(self, *args, **kwargs) -> None:
            captured["llm_review_writer"] = kwargs["llm_review_writer"]

        def run(self, received_mappings, **kwargs):
            summary = SimpleNamespace(
                selected_observation_count=0,
                processed_count=0,
                succeeded_count=0,
                failed_count=0,
                coordinate_fallback_count=0,
                published_mapping_count=0,
                published_place_count=0,
                llm_queued_count=0,
                missing_mapping_place_ids=[],
                missing_observation_place_ids=[],
                errors=[],
                items=[],
            )
            return SimpleNamespace(summary=summary, summary_path=Path("report.json"))

    monkeypatch.setattr(cli_module, "load_google_maps_manifest", lambda path: [])
    monkeypatch.setattr(cli_module, "GoogleMapsQualityReprocessor", FakeReprocessor)

    exit_code = cli_module.main(
        [
            "reprocess-google-maps",
            "--manifest",
            "manifest.json",
            "--disable-llm-review-queue",
        ]
    )

    assert exit_code == 0
    assert captured["llm_review_writer"] is None

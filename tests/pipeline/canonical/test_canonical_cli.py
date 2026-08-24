from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from io import BytesIO, TextIOWrapper
from pathlib import Path

from nextrip_pipeline.canonical import (
    ApprovedCanonicalInvalidation,
    CanonicalInvalidationReason,
    CanonicalInvalidationWriter,
    QuarantinedCanonicalIdentity,
)
from nextrip_pipeline.canonical.models import LegacyPlaceSlot
from nextrip_pipeline.canonical.resolver import build_canonical_identity_manifest
from nextrip_pipeline.cli import (
    _configure_utf8_standard_streams,
    build_parser,
    main,
)
from nextrip_pipeline.schemas import (
    BusinessStatus,
    DailyOpeningStatus,
    EntityType,
    GoogleMapsPlaceObservation,
    OpeningStatusObservation,
    VerificationStatus,
)


UTC = timezone.utc


def test_cli_reconfigures_legacy_windows_stream_for_vietnamese(
    monkeypatch,
) -> None:
    buffer = BytesIO()
    stream = TextIOWrapper(buffer, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stream)

    _configure_utf8_standard_streams()
    print("Quy Nhơn")
    stream.flush()

    assert stream.encoding == "utf-8"
    assert buffer.getvalue().decode("utf-8").splitlines() == ["Quy Nhơn"]


def test_canonical_cli_defaults_are_safe() -> None:
    legacy_import = "legacy-import"
    audit = build_parser().parse_args(
        ["audit-canonical-identities", "--master-dir", legacy_import]
    )
    build = build_parser().parse_args(
        ["build-canonical-manifest", "--master-dir", legacy_import]
    )
    approve_invalidation = build_parser().parse_args(
        [
            "approve-canonical-invalidation",
            "--place-id",
            "cafe_dn_001",
            "--observation",
            "observation.json",
            "--identity-source",
            "fixture/cafe_final.json",
            "--reviewer",
            "reviewer@example.com",
        ]
    )
    build_enrichment = build_parser().parse_args(
        ["build-canonical-replacement-enrichment"]
    )
    materialize = build_parser().parse_args(
        ["materialize-canonical-dataset", "--master-dir", legacy_import]
    )
    propose = build_parser().parse_args(
        ["propose-canonical-replacements", "--master-dir", legacy_import]
    )

    assert audit.output == Path("data/reports/canonical/duplicate-evidence.json")
    assert build.decisions == Path("config/canonical-identity-decisions.json")
    assert build.approved_invalidation_dir == Path("data/canonical/invalidations")
    assert build.ignore_master_duplicate_tags is False
    assert approve_invalidation.manifest == Path(
        "config/generated/canonical-identity-manifest.json"
    )
    assert approve_invalidation.reason == "entity_type_ineligible"
    assert approve_invalidation.identity_source == Path("fixture/cafe_final.json")
    assert approve_invalidation.output_dir == Path("data/canonical/invalidations")
    assert materialize.output_dir == Path("data/canonical/datasets")
    assert materialize.decisions == Path("config/canonical-identity-decisions.json")
    assert materialize.duplicate_evidence == Path(
        "data/reports/canonical/duplicate-evidence.json"
    )
    assert materialize.review_correction is None
    assert materialize.replacement_enrichment is None
    assert materialize.allow_unresolved_review is False
    assert build_enrichment.approved_replacement_dir == Path(
        "data/canonical/replacements"
    )
    assert build_enrichment.raw_dir == Path("data/raw")
    assert build_enrichment.output_dir == Path("data/canonical/replacement-enrichments")
    assert propose.review_correction is None
    assert propose.candidate_entity_type is None

    with_correction = build_parser().parse_args(
        [
            "materialize-canonical-dataset",
            "--master-dir",
            legacy_import,
            "--review-correction",
            "data/canonical/review-corrections/overlay.json",
        ]
    )
    assert with_correction.review_correction == Path(
        "data/canonical/review-corrections/overlay.json"
    )

    with_enrichment = build_parser().parse_args(
        [
            "materialize-canonical-dataset",
            "--master-dir",
            legacy_import,
            "--replacement-enrichment",
            "data/canonical/replacement-enrichments/overlay.json",
        ]
    )
    assert with_enrichment.replacement_enrichment == Path(
        "data/canonical/replacement-enrichments/overlay.json"
    )

    propose_with_correction = build_parser().parse_args(
        [
            "propose-canonical-replacements",
            "--master-dir",
            legacy_import,
            "--review-correction",
            "data/canonical/review-corrections/overlay.json",
        ]
    )
    assert propose_with_correction.review_correction == Path(
        "data/canonical/review-corrections/overlay.json"
    )

    cross_type = build_parser().parse_args(
        [
            "propose-canonical-replacements",
            "--master-dir",
            legacy_import,
            "--entity-type",
            "nightlife",
            "--candidate-entity-type",
            "cafe",
        ]
    )
    assert cross_type.entity_type == ["nightlife"]
    assert cross_type.candidate_entity_type == "cafe"


def test_invalidation_types_are_public_package_exports() -> None:
    assert ApprovedCanonicalInvalidation.__name__ == "ApprovedCanonicalInvalidation"
    assert CanonicalInvalidationReason.ENTITY_TYPE_INELIGIBLE.value == (
        "entity_type_ineligible"
    )
    assert CanonicalInvalidationWriter.__name__ == "CanonicalInvalidationWriter"
    assert QuarantinedCanonicalIdentity.__name__ == ("QuarantinedCanonicalIdentity")


def test_approve_canonical_invalidation_cli_writes_temp_approval(
    tmp_path: Path,
    capsys,
) -> None:
    observed_at = datetime(2026, 8, 22, 8, tzinfo=UTC)
    manifest = build_canonical_identity_manifest(
        [
            LegacyPlaceSlot(
                legacy_place_id="cafe_test_001",
                city_id="city_da_nang",
                primary_type=EntityType.CAFE,
            )
        ],
        generated_at=observed_at,
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    observation = GoogleMapsPlaceObservation(
        observation_id="observation-test-invalidation",
        run_id="run-test-invalidation",
        place_id="cafe_test_001",
        source_record_id="source-test-invalidation",
        source_id="google-maps-web",
        source_url="https://www.google.com/maps/place/test",
        name="Test non-travel business",
        category="Advertising agency",
        business_status=BusinessStatus.ACTIVE,
        opening=OpeningStatusObservation(
            observation_id="observation-test-invalidation:opening",
            run_id="run-test-invalidation",
            place_id="cafe_test_001",
            source_record_ids=["source-test-invalidation"],
            local_date=date(2026, 8, 22),
            timezone="Asia/Ho_Chi_Minh",
            status=DailyOpeningStatus.UNKNOWN,
            observed_at=observed_at,
            verification_status=VerificationStatus.PENDING_REVIEW,
        ),
        observed_at=observed_at,
        verification_status=VerificationStatus.PENDING_REVIEW,
    )
    observation_path = tmp_path / "observation.json"
    observation_path.write_text(
        observation.model_dump_json(indent=2),
        encoding="utf-8",
    )
    identity_source = tmp_path / "cafe-master.json"
    identity_source.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "id": "cafe_test_001",
                        "name": "Test non-travel business",
                        "address": "1 Test Street, Da Nang",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    observation = observation.model_copy(
        update={"address": "1 Test Street, Da Nang"},
        deep=True,
    )
    observation_path.write_text(
        observation.model_dump_json(indent=2),
        encoding="utf-8",
    )
    output = tmp_path / "invalidations"

    exit_code = main(
        [
            "approve-canonical-invalidation",
            "--manifest",
            str(manifest_path),
            "--place-id",
            "cafe_test_001",
            "--observation",
            str(observation_path),
            "--identity-source",
            str(identity_source),
            "--reviewer",
            "Oanhh",
            "--output-dir",
            str(output),
        ]
    )

    assert exit_code == 0
    record_path = next((output / "records").glob("*.json"))
    approval = ApprovedCanonicalInvalidation.model_validate_json(
        record_path.read_bytes()
    )
    assert approval.canonical_place_id == "cafe_test_001"
    assert approval.reviewer == "Oanhh"
    stdout = capsys.readouterr().out
    assert f"approval_id={approval.approval_id}" in stdout
    assert f"approval_hash={approval.approval_hash}" in stdout


def test_audit_canonical_identities_fails_closed_without_legacy_import(
    tmp_path: Path,
    capsys,
) -> None:
    output = tmp_path / "duplicate-evidence.json"
    candidate_groups = tmp_path / "candidate-groups.json"
    candidate_groups.write_text(
        json.dumps({"duplicate_identity_candidates": [["cafe_dn_001", "cafe_dn_002"]]}),
        encoding="utf-8",
    )
    missing_legacy_import = tmp_path / "missing-legacy-import"

    exit_code = main(
        [
            "audit-canonical-identities",
            "--candidate-groups",
            str(candidate_groups),
            "--master-dir",
            str(missing_legacy_import),
            "--observation-root",
            str(tmp_path / "observations"),
            "--output",
            str(output),
        ]
    )

    assert exit_code == 2
    assert not output.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Cannot audit canonical identities" in captured.err
    assert str(missing_legacy_import) in captured.err


def test_build_canonical_manifest_fails_closed_without_legacy_import(
    tmp_path: Path,
    capsys,
) -> None:
    manifest_path = tmp_path / "canonical-manifest.json"
    report_path = tmp_path / "canonical-report.json"
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "decisions": [],
                "distinct_decisions": [],
                "replacements": [],
            }
        ),
        encoding="utf-8",
    )
    missing_legacy_import = tmp_path / "missing-legacy-import"

    exit_code = main(
        [
            "build-canonical-manifest",
            "--master-dir",
            str(missing_legacy_import),
            "--decisions",
            str(decisions_path),
            "--output",
            str(manifest_path),
            "--report",
            str(report_path),
            "--approved-replacement-dir",
            str(tmp_path / "approved-replacements"),
            "--approved-invalidation-dir",
            str(tmp_path / "approved-invalidations"),
        ]
    )

    assert exit_code == 2
    assert not manifest_path.exists()
    assert not report_path.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Cannot build canonical manifest" in captured.err
    assert "verified master files are missing" in captured.err

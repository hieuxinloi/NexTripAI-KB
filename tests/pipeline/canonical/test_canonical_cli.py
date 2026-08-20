from __future__ import annotations

import json
from pathlib import Path

from nextrip_pipeline.cli import build_parser, main


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_canonical_cli_defaults_are_safe() -> None:
    audit = build_parser().parse_args(["audit-canonical-identities"])
    build = build_parser().parse_args(["build-canonical-manifest"])
    materialize = build_parser().parse_args(["materialize-canonical-dataset"])

    assert audit.output == Path("data/reports/canonical/duplicate-evidence.json")
    assert build.decisions == Path("config/canonical-identity-decisions.json")
    assert build.ignore_master_duplicate_tags is False
    assert materialize.output_dir == Path("data/canonical/datasets")
    assert materialize.decisions == Path("config/canonical-identity-decisions.json")
    assert materialize.duplicate_evidence == Path(
        "data/reports/canonical/duplicate-evidence.json"
    )
    assert materialize.review_correction is None
    assert materialize.allow_unresolved_review is False

    with_correction = build_parser().parse_args(
        [
            "materialize-canonical-dataset",
            "--review-correction",
            "data/canonical/review-corrections/overlay.json",
        ]
    )
    assert with_correction.review_correction == Path(
        "data/canonical/review-corrections/overlay.json"
    )


def test_audit_canonical_identities_writes_real_evidence(
    tmp_path: Path,
    capsys,
) -> None:
    output = tmp_path / "duplicate-evidence.json"

    exit_code = main(
        [
            "audit-canonical-identities",
            "--candidate-groups",
            str(
                REPOSITORY_ROOT
                / "config"
                / "generated"
                / "google-maps-registry-report.json"
            ),
            "--master-dir",
            str(REPOSITORY_ROOT / "travel_data_verified"),
            "--observation-root",
            str(REPOSITORY_ROOT / "data" / "normalized" / "entity=opening_status"),
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["group_count"] == 88
    assert sum(payload["status_counts"].values()) == 88
    assert "groups=88" in capsys.readouterr().out


def test_build_canonical_manifest_applies_verified_master_duplicate_tags(
    tmp_path: Path,
    capsys,
) -> None:
    manifest_path = tmp_path / "canonical-manifest.json"
    report_path = tmp_path / "canonical-report.json"

    exit_code = main(
        [
            "build-canonical-manifest",
            "--master-dir",
            str(REPOSITORY_ROOT / "travel_data_verified"),
            "--decisions",
            str(REPOSITORY_ROOT / "config" / "canonical-identity-decisions.json"),
            "--output",
            str(manifest_path),
            "--report",
            str(report_path),
            "--approved-replacement-dir",
            str(tmp_path / "approved-replacements"),
        ]
    )

    assert exit_code == 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["source_record_count"] == 692
    identity_count = len(manifest["identities"])
    retired_count = len(manifest["retired_place_ids"])
    vacancy_count = len(manifest["vacancies"])
    assert identity_count + retired_count == report["source_record_count"]
    assert vacancy_count == retired_count
    assert (
        f"canonical={identity_count} retired={retired_count} vacant={vacancy_count}"
        in capsys.readouterr().out
    )

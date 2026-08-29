from datetime import date, datetime, timezone

import pytest

from nextrip_pipeline.canonical.dataset import read_canonical_active_dataset
from nextrip_pipeline.cli import build_parser
from nextrip_pipeline.quality.opening_status_approval import (
    OpeningStatusApprovalError,
    OpeningStatusReviewApproval,
    OpeningStatusReviewApprovalWriter,
    build_opening_status_review_approvals,
)
from nextrip_pipeline.schemas import (
    DailyOpeningStatus,
    EntityType,
    OpeningStatusObservation,
    VerificationStatus,
)
from tests.canonical_dataset_support import CanonicalTestPlace, write_canonical_dataset


NOW = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)


def _dataset(tmp_path, status=VerificationStatus.PENDING_REVIEW):
    opening = OpeningStatusObservation(
        observation_id="opening-review-1",
        run_id="maps-review-1",
        place_id="cafe_dn_001",
        source_record_ids=["raw-maps-review-1"],
        local_date=date(2026, 8, 24),
        status=DailyOpeningStatus.OPEN_TODAY,
        open_now=True,
        observed_at=NOW,
        verification_status=status,
    )
    path = write_canonical_dataset(
        tmp_path / "canonical",
        [
            CanonicalTestPlace(
                place_id="cafe_dn_001",
                name="Cafe Review",
                latitude=16.06,
                longitude=108.22,
                entity_type=EntityType.CAFE,
                data={"opening_status": opening.model_dump(mode="json")},
            )
        ],
        generated_at=NOW,
    )
    return read_canonical_active_dataset(path)


def test_build_and_write_opening_approval_is_content_addressed(tmp_path) -> None:
    dataset = _dataset(tmp_path)
    approvals = build_opening_status_review_approvals(
        dataset,
        reviewer="oanh",
        approved_at=NOW,
    )

    assert len(approvals) == 1
    assert approvals[0].source_verification_status is VerificationStatus.PENDING_REVIEW
    assert approvals[0].approved_verification_status is VerificationStatus.HUMAN_VERIFIED
    writer = OpeningStatusReviewApprovalWriter(tmp_path / "approvals")
    path = writer.write(approvals[0])
    assert writer.write(approvals[0]) == path
    assert OpeningStatusReviewApproval.model_validate_json(path.read_bytes()) == approvals[0]


def test_explicit_non_pending_place_cannot_be_approved(tmp_path) -> None:
    dataset = _dataset(tmp_path, VerificationStatus.AUTO_VERIFIED)

    with pytest.raises(OpeningStatusApprovalError, match="do not contain pending"):
        build_opening_status_review_approvals(
            dataset,
            reviewer="oanh",
            place_ids=["cafe_dn_001"],
            approved_at=NOW,
        )


def test_cli_exposes_batch_opening_review_approval() -> None:
    args = build_parser().parse_args(
        [
            "approve-opening-status-reviews",
            "--canonical-dataset",
            "canonical.json",
            "--reviewer",
            "oanh",
            "--all-pending",
            "--expected-count",
            "51",
        ]
    )

    assert args.place_id == []
    assert args.all_pending is True
    assert args.expected_count == 51
    assert args.approval_output_dir.as_posix() == "data/approvals/opening_status"

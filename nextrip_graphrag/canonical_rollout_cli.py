from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nextrip_pipeline.canonical.active_pointer import (
    CanonicalActivePointerError,
    promote_canonical_active_dataset,
    resolve_active_canonical_dataset,
)
from nextrip_pipeline.canonical.dataset import (
    CanonicalActiveDataset,
    read_canonical_active_dataset,
)
from nextrip_pipeline.canonical.google_maps_refresh import (
    CanonicalGoogleMapsRefreshPatch,
    apply_google_maps_canonical_refresh_patch,
)
from nextrip_pipeline.canonical.readiness import (
    CanonicalDatasetReadinessReport,
    read_canonical_dataset_readiness,
    require_canonical_dataset_publish_ready,
)

from .config import Settings
from .neo4j_store import Neo4jGraphStore
from .versions.v8.canonical_importer import (
    CanonicalV8ImportPlan,
    CanonicalV8ReleaseManifestWriter,
    apply_canonical_v8_import,
    prepare_canonical_v8_import,
)


class CanonicalRolloutError(RuntimeError):
    """Raised when a release cannot safely move from candidate to active."""


@dataclass(frozen=True)
class ValidatedRolloutInputs:
    base: CanonicalActiveDataset
    candidate: CanonicalActiveDataset
    readiness: CanonicalDatasetReadinessReport
    patch: CanonicalGoogleMapsRefreshPatch
    active_dataset_id: str
    active_dataset_hash: str


_ACTIVE_GRAPH_RELEASE_QUERY = """
MATCH (release:DatasetRelease {kb_version: 'v8', status: 'active'})
RETURN release.id AS release_id,
       release.dataset_id AS dataset_id,
       release.dataset_hash AS dataset_hash
ORDER BY release.id
"""


def validate_rollout_inputs(
    *,
    canonical_root: str | Path,
    base_dataset: str | Path,
    candidate_dataset: str | Path,
    readiness: str | Path,
    patch: str | Path,
) -> ValidatedRolloutInputs:
    """Prove that one candidate is exactly the patch of the selected parent."""

    resolved = resolve_active_canonical_dataset(canonical_root)
    base = read_canonical_active_dataset(base_dataset)
    candidate = read_canonical_active_dataset(candidate_dataset)
    readiness_report = read_canonical_dataset_readiness(readiness)
    require_canonical_dataset_publish_ready(readiness_report)
    refresh_patch = CanonicalGoogleMapsRefreshPatch.model_validate_json(
        Path(patch).read_bytes()
    )

    if (refresh_patch.base_dataset_id, refresh_patch.base_dataset_hash) != (
        base.dataset_id,
        base.dataset_hash,
    ):
        raise CanonicalRolloutError(
            "Google Maps patch does not belong to the supplied base dataset"
        )
    expected_candidate = apply_google_maps_canonical_refresh_patch(
        base,
        refresh_patch,
    )
    if expected_candidate != candidate:
        raise CanonicalRolloutError(
            "candidate dataset is not the exact immutable result of its patch"
        )
    if (
        readiness_report.dataset_id,
        readiness_report.dataset_hash,
        readiness_report.manifest_id,
        readiness_report.manifest_hash,
    ) != (
        candidate.dataset_id,
        candidate.dataset_hash,
        candidate.manifest_id,
        candidate.manifest_hash,
    ):
        raise CanonicalRolloutError(
            "candidate dataset and readiness report identify different snapshots"
        )

    active_identity = (
        resolved.dataset.dataset_id,
        resolved.dataset.dataset_hash,
    )
    allowed_identities = {
        (base.dataset_id, base.dataset_hash),
        (candidate.dataset_id, candidate.dataset_hash),
    }
    if active_identity not in allowed_identities:
        raise CanonicalRolloutError(
            "active canonical pointer changed to an unrelated release: "
            f"{resolved.dataset.dataset_id}"
        )
    return ValidatedRolloutInputs(
        base=base,
        candidate=candidate,
        readiness=readiness_report,
        patch=refresh_patch,
        active_dataset_id=resolved.dataset.dataset_id,
        active_dataset_hash=resolved.dataset.dataset_hash,
    )


def _require_graph_release(
    store: Neo4jGraphStore,
    *,
    allowed: set[tuple[str, str]],
) -> dict[str, Any]:
    rows = store.run(_ACTIVE_GRAPH_RELEASE_QUERY)
    if len(rows) != 1:
        raise CanonicalRolloutError(
            "expected exactly one active V8 graph release; "
            f"found {len(rows)}"
        )
    row = rows[0]
    identity = (str(row.get("dataset_id")), str(row.get("dataset_hash")))
    if identity not in allowed:
        raise CanonicalRolloutError(
            "active V8 graph release is unrelated to this rollout: "
            f"{row.get('dataset_id')}"
        )
    return row


def _prepare_plan(
    *,
    candidate_dataset: str | Path,
    readiness: str | Path,
    completeness_audit: str | Path,
) -> CanonicalV8ImportPlan:
    return prepare_canonical_v8_import(
        candidate_dataset,
        readiness,
        completeness_audit,
    )


def rollout_canonical_release(args: argparse.Namespace) -> dict[str, Any]:
    inputs = validate_rollout_inputs(
        canonical_root=args.canonical_root,
        base_dataset=args.base_dataset,
        candidate_dataset=args.candidate_dataset,
        readiness=args.readiness,
        patch=args.patch,
    )
    if args.verify_only:
        return {
            "status": "candidate_verified",
            "base_dataset_id": inputs.base.dataset_id,
            "candidate_dataset_id": inputs.candidate.dataset_id,
            "patch_id": inputs.patch.patch_id,
            "active_dataset_id": inputs.active_dataset_id,
        }
    if not args.completeness_audit:
        raise CanonicalRolloutError(
            "--completeness-audit is required unless --verify-only is used"
        )
    plan = _prepare_plan(
        candidate_dataset=args.candidate_dataset,
        readiness=args.readiness,
        completeness_audit=args.completeness_audit,
    )
    if (
        plan.release.dataset_id,
        plan.release.dataset_hash,
    ) != (
        inputs.candidate.dataset_id,
        inputs.candidate.dataset_hash,
    ):
        raise CanonicalRolloutError("V8 release plan belongs to another candidate")
    release_path = CanonicalV8ReleaseManifestWriter(args.output_root).write(
        plan.release
    )
    if not args.apply:
        return {
            "status": "validated",
            "base_dataset_id": inputs.base.dataset_id,
            "candidate_dataset_id": inputs.candidate.dataset_id,
            "patch_id": inputs.patch.patch_id,
            "release_manifest": str(release_path),
            "result": plan.dry_run_result().model_dump(mode="json"),
        }

    store = Neo4jGraphStore(Settings.from_neo4j_env("v8"))
    try:
        allowed_before = {
            (inputs.base.dataset_id, inputs.base.dataset_hash),
            (inputs.candidate.dataset_id, inputs.candidate.dataset_hash),
        }
        graph_before = _require_graph_release(store, allowed=allowed_before)
        result = apply_canonical_v8_import(
            store,
            plan,
            batch_size=args.batch_size,
        )
        graph_after = _require_graph_release(
            store,
            allowed={(inputs.candidate.dataset_id, inputs.candidate.dataset_hash)},
        )
    finally:
        store.close()

    pointer = promote_canonical_active_dataset(
        args.canonical_root,
        args.candidate_dataset,
        args.readiness,
        expected_active_dataset_id=inputs.base.dataset_id,
        expected_active_dataset_hash=inputs.base.dataset_hash,
    )
    if (pointer.dataset_id, pointer.dataset_hash) != (
        inputs.candidate.dataset_id,
        inputs.candidate.dataset_hash,
    ):
        raise CanonicalRolloutError("active pointer did not select the candidate")
    return {
        "status": "active",
        "base_dataset_id": inputs.base.dataset_id,
        "candidate_dataset_id": inputs.candidate.dataset_id,
        "patch_id": inputs.patch.patch_id,
        "release_manifest": str(release_path),
        "graph_before": graph_before,
        "graph_after": graph_after,
        "promotion_id": pointer.promotion_id,
        "pointer_hash": pointer.pointer_hash,
        "result": result.model_dump(mode="json"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and atomically roll one canonical candidate into V8."
    )
    parser.add_argument("--canonical-root", default="data/canonical")
    parser.add_argument("--base-dataset", required=True)
    parser.add_argument("--candidate-dataset", required=True)
    parser.add_argument("--readiness", required=True)
    parser.add_argument("--patch", required=True)
    parser.add_argument("--completeness-audit")
    parser.add_argument("--output-root", default="data/neo4j/v8/releases")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        result = rollout_canonical_release(args)
    except (
        OSError,
        TypeError,
        ValueError,
        CanonicalActivePointerError,
        CanonicalRolloutError,
    ) as error:
        raise SystemExit(f"Canonical rollout rejected: {error}") from error
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

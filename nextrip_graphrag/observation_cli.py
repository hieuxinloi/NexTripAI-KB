from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .config import Settings
from .neo4j_store import Neo4jGraphStore
from .versions.v8.observation_publisher import (
    V8ObservationPublisher,
    build_v8_observation_plan,
    write_v8_observation_plan,
)


class _ClosableStore(Protocol):
    def run(self, query: str, **params: object) -> list[dict[str, object]]: ...

    def close(self) -> None: ...


class _DryRunObservationStore:
    def run(self, _query: str, **_params: object) -> list[dict[str, object]]:
        raise AssertionError("dry-run observation publishing touched Neo4j")

    def close(self) -> None:
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan verified dynamic observations and optionally append them "
            "to the active Neo4j V8 release."
        )
    )
    parser.add_argument("--canonical-dataset", required=True)
    parser.add_argument(
        "--hotel-price-root",
        default="data/current/hotel_price",
    )
    parser.add_argument(
        "--hotel-availability-root",
        default="data/current/hotel_availability",
    )
    parser.add_argument("--menu-root", default="data/current/menu")
    parser.add_argument(
        "--opening-approval-root",
        default="data/approvals/opening_status",
    )
    parser.add_argument(
        "--output-root",
        default="data/neo4j/v8/observation_runs",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--apply", action="store_true")
    return parser


def publish_observations(args: argparse.Namespace) -> dict[str, object]:
    plan = build_v8_observation_plan(
        args.canonical_dataset,
        hotel_price_root=args.hotel_price_root,
        hotel_availability_root=args.hotel_availability_root,
        current_menu_root=args.menu_root,
        opening_approval_root=args.opening_approval_root,
    )
    output_root = Path(args.output_root)
    plan_path = write_v8_observation_plan(
        output_root / f"plan={plan.plan_id}" / "v8-observation-plan.json",
        plan,
    )
    run_path = (
        output_root
        / f"run={plan.plan_id}-{'apply' if args.apply else 'dry-run'}-{uuid4().hex}"
        / "v8-observation-publish-manifest.json"
    )
    store: _ClosableStore = (
        Neo4jGraphStore(Settings.from_neo4j_env("v8"))
        if args.apply
        else _DryRunObservationStore()
    )
    try:
        manifest = V8ObservationPublisher(
            store,
            batch_size=args.batch_size,
        ).publish(
            plan,
            dry_run=not args.apply,
            manifest_path=run_path,
        )
    finally:
        store.close()
    return {
        "plan": str(plan_path),
        "publish_manifest": str(run_path),
        "result": manifest.model_dump(mode="json"),
    }


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    print(
        json.dumps(
            publish_observations(args),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

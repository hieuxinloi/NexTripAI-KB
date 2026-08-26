from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

from .config import Settings
from .neo4j_store import Neo4jGraphStore
from .versions.v8.observation_publisher import (
    V8ObservationPublisher,
    build_v8_observation_plan,
    write_v8_observation_plan,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build and append canonical price, availability, opening and menu "
            "observations without importing retrieval-only dependencies."
        )
    )
    parser.add_argument("--canonical-dataset", required=True)
    parser.add_argument("--hotel-price-root", default="data/current/hotel_price")
    parser.add_argument(
        "--hotel-availability-root",
        default="data/current/hotel_availability",
    )
    parser.add_argument("--menu-root", default="data/current/menu")
    parser.add_argument(
        "--opening-approval-root",
        default="data/approvals/opening_status",
    )
    parser.add_argument("--output-root", default="data/neo4j/v8/observation_runs")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
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
    if not args.apply:
        print(
            json.dumps(
                {
                    "status": "planned",
                    "plan": str(plan_path),
                    "plan_id": plan.plan_id,
                    "dataset_id": plan.dataset_id,
                    "counts": plan.counts,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    run_path = (
        output_root
        / f"run={plan.plan_id}-apply-{uuid4().hex}"
        / "v8-observation-publish-manifest.json"
    )
    store = Neo4jGraphStore(Settings.from_neo4j_env("v8"))
    try:
        manifest = V8ObservationPublisher(store, batch_size=args.batch_size).publish(
            plan,
            manifest_path=run_path,
        )
    finally:
        store.close()
    print(
        json.dumps(
            {
                "status": "published",
                "plan": str(plan_path),
                "publish_manifest": str(run_path),
                "result": manifest.model_dump(mode="json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

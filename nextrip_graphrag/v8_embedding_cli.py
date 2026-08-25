from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from nextrip_pipeline.canonical.dataset import read_canonical_active_dataset

from .config import Settings
from .enrichment.embeddings import CachedBatchEmbedder
from .gemini_client import GeminiEmbeddingClient
from .neo4j_store import Neo4jGraphStore
from .versions.v8.embedding import (
    V8EmbeddingError,
    V8EmbeddingRunWriter,
    V8EmbeddingTargetType,
    apply_v8_embedding_plan,
    build_v8_embedding_plan,
)


def _load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def _default_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"v8-embedding-{timestamp}-{uuid4().hex[:12]}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create resumable Gemini embeddings for one active canonical V8 release."
        )
    )
    parser.add_argument("--canonical-dataset", required=True)
    parser.add_argument(
        "--model",
        default=os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001"),
    )
    parser.add_argument(
        "--dimension",
        type=int,
        default=int(os.getenv("NEXTRIP_V8_EMBEDDING_DIMENSION", "1536")),
    )
    parser.add_argument(
        "--cache-root",
        default=os.getenv(
            "NEXTRIP_V8_EMBEDDING_CACHE_ROOT",
            "data/cache/v8_embeddings",
        ),
    )
    parser.add_argument(
        "--output-root",
        default=os.getenv(
            "NEXTRIP_V8_EMBEDDING_RUN_ROOT",
            "data/neo4j/v8/embedding_runs",
        ),
    )
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--request-delay", type=float, default=2.0)
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Call Gemini for missing vectors, write them to the active release, "
            "and mark the semantic index ready after 100%% coverage."
        ),
    )
    return parser


def run(args: argparse.Namespace) -> dict[str, object]:
    if args.batch_size <= 0:
        raise ValueError("--batch-size must be positive")
    if args.request_delay < 0:
        raise ValueError("--request-delay cannot be negative")
    if args.max_retries < 0:
        raise ValueError("--max-retries cannot be negative")
    dataset = read_canonical_active_dataset(args.canonical_dataset)
    settings = Settings.from_neo4j_env("v8")
    store = Neo4jGraphStore(settings)
    try:
        plan = build_v8_embedding_plan(
            store,
            dataset,
            model=args.model,
            dimension=args.dimension,
        )
        family_counts = {
            target_type.value: sum(
                target.target_type is target_type for target in plan.targets
            )
            for target_type in V8EmbeddingTargetType
        }
        pending_count = sum(not target.embedding_current for target in plan.targets)
        if not args.apply:
            return {
                "status": "planned",
                "release_id": plan.release_id,
                "dataset_id": plan.dataset_id,
                "model": plan.model,
                "dimension": plan.dimension,
                "family_counts": family_counts,
                "target_count": len(plan.targets),
                "pending_count": pending_count,
            }

        api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        embedding_settings = replace(
            settings,
            google_api_key=api_key,
            embedding_model=plan.model,
            embedding_dim=plan.dimension,
        )
        embedder = CachedBatchEmbedder(
            GeminiEmbeddingClient(embedding_settings),
            args.cache_root,
            model=plan.model,
            dimensions=plan.dimension,
            delay=args.request_delay,
            max_retries=args.max_retries,
        )
        try:
            result = apply_v8_embedding_plan(
                store,
                plan,
                embedder,
                run_id=args.run_id or _default_run_id(),
                batch_size=args.batch_size,
            )
        finally:
            embedder.close()
        summary_path = V8EmbeddingRunWriter(args.output_root).write(result)
        return {
            "status": result.semantic_index_status,
            "release_id": result.release_id,
            "dataset_id": result.dataset_id,
            "summary": str(summary_path),
            "result": result.model_dump(mode="json"),
        }
    finally:
        store.close()


def main(argv: list[str] | None = None) -> None:
    _load_dotenv_if_available()
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except (OSError, TypeError, ValueError, V8EmbeddingError) as error:
        raise SystemExit(f"V8 embedding rejected: {error}") from error
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if summary := result.get("summary"):
        print(f"summary={summary}")


if __name__ == "__main__":
    main()

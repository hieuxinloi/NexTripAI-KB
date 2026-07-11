from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Settings
from .gemini_client import GeminiClient
from .neo4j_store import Neo4jGraphStore
from .normalizer import normalize_dataset, read_processed, write_processed
from .rag import TravelGraphRAG


def load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def cmd_prepare(args: argparse.Namespace) -> None:
    bundle = normalize_dataset(args.data_dir)
    write_processed(bundle, args.out_dir)
    print(json.dumps(bundle["manifest"], ensure_ascii=False, indent=2))
    print(f"Processed data written to {Path(args.out_dir).resolve()}")


def cmd_schema(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    store = Neo4jGraphStore(settings)
    try:
        store.ensure_schema(settings.embedding_dim)
    finally:
        store.close()
    print("Neo4j constraints, fulltext index, and vector index are ready.")


def cmd_load(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    bundle = read_processed(args.processed_dir)
    embedder = GeminiClient(settings) if args.with_embeddings else None
    store = Neo4jGraphStore(settings)
    try:
        store.ensure_schema(settings.embedding_dim)
        store.load_cities(bundle["cities"])
        store.load_places(
            bundle["places"],
            embedder=embedder,
            batch_size=args.batch_size,
        )
    finally:
        store.close()

    embedding_note = "with Gemini embeddings" if args.with_embeddings else "without embeddings"
    print(
        f"Loaded {len(bundle['cities'])} cities and {len(bundle['places'])} places into Neo4j {embedding_note}."
    )


def cmd_ask(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    store = Neo4jGraphStore(settings)
    gemini = GeminiClient(settings)
    rag = TravelGraphRAG(store, gemini)
    try:
        answer = rag.answer(
            question=args.question,
            city=args.city,
            entity_types=args.type or None,
            top_k=args.top_k or settings.top_k,
        )
    finally:
        store.close()
    print(answer)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nextrip-graphrag",
        description="Build and query a Neo4j GraphRAG system for NexTrip travel data.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="Normalize verified travel data into processed files.")
    prepare.add_argument("--data-dir", default="travel_data_verified")
    prepare.add_argument("--out-dir", default="processed_verified")
    prepare.set_defaults(func=cmd_prepare)

    schema = subparsers.add_parser("schema", help="Create Neo4j constraints and indexes.")
    schema.set_defaults(func=cmd_schema)

    load = subparsers.add_parser("load", help="Load processed data into Neo4j.")
    load.add_argument("--processed-dir", default="processed_verified")
    load.add_argument("--with-embeddings", action="store_true", help="Generate Gemini embeddings while loading.")
    load.add_argument("--batch-size", type=int, default=16)
    load.set_defaults(func=cmd_load)

    ask = subparsers.add_parser("ask", help="Ask the GraphRAG chatbot.")
    ask.add_argument("question")
    ask.add_argument("--city", choices=["Quy Nhơn", "Đà Nẵng", "Quy Nhon", "Da Nang"], default=None)
    ask.add_argument(
        "--type",
        action="append",
        choices=["attraction", "cafe", "hotel", "nightlife", "restaurant"],
        help="Filter by place type. Can be passed multiple times.",
    )
    ask.add_argument("--top-k", type=int, default=None)
    ask.set_defaults(func=cmd_ask)

    return parser


def main() -> None:
    configure_console_encoding()
    load_dotenv_if_available()
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

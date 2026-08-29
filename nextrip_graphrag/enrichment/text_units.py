from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .io import read_jsonl, stable_id, utc_now, write_json, write_jsonl
from .nominatim import normalize_text


DEFAULT_CHUNK_CHARS = 1400
DEFAULT_OVERLAP_CHARS = 180


def chunk_text(
    text: str,
    *,
    max_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[dict[str, Any]]:
    if max_chars < 200:
        raise ValueError("max_chars must be at least 200")
    if overlap_chars < 0 or overlap_chars >= max_chars // 2:
        raise ValueError("overlap_chars must be non-negative and less than half max_chars")

    normalized = re.sub(r"[ \t]+", " ", text)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    chunks: list[dict[str, Any]] = []
    start = 0
    while start < len(normalized):
        hard_end = min(start + max_chars, len(normalized))
        end = hard_end
        if hard_end < len(normalized):
            minimum_break = start + int(max_chars * 0.65)
            candidates = [
                normalized.rfind("\n", minimum_break, hard_end),
                normalized.rfind(". ", minimum_break, hard_end),
                normalized.rfind("! ", minimum_break, hard_end),
                normalized.rfind("? ", minimum_break, hard_end),
            ]
            best_break = max(candidates)
            if best_break >= minimum_break:
                end = best_break + 1

        chunk = normalized[start:end].strip()
        if chunk:
            chunks.append({"text": chunk, "char_start": start, "char_end": end})
        if end >= len(normalized):
            break
        next_start = max(end - overlap_chars, start + 1)
        whitespace = normalized.find(" ", next_start, min(end, next_start + 80))
        start = whitespace + 1 if whitespace >= 0 else next_start
    return chunks


def find_mentions(text: str, places: list[dict[str, str]]) -> list[dict[str, Any]]:
    normalized_chunk = normalize_text(text)
    mentions: list[dict[str, Any]] = []
    for place in places:
        normalized_name = normalize_text(place["place_name"]).strip()
        if len(normalized_name) < 4:
            continue
        pattern = rf"(?<!\w){re.escape(normalized_name)}(?!\w)"
        if re.search(pattern, normalized_chunk):
            mentions.append(
                {
                    "place_id": place["place_id"],
                    "place_name": place["place_name"],
                    "match_type": "normalized_exact_name",
                    "confidence": 1.0,
                }
            )
    return mentions


def build_article_text_units(
    workspace: str | Path,
    *,
    max_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> dict[str, Any]:
    workspace_path = Path(workspace)
    output_dir = workspace_path / "output"
    source_path = output_dir / "source_documents.jsonl"
    source_documents = read_jsonl(source_path)
    usable_documents = [document for document in source_documents if not document.get("error")]

    documents: list[dict[str, Any]] = []
    text_units: list[dict[str, Any]] = []
    for source in source_documents:
        documents.append(
            {
                "document_id": source["document_id"],
                "url": source["url"],
                "final_url": source.get("final_url"),
                "title": source.get("title"),
                "source_name": source.get("source_name"),
                "domain": source.get("domain"),
                "content_hash": source.get("content_hash"),
                "fetched_at": source.get("fetched_at"),
                "crawl_status": "usable" if not source.get("error") else "unusable",
                "crawl_error": source.get("error"),
                "managed_by": "nextrip_enrichment",
                "place_ids": [place["place_id"] for place in source.get("places", [])],
            }
        )
        if source.get("error"):
            continue
        for sequence, chunk in enumerate(
            chunk_text(source["text"], max_chars=max_chars, overlap_chars=overlap_chars)
        ):
            text_unit_id = stable_id(
                "article_text",
                f"{source['document_id']}|{sequence}|{source.get('content_hash')}",
            )
            text_units.append(
                {
                    "text_unit_id": text_unit_id,
                    "document_id": source["document_id"],
                    "sequence": sequence,
                    "text": chunk["text"],
                    "char_start": chunk["char_start"],
                    "char_end": chunk["char_end"],
                    "title": source.get("title"),
                    "source_url": source["url"],
                    "content_hash": source.get("content_hash"),
                    "evidence_origin": "crawled_article",
                    "mentions": find_mentions(chunk["text"], source.get("places", [])),
                }
            )

    verified_units = read_jsonl(output_dir / "text_units.jsonl")
    for unit in verified_units:
        text_units.append(
            {
                "text_unit_id": unit["text_unit_id"],
                "document_id": unit["document_id"],
                "sequence": -1,
                "text": unit["text"],
                "char_start": 0,
                "char_end": len(unit["text"]),
                "title": unit["place_name"],
                "source_url": unit.get("source_url"),
                "content_hash": None,
                "evidence_origin": "verified_record",
                "mentions": [
                    {
                        "place_id": unit["place_id"],
                        "place_name": unit["place_name"],
                        "match_type": "verified_record_owner",
                        "confidence": 0.7,
                    }
                ],
            }
        )

    documents_path = output_dir / "graph_documents.jsonl"
    text_units_path = output_dir / "article_text_units.jsonl"
    write_jsonl(documents_path, documents)
    write_jsonl(text_units_path, text_units)
    mentioned_place_ids = {
        mention["place_id"] for unit in text_units for mention in unit.get("mentions", [])
    }
    report = {
        "generated_at": utc_now(),
        "source_document_count": len(source_documents),
        "usable_document_count": len(usable_documents),
        "graph_document_count": len(documents),
        "article_text_unit_count": len(text_units) - len(verified_units),
        "verified_record_text_unit_count": len(verified_units),
        "total_text_unit_count": len(text_units),
        "mention_edge_count": sum(len(unit["mentions"]) for unit in text_units),
        "mentioned_place_count": len(mentioned_place_ids),
        "documents_without_mentions": sum(
            1
            for document in documents
            if not any(unit["document_id"] == document["document_id"] and unit["mentions"] for unit in text_units)
        ),
        "max_chars": max_chars,
        "overlap_chars": overlap_chars,
        "documents_path": str(documents_path.resolve()),
        "text_units_path": str(text_units_path.resolve()),
    }
    write_json(output_dir / "article_text_units_report.json", report)
    return report

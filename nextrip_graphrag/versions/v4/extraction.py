from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from .ontology import (
    ONTOLOGY_RELATIONS,
    concepts_from_claims,
    deterministic_description_claims,
    structured_claims,
)
from .schemas import DescriptionExtraction, ExtractedClaim


SYSTEM_INSTRUCTION = f"""
Extract travel knowledge from one Vietnamese place description.
Return only claims grounded in an exact evidence_text substring.
Allowed predicates: {sorted(ONTOLOGY_RELATIONS)}.
Use negative polarity for explicit negation. Do not infer missing amenities,
quality, safety, audience suitability, weather suitability, or distance.
Do not create Cypher. Keep canonical concepts short and reusable.
""".strip()


class DescriptionExtractor:
    def __init__(self, gemini: Any | None = None, cache_dir: str | Path | None = None):
        self.gemini = gemini
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def extract(self, place: dict[str, Any]) -> DescriptionExtraction:
        description = str(place["props"].get("description") or "").strip()
        claims = structured_claims(place) + deterministic_description_claims(description)
        if self.gemini is not None and description:
            claims.extend(self._extract_with_gemini(place["id"], description).claims)
        claims = _validated_claims(description, claims)
        return DescriptionExtraction(concepts=concepts_from_claims(claims), claims=claims)

    def _extract_with_gemini(self, place_id: str, description: str) -> DescriptionExtraction:
        cache_path = self._cache_path(place_id, description)
        if cache_path and cache_path.exists():
            return DescriptionExtraction.model_validate_json(cache_path.read_text(encoding="utf-8"))
        result = self.gemini.generate_structured(
            SYSTEM_INSTRUCTION,
            f"Place ID: {place_id}\nDescription:\n{description}",
            DescriptionExtraction,
        )
        if cache_path:
            cache_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return result

    def _cache_path(self, place_id: str, description: str) -> Path | None:
        if not self.cache_dir:
            return None
        digest = sha256(description.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"{place_id}-{digest}.json"


def _validated_claims(description: str, claims: list[ExtractedClaim]) -> list[ExtractedClaim]:
    valid: dict[tuple[str, str, str, str], ExtractedClaim] = {}
    for claim in claims:
        if claim.predicate not in ONTOLOGY_RELATIONS:
            continue
        if claim.extraction_method != "structured_field" and claim.evidence_text not in description:
            continue
        key = (
            claim.predicate,
            claim.object_type.value,
            claim.object_name.casefold(),
            claim.polarity.value,
        )
        current = valid.get(key)
        if current is None or claim.confidence > current.confidence:
            valid[key] = claim
    return list(valid.values())

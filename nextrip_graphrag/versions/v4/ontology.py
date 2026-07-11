from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ...normalizer import slugify
from .policy import POLICY
from .schemas import ClaimPolarity, ConceptType, ExtractedClaim, ExtractedConcept


@dataclass(frozen=True)
class OntologyRelation:
    predicate: str
    concept_type: ConceptType
    relationship: str
    domain: str


ONTOLOGY_RELATIONS = {
    "LOCATED_NEAR": OntologyRelation("LOCATED_NEAR", ConceptType.LANDMARK, "LOCATED_NEAR", "geographic"),
    "HAS_FOOD_OFFERING": OntologyRelation("HAS_FOOD_OFFERING", ConceptType.FOOD_OFFERING, "HAS_OFFERING", "offering"),
    "HAS_DRINK_OFFERING": OntologyRelation("HAS_DRINK_OFFERING", ConceptType.DRINK_OFFERING, "HAS_OFFERING", "offering"),
    "SERVES_CUISINE": OntologyRelation("SERVES_CUISINE", ConceptType.CUISINE, "SERVES_CUISINE", "offering"),
    "SERVES_DISH": OntologyRelation("SERVES_DISH", ConceptType.DISH, "SERVES_DISH", "offering"),
    "SUPPORTS_DIET": OntologyRelation("SUPPORTS_DIET", ConceptType.DIETARY_OPTION, "SUPPORTS_DIET", "offering"),
    "PROVIDES_ACTIVITY": OntologyRelation("PROVIDES_ACTIVITY", ConceptType.ACTIVITY, "PROVIDES_ACTIVITY", "experience"),
    "OFFERS_EXPERIENCE": OntologyRelation("OFFERS_EXPERIENCE", ConceptType.EXPERIENCE, "OFFERS_EXPERIENCE", "experience"),
    "HAS_SCENERY": OntologyRelation("HAS_SCENERY", ConceptType.SCENERY, "HAS_SCENERY", "experience"),
    "HAS_AMBIENCE": OntologyRelation("HAS_AMBIENCE", ConceptType.AMBIENCE, "HAS_AMBIENCE", "experience"),
    "HAS_AMENITY": OntologyRelation("HAS_AMENITY", ConceptType.AMENITY, "HAS_AMENITY", "compatibility"),
    "HAS_QUALITY": OntologyRelation("HAS_QUALITY", ConceptType.QUALITY_CRITERION, "HAS_QUALITY", "offering"),
    "SUITABLE_FOR": OntologyRelation("SUITABLE_FOR", ConceptType.AUDIENCE, "SUITABLE_FOR", "compatibility"),
    "HAS_ACCESSIBILITY": OntologyRelation("HAS_ACCESSIBILITY", ConceptType.ACCESSIBILITY_FEATURE, "HAS_ACCESSIBILITY", "compatibility"),
    "HAS_RISK": OntologyRelation("HAS_RISK", ConceptType.RISK, "HAS_RISK", "compatibility"),
    "SUITABLE_DURING": OntologyRelation("SUITABLE_DURING", ConceptType.WEATHER_CONDITION, "SUITABLE_DURING", "temporal"),
    "BEST_DURING": OntologyRelation("BEST_DURING", ConceptType.TIME_WINDOW, "BEST_DURING", "temporal"),
    "BEST_IN_SEASON": OntologyRelation("BEST_IN_SEASON", ConceptType.SEASON, "BEST_IN_SEASON", "temporal"),
}


STRUCTURED_FIELDS: dict[str, tuple[str, ConceptType]] = {
    "amenities": ("HAS_AMENITY", ConceptType.AMENITY),
    "cuisine": ("SERVES_CUISINE", ConceptType.CUISINE),
    "signature_dishes": ("SERVES_DISH", ConceptType.DISH),
    "dietary_options": ("SUPPORTS_DIET", ConceptType.DIETARY_OPTION),
    "suitable_for": ("SUITABLE_FOR", ConceptType.AUDIENCE),
    "ambience": ("HAS_AMBIENCE", ConceptType.AMBIENCE),
    "weather_suitable": ("SUITABLE_DURING", ConceptType.WEATHER_CONDITION),
    "best_time": ("BEST_DURING", ConceptType.TIME_WINDOW),
    "drink_specialties": ("HAS_DRINK_OFFERING", ConceptType.DRINK_OFFERING),
}


CUISINE_EVIDENCE_TERMS = {
    "seafood": ("seafood", "hai san"),
    "vegetarian": ("vegetarian", "vegan", "chay"),
    "vietnamese": ("vietnamese", "mon viet", "am thuc viet"),
    "korean": ("korean", "han quoc"),
    "japanese": ("japanese", "nhat ban", "sushi"),
    "chinese": ("chinese", "trung hoa"),
    "western": ("western", "mon au"),
    "bbq": ("bbq", "nuong"),
    "asian": ("asian", "a au", "chau a"),
}


# Conservative patterns provide an offline baseline. Gemini can add claims that are
# not captured here, but it must use the same predicate and concept whitelist.
DESCRIPTION_PATTERNS: tuple[tuple[str, ConceptType, str, tuple[str, ...]], ...] = (
    ("PROVIDES_ACTIVITY", ConceptType.ACTIVITY, "check-in", ("check in", "check-in", "song ao")),
    ("PROVIDES_ACTIVITY", ConceptType.ACTIVITY, "tam bien", ("tam bien",)),
    ("PROVIDES_ACTIVITY", ConceptType.ACTIVITY, "lan ngam san ho", ("lan ngam san ho", "ngam san ho")),
    ("PROVIDES_ACTIVITY", ConceptType.ACTIVITY, "ngam canh", ("ngam canh", "ngam nhin")),
    ("HAS_SCENERY", ConceptType.SCENERY, "bien", ("bien ca", "view bien", "huong bien")),
    ("HAS_SCENERY", ConceptType.SCENERY, "nui", ("nui rung", "nui da")),
    ("HAS_AMBIENCE", ConceptType.AMBIENCE, "yen tinh", ("yen tinh", "nhe nhang")),
    ("HAS_AMBIENCE", ConceptType.AMBIENCE, "soi dong", ("soi dong", "cuong nhiet")),
    ("HAS_DRINK_OFFERING", ConceptType.DRINK_OFFERING, "cocktail", ("cocktail",)),
    ("HAS_DRINK_OFFERING", ConceptType.DRINK_OFFERING, "bia", ("bia lanh", "bia tuoi")),
    ("HAS_QUALITY", ConceptType.QUALITY_CRITERION, "tuoi", ("tuoi ngon", "hai san tuoi", "do tuoi")),
    ("HAS_AMENITY", ConceptType.AMENITY, "ho boi", ("ho boi", "be boi")),
    ("SUITABLE_FOR", ConceptType.AUDIENCE, "gia dinh", ("gia dinh",)),
    ("SUITABLE_FOR", ConceptType.AUDIENCE, "cap doi", ("cap doi",)),
    ("SUITABLE_FOR", ConceptType.AUDIENCE, "nhom ban", ("nhom ban",)),
)


NEGATION_MARKERS = ("khong co", "khong lap dat", "khong phu hop", "khong danh cho")

CONCEPT_ALIASES = {
    "be boi": "pool",
    "ho boi": "pool",
    "hai san": "seafood",
    "gia dinh": "families",
    "cap doi": "couples",
    "nhom ban": "friends",
    "tre em": "children",
    "nguoi cao tuoi": "seniors",
    "yen tinh": "quiet",
    "soi dong": "lively",
}


def canonical_concept(name: str) -> str:
    normalized = slugify(name).replace("-", " ")
    return CONCEPT_ALIASES.get(normalized, normalized)


def concept_id(concept_type: ConceptType | str, name: str) -> str:
    label = concept_type.value if isinstance(concept_type, ConceptType) else concept_type
    return f"concept:{slugify(label)}:{slugify(name)}"


def split_description(text: str) -> list[dict[str, Any]]:
    normalized = " ".join((text or "").split())
    if not normalized:
        return []
    sentences = re.split(r"(?<=[.!?])\s+", normalized)
    units: list[dict[str, Any]] = []
    cursor = 0
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        start = normalized.find(sentence, cursor)
        end = start + len(sentence)
        units.append({"text": sentence, "char_start": start, "char_end": end})
        cursor = end
    return units


def structured_claims(place: dict[str, Any]) -> list[ExtractedClaim]:
    props = place["props"]
    claims: list[ExtractedClaim] = []
    for field, (predicate, object_type) in STRUCTURED_FIELDS.items():
        raw = place.get("terms", {}).get(field) or props.get(field)
        values = raw if isinstance(raw, list) else [raw] if raw not in (None, "") else []
        for value in values:
            rendered = str(value).strip()
            if rendered and _structured_value_supported(place, field, rendered):
                claims.append(
                    ExtractedClaim(
                        predicate=predicate,
                        object_type=object_type,
                        object_name=rendered,
                        confidence=POLICY.structured_claim_confidence,
                        evidence_text=f"structured:{field}={rendered}",
                        extraction_method="structured_field",
                        subject_scope="offering"
                        if field in {"cuisine", "signature_dishes", "dietary_options", "drink_specialties"}
                        else "place",
                    )
                )
    return claims


def _structured_value_supported(place: dict[str, Any], field: str, value: str) -> bool:
    if field not in {"cuisine", "signature_dishes"}:
        return True
    props = place["props"]
    evidence = canonical_concept(
        " ".join(
            str(part)
            for part in (props.get("name"), props.get("description"), props.get("category"))
            if part
        )
    )
    claim = canonical_concept(value)
    if field == "cuisine":
        return any(term in evidence for term in CUISINE_EVIDENCE_TERMS.get(claim, (claim,)))
    return claim in evidence


def deterministic_description_claims(text: str) -> list[ExtractedClaim]:
    claims: list[ExtractedClaim] = []
    for unit in split_description(text):
        plain = slugify(unit["text"]).replace("-", " ")
        for predicate, object_type, object_name, terms in DESCRIPTION_PATTERNS:
            matched = next((term for term in terms if term in plain), None)
            if matched is None:
                continue
            position = plain.find(matched)
            prefix = plain[max(0, position - 32) : position]
            polarity = (
                ClaimPolarity.NEGATIVE
                if any(marker in prefix for marker in NEGATION_MARKERS)
                else ClaimPolarity.POSITIVE
            )
            claims.append(
                ExtractedClaim(
                    predicate=predicate,
                    object_type=object_type,
                    object_name=object_name,
                    polarity=polarity,
                    confidence=POLICY.deterministic_claim_confidence,
                    evidence_text=unit["text"],
                    evidence_start=unit["char_start"],
                    evidence_end=unit["char_end"],
                    extraction_method="deterministic_description_v4",
                    subject_scope="offering"
                    if predicate in {
                        "HAS_FOOD_OFFERING",
                        "HAS_DRINK_OFFERING",
                        "SERVES_CUISINE",
                        "SERVES_DISH",
                        "SUPPORTS_DIET",
                        "HAS_QUALITY",
                    }
                    else "place",
                )
            )
    return _deduplicate_claims(claims)


def concepts_from_claims(claims: list[ExtractedClaim]) -> list[ExtractedConcept]:
    concepts: dict[tuple[ConceptType, str], ExtractedConcept] = {}
    for claim in claims:
        canonical = canonical_concept(claim.object_name)
        concepts[(claim.object_type, canonical)] = ExtractedConcept(
            concept_type=claim.object_type,
            name=claim.object_name,
            canonical_name=canonical,
        )
    return list(concepts.values())


def _deduplicate_claims(claims: list[ExtractedClaim]) -> list[ExtractedClaim]:
    unique: dict[tuple[str, str, ClaimPolarity, str], ExtractedClaim] = {}
    for claim in claims:
        key = (
            claim.predicate,
            canonical_concept(claim.object_name),
            claim.polarity,
            claim.evidence_text,
            claim.subject_scope,
        )
        unique[key] = claim
    return list(unique.values())

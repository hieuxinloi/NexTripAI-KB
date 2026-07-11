from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from ...neo4j_store import chunks
from ...normalizer import slugify
from ..v3.graph_store import FACET_SPECS, V3GraphStore
from .extraction import DescriptionExtractor
from .ontology import ONTOLOGY_RELATIONS, canonical_concept, concept_id, split_description
from .policy import POLICY
from .schemas import ConceptType, DynamicObservationInput, ExtractedClaim


class V4GraphStore(V3GraphStore):
    kb_version = "v4"

    def __init__(self, settings: Any, description_extractor: DescriptionExtractor | None = None):
        super().__init__(settings)
        self.description_extractor = description_extractor or DescriptionExtractor()

    def ensure_v4_schema(self, embedding_dim: int) -> None:
        self.ensure_typed_schema(embedding_dim, index_prefix="v4")
        for label in {spec[0] for spec in FACET_SPECS.values()} | {"VenueType", "HotelStyle"}:
            self.run(
                f"CREATE CONSTRAINT v4_{label.lower()}_id IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE"
            )
        statements = [
            "CREATE CONSTRAINT v4_concept_id IF NOT EXISTS FOR (n:Concept) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT v4_offering_id IF NOT EXISTS FOR (n:Offering) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT v4_claim_id IF NOT EXISTS FOR (n:Claim) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT v4_conflict_id IF NOT EXISTS FOR (n:DataConflict) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT v4_community_id IF NOT EXISTS FOR (n:Community) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT v4_report_id IF NOT EXISTS FOR (n:CommunityReport) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT v4_observation_id IF NOT EXISTS FOR (n:DynamicObservation) REQUIRE n.id IS UNIQUE",
            "CREATE FULLTEXT INDEX v4_concept_fulltext IF NOT EXISTS FOR (n:Concept) ON EACH [n.name, n.canonical_name]",
        ]
        for statement in statements:
            self.run(statement)

    def after_places_loaded(self, places: list[dict[str, Any]]) -> None:
        # V4 replaces V3 facet nodes with one canonical Concept layer. It keeps
        # only the verified geographic expansion from the V3 loader.
        self._load_geo_near_edges()
        self._load_offerings(places)
        self._load_description_graph(places)
        self._promote_supported_relationships()
        self._build_conflicts()
        self._build_taxonomy_communities()
        self.run_versioned(
            """
            MATCH (catalog:TravelCatalog {kb_version: $kb_version})
            SET catalog.source_place_count = $place_count
            """,
            place_count=len(places),
        )

    def _load_offerings(self, places: list[dict[str, Any]]) -> None:
        rows = []
        for place in places:
            offering_type = _offering_type(place["entity_type"])
            if offering_type is None:
                continue
            rows.append(
                {
                    "place_id": place["id"],
                    "offering_id": f"offering:{place['id']}:{slugify(offering_type)}",
                    "offering_type": offering_type,
                    "name": f"{place['props']['name']} offering",
                }
            )
        for offering_type in sorted({row["offering_type"] for row in rows}):
            type_rows = [row for row in rows if row["offering_type"] == offering_type]
            self.run_versioned(
                f"""
                UNWIND $rows AS row
                MATCH (place:Place {{id: row.place_id, kb_version: $kb_version}})
                MERGE (offering:Offering:{offering_type} {{id: row.offering_id}})
                SET offering.name = row.name,
                    offering.offering_type = row.offering_type,
                    offering.kb_version = $kb_version
                MERGE (place)-[:HAS_OFFERING]->(offering)
                """,
                rows=type_rows,
            )

    def _load_description_graph(self, places: list[dict[str, Any]]) -> None:
        sentence_rows: list[dict[str, Any]] = []
        concept_rows: dict[str, dict[str, Any]] = {}
        claim_rows: list[dict[str, Any]] = []
        for place in places:
            description = str(place["props"].get("description") or "")
            units = split_description(description)
            for sequence, unit in enumerate(units):
                unit["id"] = f"text-unit:description:{place['id']}:{sequence}"
                unit.update({"place_id": place["id"], "sequence": sequence})
                sentence_rows.append(unit)

            extraction = self.description_extractor.extract(place)
            for concept in extraction.concepts:
                identifier = concept_id(concept.concept_type, concept.canonical_name)
                concept_rows[identifier] = {
                    "id": identifier,
                    "name": concept.name,
                    "canonical_name": concept.canonical_name,
                    "concept_type": concept.concept_type.value,
                    "domain": _concept_domain(concept.concept_type),
                }
            for claim in extraction.claims:
                claim_rows.append(_claim_row(place, claim, units))

        for batch in chunks(sentence_rows, POLICY.sentence_batch_size):
            self.run_versioned(
                """
                UNWIND $rows AS row
                MATCH (owner:TextUnit {id: 'text-unit:verified:' + row.place_id, kb_version: $kb_version})
                      -[:PART_OF]->(document:Document)
                MATCH (place:Place {id: row.place_id, kb_version: $kb_version})
                MERGE (unit:TextUnit {id: row.id})
                SET unit.text = row.text,
                    unit.sequence = row.sequence,
                    unit.char_start = row.char_start,
                    unit.char_end = row.char_end,
                    unit.evidence_origin = 'verified_description',
                    unit.kb_version = $kb_version
                MERGE (unit)-[:PART_OF]->(document)
                MERGE (unit)-[:MENTIONS]->(place)
                """,
                rows=batch,
            )

        concepts = list(concept_rows.values())
        for batch in chunks(concepts, POLICY.concept_batch_size):
            self.run_versioned(
                """
                UNWIND $rows AS row
                MERGE (concept:Concept {id: row.id})
                SET concept.name = row.name,
                    concept.canonical_name = row.canonical_name,
                    concept.concept_type = row.concept_type,
                    concept.domain = row.domain,
                    concept.kb_version = $kb_version
                """,
                rows=batch,
            )
        self._apply_concept_labels(concepts)

        for batch in chunks(claim_rows, POLICY.claim_batch_size):
            self.run_versioned(
                """
                UNWIND $rows AS row
                MATCH (place:Place {id: row.place_id, kb_version: $kb_version})
                OPTIONAL MATCH (place)-[:HAS_OFFERING]->(offering:Offering)
                WITH row, place, head(collect(offering)) AS offering
                WITH row, place, CASE WHEN row.subject_scope = 'offering' AND offering IS NOT NULL
                     THEN offering ELSE place END AS subject
                MATCH (concept:Concept {id: row.concept_id, kb_version: $kb_version})
                MATCH (unit:TextUnit {id: row.text_unit_id, kb_version: $kb_version})
                MERGE (claim:Claim {id: row.id})
                SET claim.predicate = row.predicate,
                    claim.polarity = row.polarity,
                    claim.confidence = row.confidence,
                    claim.evidence_text = row.evidence_text,
                    claim.evidence_start = row.evidence_start,
                    claim.evidence_end = row.evidence_end,
                    claim.extraction_method = row.extraction_method,
                    claim.subject_scope = row.subject_scope,
                    claim.kb_version = $kb_version
                MERGE (claim)-[:ABOUT]->(subject)
                MERGE (claim)-[:OBJECT]->(concept)
                MERGE (claim)-[:SUPPORTED_BY]->(unit)
                """,
                rows=batch,
            )

    def _apply_concept_labels(self, concepts: list[dict[str, Any]]) -> None:
        for concept_type in sorted({row["concept_type"] for row in concepts}):
            ids = [row["id"] for row in concepts if row["concept_type"] == concept_type]
            self.run_versioned(
                f"""
                MATCH (concept:Concept {{kb_version: $kb_version}})
                WHERE concept.id IN $ids
                SET concept:{concept_type}
                """,
                ids=ids,
            )

    def _promote_supported_relationships(self) -> None:
        for relation in ONTOLOGY_RELATIONS.values():
            self.run_versioned(
                f"""
                MATCH (claim:Claim {{kb_version: $kb_version, predicate: $predicate, polarity: 'positive'}})
                      -[:ABOUT]->(subject)
                MATCH (claim)-[:OBJECT]->(concept:Concept)
                WHERE claim.confidence >= $minimum_confidence
                  AND NOT EXISTS {{
                    MATCH (negative:Claim {{kb_version: $kb_version, predicate: $predicate, polarity: 'negative'}})
                          -[:ABOUT]->(subject)
                    MATCH (negative)-[:OBJECT]->(concept)
                  }}
                MERGE (subject)-[relationship:{relation.relationship}]->(concept)
                SET relationship.origin = 'claim_promotion_v4',
                    relationship.confidence = claim.confidence,
                    relationship.claim_id = claim.id
                """,
                predicate=relation.predicate,
                minimum_confidence=POLICY.relationship_promotion_confidence,
            )

    def _build_conflicts(self) -> None:
        self.run_versioned(
            """
            MATCH (positive:Claim {kb_version: $kb_version, polarity: 'positive'})-[:ABOUT]->(subject)
            MATCH (positive)-[:OBJECT]->(concept:Concept)
            MATCH (negative:Claim {
              kb_version: $kb_version,
              predicate: positive.predicate,
              polarity: 'negative'
            })-[:ABOUT]->(subject)
            MATCH (negative)-[:OBJECT]->(concept)
            WITH positive, negative, subject, concept,
                 'conflict:' + positive.id + ':' + negative.id AS conflictId
            MERGE (conflict:DataConflict {id: conflictId})
            SET conflict.predicate = positive.predicate,
                conflict.status = 'unresolved',
                conflict.kb_version = $kb_version
            MERGE (conflict)-[:HAS_POSITIVE_CLAIM]->(positive)
            MERGE (conflict)-[:HAS_NEGATIVE_CLAIM]->(negative)
            MERGE (conflict)-[:ABOUT]->(subject)
            MERGE (conflict)-[:OBJECT]->(concept)
            """
        )
        for relation in ONTOLOGY_RELATIONS.values():
            self.run_versioned(
                f"""
                MATCH (conflict:DataConflict {{kb_version: $kb_version, predicate: $predicate}})
                      -[:ABOUT]->(subject)
                MATCH (conflict)-[:OBJECT]->(concept)
                MATCH (subject)-[relationship:{relation.relationship}]->(concept)
                DELETE relationship
                """,
                predicate=relation.predicate,
            )

    def _build_taxonomy_communities(self) -> None:
        self.run_versioned(
            """
            MATCH (place:Place {kb_version: $kb_version})-[:IN_CITY]->(city:City)
            WITH place, city, place.entity_type AS entityType,
                 'community:' + city.id + ':' + place.entity_type AS communityId
            MERGE (community:Community {id: communityId})
            SET community.name = city.name + ' ' + entityType,
                community.city = city.name,
                community.entity_type = entityType,
                community.algorithm = 'taxonomy_baseline_v4',
                community.kb_version = $kb_version
            MERGE (place)-[:IN_COMMUNITY]->(community)
            """
        )
        self.run_versioned(
            """
            MATCH (community:Community {kb_version: $kb_version})<-[:IN_COMMUNITY]-(place:Place)
            WITH community, count(place) AS memberCount,
                 collect(place.name)[0..$sample_size] AS sampleNames
            MERGE (report:CommunityReport {id: 'report:' + community.id})
            SET report.title = community.name,
                report.summary = community.name + ' contains ' + toString(memberCount) + ' places.',
                report.member_count = memberCount,
                report.sample_names = sampleNames,
                report.generation_method = 'deterministic_taxonomy_summary_v4',
                report.kb_version = $kb_version
            MERGE (community)-[:HAS_REPORT]->(report)
            """,
            sample_size=POLICY.community_sample_size,
        )

    def graph_statistics(self) -> dict[str, int]:
        statistics = super().graph_statistics()
        statistics.pop("facet_edges", None)
        row = self.run_versioned(
            """
            OPTIONAL MATCH (concept:Concept {kb_version: $kb_version})
            WITH count(concept) AS concepts
            OPTIONAL MATCH (claim:Claim {kb_version: $kb_version})
            WITH concepts, count(claim) AS claims
            OPTIONAL MATCH (offering:Offering {kb_version: $kb_version})
            WITH concepts, claims, count(offering) AS offerings
            OPTIONAL MATCH (conflict:DataConflict {kb_version: $kb_version})
            WITH concepts, claims, offerings, count(conflict) AS conflicts
            OPTIONAL MATCH (community:Community {kb_version: $kb_version})
            RETURN concepts, claims, offerings, conflicts, count(community) AS communities
            """
        )[0]
        statistics.update({key: int(value) for key, value in row.items()})
        edge_row = self.run_versioned(
            """
            MATCH ()-[relationship]->()
            WHERE relationship.origin = 'claim_promotion_v4'
            RETURN count(relationship) AS ontology_edges
            """
        )[0]
        statistics["ontology_edges"] = int(edge_row["ontology_edges"])
        return statistics

    def domain_statistics(self) -> dict[str, int]:
        queries = {
            "geographic": "MATCH (place:Place {kb_version: $kb_version})-[relationship:NEAR]->() RETURN count(relationship) AS count",
            "offering_food": "MATCH (node {kb_version: $kb_version}) WHERE node:Offering OR (node:Concept AND node.domain = 'offering') RETURN count(node) AS count",
            "experience_activity": "MATCH (node:Concept {kb_version: $kb_version, domain: 'experience'}) RETURN count(node) AS count",
            "compatibility_constraint": "MATCH (node:Concept {kb_version: $kb_version, domain: 'compatibility'}) RETURN count(node) AS count",
            "temporal_dynamic": "MATCH (node {kb_version: $kb_version}) WHERE (node:Concept AND node.domain = 'temporal') OR node:DynamicObservation RETURN count(node) AS count",
            "provenance": "MATCH (claim:Claim {kb_version: $kb_version})-[:SUPPORTED_BY]->(:TextUnit) RETURN count(claim) AS count",
            "community_summary": "MATCH (community:Community {kb_version: $kb_version})-[:HAS_REPORT]->(:CommunityReport) RETURN count(community) AS count",
        }
        return {
            domain: int(self.run_versioned(query)[0]["count"])
            for domain, query in queries.items()
        }

    def validate_invariants(self, expected_places: int | None = None) -> dict[str, Any]:
        if expected_places is None:
            rows = self.run_versioned(
                """
                MATCH (catalog:TravelCatalog {kb_version: $kb_version})
                RETURN catalog.source_place_count AS place_count
                """
            )
            if not rows or rows[0]["place_count"] is None:
                raise RuntimeError("V4 catalog is missing source_place_count")
            expected_places = int(rows[0]["place_count"])
        report = super().validate_invariants(expected_places)
        row = self.run_versioned(
            """
            OPTIONAL MATCH (claim:Claim {kb_version: $kb_version})
            WITH count(claim) AS claims,
                 count(CASE WHEN count { (claim)-[:SUPPORTED_BY]->(:TextUnit) } <> 1 THEN 1 END) AS badEvidence,
                 count(CASE WHEN count { (claim)-[:ABOUT]->() } <> 1 THEN 1 END) AS badSubjects,
                 count(CASE WHEN count { (claim)-[:OBJECT]->(:Concept) } <> 1 THEN 1 END) AS badObjects
            OPTIONAL MATCH (place:Place {kb_version: $kb_version})
            WITH claims, badEvidence, badSubjects, badObjects,
                 count(CASE WHEN count { (place)-[:IN_COMMUNITY]->(:Community) } <> 1 THEN 1 END) AS badCommunities
            RETURN claims, badEvidence, badSubjects, badObjects, badCommunities
            """
        )[0]
        v4_checks = {
            "claims_have_single_evidence": row["badEvidence"] == 0,
            "claims_have_single_subject": row["badSubjects"] == 0,
            "claims_have_single_object": row["badObjects"] == 0,
            "places_have_single_community": row["badCommunities"] == 0,
            "description_claims_created": row["claims"] > 0,
            "all_domain_subgraphs_materialized": all(
                count > 0 for count in self.domain_statistics().values()
            ),
        }
        report["checks"].update(v4_checks)
        report["counts"].update({key: int(value) for key, value in row.items()})
        report["status"] = "pass" if all(report["checks"].values()) else "fail"
        return report

    def upsert_dynamic_observation(self, observation: DynamicObservationInput) -> dict[str, Any]:
        identifier = "observation:" + sha256(
            f"{observation.subject_id}|{observation.observation_type}|{observation.source}|{observation.observed_at.isoformat()}".encode(
                "utf-8"
            )
        ).hexdigest()[:24]
        rows = self.run_versioned(
            """
            MATCH (subject {id: $subject_id, kb_version: $kb_version})
            WHERE subject:Place OR subject:City OR subject:GeoArea
            MERGE (observation:DynamicObservation {id: $observation_id})
            SET observation.observation_type = $observation_type,
                observation.value_json = $value_json,
                observation.source = $source,
                observation.observed_at = datetime($observed_at),
                observation.expires_at = datetime($expires_at),
                observation.kb_version = $kb_version
            MERGE (observation)-[:OBSERVES]->(subject)
            RETURN observation.id AS observation_id,
                   subject.id AS subject_id,
                   observation.expires_at AS expires_at
            """,
            subject_id=observation.subject_id,
            observation_id=identifier,
            observation_type=observation.observation_type,
            value_json=json.dumps(observation.value, ensure_ascii=False, separators=(",", ":")),
            source=observation.source,
            observed_at=observation.observed_at.isoformat(),
            expires_at=observation.expires_at.isoformat(),
        )
        if not rows:
            raise ValueError(f"Unknown V4 observation subject: {observation.subject_id}")
        return dict(rows[0])


def _offering_type(entity_type: str) -> str | None:
    return {
        "restaurant": ConceptType.FOOD_OFFERING.value,
        "cafe": ConceptType.DRINK_OFFERING.value,
        "nightlife": ConceptType.DRINK_OFFERING.value,
        "hotel": ConceptType.ACCOMMODATION_OFFERING.value,
    }.get(entity_type)


def _concept_domain(concept_type: ConceptType) -> str:
    for relation in ONTOLOGY_RELATIONS.values():
        if relation.concept_type == concept_type:
            return relation.domain
    return "shared"


def _claim_row(
    place: dict[str, Any],
    claim: ExtractedClaim,
    units: list[dict[str, Any]],
) -> dict[str, Any]:
    text_unit_id = f"text-unit:verified:{place['id']}"
    for sequence, unit in enumerate(units):
        if claim.evidence_text == unit["text"]:
            text_unit_id = f"text-unit:description:{place['id']}:{sequence}"
            break
    key = "|".join(
        (
            place["id"],
            claim.subject_scope,
            claim.predicate,
            claim.object_type.value,
            claim.object_name.casefold(),
            claim.polarity.value,
            claim.evidence_text,
        )
    )
    return {
        "id": f"claim:{sha256(key.encode('utf-8')).hexdigest()[:24]}",
        "place_id": place["id"],
        "subject_scope": claim.subject_scope,
        "predicate": claim.predicate,
        "concept_id": concept_id(claim.object_type, canonical_concept(claim.object_name)),
        "polarity": claim.polarity.value,
        "confidence": claim.confidence,
        "evidence_text": claim.evidence_text,
        "evidence_start": claim.evidence_start,
        "evidence_end": claim.evidence_end,
        "extraction_method": claim.extraction_method,
        "text_unit_id": text_unit_id,
    }

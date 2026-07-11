from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KBVersionManifest:
    kb_version: str
    status: str
    dataset: str
    ontology_version: str
    embedding_version: str
    retrieval_version: str
    description: str

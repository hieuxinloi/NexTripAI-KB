from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class JobFailure:
    entity_ids: tuple[str, ...]
    error: str


@dataclass(slots=True)
class JobRunResult:
    run_id: str
    written_paths: list[Path] = field(default_factory=list)
    failures: list[JobFailure] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return not self.failures

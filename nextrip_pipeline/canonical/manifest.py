from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from nextrip_pipeline.publishing._file_lock import destination_file_lock

from .models import CanonicalIdentityManifest


class CanonicalIdentityManifestWriter:
    """Atomically publish one validated canonical identity manifest.

    A semantic no-op (the same manifest hash) preserves the existing bytes and
    audit timestamp. This keeps downstream change detection deterministic even
    when an orchestration run is retried at a different time.
    """

    def __init__(self, destination: str | Path) -> None:
        self.destination = Path(destination)

    def read(self) -> CanonicalIdentityManifest | None:
        if not self.destination.is_file():
            return None
        return CanonicalIdentityManifest.model_validate_json(
            self.destination.read_text(encoding="utf-8")
        )

    def write(self, manifest: CanonicalIdentityManifest) -> Path:
        # ``model_copy`` and ``model_construct`` may bypass Pydantic validation;
        # never persist such an object without rechecking its invariants.
        validated = CanonicalIdentityManifest.model_validate_json(
            manifest.model_dump_json()
        )
        destination = self.destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination_file_lock(destination):
            existing = self.read()
            if (
                existing is not None
                and existing.manifest_hash == validated.manifest_hash
            ):
                return destination

            temporary = destination.with_name(
                f".{destination.name}.{uuid4().hex}.tmp"
            )
            try:
                with temporary.open("x", encoding="utf-8", newline="\n") as file:
                    file.write(validated.model_dump_json(indent=2) + "\n")
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, destination)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return destination


def read_canonical_identity_manifest(
    path: str | Path,
) -> CanonicalIdentityManifest:
    """Read and fully validate a manifest, including all deterministic hashes."""

    manifest = CanonicalIdentityManifestWriter(path).read()
    if manifest is None:
        raise FileNotFoundError(path)
    return manifest

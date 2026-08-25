from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal
from urllib.parse import quote
from uuid import uuid4

from pydantic import AwareDatetime, Field, field_validator, model_validator

from nextrip_pipeline.canonical.dataset import (
    CanonicalActiveDataset,
    read_canonical_active_dataset,
)
from nextrip_pipeline.canonical.models import stable_sha256
from nextrip_pipeline.canonical.readiness import (
    CanonicalDatasetReadinessReport,
    require_canonical_dataset_publish_ready,
    read_canonical_dataset_readiness,
)
from nextrip_pipeline.publishing._file_lock import destination_file_lock
from nextrip_pipeline.schemas import NexTripModel


ACTIVE_DATASET_POINTER_FILENAME = "active-dataset-pointer.json"
PROMOTIONS_DIRECTORY_NAME = "promotions"
PROMOTION_FILENAME = "canonical-dataset-promotion.json"
PROMOTION_TRANSACTION_TARGET = "canonical-dataset-promotion-transaction"


class CanonicalActivePointerError(ValueError):
    """Raised when an active pointer cannot safely identify one ready dataset."""


class CanonicalPromotionAlreadyExistsError(FileExistsError):
    """Raised rather than replace a different immutable promotion audit."""


def _portable_relative_path(value: str) -> str:
    """Validate one platform-independent path rooted at the canonical directory."""

    if "\\" in value:
        raise ValueError("artifact paths must use portable POSIX separators")
    path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        not value
        or path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("artifact paths must be relative to the canonical root")
    return path.as_posix()


def _promotion_payload(
    *,
    activated_at: datetime,
    dataset_id: str,
    dataset_hash: str,
    dataset_path: str,
    readiness_id: str,
    readiness_hash: str,
    readiness_path: str,
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "activated_at": activated_at.astimezone(timezone.utc).isoformat(),
        "dataset_id": dataset_id,
        "dataset_hash": dataset_hash,
        "dataset_path": dataset_path,
        "readiness_id": readiness_id,
        "readiness_hash": readiness_hash,
        "readiness_path": readiness_path,
    }


class CanonicalDatasetPromotion(NexTripModel):
    """Immutable audit proving which publish-ready dataset was activated."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    promotion_id: str = Field(min_length=1)
    promotion_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    activated_at: AwareDatetime
    dataset_id: str = Field(min_length=1)
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_path: str = Field(min_length=1)
    readiness_id: str = Field(min_length=1)
    readiness_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    readiness_path: str = Field(min_length=1)

    @field_validator("activated_at")
    @classmethod
    def normalize_activated_at(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)

    @field_validator("dataset_path", "readiness_path")
    @classmethod
    def validate_artifact_path(cls, value: str) -> str:
        return _portable_relative_path(value)

    @model_validator(mode="after")
    def validate_promotion(self) -> CanonicalDatasetPromotion:
        payload = _promotion_payload(
            activated_at=self.activated_at,
            dataset_id=self.dataset_id,
            dataset_hash=self.dataset_hash,
            dataset_path=self.dataset_path,
            readiness_id=self.readiness_id,
            readiness_hash=self.readiness_hash,
            readiness_path=self.readiness_path,
        )
        expected_hash = stable_sha256(payload)
        if self.promotion_hash != expected_hash:
            raise ValueError("promotion_hash does not match promotion content")
        if self.promotion_id != f"canonical-promotion-{expected_hash[:20]}":
            raise ValueError("promotion_id does not match promotion_hash")
        return self


def _pointer_payload(
    *,
    promotion_id: str,
    promotion_hash: str,
    activated_at: datetime,
    dataset_id: str,
    dataset_hash: str,
    dataset_path: str,
    readiness_id: str,
    readiness_hash: str,
    readiness_path: str,
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "promotion_id": promotion_id,
        "promotion_hash": promotion_hash,
        "activated_at": activated_at.astimezone(timezone.utc).isoformat(),
        "dataset_id": dataset_id,
        "dataset_hash": dataset_hash,
        "dataset_path": dataset_path,
        "readiness_id": readiness_id,
        "readiness_hash": readiness_hash,
        "readiness_path": readiness_path,
    }


class CanonicalActiveDatasetPointer(NexTripModel):
    """Mutable, integrity-checked reference to one immutable promotion."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    pointer_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    promotion_id: str = Field(min_length=1)
    promotion_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    activated_at: AwareDatetime
    dataset_id: str = Field(min_length=1)
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_path: str = Field(min_length=1)
    readiness_id: str = Field(min_length=1)
    readiness_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    readiness_path: str = Field(min_length=1)

    @field_validator("activated_at")
    @classmethod
    def normalize_activated_at(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)

    @field_validator("dataset_path", "readiness_path")
    @classmethod
    def validate_artifact_path(cls, value: str) -> str:
        return _portable_relative_path(value)

    @model_validator(mode="after")
    def validate_pointer(self) -> CanonicalActiveDatasetPointer:
        payload = _pointer_payload(
            promotion_id=self.promotion_id,
            promotion_hash=self.promotion_hash,
            activated_at=self.activated_at,
            dataset_id=self.dataset_id,
            dataset_hash=self.dataset_hash,
            dataset_path=self.dataset_path,
            readiness_id=self.readiness_id,
            readiness_hash=self.readiness_hash,
            readiness_path=self.readiness_path,
        )
        if self.pointer_hash != stable_sha256(payload):
            raise ValueError("pointer_hash does not match pointer content")
        return self


@dataclass(frozen=True)
class ResolvedCanonicalActiveDataset:
    """Fully validated active dataset and the audit artifacts that select it."""

    pointer: CanonicalActiveDatasetPointer
    promotion: CanonicalDatasetPromotion
    dataset: CanonicalActiveDataset
    readiness: CanonicalDatasetReadinessReport
    pointer_path: Path
    promotion_path: Path
    dataset_path: Path
    readiness_path: Path


def promote_canonical_active_dataset(
    canonical_root: str | Path,
    dataset_path: str | Path,
    readiness_path: str | Path,
    *,
    activated_at: datetime | None = None,
    expected_active_dataset_id: str | None = None,
    expected_active_dataset_hash: str | None = None,
) -> CanonicalActiveDatasetPointer:
    """Validate, audit, and atomically activate a publish-ready dataset."""

    if (expected_active_dataset_id is None) != (
        expected_active_dataset_hash is None
    ):
        raise CanonicalActivePointerError(
            "expected active dataset ID and hash must be supplied together"
        )

    root = _resolved_root(canonical_root)
    # Serialize the read/check/audit/pointer transaction. Without this outer
    # lock, two workers can both observe the old pointer and create distinct
    # timestamped promotion audits for the same immutable pair.
    with destination_file_lock(root / PROMOTION_TRANSACTION_TARGET):
        return _promote_canonical_active_dataset_locked(
            root,
            dataset_path,
            readiness_path,
            activated_at=activated_at,
            expected_active_dataset_id=expected_active_dataset_id,
            expected_active_dataset_hash=expected_active_dataset_hash,
        )


def _promote_canonical_active_dataset_locked(
    canonical_root: str | Path,
    dataset_path: str | Path,
    readiness_path: str | Path,
    *,
    activated_at: datetime | None,
    expected_active_dataset_id: str | None,
    expected_active_dataset_hash: str | None,
) -> CanonicalActiveDatasetPointer:
    root = _resolved_root(canonical_root)
    dataset_file, dataset_relative = _resolve_input_artifact(root, dataset_path)
    readiness_file, readiness_relative = _resolve_input_artifact(
        root, readiness_path
    )
    dataset, readiness = _read_and_validate_pair(dataset_file, readiness_file)
    pointer_path = root / ACTIVE_DATASET_POINTER_FILENAME
    if pointer_path.exists():
        try:
            current = resolve_active_canonical_dataset(root)
        except CanonicalActivePointerError as pointer_error:
            # Preserve an immutable audit that has become unreadable. Retrying
            # a promotion must never create a replacement audit and silently
            # move the pointer around the corrupted evidence.
            try:
                existing_pointer = read_canonical_active_dataset_pointer(
                    pointer_path
                )
            except (OSError, TypeError, ValueError) as error:
                raise pointer_error from error
            existing_promotion_path = _promotion_path(
                root, existing_pointer.promotion_id
            )
            if existing_promotion_path.exists():
                try:
                    read_canonical_dataset_promotion(existing_promotion_path)
                except (OSError, TypeError, ValueError) as error:
                    raise CanonicalPromotionAlreadyExistsError(
                        "immutable promotion path is invalid: "
                        f"{existing_promotion_path}"
                    ) from error
            raise pointer_error
        if (
            current.pointer.dataset_id == dataset.dataset_id
            and current.pointer.dataset_hash == dataset.dataset_hash
            and current.pointer.readiness_id == readiness.readiness_id
            and current.pointer.readiness_hash == readiness.readiness_hash
        ):
            # Airflow/task retries for the same immutable pair are a semantic
            # no-op even when the caller does not pin an activation timestamp.
            return current.pointer
        if expected_active_dataset_id is not None and (
            current.pointer.dataset_id,
            current.pointer.dataset_hash,
        ) != (
            expected_active_dataset_id,
            expected_active_dataset_hash,
        ):
            raise CanonicalActivePointerError(
                "active canonical dataset changed before promotion: "
                f"expected {expected_active_dataset_id}, "
                f"found {current.pointer.dataset_id}"
            )
    elif expected_active_dataset_id is not None:
        raise CanonicalActivePointerError(
            "cannot compare-and-swap a missing active canonical pointer"
        )
    effective_time = activated_at or datetime.now(timezone.utc)
    if effective_time.tzinfo is None or effective_time.utcoffset() is None:
        raise CanonicalActivePointerError("activated_at must be timezone-aware")
    effective_time = effective_time.astimezone(timezone.utc)

    promotion_values = _promotion_payload(
        activated_at=effective_time,
        dataset_id=dataset.dataset_id,
        dataset_hash=dataset.dataset_hash,
        dataset_path=dataset_relative,
        readiness_id=readiness.readiness_id,
        readiness_hash=readiness.readiness_hash,
        readiness_path=readiness_relative,
    )
    promotion_hash = stable_sha256(promotion_values)
    promotion = CanonicalDatasetPromotion(
        promotion_id=f"canonical-promotion-{promotion_hash[:20]}",
        promotion_hash=promotion_hash,
        **promotion_values,
    )
    promotion_path = _promotion_path(root, promotion.promotion_id)
    _write_immutable_promotion(promotion_path, promotion)

    pointer_values = _pointer_payload(
        promotion_id=promotion.promotion_id,
        promotion_hash=promotion.promotion_hash,
        activated_at=promotion.activated_at,
        dataset_id=promotion.dataset_id,
        dataset_hash=promotion.dataset_hash,
        dataset_path=promotion.dataset_path,
        readiness_id=promotion.readiness_id,
        readiness_hash=promotion.readiness_hash,
        readiness_path=promotion.readiness_path,
    )
    pointer = CanonicalActiveDatasetPointer(
        pointer_hash=stable_sha256(pointer_values),
        **pointer_values,
    )
    _write_mutable_pointer(pointer_path, pointer)
    return pointer


def read_canonical_active_dataset_pointer(
    path: str | Path,
) -> CanonicalActiveDatasetPointer:
    """Read and validate a pointer file, including its deterministic hash."""

    return CanonicalActiveDatasetPointer.model_validate_json(Path(path).read_bytes())


def read_canonical_dataset_promotion(
    path: str | Path,
) -> CanonicalDatasetPromotion:
    """Read and validate one immutable promotion audit."""

    return CanonicalDatasetPromotion.model_validate_json(Path(path).read_bytes())


def resolve_active_canonical_dataset(
    canonical_root: str | Path,
) -> ResolvedCanonicalActiveDataset:
    """Resolve the pointer and fail closed if any pinned artifact has changed."""

    root = _resolved_root(canonical_root)
    pointer_path = root / ACTIVE_DATASET_POINTER_FILENAME
    try:
        pointer = read_canonical_active_dataset_pointer(pointer_path)
    except (OSError, TypeError, ValueError) as error:
        raise CanonicalActivePointerError(
            f"cannot read active canonical pointer {pointer_path}: {error}"
        ) from error

    promotion_path = _promotion_path(root, pointer.promotion_id)
    try:
        promotion = read_canonical_dataset_promotion(promotion_path)
    except (OSError, TypeError, ValueError) as error:
        raise CanonicalActivePointerError(
            f"cannot read canonical promotion {promotion_path}: {error}"
        ) from error
    _require_pointer_matches_promotion(pointer, promotion)

    dataset_path = _resolve_stored_artifact(root, pointer.dataset_path)
    readiness_path = _resolve_stored_artifact(root, pointer.readiness_path)
    dataset, readiness = _read_and_validate_pair(dataset_path, readiness_path)
    if (dataset.dataset_id, dataset.dataset_hash) != (
        pointer.dataset_id,
        pointer.dataset_hash,
    ):
        raise CanonicalActivePointerError(
            "active pointer does not match its canonical dataset"
        )
    if (readiness.readiness_id, readiness.readiness_hash) != (
        pointer.readiness_id,
        pointer.readiness_hash,
    ):
        raise CanonicalActivePointerError(
            "active pointer does not match its readiness report"
        )
    return ResolvedCanonicalActiveDataset(
        pointer=pointer,
        promotion=promotion,
        dataset=dataset,
        readiness=readiness,
        pointer_path=pointer_path,
        promotion_path=promotion_path,
        dataset_path=dataset_path,
        readiness_path=readiness_path,
    )


def _resolved_root(canonical_root: str | Path) -> Path:
    root = Path(canonical_root)
    try:
        return root.resolve(strict=True)
    except OSError as error:
        raise CanonicalActivePointerError(
            f"canonical root does not exist: {root}"
        ) from error


def _resolve_input_artifact(root: Path, value: str | Path) -> tuple[Path, str]:
    supplied = Path(value)
    candidate = supplied if supplied.is_absolute() else root / supplied
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise CanonicalActivePointerError(
            f"canonical artifact does not exist: {candidate}"
        ) from error
    if not resolved.is_file():
        raise CanonicalActivePointerError(
            f"canonical artifact is not a file: {resolved}"
        )
    return resolved, _relative_to_root(root, resolved)


def _resolve_stored_artifact(root: Path, value: str) -> Path:
    relative = _portable_relative_path(value)
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise CanonicalActivePointerError(
            f"active canonical artifact does not exist: {candidate}"
        ) from error
    if not resolved.is_file():
        raise CanonicalActivePointerError(
            f"active canonical artifact is not a file: {resolved}"
        )
    _relative_to_root(root, resolved)
    return resolved


def _relative_to_root(root: Path, artifact: Path) -> str:
    try:
        relative = artifact.relative_to(root)
    except ValueError as error:
        raise CanonicalActivePointerError(
            f"canonical artifact escapes canonical root: {artifact}"
        ) from error
    return _portable_relative_path(PurePosixPath(*relative.parts).as_posix())


def _read_and_validate_pair(
    dataset_path: Path,
    readiness_path: Path,
) -> tuple[CanonicalActiveDataset, CanonicalDatasetReadinessReport]:
    try:
        dataset = read_canonical_active_dataset(dataset_path)
    except (OSError, TypeError, ValueError) as error:
        raise CanonicalActivePointerError(
            f"cannot validate canonical dataset {dataset_path}: {error}"
        ) from error
    try:
        readiness = read_canonical_dataset_readiness(readiness_path)
    except (OSError, TypeError, ValueError) as error:
        raise CanonicalActivePointerError(
            f"cannot validate canonical readiness {readiness_path}: {error}"
        ) from error
    if (
        readiness.dataset_id != dataset.dataset_id
        or readiness.dataset_hash != dataset.dataset_hash
        or readiness.manifest_id != dataset.manifest_id
        or readiness.manifest_hash != dataset.manifest_hash
    ):
        raise CanonicalActivePointerError(
            "canonical dataset and readiness report identify different snapshots"
        )
    require_canonical_dataset_publish_ready(readiness)
    return dataset, readiness


def _promotion_path(root: Path, promotion_id: str) -> Path:
    return (
        root
        / PROMOTIONS_DIRECTORY_NAME
        / f"promotion={quote(promotion_id, safe='-_.')}"
        / PROMOTION_FILENAME
    )


def _require_pointer_matches_promotion(
    pointer: CanonicalActiveDatasetPointer,
    promotion: CanonicalDatasetPromotion,
) -> None:
    pointer_values = pointer.model_dump(mode="json", exclude={"pointer_hash"})
    promotion_values = promotion.model_dump(
        mode="json", exclude={"promotion_id", "promotion_hash"}
    )
    expected = {
        **promotion_values,
        "promotion_id": promotion.promotion_id,
        "promotion_hash": promotion.promotion_hash,
    }
    if pointer_values != expected:
        raise CanonicalActivePointerError(
            "active pointer does not match its immutable promotion audit"
        )


def _serialized_json(value: NexTripModel) -> str:
    return (
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _write_immutable_promotion(
    destination: Path,
    promotion: CanonicalDatasetPromotion,
) -> None:
    validated = CanonicalDatasetPromotion.model_validate_json(
        promotion.model_dump_json()
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination_file_lock(destination):
        if destination.exists():
            try:
                existing = read_canonical_dataset_promotion(destination)
            except (OSError, TypeError, ValueError) as error:
                raise CanonicalPromotionAlreadyExistsError(
                    f"immutable promotion path is invalid: {destination}"
                ) from error
            if existing == validated:
                return
            raise CanonicalPromotionAlreadyExistsError(
                f"immutable promotion path already exists: {destination}"
            )
        _atomic_replace(destination, _serialized_json(validated))


def _write_mutable_pointer(
    destination: Path,
    pointer: CanonicalActiveDatasetPointer,
) -> None:
    validated = CanonicalActiveDatasetPointer.model_validate_json(
        pointer.model_dump_json()
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination_file_lock(destination):
        if destination.exists():
            try:
                existing = read_canonical_active_dataset_pointer(destination)
            except (OSError, TypeError, ValueError) as error:
                raise CanonicalActivePointerError(
                    f"existing active pointer is invalid: {destination}"
                ) from error
            if existing == validated:
                return
        _atomic_replace(destination, _serialized_json(validated))


def _atomic_replace(destination: Path, content: str) -> None:
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from nextrip_pipeline.canonical import hotel_identity_replacement as replacement
from nextrip_pipeline.schemas import NexTripModel


_SHA256_PATTERN = r"^[a-f0-9]{64}$"


class HotelIdentityReplacementRollbackError(ValueError):
    """Raised when a replacement batch cannot be rolled back safely."""


class HotelIdentityReplacementRollbackResult(NexTripModel):
    mode: Literal["check", "apply"]
    status: Literal["eligible", "rolled_back", "already_rolled_back"]
    source_batch_id: str
    source_batch_hash: str = Field(pattern=_SHA256_PATTERN)
    source_audit_path: str
    source_intent_path: str
    master_path: str
    expected_current_sha256: str = Field(pattern=_SHA256_PATTERN)
    restored_sha256: str = Field(pattern=_SHA256_PATTERN)
    rollback_intent_path: str | None = None
    rollback_audit_path: str | None = None


def rollback_hotel_identity_replacement(
    *,
    batch_hash: str,
    master_path: str | Path,
    repository_root: str | Path = ".",
    audit_root: str | Path = "data/audit/hotel_identity_replacements",
    apply: bool = False,
    now: datetime | None = None,
) -> HotelIdentityReplacementRollbackResult:
    normalized_batch_hash = batch_hash.casefold()
    if re.fullmatch(_SHA256_PATTERN, normalized_batch_hash) is None:
        raise HotelIdentityReplacementRollbackError(
            "rollback batch_hash must be a full lowercase SHA-256"
        )

    root = Path(repository_root).resolve()
    master_destination = replacement._resolve_path(root, master_path)
    audit_directory = replacement._resolve_path(root, audit_root)
    prefix = normalized_batch_hash[:20]
    source_audit_path = audit_directory / f"batch={prefix}.json"
    expected_source_intent_path = audit_directory / f"intent={prefix}.json"
    rollback_intent_path = audit_directory / f"rollback-intent={prefix}.json"
    rollback_audit_path = audit_directory / f"rollback={prefix}.json"

    source_audit_bytes, source_audit = _read_json_object(
        source_audit_path, "source replacement audit"
    )
    _validate_hashed_document(
        source_audit,
        hash_field="audit_hash",
        id_field="audit_id",
        id_prefix="hotel-identity-replacement-audit-",
        label="source replacement audit",
    )
    source_batch_id = f"hotel-identity-replacement-{prefix}"
    expected_audit_header = {
        "batch_id": source_batch_id,
        "batch_hash": normalized_batch_hash,
        "master_path": replacement._relative_text(root, master_destination),
    }
    _require_values(
        source_audit,
        expected_audit_header,
        label="source replacement audit",
    )
    source_master_before = _require_sha256(
        source_audit.get("master_before_sha256"),
        "source audit master_before_sha256",
    )
    source_master_after = _require_sha256(
        source_audit.get("master_after_sha256"),
        "source audit master_after_sha256",
    )
    if source_master_before == source_master_after:
        raise HotelIdentityReplacementRollbackError(
            "source replacement audit does not describe a master mutation"
        )

    source_intent_reference = source_audit.get("intent_path")
    source_intent_sha256 = _require_sha256(
        source_audit.get("intent_sha256"), "source audit intent_sha256"
    )
    if not isinstance(source_intent_reference, str):
        raise HotelIdentityReplacementRollbackError(
            "source replacement audit has no immutable intent reference"
        )
    source_intent_path = replacement._resolve_path(root, source_intent_reference)
    if source_intent_path != expected_source_intent_path.resolve():
        raise HotelIdentityReplacementRollbackError(
            "source replacement audit references an unexpected intent path"
        )
    source_intent_bytes, source_intent = _read_json_object(
        source_intent_path, "source replacement intent"
    )
    if replacement._sha256(source_intent_bytes) != source_intent_sha256:
        raise HotelIdentityReplacementRollbackError(
            "source replacement intent file hash mismatch"
        )
    _validate_hashed_document(
        source_intent,
        hash_field="intent_hash",
        id_field="intent_id",
        id_prefix="hotel-identity-replacement-intent-",
        label="source replacement intent",
    )
    _require_values(
        source_intent,
        {
            "batch_id": source_batch_id,
            "batch_hash": normalized_batch_hash,
            "master_path": replacement._relative_text(root, master_destination),
            "master_preimage_sha256": source_master_before,
        },
        label="source replacement intent",
    )
    _validate_target_chain(source_audit, source_intent)
    master_preimage = _decode_master_preimage(source_intent)
    if replacement._sha256(master_preimage) != source_master_before:
        raise HotelIdentityReplacementRollbackError(
            "source replacement intent preimage does not match source audit"
        )
    _validate_master_document(master_preimage)

    source_audit_sha256 = replacement._sha256(source_audit_bytes)
    rollback_intent = _rollback_intent_document(
        source_batch_id=source_batch_id,
        source_batch_hash=normalized_batch_hash,
        source_audit_path=replacement._relative_text(root, source_audit_path),
        source_audit_sha256=source_audit_sha256,
        source_intent_path=replacement._relative_text(root, source_intent_path),
        source_intent_sha256=source_intent_sha256,
        master_path=replacement._relative_text(root, master_destination),
        expected_current_sha256=source_master_after,
        restored_sha256=source_master_before,
    )

    current_sha256 = replacement._sha256(master_destination.read_bytes())
    if rollback_audit_path.is_file():
        _validate_completed_rollback(
            root=root,
            path=rollback_audit_path,
            current_master_sha256=current_sha256,
            expected_intent=rollback_intent,
            rollback_intent_path=rollback_intent_path,
            source_batch_hash=normalized_batch_hash,
            restored_sha256=source_master_before,
        )
        return _result(
            mode="apply" if apply else "check",
            status="already_rolled_back",
            root=root,
            source_batch_id=source_batch_id,
            source_batch_hash=normalized_batch_hash,
            source_audit_path=source_audit_path,
            source_intent_path=source_intent_path,
            master_path=master_destination,
            expected_current_sha256=source_master_after,
            restored_sha256=source_master_before,
            rollback_intent_path=rollback_intent_path,
            rollback_audit_path=rollback_audit_path,
        )

    interrupted_after_restore = False
    if current_sha256 == source_master_before and rollback_intent_path.is_file():
        interrupted_after_restore = True
    elif current_sha256 != source_master_after:
        raise HotelIdentityReplacementRollbackError(
            "current master SHA does not match the source audit master_after SHA"
        )

    if not apply:
        return _result(
            mode="check",
            status="eligible",
            root=root,
            source_batch_id=source_batch_id,
            source_batch_hash=normalized_batch_hash,
            source_audit_path=source_audit_path,
            source_intent_path=source_intent_path,
            master_path=master_destination,
            expected_current_sha256=source_master_after,
            restored_sha256=source_master_before,
            rollback_intent_path=(
                rollback_intent_path if rollback_intent_path.is_file() else None
            ),
            rollback_audit_path=None,
        )

    replacement._write_immutable_json(rollback_intent_path, rollback_intent)
    if not interrupted_after_restore:
        replacement._atomic_replace_bytes(master_destination, master_preimage)
    if replacement._sha256(master_destination.read_bytes()) != source_master_before:
        raise HotelIdentityReplacementRollbackError(
            "master SHA verification failed after rollback"
        )

    rolled_back_at = now or datetime.now(timezone.utc)
    if rolled_back_at.tzinfo is None or rolled_back_at.utcoffset() is None:
        raise HotelIdentityReplacementRollbackError(
            "rollback time must be timezone-aware"
        )
    rollback_values: dict[str, Any] = {
        "schema_version": "1.0.0",
        "source_batch_id": source_batch_id,
        "source_batch_hash": normalized_batch_hash,
        "source_audit_path": replacement._relative_text(root, source_audit_path),
        "source_audit_sha256": source_audit_sha256,
        "source_intent_path": replacement._relative_text(root, source_intent_path),
        "source_intent_sha256": source_intent_sha256,
        "rollback_intent_path": replacement._relative_text(root, rollback_intent_path),
        "rollback_intent_sha256": replacement._sha256(
            rollback_intent_path.read_bytes()
        ),
        "master_path": replacement._relative_text(root, master_destination),
        "master_before_rollback_sha256": source_master_after,
        "master_after_rollback_sha256": source_master_before,
        "rolled_back_at": rolled_back_at.isoformat(),
        "recovered_after_interrupted_rollback": interrupted_after_restore,
        "projection_policy": (
            "No current projection, mapping, availability, price, traffic-cache, "
            "raw, review, source audit, or source intent artifact was modified."
        ),
    }
    rollback_hash = replacement._stable_sha256(rollback_values)
    rollback_document = {
        **rollback_values,
        "rollback_id": f"hotel-identity-replacement-rollback-{rollback_hash[:20]}",
        "rollback_hash": rollback_hash,
    }
    replacement._write_immutable_json(rollback_audit_path, rollback_document)
    return _result(
        mode="apply",
        status="rolled_back",
        root=root,
        source_batch_id=source_batch_id,
        source_batch_hash=normalized_batch_hash,
        source_audit_path=source_audit_path,
        source_intent_path=source_intent_path,
        master_path=master_destination,
        expected_current_sha256=source_master_after,
        restored_sha256=source_master_before,
        rollback_intent_path=rollback_intent_path,
        rollback_audit_path=rollback_audit_path,
    )


def _rollback_intent_document(
    *,
    source_batch_id: str,
    source_batch_hash: str,
    source_audit_path: str,
    source_audit_sha256: str,
    source_intent_path: str,
    source_intent_sha256: str,
    master_path: str,
    expected_current_sha256: str,
    restored_sha256: str,
) -> dict[str, Any]:
    values = {
        "schema_version": "1.0.0",
        "source_batch_id": source_batch_id,
        "source_batch_hash": source_batch_hash,
        "source_audit_path": source_audit_path,
        "source_audit_sha256": source_audit_sha256,
        "source_intent_path": source_intent_path,
        "source_intent_sha256": source_intent_sha256,
        "master_path": master_path,
        "expected_current_sha256": expected_current_sha256,
        "restored_sha256": restored_sha256,
    }
    intent_hash = replacement._stable_sha256(values)
    return {
        **values,
        "rollback_intent_id": (
            f"hotel-identity-replacement-rollback-intent-{intent_hash[:20]}"
        ),
        "rollback_intent_hash": intent_hash,
    }


def _validate_completed_rollback(
    *,
    root: Path,
    path: Path,
    current_master_sha256: str,
    expected_intent: Mapping[str, Any],
    rollback_intent_path: Path,
    source_batch_hash: str,
    restored_sha256: str,
) -> None:
    if current_master_sha256 != restored_sha256:
        raise HotelIdentityReplacementRollbackError(
            "completed rollback master has drifted"
        )
    rollback_intent_bytes, actual_intent = _read_json_object(
        rollback_intent_path, "rollback intent"
    )
    if actual_intent != expected_intent:
        raise HotelIdentityReplacementRollbackError("rollback intent has drifted")
    _, audit = _read_json_object(path, "rollback audit")
    _validate_hashed_document(
        audit,
        hash_field="rollback_hash",
        id_field="rollback_id",
        id_prefix="hotel-identity-replacement-rollback-",
        label="rollback audit",
    )
    _require_values(
        audit,
        {
            "source_batch_hash": source_batch_hash,
            "rollback_intent_path": replacement._relative_text(
                root, rollback_intent_path
            ),
            "rollback_intent_sha256": replacement._sha256(rollback_intent_bytes),
            "master_after_rollback_sha256": restored_sha256,
        },
        label="rollback audit",
    )


def _validate_target_chain(
    source_audit: Mapping[str, Any], source_intent: Mapping[str, Any]
) -> None:
    replacements = source_audit.get("replacements")
    targets = source_intent.get("targets")
    if not isinstance(replacements, list) or not isinstance(targets, list):
        raise HotelIdentityReplacementRollbackError(
            "source audit/intent target chain is invalid"
        )
    audit_target_ids: list[str] = []
    for item in replacements:
        if not isinstance(item, Mapping) or not isinstance(
            item.get("old_identity"), Mapping
        ):
            raise HotelIdentityReplacementRollbackError(
                "source audit replacement target is invalid"
            )
        target_id = str(item.get("target_id") or "")
        if not target_id:
            raise HotelIdentityReplacementRollbackError(
                "source audit replacement target is invalid"
            )
        audit_target_ids.append(target_id)
    intent_target_ids = [
        str(item.get("target_id") or "")
        for item in targets
        if isinstance(item, Mapping)
    ]
    if (
        len(intent_target_ids) != len(targets)
        or any(not item for item in intent_target_ids)
        or len(set(audit_target_ids)) != len(audit_target_ids)
        or len(set(intent_target_ids)) != len(intent_target_ids)
        or sorted(audit_target_ids) != sorted(intent_target_ids)
    ):
        raise HotelIdentityReplacementRollbackError(
            "source audit replacements do not match source intent targets"
        )


def _decode_master_preimage(intent: Mapping[str, Any]) -> bytes:
    encoded = intent.get("master_preimage_base64")
    if not isinstance(encoded, str):
        raise HotelIdentityReplacementRollbackError(
            "source replacement intent has no encoded master preimage"
        )
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise HotelIdentityReplacementRollbackError(
            "source replacement intent master preimage is invalid"
        ) from error


def _validate_master_document(payload: bytes) -> None:
    try:
        document = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HotelIdentityReplacementRollbackError(
            f"source replacement intent master preimage is invalid: {error}"
        ) from error
    if not isinstance(document, Mapping) or not isinstance(document.get("data"), list):
        raise HotelIdentityReplacementRollbackError(
            "source replacement intent preimage is not a hotel master document"
        )


def _read_json_object(path: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    try:
        payload = path.read_bytes()
        document = json.loads(payload.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HotelIdentityReplacementRollbackError(
            f"{label} is unreadable: {error}"
        ) from error
    if not isinstance(document, dict):
        raise HotelIdentityReplacementRollbackError(f"{label} must be an object")
    return payload, document


def _validate_hashed_document(
    document: Mapping[str, Any],
    *,
    hash_field: str,
    id_field: str,
    id_prefix: str,
    label: str,
) -> None:
    actual_hash = str(document.get(hash_field) or "")
    values = {
        key: value
        for key, value in document.items()
        if key not in {hash_field, id_field}
    }
    expected_hash = replacement._stable_sha256(values)
    if actual_hash != expected_hash:
        raise HotelIdentityReplacementRollbackError(f"{label} {hash_field} mismatch")
    if document.get(id_field) != f"{id_prefix}{expected_hash[:20]}":
        raise HotelIdentityReplacementRollbackError(f"{label} {id_field} mismatch")


def _require_values(
    document: Mapping[str, Any], expected: Mapping[str, Any], *, label: str
) -> None:
    for key, value in expected.items():
        if document.get(key) != value:
            raise HotelIdentityReplacementRollbackError(f"{label} mismatch: {key}")


def _require_sha256(value: object, label: str) -> str:
    normalized = str(value or "").casefold()
    if re.fullmatch(_SHA256_PATTERN, normalized) is None:
        raise HotelIdentityReplacementRollbackError(f"{label} is invalid")
    return normalized


def _result(
    *,
    mode: Literal["check", "apply"],
    status: Literal["eligible", "rolled_back", "already_rolled_back"],
    root: Path,
    source_batch_id: str,
    source_batch_hash: str,
    source_audit_path: Path,
    source_intent_path: Path,
    master_path: Path,
    expected_current_sha256: str,
    restored_sha256: str,
    rollback_intent_path: Path | None,
    rollback_audit_path: Path | None,
) -> HotelIdentityReplacementRollbackResult:
    return HotelIdentityReplacementRollbackResult(
        mode=mode,
        status=status,
        source_batch_id=source_batch_id,
        source_batch_hash=source_batch_hash,
        source_audit_path=replacement._relative_text(root, source_audit_path),
        source_intent_path=replacement._relative_text(root, source_intent_path),
        master_path=replacement._relative_text(root, master_path),
        expected_current_sha256=expected_current_sha256,
        restored_sha256=restored_sha256,
        rollback_intent_path=(
            replacement._relative_text(root, rollback_intent_path)
            if rollback_intent_path is not None
            else None
        ),
        rollback_audit_path=(
            replacement._relative_text(root, rollback_audit_path)
            if rollback_audit_path is not None
            else None
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audit or apply an exact-master rollback for one replacement batch."
        )
    )
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--batch-hash", required=True)
    parser.add_argument(
        "--master",
        type=Path,
        required=True,
        help="Explicit legacy hotel corpus mutated by the replacement batch.",
    )
    parser.add_argument(
        "--audit-root",
        type=Path,
        default=Path("data/audit/hotel_identity_replacements"),
    )
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        result = rollback_hotel_identity_replacement(
            repository_root=arguments.repository_root,
            batch_hash=arguments.batch_hash,
            master_path=arguments.master,
            audit_root=arguments.audit_root,
            apply=arguments.apply,
        )
    except (HotelIdentityReplacementRollbackError, OSError, ValueError) as error:
        print(f"Cannot rollback hotel identity replacement: {error}")
        return 1
    print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

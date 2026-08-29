"""A small, thread-safe SQLite cache for route and matrix payloads.

The cache deliberately knows nothing about Pydantic traffic models.  Values are
stored as canonical JSON so providers and services can evolve independently of
the persistence layer.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Self


def _utc_datetime(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime, *, field_name: str) -> str:
    return (
        _utc_datetime(value, field_name=field_name)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _json_default(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, datetime):
        return _timestamp(value, field_name="JSON datetime")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def make_cache_key(namespace: str, value: Any) -> str:
    """Return a stable namespaced key for any JSON-serializable value."""

    normalized_namespace = namespace.strip()
    if not normalized_namespace:
        raise ValueError("namespace must not be blank")
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    return f"{normalized_namespace}:{digest}"


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """A cached JSON payload together with its persistence metadata."""

    key: str
    payload: Any
    expires_at: datetime
    created_at: datetime
    updated_at: datetime
    observation_id: str | None
    matrix_id: str | None
    is_stale: bool


@dataclass(frozen=True, slots=True)
class CacheStats:
    """Current cache entry counts at a given instant."""

    total_entries: int
    fresh_entries: int
    stale_entries: int

    def as_dict(self) -> dict[str, int]:
        return {
            "total_entries": self.total_entries,
            "fresh_entries": self.fresh_entries,
            "stale_entries": self.stale_entries,
        }


class SQLiteTrafficCache:
    """Persistent JSON cache backed by one synchronized SQLite connection."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be non-negative")

        raw_path = str(path)
        if raw_path != ":memory:":
            Path(raw_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            raw_path = str(Path(raw_path).expanduser())

        self.path = raw_path
        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            raw_path,
            timeout=busy_timeout_ms / 1_000,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms:d}")
            if raw_path != ":memory:":
                self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = NORMAL")
            self._create_schema()

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS traffic_cache (
                    cache_key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    observation_id TEXT,
                    matrix_id TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_traffic_cache_expires_at
                    ON traffic_cache(expires_at);
                CREATE INDEX IF NOT EXISTS idx_traffic_cache_observation_id
                    ON traffic_cache(observation_id);
                CREATE INDEX IF NOT EXISTS idx_traffic_cache_matrix_id
                    ON traffic_cache(matrix_id);
                """
            )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("traffic cache is closed")

    @staticmethod
    def _payload_id(payload: Any, name: str) -> str | None:
        if not isinstance(payload, Mapping):
            return None
        value = payload.get(name)
        if value is None:
            nested_name = "observation" if name == "observation_id" else "matrix"
            nested = payload.get(nested_name)
            if isinstance(nested, Mapping):
                value = nested.get(name)
        return str(value) if value not in (None, "") else None

    def put(
        self,
        key: str,
        payload: Any,
        *,
        expires_at: datetime,
        observation_id: str | None = None,
        matrix_id: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Atomically insert or replace a cache entry."""

        normalized_key = key.strip()
        if not normalized_key:
            raise ValueError("key must not be blank")
        expires_text = _timestamp(expires_at, field_name="expires_at")
        now_value = now or datetime.now(UTC)
        now_text = _timestamp(now_value, field_name="now")
        payload_json = _canonical_json(payload)
        observation_id = observation_id or self._payload_id(payload, "observation_id")
        matrix_id = matrix_id or self._payload_id(payload, "matrix_id")

        with self._lock:
            self._ensure_open()
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO traffic_cache (
                        cache_key,
                        payload_json,
                        expires_at,
                        created_at,
                        updated_at,
                        observation_id,
                        matrix_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        payload_json = excluded.payload_json,
                        expires_at = excluded.expires_at,
                        updated_at = excluded.updated_at,
                        observation_id = excluded.observation_id,
                        matrix_id = excluded.matrix_id
                    """,
                    (
                        normalized_key,
                        payload_json,
                        expires_text,
                        now_text,
                        now_text,
                        observation_id,
                        matrix_id,
                    ),
                )

    def _row_to_entry(self, row: sqlite3.Row, *, now: datetime) -> CacheEntry:
        expires_at = _parse_timestamp(row["expires_at"])
        return CacheEntry(
            key=row["cache_key"],
            payload=json.loads(row["payload_json"]),
            expires_at=expires_at,
            created_at=_parse_timestamp(row["created_at"]),
            updated_at=_parse_timestamp(row["updated_at"]),
            observation_id=row["observation_id"],
            matrix_id=row["matrix_id"],
            is_stale=expires_at <= now,
        )

    def get_entry(
        self,
        key: str,
        *,
        allow_stale: bool = False,
        now: datetime | None = None,
    ) -> CacheEntry | None:
        """Read an entry, treating expired values as misses by default."""

        now_value = _utc_datetime(now or datetime.now(UTC), field_name="now")
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM traffic_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        entry = self._row_to_entry(row, now=now_value)
        if entry.is_stale and not allow_stale:
            return None
        return entry

    def get(
        self,
        key: str,
        *,
        allow_stale: bool = False,
        now: datetime | None = None,
    ) -> Any | None:
        entry = self.get_entry(key, allow_stale=allow_stale, now=now)
        return None if entry is None else entry.payload

    def _get_by_id(
        self,
        column: str,
        value: str,
        *,
        allow_stale: bool,
        now: datetime | None,
    ) -> CacheEntry | None:
        now_value = _utc_datetime(now or datetime.now(UTC), field_name="now")
        query = (
            f"SELECT * FROM traffic_cache WHERE {column} = ? "  # noqa: S608
            "ORDER BY updated_at DESC LIMIT 1"
        )
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(query, (value,)).fetchone()
        if row is None:
            return None
        entry = self._row_to_entry(row, now=now_value)
        if entry.is_stale and not allow_stale:
            return None
        return entry

    def get_by_observation_id(
        self,
        observation_id: str,
        *,
        allow_stale: bool = False,
        now: datetime | None = None,
    ) -> CacheEntry | None:
        return self._get_by_id(
            "observation_id",
            observation_id,
            allow_stale=allow_stale,
            now=now,
        )

    def get_by_matrix_id(
        self,
        matrix_id: str,
        *,
        allow_stale: bool = False,
        now: datetime | None = None,
    ) -> CacheEntry | None:
        return self._get_by_id(
            "matrix_id",
            matrix_id,
            allow_stale=allow_stale,
            now=now,
        )

    def delete_expired(self, *, now: datetime | None = None) -> int:
        """Delete expired entries and return the number removed."""

        now_text = _timestamp(now or datetime.now(UTC), field_name="now")
        with self._lock:
            self._ensure_open()
            with self._connection:
                cursor = self._connection.execute(
                    "DELETE FROM traffic_cache WHERE expires_at <= ?",
                    (now_text,),
                )
                return cursor.rowcount

    def stats(self, *, now: datetime | None = None) -> CacheStats:
        now_text = _timestamp(now or datetime.now(UTC), field_name="now")
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                """
                SELECT
                    COUNT(*) AS total_entries,
                    COALESCE(SUM(expires_at > ?), 0) AS fresh_entries,
                    COALESCE(SUM(expires_at <= ?), 0) AS stale_entries
                FROM traffic_cache
                """,
                (now_text, now_text),
            ).fetchone()
        return CacheStats(
            total_entries=int(row["total_entries"]),
            fresh_entries=int(row["fresh_entries"]),
            stale_entries=int(row["stale_entries"]),
        )

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def __enter__(self) -> Self:
        self._ensure_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

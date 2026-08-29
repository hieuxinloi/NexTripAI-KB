from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from nextrip_traffic.cache import SQLiteTrafficCache, make_cache_key


NOW = datetime(2026, 8, 19, 8, 0, tzinfo=UTC)


def test_cache_key_is_canonical_and_namespaced() -> None:
    first = make_cache_key("route", {"mode": "drive", "points": [1, 2]})
    second = make_cache_key("route", {"points": [1, 2], "mode": "drive"})

    assert first == second
    assert first.startswith("route:")
    assert first != make_cache_key("matrix", {"mode": "drive", "points": [1, 2]})


def test_get_hides_stale_entries_unless_requested(tmp_path) -> None:
    with SQLiteTrafficCache(tmp_path / "nested" / "traffic.sqlite3") as cache:
        cache.put(
            "route:one",
            {"duration_seconds": 100},
            expires_at=NOW + timedelta(minutes=10),
            now=NOW,
        )

        assert cache.get("route:one", now=NOW) == {"duration_seconds": 100}
        assert cache.get("route:one", now=NOW + timedelta(minutes=10)) is None
        stale = cache.get_entry(
            "route:one",
            allow_stale=True,
            now=NOW + timedelta(minutes=10),
        )

    assert stale is not None
    assert stale.is_stale is True
    assert stale.payload == {"duration_seconds": 100}


def test_put_overwrites_atomically_and_preserves_created_at(tmp_path) -> None:
    with SQLiteTrafficCache(tmp_path / "traffic.sqlite3") as cache:
        cache.put(
            "route:one",
            {"version": 1},
            expires_at=NOW + timedelta(minutes=5),
            now=NOW,
        )
        cache.put(
            "route:one",
            {"version": 2},
            expires_at=NOW + timedelta(minutes=20),
            now=NOW + timedelta(minutes=1),
        )
        entry = cache.get_entry("route:one", now=NOW + timedelta(minutes=2))

    assert entry is not None
    assert entry.payload == {"version": 2}
    assert entry.created_at == NOW
    assert entry.updated_at == NOW + timedelta(minutes=1)
    assert entry.expires_at == NOW + timedelta(minutes=20)


def test_cleanup_stats_and_id_lookup(tmp_path) -> None:
    with SQLiteTrafficCache(tmp_path / "traffic.sqlite3") as cache:
        cache.put(
            "route:old",
            {"observation_id": "obs-old"},
            expires_at=NOW,
            now=NOW - timedelta(minutes=1),
        )
        cache.put(
            "matrix:fresh",
            {"matrix_id": "matrix-fresh", "cells": []},
            expires_at=NOW + timedelta(hours=1),
            now=NOW,
        )

        assert cache.stats(now=NOW).as_dict() == {
            "total_entries": 2,
            "fresh_entries": 1,
            "stale_entries": 1,
        }
        assert cache.get_by_observation_id("obs-old", now=NOW) is None
        assert (
            cache.get_by_observation_id("obs-old", now=NOW, allow_stale=True)
            is not None
        )
        matrix = cache.get_by_matrix_id("matrix-fresh", now=NOW)
        assert matrix is not None
        assert matrix.payload["cells"] == []
        assert cache.delete_expired(now=NOW) == 1
        assert cache.stats(now=NOW).total_entries == 1


def test_cache_persists_across_instances(tmp_path) -> None:
    database = tmp_path / "traffic.sqlite3"
    with SQLiteTrafficCache(database) as cache:
        cache.put(
            "route:persistent",
            {"distance_meters": 42},
            expires_at=NOW + timedelta(hours=1),
            now=NOW,
        )

    with SQLiteTrafficCache(database) as reopened:
        assert reopened.get("route:persistent", now=NOW) == {
            "distance_meters": 42
        }


def test_one_instance_supports_concurrent_threads(tmp_path) -> None:
    with SQLiteTrafficCache(tmp_path / "traffic.sqlite3") as cache:

        def write_and_read(index: int) -> dict[str, int] | None:
            key = f"route:{index}"
            value = {"index": index}
            cache.put(
                key,
                value,
                expires_at=NOW + timedelta(hours=1),
                now=NOW,
            )
            return cache.get(key, now=NOW)

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(write_and_read, range(100)))

        assert results == [{"index": index} for index in range(100)]
        assert cache.stats(now=NOW).fresh_entries == 100


def test_rejects_naive_expiry_and_operations_after_close(tmp_path) -> None:
    cache = SQLiteTrafficCache(tmp_path / "traffic.sqlite3")
    with pytest.raises(ValueError, match="timezone-aware"):
        cache.put(
            "route:one",
            {},
            expires_at=datetime(2026, 8, 19, 8, 0),
        )

    cache.close()
    cache.close()
    with pytest.raises(RuntimeError, match="closed"):
        cache.get("route:one")

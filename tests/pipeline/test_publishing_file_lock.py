from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from nextrip_pipeline.publishing._file_lock import destination_file_lock


def _acquire(destination: Path) -> Path:
    with destination_file_lock(destination):
        return destination


def test_distinct_destination_locks_do_not_block_each_other(tmp_path) -> None:
    first = tmp_path / "hotel=one" / "current.json"
    second = tmp_path / "hotel=two" / "current.json"

    with destination_file_lock(first), ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_acquire, second)
        assert future.result(timeout=1) == second

    assert first.with_name(f".{first.name}.lock").exists()
    assert second.with_name(f".{second.name}.lock").exists()

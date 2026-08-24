from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).parents[2]


def _load_dag_module():
    dag_path = ROOT / "dags" / "nextrip_hotel_prices.py"
    spec = importlib.util.spec_from_file_location("nextrip_hotel_prices_dag", dag_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hotel_dag_module_is_safe_without_airflow_and_uses_mcp_cli() -> None:
    module = _load_dag_module()

    registry_command = module._registry_command()
    batch_command = module._batch_command()
    assert "build-trivago-registry" in registry_command
    assert "NEXTRIP_CANONICAL_DATASET:?" in registry_command
    assert '--canonical-dataset "$NEXTRIP_CANONICAL_DATASET"' in registry_command
    assert "--master-file" not in registry_command
    assert "travel_data_verified" not in registry_command
    assert "--override config/trivago-mapping.json" in registry_command
    assert "batch-trivago-availability" in batch_command
    assert "NEXTRIP_HOTEL_CHECK_IN_OFFSET_DAYS:-1" in batch_command
    assert "NEXTRIP_HOTEL_STAY_NIGHTS:-1" in batch_command
    assert "NEXTRIP_HOTEL_LOOKAHEAD_DAYS:-1" in batch_command
    assert "NEXTRIP_HOTEL_ADULTS:-2" in batch_command
    assert "NEXTRIP_HOTEL_ROOMS:-1" in batch_command
    assert "--include-identity-discovery" not in batch_command
    assert "playwright" not in batch_command.casefold()
    assert "neo4j" not in batch_command.casefold()


def test_trivago_source_and_five_hour_job_are_enabled_only_for_hotel() -> None:
    sources = json.loads((ROOT / "config" / "sources.json").read_text("utf-8"))[
        "sources"
    ]
    jobs = json.loads((ROOT / "config" / "jobs.json").read_text("utf-8"))["jobs"]

    source = next(item for item in sources if item["source_id"] == "trivago-mcp")
    job = next(item for item in jobs if item["job_id"] == "hotel-price-capture")
    google_source = next(
        item for item in sources if item["source_id"] == "google-maps-web"
    )

    assert source["enabled"] is True
    assert source["entity_types"] == ["hotel"]
    assert source["schedule_interval_minutes"] == 300
    assert job == {
        "job_id": "hotel-price-capture",
        "source_id": "trivago-mcp",
        "interval_minutes": 300,
        "enabled": True,
    }
    assert google_source["enabled"] is False
    assert all(
        item["enabled"] is False
        for item in jobs
        if item["job_id"] != "hotel-price-capture"
    )

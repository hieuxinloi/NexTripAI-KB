from pathlib import Path
from types import SimpleNamespace
from typing import Any

from nextrip_graphrag import observation_cli
from nextrip_graphrag.observation_publish_cli import build_parser
from nextrip_graphrag.versions.v8.observation_publisher import (
    V8HotelPriceCleanupGate,
)


def test_lightweight_observation_publish_cli_defaults() -> None:
    arguments = build_parser().parse_args(
        ["--canonical-dataset", "canonical.json"]
    )

    assert arguments.canonical_dataset == "canonical.json"
    assert arguments.hotel_price_root == "data/current/hotel_price"
    assert arguments.hotel_availability_root == "data/current/hotel_availability"
    assert arguments.menu_root == "data/current/menu"
    assert arguments.opening_approval_root == "data/approvals/opening_status"
    assert (
        arguments.hotel_batch_summary_root
        == "data/runs/trivago_availability_batch"
    )
    assert arguments.hotel_price_previous_days == 1
    assert arguments.apply is False


def test_airflow_cli_pins_one_cleanup_gate_for_plan_and_publish(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    gate = V8HotelPriceCleanupGate(reason="batch_incomplete")
    captured: dict[str, object] = {}
    plan = SimpleNamespace(plan_id="plan-1")
    manifest = SimpleNamespace(model_dump=lambda **_kwargs: {"status": "dry_run"})

    def fake_build(*_args: object, **kwargs: object) -> object:
        captured["plan_gate"] = kwargs["hotel_price_cleanup_gate"]
        return plan

    class FakePublisher:
        def __init__(self, _store: object, **kwargs: object) -> None:
            captured["publisher_gate"] = kwargs["hotel_price_cleanup_gate"]

        def publish(self, received_plan: object, **_kwargs: object) -> object:
            assert received_plan is plan
            return manifest

    monkeypatch.setattr(
        observation_cli,
        "load_latest_hotel_price_cleanup_gate",
        lambda _root: gate,
    )
    monkeypatch.setattr(observation_cli, "build_v8_observation_plan", fake_build)
    monkeypatch.setattr(
        observation_cli,
        "write_v8_observation_plan",
        lambda path, _plan: path,
    )
    monkeypatch.setattr(observation_cli, "V8ObservationPublisher", FakePublisher)
    args = observation_cli.build_parser().parse_args(
        [
            "--canonical-dataset",
            "canonical.json",
            "--output-root",
            str(tmp_path),
        ]
    )

    result = observation_cli.publish_observations(args)

    assert captured == {"plan_gate": gate, "publisher_gate": gate}
    assert result["result"] == {"status": "dry_run"}

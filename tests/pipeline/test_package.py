from nextrip_pipeline.cli import build_parser, main


def test_pipeline_health(capsys) -> None:
    exit_code = main(["health"])

    assert exit_code == 0
    assert "pipeline is ready" in capsys.readouterr().out


def test_validate_source_registry(capsys) -> None:
    exit_code = main(["validate-sources", "--config", "config/sources.json"])

    assert exit_code == 0
    assert "2 sources, 1 enabled" in capsys.readouterr().out


def test_hotel_cli_occupancy_defaults_can_be_overridden() -> None:
    base_arguments = [
        "crawl-hotel-prices",
        "--mapping",
        "config/trivago-mapping.json",
        "--hotel-name",
        "Fleur De Lys Hotel Quy Nhon",
        "--destination",
        "Quy Nhơn",
        "--check-in",
        "2026-08-20",
        "--check-out",
        "2026-08-21",
    ]

    defaults = build_parser().parse_args(base_arguments)
    overridden = build_parser().parse_args(
        [*base_arguments, "--adults", "4", "--rooms", "2"]
    )

    assert (defaults.adults, defaults.rooms) == (2, 1)
    assert (overridden.adults, overridden.rooms) == (4, 2)

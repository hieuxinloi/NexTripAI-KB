from nextrip_graphrag.observation_publish_cli import build_parser


def test_lightweight_observation_publish_cli_defaults() -> None:
    arguments = build_parser().parse_args(
        ["--canonical-dataset", "canonical.json"]
    )

    assert arguments.canonical_dataset == "canonical.json"
    assert arguments.hotel_price_root == "data/current/hotel_price"
    assert arguments.hotel_availability_root == "data/current/hotel_availability"
    assert arguments.menu_root == "data/current/menu"
    assert arguments.opening_approval_root == "data/approvals/opening_status"
    assert arguments.apply is False

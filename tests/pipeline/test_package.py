from nextrip_pipeline.cli import main


def test_pipeline_health(capsys) -> None:
    exit_code = main(["health"])

    assert exit_code == 0
    assert "pipeline is ready" in capsys.readouterr().out
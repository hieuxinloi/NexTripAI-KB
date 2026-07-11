from fastapi import FastAPI
from fastapi.testclient import TestClient

from nextrip_graphrag.logging import configure_logging, install_request_logging, safe_text


def test_request_logging_preserves_correlation_id() -> None:
    configure_logging(service="test", level="ERROR")
    app = FastAPI()
    install_request_logging(app)

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"status": "ok"}

    response = TestClient(app).get("/ping", headers={"X-Request-ID": "pipeline-123"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "pipeline-123"


def test_safe_text_compacts_and_limits_log_values() -> None:
    assert safe_text("hello\n  world") == "hello world"
    assert safe_text("123456", max_length=5) == "12..."

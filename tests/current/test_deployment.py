from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_runtime_image_contains_current_data_package() -> None:
    dockerfile = (ROOT / "deploy" / "traffic" / "Dockerfile").read_text("utf-8")

    assert "COPY nextrip_current ./nextrip_current" in dockerfile
    assert "/app/data/current/hotel_price" in dockerfile
    assert "/app/data/current/hotel_availability" in dockerfile
    assert "/app/data/current/trivago_mappings" in dockerfile


def test_compose_exposes_authenticated_current_data_facade() -> None:
    compose = (ROOT / "deploy" / "traffic" / "compose.yaml").read_text("utf-8")

    assert "  current-data-api:" in compose
    assert "nextrip_current.api:app" in compose
    assert 'CURRENT_DATA_API_KEY: "${CURRENT_DATA_API_KEY:?' in compose
    assert "CURRENT_DATA_TRAFFIC_API_URL: http://traffic-api:8010" in compose
    assert 'TRAFFIC_API_KEY: "${TRAFFIC_API_KEY:?' in compose
    assert 'source: "${NEXTRIP_DATA_ROOT:-../../data}"' in compose
    assert "target: /app/data" in compose
    assert "http://127.0.0.1:8020/ready" in compose
    assert "${CURRENT_DATA_BIND_ADDRESS:-127.0.0.1}" in compose
    assert "${CURRENT_DATA_PORT:-8020}:8020" in compose
    current_service = compose.split("  current-data-api:", 1)[1]
    assert '- "1"' in current_service
    assert "start_period: 60s" in current_service


def test_deployment_template_keeps_internal_credentials_separate() -> None:
    template = (
        ROOT / "deploy" / "traffic" / "traffic.env.example"
    ).read_text("utf-8")

    assert "CURRENT_DATA_API_KEY=replace-with-a-long-random-secret" in template
    assert "CURRENT_DATA_TRIVAGO_REFRESH_ENABLED=false" in template
    assert "CURRENT_DATA_PORT=8020" in template
    assert "TRAFFIC_API_KEY=replace-with-a-long-random-secret" in template


def test_compose_packages_v8_kb_api_as_a_hardened_background_service() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text("utf-8")
    compose = (ROOT / "deploy" / "traffic" / "compose.yaml").read_text("utf-8")

    assert "COPY nextrip_pipeline ./nextrip_pipeline" in dockerfile
    assert "  kb-api:" in compose
    kb_service = compose.split("  kb-api:", 1)[1].split("\nvolumes:", 1)[0]
    assert "nextrip_graphrag.api.app:app" in dockerfile
    assert "ACTIVE_KB_VERSION: v8" in kb_service
    assert "http://127.0.0.1:8011/ready?version=v8" in kb_service
    assert "${KB_API_BIND_ADDRESS:-127.0.0.1}" in kb_service
    assert "${KB_API_PORT:-8011}:8011" in kb_service
    assert "restart: unless-stopped" in kb_service
    assert "read_only: true" in kb_service

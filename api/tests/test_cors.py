"""CORS tests.

These deliberately use `/health` and a bare TestClient rather than the `client`
fixture: CORS is middleware, so it can be exercised without a database, and
these tests stay runnable when PostGIS is not up.
"""

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

CAPTURE_ORIGIN = "http://localhost:5173"

client = TestClient(app)


def test_allowed_origin_gets_cors_header():
    resp = client.get("/health", headers={"Origin": CAPTURE_ORIGIN})

    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == CAPTURE_ORIGIN


def test_preflight_for_trace_upload_is_allowed():
    """The exact preflight the capture PWA's fetch() triggers."""
    resp = client.options(
        "/traces",
        headers={
            "Origin": CAPTURE_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == CAPTURE_ORIGIN
    assert "POST" in resp.headers["access-control-allow-methods"]
    assert "content-type" in resp.headers["access-control-allow-headers"].lower()


def test_unknown_origin_gets_no_cors_header():
    resp = client.get("/health", headers={"Origin": "https://evil.example"})

    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


def test_cors_origins_parses_comma_separated_value():
    assert settings.cors_origin_list == [
        origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()
    ]
    assert CAPTURE_ORIGIN in settings.cors_origin_list

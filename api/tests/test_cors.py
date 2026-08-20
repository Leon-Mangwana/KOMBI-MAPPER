"""CORS tests.

These deliberately use `/health` and a bare TestClient rather than the `client`
fixture: CORS is middleware, so it can be exercised without a database, and
these tests stay runnable when PostGIS is not up.
"""

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

CAPTURE_ORIGIN = "http://localhost:5173"
WEB_ORIGIN = "http://localhost:5174"

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


def test_web_map_origin_is_allowed():
    """The public map reads /routes and /stops from its own origin."""
    resp = client.get("/health", headers={"Origin": WEB_ORIGIN})

    assert resp.headers["access-control-allow-origin"] == WEB_ORIGIN


def test_preflight_for_operator_rename_is_allowed():
    """The exact preflight operator mode's PATCH triggers.

    Both halves matter and each fails on its own: PATCH is not a simple method,
    and X-Admin-Token is not a CORS-safelisted header, so a response naming only
    one of them still leaves the browser refusing the request. Shipping without
    this test is how operator mode reached production renaming nothing — the
    upload preflight above passed throughout, because POST and Content-Type were
    already allowed for the capture PWA.
    """
    resp = client.options(
        "/routes/1",
        headers={
            "Origin": WEB_ORIGIN,
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "content-type,x-admin-token",
        },
    )

    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == WEB_ORIGIN
    assert "PATCH" in resp.headers["access-control-allow-methods"]

    allowed = resp.headers["access-control-allow-headers"].lower()
    assert "content-type" in allowed
    assert "x-admin-token" in allowed


def test_preflight_for_stop_rename_is_allowed():
    """Operator mode renames stops through a second endpoint, on the same terms."""
    resp = client.options(
        "/stops/1",
        headers={
            "Origin": WEB_ORIGIN,
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "content-type,x-admin-token",
        },
    )

    assert resp.status_code == 200
    assert "PATCH" in resp.headers["access-control-allow-methods"]


def test_preflight_does_not_allow_unused_write_methods():
    """Nothing in either front end deletes, so the preflight should refuse it."""
    resp = client.options(
        "/routes/1",
        headers={
            "Origin": WEB_ORIGIN,
            "Access-Control-Request-Method": "DELETE",
        },
    )

    assert resp.status_code == 400
    assert "DELETE" not in resp.headers.get("access-control-allow-methods", "")


def test_unknown_origin_gets_no_cors_header():
    resp = client.get("/health", headers={"Origin": "https://evil.example"})

    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


def test_cors_origins_parses_comma_separated_value():
    assert settings.cors_origin_list == [
        origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()
    ]
    assert CAPTURE_ORIGIN in settings.cors_origin_list
    assert WEB_ORIGIN in settings.cors_origin_list

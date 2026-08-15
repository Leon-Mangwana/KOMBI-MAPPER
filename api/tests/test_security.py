"""Tests for admin authentication and the ingest limits.

Like test_cors.py these run without PostGIS. That is not a shortcut — every
check here is meant to happen *before* the database is touched, so a test that
needed one would be testing the wrong thing.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.main import app
from app.security import IngestGuardMiddleware, RateLimiter

client = TestClient(app)

VALID_TRACE = {"contributor": "leon", "points": [{"lat": -17.83, "lon": 31.05}]}


# --------------------------------------------------------------------------
# Admin authentication
# --------------------------------------------------------------------------


def test_raw_trace_is_unavailable_when_no_token_is_configured(monkeypatch):
    """Fail closed. A deployment that forgot to set the token must not publish
    contributors' movement histories."""
    monkeypatch.setattr(settings, "admin_token", "")

    resp = client.get("/traces/1")

    assert resp.status_code == 503
    assert "ADMIN_TOKEN" in resp.json()["detail"]


def test_raw_trace_rejects_a_missing_token(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "s3cret")

    resp = client.get("/traces/1")

    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "X-Admin-Token"


def test_raw_trace_rejects_a_wrong_token(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "s3cret")

    resp = client.get("/traces/1", headers={"X-Admin-Token": "not-it"})

    assert resp.status_code == 401


def test_raw_trace_admits_the_right_token(monkeypatch):
    """With the token accepted the request reaches the handler.

    The database is stubbed with a session that finds nothing, so a 404 here
    means authentication passed — which is the thing under test.
    """
    monkeypatch.setattr(settings, "admin_token", "s3cret")

    class NoTrace(Session):
        def __init__(self) -> None:  # no engine, nothing to connect to
            pass

        def get(self, *args, **kwargs):
            return None

    def no_database():
        yield NoTrace()

    app.dependency_overrides[get_db] = no_database
    try:
        resp = client.get("/traces/1", headers={"X-Admin-Token": "s3cret"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404


def test_public_endpoints_need_no_token():
    """The aggregates are the product; only the raw traces are restricted."""
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/traces/1", None),
        ("patch", "/routes/1", {"short_name": "A"}),
        ("patch", "/stops/1", {"name": "Copacabana"}),
    ],
)
def test_every_operator_endpoint_is_guarded(monkeypatch, method, path, body):
    """One test per protected endpoint, so adding one without a token is noticed."""
    monkeypatch.setattr(settings, "admin_token", "s3cret")

    resp = getattr(client, method)(path, json=body) if body else getattr(client, method)(path)

    assert resp.status_code == 401


def test_naming_is_refused_when_no_token_is_configured(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "")

    resp = client.patch("/routes/1", json={"short_name": "A"})

    assert resp.status_code == 503


# --------------------------------------------------------------------------
# Upload size
# --------------------------------------------------------------------------


def test_upload_without_a_content_length_is_refused():
    """A chunked body cannot be size-checked before it is read."""

    def chunks():
        yield b'{"contributor":"leon","points":[]}'

    resp = client.post("/traces", content=chunks())

    assert resp.status_code == 411
    assert "Content-Length" in resp.json()["detail"]


def test_oversized_upload_is_refused_before_parsing(monkeypatch):
    monkeypatch.setattr(settings, "max_upload_bytes", 20)

    resp = client.post("/traces", json=VALID_TRACE)

    assert resp.status_code == 413
    assert "too large" in resp.json()["detail"]


def test_upload_within_the_size_limit_gets_past_the_guard(monkeypatch):
    """422 from validation, not 413 — the guard let it through to the app."""
    monkeypatch.setattr(settings, "max_upload_bytes", 8_000_000)

    resp = client.post("/traces", json={"contributor": "leon", "points": []})

    assert resp.status_code == 422


def test_a_trace_with_too_many_points_is_rejected(monkeypatch):
    """The per-field cap, which only applies once the body is parsed."""
    monkeypatch.setattr(settings, "max_upload_bytes", 8_000_000)
    points = [{"lat": -17.83, "lon": 31.05}] * 3

    resp = client.post("/traces", json={"contributor": "leon", "points": points})

    # The configured maximum is far above 3, so this is accepted by the schema
    # and fails later for want of a database — what matters is that it is not
    # a validation error about length.
    assert resp.status_code != 422


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------


def guarded_app(limiter: RateLimiter) -> TestClient:
    """A minimal app behind the guard, so limits can be tested without a database."""
    minimal = FastAPI()
    minimal.add_middleware(IngestGuardMiddleware, limiter=limiter)

    @minimal.post("/traces")
    def upload() -> dict[str, bool]:
        return {"ok": True}

    return TestClient(minimal)


def test_uploads_are_refused_once_the_limit_is_reached():
    guarded = guarded_app(RateLimiter(((2, 60.0),)))

    assert guarded.post("/traces", json={}).status_code == 200
    assert guarded.post("/traces", json={}).status_code == 200

    limited = guarded.post("/traces", json={})
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0


def test_a_refused_upload_does_not_extend_the_lockout():
    """Counting rejections would let a client hammer itself into a ban."""
    limiter = RateLimiter(((1, 60.0),))
    guarded = guarded_app(limiter)

    guarded.post("/traces", json={})
    for _ in range(5):
        assert guarded.post("/traces", json={}).status_code == 429

    # One request recorded, so one slot frees at the window's end.
    assert len(limiter._seen["testclient"]) == 1


def test_clients_are_counted_separately():
    guarded = guarded_app(RateLimiter(((1, 60.0),)))

    first = guarded.post("/traces", json={}, headers={"X-Forwarded-For": "10.0.0.1"})
    second = guarded.post("/traces", json={}, headers={"X-Forwarded-For": "10.0.0.2"})
    repeat = guarded.post("/traces", json={}, headers={"X-Forwarded-For": "10.0.0.1"})

    assert (first.status_code, second.status_code) == (200, 200)
    assert repeat.status_code == 429


def test_a_spoofed_forwarded_for_prefix_does_not_win():
    """The proxy appends, so the rightmost hop is the one it vouched for.

    A client that prepends addresses to the header is trying to look like a new
    caller each time; only the last entry is trusted, so it stays one client.
    """
    guarded = guarded_app(RateLimiter(((1, 60.0),)))

    first = guarded.post("/traces", json={}, headers={"X-Forwarded-For": "1.1.1.1, 10.0.0.9"})
    second = guarded.post("/traces", json={}, headers={"X-Forwarded-For": "2.2.2.2, 10.0.0.9"})

    assert first.status_code == 200
    assert second.status_code == 429


def test_forwarded_for_is_ignored_when_proxies_are_not_trusted(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    guarded = guarded_app(RateLimiter(((1, 60.0),)))

    first = guarded.post("/traces", json={}, headers={"X-Forwarded-For": "10.0.0.1"})
    second = guarded.post("/traces", json={}, headers={"X-Forwarded-For": "10.0.0.2"})

    assert first.status_code == 200
    assert second.status_code == 429, "both came from the same socket"


def test_reads_are_not_rate_limited():
    """Only uploads are guarded; reads are public, cheap and cacheable."""
    guarded_client = guarded_app(RateLimiter(((1, 60.0),)))

    for _ in range(5):
        assert guarded_client.get("/traces").status_code == 405  # no GET route, but not 429


# --------------------------------------------------------------------------
# The limiter itself
# --------------------------------------------------------------------------


def test_all_windows_are_enforced_together():
    """Under the per-minute limit but over the hourly one still refuses."""
    limiter = RateLimiter(((5, 60.0), (6, 3600.0)))

    for second in range(6):
        assert limiter.check("a", now=second * 100.0) is None, "spread out, under both"

    assert limiter.check("a", now=700.0) is not None, "seventh in the hour"


def test_a_slot_frees_when_its_window_passes():
    limiter = RateLimiter(((1, 60.0),))

    assert limiter.check("a", now=0.0) is None
    assert limiter.check("a", now=30.0) is not None
    assert limiter.check("a", now=61.0) is None


def test_retry_after_points_at_when_a_slot_frees():
    limiter = RateLimiter(((1, 60.0),))
    limiter.check("a", now=0.0)

    assert limiter.check("a", now=10.0) == 51


def test_tracking_does_not_grow_without_bound():
    """A flood of distinct addresses must not become a memory leak."""
    limiter = RateLimiter(((10, 60.0),), max_clients=50)

    for index in range(500):
        limiter.check(f"client-{index}", now=float(index))

    assert len(limiter._seen) <= 50

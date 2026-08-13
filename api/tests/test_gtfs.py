"""Tests for serving the GTFS feed.

These need no database, and deliberately do not use the `client` fixture: the
endpoint reads a file and nothing else, and a test that needed PostGIS to prove
that would be testing the fixture. `settings.gtfs_path` is redirected at a
temporary file instead — the same object `app.routers.gtfs` reads.
"""

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture()
def feedless_client(tmp_path, monkeypatch):
    """A client whose configured feed path does not exist."""
    monkeypatch.setattr(settings, "gtfs_path", tmp_path / "gtfs.zip")
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def feed_client(tmp_path, monkeypatch):
    """A client serving a small but real zip archive."""
    path = tmp_path / "gtfs.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("agency.txt", "agency_id,agency_name\nkombi,Kombi Route Mapper\n")
    path.write_bytes(buffer.getvalue())

    monkeypatch.setattr(settings, "gtfs_path", path)
    with TestClient(app) as client:
        yield client


def test_serves_the_exported_feed_as_a_zip(feed_client):
    response = feed_client.get("/gtfs.zip")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == ["agency.txt"]


def test_the_response_can_be_cached_and_downloaded(feed_client):
    response = feed_client.get("/gtfs.zip")

    # A GTFS consumer polls the same URL on a schedule; these are what let it
    # get a 304 rather than the whole feed again.
    assert response.headers["etag"]
    assert response.headers["last-modified"]
    assert "gtfs.zip" in response.headers["content-disposition"]


def test_an_unchanged_feed_is_not_sent_twice(feed_client):
    first = feed_client.get("/gtfs.zip")

    second = feed_client.get("/gtfs.zip", headers={"If-None-Match": first.headers["etag"]})

    assert second.status_code == 304
    assert not second.content


def test_404_until_the_pipeline_has_exported_one(feedless_client):
    response = feedless_client.get("/gtfs.zip")

    assert response.status_code == 404
    # The fix is to run an export, so the message has to say so.
    assert "gtfs.py" in response.json()["detail"]

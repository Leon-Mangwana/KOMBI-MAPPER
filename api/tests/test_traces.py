import pytest

# The `admin` fixture lives in conftest.py — `GET /traces/{id}` is
# operator-only, since it returns a contributor's raw timestamped ride.

# A short trace through central Harare.
SAMPLE_POINTS = [
    {
        "lat": -17.8292,
        "lon": 31.0522,
        "recorded_at": "2026-07-24T08:00:00Z",
        "accuracy_m": 5.0,
        "speed_mps": 8.3,
    },
    {
        "lat": -17.8300,
        "lon": 31.0530,
        "recorded_at": "2026-07-24T08:00:05Z",
        "accuracy_m": 4.0,
        "speed_mps": 9.1,
    },
    {
        "lat": -17.8310,
        "lon": 31.0541,
        "recorded_at": "2026-07-24T08:00:10Z",
    },
]


def test_post_trace_creates_trace_and_points(client):
    resp = client.post("/traces", json={"contributor": "leon", "points": SAMPLE_POINTS})

    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] > 0
    assert body["contributor"] == "leon"
    assert body["status"] == "uploaded"
    assert body["point_count"] == 3


def test_get_trace_returns_geojson_feature(client, admin):
    trace_id = client.post("/traces", json={"contributor": "leon", "points": SAMPLE_POINTS}).json()[
        "id"
    ]

    resp = client.get(f"/traces/{trace_id}", headers=admin)

    assert resp.status_code == 200
    feature = resp.json()

    assert feature["type"] == "Feature"
    assert feature["geometry"]["type"] == "LineString"

    coords = feature["geometry"]["coordinates"]
    assert len(coords) == 3
    # GeoJSON is [lon, lat] order, in EPSG:4326.
    assert coords[0][0] == pytest.approx(31.0522)
    assert coords[0][1] == pytest.approx(-17.8292)

    props = feature["properties"]
    assert props["id"] == trace_id
    assert props["contributor"] == "leon"
    assert props["status"] == "uploaded"
    assert props["route_id"] is None

    # Points are ordered by seq and carry their attributes.
    points = props["points"]
    assert [p["seq"] for p in points] == [0, 1, 2]
    assert points[0]["accuracy_m"] == pytest.approx(5.0)
    assert points[0]["speed_mps"] == pytest.approx(8.3)
    assert points[2]["accuracy_m"] is None


def test_get_missing_trace_returns_404(client, admin):
    resp = client.get("/traces/999999", headers=admin)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Trace not found."


def test_post_trace_rejects_empty_points(client):
    resp = client.post("/traces", json={"contributor": "leon", "points": []})
    assert resp.status_code == 422


def test_post_trace_is_atomic_on_bad_point(client):
    # lat out of range -> Pydantic 422, nothing should be written.
    bad_points = [
        {"lat": -17.83, "lon": 31.05},
        {"lat": 999.0, "lon": 31.05},
    ]
    resp = client.post("/traces", json={"contributor": "leon", "points": bad_points})
    assert resp.status_code == 422

    # The next inserted trace still starts fresh (id 1) thanks to the rollback
    # and the per-test truncate.
    ok = client.post("/traces", json={"contributor": "leon", "points": SAMPLE_POINTS})
    assert ok.status_code == 201

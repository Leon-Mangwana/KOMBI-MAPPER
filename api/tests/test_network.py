"""Tests for the routes and stops endpoints.

Like the rest of the API suite these need a real PostGIS — `ST_AsGeoJSON`,
`ST_Transform` and the geometry types have no SQLite equivalent. See conftest.py.

The rows are inserted directly rather than by running the pipeline: what is
under test is the serving, and clustering has its own suite in ../../pipeline.
"""

import pytest
from sqlalchemy import text

# Two short lines through central Harare, sharing a stop at the CBD end.
ROUTE_A = "LINESTRING(31.0522 -17.8292, 31.0530 -17.8300, 31.0541 -17.8310)"
ROUTE_B = "LINESTRING(31.0522 -17.8292, 31.0600 -17.8350)"

STOP_CBD = "POINT(31.0522 -17.8292)"
STOP_MBARE = "POINT(31.0541 -17.8310)"
STOP_HIGHFIELD = "POINT(31.0600 -17.8350)"


@pytest.fixture()
def network(engine):
    """Two routes, three stops, and a trace assigned to the first route."""
    with engine.begin() as conn:
        route_a = conn.execute(
            text(
                "INSERT INTO routes (short_name, long_name, geom) "
                "VALUES ('A', 'CBD - Mbare', ST_GeomFromText(:wkt, 4326)) RETURNING id"
            ),
            {"wkt": ROUTE_A},
        ).scalar_one()
        route_b = conn.execute(
            text("INSERT INTO routes (geom) VALUES (ST_GeomFromText(:wkt, 4326)) RETURNING id"),
            {"wkt": ROUTE_B},
        ).scalar_one()

        stop_ids = {}
        for key, name, wkt in [
            ("cbd", "Copacabana", STOP_CBD),
            ("mbare", None, STOP_MBARE),
            ("highfield", None, STOP_HIGHFIELD),
        ]:
            stop_ids[key] = conn.execute(
                text(
                    "INSERT INTO stops (name, geom) "
                    "VALUES (:name, ST_GeomFromText(:wkt, 4326)) RETURNING id"
                ),
                {"name": name, "wkt": wkt},
            ).scalar_one()

        conn.execute(
            text("INSERT INTO route_stops (route_id, stop_id, seq) VALUES (:r, :s, :seq)"),
            [
                {"r": route_a, "s": stop_ids["cbd"], "seq": 0},
                {"r": route_a, "s": stop_ids["mbare"], "seq": 1},
                {"r": route_b, "s": stop_ids["cbd"], "seq": 0},
                {"r": route_b, "s": stop_ids["highfield"], "seq": 1},
            ],
        )

        conn.execute(
            text(
                "INSERT INTO traces (contributor, status, route_id) VALUES ('leon', 'matched', :r)"
            ),
            {"r": route_a},
        )

    return {"route_a": route_a, "route_b": route_b, "stops": stop_ids}


# --------------------------------------------------------------------------
# /routes
# --------------------------------------------------------------------------


def test_routes_are_a_feature_collection(client, network):
    resp = client.get("/routes")

    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "FeatureCollection"
    assert len(body["features"]) == 2

    feature = body["features"][0]
    assert feature["type"] == "Feature"
    assert feature["geometry"]["type"] == "LineString"
    # GeoJSON is [lon, lat] in EPSG:4326.
    assert feature["geometry"]["coordinates"][0] == pytest.approx([31.0522, -17.8292])


def test_route_properties_carry_names_and_counts(client, network):
    features = client.get("/routes").json()["features"]
    by_id = {f["properties"]["id"]: f["properties"] for f in features}

    named = by_id[network["route_a"]]
    assert named["short_name"] == "A"
    assert named["long_name"] == "CBD - Mbare"
    assert named["stop_count"] == 2
    assert named["trace_count"] == 1

    # Clustering cannot name a route, so unnamed is the normal state.
    unnamed = by_id[network["route_b"]]
    assert unnamed["short_name"] is None
    assert unnamed["trace_count"] == 0


def test_route_length_is_metres_not_degrees(client, network):
    """The line spans ~0.002 degrees; in metres it is a couple of hundred."""
    features = client.get("/routes").json()["features"]
    length_m = {f["properties"]["id"]: f["properties"]["length_m"] for f in features}

    assert 200 < length_m[network["route_a"]] < 400
    assert 900 < length_m[network["route_b"]] < 1200


def test_routes_can_be_fetched_without_geometry(client, network):
    body = client.get("/routes", params={"include_geometry": "false"}).json()

    assert [f["geometry"] for f in body["features"]] == [None, None]
    assert body["features"][0]["properties"]["stop_count"] == 2


def test_empty_network_is_an_empty_collection(client):
    body = client.get("/routes").json()

    assert body == {"type": "FeatureCollection", "features": []}


# --------------------------------------------------------------------------
# /routes/{id}
# --------------------------------------------------------------------------


def test_single_route_lists_its_stops_in_order(client, network):
    resp = client.get(f"/routes/{network['route_a']}")

    assert resp.status_code == 200
    feature = resp.json()
    assert feature["type"] == "Feature"

    stops = feature["properties"]["stops"]
    assert [s["seq"] for s in stops] == [0, 1]
    assert stops[0]["id"] == network["stops"]["cbd"]
    assert stops[0]["name"] == "Copacabana"
    assert stops[0]["lon"] == pytest.approx(31.0522)
    assert stops[1]["name"] is None


def test_missing_route_is_a_404(client, network):
    resp = client.get("/routes/999999")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Route not found."


def test_route_list_does_not_carry_stops(client, network):
    """The list feeds a map layer; per-route stop lists would bloat every response."""
    feature = client.get("/routes").json()["features"][0]

    assert "stops" not in feature["properties"]


# --------------------------------------------------------------------------
# /stops
# --------------------------------------------------------------------------


def test_stops_are_a_feature_collection(client, network):
    resp = client.get("/stops")

    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "FeatureCollection"
    assert len(body["features"]) == 3

    first = body["features"][0]
    assert first["geometry"] == {"type": "Point", "coordinates": [31.0522, -17.8292]}
    assert first["properties"]["name"] == "Copacabana"


def test_a_stop_names_every_route_that_serves_it(client, network):
    features = client.get("/stops").json()["features"]
    serving = {f["properties"]["id"]: f["properties"]["route_ids"] for f in features}

    assert sorted(serving[network["stops"]["cbd"]]) == sorted(
        [network["route_a"], network["route_b"]]
    )
    assert serving[network["stops"]["mbare"]] == [network["route_a"]]


def test_stop_with_no_routes_reports_an_empty_list(client, engine):
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO stops (geom) VALUES (ST_GeomFromText(:wkt, 4326))"),
            {"wkt": STOP_CBD},
        )

    features = client.get("/stops").json()["features"]

    assert features[0]["properties"]["route_ids"] == []

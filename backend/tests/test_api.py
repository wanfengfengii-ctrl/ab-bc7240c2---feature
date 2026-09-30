"""API smoke tests: health, input errors vs infeasibility, success shape."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _exposure(id, **kw):
    base = dict(
        id=id, duration=2, earliest_start=0, latest_start=100,
        equipment="X", cooling=0,
    )
    base.update(kw)
    return base


def _body(exposures=None, links=None, horizon=1000):
    return {
        "horizon": horizon,
        "exposures": exposures if exposures is not None else [
            _exposure("A"), _exposure("B", equipment="Y"),
            _exposure("C", equipment="Z"), _exposure("D", equipment="W"),
            _exposure("F", equipment="V"),
        ],
        "links": links or [],
    }


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_feasible_response_shape():
    r = client.post("/api/schedule", json=_body())
    assert r.status_code == 200
    data = r.json()
    assert data["feasible"] is True
    assert len(data["starts"]) == 5
    assert len(data["finishes"]) == 5
    assert data["makespan"] is not None
    assert isinstance(data["equipment_orders"], list)


def test_infeasible_is_200_without_partial_solution():
    body = _body(exposures=[
        _exposure("A", duration=5, latest_start=4),
        _exposure("B", duration=5, latest_start=4),
        _exposure("C", equipment="Y"), _exposure("D", equipment="Z"),
        _exposure("F", equipment="W"),
    ])
    r = client.post("/api/schedule", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["feasible"] is False
    assert data["reason"] == "no_schedule"
    assert data["starts"] is None
    assert data["finishes"] is None


def test_input_error_is_400():
    body = _body(exposures=[
        _exposure("A", earliest_start=40, latest_start=10),
        _exposure("B"), _exposure("C", equipment="Y"),
        _exposure("D", equipment="Z"), _exposure("F", equipment="W"),
    ])
    r = client.post("/api/schedule", json=body)
    assert r.status_code == 400
    data = r.json()
    assert data["feasible"] is False
    assert data["reason"] == "input_error"
    assert data["field_errors"]


def test_unknown_link_reference_is_400():
    body = _body(links=[{"from_id": "A", "to_id": "ZZ", "min_gap": 0}])
    r = client.post("/api/schedule", json=body)
    assert r.status_code == 400
    assert "unknown exposure" in " ".join(r.json()["field_errors"])


def test_schema_rejects_wrong_count():
    body = _body()
    body["exposures"] = body["exposures"][:3]
    r = client.post("/api/schedule", json=body)
    assert r.status_code == 422


def test_schema_rejects_negative_duration():
    body = _body(exposures=[
        _exposure("A", duration=-1), _exposure("B"),
        _exposure("C", equipment="Y"), _exposure("D", equipment="Z"),
        _exposure("F", equipment="W"),
    ])
    r = client.post("/api/schedule", json=body)
    assert r.status_code == 422


def test_openapi_available():
    r = client.get("/openapi.json")
    assert r.status_code == 200
    assert "/api/schedule" in r.json()["paths"]


def _mode_body():
    body = _body(exposures=[
        _exposure("A", equipment="X", duration=2, cooling=4),
        _exposure("B", equipment="X", duration=2),
        _exposure("C", equipment="Y"), _exposure("D", equipment="Y"),
        _exposure("F", equipment="Z"),
    ])
    body["exposures"][0]["mode"] = "T"
    body["exposures"][1]["mode"] = "F"
    body["modes"] = {"X": {
        "initial_mode": "T",
        "transitions": [
            {"from_mode": "T", "to_mode": "F", "duration": 1},
            {"from_mode": "F", "to_mode": "T", "duration": 9},
        ],
    }}
    body["links"] = [{"from_id": "A", "to_id": "B", "min_gap": 0}]
    return body


def test_mode_enabled_schedule_shape_and_segments():
    r = client.post("/api/schedule", json=_mode_body())
    assert r.status_code == 200
    data = r.json()
    assert data["feasible"] is True
    assert data["starts"] == [0, 7, 0, 2, 0]
    assert len(data["calibrations"]) == 1
    seg = data["calibrations"][0]
    assert seg["equipment"] == "X"
    assert (seg["start"], seg["finish"], seg["duration"]) == (6, 7, 1)
    assert (seg["from_mode"], seg["to_mode"]) == ("T", "F")
    assert (seg["predecessor_id"], seg["successor_id"]) == ("A", "B")
    assert seg["wait_before"] == 0
    assert seg["margin"] == 0


def test_missing_transition_is_infeasible_not_zero_cost():
    body = _mode_body()
    # Remove the only T->F transition B needs; distinct-mode pair absent.
    body["modes"]["X"]["transitions"] = [
        {"from_mode": "F", "to_mode": "T", "duration": 9},
    ]
    r = client.post("/api/schedule", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["feasible"] is False
    assert data["reason"] == "no_schedule"
    assert data["starts"] is None
    assert data["calibrations"] is None


def test_bad_mode_reference_is_input_error():
    body = _mode_body()
    body["exposures"][0]["mode"] = "Q"
    r = client.post("/api/schedule", json=body)
    assert r.status_code == 400
    data = r.json()
    assert data["reason"] == "input_error"
    assert "unknown mode 'Q'" in " ".join(data["field_errors"])

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


def _modes_body():
    return {
        "horizon": 500,
        "exposures": [
            _exposure("A", equipment="X", mode="a"),
            _exposure("B", equipment="X", mode="b"),
            _exposure("C", equipment="Y", mode="a"),
            _exposure("D", equipment="Y", mode="a"),
            _exposure("F", equipment="Z", mode="a"),
        ],
        "links": [],
        "readout_modes": [
            {"equipment": "X", "initial_mode": "a", "transitions": [
                {"from_mode": "a", "to_mode": "b", "duration": 2},
                {"from_mode": "b", "to_mode": "a", "duration": 6},
            ]},
            {"equipment": "Y", "initial_mode": "a", "transitions": []},
            {"equipment": "Z", "initial_mode": "a", "transitions": []},
        ],
    }


def test_readout_modes_feasible_shape():
    r = client.post("/api/schedule", json=_modes_body())
    assert r.status_code == 200
    data = r.json()
    assert data["feasible"] is True
    assert isinstance(data["calibrations"], list)
    assert data["calibrations"]  # the a->b switch on X must appear
    cal = data["calibrations"][0]
    assert cal["equipment"] == "X"
    assert cal["to_mode"] == "b"
    assert cal["switch_duration"] == 2
    assert cal["cal_end"] - cal["cal_start"] == 2
    assert cal["wait_margin"] >= 0
    orders = {o["equipment"]: o for o in data["equipment_orders"]}
    assert orders["X"]["initial_mode"] == "a"
    assert dict(zip(orders["X"]["sequence"], orders["X"]["modes"])) == {
        "A": "a", "B": "b",
    }


def test_unregistered_transition_is_infeasible_not_zero_cost():
    body = _modes_body()
    # Force B(b) before A(a) while b->a is unregistered.
    body["links"] = [{"from_id": "B", "to_id": "A", "min_gap": 0}]
    body["readout_modes"][0]["transitions"] = [
        {"from_mode": "a", "to_mode": "b", "duration": 2}
    ]
    r = client.post("/api/schedule", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["feasible"] is False
    assert data["reason"] == "no_schedule"
    assert data["starts"] is None
    assert data["calibrations"] is None


def test_bad_mode_reference_is_400_input_error():
    body = _modes_body()
    body["exposures"][1]["mode"] = "turbo"
    r = client.post("/api/schedule", json=body)
    assert r.status_code == 400
    data = r.json()
    assert data["reason"] == "input_error"
    assert any("turbo" in m and "not registered" in m
               for m in data["field_errors"])


def test_conflicting_transition_table_is_400():
    body = _modes_body()
    body["readout_modes"][0]["transitions"].append(
        {"from_mode": "a", "to_mode": "b", "duration": 9})
    r = client.post("/api/schedule", json=body)
    assert r.status_code == 400
    assert any("conflicting duplicate transition" in m
               for m in r.json()["field_errors"])


def test_classic_request_has_no_calibration_payload():
    r = client.post("/api/schedule", json=_body())
    assert r.status_code == 200
    data = r.json()
    assert data["feasible"] is True
    assert "calibrations" not in data
    assert "initial_mode" not in data["equipment_orders"][0]

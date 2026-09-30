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

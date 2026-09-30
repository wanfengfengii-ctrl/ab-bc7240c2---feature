#!/usr/bin/env python3
"""One-shot verification for the beamline scheduler stack.

Aggregated exit code is a bitmask (0 = everything passed):
    bit 0 (1)  code tests (pytest)
    bit 1 (2)  build / static integrity checks
    bit 2 (4)  feasible schedule produced by the solver
    bit 3 (8)  infeasible-input API smoke (200 + feasible=false, no partial)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback

import httpx

API_URL = os.environ.get("API_URL", "http://api:8000").rstrip("/")
WEB_URL = os.environ.get("WEB_URL", "http://web").rstrip("/")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

results: dict[str, bool] = {}


def section(name):
    def deco(fn):
        def wrapped():
            print(f"\n=== {name} ===", flush=True)
            try:
                fn()
                results[name] = True
                print(f"[PASS] {name}", flush=True)
            except Exception as exc:  # noqa: BLE001
                results[name] = False
                print(f"[FAIL] {name}: {exc}", flush=True)
                traceback.print_exc()
        return wrapped
    return deco


@section("code tests (pytest)")
def check_pytest():
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q"],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    print(proc.stdout[-2000:])
    print(proc.stderr[-1000:])
    if proc.returncode != 0:
        raise RuntimeError(f"pytest exited {proc.returncode}")


@section("build / static integrity")
def check_build():
    proc = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", "app", "scripts"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or "compileall failed")
    import app.main  # full import graph incl. ortools must load

    static_dir = os.environ.get("WEB_STATIC_DIR")
    if static_dir:
        for name in ("index.html", "app.js", "styles.css"):
            path = os.path.join(static_dir, name)
            if not os.path.isfile(path) or os.path.getsize(path) == 0:
                raise RuntimeError(f"missing or empty static asset: {path}")


def _five(exposures):
    return {
        "horizon": 1000,
        "exposures": [
            {
                "id": eid, "duration": dur, "earliest_start": es,
                "latest_start": ls, "equipment": eq, "cooling": cool,
            }
            for (eid, dur, es, ls, eq, cool) in exposures
        ],
        "links": [],
    }


@section("feasible schedule")
def check_feasible():
    from app.scheduler import solve
    from app.schemas import ScheduleRequest

    body = _five([
        ("A", 4, 0, 50, "X", 2),
        ("B", 3, 0, 50, "X", 1),
        ("C", 5, 0, 50, "Y", 0),
        ("D", 2, 0, 50, "Y", 3),
        ("E", 6, 2, 40, "Z", 0),
    ])
    body["links"] = [
        {"from_id": "A", "to_id": "B", "min_gap": 0, "max_gap": 20},
    ]
    r = solve(ScheduleRequest(**body), phase_seconds=5)
    if not r.get("feasible"):
        raise RuntimeError(f"expected feasible, got {r}")
    starts, finishes = r["starts"], r["finishes"]
    if len(starts) != 5 or len(finishes) != 5:
        raise RuntimeError("incomplete solution returned")

    # Re-verify every constraint independently of the solver's own claims.
    exps = {e["id"]: e for e in body["exposures"]}
    ids = [e["id"] for e in body["exposures"]]
    for sid, eid in zip(starts, ids):
        e = exps[eid]
        if not (e["earliest_start"] <= sid <= e["latest_start"]):
            raise RuntimeError(f"{eid} start {sid} outside window")
    for eq in {e["equipment"] for e in body["exposures"]}:
        on_eq = sorted(
            (starts[i], starts[i] + exps[eid]["duration"] + exps[eid]["cooling"], eid)
            for i, eid in enumerate(ids) if exps[eid]["equipment"] == eq
        )
        for (s1, occ1, a), (s2, _, b) in zip(on_eq, on_eq[1:]):
            if s2 < occ1:
                raise RuntimeError(f"equipment {eq}: {b} starts {s2} before {a} released {occ1}")
    a, b = ids.index("A"), ids.index("B")
    gap = starts[b] - finishes[a]
    if not (0 <= gap <= 20):
        raise RuntimeError(f"link gap {gap} outside [0,20]")
    print(f"  classic starts={starts} makespan={r['makespan']} sum={r['sum_starts']}")

    # ---- readout-mode scenario: first switch, asymmetric direction, cooling
    # occupation and wait margins are all jointly solved and re-checked here.
    mode_body = {
        "horizon": 500,
        "exposures": [
            {"id": "A", "duration": 2, "earliest_start": 0, "latest_start": 100,
             "equipment": "X", "cooling": 0, "mode": "b"},
            {"id": "B", "duration": 2, "earliest_start": 0, "latest_start": 100,
             "equipment": "X", "cooling": 4, "mode": "a"},
            {"id": "C", "duration": 2, "earliest_start": 0, "latest_start": 100,
             "equipment": "X", "cooling": 0, "mode": "b"},
            {"id": "D", "duration": 1, "earliest_start": 0, "latest_start": 100,
             "equipment": "Y", "cooling": 0, "mode": "a"},
            {"id": "E", "duration": 1, "earliest_start": 0, "latest_start": 100,
             "equipment": "Z", "cooling": 0, "mode": "a"},
        ],
        "links": [
            {"from_id": "A", "to_id": "B", "min_gap": 0},
            {"from_id": "B", "to_id": "C", "min_gap": 0},
        ],
        "readout_modes": [
            {"equipment": "X", "initial_mode": "a", "transitions": [
                {"from_mode": "a", "to_mode": "b", "duration": 3},
                {"from_mode": "b", "to_mode": "a", "duration": 5},
            ]},
            {"equipment": "Y", "initial_mode": "a", "transitions": []},
            {"equipment": "Z", "initial_mode": "a", "transitions": []},
        ],
    }
    mr = solve(ScheduleRequest(**mode_body), phase_seconds=5)
    if not mr.get("feasible"):
        raise RuntimeError(f"mode scenario expected feasible, got {mr}")
    ms = dict(zip(["A", "B", "C", "D", "E"], mr["starts"]))
    mf = dict(zip(["A", "B", "C", "D", "E"], mr["finishes"]))
    cals = {(c["prev_exposure_id"], c["exposure_id"]): c for c in mr["calibrations"]}

    def expect_cal(prev, nxt, frm, to, dur, cal_start, wait):
        c = cals.get((prev, nxt))
        if c is None:
            raise RuntimeError(f"missing calibration {prev}->{nxt}")
        if (c["from_mode"], c["to_mode"], c["switch_duration"]) != (frm, to, dur):
            raise RuntimeError(f"calibration {prev}->{nxt} wrong directed switch: {c}")
        if (c["cal_start"], c["cal_end"]) != (cal_start, cal_start + dur):
            raise RuntimeError(f"calibration {prev}->{nxt} timing wrong: {c}")
        if c["cal_end"] > ms[nxt] or c["wait_margin"] != ms[nxt] - c["cal_end"]:
            raise RuntimeError(f"calibration {prev}->{nxt} margin wrong: {c}")
        if c["wait_margin"] != wait:
            raise RuntimeError(f"calibration {prev}->{nxt}: wait {c['wait_margin']} != {wait}")

    # Chain fixed by links: A first (switch initial a->b = 3), B second
    # (reverse direction b->a = 5), C third (a->b = 3 after B's cooling 4).
    if ms["A"] != 3:
        raise RuntimeError(f"first switch not paid before first exposure: {ms}")
    expect_cal(None, "A", "a", "b", 3, 0, 0)
    expect_cal("A", "B", "b", "a", 5, mf["A"], ms["B"] - mf["A"] - 5)
    # B cools until finish+4; C's calibration must start exactly there.
    b_release = mf["B"] + 4
    expect_cal("B", "C", "a", "b", 3, b_release, 0)
    if ms["C"] != b_release + 3:
        raise RuntimeError("calibration did not continuously occupy through cooling")

    # Direction asymmetry: reverse direction must carry its own duration.
    if cals[("A", "B")]["switch_duration"] == cals[("B", "C")]["switch_duration"]:
        raise RuntimeError("directed switch durations collapsed to one value")

    # Independent overlap check on equipment X across exposures+cooling+cals.
    segments = []
    for eid in ("A", "B", "C"):
        e = next(x for x in mode_body["exposures"] if x["id"] == eid)
        segments.append((ms[eid], ms[eid] + e["duration"] + e["cooling"], f"{eid} occ"))
    for c in mr["calibrations"]:
        if c["equipment"] == "X":
            segments.append((c["cal_start"], c["cal_end"], f"cal->{c['exposure_id']}"))
    segments.sort()
    for (s1, t1, n1), (s2, t2, n2) in zip(segments, segments[1:]):
        if s2 < t1:
            raise RuntimeError(f"equipment X overlap: {n1}[{s1},{t1}) vs {n2}[{s2},{t2})")

    orders = {o["equipment"]: o for o in mr["equipment_orders"]}
    if orders["X"]["sequence"] != ["A", "B", "C"]:
        raise RuntimeError(f"equipment order wrong: {orders['X']}")
    if orders["X"]["modes"] != ["b", "a", "b"] or orders["X"]["initial_mode"] != "a":
        raise RuntimeError(f"order modes wrong: {orders['X']}")
    print(f"  mode  starts={mr['starts']} calibrations={len(mr['calibrations'])}")


def check_api_modes():
    with httpx.Client(base_url=API_URL, timeout=60) as cli:
        def mode_body():
            return {
                "horizon": 500,
                "exposures": [
                    {"id": "A", "duration": 2, "earliest_start": 0, "latest_start": 100,
                     "equipment": "X", "cooling": 0, "mode": "a"},
                    {"id": "B", "duration": 2, "earliest_start": 0, "latest_start": 100,
                     "equipment": "X", "cooling": 0, "mode": "b"},
                    {"id": "C", "duration": 1, "earliest_start": 0, "latest_start": 100,
                     "equipment": "Y", "cooling": 0, "mode": "a"},
                    {"id": "D", "duration": 1, "earliest_start": 0, "latest_start": 100,
                     "equipment": "Y", "cooling": 0, "mode": "a"},
                    {"id": "E", "duration": 1, "earliest_start": 0, "latest_start": 100,
                     "equipment": "Z", "cooling": 0, "mode": "a"},
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

        # Unreachable directed transition: link forces b before a while b->a
        # is not registered. Must be 200 feasible=false, never zero-cost.
        body = mode_body()
        body["links"] = [{"from_id": "B", "to_id": "A", "min_gap": 0}]
        body["readout_modes"][0]["transitions"] = [
            {"from_mode": "a", "to_mode": "b", "duration": 2}]
        r = cli.post("/api/schedule", json=body)
        if r.status_code != 200 or r.json().get("feasible") is not False:
            raise RuntimeError(f"unregistered transition must be infeasible: {r.status_code} {r.text[:200]}")
        d = r.json()
        if d.get("starts") is not None or d.get("calibrations") is not None:
            raise RuntimeError("partial/stale plan returned for unreachable transition")

        # Same request with the reverse transition registered becomes feasible.
        body["readout_modes"][0]["transitions"].append(
            {"from_mode": "b", "to_mode": "a", "duration": 6})
        r = cli.post("/api/schedule", json=body)
        if r.status_code != 200 or not r.json().get("feasible"):
            raise RuntimeError(f"registered reverse transition should solve: {r.status_code} {r.text[:200]}")

        # Bad mode reference: 400 input error, located to the exposure.
        body = mode_body()
        body["exposures"][1]["mode"] = "turbo"
        r = cli.post("/api/schedule", json=body)
        if r.status_code != 400 or r.json().get("reason") != "input_error":
            raise RuntimeError(f"bad mode reference must be 400 input_error: {r.status_code} {r.text[:200]}")
        if not any("turbo" in m for m in r.json()["field_errors"]):
            raise RuntimeError("mode reference error not located to field")

        # Conflicting switch table: same directed pair twice.
        body = mode_body()
        body["readout_modes"][0]["transitions"].append(
            {"from_mode": "a", "to_mode": "b", "duration": 9})
        r = cli.post("/api/schedule", json=body)
        if r.status_code != 400 or r.json().get("reason") != "input_error":
            raise RuntimeError(f"conflicting switch table must be 400: {r.status_code} {r.text[:200]}")

        # Compatibility: classic request (no readout_modes) stays unchanged.
        classic = {
            "horizon": 1000,
            "exposures": [
                {"id": "A", "duration": 2, "earliest_start": 0, "latest_start": 100,
                 "equipment": "X", "cooling": 0},
                {"id": "B", "duration": 2, "earliest_start": 0, "latest_start": 100,
                 "equipment": "X", "cooling": 0},
                {"id": "C", "duration": 1, "earliest_start": 0, "latest_start": 100,
                 "equipment": "Y", "cooling": 0},
                {"id": "D", "duration": 1, "earliest_start": 0, "latest_start": 100,
                 "equipment": "Y", "cooling": 0},
                {"id": "E", "duration": 1, "earliest_start": 0, "latest_start": 100,
                 "equipment": "Z", "cooling": 0},
            ],
            "links": [],
        }
        r = cli.post("/api/schedule", json=classic)
        if r.status_code != 200 or not r.json().get("feasible"):
            raise RuntimeError(f"classic request broken: {r.status_code} {r.text[:200]}")
        d = r.json()
        if "calibrations" in d or "initial_mode" in d["equipment_orders"][0]:
            raise RuntimeError("classic response leaked mode-specific fields")



@section("infeasible API smoke")
def check_api_infeasible():
    check_api_modes()
    with httpx.Client(base_url=API_URL, timeout=30) as cli:
        h = cli.get("/health")
        h.raise_for_status()
        if h.json().get("status") != "ok":
            raise RuntimeError("api /health not ok")

        # Valid input, impossible timing: two long exposures on one equipment,
        # both forced to start within the first 4 time units.
        body = _five([
            ("A", 5, 0, 4, "X", 0),
            ("B", 5, 0, 4, "X", 0),
            ("C", 1, 0, 50, "Y", 0),
            ("D", 1, 0, 50, "Y", 0),
            ("E", 1, 0, 50, "Z", 0),
        ])
        r = cli.post("/api/schedule", json=body)
        if r.status_code != 200:
            raise RuntimeError(f"expected HTTP 200 for infeasible, got {r.status_code}")
        data = r.json()
        if data.get("feasible") is not False or data.get("reason") != "no_schedule":
            raise RuntimeError(f"unexpected infeasible payload: {data}")
        if data.get("starts") is not None or data.get("finishes") is not None:
            raise RuntimeError("partial/stale solution returned for infeasible case")

        # Contrast: malformed input must be a 400 input_error.
        bad = _five([
            ("A", 1, 40, 10, "X", 0),
            ("B", 1, 0, 50, "X", 0),
            ("C", 1, 0, 50, "Y", 0),
            ("D", 1, 0, 50, "Y", 0),
            ("E", 1, 0, 50, "Z", 0),
        ])
        r2 = cli.post("/api/schedule", json=bad)
        if r2.status_code != 400 or r2.json().get("reason") != "input_error":
            raise RuntimeError(f"expected 400 input_error, got {r2.status_code} {r2.text[:200]}")

    # Web tier health (proxied page server up).
    try:
        with httpx.Client(base_url=WEB_URL, timeout=10) as cli:
            wh = cli.get("/health")
            wh.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"web health failed: {exc}") from exc


def main() -> int:
    check_pytest()
    check_build()
    check_feasible()
    check_api_infeasible()

    bit = {"code tests (pytest)": 1, "build / static integrity": 2,
           "feasible schedule": 4, "infeasible API smoke": 8}
    code = 0
    print("\n=== summary ===")
    for name, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            code |= bit[name]
    print(f"\nexit code: {code} (bitmask 1=tests 2=build 4=feasible 8=api-smoke)")
    return code


if __name__ == "__main__":
    sys.exit(main())

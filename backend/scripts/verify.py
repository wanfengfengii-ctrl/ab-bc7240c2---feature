#!/usr/bin/env python3
"""One-shot verification for the beamline scheduler stack.

Aggregated exit code is a bitmask (0 = everything passed):
    bit 0 (1)  code tests (pytest)
    bit 1 (2)  build / static integrity checks
    bit 2 (4)  feasible schedule produced by the solver
    bit 3 (8)  infeasible-input API smoke (200 + feasible=false, no partial)
    bit 4 (16) readout-mode calibration (compatibility, initial switch,
               directed asymmetry, contiguous cooling occupancy, unreachable
               transition)
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
    print(f"  starts={starts} makespan={r['makespan']} sum={r['sum_starts']}")


def _mode_body():
    return {
        "horizon": 1000,
        "exposures": [
            {"id": "A", "duration": 2, "earliest_start": 0, "latest_start": 100,
             "equipment": "X", "cooling": 4, "mode": "T"},
            {"id": "B", "duration": 2, "earliest_start": 0, "latest_start": 100,
             "equipment": "X", "cooling": 0, "mode": "F"},
            {"id": "C", "duration": 2, "earliest_start": 0, "latest_start": 100,
             "equipment": "Y", "cooling": 0, "mode": ""},
            {"id": "D", "duration": 2, "earliest_start": 0, "latest_start": 100,
             "equipment": "Y", "cooling": 0, "mode": ""},
            {"id": "F", "duration": 1, "earliest_start": 0, "latest_start": 100,
             "equipment": "Z", "cooling": 0, "mode": ""},
        ],
        "links": [{"from_id": "A", "to_id": "B", "min_gap": 0, "max_gap": None}],
        "modes": {"X": {
            "initial_mode": "T",
            "transitions": [
                {"from_mode": "T", "to_mode": "F", "duration": 1},
                {"from_mode": "F", "to_mode": "T", "duration": 9},
            ],
        }},
    }


def _check_segments(body, r):
    """Independent re-verification of calibration segments and timelines."""
    exps = {e["id"]: e for e in body["exposures"]}
    ids = [e["id"] for e in body["exposures"]]
    starts = dict(zip(ids, r["starts"]))
    tables = {
        eq: {(t["from_mode"], t["to_mode"]): t["duration"]
             for t in setup["transitions"]}
        for eq, setup in body["modes"].items()
    }
    initial = {eq: setup["initial_mode"] for eq, setup in body["modes"].items()}

    # Rebuild per-equipment order and demand one segment per real switch,
    # anchored exactly at the predecessor's cooling release.
    for eq, setup in body["modes"].items():
        seq = [i for i in ids if exps[i]["equipment"] == eq]
        seq.sort(key=lambda i: (starts[i], i))
        prev, prev_mode = None, initial[eq]
        for i in seq:
            mode = exps[i]["mode"]
            if mode == prev_mode:
                prev, prev_mode = i, mode
                continue
            segs = [c for c in r["calibrations"]
                    if c["equipment"] == eq and c["successor_id"] == i]
            if len(segs) != 1:
                raise RuntimeError(f"{eq}: expected exactly one segment into {i}")
            c = segs[0]
            if c["predecessor_id"] != prev:
                raise RuntimeError(f"{eq}: segment into {i} not anchored at adjacent {prev}")
            if (c["from_mode"], c["to_mode"]) != (prev_mode, mode):
                raise RuntimeError(f"{eq}: segment modes {c} mismatch")
            want_dur = tables[eq][(prev_mode, mode)]
            if c["duration"] != want_dur or c["finish"] - c["start"] != want_dur:
                raise RuntimeError(f"{eq}: segment duration mismatch: {c}")
            if prev is None:
                if c["start"] != 0:
                    raise RuntimeError(f"{eq}: initial switch must start at 0")
            else:
                release = starts[prev] + exps[prev]["duration"] + exps[prev]["cooling"]
                if c["start"] != release:
                    raise RuntimeError(
                        f"{eq}: segment {prev}->{i} starts {c['start']} != cooling "
                        f"release {release} (must occupy contiguously)"
                    )
            if c["finish"] > starts[i]:
                raise RuntimeError(f"{eq}: calibration into {i} ends after its start")
            prev, prev_mode = i, mode

    # Every claimed pair must be registered (missing pairs are not zero-cost).
    for c in r["calibrations"]:
        pair = (c["from_mode"], c["to_mode"])
        if c["from_mode"] != c["to_mode"] and pair not in tables[c["equipment"]]:
            raise RuntimeError(f"unregistered transition used as zero cost: {c}")


@section("readout-mode calibration")
def check_readout_modes():
    from app.scheduler import solve
    from app.schemas import ScheduleRequest

    # (a) compatibility: mode-less request is unchanged and carries no segments.
    legacy = _five([
        ("A", 4, 0, 50, "X", 2),
        ("B", 3, 0, 50, "X", 1),
        ("C", 5, 0, 50, "Y", 0),
        ("D", 2, 0, 50, "Y", 3),
        ("E", 6, 2, 40, "Z", 0),
    ])
    rl = solve(ScheduleRequest(**legacy), phase_seconds=5)
    if not rl.get("feasible") or rl.get("calibrations"):
        raise RuntimeError(f"legacy request changed: {rl}")

    # (b) initial-mode switch + cooling-contiguous calibration.
    body = _mode_body()
    r = solve(ScheduleRequest(**body), phase_seconds=5)
    if not r.get("feasible"):
        raise RuntimeError(f"mode request expected feasible: {r}")
    starts = dict(zip([e["id"] for e in body["exposures"]], r["starts"]))
    if starts["A"] != 0 or starts["B"] != 7:  # 2 exp + 4 cooling + 1 cal
        raise RuntimeError(f"unexpected starts {starts} (want A=0 B=7)")
    _check_segments(body, r)
    seg = r["calibrations"][0]
    assert seg["start"] == 6 and seg["finish"] == 7 and seg["margin"] == 0

    # (c) directed asymmetry: initial mode F with only F->T registered (9).
    # B(F) can start immediately (same mode) and A(T) follows; the order
    # A->B would require the unregistered T->F and is impossible.
    body_asym = _mode_body()
    body_asym["modes"]["X"] = {
        "initial_mode": "F",
        "transitions": [{"from_mode": "F", "to_mode": "T", "duration": 9}],
    }
    body_asym["links"] = []
    ra = solve(ScheduleRequest(**body_asym), phase_seconds=5)
    if not ra.get("feasible"):
        raise RuntimeError("asymmetric table should allow the reversed order")
    order = next(o for o in ra["equipment_orders"] if o["equipment"] == "X")
    if order["sequence"] != ["B", "A"]:
        raise RuntimeError(f"expected order B->A, got {order['sequence']}")
    _check_segments(body_asym, ra)

    # (d) the reverse direction missing entirely (initial F, A needs T,
    # no F->T anywhere) is infeasible: 200/feasible=false, never partial.
    body_unreach = _mode_body()
    body_unreach["exposures"][0]["mode"] = "T"
    body_unreach["exposures"][1]["mode"] = "T"
    body_unreach["links"] = []
    body_unreach["modes"]["X"] = {
        "initial_mode": "F",
        "transitions": [{"from_mode": "T", "to_mode": "F", "duration": 1}],
    }
    ru = solve(ScheduleRequest(**body_unreach), phase_seconds=5)
    if ru.get("feasible") is not False:
        raise RuntimeError(f"unreachable transition must be infeasible: {ru}")
    if "starts" in ru or "calibrations" in ru:
        raise RuntimeError("infeasible mode result leaked partial fields")

    # API-level confirmation of (d) and the 400 contrast for a bad mode name.
    with httpx.Client(base_url=API_URL, timeout=30) as cli:
        resp = cli.post("/api/schedule", json=body_unreach)
        if resp.status_code != 200 or resp.json().get("feasible") is not False:
            raise RuntimeError(
                f"API unreachable-transition case: {resp.status_code} {resp.text[:200]}"
            )
        if resp.json().get("starts") is not None:
            raise RuntimeError("API returned partial schedule for infeasible mode case")
        bad = _mode_body()
        bad["exposures"][0]["mode"] = "Q"
        rb = cli.post("/api/schedule", json=bad)
        if rb.status_code != 400 or rb.json().get("reason") != "input_error":
            raise RuntimeError(
                f"bad mode reference must be 400 input_error, got "
                f"{rb.status_code} {rb.text[:200]}"
            )


@section("infeasible API smoke")
def check_api_infeasible():
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
    check_readout_modes()
    check_api_infeasible()

    bit = {"code tests (pytest)": 1, "build / static integrity": 2,
           "feasible schedule": 4, "readout-mode calibration": 16,
           "infeasible API smoke": 8}
    code = 0
    print("\n=== summary ===")
    for name, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            code |= bit[name]
    print(f"\nexit code: {code} "
          "(bitmask 1=tests 2=build 4=feasible 8=api-smoke 16=readout-modes)")
    return code


if __name__ == "__main__":
    sys.exit(main())

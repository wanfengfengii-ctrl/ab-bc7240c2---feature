"""Unit tests for optional readout-mode calibration scheduling."""
import pytest

from app.scheduler import InputValidationError, solve
from app.schemas import Link, ModeSetup, TransitionSpec


def _exposures():
    # A,B share X; C,D share Y; F alone on Z.
    return [
        ("A", "X"), ("B", "X"), ("C", "Y"), ("D", "Y"), ("F", "Z"),
    ]


def _req(modes=None, modes_of=None, durations=None, coolings=None,
         windows=None, links=None, horizon=1000):
    from app.schemas import Exposure, ScheduleRequest

    durations = durations or {}
    coolings = coolings or {}
    windows = windows or {}
    exps = [
        Exposure(
            id=i, duration=durations.get(i, 2),
            earliest_start=windows.get(i, (0, 100))[0],
            latest_start=windows.get(i, (0, 100))[1],
            equipment=eq, cooling=coolings.get(i, 0),
            mode=(modes_of or {}).get(i, ""),
        )
        for i, eq in _exposures()
    ]
    return ScheduleRequest(horizon=horizon, exposures=exps,
                           links=links or [], modes=modes or {})


def _setup(initial, pairs):
    return ModeSetup(
        initial_mode=initial,
        transitions=[
            TransitionSpec(from_mode=a, to_mode=b, duration=d)
            for a, b, d in pairs
        ],
    )


def test_disabled_modes_keep_legacy_behavior(builder, E):
    # No modes registry at all: empty mode fields are accepted and the result
    # stays identical to the mode-less scheduler.
    req = builder([
        E("A", 2, 0, 50, "X"), E("B", 3, 0, 50, "X"),
        E("C", 2, 0, 50, "Y"), E("D", 4, 0, 50, "Y"),
        E("F", 1, 0, 50, "Z"),
    ])
    r = solve(req, phase_seconds=2)
    assert r["feasible"] is True
    assert r["starts"] == [0, 2, 0, 2, 0]
    assert r["calibrations"] == []


def test_first_exposure_switches_from_initial_mode():
    # X starts in mode F; both exposures need T and F->T costs 3.
    req = _req(
        modes_of={"A": "T", "B": "T"},
        modes={"X": _setup("F", [("F", "T", 3)])},
    )
    r = solve(req, phase_seconds=3)
    assert r["feasible"] is True
    s = dict(zip(["A", "B", "C", "D", "F"], r["starts"]))
    # First item on X cannot start before the 3-unit initial calibration.
    assert s["A"] == 3
    first = [c for c in r["calibrations"] if c["successor_id"] == "A"][0]
    assert (first["start"], first["finish"]) == (0, 3)
    assert first["predecessor_id"] is None
    assert (first["from_mode"], first["to_mode"]) == ("F", "T")
    assert first["margin"] == 0
    # Same-mode follow-up A->B needs no calibration segment.
    assert [c["successor_id"] for c in r["calibrations"]] == ["A"]


def test_directed_asymmetry_changes_feasible_order():
    # F->T registered (3), T->F is NOT: B(F) must precede A(T) on X.
    req = _req(
        modes_of={"A": "T", "B": "F"},
        durations={"A": 2, "B": 10},
        modes={"X": _setup("F", [("F", "T", 3)])},
    )
    r = solve(req, phase_seconds=3)
    assert r["feasible"] is True
    order = next(o for o in r["equipment_orders"] if o["equipment"] == "X")
    assert order["sequence"] == ["B", "A"]
    s = dict(zip(["A", "B", "C", "D", "F"], r["starts"]))
    assert s["B"] == 0  # initial mode F, no calibration needed
    assert s["A"] == 13  # 10 exposure + 3 directed switch
    seg = r["calibrations"][0]
    assert (seg["from_mode"], seg["to_mode"], seg["duration"]) == ("F", "T", 3)
    assert seg["predecessor_id"] == "B"

    # Reverse the registered direction only: T->F present but F->T missing,
    # and initial mode F means nothing can ever reach T for A.
    req_bad = _req(
        modes_of={"A": "T", "B": "F"},
        modes={"X": _setup("F", [("T", "F", 1)])},
    )
    r_bad = solve(req_bad, phase_seconds=3)
    assert r_bad["feasible"] is False
    assert "starts" not in r_bad


def test_calibration_occupies_machine_contiguously_after_cooling():
    # A: duration 2, cooling 4; B then needs a 1-unit T->F calibration.
    # The calibration must start exactly at A's cooling release (6) and
    # finish (7) before B starts.
    req = _req(
        modes_of={"A": "T", "B": "F"},
        durations={"A": 2}, coolings={"A": 4},
        links=[Link(from_id="A", to_id="B", min_gap=0)],
        modes={"X": _setup("T", [("T", "F", 1), ("F", "T", 9)])},
    )
    r = solve(req, phase_seconds=3)
    assert r["feasible"] is True
    s = dict(zip(["A", "B", "C", "D", "F"], r["starts"]))
    assert s["A"] == 0
    assert s["B"] == 7
    seg = [c for c in r["calibrations"] if c["successor_id"] == "B"][0]
    assert (seg["start"], seg["finish"]) == (6, 7)
    assert seg["wait_before"] == 0
    assert seg["margin"] == 0
    assert (seg["from_mode"], seg["to_mode"]) == ("T", "F")

    # Forcing B earlier than exposure+cooling+calibration leaves no schedule.
    req_tight = _req(
        modes_of={"A": "T", "B": "F"},
        durations={"A": 2}, coolings={"A": 4},
        windows={"B": (0, 6)},
        links=[Link(from_id="A", to_id="B", min_gap=0)],
        modes={"X": _setup("T", [("T", "F", 1)])},
    )
    r_tight = solve(req_tight, phase_seconds=3)
    assert r_tight["feasible"] is False


def test_calibration_only_counts_against_adjacent_predecessor():
    # Three exposures on X: A=T, G=T, B=F. Only G->B pays T->F; the
    # non-adjacent A must not be charged for the same switch.
    from app.schemas import Exposure, ScheduleRequest

    exps = [
        Exposure(id="A", duration=2, earliest_start=0, latest_start=100,
                 equipment="X", cooling=0, mode="T"),
        Exposure(id="B", duration=2, earliest_start=0, latest_start=100,
                 equipment="X", cooling=0, mode="F"),
        Exposure(id="G", duration=2, earliest_start=0, latest_start=100,
                 equipment="X", cooling=0, mode="T"),
        Exposure(id="C", duration=2, earliest_start=0, latest_start=100,
                 equipment="Y", cooling=0),
        Exposure(id="D", duration=2, earliest_start=0, latest_start=100,
                 equipment="Y", cooling=0),
    ]
    req = ScheduleRequest(
        exposures=exps,
        modes={"X": _setup("T", [("T", "F", 1), ("F", "T", 1)])},
    )
    r = solve(req, phase_seconds=3)
    assert r["feasible"] is True
    s = {e.id: v for e, v in zip(exps, r["starts"])}
    # T,T,F order packs as A=0, G=2 (same mode, no calibration),
    # B=5 (calibration G->B runs 4..5).
    assert (s["A"], s["G"], s["B"]) == (0, 2, 5)
    segs = {(c["predecessor_id"], c["successor_id"]): c for c in r["calibrations"]}
    assert set(segs) == {("G", "B")}
    assert (segs[("G", "B")]["start"], segs[("G", "B")]["finish"]) == (4, 5)


def test_margin_reported_when_successor_window_delays_start():
    req = _req(
        modes_of={"A": "T", "B": "F"},
        windows={"B": (20, 50)},
        modes={"X": _setup("T", [("T", "F", 1), ("F", "T", 9)])},
    )
    r = solve(req, phase_seconds=3)
    assert r["feasible"] is True
    seg = [c for c in r["calibrations"] if c["successor_id"] == "B"][0]
    # Calibration still runs right after A's cooling (2..3); B only opens at 20.
    assert (seg["start"], seg["finish"]) == (2, 3)
    assert seg["margin"] == 17


def test_no_reachable_transition_is_infeasible_not_input_error():
    # Complete, conflict-free table; the needed F->T pair is simply absent.
    req = _req(
        modes_of={"A": "T", "B": "T"},
        modes={"X": _setup("F", [("T", "F", 1)])},
    )
    r = solve(req, phase_seconds=3)
    assert r["feasible"] is False
    assert r["reason"] == "infeasible"
    assert "starts" not in r
    assert "calibrations" not in r


def test_unknown_mode_reference_is_input_error():
    req = _req(
        modes_of={"A": "Q", "B": "T"},
        modes={"X": _setup("F", [("F", "T", 3)])},
    )
    with pytest.raises(InputValidationError) as ei:
        solve(req, phase_seconds=2)
    assert any("unknown mode 'Q'" in m for m in ei.value.errors)


def test_mode_selected_without_registry_is_input_error():
    req = _req(modes_of={"A": "T"})
    with pytest.raises(InputValidationError) as ei:
        solve(req, phase_seconds=2)
    assert any("no registered mode table" in m for m in ei.value.errors)


def test_registered_equipment_requires_mode_selection():
    req = _req(
        modes_of={"B": "T"},
        modes={"X": _setup("F", [("F", "T", 3)])},
    )
    with pytest.raises(InputValidationError) as ei:
        solve(req, phase_seconds=2)
    assert any("no mode was selected" in m for m in ei.value.errors)


def test_conflicting_duplicate_transition_is_input_error():
    req = _req(
        modes_of={"A": "T", "B": "F"},
        modes={"X": ModeSetup(initial_mode="F", transitions=[
            TransitionSpec(from_mode="F", to_mode="T", duration=3),
            TransitionSpec(from_mode="F", to_mode="T", duration=5),
        ])},
    )
    with pytest.raises(InputValidationError) as ei:
        solve(req, phase_seconds=2)
    assert any("duplicate transition F->T" in m for m in ei.value.errors)

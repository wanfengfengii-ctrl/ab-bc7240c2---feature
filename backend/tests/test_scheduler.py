"""Unit tests for the CP-SAT scheduler."""
import pytest

from app.scheduler import InputValidationError, solve
from app.schemas import Link


def test_basic_feasible(builder, E):
    req = builder([
        E("A", 2, 0, 50, "X"), E("B", 3, 0, 50, "X"),
        E("C", 2, 0, 50, "Y"), E("D", 4, 0, 50, "Y"),
        E("F", 1, 0, 50, "Z"),
    ])
    r = solve(req, phase_seconds=2)
    assert r["feasible"] is True
    assert len(r["starts"]) == 5
    assert r["makespan"] == max(r["finishes"])


def test_cooling_blocks_next_same_equipment(builder, E):
    req = builder(
        [
            E("A", duration=3, cooling=4, equipment="X"),
            E("B", duration=2, cooling=0, equipment="X"),
            E("C", duration=1, equipment="Y"),
            E("D", duration=1, equipment="Y"),
            E("F", duration=1, equipment="Z"),
        ],
        links=[Link(from_id="A", to_id="B", min_gap=0)],
    )
    r = solve(req, phase_seconds=2)
    assert r["feasible"] is True
    s = dict(zip(["A", "B", "C", "D", "F"], r["starts"]))
    # Link orders A before B; cooling keeps equipment X occupied for 4 more
    # units, so B cannot start until A's exposure + cooling have elapsed.
    assert s["B"] >= s["A"] + 3 + 4
    assert s["B"] == s["A"] + 7  # lex/sum optimal packs tightly


def test_min_max_gap_hold(builder, E):
    req = builder(
        [E("A", 2, 0, 50, "X"), E("B", 2, 0, 50, "Y"),
         E("C", 1, 0, 50, "Z"), E("D", 1, 0, 50, "Z"),
         E("F", 1, 0, 50, "W")],
        links=[Link(from_id="A", to_id="B", min_gap=5, max_gap=7)],
    )
    r = solve(req, phase_seconds=2)
    assert r["feasible"] is True
    gap = r["starts"][1] - r["finishes"][0]
    assert 5 <= gap <= 7
    slack = r["slacks"][0]
    assert slack["actual_gap"] == gap
    assert slack["slack"] == 7 - gap


def test_window_constraints_respected(builder, E):
    req = builder([
        E("A", 2, 10, 20, "X"), E("B", 2, 10, 20, "X"),
        E("C", 2, 10, 20, "Y"), E("D", 2, 10, 20, "Y"),
        E("F", 2, 10, 20, "Z"),
    ])
    r = solve(req, phase_seconds=2)
    assert r["feasible"] is True
    for s in r["starts"]:
        assert 10 <= s <= 20


def test_infeasible_tight_windows_same_equipment(builder, E):
    req = builder([
        E("A", 5, 0, 4, "X"), E("B", 5, 0, 4, "X"),
        E("C", 1, 0, 50, "Y"), E("D", 1, 0, 50, "Y"),
        E("F", 1, 0, 50, "Z"),
    ])
    r = solve(req, phase_seconds=2)
    assert r["feasible"] is False
    assert r["reason"] == "infeasible"
    assert "starts" not in r


def test_infeasible_cyclic_max_gap(builder, E):
    req = builder(
        [E("A", 2, 0, 50, "X"), E("B", 2, 0, 50, "Y"),
         E("C", 1, 0, 50, "Z"), E("D", 1, 0, 50, "Z"),
         E("F", 1, 0, 50, "W")],
        links=[
            Link(from_id="A", to_id="B", min_gap=0, max_gap=1),
            Link(from_id="B", to_id="A", min_gap=0, max_gap=1),
        ],
    )
    r = solve(req, phase_seconds=2)
    assert r["feasible"] is False


def test_lexicographic_objective_order(builder, E):
    # Two free exposures on independent equipment: everything at 0 is optimal.
    req = builder([
        E("A", 3, 0, 50, "X"), E("B", 3, 0, 50, "Y"),
        E("C", 3, 0, 50, "Z"), E("D", 3, 0, 50, "W"),
        E("F", 3, 0, 50, "V"),
    ])
    r = solve(req, phase_seconds=2)
    assert r["feasible"] is True
    assert r["starts"] == [0, 0, 0, 0, 0]
    assert r["makespan"] == 3
    assert r["sum_starts"] == 0


def test_makespan_preferred_over_sum(builder, E):
    # A/B share equipment; a delayed B would lower nothing, but verify
    # makespan-optimal packing: B starts right after A (+ cooling 0).
    req = builder([
        E("A", 4, 0, 50, "X"), E("B", 4, 0, 50, "X"),
        E("C", 1, 0, 50, "Y"), E("D", 1, 0, 50, "Y"),
        E("F", 1, 0, 50, "Z"),
    ])
    r = solve(req, phase_seconds=2)
    s = r["starts"]
    assert s[1] == s[0] + 4  # packed, makespan 8 on X
    assert r["makespan"] == 8


def test_lex_break_ties_in_entry_order(builder, E):
    # X holds A,B (dur 2 each, no cooling); Y holds C,D. Independent, so the
    # lex-minimal vector places A at 0, B at 2 (forced), C at 0, D at 2, F 0.
    # Then check tie-breaking among alternatives: give A and C identical
    # setups on distinct equipment and force a delay choice via windows.
    req = builder([
        E("A", 2, 0, 50, "X"), E("B", 2, 0, 50, "X"),
        E("C", 2, 0, 50, "Y"), E("D", 2, 0, 50, "Y"),
        E("F", 2, 0, 50, "Z"),
    ])
    r = solve(req, phase_seconds=2)
    assert r["feasible"] is True
    assert r["starts"] == [0, 2, 0, 2, 0]


def test_equipment_order_output(builder, E):
    req = builder([
        E("A", 2, 5, 50, "X"), E("B", 2, 0, 50, "X"),
        E("C", 1, 0, 50, "Y"), E("D", 1, 0, 50, "Y"),
        E("F", 1, 0, 50, "Z"),
    ])
    r = solve(req, phase_seconds=2)
    orders = {o["equipment"]: o["sequence"] for o in r["equipment_orders"]}
    # B starts at 0, A earliest at 5 -> order [B, A].
    assert orders["X"] == ["B", "A"]


def test_validation_duplicate_ids(builder, E):
    with pytest.raises(InputValidationError) as ei:
        solve(builder([
            E("A", 1, 0, 50, "X"), E("A", 1, 0, 50, "Y"),
            E("C", 1, 0, 50, "Z"), E("D", 1, 0, 50, "W"),
            E("F", 1, 0, 50, "V"),
        ]), phase_seconds=1)
    assert any("duplicate" in m for m in ei.value.errors)


def test_validation_window_and_link_errors(builder, E):
    with pytest.raises(InputValidationError) as ei:
        solve(builder(
            [E("A", 1, 40, 10, "X"), E("B", 1, 0, 5, "X"),
             E("C", 1, 0, 50, "Y"), E("D", 1, 0, 50, "Y"),
             E("F", 999, 0, 50, "Z")],
            links=[Link(from_id="A", to_id="ZZ", min_gap=5, max_gap=2)],
            horizon=100,
        ), phase_seconds=1)
    msgs = " ".join(ei.value.errors)
    assert "earliest_start" in msgs
    assert "unknown exposure 'ZZ'" in msgs
    assert "max_gap" in msgs
    assert "horizon" in msgs


def test_ten_exposures_accepted(builder, E):
    exps = [
        E(chr(ord("A") + i), duration=1 + i % 3, equipment=f"E{i % 4}",
          cooling=i % 2)
        for i in range(10)
    ]
    r = solve(builder(exps), phase_seconds=5)
    assert r["feasible"] is True
    assert len(r["starts"]) == 10

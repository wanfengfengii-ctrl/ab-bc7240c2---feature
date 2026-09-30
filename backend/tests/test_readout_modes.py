"""Tests for optional detector readout-mode switching (calibration segments)."""
import pytest

from app.scheduler import InputValidationError, solve
from app.schemas import (
    EquipmentModeConfig,
    Exposure,
    Link,
    ModeTransition,
    ScheduleRequest,
)


def ex(id, equipment="X", mode="a", **kw):
    base = dict(duration=2, earliest_start=0, latest_start=100, cooling=0)
    base.update(kw)
    return Exposure(id=id, equipment=equipment, mode=mode, **base)


def cfg(equipment, initial="a", transitions=()):
    return EquipmentModeConfig(
        equipment=equipment,
        initial_mode=initial,
        transitions=[ModeTransition(from_mode=a, to_mode=b, duration=d)
                     for a, b, d in transitions],
    )


def make(exposures, configs, links=None, horizon=500):
    return ScheduleRequest(
        horizon=horizon, exposures=exposures, links=links or [],
        readout_modes=configs,
    )


def three_eq_configs(x_transitions=(), x_initial="a"):
    # Minimal 3-equipment config so the mandatory 5 exposures spread out;
    # tests that use only X still need Y/Z declared for the other exposures.
    return [
        cfg("X", x_initial, x_transitions),
        cfg("Y", "a", ()),
        cfg("Z", "a", ()),
    ]


def five_xxyyz(a_mode="a", b_mode="a", **kw_a):
    return [
        ex("A", "X", a_mode, **kw_a),
        ex("B", "X", b_mode),
        ex("C", "Y", "a"),
        ex("D", "Y", "a"),
        ex("F", "Z", "a"),
    ]


def by_exp(result):
    return {cid: result["starts"][i]
            for i, cid in enumerate(["A", "B", "C", "D", "F"])}


def test_compat_request_without_readout_modes(builder, E):
    """Feature disabled: classic request/optimization/output stay identical."""
    req = builder([
        E("A", 2, 0, 50, "X"), E("B", 3, 0, 50, "X"),
        E("C", 2, 0, 50, "Y"), E("D", 4, 0, 50, "Y"),
        E("F", 1, 0, 50, "Z"),
    ])
    r = solve(req, phase_seconds=2)
    assert r["feasible"] is True
    assert r.get("calibrations") is None
    order = r["equipment_orders"][0]
    assert "initial_mode" not in order and "modes" not in order


def test_mode_field_ignored_when_feature_disabled(builder, E):
    """A stray mode on an exposure must not switch the feature on."""
    req = builder([
        Exposure(id="A", duration=2, earliest_start=0, latest_start=50,
                 equipment="X", cooling=0, mode="b"),
        E("B", 2, 0, 50, "X"),
        E("C", 1, 0, 50, "Y"), E("D", 1, 0, 50, "Y"),
        E("F", 1, 0, 50, "Z"),
    ])
    r = solve(req, phase_seconds=2)
    assert r["feasible"] is True
    assert r.get("calibrations") is None
    # No calibration is paid: B packs right after A.
    s = by_exp(r)
    assert s["B"] == s["A"] + 2


def test_first_exposure_switches_from_initial_mode():
    """Only exposure on X wants mode b: first item calibrates initial a->b."""
    r = solve(make(five_xxyyz("b", "b"),
                   three_eq_configs([("a", "b", 3)])), phase_seconds=3)
    assert r["feasible"] is True
    s = by_exp(r)
    cals = r["calibrations"]
    assert len(cals) == 1
    c = cals[0]
    assert c["equipment"] == "X"
    assert c["prev_exposure_id"] is None
    assert c["exposure_id"] in ("A", "B")
    assert (c["from_mode"], c["to_mode"]) == ("a", "b")
    assert c["switch_duration"] == 3
    first_id = c["exposure_id"]
    # First calibration ends exactly when the first exposure starts (JIT).
    assert c["cal_end"] == s[first_id]
    assert c["cal_start"] == s[first_id] - 3
    assert c["wait_margin"] == 0


def test_first_switch_can_delay_first_start():
    """Pinning the first X exposure at t=0 is infeasible if it needs a switch."""
    req = make(
        five_xxyyz("b", "b", earliest_start=0, latest_start=2),
        three_eq_configs([("a", "b", 5)]))
    r = solve(req, phase_seconds=3)
    # Starts 0..2 cannot fit a 5-unit pre-start calibration -> infeasible.
    assert r["feasible"] is False
    assert "starts" not in r


def test_switching_is_directional_asymmetric():
    """a->b takes 2, b->a takes 6; solver may choose order, paid arc must match."""
    r = solve(make(
        five_xxyyz("a", "b"),
        three_eq_configs([("a", "b", 2), ("b", "a", 6)])), phase_seconds=3)
    assert r["feasible"] is True
    s = by_exp(r)
    assert len(r["calibrations"]) == 1
    c = r["calibrations"][0]
    first, second = sorted([("A", s["A"], "a"), ("B", s["B"], "b")],
                           key=lambda t: t[1])
    assert c["exposure_id"] == second[0]
    assert c["prev_exposure_id"] == first[0]
    assert c["from_mode"] == first[2] and c["to_mode"] == second[2]
    expected = 2 if first[2] == "a" else 6
    assert c["switch_duration"] == expected
    # Calibration begins right when the predecessor cools out and runs
    # continuously; successor starts no earlier than release + switch.
    pred_dur = 2
    assert c["cal_start"] == first[1] + pred_dur
    assert c["cal_end"] == c["cal_start"] + expected
    assert second[1] >= c["cal_end"]


def test_calibration_waits_for_cooling_and_occupies_equipment():
    """Calibration may only start after predecessor cooling ends (continuous)."""
    req = make(
        [ex("A", "X", "a", duration=3, cooling=4),
         ex("B", "X", "b"),
         ex("C", "Y", "a"), ex("D", "Y", "a"), ex("F", "Z", "a")],
        three_eq_configs([("a", "b", 2)]))
    r = solve(req, phase_seconds=3)
    assert r["feasible"] is True
    s = by_exp(r)
    c = [c for c in r["calibrations"] if c["equipment"] == "X"][0]
    # A release = start + 3 (exposure) + 4 (cooling); cal is 2 -> B at +9.
    assert c["cal_start"] == s["A"] + 7
    assert c["cal_end"] == s["A"] + 9
    assert s["B"] >= s["A"] + 9


def test_wait_margin_reports_idle_gap():
    """When a link delays the successor, calibration still hugs cooling end;
    the remaining idle time shows up as wait_margin."""
    req = make(
        five_xxyyz("a", "b"),
        three_eq_configs([("a", "b", 2)]),
        links=[Link(from_id="A", to_id="B", min_gap=10)])
    r = solve(req, phase_seconds=3)
    assert r["feasible"] is True
    s = by_exp(r)
    c = r["calibrations"][0]
    assert c["prev_exposure_id"] == "A"
    assert c["cal_start"] == s["A"] + 2
    assert c["cal_end"] == s["A"] + 4
    assert c["wait_margin"] == s["B"] - c["cal_end"]
    assert c["wait_margin"] > 0


def test_unregistered_transition_is_unreachable_infeasible():
    """Only a->b registered, but a link forces b before a: no schedule."""
    req = make(
        five_xxyyz("a", "b"),
        three_eq_configs([("a", "b", 2)]),
        links=[Link(from_id="B", to_id="A", min_gap=0)])
    r = solve(req, phase_seconds=5)
    assert r["feasible"] is False
    assert r["reason"] == "infeasible"
    assert "starts" not in r
    # Registering the reverse directed transition makes it feasible.
    req2 = make(
        five_xxyyz("a", "b"),
        three_eq_configs([("a", "b", 2), ("b", "a", 4)]),
        links=[Link(from_id="B", to_id="A", min_gap=0)])
    assert solve(req2, phase_seconds=5)["feasible"] is True


def test_missing_transition_is_never_zero_duration():
    """Only b->a is registered while the initial mode is a, so the chain can
    neither start on mode b nor move a->b: a missing directed pair must not be
    silently treated as a zero-duration switch."""
    req = make(five_xxyyz("a", "b"),
               three_eq_configs([("b", "a", 1)]))
    r = solve(req, phase_seconds=5)
    assert r["feasible"] is False


def test_same_mode_adjacency_pays_no_calibration():
    req = make(five_xxyyz("a", "a"), three_eq_configs([("a", "b", 9)]))
    r = solve(req, phase_seconds=3)
    assert r["feasible"] is True
    assert r["calibrations"] == []
    s = by_exp(r)
    # No switch cost: exposures pack back to back despite a->b being registered.
    assert abs(s["B"] - s["A"]) == 2


def test_order_and_modes_in_output():
    req = make(five_xxyyz("b", "a"),
               three_eq_configs([("a", "b", 2), ("b", "a", 6)]))
    r = solve(req, phase_seconds=3)
    assert r["feasible"] is True
    orders = {o["equipment"]: o for o in r["equipment_orders"]}
    ox = orders["X"]
    assert ox["initial_mode"] == "a"
    pairs = list(zip(ox["sequence"], ox["modes"]))
    assert dict(pairs) == {"A": "b", "B": "a"}
    # Sequence must agree with the start-time order.
    s = by_exp(r)
    assert [pid for pid, _ in sorted(pairs, key=lambda p: s[p[0]])] == ox["sequence"]


def test_initial_mode_drives_first_switch_only():
    """Chain A(a) -> B(b): B calibrates off A's mode, never re-reads initial."""
    req = make(
        [ex("A", "X", "a"), ex("B", "X", "b"),
         ex("C", "Y", "a"), ex("D", "Y", "a"), ex("F", "Z", "a")],
        [cfg("X", "b", [("b", "a", 3), ("a", "b", 2)]),
         cfg("Y", "a"), cfg("Z", "a")],
        links=[Link(from_id="A", to_id="B", min_gap=0)])
    r = solve(req, phase_seconds=3)
    assert r["feasible"] is True
    s = by_exp(r)
    x_cals = [c for c in r["calibrations"] if c["equipment"] == "X"]
    # Initial mode is b; A first needs b->a (3), then A(a)->B(b) needs 2.
    assert {(c["prev_exposure_id"], c["from_mode"], c["to_mode"],
             c["switch_duration"]) for c in x_cals} == {
        (None, "b", "a", 3),
        ("A", "a", "b", 2),
    }
    # First calibration sits immediately before A; the second after A.
    first_cal = [c for c in x_cals if c["prev_exposure_id"] is None][0]
    assert first_cal["cal_end"] == s["A"]


def test_validation_unknown_mode_reference():
    req = make(five_xxyyz("a", "b"),
               three_eq_configs([("a", "b", 2)]))
    req.exposures[1].mode = "photon-counting-x9000"
    with pytest.raises(InputValidationError) as ei:
        solve(req, phase_seconds=1)
    assert any("photon-counting-x9000" in m and "not registered" in m
               for m in ei.value.errors)


def test_validation_conflicting_duplicate_transition():
    req = make(five_xxyyz("a", "b"),
               three_eq_configs([("a", "b", 2), ("a", "b", 5)]))
    with pytest.raises(InputValidationError) as ei:
        solve(req, phase_seconds=1)
    assert any("conflicting duplicate transition a->b" in m
               for m in ei.value.errors)


def test_validation_duplicate_equipment_config_and_missing_mode():
    req = make(
        [ex("A", "X", "a"),
         Exposure(id="B", duration=2, earliest_start=0, latest_start=100,
                  equipment="X", cooling=0, mode=None),
         ex("C", "Y", "a"), ex("D", "Y", "a"), ex("F", "Z", "a")],
        [cfg("X", "a", [("a", "b", 2)]),
         cfg("X", "a", []),
         cfg("Y", "a")])
    with pytest.raises(InputValidationError) as ei:
        solve(req, phase_seconds=1)
    msgs = " ".join(ei.value.errors)
    assert "conflicting duplicate equipment config 'X'" in msgs
    assert "readout mode is required" in msgs
    assert "'Z' has no readout_modes config" in msgs


def test_self_transition_is_input_error():
    req = make(five_xxyyz("a", "a"),
               three_eq_configs([("a", "a", 1)]))
    with pytest.raises(InputValidationError) as ei:
        solve(req, phase_seconds=1)
    assert any("self transition" in m for m in ei.value.errors)

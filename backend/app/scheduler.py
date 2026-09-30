"""CP-SAT scheduler for synchrotron beamline exposures.

Optimization order (lexicographic):
  1. minimum final finish time (makespan)
  2. minimum sum of all start times
  3. lexicographically smallest start-time vector in entry order

Each exposure occupies its equipment from ``start`` to ``start + duration +
cooling``; intervals on the same equipment may not overlap, which enforces both
exclusive equipment use and the post-exposure cooling window.

Optional readout-mode switching (``readout_modes`` in the request):
detectors must be calibrated exclusively when switching readout modes. The
equipment execution order and every calibration segment are solved *jointly*
with a per-equipment circuit constraint (one Hamiltonian chain out of a virtual
depot carrying the equipment's initial mode):

  * the first exposure switches out of the registered initial mode, calibrated
    just-in-time before it starts;
  * every later exposure only pays the directed switch from the IMMEDIATELY
    preceding exposure on the same equipment — a transition is never charged
    against a non-adjacent exposure;
  * an unregistered directed transition is simply an absent arc, so it is
    unreachable (the model proves infeasibility) rather than zero-cost;
  * calibration starts exactly when the predecessor's cooling ends (continuous
    occupation) and finishes no later than the successor starts.
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from ortools.sat.python import cp_model

from .schemas import (
    CalibrationSegment,
    EquipmentOrder,
    Exposure,
    Link,
    ScheduleRequest,
    SlackInfo,
)


class InputValidationError(ValueError):
    """Semantic input errors (HTTP 400), distinct from infeasibility."""

    def __init__(self, errors: List[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def validate_request(req: ScheduleRequest) -> None:
    errors: List[str] = []
    ids = [e.id for e in req.exposures]
    id_set = set(ids)
    if len(id_set) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        errors.append(f"duplicate exposure id: {', '.join(dupes)}")

    for idx, e in enumerate(req.exposures):
        if e.earliest_start > e.latest_start:
            errors.append(
                f"exposure {e.id}: earliest_start ({e.earliest_start}) > "
                f"latest_start ({e.latest_start})"
            )
        if e.latest_start + e.duration > req.horizon:
            errors.append(
                f"exposure {e.id}: latest possible finish "
                f"{e.latest_start + e.duration} exceeds horizon {req.horizon}"
            )

    pair_seen = set()
    for ln in req.links:
        if ln.from_id not in id_set:
            errors.append(f"link references unknown exposure '{ln.from_id}'")
        if ln.to_id not in id_set:
            errors.append(f"link references unknown exposure '{ln.to_id}'")
        if ln.from_id == ln.to_id:
            errors.append(f"link {ln.from_id}->{ln.to_id} references the same exposure")
        if ln.max_gap is not None and ln.max_gap < ln.min_gap:
            errors.append(
                f"link {ln.from_id}->{ln.to_id}: max_gap ({ln.max_gap}) < "
                f"min_gap ({ln.min_gap})"
            )
        key = (ln.from_id, ln.to_id)
        if key in pair_seen:
            errors.append(f"duplicate link {ln.from_id}->{ln.to_id}")
        pair_seen.add(key)

    if req.readout_modes is not None:
        errors.extend(_validate_readout_modes(req))

    if errors:
        raise InputValidationError(errors)


def _validate_readout_modes(req: ScheduleRequest) -> List[str]:
    errors: List[str] = []
    configured: set[str] = set()
    known_modes: Dict[str, set[str]] = {}
    for cfg in req.readout_modes:
        if cfg.equipment in configured:
            errors.append(
                f"readout_modes: conflicting duplicate equipment config "
                f"'{cfg.equipment}'"
            )
            continue
        configured.add(cfg.equipment)
        modes = {cfg.initial_mode}
        pairs: set[Tuple[str, str]] = set()
        for tr in cfg.transitions:
            if tr.from_mode == tr.to_mode:
                errors.append(
                    f"readout_modes {cfg.equipment}: self transition "
                    f"{tr.from_mode}->{tr.to_mode} is not allowed "
                    "(same-mode adjacency needs no calibration)"
                )
            pair = (tr.from_mode, tr.to_mode)
            if pair in pairs:
                errors.append(
                    f"readout_modes {cfg.equipment}: conflicting duplicate "
                    f"transition {tr.from_mode}->{tr.to_mode}"
                )
            pairs.add(pair)
            modes.add(tr.from_mode)
            modes.add(tr.to_mode)
        known_modes[cfg.equipment] = modes

    for e in req.exposures:
        if e.equipment not in configured:
            errors.append(
                f"exposure {e.id}: equipment '{e.equipment}' has no "
                "readout_modes config while mode switching is enabled"
            )
        elif e.mode is None:
            errors.append(
                f"exposure {e.id}: readout mode is required while "
                "readout_modes is enabled"
            )
        elif e.mode not in known_modes[e.equipment]:
            errors.append(
                f"exposure {e.id}: mode '{e.mode}' is not registered for "
                f"equipment '{e.equipment}' "
                f"(initial mode or a transition endpoint)"
            )
    return errors


def _build_model(req: ScheduleRequest) -> Tuple[cp_model.CpModel, Dict[str, Any]]:
    model = cp_model.CpModel()
    exps: List[Exposure] = req.exposures
    n = len(exps)
    H = req.horizon

    starts: List[cp_model.IntVar] = []
    ends: List[cp_model.IntVar] = []
    occupy_until: List[cp_model.IntVar] = []
    intervals_by_eq: Dict[str, List[cp_model.IntervalVar]] = defaultdict(list)

    for e in exps:
        s = model.new_int_var(e.earliest_start, e.latest_start, f"start_{e.id}")
        finish = s + e.duration
        release = finish + e.cooling
        # Equipment stays occupied through the cooling window.
        iv = model.new_interval_var(s, e.duration + e.cooling, release, f"occ_{e.id}")
        intervals_by_eq[e.equipment].append(iv)
        starts.append(s)
        ends.append(finish)
        occupy_until.append(release)

    mode_plan: Optional[Dict[str, Any]] = None
    if req.readout_modes is not None:
        mode_plan = _add_mode_circuits(
            model, req, starts, occupy_until, intervals_by_eq, H
        )

    for eq, ivs in intervals_by_eq.items():
        if len(ivs) > 1:
            model.add_no_overlap(ivs)

    by_id = {e.id: i for i, e in enumerate(exps)}
    for ln in req.links:
        a, b = by_id[ln.from_id], by_id[ln.to_id]
        gap = starts[b] - ends[a]
        model.add(gap >= ln.min_gap)
        if ln.max_gap is not None:
            model.add(gap <= ln.max_gap)

    makespan = model.new_int_var(0, H, "makespan")
    model.add_max_equality(makespan, ends)
    sum_starts = model.new_int_var(0, H * n, "sum_starts")
    model.add(sum_starts == sum(starts))

    v: Dict[str, Any] = {
        "starts": starts,
        "ends": ends,
        "occupy_until": occupy_until,
        "makespan": makespan,
        "sum_starts": sum_starts,
    }
    if mode_plan is not None:
        v["mode_plan"] = mode_plan
    return model, v


def _add_mode_circuits(
    model: cp_model.CpModel,
    req: ScheduleRequest,
    starts: List[cp_model.IntVar],
    occupy_until: List[cp_model.IntVar],
    intervals_by_eq: Dict[str, List[cp_model.IntervalVar]],
    horizon: int,
) -> Dict[str, Any]:
    """Add one circuit per equipment jointly ordering exposures/calibrations.

    Nodes 0..k-1 are the equipment's exposures (local indices), node k is a
    virtual depot carrying the initial mode. A chosen arc u->v carries exactly
    one directed switch: depot->v switches initial->mode(v), u->v switches
    mode(u)->mode(v). Missing directed pairs have no arc at all.
    """
    exps = req.exposures
    cfg_by_eq = {c.equipment: c for c in req.readout_modes}
    grouped: Dict[str, List[int]] = defaultdict(list)
    for i, e in enumerate(exps):
        grouped[e.equipment].append(i)

    plan: Dict[str, Any] = {}
    for eq, idxs in sorted(grouped.items()):
        cfg = cfg_by_eq[eq]
        table = {(t.from_mode, t.to_mode): t.duration for t in cfg.transitions}
        k = len(idxs)
        depot = k

        def node_mode(node: int) -> str:
            return cfg.initial_mode if node == depot else exps[idxs[node]].mode

        # arc (u, v) -> (literal, switch_duration)
        arcs: List[Tuple[int, int, cp_model.IntVar]] = []
        info: Dict[Tuple[int, int], Tuple[cp_model.IntVar, int]] = {}
        cal_intervals: List[cp_model.IntervalVar] = []

        def register(u: int, v: int, duration: int) -> None:
            lit = model.new_bool_var(f"arc_{eq}_{u}_to_{v}")
            arcs.append((u, v, lit))
            info[(u, v)] = (lit, duration)
            if v == depot:
                return  # returning to the depot needs no calibration segment
            j = idxs[v]
            # cal_start is a free non-negative var while the arc is absent and
            # is pinned only when this arc is the chosen predecessor of v.
            cal_start = model.new_int_var(0, horizon, f"calstart_{eq}_{u}_to_{v}")
            if u == depot:
                # First exposure: calibrate just-in-time out of the initial
                # mode, finishing exactly when the exposure starts.
                model.add(cal_start == starts[j] - duration).only_enforce_if(lit)
                model.add(starts[j] >= duration).only_enforce_if(lit)
            else:
                i = idxs[u]
                # Calibration starts right after predecessor cooling ends and
                # holds the equipment continuously; it must finish before the
                # successor starts.
                model.add(cal_start == occupy_until[i]).only_enforce_if(lit)
                model.add(
                    starts[j] >= occupy_until[i] + duration
                ).only_enforce_if(lit)
            cal_iv = model.new_optional_interval_var(
                cal_start,
                duration,
                cal_start + duration,
                lit,
                f"cal_{eq}_{u}_to_{v}",
            )
            cal_intervals.append(cal_iv)

        # Depot self-loop (standard circuit idiom; only selectable when no
        # exposure exists, which never happens here).
        depot_loop = model.new_bool_var(f"depot_loop_{eq}")
        arcs.append((depot, depot, depot_loop))

        for a in range(k):
            # depot -> a: first exposure switches out of the initial mode.
            d_first = (
                0
                if node_mode(a) == cfg.initial_mode
                else table.get((cfg.initial_mode, node_mode(a)))
            )
            if d_first is not None:
                register(depot, a, d_first)
            # a -> depot: any exposure may be the last one.
            register(a, depot, 0)
            for b in range(k):
                if a == b:
                    continue
                d_switch = (
                    0 if node_mode(a) == node_mode(b)
                    else table.get((node_mode(a), node_mode(b)))
                )
                if d_switch is not None:
                    register(a, b, d_switch)

        model.add_circuit(arcs)
        intervals_by_eq[eq].extend(cal_intervals)
        plan[eq] = {
            "idxs": idxs,
            "depot": depot,
            "arcs": info,
            "initial_mode": cfg.initial_mode,
        }

    return plan


class SolverTimeoutError(RuntimeError):
    """The solver could not decide within the time limit (status UNKNOWN)."""


def _solve_phase(
    model: cp_model.CpModel,
    vars_: Dict[str, Any],
    phase_seconds: float,
    hint: Optional[List[int]],
) -> Tuple[Optional[cp_model.CpSolver], int, str]:
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = phase_seconds
    solver.parameters.num_search_workers = 8
    solver.parameters.random_seed = 20260929
    if hint is not None:
        for s, v in zip(vars_["starts"], hint):
            model.add_hint(s, v)
    status = solver.solve(model)
    name = solver.status_name(status)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return solver, status, name
    return None, status, name


def solve(req: ScheduleRequest, phase_seconds: float = 10.0) -> Dict[str, Any]:
    """Run the staged lexicographic optimization.

    Raises InputValidationError for bad input. Returns a result dict with
    feasible=False when no executable timing exists.
    """
    validate_request(req)
    exps: List[Exposure] = req.exposures
    links: List[Link] = req.links
    n = len(exps)
    modes_enabled = req.readout_modes is not None

    t0 = time.perf_counter()

    # Phase 1: minimize makespan.
    model, v = _build_model(req)
    model.minimize(v["makespan"])
    solver, status, status_name = _solve_phase(model, v, phase_seconds, None)
    if solver is None:
        if status == cp_model.INFEASIBLE:
            return {"feasible": False, "reason": "infeasible", "status": status_name}
        # UNKNOWN: time limit hit without proving infeasibility — not a
        # definitive "no schedule", surface as an error instead.
        raise SolverTimeoutError(f"phase makespan: solver status {status_name}")
    best_makespan = solver.value(v["makespan"])
    hint = [solver.value(s) for s in v["starts"]]

    # Phase 2: minimize sum of starts, makespan pinned.
    model, v = _build_model(req)
    model.add(v["makespan"] == best_makespan)
    model.minimize(v["sum_starts"])
    solver, _status, status_name = _solve_phase(model, v, phase_seconds, hint)
    if solver is None:
        # The hint from phase 1 already satisfies this pin, so INFEASIBLE
        # cannot occur; UNKNOWN would mean the time limit was hit.
        raise SolverTimeoutError(f"phase sum_starts: solver status {status_name}")
    best_sum = solver.value(v["sum_starts"])
    hint = [solver.value(s) for s in v["starts"]]

    # Phase 3: lex-minimize the start vector in entry order, one var per stage.
    starts_values = list(hint)
    for i in range(n):
        model, v = _build_model(req)
        model.add(v["makespan"] == best_makespan)
        model.add(v["sum_starts"] == best_sum)
        for j in range(i):
            model.add(v["starts"][j] == starts_values[j])
        model.minimize(v["starts"][i])
        solver, _status, status_name = _solve_phase(model, v, phase_seconds, hint)
        if solver is None:
            raise SolverTimeoutError(f"phase lex[{i}]: solver status {status_name}")
        starts_values = [solver.value(s) for s in v["starts"]]
        hint = starts_values

    starts = starts_values
    finishes = [starts[i] + exps[i].duration for i in range(n)]

    # Per-link margins.
    by_id = {e.id: i for i, e in enumerate(exps)}
    slacks: List[SlackInfo] = []
    for ln in links:
        a, b = by_id[ln.from_id], by_id[ln.to_id]
        gap = starts[b] - finishes[a]
        slacks.append(
            SlackInfo(
                from_id=ln.from_id,
                to_id=ln.to_id,
                min_gap=ln.min_gap,
                max_gap=ln.max_gap,
                actual_gap=gap,
                slack=(None if ln.max_gap is None else ln.max_gap - gap),
            )
        )

    result: Dict[str, Any] = {
        "feasible": True,
        "starts": starts,
        "finishes": finishes,
        "makespan": max(finishes),
        "sum_starts": sum(starts),
        "slacks": [s.model_dump() for s in slacks],
        "solver_time_ms": int((time.perf_counter() - t0) * 1000),
    }

    if modes_enabled:
        orders, calibrations = _extract_mode_solution(exps, starts, finishes, v, solver)
        result["equipment_orders"] = [o.model_dump() for o in orders]
        result["calibrations"] = [c.model_dump() for c in calibrations]
    else:
        # Equipment execution orders (classic output, no mode fields).
        grouped: Dict[str, List[Tuple[int, str]]] = defaultdict(list)
        for i, e in enumerate(exps):
            grouped[e.equipment].append((starts[i], e.id))
        equipment_orders = [
            EquipmentOrder(equipment=eq, sequence=[iid for _, iid in sorted(items)])
            for eq, items in sorted(grouped.items())
        ]
        result["equipment_orders"] = [
            o.model_dump(exclude_none=True) for o in equipment_orders
        ]

    return result


def _extract_mode_solution(
    exps: List[Exposure],
    starts: List[int],
    finishes: List[int],
    vars_: Dict[str, Any],
    solver: cp_model.CpSolver,
) -> Tuple[List[EquipmentOrder], List[CalibrationSegment]]:
    """Read chosen arcs out of the solved circuits and build result records."""
    orders: List[EquipmentOrder] = []
    calibrations: List[CalibrationSegment] = []

    for eq, plan in sorted(vars_["mode_plan"].items()):
        idxs: List[int] = plan["idxs"]
        depot: int = plan["depot"]
        arcs = plan["arcs"]
        initial_mode = plan["initial_mode"]

        # Resolve node modes from the request via the exposure list.
        local_modes = [exps[gi].mode for gi in idxs]
        chosen: Dict[int, int] = {}
        for (u, w), (lit, _d) in arcs.items():
            if solver.value(lit) == 1:
                chosen[u] = w

        # Follow the single chain depot -> ... -> depot.
        chain: List[int] = []
        cur = depot
        while chosen.get(cur) is not None and chosen[cur] != depot:
            nxt = chosen[cur]
            chain.append(nxt)
            cur = nxt
        if sorted(chain) != list(range(len(idxs))):  # pragma: no cover
            raise RuntimeError(f"circuit on equipment {eq} did not visit every exposure")

        seq_ids = [exps[idxs[node]].id for node in chain]
        seq_modes = [local_modes[node] for node in chain]
        orders.append(
            EquipmentOrder(
                equipment=eq,
                sequence=seq_ids,
                initial_mode=initial_mode,
                modes=seq_modes,
            )
        )

        # Calibration segments, in chain order. The first arc leaves the depot
        # (initial mode); later arcs leave the immediately preceding exposure.
        prev_node: Optional[int] = None
        for node in chain:
            u = depot if prev_node is None else prev_node
            _lit, duration = arcs[(u, node)]
            from_mode = initial_mode if prev_node is None else local_modes[prev_node]
            to_mode = local_modes[node]
            j = idxs[node]
            if prev_node is None:
                cal_start = starts[j] - duration
                cal_end = starts[j]
                wait_margin = 0
                prev_id: Optional[str] = None
            else:
                pi = idxs[prev_node]
                cal_start = finishes[pi] + exps[pi].cooling
                cal_end = cal_start + duration
                wait_margin = starts[j] - cal_end
                prev_id = exps[pi].id
            # Only real mode changes are calibration segments; an instant
            # registered switch (duration 0) is still reported.
            if from_mode != to_mode:
                calibrations.append(
                    CalibrationSegment(
                        equipment=eq,
                        prev_exposure_id=prev_id,
                        exposure_id=exps[j].id,
                        from_mode=from_mode,
                        to_mode=to_mode,
                        switch_duration=duration,
                        cal_start=cal_start,
                        cal_end=cal_end,
                        wait_margin=wait_margin,
                    )
                )
            prev_node = node

    return orders, calibrations

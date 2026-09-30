"""CP-SAT scheduler for synchrotron beamline exposures.

Optimization order (lexicographic):
  1. minimum final finish time (makespan)
  2. minimum sum of all start times
  3. lexicographically smallest start-time vector in entry order

Each exposure occupies its equipment from ``start`` to ``start + duration +
cooling``; intervals on the same equipment may not overlap, which enforces both
exclusive equipment use and the post-exposure cooling window.
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from ortools.sat.python import cp_model

from .schemas import EquipmentOrder, Exposure, Link, ScheduleRequest, SlackInfo


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

    if errors:
        raise InputValidationError(errors)


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

    return model, {
        "starts": starts,
        "ends": ends,
        "occupy_until": occupy_until,
        "makespan": makespan,
        "sum_starts": sum_starts,
    }


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

    # Equipment execution orders.
    grouped: Dict[str, List[Tuple[int, str]]] = defaultdict(list)
    for i, e in enumerate(exps):
        grouped[e.equipment].append((starts[i], e.id))
    equipment_orders = [
        EquipmentOrder(equipment=eq, sequence=[iid for _, iid in sorted(items)])
        for eq, items in sorted(grouped.items())
    ]

    return {
        "feasible": True,
        "starts": starts,
        "finishes": finishes,
        "makespan": max(finishes),
        "sum_starts": sum(starts),
        "slacks": [s.model_dump() for s in slacks],
        "equipment_orders": [o.model_dump() for o in equipment_orders],
        "solver_time_ms": int((time.perf_counter() - t0) * 1000),
    }

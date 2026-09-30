"""CP-SAT scheduler for synchrotron beamline exposures.

Optimization order (lexicographic):
  1. minimum final finish time (makespan)
  2. minimum sum of all start times
  3. lexicographically smallest start-time vector in entry order

Each exposure occupies its equipment from ``start`` to ``start + duration +
cooling``; intervals on the same equipment may not overlap, which enforces both
exclusive equipment use and the post-exposure cooling window.

Optional readout-mode calibration (``req.modes``):
when equipment is registered with an initial mode and a list of *directed*
transitions, every exposure on that equipment needs a registered transition
into its chosen mode — the first exposure switches from ``initial_mode`` and
each later exposure switches from the mode of the immediately preceding
exposure on the same equipment. An unregistered pair is unreachable and is
never treated as zero-cost. The calibration is an exclusive interval: it may
start only after the predecessor's cooling has ended and must finish before the
successor exposure starts. The per-equipment execution order (a CP-SAT circuit)
and the calibration segments are solved jointly with all exposure starts;
nothing is patched in after the fact.
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

# Type of one incoming calibration choice modeled on the circuit:
# (arc literal, predecessor exposure index or None for the initial switch,
#  calibration duration, from_mode, to_mode)
Choice = Tuple[Any, Optional[int], int, str, str]


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

    for e in req.exposures:
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

    # ---- readout-mode registries --------------------------------------
    known_modes: Dict[str, set] = {}
    for eq, setup in req.modes.items():
        if not eq:
            errors.append("mode registry has an empty equipment name")
            continue
        names = {setup.initial_mode}
        pairs: set = set()
        for tr in setup.transitions:
            pair = (tr.from_mode, tr.to_mode)
            if pair in pairs:
                errors.append(
                    f"equipment {eq}: conflicting duplicate transition "
                    f"{tr.from_mode}->{tr.to_mode}"
                )
                continue
            pairs.add(pair)
            names.add(tr.from_mode)
            names.add(tr.to_mode)
        known_modes[eq] = names

    for e in req.exposures:
        if e.equipment in known_modes:
            if not e.mode:
                errors.append(
                    f"exposure {e.id}: equipment {e.equipment} is registered for "
                    f"readout modes but no mode was selected"
                )
            elif e.mode not in known_modes[e.equipment]:
                errors.append(
                    f"exposure {e.id}: unknown mode '{e.mode}' for equipment "
                    f"{e.equipment} (not declared in its mode table)"
                )
        elif e.mode:
            errors.append(
                f"exposure {e.id}: mode '{e.mode}' selected but equipment "
                f"{e.equipment} has no registered mode table"
            )

    if errors:
        raise InputValidationError(errors)


class _ModeContext:
    """Resolved mode data shared across the staged model rebuilds."""

    def __init__(
        self,
        groups: Dict[str, List[int]],
        tables: Dict[str, Dict[Tuple[str, str], int]],
        initial: Dict[str, str],
    ):
        self.groups = groups
        self.tables = tables
        self.initial = initial
        self.max_switch = max(
            (d for table in tables.values() for d in table.values()), default=0
        )


def _build_model(
    req: ScheduleRequest,
    mode_ctx: Optional[_ModeContext] = None,
) -> Tuple[cp_model.CpModel, Dict[str, Any]]:
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
        if len(ivs) > 1 and eq not in (mode_ctx.groups if mode_ctx is not None else ()):
            model.add_no_overlap(ivs)

    by_id = {e.id: i for i, e in enumerate(exps)}
    for ln in req.links:
        a, b = by_id[ln.from_id], by_id[ln.to_id]
        gap = starts[b] - ends[a]
        model.add(gap >= ln.min_gap)
        if ln.max_gap is not None:
            model.add(gap <= ln.max_gap)

    # ---- readout-mode calibration (joint with the order) ---------------
    if mode_ctx is not None:
        bound = mode_ctx.max_switch

        for eq, idxs in mode_ctx.groups.items():
            table = mode_ctx.tables[eq]
            init = mode_ctx.initial[eq]
            node = {exp_i: pos + 1 for pos, exp_i in enumerate(idxs)}
            arcs: List[Tuple[int, int, Any]] = []

            for ii in idxs:
                to_mode = exps[ii].mode
                choices: List[Choice] = []

                # This exposure could be the first one: switch from the
                # equipment's initial mode. Staying in the same mode is a
                # zero-duration no-op (no calibration segment); a switch to a
                # different mode must be explicitly registered.
                if (init, to_mode) in table:
                    lit = model.new_bool_var(f"first_{exps[ii].id}")
                    arcs.append((0, node[ii], lit))
                    choices.append((lit, None, table[(init, to_mode)], init, to_mode))
                elif init == to_mode:
                    lit = model.new_bool_var(f"first_{exps[ii].id}")
                    arcs.append((0, node[ii], lit))
                    choices.append((lit, None, 0, init, to_mode))

                # Or it follows some other exposure ON THE SAME equipment;
                # only the circuit-selected predecessor is adjacent.
                for pi in idxs:
                    if pi == ii:
                        continue
                    from_mode = exps[pi].mode
                    if (from_mode, to_mode) in table:
                        lit = model.new_bool_var(f"next_{exps[pi].id}_{exps[ii].id}")
                        arcs.append((node[pi], node[ii], lit))
                        choices.append(
                            (lit, pi, table[(from_mode, to_mode)], from_mode, to_mode)
                        )
                    elif from_mode == to_mode:
                        lit = model.new_bool_var(f"next_{exps[pi].id}_{exps[ii].id}")
                        arcs.append((node[pi], node[ii], lit))
                        choices.append((lit, pi, 0, from_mode, to_mode))

                # Every exposure is a possible end of the equipment sequence.
                last_lit = model.new_bool_var(f"last_{exps[ii].id}")
                arcs.append((node[ii], 0, last_lit))

                # Exactly one incoming arc is selected (the circuit guarantees
                # it); calibration start is 0 for the initial switch and the
                # predecessor's exact cooling-release otherwise, so the
                # calibration occupies the machine continuously with no idle
                # gap. The only spare time is the margin after it finishes.
                cs = model.new_int_var(0, H, f"cal_start_{exps[ii].id}")
                cd = model.new_int_var(0, bound, f"cal_dur_{exps[ii].id}")
                ce = model.new_int_var(0, H, f"cal_end_{exps[ii].id}")
                civ = model.new_interval_var(cs, cd, ce, f"cal_{exps[ii].id}")
                intervals_by_eq[eq].append(civ)
                if choices:
                    model.add(cd == sum(lit * dur for lit, _, dur, _, _ in choices))
                    # The circuit selects exactly one incoming literal. Its
                    # chosen predecessor fixes the calibration start exactly.
                    for lit, pi, _dur, _frm, _to in choices:
                        anchor = 0 if pi is None else occupy_until[pi]
                        model.add(cs == anchor).only_enforce_if(lit)
                    model.add(ce == cs + cd)
                    # Calibration must finish before the successor starts.
                    model.add(ce <= starts[ii])
                else:
                    # Nothing in the registry reaches this exposure's mode:
                    # valid input with no executable sequence on this
                    # equipment, so the whole model is infeasible.
                    model.add(1 == 0)

            # One Hamiltonian circuit through node 0 fixes a single linear
            # execution order per equipment; order and timing are solved
            # together with all other constraints.
            model.add_circuit(arcs)

        # Exclusive occupancy: exposure+cooling intervals and calibration
        # intervals on the same equipment may never overlap.
        for eq in mode_ctx.groups:
            ivs = intervals_by_eq[eq]
            if len(ivs) > 1:
                model.add_no_overlap(ivs)

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
        for s, val in zip(vars_["starts"], hint):
            model.add_hint(s, val)
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

    # Only equipment actually used by an exposure participates; requests
    # without managed equipment keep the exact legacy behavior.
    used_eq = {e.equipment for e in exps}
    managed_eq = sorted(used_eq & set(req.modes))
    mode_ctx: Optional[_ModeContext] = None
    if managed_eq:
        groups: Dict[str, List[int]] = defaultdict(list)
        for i, e in enumerate(exps):
            if e.equipment in managed_eq:
                groups[e.equipment].append(i)
        tables = {
            eq: {
                (tr.from_mode, tr.to_mode): tr.duration
                for tr in req.modes[eq].transitions
            }
            for eq in managed_eq
        }
        initial = {eq: req.modes[eq].initial_mode for eq in managed_eq}
        mode_ctx = _ModeContext(groups, tables, initial)

    t0 = time.perf_counter()

    # Phase 1: minimize makespan.
    model, v = _build_model(req, mode_ctx)
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
    model, v = _build_model(req, mode_ctx)
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
        model, v = _build_model(req, mode_ctx)
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

    # Calibration segments, grouped by equipment in execution order. Because
    # the model places each calibration continuously from the predecessor's
    # cooling release (or at time 0 for the first item), every segment time is
    # determined by the joint start solution and the selected order.
    calibrations: List[Dict[str, Any]] = []
    if mode_ctx is not None:
        for eq in managed_eq:
            ordered = sorted(
                mode_ctx.groups[eq], key=lambda i: (starts[i], exps[i].id)
            )
            prev_i: Optional[int] = None
            for ii in ordered:
                frm = mode_ctx.initial[eq] if prev_i is None else exps[prev_i].mode
                to = exps[ii].mode
                pair = (frm, to)
                if pair not in mode_ctx.tables[eq]:
                    # Feasibility guarantees every distinct-mode pair is
                    # registered; same mode with no row is a free no-op.
                    if frm == to:
                        prev_i = ii
                        continue
                    raise SolverTimeoutError(  # pragma: no cover - impossible in a solution
                        f"missing transition {frm}->{to} for {eq}"
                    )
                dur = mode_ctx.tables[eq][pair]
                if dur == 0:
                    # Zero-cost (possibly explicitly registered) switch:
                    # nothing occupies the machine, so no segment to show.
                    prev_i = ii
                    continue
                cs = (
                    0
                    if prev_i is None
                    else starts[prev_i] + exps[prev_i].duration + exps[prev_i].cooling
                )
                ce = cs + dur
                calibrations.append(
                    CalibrationSegment(
                        equipment=eq,
                        start=cs,
                        finish=ce,
                        duration=dur,
                        from_mode=frm,
                        to_mode=to,
                        predecessor_id=None if prev_i is None else exps[prev_i].id,
                        successor_id=exps[ii].id,
                        wait_before=0,
                        margin=starts[ii] - ce,
                    ).model_dump()
                )
                prev_i = ii

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
        "calibrations": calibrations,
        "solver_time_ms": int((time.perf_counter() - t0) * 1000),
    }

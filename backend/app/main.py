"""FastAPI application: beamline exposure scheduling service."""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .scheduler import InputValidationError, SolverTimeoutError, solve
from .schemas import ScheduleRequest, SolutionPayload

logger = logging.getLogger("beamline")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Beamline Exposure Scheduler", version="1.0.0")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "api"}


@app.post("/api/schedule", response_model=SolutionPayload)
def schedule(req: ScheduleRequest) -> JSONResponse:
    try:
        result = solve(req)
    except InputValidationError as exc:
        # 400: the input itself is wrong (references, bad windows, ...).
        return JSONResponse(
            status_code=400,
            content={
                "feasible": False,
                "reason": "input_error",
                "field_errors": exc.errors,
                "starts": None,
                "finishes": None,
                "makespan": None,
                "sum_starts": None,
                "slacks": None,
                "equipment_orders": None,
                "solver_time_ms": None,
            },
        )
    except SolverTimeoutError as exc:
        # 503: neither feasibility nor infeasibility was proven in time.
        logger.warning("solver timeout: %s", exc)
        return JSONResponse(
            status_code=503,
            content={
                "feasible": False,
                "reason": "solver_timeout",
                "field_errors": [],
                "starts": None,
                "finishes": None,
                "makespan": None,
                "sum_starts": None,
                "slacks": None,
                "equipment_orders": None,
                "solver_time_ms": None,
            },
        )
    if not result["feasible"]:
        # 200 with feasible=false: input is valid, but no executable timing
        # exists. Never return partial starts or a stale plan.
        return JSONResponse(
            status_code=200,
            content={
                "feasible": False,
                "reason": "no_schedule",
                "field_errors": [],
                "starts": None,
                "finishes": None,
                "makespan": None,
                "sum_starts": None,
                "slacks": None,
                "equipment_orders": None,
                "solver_time_ms": None,
            },
        )
    return JSONResponse(status_code=200, content=result)

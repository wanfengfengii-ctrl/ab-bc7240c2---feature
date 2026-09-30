"""Pydantic schemas for the exposure scheduling API."""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class Exposure(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    duration: int = Field(..., ge=1)
    earliest_start: int = Field(..., ge=0)
    latest_start: int = Field(..., ge=0)
    equipment: str = Field(..., min_length=1, max_length=64)
    cooling: int = Field(..., ge=0)
    # Required readout mode for this exposure. It only takes effect when the
    # owning equipment is registered in request.modes; absent/empty keeps the
    # legacy mode-less request fully compatible.
    mode: str = Field(default="", max_length=64)

    @field_validator("id", "equipment", "mode")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip() if v is not None else v


class Link(BaseModel):
    """A separation constraint between two exposures.

    min_gap <= start(to) - (start(from) + duration(from)) <= max_gap
    Cooling time is handled by the scheduler and is independent of links.
    """

    from_id: str
    to_id: str
    min_gap: int = Field(0, ge=0)
    max_gap: Optional[int] = Field(None, ge=0)


class TransitionSpec(BaseModel):
    """One directed readout-mode switch and its exclusive calibration time."""

    from_mode: str = Field(..., min_length=1, max_length=64)
    to_mode: str = Field(..., min_length=1, max_length=64)
    duration: int = Field(..., ge=0)

    @field_validator("from_mode", "to_mode")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class ModeSetup(BaseModel):
    """Per-equipment mode registry.

    initial_mode is the detector's mode before the first exposure on this
    equipment; transitions lists directed switch costs. A pair that is not
    registered is unreachable (it is never treated as zero-cost).
    """

    initial_mode: str = Field(..., min_length=1, max_length=64)
    transitions: List[TransitionSpec] = Field(default_factory=list)

    @field_validator("initial_mode")
    @classmethod
    def _strip_initial(cls, v: str) -> str:
        return v.strip()


class ScheduleRequest(BaseModel):
    horizon: int = Field(10_000, ge=1, le=1_000_000)
    exposures: List[Exposure] = Field(..., min_length=5, max_length=10)
    links: List[Link] = Field(default_factory=list)
    # equipment name -> mode registry; absent/empty => legacy behavior.
    modes: dict[str, ModeSetup] = Field(default_factory=dict)

    @field_validator("links")
    @classmethod
    def _validate_links(cls, links: List[Link]) -> List[Link]:
        seen = set()
        for ln in links:
            key = (ln.from_id, ln.to_id)
            if key in seen:
                raise ValueError(f"duplicate link {ln.from_id}->{ln.to_id}")
            seen.add(key)
        return links


class SlackInfo(BaseModel):
    from_id: str
    to_id: str
    min_gap: int
    max_gap: Optional[int]
    actual_gap: int
    slack: Optional[int] = None


class EquipmentOrder(BaseModel):
    equipment: str
    sequence: List[str]


class CalibrationSegment(BaseModel):
    """One exclusive mode-switch calibration on an equipment timeline."""

    equipment: str
    start: int
    finish: int
    duration: int
    from_mode: str
    to_mode: str
    # Exposure directly before this segment on the same equipment; None marks
    # the initial-mode switch performed before the first exposure.
    predecessor_id: Optional[str]
    successor_id: str
    # Idle time between predecessor cooling release and calibration start
    # (0 for the initial-mode switch, which may start at time 0).
    wait_before: int
    # Spare time between calibration finish and successor exposure start.
    margin: int


class SolutionPayload(BaseModel):
    feasible: bool
    reason: Optional[str] = None
    field_errors: List[str] = Field(default_factory=list)
    starts: Optional[List[int]] = None
    finishes: Optional[List[int]] = None
    makespan: Optional[int] = None
    sum_starts: Optional[int] = None
    slacks: Optional[List[SlackInfo]] = None
    equipment_orders: Optional[List[EquipmentOrder]] = None
    calibrations: Optional[List[CalibrationSegment]] = None
    solver_time_ms: Optional[int] = None

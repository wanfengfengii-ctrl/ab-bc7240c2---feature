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
    # Required detector readout mode. Only meaningful when the request carries
    # readout_modes configuration; otherwise it must stay unset (classic API).
    mode: Optional[str] = Field(None, min_length=1, max_length=64)

    @field_validator("id", "equipment")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("mode")
    @classmethod
    def _strip_mode(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None


class ModeTransition(BaseModel):
    """Directed calibration time for switching one detector's readout mode.

    Switching is asymmetric: an a->b entry says nothing about b->a, which must
    be registered separately. A missing directed pair is unreachable, never a
    zero-duration switch.
    """

    from_mode: str = Field(..., min_length=1, max_length=64)
    to_mode: str = Field(..., min_length=1, max_length=64)
    duration: int = Field(..., ge=0)

    @field_validator("from_mode", "to_mode")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v


class EquipmentModeConfig(BaseModel):
    """Per-equipment registration: initial readout mode and switch table."""

    equipment: str = Field(..., min_length=1, max_length=64)
    initial_mode: str = Field(..., min_length=1, max_length=64)
    transitions: List[ModeTransition] = Field(default_factory=list)

    @field_validator("equipment", "initial_mode")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v


class Link(BaseModel):
    """A separation constraint between two exposures.

    min_gap <= start(to) - (start(from) + duration(from)) <= max_gap
    Cooling time is handled by the scheduler and is independent of links.
    """

    from_id: str
    to_id: str
    min_gap: int = Field(0, ge=0)
    max_gap: Optional[int] = Field(None, ge=0)


class ScheduleRequest(BaseModel):
    horizon: int = Field(10_000, ge=1, le=1_000_000)
    exposures: List[Exposure] = Field(..., min_length=5, max_length=10)
    links: List[Link] = Field(default_factory=list)
    # When None the readout-mode feature is disabled and every request behaves
    # exactly like the classic API. When present, detector mode switching is
    # jointly scheduled with equipment execution order.
    readout_modes: Optional[List[EquipmentModeConfig]] = None

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


class CalibrationSegment(BaseModel):
    """One exclusive calibration segment on one equipment's timeline.

    For the first exposure of an equipment the segment switches out of the
    registered initial mode (prev_exposure_id is null); otherwise it switches
    out of the immediately preceding exposure's mode. Calibration starts right
    after the predecessor's cooling ends (continuous occupation) and
    wait_margin is the idle time left between its completion and the next
    exposure start.
    """

    equipment: str
    prev_exposure_id: Optional[str]
    exposure_id: str
    from_mode: str
    to_mode: str
    switch_duration: int
    cal_start: int
    cal_end: int
    wait_margin: int


class EquipmentOrder(BaseModel):
    equipment: str
    sequence: List[str]
    initial_mode: Optional[str] = None
    modes: Optional[List[str]] = None


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

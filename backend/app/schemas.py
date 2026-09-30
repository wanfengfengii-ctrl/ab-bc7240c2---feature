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

    @field_validator("id", "equipment")
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
    solver_time_ms: Optional[int] = None

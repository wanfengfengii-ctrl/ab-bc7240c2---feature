"""Pytest fixtures and request builders."""
import pytest

from app.schemas import Exposure, Link, ScheduleRequest


def exp(id, duration=2, earliest=0, latest=100, equipment="X", cooling=0):
    return Exposure(
        id=id,
        duration=duration,
        earliest_start=earliest,
        latest_start=latest,
        equipment=equipment,
        cooling=cooling,
    )


def make_request(exposures, links=None, horizon=1000):
    return ScheduleRequest(horizon=horizon, exposures=exposures, links=links or [])


@pytest.fixture
def builder():
    return make_request


@pytest.fixture
def E():
    return exp

"""Drivers connect Chaos Monkey to the platforms whose instances it terminates."""

from chaosmonkey.clock import Clock
from chaosmonkey.drivers.base import Driver, DriverError
from chaosmonkey.drivers.simulator import SimulatorDriver
from chaosmonkey.settings import Settings

__all__ = ["Driver", "DriverError", "SimulatorDriver", "build_driver"]


def build_driver(settings: Settings, clock: Clock) -> Driver:
    if settings.driver == "simulator":
        return SimulatorDriver(settings.simulator_fleet, settings.simulator_state, clock)
    raise ValueError(f"Unknown driver {settings.driver!r}")  # pragma: no cover

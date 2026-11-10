"""What every driver (Kubernetes, AWS, simulator) must provide.

Drivers are deliberately small: list the opted-in targets, and terminate one. There
is no way to run a command on a target. Like Netflix's Chaos Monkey 2.0, this one only
terminates.
"""

from typing import Protocol

from chaosmonkey.domain import Target


class DriverError(Exception):
    """The platform refused or failed the request."""


class Driver(Protocol):
    name: str

    def list_targets(self) -> list[Target]:
        """Every target that carries Chaos Monkey's opt-in label or tag."""
        ...

    def terminate(self, target: Target) -> None:
        """Terminate one target. Raise DriverError if the platform refuses."""
        ...

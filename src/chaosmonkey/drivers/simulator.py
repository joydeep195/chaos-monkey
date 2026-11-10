"""A simulated fleet for local runs, demos and tests. Nothing here touches a real system.

The fleet is described in a TOML file (see fleet.example.toml). Terminated instances
are replaced after `recovery_seconds`, like a Kubernetes ReplicaSet or an AWS
auto-scaling group, unless the group is marked `recovers = false`, which is how a
fragile service is rehearsed.
"""

import json
import os
import tempfile
import tomllib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from chaosmonkey.clock import Clock
from chaosmonkey.domain import Target
from chaosmonkey.drivers.base import DriverError


class SimGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    app: str
    account: str
    region: str
    stack: str = ""
    detail: str = ""
    replicas: int = Field(ge=1, le=1_000)
    recovery_seconds: int = Field(default=60, ge=0, le=86_400)
    recovers: bool = True
    protected: bool = False
    unhealthy: int = Field(default=0, ge=0)

    @property
    def prefix(self) -> str:
        probe = Target("", self.app, self.account, self.region, self.stack, self.detail)
        return f"{self.account}-{self.region}-{probe.cluster}"

    def instance_id(self, serial: int) -> str:
        return f"{self.prefix}-{serial:03d}"


class SimulatorDriver:
    name = "simulator"

    def __init__(self, fleet_path: Path, state_path: Path, clock: Clock) -> None:
        self.fleet_path = fleet_path
        self.state_path = state_path
        self.clock = clock
        self.groups = _load_fleet(fleet_path)

    def list_targets(self) -> list[Target]:
        state = self._read_state()
        now = self.clock.now()
        targets: list[Target] = []
        for group in self.groups:
            records = [r for r in state["terminations"] if r["group"] == group.prefix]
            gone = {r["id"] for r in records}
            alive = [group.instance_id(n) for n in range(1, group.replicas + 1)]
            alive += [
                r["replacement"]
                for r in records
                if r["replacement"]
                and now >= datetime.fromisoformat(r["at"]) + timedelta(seconds=r["recovery"])
            ]
            unhealthy = {group.instance_id(n) for n in range(1, group.unhealthy + 1)}
            targets += [
                Target(
                    id=instance,
                    app=group.app,
                    account=group.account,
                    region=group.region,
                    stack=group.stack,
                    detail=group.detail,
                    healthy=instance not in unhealthy,
                    protected=group.protected,
                )
                for instance in alive
                if instance not in gone
            ]
        return targets

    def terminate(self, target: Target) -> None:
        if target.id not in {t.id for t in self.list_targets()}:
            raise DriverError(f"Instance {target.id} does not exist")
        group = next(
            g
            for g in self.groups
            if (g.app, g.account, g.region, g.stack, g.detail)
            == (target.app, target.account, target.region, target.stack, target.detail)
        )
        state = self._read_state()
        serials: dict[str, int] = state["serials"]
        replacement = None
        if group.recovers:
            serial = serials.get(group.prefix, group.replicas) + 1
            serials[group.prefix] = serial
            replacement = group.instance_id(serial)
        state["terminations"].append(
            {
                "group": group.prefix,
                "id": target.id,
                "at": self.clock.now().isoformat(),
                "recovery": group.recovery_seconds,
                "replacement": replacement,
            }
        )
        self._write_state(state)

    def reset(self) -> None:
        """Bring every simulated instance back."""
        self.state_path.unlink(missing_ok=True)

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {"terminations": [], "serials": {}}
        data: dict[str, Any] = json.loads(self.state_path.read_text(encoding="utf-8"))
        return data

    def _write_state(self, state: dict[str, Any]) -> None:
        # Write to a temporary file and swap it in, so a crash never leaves half a file.
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=self.state_path.parent, suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(state, file, indent=2)
        Path(temporary).replace(self.state_path)


def _load_fleet(path: Path) -> list[SimGroup]:
    if not path.is_file():
        raise DriverError(
            f"Simulated fleet file not found: {path}. Copy fleet.example.toml to {path}."
        )
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    groups = [SimGroup.model_validate(item) for item in data.get("group", [])]
    prefixes = [g.prefix for g in groups]
    if len(prefixes) != len(set(prefixes)):
        raise DriverError(f"{path} lists the same app, account, region and cluster twice")
    return groups

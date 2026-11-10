"""Builds the pieces of Chaos Monkey from settings, in one place for the CLI, API and tests."""

import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cached_property

from chaosmonkey.clock import Clock, SystemClock
from chaosmonkey.db import Database
from chaosmonkey.drivers import Driver, build_driver
from chaosmonkey.executor import Executor
from chaosmonkey.service import ChaosMonkey
from chaosmonkey.settings import Settings


@dataclass
class Runtime:
    settings: Settings
    clock: Clock = field(default_factory=SystemClock)

    @cached_property
    def db(self) -> Database:
        database = Database(self.settings.database_url.get_secret_value())
        database.create_schema()
        return database

    @cached_property
    def service(self) -> ChaosMonkey:
        return ChaosMonkey(self.settings, self.db, self.clock)

    @cached_property
    def driver(self) -> Driver:
        return build_driver(self.settings, self.clock)

    def executor(self) -> Executor:
        return Executor(self.settings, self.db, self.driver, self.clock)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, ready for a log platform."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def configure_logging(*, json_lines: bool, level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        JsonFormatter() if json_lines else logging.Formatter("%(levelname)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

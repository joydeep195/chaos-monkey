"""Command line for operators: `uv run chaosmonkey --help`.

The CLI talks straight to the database, so it is for people who already hold
database access. In development it acts as you with every role (use --as to act as
someone else, for example to try the four-eyes rule). In production it is limited to
reading and to break-glass actions (kill switch on, declare an incident); every other
change goes through the API, where it is tied to a signed-in person.
"""

import getpass
import json
import logging
import signal
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from chaosmonkey.api.auth import issue_dev_token
from chaosmonkey.domain import Actor, AppConfig, ExceptionRule, Grouping, Mode, Role
from chaosmonkey.drivers.base import DriverError
from chaosmonkey.runtime import Runtime, configure_logging
from chaosmonkey.service import ServiceError
from chaosmonkey.settings import Settings, SettingsError, load_settings

app = typer.Typer(
    no_args_is_help=True, add_completion=False, pretty_exceptions_enable=False, help=__doc__
)
apps_cli = typer.Typer(no_args_is_help=True, help="Opt apps in and configure them.")
approvals_cli = typer.Typer(no_args_is_help=True, help="Ask for and decide on going live.")
switch_cli = typer.Typer(no_args_is_help=True, help="Stop everything, or resume.")
incident_cli = typer.Typer(no_args_is_help=True, help="Declare or close an incident.")
freeze_cli = typer.Typer(no_args_is_help=True, help="Periods when nothing may be killed.")
audit_cli = typer.Typer(no_args_is_help=True, help="Read, verify and export the audit log.")
app.add_typer(apps_cli, name="apps")
app.add_typer(approvals_cli, name="approvals")
app.add_typer(switch_cli, name="kill-switch")
app.add_typer(incident_cli, name="incident")
app.add_typer(freeze_cli, name="freeze")
app.add_typer(audit_cli, name="audit")


class Context:
    def __init__(self, config: Path | None, act_as: str | None) -> None:
        self.config = config
        self.act_as = act_as

    @property
    def settings(self) -> Settings:
        return load_settings(self.config)

    def runtime(self) -> Runtime:
        return Runtime(self.settings)

    def actor(self, settings: Settings) -> Actor:
        if settings.is_production:
            if self.act_as:
                fail("--as is not available in production")
            return Actor(
                subject=getpass.getuser(),
                display_name=getpass.getuser(),
                roles=frozenset({Role.VIEWER, Role.OPERATOR}),
                channel="cli",
            )
        subject = self.act_as or getpass.getuser()
        return Actor(subject=subject, display_name=subject, roles=frozenset(Role), channel="cli")


# Accepted date and time formats on the command line, with or without seconds.
TIME_FORMATS = [
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
]


def fail(message: str) -> NoReturn:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def ctx_of(ctx: typer.Context) -> Context:
    found: Context = ctx.obj
    return found


def change_allowed(settings: Settings, *, break_glass: bool = False) -> None:
    if settings.is_production and not break_glass:
        fail(
            "In production, make this change through the API so it is tied to a signed-in "
            "person. The CLI can only read, switch the kill switch on and declare incidents."
        )


def show(rows: list[dict[str, Any]], columns: list[str]) -> None:
    if not rows:
        typer.echo("(none)")
        return
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    typer.echo("  ".join(c.upper().ljust(widths[c]) for c in columns))
    for row in rows:
        typer.echo("  ".join(str(row.get(c, "")).ljust(widths[c]) for c in columns))


def short_time(moment: datetime | None) -> str:
    return moment.isoformat(timespec="minutes") if moment else ""


@app.callback()
def main_options(
    ctx: typer.Context,
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Settings file (default chaosmonkey.toml)")
    ] = None,
    act_as: Annotated[
        str | None, typer.Option("--as", help="Development only: act as this person")
    ] = None,
) -> None:
    ctx.obj = Context(config, act_as)


# ----- overview ---------------------------------------------------------------------------


@app.command()
def status(ctx: typer.Context) -> None:
    """Switches, kill switch, incident and today's plan at a glance."""
    c = ctx_of(ctx)
    runtime = c.runtime()
    settings = runtime.settings
    actor = c.actor(settings)
    service = runtime.service
    switch = service.kill_switch(actor)
    incident = service.incident(actor)
    typer.echo(f"Environment:      {settings.environment}")
    typer.echo(f"Enabled:          {settings.enabled}")
    typer.echo(f"Leashed:          {settings.leashed} (true means every kill is a dry run)")
    typer.echo(f"Scheduling:       {settings.schedule_enabled}")
    typer.echo(
        f"Working hours:    {settings.start_hour:02d}:00-{settings.end_hour:02d}:00 "
        f"{settings.time_zone}, {', '.join(settings.working_days)}"
    )
    typer.echo(f"Allowed accounts: {', '.join(settings.allowed_accounts) or '(none)'}")
    typer.echo(f"Kill switch:      {'ON: ' + switch.reason if switch.engaged else 'off'}")
    typer.echo(f"Incident:         {'OPEN: ' + incident.summary if incident.open else 'none'}")
    typer.echo(f"Apps:             {len(service.list_apps(actor))}")
    if settings.is_production:
        problems = settings.production_problems()
        typer.echo(f"Production ready: {'yes' if not problems else 'NO'}")
        for problem in problems:
            typer.echo(f"  - {problem}")


@app.command()
def init(ctx: typer.Context) -> None:
    """Create the database tables (safe to run again)."""
    runtime = ctx_of(ctx).runtime()
    runtime.db.create_schema()
    typer.echo("Database ready.")


# ----- apps -------------------------------------------------------------------------------


@apps_cli.command("list")
def apps_list(ctx: typer.Context) -> None:
    """Every registered app and whether it would run live."""
    c = ctx_of(ctx)
    runtime = c.runtime()
    views = runtime.service.list_apps(c.actor(runtime.settings))
    show(
        [
            {
                "name": v.config.name,
                "team": v.config.owner_team,
                "enabled": v.config.enabled,
                "mode": v.config.mode.value,
                "runs_as": v.effective_mode.value,
                "mean_days": v.config.mean_days_between_kills,
                "grouping": v.config.grouping.value,
                "version": v.config_version,
            }
            for v in views
        ],
        ["name", "team", "enabled", "mode", "runs_as", "mean_days", "grouping", "version"],
    )


@apps_cli.command("show")
def apps_show(ctx: typer.Context, name: str) -> None:
    """One app in full, as JSON."""
    c = ctx_of(ctx)
    runtime = c.runtime()
    view = runtime.service.get_app(c.actor(runtime.settings), name)
    typer.echo(view.model_dump_json(indent=2))


@apps_cli.command("set")
def apps_set(
    ctx: typer.Context,
    name: str,
    team: Annotated[str | None, typer.Option(help="Owning team (needed for a new app)")] = None,
    enabled: Annotated[bool | None, typer.Option("--enable/--disable")] = None,
    mode: Annotated[Mode | None, typer.Option(help="dry_run or live (live needs approval)")] = None,
    mean_days: Annotated[
        float | None, typer.Option(help="Average working days between kills")
    ] = None,
    min_days: Annotated[int | None, typer.Option(help="Minimum working days between kills")] = None,
    grouping: Annotated[Grouping | None, typer.Option(help="app, stack or cluster")] = None,
    regions_independent: Annotated[
        bool | None, typer.Option("--regions-independent/--regions-linked")
    ] = None,
    min_healthy: Annotated[
        int | None, typer.Option(help="Never leave fewer healthy instances than this")
    ] = None,
    exception: Annotated[
        list[str] | None,
        typer.Option(
            "--except", help="Opt out account/region/stack/detail ('*' for any). Repeatable."
        ),
    ] = None,
    clear_exceptions: Annotated[bool, typer.Option(help="Remove all exceptions")] = False,
) -> None:
    """Create an app or change some of its settings. Any change voids its approval."""
    c = ctx_of(ctx)
    runtime = c.runtime()
    change_allowed(runtime.settings)
    actor = c.actor(runtime.settings)
    service = runtime.service
    try:
        current = service.get_app(actor, name)
        base = current.config.model_dump()
        version: int | None = current.config_version
    except ServiceError:
        if team is None:
            fail("A new app needs --team")
        base = {"name": name}
        version = None
    updates: dict[str, Any] = {
        "owner_team": team,
        "enabled": enabled,
        "mode": mode,
        "mean_days_between_kills": mean_days,
        "min_days_between_kills": min_days,
        "grouping": grouping,
        "regions_independent": regions_independent,
        "min_healthy": min_healthy,
    }
    base.update({k: v for k, v in updates.items() if v is not None})
    rules = [] if clear_exceptions else list(base.get("exceptions", ()))
    for text in exception or []:
        parts = text.split("/")
        if len(parts) != 4:
            fail(f"--except needs account/region/stack/detail, got {text!r}")
        rules.append(
            ExceptionRule(account=parts[0], region=parts[1], stack=parts[2], detail=parts[3])
        )
    base["exceptions"] = rules
    try:
        config = AppConfig.model_validate(base)
    except ValueError as error:
        fail(str(error))
    view = service.save_app(actor, config, version)
    typer.echo(f"Saved {name} (version {view.config_version}). {view.mode_reason}.")


# ----- approvals --------------------------------------------------------------------------


@approvals_cli.command("list")
def approvals_list(
    ctx: typer.Context, status_filter: Annotated[str | None, typer.Option("--status")] = None
) -> None:
    c = ctx_of(ctx)
    runtime = c.runtime()
    views = runtime.service.list_approvals(c.actor(runtime.settings), status_filter)
    show(
        [
            {
                "id": v.id,
                "app": v.app_name,
                "version": v.config_version,
                "status": v.status,
                "requested_by": v.requested_by,
                "decided_by": v.decided_by or "",
                "expires": short_time(v.expires_at),
            }
            for v in views
        ],
        ["id", "app", "version", "status", "requested_by", "decided_by", "expires"],
    )


@approvals_cli.command("request")
def approvals_request(
    ctx: typer.Context, app_name: str, reason: Annotated[str, typer.Option(prompt=True)]
) -> None:
    """Ask a second person to approve this app's current configuration for live runs."""
    c = ctx_of(ctx)
    runtime = c.runtime()
    change_allowed(runtime.settings)
    view = runtime.service.request_approval(c.actor(runtime.settings), app_name, reason)
    typer.echo(f"Request {view.id} is waiting for an approver.")


@approvals_cli.command("approve")
def approvals_approve(
    ctx: typer.Context, approval_id: int, note: Annotated[str, typer.Option(prompt=True)]
) -> None:
    c = ctx_of(ctx)
    runtime = c.runtime()
    change_allowed(runtime.settings)
    actor = c.actor(runtime.settings)
    view = runtime.service.decide_approval(actor, approval_id, approve=True, note=note)
    typer.echo(f"Approved request {view.id} until {short_time(view.expires_at)}.")


@approvals_cli.command("reject")
def approvals_reject(
    ctx: typer.Context, approval_id: int, note: Annotated[str, typer.Option(prompt=True)]
) -> None:
    c = ctx_of(ctx)
    runtime = c.runtime()
    change_allowed(runtime.settings)
    actor = c.actor(runtime.settings)
    runtime.service.decide_approval(actor, approval_id, approve=False, note=note)
    typer.echo(f"Rejected request {approval_id}.")


# ----- kill switch, incidents, freeze windows ---------------------------------------------


@switch_cli.command("status")
def switch_status(ctx: typer.Context) -> None:
    c = ctx_of(ctx)
    runtime = c.runtime()
    view = runtime.service.kill_switch(c.actor(runtime.settings))
    typer.echo(f"ON: {view.reason} (by {view.by} at {view.at})" if view.engaged else "off")


@switch_cli.command("engage")
def switch_engage(ctx: typer.Context, reason: Annotated[str, typer.Option(prompt=True)]) -> None:
    """Stop all chaos now. Works in production too (break glass)."""
    c = ctx_of(ctx)
    runtime = c.runtime()
    change_allowed(runtime.settings, break_glass=True)
    runtime.service.engage_kill_switch(c.actor(runtime.settings), reason)
    typer.secho("Kill switch is ON. Nothing will be terminated.", fg=typer.colors.YELLOW)


@switch_cli.command("release")
def switch_release(ctx: typer.Context, reason: Annotated[str, typer.Option(prompt=True)]) -> None:
    c = ctx_of(ctx)
    runtime = c.runtime()
    change_allowed(runtime.settings)
    runtime.service.release_kill_switch(c.actor(runtime.settings), reason)
    typer.echo("Kill switch is off.")


@incident_cli.command("status")
def incident_status(ctx: typer.Context) -> None:
    c = ctx_of(ctx)
    runtime = c.runtime()
    view = runtime.service.incident(c.actor(runtime.settings))
    typer.echo(f"OPEN: {view.summary} (by {view.by} at {view.at})" if view.open else "none")


@incident_cli.command("declare")
def incident_declare(
    ctx: typer.Context, summary: Annotated[str, typer.Option(prompt=True)]
) -> None:
    """Pause all chaos while an incident is handled. Works in production too."""
    c = ctx_of(ctx)
    runtime = c.runtime()
    change_allowed(runtime.settings, break_glass=True)
    runtime.service.declare_incident(c.actor(runtime.settings), summary)
    typer.echo("Incident declared. No kills until it is closed.")


@incident_cli.command("close")
def incident_close(ctx: typer.Context, note: Annotated[str, typer.Option(prompt=True)]) -> None:
    c = ctx_of(ctx)
    runtime = c.runtime()
    change_allowed(runtime.settings)
    runtime.service.close_incident(c.actor(runtime.settings), note)
    typer.echo("Incident closed.")


@freeze_cli.command("list")
def freeze_list(ctx: typer.Context) -> None:
    c = ctx_of(ctx)
    runtime = c.runtime()
    views = runtime.service.freeze_windows(c.actor(runtime.settings))
    show(
        [
            {
                "id": v.id,
                "name": v.name,
                "starts": short_time(v.starts_at),
                "ends": short_time(v.ends_at),
                "by": v.created_by,
            }
            for v in views
        ],
        ["id", "name", "starts", "ends", "by"],
    )


@freeze_cli.command("add")
def freeze_add(
    ctx: typer.Context,
    name: str,
    start: Annotated[
        datetime, typer.Option(formats=TIME_FORMATS, help="Start, e.g. 2026-11-30T09:00")
    ],
    end: Annotated[datetime, typer.Option(formats=TIME_FORMATS, help="End, e.g. 2026-12-02T18:00")],
) -> None:
    """Add a freeze window. Times without a zone are read in the configured time zone."""
    c = ctx_of(ctx)
    runtime = c.runtime()
    change_allowed(runtime.settings)
    zone = runtime.settings.zone
    start = start if start.tzinfo else start.replace(tzinfo=zone)
    end = end if end.tzinfo else end.replace(tzinfo=zone)
    view = runtime.service.add_freeze_window(c.actor(runtime.settings), name, start, end)
    typer.echo(f"Freeze window {view.id} added.")


@freeze_cli.command("remove")
def freeze_remove(ctx: typer.Context, window_id: int) -> None:
    c = ctx_of(ctx)
    runtime = c.runtime()
    change_allowed(runtime.settings)
    runtime.service.remove_freeze_window(c.actor(runtime.settings), window_id)
    typer.echo(f"Freeze window {window_id} removed.")


# ----- history and audit ------------------------------------------------------------------


@app.command()
def schedule(
    ctx: typer.Context, day: Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"])] = None
) -> None:
    """Planned kills and what happened to each."""
    c = ctx_of(ctx)
    runtime = c.runtime()
    on: date | None = day.date() if day else None
    views = runtime.service.schedule(c.actor(runtime.settings), on)
    show(
        [
            {
                "id": v.id,
                "run_at": short_time(v.run_at),
                "group": v.group,
                "status": v.status,
                "outcome": (v.outcome or "")[:90],
            }
            for v in views
        ],
        ["id", "run_at", "group", "status", "outcome"],
    )


@app.command()
def terminations(ctx: typer.Context, limit: int = 20) -> None:
    """Recent kills (live and dry run) and whether each group recovered."""
    c = ctx_of(ctx)
    runtime = c.runtime()
    views = runtime.service.terminations(c.actor(runtime.settings), limit)
    show(
        [
            {
                "at": short_time(v.executed_at),
                "target": v.target_id,
                "mode": v.mode,
                "recovery": v.recovery_status,
            }
            for v in views
        ],
        ["at", "target", "mode", "recovery"],
    )


@audit_cli.command("list")
def audit_list(ctx: typer.Context, after: int = 0, limit: int = 50) -> None:
    c = ctx_of(ctx)
    runtime = c.runtime()
    views = runtime.service.audit_log(c.actor(runtime.settings), after, limit)
    show(
        [
            {
                "seq": v.seq,
                "at": short_time(v.at),
                "actor": v.actor,
                "action": v.action,
                "subject": v.subject[:50],
            }
            for v in views
        ],
        ["seq", "at", "actor", "action", "subject"],
    )


@audit_cli.command("verify")
def audit_verify(
    ctx: typer.Context,
    expect_head: Annotated[
        str | None, typer.Option(help="SEQ:HASH recorded earlier outside the database")
    ] = None,
) -> None:
    """Recompute the hash chain. Exits with 1 if anything was altered or removed."""
    c = ctx_of(ctx)
    runtime = c.runtime()
    head = None
    if expect_head:
        seq, _, digest = expect_head.partition(":")
        if not seq.isdigit() or len(digest) != 64:
            fail("--expect-head must look like 42:<64 hex characters>")
        head = (int(seq), digest)
    report = runtime.service.verify_audit(c.actor(runtime.settings), head)
    if report.ok:
        typer.secho(f"Audit log intact: {report.entries} entries.", fg=typer.colors.GREEN)
        typer.echo(f"Head (record this somewhere safe): {report.head_seq}:{report.head_hash}")
    else:
        fail(f"AUDIT LOG BROKEN: {report.problem}")


@audit_cli.command("export")
def audit_export(ctx: typer.Context, out: Path) -> None:
    """Write the whole log as JSON lines, for write-once storage or an auditor."""
    c = ctx_of(ctx)
    runtime = c.runtime()
    actor = c.actor(runtime.settings)
    count, after = 0, 0
    with out.open("w", encoding="utf-8", newline="\n") as file:
        while page := runtime.service.audit_log(actor, after, 500):
            for entry in page:
                file.write(json.dumps(entry.model_dump(mode="json"), ensure_ascii=False) + "\n")
            count += len(page)
            after = page[-1].seq
    typer.echo(f"Wrote {count} entries to {out}.")


# ----- running ----------------------------------------------------------------------------


@app.command()
def run(
    ctx: typer.Context,
    once: Annotated[bool, typer.Option(help="Do a single tick and stop")] = False,
) -> None:
    """Run the scheduler and executor (without the API)."""
    c = ctx_of(ctx)
    runtime = c.runtime()
    runtime.settings.ensure_production_ready()
    configure_logging(json_lines=runtime.settings.is_production)
    executor = runtime.executor()
    if once:
        if not executor.take_lease():
            fail("Another Chaos Monkey executor is running; not starting a second one.")
        try:
            report = executor.tick()
        finally:
            executor.release()
        for line in report.lines or ["Nothing to do."]:
            typer.echo(line)
        return
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    executor.run_forever(stop)


@app.command()
def serve(ctx: typer.Context) -> None:
    """Run the API (and, unless api.run_executor = false, the executor)."""
    import uvicorn

    from chaosmonkey.api.app import create_api

    runtime = ctx_of(ctx).runtime()
    settings = runtime.settings
    settings.ensure_production_ready()
    configure_logging(json_lines=settings.is_production)
    api = settings.api
    uvicorn.run(
        create_api(runtime),
        host=api.host,
        port=api.port,
        server_header=False,
        date_header=False,
        proxy_headers=api.behind_tls_proxy,
        ssl_certfile=str(api.tls_cert_file) if api.tls_cert_file else None,
        ssl_keyfile=str(api.tls_key_file) if api.tls_key_file else None,
        log_config=None,
    )


@app.command()
def fleet(ctx: typer.Context) -> None:
    """Show the simulated fleet as Chaos Monkey sees it now."""
    runtime = ctx_of(ctx).runtime()
    targets = runtime.driver.list_targets()
    show(
        [
            {
                "id": t.id,
                "app": t.app,
                "account": t.account,
                "region": t.region,
                "cluster": t.cluster,
                "healthy": t.healthy,
                "protected": t.protected,
            }
            for t in targets
        ],
        ["id", "app", "account", "region", "cluster", "healthy", "protected"],
    )


@app.command("dev-token")
def dev_token(
    ctx: typer.Context,
    subject: Annotated[str, typer.Option(help="Who the token is for")],
    role: Annotated[list[Role], typer.Option(help="Repeat for several roles")],
    team: Annotated[list[str] | None, typer.Option(help="Repeat for several teams")] = None,
    hours: Annotated[float, typer.Option(min=0.1, max=24)] = 8,
) -> None:
    """Development only: make a sign-in token for trying the API locally."""
    runtime = ctx_of(ctx).runtime()
    settings = runtime.settings
    if settings.is_production or not settings.api.dev_auth:
        fail("Development tokens need environment = 'development' and api.dev_auth = true.")
    token = issue_dev_token(
        settings,
        subject=subject,
        roles=role,
        teams=team or [],
        now=runtime.clock.now(),
        hours=hours,
    )
    typer.echo(token)


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    try:
        app()
    except SettingsError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise SystemExit(2) from error
    except ServiceError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise SystemExit(1) from error
    except DriverError as error:
        typer.secho(f"Driver: {error}", fg=typer.colors.RED, err=True)
        raise SystemExit(1) from error

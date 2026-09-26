"""``cashmaxx`` command line: onboard, guard, status, freeze, unfreeze, settings, workspace."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import typer

from cashmaxx.config import CashmaxxAgentConfig, CashmaxxSettings
from cashmaxx.guard.client import JSON, GuardClient, GuardError, GuardUnavailable

main = typer.Typer(
    name="cashmaxx",
    help="Cashmaxx: an agent that tries to earn more than it costs, with a guarded USDC wallet.",
    no_args_is_help=True,
    add_completion=False,
)
settings_app = typer.Typer(help="Show or change the owner's Cashmaxx settings.", no_args_is_help=True)
main.add_typer(settings_app, name="settings")

T = TypeVar("T")
ConfigOption = typer.Option(None, "--config", "-c", help="nanobot config file")


def _agent_config(config_path: str | None) -> CashmaxxAgentConfig:
    from nanobot.config.loader import get_config_path, load_config, set_config_path

    path = Path(config_path).expanduser() if config_path else get_config_path()
    if config_path:
        set_config_path(path)
    if not path.exists():
        typer.echo(f"No nanobot config at {path}. Run `cashmaxx onboard` first.", err=True)
        raise typer.Exit(1)
    cfg = load_config(path).cashmaxx
    if cfg is None:
        typer.echo("Cashmaxx is not configured in the nanobot config. Run `cashmaxx onboard`.",
                   err=True)
        raise typer.Exit(1)
    return cfg


def _client(
    cfg: CashmaxxAgentConfig, *, owner_session: str | None = None, anonymous: bool = False
) -> GuardClient:
    """Agent-token client by default; owner calls carry only the session, the PIN call nothing."""
    from cashmaxx import plugin

    return plugin.guard_client(cfg, owner_session=owner_session,
                               use_agent_token=owner_session is None and not anonymous)


def _run(coro_fn: Callable[[], Awaitable[T]]) -> T:
    try:
        return asyncio.run(coro_fn())  # type: ignore[arg-type]
    except GuardUnavailable:
        typer.echo("Guard unreachable — spending is disabled. Start it with `cashmaxx guard`.",
                   err=True)
        raise typer.Exit(2) from None
    except GuardError as exc:
        typer.echo(f"Guard error ({exc.code}): {exc.message}", err=True)
        raise typer.Exit(1) from None


async def _owner_call(cfg: CashmaxxAgentConfig, pin: str, fn: Callable[[GuardClient], Awaitable[JSON]]) -> JSON:
    async with _client(cfg, anonymous=True) as anon:
        session = (await anon.owner_session(pin))["session"]
    async with _client(cfg, owner_session=str(session)) as owner:
        return await fn(owner)


def _ask_pin(pin: str | None) -> str:
    return pin if pin is not None else typer.prompt("Owner PIN", hide_input=True)


@main.command()
def onboard(
    config: str | None = ConfigOption,
    force: bool = typer.Option(False, "--force", help="Overwrite workspace files you edited"),
) -> None:
    """Set up Cashmaxx: budget, rules, wallet, approvals bot, PIN and workspace."""
    from cashmaxx.onboarding import run_onboarding

    try:
        run_onboarding(
            nanobot_config_path=Path(config).expanduser() if config else None,
            force_workspace=force,
        )
    except KeyboardInterrupt:
        typer.echo("\nOnboarding cancelled. Nothing more was written.")
        raise typer.Exit(130) from None


@main.command()
def guard(
    guard_config: str | None = typer.Option(None, "--guard-config", help="Path to guard.json"),
) -> None:
    """Run the guard (wallet policy, approvals, ledger) in the foreground."""
    from cashmaxx.guard.app import run  # lazy: the guard is a separate component

    run(Path(guard_config).expanduser() if guard_config else None)


@main.command()
def status(config: str | None = ConfigOption) -> None:
    """Wallet, balance, budget, 7-day P&L, pending approvals and frozen state."""
    cfg = _agent_config(config)

    async def _go() -> str:
        from cashmaxx.plugin.commands import status_text
        from nanobot.config.loader import load_config

        async with _client(cfg) as client:
            return await status_text(client, load_config().workspace_path)

    typer.echo(_run(_go))


@main.command()
def freeze(
    reason: str = typer.Argument("frozen from the CLI", help="Why"),
    config: str | None = ConfigOption,
) -> None:
    """Pull the kill switch: stop all spending."""
    cfg = _agent_config(config)

    async def _go() -> JSON:
        async with _client(cfg) as client:
            return await client.freeze(reason)

    _run(_go)
    typer.echo("Cashmaxx is frozen. Unfreeze with `cashmaxx unfreeze` (needs the owner PIN).")


@main.command()
def unfreeze(
    config: str | None = ConfigOption,
    pin: str | None = typer.Option(None, "--pin", help="Owner PIN (prompted if omitted)"),
) -> None:
    """Clear the freeze (owner only)."""
    cfg = _agent_config(config)
    owner_pin = _ask_pin(pin)
    _run(lambda: _owner_call(cfg, owner_pin, lambda c: c.unfreeze()))
    typer.echo("Cashmaxx is unfrozen.")


@settings_app.command("show")
def settings_show(config: str | None = ConfigOption) -> None:
    """Print the current settings (no secrets)."""
    cfg = _agent_config(config)

    async def _go() -> JSON:
        async with _client(cfg) as client:
            return await client.settings()

    typer.echo(json.dumps(_run(_go).get("settings", {}), indent=2, sort_keys=True))


def parse_setting_value(key: str, raw: str) -> tuple[str, Any]:
    """Map ``key=value`` from the CLI to a validated settings patch entry (camelCase key)."""
    fields = CashmaxxSettings.model_fields
    by_alias = {(f.alias or name): name for name, f in fields.items()}
    name = key if key in fields else by_alias.get(key)
    if name is None:
        raise typer.BadParameter(f"unknown setting {key!r}. Known: {', '.join(sorted(by_alias))}")
    if name in {"frozen", "frozen_reason"}:
        raise typer.BadParameter("use `cashmaxx freeze` / `cashmaxx unfreeze`")
    try:
        value: Any = json.loads(raw)
    except json.JSONDecodeError:
        value = raw
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = raw  # keep decimals exact; pydantic parses numeric strings
    if name in {"earning_methods", "rules", "allowlist"} and isinstance(value, str):
        value = [v.strip() for v in value.split(",") if v.strip()]
    alias = fields[name].alias or name
    return alias, value


@settings_app.command("set")
def settings_set(
    key: str = typer.Argument(..., help="Setting name, e.g. dailyCapUsd or earningMethods"),
    value: str = typer.Argument(..., help='Value, e.g. 25, true, or "bounties,x402_apis"'),
    config: str | None = ConfigOption,
    pin: str | None = typer.Option(None, "--pin", help="Owner PIN (prompted if omitted)"),
) -> None:
    """Change one setting (owner only). The workspace rules are re-rendered afterwards."""
    cfg = _agent_config(config)
    alias, parsed = parse_setting_value(key, value)
    owner_pin = _ask_pin(pin)
    result = _run(lambda: _owner_call(cfg, owner_pin, lambda c: c.update_settings({alias: parsed})))
    _refresh_workspace(result)
    typer.echo(f"{alias} updated.")
    if result.get("restart_required"):
        typer.echo("Restart the guard for this change to take effect.")


def _refresh_workspace(result: JSON) -> None:
    from cashmaxx.plugin.workspace import install_workspace
    from nanobot.config.loader import load_config

    try:
        settings = CashmaxxSettings.model_validate(result.get("settings", result))
    except Exception:
        return
    report = install_workspace(load_config().workspace_path, settings)
    if report.skipped:
        typer.echo("Not updated (edited by you): " + ", ".join(report.skipped))


@main.command()
def workspace(
    config: str | None = ConfigOption,
    force: bool = typer.Option(False, "--force", help="Overwrite files you edited"),
) -> None:
    """Re-install the mission templates and skills from the guard's current settings."""
    cfg = _agent_config(config)

    async def _go() -> JSON:
        async with _client(cfg) as client:
            return await client.settings()

    from cashmaxx.plugin.workspace import install_workspace
    from nanobot.config.loader import load_config

    settings = CashmaxxSettings.model_validate(_run(_go).get("settings", {}))
    report = install_workspace(load_config().workspace_path, settings, force=force)
    for path, outcome in report.results.items():
        typer.echo(f"{outcome:9} {path}")


if __name__ == "__main__":
    main()

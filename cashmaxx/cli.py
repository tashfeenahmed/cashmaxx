"""``cashmaxx`` command line: onboard, guard, status, freeze, unfreeze, settings, workspace,
integrations, connect, disconnect, test."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast

import click
import typer

from cashmaxx.config import CashmaxxAgentConfig, CashmaxxSettings
from cashmaxx.guard.client import JSON, GuardClient, GuardError, GuardUnavailable
from cashmaxx.integrations_catalog import INTEGRATIONS, IntegrationSpec, get_spec

if TYPE_CHECKING:
    from nanobot.config.schema import Config

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


def _load(config_path: str | None) -> tuple[Config, Path, CashmaxxAgentConfig]:
    """nanobot's full config, its path and the Cashmaxx part (exits when missing)."""
    from nanobot.config.loader import get_config_path, load_config, set_config_path

    path = Path(config_path).expanduser() if config_path else get_config_path()
    if config_path:
        set_config_path(path)
    if not path.exists():
        typer.echo(f"No nanobot config at {path}. Run `cashmaxx onboard` first.", err=True)
        raise typer.Exit(1)
    config = load_config(path)
    cfg = config.cashmaxx
    if cfg is None:
        typer.echo("Cashmaxx is not configured in the nanobot config. Run `cashmaxx onboard`.",
                   err=True)
        raise typer.Exit(1)
    return config, path, cfg


def _agent_config(config_path: str | None) -> CashmaxxAgentConfig:
    return _load(config_path)[2]


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


# --- integrations ------------------------------------------------------------------------------

PinOption = typer.Option(None, "--pin", help="Owner PIN (prompted if omitted)")
IdArgument = typer.Argument(..., help="Integration id, e.g. gmail (see `cashmaxx integrations`)")
_SOCIAL = ("bluesky", "x", "reddit")


class TyperPrompter:
    """The onboarding ``Prompter`` on plain terminal prompts (works with piped input)."""

    def say(self, text: str) -> None:
        from rich.text import Text

        typer.echo(Text.from_markup(text).plain)

    def select(self, message: str, choices: list[tuple[str, str]], default: str | None = None) -> str:
        values = [value for value, _ in choices]
        return str(typer.prompt(message, default=default, type=click.Choice(values)))

    def checkbox(self, message: str, choices: list[tuple[str, str]], checked: set[str]) -> list[str]:
        values = [value for value, _ in choices]
        raw = str(typer.prompt(f"{message} (comma separated: {', '.join(values)})",
                               default=",".join(sorted(checked)), show_default=bool(checked)))
        return [v.strip() for v in raw.split(",") if v.strip() in values]

    def text(self, message: str, default: str = "",
             validate: Callable[[str], str | None] | None = None) -> str:
        while True:
            value = str(typer.prompt(message, default=default, show_default=bool(default)))
            problem = validate(value) if validate is not None else None
            if problem is None:
                return value
            typer.echo(problem, err=True)

    def secret(self, message: str) -> str:
        return str(typer.prompt(message, default="", hide_input=True, show_default=False))

    def confirm(self, message: str, default: bool = True) -> bool:
        return typer.confirm(message, default=default)

    def autocomplete(self, message: str, choices: list[str], default: str) -> str:
        return self.text(message, default)


def _spec(integration_id: str) -> IntegrationSpec:
    try:
        return get_spec(integration_id)
    except KeyError:
        known = ", ".join(s.id for s in INTEGRATIONS)
        raise typer.BadParameter(f"unknown integration {integration_id!r}. Known: {known}") from None


def _owner_login(cfg: CashmaxxAgentConfig, pin: str) -> str:
    async def _go() -> str:
        async with _client(cfg, anonymous=True) as anon:
            return str((await anon.owner_session(pin))["session"])

    return _run(_go)


def _as_owner(cfg: CashmaxxAgentConfig, session: str,
              fn: Callable[[GuardClient], Awaitable[JSON]]) -> JSON:
    async def _go() -> JSON:
        async with _client(cfg, owner_session=session) as owner:
            return await fn(owner)

    return _run(_go)


def _items(payload: JSON) -> dict[str, dict[str, Any]]:
    raw: object = payload.get("integrations", [])
    items: dict[str, dict[str, Any]] = {}
    if isinstance(raw, list):
        for entry in cast(list[object], raw):
            if isinstance(entry, dict):
                item = {str(k): v for k, v in cast(dict[object, Any], entry).items()}
                items[str(item.get("id", ""))] = item
    return items


def _yes_no(value: object) -> str:
    return "yes" if value is True else "no" if value is False else "?"


def _last_test(item: dict[str, Any]) -> str:
    test: object = item.get("last_test")
    if not isinstance(test, dict):
        return "-"
    data = cast(dict[str, Any], test)
    status = "ok" if data.get("ok") else "failed"
    message = str(data.get("message") or "")
    return f"{status}: {message}"[:60] if message else status


@main.command()
def integrations(
    config: str | None = ConfigOption,
    pin: str | None = typer.Option(None, "--pin", help="Owner PIN: adds the last test results"),
) -> None:
    """List integrations: which are connected and how their last test went."""
    from rich.console import Console
    from rich.table import Table

    from cashmaxx.agent_integrations import agent_integration_item

    nb_config, _, cfg = _load(config)
    if pin:
        session = _owner_login(cfg, pin)
        guard_items = _items(_as_owner(cfg, session, lambda c: c.owner_integrations()))
    else:
        async def _go() -> JSON:
            async with _client(cfg) as client:
                return await client.integrations()

        guard_items = _items(_run(_go))

    table = Table("id", "label", "kind", "connected", "last test")
    for spec in INTEGRATIONS:
        if spec.kind == "agent":
            item = agent_integration_item(nb_config, spec.id, owner=False)
        else:
            item = guard_items.get(spec.id, {})
        table.add_row(spec.id, spec.label, spec.kind, _yes_no(item.get("connected")),
                      _last_test(item) if pin else "-")
    Console(width=120).print(table)


def _print_test(label: str, result: JSON) -> bool:
    ok = bool(result.get("ok"))
    message = str(result.get("message") or "")
    typer.echo(f"{label} test {'passed' if ok else 'FAILED'}" + (f": {message}" if message else ""))
    return ok


def _after_connect(cfg: CashmaxxAgentConfig, session: str, spec: IntegrationSpec) -> None:
    """Offer the settings that switch the new capability on."""
    from cashmaxx.onboarding import GMAIL_WARMUP_TEXT, SOCIAL_CAP_TEXT

    patch: JSON = {}
    if spec.id in ("gmail", "agentmail"):
        if spec.id == "gmail":
            typer.echo(GMAIL_WARMUP_TEXT)
        if typer.confirm(f"Send the agent's email from {spec.label} (emailProvider)?", default=True):
            patch["emailProvider"] = spec.id
    elif spec.id == "hosting":
        if typer.confirm("Turn on public hosting (hostingEnabled)?", default=False):
            patch["hostingEnabled"] = True
    elif spec.id in _SOCIAL:
        typer.echo(SOCIAL_CAP_TEXT)
    if patch:
        result = _as_owner(cfg, session, lambda c: c.update_settings(patch))
        _refresh_workspace(result)
        typer.echo(", ".join(patch) + " updated.")


@main.command()
def connect(
    integration_id: str = IdArgument,
    config: str | None = ConfigOption,
    pin: str | None = PinOption,
) -> None:
    """Connect or update an integration (owner only), then test it."""
    from cashmaxx.onboarding import collect_integration_fields

    spec = _spec(integration_id)
    nb_config, path, cfg = _load(config)
    prompter = TyperPrompter()
    prompter.say(f"{spec.label}: {spec.summary}"
                 + (f" Credentials: {spec.docs_url}" if spec.docs_url else ""))

    if spec.kind == "agent":
        from cashmaxx.agent_integrations import agent_integration_item

        item = agent_integration_item(nb_config, spec.id, owner=True)
        current = {str(f["name"]): f for f in cast(list[dict[str, Any]], item["fields"])}
        values = collect_integration_fields(prompter, spec, current,
                                            connected=bool(item["connected"]))
        session = _owner_login(cfg, _ask_pin(pin))
        _connect_agent(cfg, session, nb_config, path, spec, values)
        return

    async def _listing() -> JSON:
        async with _client(cfg) as client:
            return await client.integrations()

    connected = _items(_run(_listing)).get(spec.id, {}).get("connected") is True
    values = collect_integration_fields(prompter, spec, connected=connected)
    secret = {f.name for f in spec.fields if f.secret}
    # An empty secret keeps the stored one; an empty plain field is left out so it is kept too.
    fields = {k: v for k, v in values.items() if v or (k in secret and connected)}
    session = _owner_login(cfg, _ask_pin(pin))
    _as_owner(cfg, session, lambda c: c.update_integration(spec.id, fields))
    typer.echo(f"{spec.label} saved.")
    _print_test(spec.label, _as_owner(cfg, session, lambda c: c.test_integration(spec.id)))
    _after_connect(cfg, session, spec)


def _connect_agent(cfg: CashmaxxAgentConfig, session: str, nb_config: Config, path: Path,
                   spec: IntegrationSpec, values: dict[str, str]) -> None:
    from cashmaxx.agent_integrations import AgentIntegrationError, apply_agent_integration
    from nanobot.config.loader import save_config

    _as_owner(cfg, session, lambda c: c.owner_check())
    try:
        server = apply_agent_integration(nb_config, spec.id, values)
    except AgentIntegrationError as exc:
        typer.echo(f"Could not configure {spec.label}: {exc}", err=True)
        raise typer.Exit(1) from None
    save_config(nb_config, path)
    typer.echo(f"{spec.label} saved as MCP server '{server}' in {path}. The gateway picks it up "
               "on its next MCP reload (or restart `nanobot gateway`).")


@main.command()
def disconnect(
    integration_id: str = IdArgument,
    config: str | None = ConfigOption,
    pin: str | None = PinOption,
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation"),
) -> None:
    """Disconnect an integration and forget its credentials (owner only)."""
    spec = _spec(integration_id)
    nb_config, path, cfg = _load(config)
    if not yes and not typer.confirm(f"Disconnect {spec.label} and delete its credentials?",
                                     default=False):
        raise typer.Exit(1)
    session = _owner_login(cfg, _ask_pin(pin))
    if spec.kind == "guard":
        _as_owner(cfg, session, lambda c: c.remove_integration(spec.id))
        typer.echo(f"{spec.label} disconnected.")
        return

    from cashmaxx.agent_integrations import remove_agent_integration
    from nanobot.config.loader import save_config

    _as_owner(cfg, session, lambda c: c.owner_check())
    removed = remove_agent_integration(nb_config, spec.id)
    if not removed:
        typer.echo(f"{spec.label} was not connected.")
        return
    save_config(nb_config, path)
    typer.echo(f"{spec.label} disconnected (removed MCP server {', '.join(removed)}). The gateway "
               "drops it on its next MCP reload or restart.")


@main.command("test")
def test_cmd(
    integration_id: str = IdArgument,
    config: str | None = ConfigOption,
    pin: str | None = PinOption,
) -> None:
    """Run a live check of an integration (owner only for guard integrations)."""
    spec = _spec(integration_id)
    nb_config, _, cfg = _load(config)
    if spec.kind == "agent":
        from cashmaxx.agent_integrations import active_server

        server = active_server(nb_config, spec.id)
        if server is None:
            typer.echo(f"{spec.label} is not connected. Run `cashmaxx connect {spec.id}`.")
            raise typer.Exit(1)
        typer.echo(f"{spec.label} is configured as MCP server '{server}'. The gateway connects "
                   "to it when it loads MCP servers; check `nanobot gateway` logs for errors.")
        return
    session = _owner_login(cfg, _ask_pin(pin))
    if not _print_test(spec.label, _as_owner(cfg, session, lambda c: c.test_integration(spec.id))):
        raise typer.Exit(1)


if __name__ == "__main__":
    main()

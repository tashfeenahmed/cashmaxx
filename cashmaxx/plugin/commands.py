"""Cashmaxx slash commands. They run without the LLM.

- ``/cashmaxx``: status (wallet, balance, available budget, 7-day P&L, pending approvals, frozen).
- ``/pause`` and ``/resume``: stop or restart the money loop by toggling the Cashmaxx block in
  ``HEARTBEAT.md`` (see ``workspace.set_loop_paused``). Other heartbeat tasks are unaffected.
- ``/freeze [reason]``: pulls the guard's kill switch. Anyone may freeze; only the owner unfreezes.

There is deliberately no ``/approve``. Approvals happen only in the guard's own Telegram bot or in
the WebUI with the owner PIN, so a hijacked agent chat can never approve a payment.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from cashmaxx import plugin
from cashmaxx.guard.client import GuardClient, GuardError, GuardUnavailable
from cashmaxx.plugin import workspace as ws
from nanobot.bus.events import OutboundMessage

if TYPE_CHECKING:
    from nanobot.command.router import CommandContext, CommandRouter

GUARD_DOWN = "Guard unreachable — spending is disabled."
NOT_CONFIGURED = "Cashmaxx is not configured. Run `cashmaxx onboard`."


def _reply(ctx: CommandContext, content: str) -> OutboundMessage:
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


def _workspace(ctx: CommandContext) -> Path:
    return plugin.state.workspace or Path(ctx.loop.workspace)


async def status_text(client: GuardClient, workspace: Path) -> str:
    health, wallet, ledger, approvals = await asyncio.gather(
        client.health(), client.wallet(), client.ledger("7d"), client.approvals("pending"),
    )
    items: object = approvals.get("approvals")
    pending = [
        cast(dict[str, Any], item) for item in cast(list[object], items)
        if isinstance(item, dict)
    ] if isinstance(items, list) else []
    paused = ws.loop_paused(workspace)
    loop_state = {True: "paused", False: "running", None: "not installed"}[paused]
    lines = [
        "Cashmaxx status",
        f"Wallet: {wallet.get('address')} ({wallet.get('network')})",
        f"Balance: {wallet.get('balance_usdc')} USDC",
        f"Available budget: {wallet.get('available_budget_usd')} USD",
        f"7-day P&L: income {ledger.get('income')}, costs {ledger.get('costs')}, "
        f"net {ledger.get('net')} USD",
        f"Pending approvals: {len(pending)}",
    ]
    for item in pending[:5]:
        raw_payment: object = item.get("payment")
        payment = cast(dict[str, Any], raw_payment) if isinstance(raw_payment, dict) else {}
        lines.append(
            f"  - {item.get('id')}: {payment.get('amount_usd', '?')} USD to "
            f"{payment.get('to', '?')} ({item.get('reason', '')}): {payment.get('purpose', '')}"
        )
    frozen = bool(health.get("frozen"))
    lines.append(f"Frozen: {'YES' if frozen else 'no'}")
    lines.append(f"Money loop: {loop_state}")
    lines.append("Approve payments in the guard's Telegram bot or the WebUI (owner PIN).")
    return "\n".join(lines)


async def cmd_cashmaxx(ctx: CommandContext) -> OutboundMessage:
    cfg = plugin.resolve_agent_config()
    if cfg is None:
        return _reply(ctx, NOT_CONFIGURED)
    try:
        async with plugin.guard_client(cfg) as client:
            return _reply(ctx, await status_text(client, _workspace(ctx)))
    except GuardUnavailable:
        return _reply(ctx, GUARD_DOWN)
    except GuardError as exc:
        return _reply(ctx, f"Guard error ({exc.code}): {exc.message}")


async def _set_paused(ctx: CommandContext, paused: bool) -> OutboundMessage:
    try:
        changed = ws.set_loop_paused(_workspace(ctx), paused)
    except LookupError as exc:
        return _reply(ctx, str(exc))
    if paused:
        text = "Money loop paused." if changed else "Money loop was already paused."
        text += " Heartbeat runs skip it until /resume. The wallet rules are unchanged."
    else:
        text = "Money loop resumed." if changed else "Money loop was already running."
        text += " It runs on the next heartbeat."
    return _reply(ctx, text)


async def cmd_pause(ctx: CommandContext) -> OutboundMessage:
    return await _set_paused(ctx, True)


async def cmd_resume(ctx: CommandContext) -> OutboundMessage:
    return await _set_paused(ctx, False)


async def cmd_freeze(ctx: CommandContext) -> OutboundMessage:
    reason = ctx.args.strip() or f"/freeze from {ctx.msg.channel}"
    cfg = plugin.resolve_agent_config()
    if cfg is None:
        return _reply(ctx, NOT_CONFIGURED)
    try:
        async with plugin.guard_client(cfg) as client:
            await client.freeze(reason)
    except GuardUnavailable:
        return _reply(ctx, GUARD_DOWN + " Stop the guard process to be sure nothing moves.")
    except GuardError as exc:
        return _reply(ctx, f"Freeze failed ({exc.code}): {exc.message}")
    return _reply(
        ctx, "Cashmaxx is frozen. All spending is stopped. Only the owner can unfreeze it "
        "(guard Telegram bot, WebUI with PIN, or `cashmaxx unfreeze`)."
    )


def register_commands(router: CommandRouter) -> None:
    router.exact("/cashmaxx", cmd_cashmaxx)
    router.exact("/pause", cmd_pause)
    router.exact("/resume", cmd_resume)
    # Bare /freeze is a priority command so it works even while a turn is running.
    router.priority("/freeze", cmd_freeze)
    router.exact("/freeze", cmd_freeze)
    router.prefix("/freeze ", cmd_freeze)

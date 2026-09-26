"""The guard's own Telegram bot (raw Bot API over httpx, long polling).

This is a different bot token from the agent's Telegram channel. Only updates whose
``chat.id`` equals ``owner_chat_id`` are acted on; everything else is dropped.

Approval cards carry inline buttons with ``callback_data`` ``a:<approval_id>`` (approve) and
``d:<approval_id>`` (deny). Commands: /status /pnl /freeze /unfreeze /pending.
With no token configured the bot is a no-op notifier.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Protocol, cast

import httpx
from loguru import logger

API_BASE = "https://api.telegram.org"
MAX_TEXT = 4000


class Notifier(Protocol):
    async def send(self, text: str, *, approval_id: str | None = None) -> None: ...


class BotActions(Protocol):
    """What the bot may ask the guard to do (implemented by ``app.Guard``)."""

    async def bot_status(self) -> str: ...

    async def bot_pnl(self) -> str: ...

    async def bot_pending(self) -> list[tuple[str, str]]: ...

    async def bot_freeze(self, reason: str) -> str: ...

    async def bot_unfreeze(self) -> str: ...

    async def bot_decide(self, approval_id: str, approve: bool) -> str: ...


class NullNotifier:
    async def send(self, text: str, *, approval_id: str | None = None) -> None:
        logger.debug("notify (no telegram): {}", text)


def approval_keyboard(approval_id: str) -> dict[str, Any]:
    return {"inline_keyboard": [[
        {"text": "Approve", "callback_data": f"a:{approval_id}"},
        {"text": "Deny", "callback_data": f"d:{approval_id}"},
    ]]}


class TelegramBot:
    def __init__(
        self,
        token: str,
        owner_chat_id: str,
        *,
        actions: BotActions | None = None,
        http: httpx.AsyncClient | None = None,
        poll_timeout: int = 30,
    ) -> None:
        self.enabled = bool(token and owner_chat_id)
        self._token = token
        self._owner_chat_id = str(owner_chat_id).strip()
        self.actions = actions
        self._http = http or httpx.AsyncClient(timeout=poll_timeout + 15)
        self._poll_timeout = poll_timeout
        self._offset = 0
        self._task: asyncio.Task[None] | None = None

    # --- Bot API ------------------------------------------------------------------------------
    async def _call(self, method: str, payload: dict[str, Any]) -> Any:
        resp = await self._http.post(f"{API_BASE}/bot{self._token}/{method}", json=payload)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {data.get('description', resp.status_code)}")
        return data.get("result")

    async def send(self, text: str, *, approval_id: str | None = None) -> None:
        if not self.enabled:
            logger.debug("notify (telegram disabled): {}", text)
            return
        payload: dict[str, Any] = {"chat_id": self._owner_chat_id, "text": text[:MAX_TEXT]}
        if approval_id:
            payload["reply_markup"] = approval_keyboard(approval_id)
        try:
            await self._call("sendMessage", payload)
        except Exception as exc:  # notifications must never break the guard
            logger.warning("telegram send failed: {}", exc)

    # --- updates ------------------------------------------------------------------------------
    def _is_owner_chat(self, chat: Any, sender: Any = None) -> bool:
        """Only the owner's private chat counts: in a group, any member could press Approve."""
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            return False
        chat_obj = cast(dict[str, Any], chat)  # checked by isinstance above
        sender_obj = cast(dict[str, Any], sender)
        return (
            chat_obj.get("type") == "private"
            and str(chat_obj.get("id")) == self._owner_chat_id
            and str(sender_obj.get("id")) == self._owner_chat_id
        )

    async def handle_update(self, update: dict[str, Any]) -> None:
        if self.actions is None:
            return
        if cq := update.get("callback_query"):
            await self._handle_callback(cq)
        elif msg := update.get("message"):
            await self._handle_message(msg)

    async def _handle_callback(self, cq: dict[str, Any]) -> None:
        assert self.actions is not None
        message: dict[str, Any] = cq.get("message") or {}
        if not self._is_owner_chat(message.get("chat"), cq.get("from")):
            logger.warning("guard bot: ignored callback from chat {}", message.get("chat"))
            return
        data = str(cq.get("data") or "")
        action, _, approval_id = data.partition(":")
        if action not in ("a", "d") or not approval_id:
            return
        result = await self.actions.bot_decide(approval_id, approve=(action == "a"))
        with contextlib.suppress(Exception):
            await self._call("answerCallbackQuery",
                             {"callback_query_id": cq.get("id"), "text": result[:190]})
        await self.send(result)

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        assert self.actions is not None
        if not self._is_owner_chat(msg.get("chat"), msg.get("from")):
            logger.warning("guard bot: ignored message from chat {}", (msg.get("chat") or {}))
            return
        text = str(msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        command, _, rest = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        if command == "/status":
            await self.send(await self.actions.bot_status())
        elif command == "/pnl":
            await self.send(await self.actions.bot_pnl())
        elif command == "/freeze":
            await self.send(await self.actions.bot_freeze(rest.strip() or "owner via telegram"))
        elif command == "/unfreeze":
            await self.send(await self.actions.bot_unfreeze())
        elif command == "/pending":
            pending = await self.actions.bot_pending()
            if not pending:
                await self.send("No pending approvals.")
            for approval_id, card in pending:
                await self.send(card, approval_id=approval_id)
        else:
            await self.send("Commands: /status /pnl /pending /freeze [reason] /unfreeze")

    # --- polling ------------------------------------------------------------------------------
    async def _poll_forever(self) -> None:
        while True:
            try:
                updates: list[dict[str, Any]] = await self._call("getUpdates", {
                    "offset": self._offset, "timeout": self._poll_timeout,
                    "allowed_updates": ["message", "callback_query"],
                }) or []
                for update in updates:
                    self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                    try:
                        await self.handle_update(update)
                    except Exception as exc:
                        logger.exception("guard bot: update failed: {}", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("guard bot: polling error: {}", exc)
                await asyncio.sleep(5)

    def start(self) -> None:
        if self.enabled and self.actions is not None and self._task is None:
            self._task = asyncio.create_task(self._poll_forever(), name="cashmaxx-guard-bot")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._http.aclose()

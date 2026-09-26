from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from guard_testlib import BOB, Env, EnvFactory

from cashmaxx.guard.telegram_bot import TelegramBot

OWNER_CHAT = "4242"


class BotApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        self.calls.append((method, json.loads(request.content or b"{}")))
        return httpx.Response(200, json={"ok": True, "result": True})

    def sent_texts(self) -> list[str]:
        return [p["text"] for m, p in self.calls if m == "sendMessage"]


def make_bot(env: Env | None = None, token: str = "123:abc") -> tuple[TelegramBot, BotApi]:
    api = BotApi()
    bot = TelegramBot(token, OWNER_CHAT, actions=env.guard if env else None,
                      http=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)))
    return bot, api


def callback(
    data: str, chat_id: int | str, *, sender_id: int | str | None = None,
    chat_type: str = "private",
) -> dict[str, Any]:
    chat = {"id": chat_id, "type": chat_type}
    return {"update_id": 1, "callback_query": {
        "id": "cq1", "data": data, "from": {"id": sender_id if sender_id is not None else chat_id},
        "message": {"message_id": 5, "chat": chat}}}


def message(text: str, chat_id: int | str) -> dict[str, Any]:
    return {"update_id": 2, "message": {
        "message_id": 6, "text": text, "chat": {"id": chat_id, "type": "private"},
        "from": {"id": chat_id}}}


@pytest.fixture
async def pending(make_env: EnvFactory) -> tuple[Env, str]:
    env = await make_env()
    _, body = await env.spend(to=BOB, amount="1", key="tg")
    return env, body["approval_id"]


async def test_approval_card_has_buttons() -> None:
    bot, api = make_bot()
    await bot.send("card", approval_id="apr_1")
    method, payload = api.calls[0]
    assert method == "sendMessage" and payload["chat_id"] == OWNER_CHAT
    buttons = payload["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["a:apr_1", "d:apr_1"]


async def test_callback_from_other_chat_is_ignored(pending: tuple[Env, str]) -> None:
    env, approval_id = pending
    bot, api = make_bot(env)
    await bot.handle_update(callback(f"a:{approval_id}", 999))
    await bot.handle_update(message("/unfreeze", 999))
    assert api.calls == []
    approval = await env.guard.store.get_approval(approval_id)
    assert approval is not None and approval.status == "pending"
    assert env.wallet.transfers == []


async def test_callback_from_group_member_is_ignored(pending: tuple[Env, str]) -> None:
    env, approval_id = pending
    bot, api = make_bot(env)
    # A group chat whose id matches, pressed by someone other than the owner.
    await bot.handle_update(
        callback(f"a:{approval_id}", OWNER_CHAT, sender_id=999, chat_type="group"))
    await bot.handle_update(callback(f"a:{approval_id}", OWNER_CHAT, sender_id=999))
    assert api.calls == []
    approval = await env.guard.store.get_approval(approval_id)
    assert approval is not None and approval.status == "pending"
    assert env.wallet.transfers == []


async def test_callback_approve_from_owner_pays(pending: tuple[Env, str]) -> None:
    env, approval_id = pending
    bot, api = make_bot(env)
    await bot.handle_update(callback(f"a:{approval_id}", int(OWNER_CHAT)))
    approval = await env.guard.store.get_approval(approval_id)
    assert approval is not None and approval.status == "approved"
    assert approval.decided_via == "telegram"
    assert len(env.wallet.transfers) == 1
    assert any(m == "answerCallbackQuery" for m, _ in api.calls)
    assert "paid $1.00" in api.sent_texts()[-1]
    # pressing again is harmless
    await bot.handle_update(callback(f"a:{approval_id}", OWNER_CHAT))
    assert api.sent_texts()[-1] == f"{approval_id}: approval is approved"
    assert len(env.wallet.transfers) == 1


async def test_callback_deny(pending: tuple[Env, str]) -> None:
    env, approval_id = pending
    bot, api = make_bot(env)
    await bot.handle_update(callback(f"d:{approval_id}", OWNER_CHAT))
    payment = await env.guard.store.get_payment((await env.guard.store.get_approval(
        approval_id)).payment_id)  # type: ignore[union-attr]
    assert payment is not None and payment.status == "denied"
    assert api.sent_texts()[-1] == f"Denied {approval_id}."


async def test_commands(pending: tuple[Env, str]) -> None:
    env, approval_id = pending
    bot, api = make_bot(env)
    await bot.handle_update(message("/status", OWNER_CHAT))
    assert "Pending approvals: 1" in api.sent_texts()[-1]
    await bot.handle_update(message("/pnl@cashmaxx_guard_bot", OWNER_CHAT))
    assert api.sent_texts()[-1].startswith("P&L")
    await bot.handle_update(message("/pending", OWNER_CHAT))
    last = api.calls[-1][1]
    assert approval_id in last["text"] and last["reply_markup"]
    await bot.handle_update(message("/freeze going on holiday", OWNER_CHAT))
    assert env.guard.settings.frozen and env.guard.settings.frozen_reason == "going on holiday"
    await bot.handle_update(message("/unfreeze", OWNER_CHAT))
    assert not env.guard.settings.frozen
    await bot.handle_update(message("hello", OWNER_CHAT))  # plain text: ignored
    await bot.handle_update(message("/help", OWNER_CHAT))
    assert "Commands:" in api.sent_texts()[-1]


async def test_no_token_is_noop() -> None:
    bot, api = make_bot(token="")
    assert bot.enabled is False
    await bot.send("hi", approval_id="apr_1")
    bot.start()
    await bot.stop()
    assert api.calls == []

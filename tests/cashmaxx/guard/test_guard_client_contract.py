"""Drive the real ``GuardClient`` against the real app to prove paths, headers and shapes match."""

from __future__ import annotations

import pytest
from guard_testlib import AGENT_TOKEN, ALICE, BOB, PIN, Env

from cashmaxx.guard.client import GuardClient, GuardError, GuardUnavailable


async def test_agent_client_flow(env: Env) -> None:
    async with env.guard_client(agent_token=AGENT_TOKEN) as gc:
        assert (await gc.health())["ok"] is True
        wallet = await gc.wallet()
        assert wallet["network"] == "fake" and wallet["balance_usdc"] == "100.00"

        paid = await gc.spend(to=ALICE, amount_usd="1.50", purpose="hosting",
                              idempotency_key="c1")
        assert paid["status"] == "paid"
        assert (await gc.payment(paid["payment_id"]))["tx_hash"] == paid["tx_hash"]

        pending = await gc.spend(to=BOB, amount_usd="1", purpose="new", idempotency_key="c2")
        assert pending["status"] == "pending" and pending["approval_id"]
        assert len((await gc.approvals())["approvals"]) == 1

        ledger = await gc.ledger("7d")
        assert ledger["costs"] == "1.50" and ledger["net"] == "-1.50"
        await gc.record_cost(amount_usd="2", category="fees", note="gas")
        assert (await gc.ledger("all"))["costs"] == "3.50"

        assert (await gc.settings())["settings"]["network"] == "fake"
        assert any(e["type"] == "spend_decision" for e in (await gc.events())["events"])

        with pytest.raises(GuardError) as err:
            await gc.approve(pending["approval_id"])
        assert err.value.status == 403 and err.value.code == "forbidden"
        with pytest.raises(GuardError) as err:
            await gc.unfreeze()
        assert err.value.status == 403

        await gc.freeze("stop")
        with pytest.raises(GuardError) as err:
            await gc.spend(to=ALICE, amount_usd="1", purpose="x", idempotency_key="c3")
        assert err.value.status == 409 and err.value.code == "frozen"


async def test_owner_client_flow(env: Env) -> None:
    async with env.guard_client(agent_token=AGENT_TOKEN) as agent:
        pending = await agent.spend(to=BOB, amount_usd="1", purpose="new", idempotency_key="o1")
        await agent.freeze("pause")

    async with env.guard_client() as anon:
        with pytest.raises(GuardError) as err:
            await anon.owner_session("wrong")
        assert err.value.status == 401
        session = (await anon.owner_session(PIN))["session"]

    async with env.guard_client(owner_session=session) as owner:
        await owner.unfreeze()
        result = await owner.approve(pending["approval_id"])
        assert result["status"] == "paid"
        patched = await owner.update_settings({"perTxApprovalUsd": "7"})
        assert patched["settings"]["perTxApprovalUsd"] == "7"
        with pytest.raises(GuardError) as err:
            await owner.update_settings({"budgetUsd": "nope"})
        assert err.value.status == 422
        with pytest.raises(GuardError) as err:
            await owner.spend(to=ALICE, amount_usd="1", purpose="x", idempotency_key="o2")
        assert err.value.status == 403


async def test_webui_proxy_style_client_with_both_headers(env: Env) -> None:
    session = (await (await env.client.post("/owner/session", json={"pin": PIN})).json())["session"]
    async with env.guard_client(agent_token=AGENT_TOKEN, owner_session=session) as both:
        await both.freeze("x")
        assert (await both.unfreeze())["frozen"] is False


async def test_unreachable_guard_fails_closed() -> None:
    async with GuardClient("http://127.0.0.1:9", agent_token="x", timeout=1) as gc:
        with pytest.raises(GuardUnavailable):
            await gc.wallet()

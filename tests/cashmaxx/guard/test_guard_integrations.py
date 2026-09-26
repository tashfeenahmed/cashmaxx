"""Integrations registry endpoints: scopes, secrets, merge semantics, legacy mapping, tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from guard_testlib import AGENT_TOKEN, PIN, Env, EnvFactory, FakeClock, make_config
from integrations_testlib import (
    GMAIL_ADDR,
    GMAIL_PW,
    SECRET_MARK,
    FakeCloudflared,
    Mailbox,
    Router,
    connect,
    deps,
)

from cashmaxx.config import load_guard_config
from cashmaxx.guard.app import GUARD_KEY, create_app
from cashmaxx.guard.client import GuardError
from cashmaxx.guard.stripe_client import StripeClient
from cashmaxx.guard.telegram_bot import TelegramBot
from cashmaxx.integrations_catalog import INTEGRATIONS

OWNER_ROUTES = [
    ("PUT", "/integrations/gmail", {"fields": {}}),
    ("POST", "/integrations/gmail/test", None),
    ("DELETE", "/integrations/gmail", None),
    ("GET", "/owner/check", None),
    ("PATCH", "/settings", {"emailProvider": "none"}),
]


async def _req(env: Env, method: str, path: str, headers: dict[str, str],
               body: Any = None) -> tuple[int, dict[str, Any], str]:
    resp = await env.client.request(method, path, json=body, headers=headers)
    text = await resp.text()
    return resp.status, json.loads(text), text


def _item(listing: dict[str, Any], integration_id: str) -> dict[str, Any]:
    return next(i for i in listing["integrations"] if i["id"] == integration_id)


async def test_agent_listing_is_reduced(env: Env) -> None:
    status, body, _ = await _req(env, "GET", "/integrations", env.agent)
    assert status == 200
    ids = [i["id"] for i in body["integrations"]]
    assert ids == [s.id for s in INTEGRATIONS]
    for item in body["integrations"]:
        assert set(item) == {"id", "label", "kind", "category", "connected"}
        if item["kind"] == "agent":
            assert item["connected"] is None
        else:
            assert item["connected"] is False


async def test_owner_routes_refuse_agent_and_anonymous(env: Env) -> None:
    for method, path, body in OWNER_ROUTES:
        status, err, _ = await _req(env, method, path, env.agent, body)
        assert status == 403 and err["error"] == "forbidden", (method, path)
        status, err, _ = await _req(env, method, path, {}, body)
        assert status == 401, (method, path)
    owner = await env.owner()
    status, body, _ = await _req(env, "GET", "/owner/check", owner)
    assert status == 200 and body == {"ok": True}


async def test_agent_only_routes_refuse_owner(env: Env) -> None:
    owner = await env.owner()
    for path, body in (("/email/send", {}), ("/social/post", {}), ("/hosting/expose", {})):
        status, err, _ = await _req(env, "POST", path, owner, body)
        assert status == 403 and err["error"] == "forbidden", path
    for path in ("/email/status", "/email/inbox", "/email/messages/1", "/hosting",
                 "/integrations"):
        status, _, _ = await _req(env, "GET", path, {})
        assert status == 401, path


async def test_put_merges_and_never_returns_secrets(make_env: EnvFactory) -> None:
    env = await make_env(integration_deps=deps())
    owner = await env.owner()
    status, item, text = await _req(env, "PUT", "/integrations/gmail", owner,
                                    {"fields": {"address": GMAIL_ADDR, "appPassword": GMAIL_PW}})
    assert status == 200, text
    assert SECRET_MARK not in text
    fields = {f["name"]: f for f in item["fields"]}
    assert fields["address"] == {**fields["address"], "set": True, "value": GMAIL_ADDR}
    assert fields["appPassword"]["set"] is True and "value" not in fields["appPassword"]
    assert item["connected"] is True and item["connected_at"] == "2026-09-01T12:00:00.000000Z"
    assert item["last_test"] is None and item["docs_url"] and item["summary"]

    # "" keeps the stored secret, and rotating only the secret keeps connected_at
    env.clock.advance(days=3)
    owner = await env.owner()  # sessions last 12h
    status, item, text = await _req(env, "PUT", "/integrations/gmail", owner,
                                    {"fields": {"appPassword": ""}})
    assert status == 200 and SECRET_MARK not in text
    assert env.guard.config.integrations["gmail"].fields["appPassword"] == GMAIL_PW
    status, item, _ = await _req(env, "PUT", "/integrations/gmail", owner,
                                 {"fields": {"appPassword": f"{SECRET_MARK}-new"}})
    assert item["connected_at"] == "2026-09-01T12:00:00.000000Z"
    assert env.guard.config.integrations["gmail"].fields["appPassword"] == f"{SECRET_MARK}-new"
    # a new address is a new account: the warm-up restarts
    status, item, _ = await _req(env, "PUT", "/integrations/gmail", owner,
                                 {"fields": {"address": "other@gmail.com"}})
    assert item["connected_at"] == "2026-09-04T12:00:00.000000Z"

    # persisted to guard.json (0600) with the secret, never in listings or events
    saved = load_guard_config(env.config_path)
    assert saved.integrations["gmail"].fields["appPassword"] == f"{SECRET_MARK}-new"
    assert env.config_path.stat().st_mode & 0o777 == 0o600
    for headers in (owner, env.agent):
        for path in ("/integrations", "/settings", "/events"):
            _, _, text = await _req(env, "GET", path, headers)
            assert SECRET_MARK not in text, path
    _, listing, _ = await _req(env, "GET", "/integrations", env.agent)
    assert _item(listing, "gmail")["connected"] is True


async def test_put_validation(env: Env) -> None:
    owner = await env.owner()
    cases: list[tuple[str, Any, int, str]] = [
        ("gmail", {"address": GMAIL_ADDR}, 422, "invalid"),  # missing app password
        ("gmail", {"address": "nope", "appPassword": "x"}, 422, "invalid"),
        ("gmail", {"bogus": "x"}, 422, "invalid"),
        ("gmail", {"address": 5}, 422, "invalid"),
        ("gmail", "not-an-object", 422, "invalid"),
        ("hosting", {"provider": "cloudflare_token"}, 422, "invalid"),  # needs the token
        ("hosting", {"provider": "ngrok"}, 422, "invalid"),
        ("browser", {"provider": "playwright"}, 400, "agent_integration"),
        ("github", {"token": "x"}, 400, "agent_integration"),
        ("nope", {}, 404, "not_found"),
    ]
    for integration_id, fields, want_status, want_code in cases:
        status, err, _ = await _req(env, "PUT", f"/integrations/{integration_id}", owner,
                                    {"fields": fields})
        assert (status, err["error"]) == (want_status, want_code), (integration_id, fields)
    for method, path in (("POST", "/integrations/search/test"),
                         ("DELETE", "/integrations/browser")):
        status, err, _ = await _req(env, method, path, owner)
        assert status == 400 and err["error"] == "agent_integration"
    assert env.guard.config.integrations == {}


async def test_legacy_fields_round_trip(env: Env) -> None:
    owner = await env.owner()
    status, item, text = await _req(env, "PUT", "/integrations/telegram", owner, {
        "fields": {"botToken": f"123:{SECRET_MARK}", "ownerChatId": "42"}})
    assert status == 200 and SECRET_MARK not in text
    assert env.guard.config.telegram.bot_token == f"123:{SECRET_MARK}"
    assert env.guard.config.telegram.owner_chat_id == "42"
    assert env.guard.config.integrations["telegram"].fields == {}
    assert {f["name"]: f.get("value") for f in item["fields"]} == {
        "botToken": None, "ownerChatId": "42"}

    await _req(env, "PUT", "/integrations/cdp", owner, {"fields": {
        "apiKeyId": "kid", "apiKeySecret": f"{SECRET_MARK}1", "walletSecret": f"{SECRET_MARK}2"}})
    cdp = env.guard.config.cdp
    assert (cdp.api_key_id, cdp.api_key_secret, cdp.wallet_secret) == (
        "kid", f"{SECRET_MARK}1", f"{SECRET_MARK}2")
    await _req(env, "PUT", "/integrations/openrouter", owner,
               {"fields": {"apiKey": f"sk-or-{SECRET_MARK}"}})
    assert env.guard.config.openrouter_api_key == f"sk-or-{SECRET_MARK}"
    assert env.guard.openrouter is not None and env.guard.compute.openrouter is env.guard.openrouter

    saved = load_guard_config(env.config_path)
    assert saved.telegram.bot_token == f"123:{SECRET_MARK}"
    assert saved.cdp.wallet_secret == f"{SECRET_MARK}2"
    assert saved.openrouter_api_key == f"sk-or-{SECRET_MARK}"

    _, listing, text = await _req(env, "GET", "/integrations", owner)
    assert SECRET_MARK not in text
    for integration_id in ("telegram", "cdp", "openrouter"):
        assert _item(listing, integration_id)["connected"] is True
    _, settings, _ = await _req(env, "GET", "/settings", owner)
    assert settings["integrations"]["telegram"] is True and settings["integrations"]["gmail"] is False

    status, removed, _ = await _req(env, "DELETE", "/integrations/telegram", owner)
    assert status == 200 and removed["removed"] is True and removed["connected"] is False
    assert env.guard.config.telegram.bot_token == ""
    await _req(env, "DELETE", "/integrations/openrouter", owner)
    assert env.guard.openrouter is None and env.guard.compute.openrouter is None


async def test_telegram_bot_hot_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []
    monkeypatch.setattr(TelegramBot, "start", lambda self: started.append(self._token))  # pyright: ignore[reportPrivateUsage]
    app = create_app(make_config(), config_path=tmp_path / "g.json",
                     db_path=tmp_path / "g.sqlite3", clock=FakeClock(), watch_interval_s=None,
                     integration_deps=deps())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        guard = app[GUARD_KEY]
        old = guard.bot
        assert old is not None and old.enabled is False
        session = (await (await client.post("/owner/session", json={"pin": PIN})).json())["session"]
        resp = await client.put("/integrations/telegram", headers={"X-Cashmaxx-Owner": session},
                                json={"fields": {"botToken": "5:abc", "ownerChatId": "7"}})
        assert resp.status == 200
        assert guard.bot is not old and guard.bot is not None and guard.bot.enabled
        assert guard.notifier is guard.bot and guard.bot.actions is guard
        assert started[-1] == "5:abc"
    finally:
        await client.close()


async def test_stripe_hot_reload(make_env: EnvFactory) -> None:
    made: list[str] = []

    class Stub:
        def __init__(self, key: str) -> None:
            made.append(key)

    def factory(key: str) -> StripeClient:
        return StripeClient(key, client_factory=Stub)

    env = await make_env(integration_deps=deps(stripe_factory=factory))
    owner = await env.owner()
    assert env.guard.stripe is None
    await _req(env, "PUT", "/integrations/stripe", owner,
               {"fields": {"restrictedKey": "rk_test_1"}})
    assert env.guard.stripe is not None and made == ["rk_test_1"]
    await _req(env, "DELETE", "/integrations/stripe", owner)
    assert env.guard.stripe is None


async def test_cdp_change_needs_restart(make_env: EnvFactory) -> None:
    config = make_config()
    config.settings = config.settings.model_copy(update={"network": "base-sepolia"})
    env = await make_env(config=config)
    owner = await env.owner()
    _, item, _ = await _req(env, "PUT", "/integrations/cdp", owner, {"fields": {
        "apiKeyId": "a", "apiKeySecret": "b", "walletSecret": "c"}})
    assert item["restart_required"] is True


async def test_email_provider_setting_needs_connection(make_env: EnvFactory) -> None:
    env = await make_env(integration_deps=deps())
    owner = await env.owner()
    for provider in ("gmail", "agentmail"):
        status, err, _ = await _req(env, "PATCH", "/settings", owner, {"emailProvider": provider})
        assert status == 400 and err["error"] == "email_not_connected"
    assert env.guard.settings.email_provider == "none"
    await _req(env, "PUT", "/integrations/gmail", owner,
               {"fields": {"address": GMAIL_ADDR, "appPassword": GMAIL_PW}})
    status, body, _ = await _req(env, "PATCH", "/settings", owner, {"emailProvider": "gmail"})
    assert status == 200 and body["settings"]["emailProvider"] == "gmail"
    # unrelated patches still work while gmail is chosen
    status, _, _ = await _req(env, "PATCH", "/settings", owner, {"emailDailyCap": 5})
    assert status == 200
    # removing the chosen provider resets it to none
    status, removed, _ = await _req(env, "DELETE", "/integrations/gmail", owner)
    assert status == 200 and removed["email_provider"] == "none"
    assert env.guard.settings.email_provider == "none"
    assert load_guard_config(env.config_path).settings.email_provider == "none"
    assert "gmail" not in env.guard.config.integrations


async def test_live_tests_record_results_and_scrub_secrets(make_env: EnvFactory) -> None:
    router = Router()
    router.add("POST api.telegram.org/bot", {"ok": True, "result": {"username": "guardbot"}})
    router.add("GET openrouter.ai/api/v1/key", {"data": {"usage": "1.25"}})
    router.add("POST bsky.social/xrpc/com.atproto.server.createSession",
               {"error": "AuthenticationRequired", "message": "Invalid identifier or password"},
               status=401)
    box = Mailbox()
    config = make_config()
    connect(config, "bluesky", {"handle": "cm.bsky.social", "appPassword": f"{SECRET_MARK}-b"})
    connect(config, "gmail", {"address": GMAIL_ADDR, "appPassword": f"{SECRET_MARK}-wrong"})
    config.telegram.bot_token = f"99:{SECRET_MARK}"
    config.telegram.owner_chat_id = "1"
    config.openrouter_api_key = f"sk-{SECRET_MARK}"
    env = await make_env(config=config, integration_deps=deps(
        router=router, box=box, cloudflared=FakeCloudflared()))
    owner = await env.owner()

    status, result, text = await _req(env, "POST", "/integrations/telegram/test", owner)
    assert status == 200 and result["ok"] is True and "@guardbot" in result["message"]
    assert SECRET_MARK not in text
    _, result, _ = await _req(env, "POST", "/integrations/openrouter/test", owner)
    assert result["ok"] is True and "1.25" in result["message"]
    _, result, _ = await _req(env, "POST", "/integrations/bluesky/test", owner)
    assert result["ok"] is False and "401" in result["message"]
    # the fake IMAP server echoes the password in its error: it must be scrubbed
    _, result, text = await _req(env, "POST", "/integrations/gmail/test", owner)
    assert result["ok"] is False and SECRET_MARK not in text and "***" in result["message"]
    _, result, _ = await _req(env, "POST", "/integrations/x/test", owner)
    assert result == {**result, "ok": False} and "missing" in result["message"]
    _, result, _ = await _req(env, "POST", "/integrations/hosting/test", owner)
    assert result["ok"] is False  # not connected yet

    _, listing, text = await _req(env, "GET", "/integrations", owner)
    assert SECRET_MARK not in text
    assert _item(listing, "telegram")["last_test"]["ok"] is True
    assert _item(listing, "bluesky")["last_test"]["ok"] is False
    saved = load_guard_config(env.config_path)
    assert saved.integrations["telegram"].last_test_ok is True
    _, events, text = await _req(env, "GET", "/events", owner)
    assert SECRET_MARK not in text
    assert [e["data"]["id"] for e in events["events"] if e["type"] == "integration_tested"][:2] \
        == ["telegram", "openrouter"]


async def test_gmail_test_ok_and_hosting_version(make_env: EnvFactory) -> None:
    cloudflared = FakeCloudflared()
    config = make_config()
    connect(config, "gmail", {"address": GMAIL_ADDR, "appPassword": GMAIL_PW})
    connect(config, "hosting", {"provider": "cloudflare_quick"})
    env = await make_env(config=config, integration_deps=deps(cloudflared=cloudflared))
    owner = await env.owner()
    _, result, _ = await _req(env, "POST", "/integrations/gmail/test", owner)
    assert result["ok"] is True and "IMAP and SMTP" in result["message"]
    _, result, _ = await _req(env, "POST", "/integrations/hosting/test", owner)
    assert result["ok"] is True and "2026.9.0" in result["message"]
    cloudflared.installed = False
    _, result, _ = await _req(env, "POST", "/integrations/hosting/test", owner)
    assert result["ok"] is False and "brew install cloudflared" in result["message"]


async def test_client_contract_for_integrations(make_env: EnvFactory) -> None:
    env = await make_env(integration_deps=deps())
    async with env.guard_client(agent_token=AGENT_TOKEN) as agent:
        listing = await agent.integrations()
        assert set(listing["integrations"][0]) == {"id", "label", "kind", "category", "connected"}
        with pytest.raises(GuardError) as err:
            await agent.update_integration("gmail", {"address": GMAIL_ADDR})
        assert err.value.status == 403
        with pytest.raises(GuardError) as err:
            await agent.owner_check()
        assert err.value.status == 403
    session = (await (await env.client.post("/owner/session", json={"pin": PIN})).json())[
        "session"]
    async with env.guard_client(owner_session=session) as owner:
        assert (await owner.owner_check()) == {"ok": True}
        item = await owner.update_integration("gmail", {"address": GMAIL_ADDR,
                                                        "appPassword": GMAIL_PW})
        assert item["connected"] is True
        full = await owner.owner_integrations()
        assert "fields" in _item(full, "gmail")
        assert (await owner.test_integration("gmail"))["ok"] is True
        assert (await owner.remove_integration("gmail"))["removed"] is True
        with pytest.raises(GuardError) as err:
            await owner.update_integration("browser", {"provider": "playwright"})
        assert err.value.code == "agent_integration"

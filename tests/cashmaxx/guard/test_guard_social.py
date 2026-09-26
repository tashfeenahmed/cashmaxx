"""Social posts through the guard: OAuth 1.0a signing, Bluesky/X/Reddit calls, the shared cap."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from guard_testlib import AGENT_TOKEN, Env, EnvFactory, make_config
from integrations_testlib import SECRET_MARK, Router, connect, deps

from cashmaxx.guard.client import GuardError
from cashmaxx.guard.integrations.social import bluesky_link_facet, oauth1_header, oauth1_signature

# Twitter's documented example ("Creating a signature", developer.x.com OAuth 1.0a docs).
TW_URL = "https://api.twitter.com/1.1/statuses/update.json"
TW_PARAMS = {
    "status": "Hello Ladies + Gentlemen, a signed OAuth request!",
    "include_entities": "true",
    "oauth_consumer_key": "xvz1evFS4wEEPTGEFPHBog",
    "oauth_nonce": "kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
    "oauth_signature_method": "HMAC-SHA1",
    "oauth_timestamp": "1318622958",
    "oauth_token": "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
    "oauth_version": "1.0",
}
TW_CONSUMER_SECRET = "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw"
TW_TOKEN_SECRET = "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE"
TW_SIGNATURE = "hCtSmYh+iHYCEqBWrE7C7hYmtUk="


def test_oauth1_signature_matches_twitter_example() -> None:
    assert oauth1_signature("POST", TW_URL, TW_PARAMS, TW_CONSUMER_SECRET,
                            TW_TOKEN_SECRET) == TW_SIGNATURE


def test_oauth1_header_matches_twitter_example() -> None:
    header = oauth1_header(
        "POST", TW_URL, consumer_key=TW_PARAMS["oauth_consumer_key"],
        consumer_secret=TW_CONSUMER_SECRET, token=TW_PARAMS["oauth_token"],
        token_secret=TW_TOKEN_SECRET,
        request_params={"status": TW_PARAMS["status"], "include_entities": "true"},
        nonce=TW_PARAMS["oauth_nonce"], timestamp=TW_PARAMS["oauth_timestamp"],
    )
    assert header.startswith("OAuth ")
    assert 'oauth_signature="hCtSmYh%2BiHYCEqBWrE7C7hYmtUk%3D"' in header
    assert TW_CONSUMER_SECRET not in header and TW_TOKEN_SECRET not in header


def test_bluesky_facet_uses_utf8_byte_offsets() -> None:
    text = "café → https://x.io"
    facet = bluesky_link_facet(text, "https://x.io")
    assert facet is not None
    start = facet["index"]["byteStart"]
    assert text.encode()[start:facet["index"]["byteEnd"]] == b"https://x.io"


X_FIELDS = {"apiKey": f"ck-{SECRET_MARK}", "apiSecret": f"cs-{SECRET_MARK}",
            "accessToken": f"at-{SECRET_MARK}", "accessSecret": f"as-{SECRET_MARK}"}


def social_router() -> Router:
    router = Router()
    router.add("POST bsky.social/xrpc/com.atproto.server.createSession",
               {"accessJwt": "jwt-1", "did": "did:plc:abc", "handle": "cm.bsky.social"})
    router.add("POST bsky.social/xrpc/com.atproto.repo.createRecord",
               {"uri": "at://did:plc:abc/app.bsky.feed.post/3kpost", "cid": "c"})
    router.add("POST api.x.com/2/tweets", {"data": {"id": "1799", "text": "hi"}})
    router.add("GET api.x.com/2/users/me", {"data": {"id": "1", "username": "cmx"}})
    router.add("POST www.reddit.com/api/v1/access_token", {"access_token": "rt-1"})
    router.add("POST oauth.reddit.com/api/submit", {"json": {"errors": [], "data": {
        "url": "https://www.reddit.com/r/test/comments/abc/hello/", "id": "abc"}}})
    return router


async def social_env(make_env: EnvFactory, router: Router, **settings: Any) -> Env:
    config = make_config(**settings)
    connect(config, "bluesky", {"handle": "cm.bsky.social", "appPassword": f"bp-{SECRET_MARK}"})
    connect(config, "x", dict(X_FIELDS))
    connect(config, "reddit", {"clientId": "cid", "clientSecret": f"rs-{SECRET_MARK}",
                               "username": "cmx", "password": f"rp-{SECRET_MARK}"})
    return await make_env(config=config, integration_deps=deps(router=router))


async def post(env: Env, key: str, **body: Any) -> tuple[int, dict[str, Any]]:
    payload = {"text": "hello world", "idempotency_key": key, **body}
    resp = await env.client.post("/social/post", json=payload, headers=env.agent)
    return resp.status, await resp.json()


async def test_posts_on_each_platform(make_env: EnvFactory) -> None:
    router = social_router()
    env = await social_env(make_env, router, social_daily_cap=5)

    code, body = await post(env, "b1", platform="bluesky", link="https://cashmaxx.dev/p")
    assert code == 200 and body == {"status": "posted", "remaining_today": 4,
                                    "url": "https://bsky.app/profile/cm.bsky.social/post/3kpost"}
    record = json.loads(router.calls[1].content)
    assert record["repo"] == "did:plc:abc" and router.calls[1].headers["authorization"] == \
        "Bearer jwt-1"
    assert record["record"]["text"] == "hello world\nhttps://cashmaxx.dev/p"
    assert record["record"]["facets"][0]["features"][0]["uri"] == "https://cashmaxx.dev/p"

    code, body = await post(env, "x1", platform="x")
    assert code == 200 and body["url"] == "https://x.com/i/status/1799"
    auth = router.calls[-1].headers["authorization"]
    assert auth.startswith("OAuth ") and f"ck-{SECRET_MARK}" in auth  # consumer key is public
    assert f"cs-{SECRET_MARK}" not in auth and f"as-{SECRET_MARK}" not in auth
    assert json.loads(router.calls[-1].content) == {"text": "hello world"}

    code, body = await post(env, "r1", platform="reddit", subreddit="r/test", title="Hello")
    assert code == 200 and body["url"].startswith("https://www.reddit.com/r/test/")
    token_call, submit = router.calls[-2], router.calls[-1]
    assert parse_qs(token_call.content.decode())["grant_type"] == ["password"]
    assert token_call.headers["authorization"].startswith("Basic ")
    form = parse_qs(submit.content.decode())
    assert form["sr"] == ["test"] and form["kind"] == ["self"] and form["title"] == ["Hello"]
    assert "cmx" in submit.headers["user-agent"]

    events = (await (await env.client.get("/events", headers=env.agent)).json())["events"]
    assert [e["data"]["platform"] for e in events if e["type"] == "social_posted"] == [
        "bluesky", "x", "reddit"]
    assert SECRET_MARK not in json.dumps(events)


async def test_cap_is_shared_across_platforms(make_env: EnvFactory) -> None:
    env = await social_env(make_env, social_router(), social_daily_cap=2)
    assert (await post(env, "1", platform="bluesky"))[0] == 200
    assert (await post(env, "2", platform="x"))[0] == 200
    code, err = await post(env, "3", platform="reddit", subreddit="test", title="t")
    assert code == 429 and err["error"] == "social_cap" and err["remaining_today"] == 0
    code, _ = await post(env, "4", platform="bluesky")
    assert code == 429
    assert sum("social post cap" in t for t in env.notifier.texts()) == 1
    env.clock.advance(days=1)
    assert (await post(env, "5", platform="x"))[0] == 200


async def test_idempotent_post(make_env: EnvFactory) -> None:
    router = social_router()
    env = await social_env(make_env, router)
    _, first = await post(env, "same", platform="x")
    _, second = await post(env, "same", platform="bluesky", text="different")
    assert second["url"] == first["url"] and second["replayed"] is True
    assert sum(1 for c in router.calls if c.url.path == "/2/tweets") == 1
    assert not any("bsky" in str(c.url) for c in router.calls)


async def test_refusals(make_env: EnvFactory) -> None:
    router = social_router()
    env = await social_env(make_env, router)
    plain = await make_env()
    code, err = await post(plain, "n", platform="bluesky")
    assert code == 409 and err["error"] == "social_not_connected"
    for body in ({"platform": "myspace"}, {"platform": "reddit", "title": "t"},
                 {"platform": "reddit", "subreddit": "test"},
                 {"platform": "reddit", "subreddit": "bad name!", "title": "t"},
                 {"platform": "x", "link": "javascript:alert(1)"},
                 {"platform": "x", "text": ""}):
        code, err = await post(env, "bad", **body)
        assert code == 422 and err["error"] == "invalid", body
    await env.client.post("/freeze", json={"reason": "x"}, headers=env.agent)
    code, err = await post(env, "f", platform="x")
    assert code == 423 and err["error"] == "frozen"
    assert router.calls == []


async def test_platform_error_is_scrubbed_and_frees_the_slot(make_env: EnvFactory) -> None:
    router = social_router()

    def fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": f"denied {request.headers['authorization']}"})

    router.add("POST api.x.com/2/tweets", fail)
    env = await social_env(make_env, router, social_daily_cap=1)
    code, err = await post(env, "x", platform="x")
    assert code == 502 and err["error"] == "social_failed"
    assert (await post(env, "b", platform="bluesky"))[0] == 200  # the failed post used no slot


async def test_social_client_contract(make_env: EnvFactory) -> None:
    env = await social_env(make_env, social_router())
    async with env.guard_client(agent_token=AGENT_TOKEN) as gc:
        result = await gc.social_post(platform="reddit", text="t", idempotency_key="c1",
                                      subreddit="test", title="T", link="https://a.b/c")
        assert result["status"] == "posted"
        with pytest.raises(GuardError) as err:
            await gc.social_post(platform="nope", text="t", idempotency_key="c2")
        assert err.value.status == 422

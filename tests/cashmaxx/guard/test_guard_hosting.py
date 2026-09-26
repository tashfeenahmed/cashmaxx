"""Public hosting through cloudflared (subprocess mocked): refusals, limits, token tunnels, shutdown."""

from __future__ import annotations

from typing import Any

import pytest
from guard_testlib import AGENT_TOKEN, Env, EnvFactory, make_config
from integrations_testlib import FakeCloudflared, connect, deps

from cashmaxx.guard.client import GuardError
from cashmaxx.guard.integrations.hosting import GATEWAY_PORT, WEBUI_PORT

TOKEN = "eyJhIjoiYWNjdCIsInQiOiJ0dW4tMTIzIiwicyI6InNlY3JldCJ9"  # {"a":"acct","t":"tun-123","s":"secret"}


async def hosting_env(
    make_env: EnvFactory, cloudflared: FakeCloudflared | None = None, *, enabled: bool = True,
    fields: dict[str, str] | None = None,
) -> tuple[Env, FakeCloudflared]:
    cloudflared = cloudflared or FakeCloudflared()
    config = make_config(hosting_enabled=enabled)
    connect(config, "hosting", fields or {"provider": "cloudflare_quick"})
    env = await make_env(config=config, integration_deps=deps(cloudflared=cloudflared))
    return env, cloudflared


async def expose(env: Env, port: Any, name: Any = "api") -> tuple[int, dict[str, Any]]:
    resp = await env.client.post("/hosting/expose", json={"port": port, "name": name},
                                 headers=env.agent)
    return resp.status, await resp.json()


async def test_quick_tunnel_lifecycle(make_env: EnvFactory) -> None:
    env, cf = await hosting_env(make_env)
    code, body = await expose(env, 8080)
    assert code == 200 and body["url"] == "https://quick-1-abc.trycloudflare.com"
    argv, run_env = cf.spawned[0]
    assert argv[1:] == ["tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:8080"]
    assert run_env is None
    listing = await (await env.client.get("/hosting", headers=env.agent)).json()
    assert listing["enabled"] is True
    assert [(t["name"], t["port"], t["url"]) for t in listing["tunnels"]] == [
        ("api", 8080, "https://quick-1-abc.trycloudflare.com")]
    assert listing["tunnels"][0]["started_at"].startswith("2026-09-01T12:00")

    resp = await env.client.delete("/hosting/api", headers=env.agent)
    assert resp.status == 200 and cf.processes[0].terminated
    assert (await env.client.delete("/hosting/api", headers=env.agent)).status == 404
    events = (await (await env.client.get("/events", headers=env.agent)).json())["events"]
    assert {"tunnel_started", "tunnel_stopped"} <= {e["type"] for e in events}


async def test_forbidden_ports(make_env: EnvFactory) -> None:
    env, cf = await hosting_env(make_env)
    guard_port = env.guard.config.port
    for port in (guard_port, GATEWAY_PORT, WEBUI_PORT, 80, 443, 1023, 0, -1, 70000):
        code, err = await expose(env, port)
        assert code == 400 and err["error"] == "forbidden_port", port
    for port in ("8080", 80.5, True, None):
        code, err = await expose(env, port)
        assert code == 422, port
    for name in ("", "UPPER", "a" * 33, "../x", "-x", 5):
        code, err = await expose(env, 8080, name)
        assert code == 422, name
    assert cf.spawned == []


async def test_max_three_tunnels_and_unique_names(make_env: EnvFactory) -> None:
    env, cf = await hosting_env(make_env)
    for i in range(3):
        assert (await expose(env, 8000 + i, f"t{i}"))[0] == 200
    code, err = await expose(env, 8000, "t0")
    assert code == 409 and err["error"] == "name_taken"
    code, err = await expose(env, 8010, "t3")
    assert code == 409 and err["error"] == "too_many_tunnels"
    # a tunnel that died frees its slot
    cf.processes[0].terminate()
    assert (await expose(env, 8010, "t3"))[0] == 200


async def test_refusals(make_env: EnvFactory) -> None:
    env, cf = await hosting_env(make_env, enabled=False)
    code, err = await expose(env, 8080)
    assert code == 403 and err["error"] == "hosting_disabled"
    config = make_config(hosting_enabled=True)
    plain = await make_env(config=config, integration_deps=deps(cloudflared=cf))
    code, err = await expose(plain, 8080)
    assert code == 409 and err["error"] == "hosting_not_connected"

    missing = FakeCloudflared(installed=False)
    env2, _ = await hosting_env(make_env, missing)
    code, err = await expose(env2, 8080)
    assert code == 409 and err["error"] == "cloudflared_missing"
    assert "brew install cloudflared" in err["message"]

    env3, _ = await hosting_env(make_env)
    await env3.client.post("/freeze", json={"reason": "x"}, headers=env3.agent)
    code, err = await expose(env3, 8080)
    assert code == 423 and err["error"] == "frozen"
    assert cf.spawned == []


async def test_cloudflared_exit_and_timeout(make_env: EnvFactory) -> None:
    cf = FakeCloudflared(output=lambda _argv: ["ERR failed to request quick Tunnel"])
    env, _ = await hosting_env(make_env, cf)
    code, err = await expose(env, 8080)
    assert code == 504 and err["error"] == "tunnel_timeout"
    assert cf.processes[0].terminated
    assert (await (await env.client.get("/hosting", headers=env.agent)).json())["tunnels"] == []


async def test_token_tunnel(make_env: EnvFactory) -> None:
    config_line = ('INF Updated to new configuration config="{\\"ingress\\":[{\\"hostname\\":'
                   '\\"api.example.com\\",\\"service\\":\\"http://localhost:9000\\"},'
                   '{\\"service\\":\\"http_status:404\\"}]}" version=2')
    cf = FakeCloudflared(output=lambda _argv: ["INF Starting tunnel", config_line])
    env, _ = await hosting_env(make_env, cf, fields={"provider": "cloudflare_token",
                                                     "tunnelToken": TOKEN})
    code, body = await expose(env, 8080)
    assert code == 200 and body["url"] == "https://api.example.com"
    assert "9000" in body["note"]
    argv, run_env = cf.spawned[0]
    assert TOKEN not in argv and run_env is not None and run_env["TUNNEL_TOKEN"] == TOKEN
    code, err = await expose(env, 8081, "second")
    assert code == 409 and err["error"] == "too_many_tunnels"

    owner = await env.owner()
    result = await (await env.client.post("/integrations/hosting/test", headers=owner)).json()
    assert result["ok"] is True and "tun-123" in result["message"]
    assert TOKEN not in str(result)


async def test_token_tunnel_without_hostname_in_logs(make_env: EnvFactory) -> None:
    cf = FakeCloudflared(output=lambda _argv: ["INF Registered tunnel connection connIndex=0"])
    env, _ = await hosting_env(make_env, cf, fields={"provider": "cloudflare_token",
                                                     "tunnelToken": TOKEN})
    code, body = await expose(env, 8080)
    assert code == 200 and body["url"] is None and "Cloudflare dashboard" in body["note"]


async def test_disabling_or_disconnecting_stops_tunnels(make_env: EnvFactory) -> None:
    env, cf = await hosting_env(make_env)
    await expose(env, 8080, "a")
    owner = await env.owner()
    resp = await env.client.patch("/settings", json={"hostingEnabled": False}, headers=owner)
    assert resp.status == 200 and cf.processes[0].terminated
    await env.client.patch("/settings", json={"hostingEnabled": True}, headers=owner)
    await expose(env, 8080, "b")
    await env.client.delete("/integrations/hosting", headers=owner)
    assert cf.processes[1].terminated


async def test_guard_shutdown_stops_tunnels(make_env: EnvFactory) -> None:
    env, cf = await hosting_env(make_env)
    await expose(env, 8080)
    await env.client.close()
    assert cf.processes[0].terminated


async def test_hosting_client_contract(make_env: EnvFactory) -> None:
    env, _ = await hosting_env(make_env)
    async with env.guard_client(agent_token=AGENT_TOKEN) as gc:
        assert (await gc.hosting_expose(port=3000, name="shop"))["url"]
        assert len((await gc.hosting_list())["tunnels"]) == 1
        assert (await gc.hosting_stop("shop"))["stopped"] is True
        with pytest.raises(GuardError) as err:
            await gc.hosting_expose(port=18790, name="gw")
        assert err.value.code == "forbidden_port"

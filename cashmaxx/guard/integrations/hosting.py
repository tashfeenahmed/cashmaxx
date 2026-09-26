"""Public URLs for services the agent runs, through ``cloudflared``.

- ``cloudflare_quick``: ``cloudflared tunnel --url http://127.0.0.1:<port>``; the random
  ``*.trycloudflare.com`` URL is parsed from cloudflared's log output.
- ``cloudflare_token``: ``cloudflared tunnel run`` with ``TUNNEL_TOKEN`` in the environment (not
  argv, so it never shows in ``ps``). The public hostname and the origin port are configured in the
  Cloudflare dashboard; the hostname is read from the "Updated to new configuration" log line when
  cloudflared prints it, otherwise the URL is ``None`` with a note saying where to find it.

The guard stops every tunnel when it shuts down. Tests inject ``spawn`` and ``which``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from loguru import logger

from cashmaxx.guard.errors import ApiError

GATEWAY_PORT = 18790
WEBUI_PORT = 8765
MAX_TUNNELS = 3
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
QUICK_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
READY_RE = re.compile(r"Registered tunnel connection|Connection [0-9a-f-]+ registered")
START_TIMEOUT_S = 30.0
INSTALL_HINT = "cloudflared is not installed on the guard's machine (macOS: brew install cloudflared)"


class LineStream(Protocol):
    async def readline(self) -> bytes: ...


class Process(Protocol):
    @property
    def stdout(self) -> Any: ...

    @property
    def returncode(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


Spawn = Callable[[list[str], dict[str, str] | None], Awaitable[Process]]
Which = Callable[[str], str | None]


async def default_spawn(argv: list[str], env: dict[str, str] | None) -> Process:
    return await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, env=env,
    )


@dataclass
class Tunnel:
    name: str
    port: int
    url: str | None
    started_at: str
    provider: str
    process: Process
    note: str | None = None
    log: list[str] = field(default_factory=list[str])
    drain: asyncio.Task[None] | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "port": self.port, "url": self.url,
                               "started_at": self.started_at, "provider": self.provider}
        if self.note:
            out["note"] = self.note
        return out


def check_port(port: object, guard_port: int) -> int:
    if not isinstance(port, int) or isinstance(port, bool):
        raise ApiError(422, "invalid", "port must be an integer")
    if port < 1024 or port > 65535:
        raise ApiError(400, "forbidden_port", "ports below 1024 (and above 65535) are refused")
    if port in (guard_port, GATEWAY_PORT, WEBUI_PORT):
        raise ApiError(400, "forbidden_port",
                       f"port {port} belongs to Cashmaxx (guard, gateway or WebUI) and is "
                       "never exposed")
    return port


def check_name(name: object) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ApiError(422, "invalid",
                       "name must be 1-32 chars of lowercase letters, digits and dashes")
    return name


def _hostnames(line: str) -> list[str]:
    """Ingress hostnames from cloudflared's 'Updated to new configuration config=...' line."""
    if "Updated to new configuration" not in line:
        return []
    unescaped = line.replace('\\"', '"')
    hosts = re.findall(r'"hostname"\s*:\s*"([A-Za-z0-9.-]+)"', unescaped)
    return list(dict.fromkeys(hosts))


def _ingress_ports(line: str) -> list[int]:
    unescaped = line.replace('\\"', '"')
    return [int(p) for p in re.findall(r'"service"\s*:\s*"https?://[^":/]+:(\d+)', unescaped)]


class HostingManager:
    def __init__(self, *, spawn: Spawn | None = None, which: Which | None = None,
                 start_timeout_s: float = START_TIMEOUT_S) -> None:
        self.spawn: Spawn = spawn or default_spawn
        self.which: Which = which or shutil.which
        self.start_timeout_s = start_timeout_s
        self.tunnels: dict[str, Tunnel] = {}
        self._lock = asyncio.Lock()

    def binary(self) -> str:
        path = self.which("cloudflared")
        if not path:
            raise ApiError(409, "cloudflared_missing", INSTALL_HINT)
        return path

    async def version(self) -> str:
        proc = await self.spawn([self.binary(), "--version"], None)
        lines: list[str] = []
        try:
            async with asyncio.timeout(15):
                while line := await proc.stdout.readline():
                    lines.append(line.decode("utf-8", "replace").strip())
                await proc.wait()
        except TimeoutError:
            _kill(proc)
            raise ApiError(504, "timeout", "cloudflared --version did not answer") from None
        text = next((ln for ln in lines if ln), "cloudflared (no version output)")
        return text[:200]

    def prune(self) -> None:
        for name in [n for n, t in self.tunnels.items() if t.process.returncode is not None]:
            logger.info("hosting: tunnel {} exited", name)
            tunnel = self.tunnels.pop(name)
            if tunnel.drain is not None:
                tunnel.drain.cancel()

    def list(self) -> list[dict[str, Any]]:
        self.prune()
        return [t.to_json() for t in self.tunnels.values()]

    async def expose(
        self, *, name: str, port: int, provider: str, token: str, started_at: str
    ) -> Tunnel:
        async with self._lock:
            self.prune()
            if name in self.tunnels:
                raise ApiError(409, "name_taken", f"a tunnel named {name!r} is already running")
            if len(self.tunnels) >= MAX_TUNNELS:
                raise ApiError(409, "too_many_tunnels",
                               f"at most {MAX_TUNNELS} tunnels; stop one first")
            if provider == "cloudflare_token" and any(
                t.provider == "cloudflare_token" for t in self.tunnels.values()
            ):
                raise ApiError(409, "too_many_tunnels",
                               "a token tunnel is already running; its routes are set in the "
                               "Cloudflare dashboard")
            binary = self.binary()
            if provider == "cloudflare_token":
                argv = [binary, "tunnel", "--no-autoupdate", "run"]
                env = {**os.environ, "TUNNEL_TOKEN": token}
            else:
                argv = [binary, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"]
                env = None
            proc = await self.spawn(argv, env)
            tunnel = Tunnel(name=name, port=port, url=None, started_at=started_at,
                            provider=provider, process=proc)
            try:
                await self._wait_ready(tunnel)
            except BaseException:
                _kill(proc)
                raise
            tunnel.drain = asyncio.create_task(self._drain(tunnel), name=f"cloudflared-{name}")
            self.tunnels[name] = tunnel
            return tunnel

    async def _wait_ready(self, tunnel: Tunnel) -> None:
        registered = False
        try:
            async with asyncio.timeout(self.start_timeout_s):
                while True:
                    raw = await tunnel.process.stdout.readline()
                    if not raw:
                        tail = " | ".join(tunnel.log[-3:])[:300]
                        raise ApiError(502, "tunnel_failed", f"cloudflared exited: {tail}")
                    line = raw.decode("utf-8", "replace").strip()
                    tunnel.log = [*tunnel.log[-19:], line]
                    if tunnel.provider == "cloudflare_quick":
                        if m := QUICK_URL_RE.search(line):
                            tunnel.url = m.group(0)
                            return
                        continue
                    hosts = _hostnames(line)
                    if hosts:
                        tunnel.url = f"https://{hosts[0]}"
                        ports = _ingress_ports(line)
                        if ports and tunnel.port not in ports:
                            tunnel.note = (f"the Cloudflare dashboard routes this tunnel to port(s) "
                                           f"{ports}, not {tunnel.port}")
                        return
                    registered = registered or bool(READY_RE.search(line))
        except TimeoutError:
            if tunnel.provider == "cloudflare_token" and registered:
                tunnel.note = ("tunnel is running, but cloudflared did not print its public "
                               "hostname; it is the Public Hostname set for this tunnel in the "
                               "Cloudflare dashboard (Zero Trust > Networks > Tunnels)")
                return
            raise ApiError(504, "tunnel_timeout",
                           "cloudflared did not report a public URL in time") from None

    async def _drain(self, tunnel: Tunnel) -> None:
        """Keep reading output so the pipe never fills and blocks cloudflared."""
        with contextlib.suppress(Exception):
            while raw := await tunnel.process.stdout.readline():
                line = raw.decode("utf-8", "replace").strip()
                tunnel.log = [*tunnel.log[-19:], line]
                if tunnel.url is None and (hosts := _hostnames(line)):
                    tunnel.url = f"https://{hosts[0]}"
                    tunnel.note = None

    async def stop(self, name: str) -> bool:
        async with self._lock:
            tunnel = self.tunnels.pop(name, None)
        if tunnel is None:
            return False
        await _terminate(tunnel)
        return True

    async def stop_all(self) -> list[str]:
        async with self._lock:
            tunnels = list(self.tunnels.values())
            self.tunnels.clear()
        for tunnel in tunnels:
            await _terminate(tunnel)
        return [t.name for t in tunnels]


def _kill(proc: Process) -> None:
    with contextlib.suppress(ProcessLookupError, OSError):
        if proc.returncode is None:
            proc.kill()


async def _terminate(tunnel: Tunnel) -> None:
    if tunnel.drain is not None:
        tunnel.drain.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await tunnel.drain
    proc = tunnel.process
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.terminate()
    try:
        async with asyncio.timeout(5):
            await proc.wait()
    except TimeoutError:
        _kill(proc)
        with contextlib.suppress(Exception):
            await proc.wait()


def token_config_hint(token: str) -> str | None:
    """The tunnel id inside a token (base64 JSON ``{a, t, s}``), for messages. Never the secret."""
    import base64

    try:
        data = json.loads(base64.b64decode(token + "=" * (-len(token) % 4)))
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict) and isinstance(data.get("t"), str):  # pyright: ignore[reportUnknownMemberType]
        return str(data["t"])  # pyright: ignore[reportUnknownArgumentType]
    return None

"""Fakes for the integration tests: IMAP/SMTP mailboxes, an HTTP router, cloudflared processes."""

from __future__ import annotations

import asyncio
import imaplib
import json
import smtplib
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any

import httpx

from cashmaxx.config import GuardConfig, IntegrationConfig
from cashmaxx.guard.integrations.hosting import HostingManager
from cashmaxx.guard.integrations.service import IntegrationDeps

SECRET_MARK = "SeCrEt"
GMAIL_ADDR = "agent@gmail.com"
GMAIL_PW = f"{SECRET_MARK}-gmail-app-pw"


def connect(config: GuardConfig, integration_id: str, fields: dict[str, str],
            connected_at: str = "2026-09-01T12:00:00.000000Z") -> GuardConfig:
    config.integrations[integration_id] = IntegrationConfig(fields=fields,
                                                            connected_at=connected_at)
    return config


# --- IMAP / SMTP ---------------------------------------------------------------------------------
def make_raw(subject: str, body: str, *, sender: str = "buyer@example.com",
             html: str | None = None, message_id: str = "<m1@example.com>") -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = GMAIL_ADDR
    msg["Subject"] = subject
    msg["Date"] = "Tue, 01 Sep 2026 10:00:00 +0000"
    msg["Message-ID"] = message_id
    if html is not None and not body:
        msg.set_content(html, subtype="html")
    else:
        msg.set_content(body)
        if html is not None:
            msg.add_alternative(html, subtype="html")
    return bytes(msg)


@dataclass
class Mailbox:
    password: str = GMAIL_PW
    messages: dict[int, tuple[bytes, set[str], int]] = field(
        default_factory=dict[int, tuple[bytes, set[str], int]])
    sent: list[EmailMessage] = field(default_factory=list[EmailMessage])
    smtp_fail: Exception | None = None

    def add(self, uid: int, raw: bytes, *, seen: bool = False, thread: int = 777) -> None:
        self.messages[uid] = (raw, {"\\Seen"} if seen else set(), thread)

    def imap(self) -> FakeImap:
        return FakeImap(self)

    def smtp(self) -> FakeSmtp:
        return FakeSmtp(self)


class FakeImap:
    def __init__(self, box: Mailbox) -> None:
        self.box = box
        self.readonly = True

    def login(self, user: str, password: str) -> Any:
        if password != self.box.password:
            raise imaplib.IMAP4.error(f"[AUTHENTICATIONFAILED] Invalid credentials {password}")
        return ("OK", [b"ok"])

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> Any:
        self.readonly = readonly
        return ("OK", [str(len(self.box.messages)).encode()])

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        if command == "search":
            unseen = args[-1] == "UNSEEN"
            uids = [u for u, (_, flags, _) in sorted(self.box.messages.items())
                    if not (unseen and "\\Seen" in flags)]
            return "OK", [" ".join(str(u) for u in uids).encode()]
        assert command == "fetch"
        uid, spec = int(args[0]), str(args[1])
        if uid not in self.box.messages:
            return "OK", [None]
        raw, flags, thread = self.box.messages[uid]
        if "BODY[]" in spec and "PEEK" not in spec and not self.readonly:
            flags.add("\\Seen")
        if "HEADER.FIELDS" in spec:
            raw = raw.split(b"\n\n", 1)[0] + b"\n\n"
        meta = (f"1 (UID {uid} FLAGS ({' '.join(sorted(flags))}) X-GM-THRID {thread} "
                f"BODY[] {{{len(raw)}}}")
        return "OK", [(meta.encode(), raw), b")"]

    def logout(self) -> Any:
        return ("BYE", [])


class FakeSmtp:
    def __init__(self, box: Mailbox) -> None:
        self.box = box

    def login(self, user: str, password: str) -> Any:
        if password != self.box.password:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
        return (235, b"ok")

    def send_message(self, msg: EmailMessage) -> Any:
        if self.box.smtp_fail is not None:
            raise self.box.smtp_fail
        self.box.sent.append(msg)
        return {}

    def quit(self) -> Any:
        return (221, b"bye")


# --- HTTP ----------------------------------------------------------------------------------------
Route = Callable[[httpx.Request], httpx.Response]


@dataclass
class Router:
    """``httpx.MockTransport`` handler keyed by ``"METHOD host/path"`` prefixes."""

    routes: dict[str, Route] = field(default_factory=dict[str, Route])
    calls: list[httpx.Request] = field(default_factory=list[httpx.Request])

    def add(self, key: str, route: Route | dict[str, Any], status: int = 200) -> None:
        if isinstance(route, dict):
            body = route
            self.routes[key] = lambda _req: httpx.Response(status, json=body)
        else:
            self.routes[key] = route

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        raw_path = request.url.raw_path.decode().split("?", 1)[0]  # still percent-encoded
        key = f"{request.method} {request.url.host}{raw_path}"
        for prefix in sorted(self.routes, key=len, reverse=True):  # most specific first
            if key == prefix or key.startswith(prefix):
                return self.routes[prefix](request)
        return httpx.Response(404, json={"message": f"no route for {key}"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def json_of(self, index: int) -> Any:
        return json.loads(self.calls[index].content)


# --- cloudflared ---------------------------------------------------------------------------------
class FakeStream:
    def __init__(self, lines: list[str], eof: bool) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()
        for line in lines:
            self.queue.put_nowait((line + "\n").encode())
        if eof:
            self.queue.put_nowait(b"")

    async def readline(self) -> bytes:
        return await self.queue.get()


class FakeProcess:
    def __init__(self, lines: list[str], *, eof: bool = False) -> None:
        self.stdout = FakeStream(lines, eof)
        self.returncode: int | None = None
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0
        self.stdout.queue.put_nowait(b"")

    def kill(self) -> None:
        self.terminate()

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0)
        return self.returncode


@dataclass
class FakeCloudflared:
    installed: bool = True
    output: Callable[[list[str]], list[str]] | None = None
    spawned: list[tuple[list[str], dict[str, str] | None]] = field(
        default_factory=list[tuple[list[str], dict[str, str] | None]])
    processes: list[FakeProcess] = field(default_factory=list[FakeProcess])

    def which(self, name: str) -> str | None:
        return "/opt/homebrew/bin/cloudflared" if self.installed else None

    async def spawn(self, argv: list[str], env: dict[str, str] | None) -> FakeProcess:
        self.spawned.append((argv, env))
        if argv[1:] == ["--version"]:
            proc = FakeProcess(["cloudflared version 2026.9.0 (built 2026-09-01)"], eof=True)
            proc.returncode = 0
        else:
            n = len(self.processes) + 1
            lines = self.output(argv) if self.output else [
                "INF Requesting new quick Tunnel on trycloudflare.com...",
                f"INF |  https://quick-{n}-abc.trycloudflare.com  |",
            ]
            proc = FakeProcess(lines)
        self.processes.append(proc)
        return proc

    def manager(self, timeout: float = 0.3) -> HostingManager:
        return HostingManager(spawn=self.spawn, which=self.which, start_timeout_s=timeout)


def deps(*, router: Router | None = None, box: Mailbox | None = None,
         cloudflared: FakeCloudflared | None = None, **extra: Any) -> IntegrationDeps:
    box = box or Mailbox()
    return IntegrationDeps(
        http_transport=(router or Router()).transport(),
        imap_factory=box.imap, smtp_factory=box.smtp,
        hosting=(cloudflared or FakeCloudflared()).manager(), **extra,
    )

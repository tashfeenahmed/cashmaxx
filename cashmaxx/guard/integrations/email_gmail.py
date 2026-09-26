"""Gmail over IMAP (``imap.gmail.com:993``) and SMTP (``smtp.gmail.com:465``, 587 STARTTLS
fallback) with an app password. The stdlib clients are blocking, so every call runs in a thread.

Message ids are IMAP UIDs in INBOX. Listing uses ``BODY.PEEK`` so it never marks mail read;
reading one message does (``\\Seen``). Thread ids come from Gmail's ``X-GM-THRID`` extension.
"""

from __future__ import annotations

import asyncio
import imaplib
import re
import smtplib
import ssl
from collections.abc import Callable
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any, Protocol, TypeVar, cast

from cashmaxx.guard.integrations.email_common import (
    EmailError,
    addresses,
    body_text,
    clip,
    iso_date,
    snippet,
)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.gmail.com"
SMTP_SSL_PORT = 465
SMTP_STARTTLS_PORT = 587
TIMEOUT_S = 30
MAX_LIST = 50
_UID_RE = re.compile(r"^\d{1,12}$")
T = TypeVar("T")


class ImapConn(Protocol):
    def login(self, user: str, password: str) -> Any: ...

    def select(self, mailbox: str = ..., readonly: bool = ...) -> Any: ...

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]: ...

    def logout(self) -> Any: ...


class SmtpConn(Protocol):
    def login(self, user: str, password: str) -> Any: ...

    def send_message(self, msg: EmailMessage) -> Any: ...

    def quit(self) -> Any: ...


ImapFactory = Callable[[], ImapConn]
SmtpFactory = Callable[[], SmtpConn]


def default_imap() -> ImapConn:
    return cast(ImapConn, imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=TIMEOUT_S))


def default_smtp() -> SmtpConn:
    context = ssl.create_default_context()
    try:
        return cast(SmtpConn, smtplib.SMTP_SSL(SMTP_HOST, SMTP_SSL_PORT, timeout=TIMEOUT_S,
                                               context=context))
    except OSError:
        conn = smtplib.SMTP(SMTP_HOST, SMTP_STARTTLS_PORT, timeout=TIMEOUT_S)
        conn.starttls(context=context)
        return cast(SmtpConn, conn)


def _meta_value(meta: str, key: str) -> str | None:
    m = re.search(rf"{key} (\d+)", meta)
    return m.group(1) if m else None


def _parse_fetch(data: list[Any]) -> tuple[str, bytes] | None:
    """``(meta, raw)`` from an imaplib FETCH response, or ``None`` if nothing came back."""
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2:  # pyright: ignore[reportUnknownArgumentType]
            parts = cast(tuple[Any, ...], item)
            meta, raw = parts[0], parts[1]
            if isinstance(meta, bytes) and isinstance(raw, bytes):
                return meta.decode("utf-8", "replace"), raw
    return None


def _flags(meta: str) -> list[str]:
    m = re.search(r"FLAGS \(([^)]*)\)", meta)
    return m.group(1).split() if m else []


class GmailProvider:
    name = "gmail"

    def __init__(
        self,
        address: str,
        app_password: str,
        *,
        imap_factory: ImapFactory | None = None,
        smtp_factory: SmtpFactory | None = None,
    ) -> None:
        self._address = address
        self._password = app_password.replace(" ", "")  # Google shows it in groups of four
        self._imap_factory = imap_factory or default_imap
        self._smtp_factory = smtp_factory or default_smtp

    @property
    def address(self) -> str | None:
        return self._address

    # --- plumbing ---------------------------------------------------------------------------
    def _imap(self, *, readonly: bool = True) -> ImapConn:
        conn = self._imap_factory()
        try:
            conn.login(self._address, self._password)
            status, _ = cast(tuple[str, Any], conn.select("INBOX", readonly=readonly))
            if status != "OK":
                raise EmailError("could not open INBOX")
        except BaseException:
            _quietly(conn.logout)
            raise
        return conn

    @staticmethod
    def _check_uid(message_id: str) -> str:
        if not _UID_RE.match(message_id):
            raise EmailError("unknown message id")
        return message_id

    def _item(self, uid: str, meta: str, raw: bytes, *, full: bool) -> dict[str, Any]:
        msg = message_from_bytes(raw, policy=policy.default)
        text = body_text(msg)
        item: dict[str, Any] = {
            "id": uid,
            "from": str(msg.get("from", "")),
            "to": addresses(msg.get("to")),
            "subject": str(msg.get("subject", "")),
            "date": iso_date(msg.get("date")),
            "thread_id": _meta_value(meta, "X-GM-THRID"),
        }
        if full:
            item.update({"cc": addresses(msg.get("cc")), "text": clip(text)})
        else:
            item.update({"snippet": snippet(text), "unread": "\\Seen" not in _flags(meta)})
        return item

    # --- blocking bodies ----------------------------------------------------------------------
    def _test_sync(self) -> str:
        conn = self._imap()
        _quietly(conn.logout)
        smtp = self._smtp_factory()
        try:
            smtp.login(self._address, self._password)
        finally:
            _quietly(smtp.quit)
        return f"IMAP and SMTP login ok for {self._address}"

    def _reply_headers(self, in_reply_to: str) -> tuple[str, str]:
        """``(Message-ID, References)`` of the message being answered."""
        if not _UID_RE.match(in_reply_to):
            if re.fullmatch(r"<[^<>\s]{3,500}>", in_reply_to):
                return in_reply_to, in_reply_to
            raise EmailError("in_reply_to must be a message id from the inbox")
        conn = self._imap()
        try:
            _, data = conn.uid("fetch", in_reply_to,
                               "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES)])")
        finally:
            _quietly(conn.logout)
        parsed = _parse_fetch(data)
        if parsed is None:
            raise EmailError("in_reply_to: no such message")
        headers = message_from_bytes(parsed[1], policy=policy.default)
        mid = str(headers.get("message-id", "")).strip()
        if not mid:
            raise EmailError("in_reply_to: the message has no Message-ID")
        refs = str(headers.get("references", "")).strip()
        return mid, f"{refs} {mid}".strip()

    def _send_sync(
        self, to: list[str], subject: str, text: str, html: str | None, in_reply_to: str | None
    ) -> str:
        msg = EmailMessage()
        msg["From"] = self._address
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        msg["Date"] = formatdate(usegmt=True)
        domain = self._address.rsplit("@", 1)[-1]
        message_id = make_msgid(domain=domain)
        msg["Message-ID"] = message_id
        if in_reply_to:
            parent, refs = self._reply_headers(in_reply_to)
            msg["In-Reply-To"] = parent
            msg["References"] = refs
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        smtp = self._smtp_factory()
        try:
            smtp.login(self._address, self._password)
            smtp.send_message(msg)
        finally:
            _quietly(smtp.quit)
        return message_id

    def _inbox_sync(self, unread_only: bool, limit: int) -> list[dict[str, Any]]:
        conn = self._imap()
        try:
            _, data = conn.uid("search", None, "UNSEEN" if unread_only else "ALL")
            raw_ids = data[0] if data and isinstance(data[0], bytes) else b""
            uids = raw_ids.decode().split()[-limit:][::-1]  # newest first
            items: list[dict[str, Any]] = []
            for uid in uids:
                _, fetched = conn.uid("fetch", uid, "(UID FLAGS X-GM-THRID BODY.PEEK[])")
                parsed = _parse_fetch(fetched)
                if parsed is not None:
                    items.append(self._item(uid, parsed[0], parsed[1], full=False))
            return items
        finally:
            _quietly(conn.logout)

    def _message_sync(self, message_id: str) -> dict[str, Any]:
        uid = self._check_uid(message_id)
        conn = self._imap(readonly=False)
        try:
            _, fetched = conn.uid("fetch", uid, "(UID FLAGS X-GM-THRID BODY[])")
        finally:
            _quietly(conn.logout)
        parsed = _parse_fetch(fetched)
        if parsed is None:
            raise EmailError("unknown message id")
        return self._item(uid, parsed[0], parsed[1], full=True)

    # --- async API ----------------------------------------------------------------------------
    async def test(self) -> str:
        return await _run(self._test_sync)

    async def send(
        self, *, to: list[str], subject: str, text: str, html: str | None,
        in_reply_to: str | None,
    ) -> str:
        return await _run(lambda: self._send_sync(to, subject, text, html, in_reply_to))

    async def inbox(self, *, unread_only: bool, limit: int) -> list[dict[str, Any]]:
        return await _run(lambda: self._inbox_sync(unread_only, max(1, min(limit, MAX_LIST))))

    async def message(self, message_id: str) -> dict[str, Any]:
        return await _run(lambda: self._message_sync(message_id))


def _quietly(fn: Callable[[], Any]) -> None:
    try:
        fn()
    except Exception:
        pass


async def _run(fn: Callable[[], T]) -> T:
    try:
        return await asyncio.to_thread(fn)
    except EmailError:
        raise
    except imaplib.IMAP4.error as exc:
        raise EmailError(f"IMAP error: {exc}") from exc
    except smtplib.SMTPAuthenticationError as exc:
        raise EmailError("SMTP login failed: check the address and app password") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailError(f"{type(exc).__name__}: {exc}") from exc

"""Shared email helpers: the provider protocol, HTML-to-text and message shaping.

Everything read from a mailbox is untrusted: bodies are plain text only, capped, and every
message the API returns carries ``untrusted: true``.
"""

from __future__ import annotations

import html
import re
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Protocol

MAX_BODY_CHARS = 12_000
SNIPPET_CHARS = 200


class EmailError(Exception):
    """A provider call failed. The message must not contain secrets (the caller scrubs it too)."""


class EmailProvider(Protocol):
    name: str

    @property
    def address(self) -> str | None: ...

    async def test(self) -> str: ...

    async def send(
        self, *, to: list[str], subject: str, text: str, html: str | None,
        in_reply_to: str | None,
    ) -> str: ...

    async def inbox(self, *, unread_only: bool, limit: int) -> list[dict[str, Any]]: ...

    async def message(self, message_id: str) -> dict[str, Any]: ...


_BLOCK_TAGS = frozenset({
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
    "table", "ul", "ol", "hr", "section", "article", "header", "footer",
})
_SKIP_TAGS = frozenset({"script", "style", "head", "title", "noscript", "template"})


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
        text = "".join(parser.parts)
    except Exception:  # malformed markup: fall back to crude tag stripping
        text = html.unescape(re.sub(r"<[^>]*>", " ", markup))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def clip(text: str, limit: int = MAX_BODY_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "\n[truncated]"


def snippet(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()[:SNIPPET_CHARS]


def body_text(msg: EmailMessage) -> str:
    """Plain-text body: the text/plain part, else the HTML part converted to text."""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, ValueError):
        payload = part.get_payload(decode=True)
        content = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else ""
    if not isinstance(content, str):
        return ""
    if part.get_content_type() == "text/html":
        return html_to_text(content)
    return content.strip()


def addresses(value: str | None) -> list[str]:
    if not value:
        return []
    return [addr if not name else f"{name} <{addr}>"
            for name, addr in getaddresses([value]) if addr]


def iso_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return value

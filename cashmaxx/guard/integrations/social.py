"""Posting to Bluesky, X and Reddit with the owner's credentials (held only by the guard).

- Bluesky: atproto XRPC ``com.atproto.server.createSession`` then ``com.atproto.repo.createRecord``
  (``app.bsky.feed.post``), with a link facet when a link is attached.
- X: API v2 ``POST /2/tweets`` signed with OAuth 1.0a user context (HMAC-SHA1, stdlib only).
- Reddit: script-app password grant at ``/api/v1/access_token``, then ``POST /api/submit``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

BSKY_BASE = "https://bsky.social/xrpc"
X_BASE = "https://api.x.com/2"
REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_API = "https://oauth.reddit.com"
USER_AGENT = "cashmaxx-guard/0.1"
BSKY_MAX_CHARS = 300
X_MAX_CHARS = 280
PLATFORMS = ("bluesky", "x", "reddit")


class SocialError(Exception):
    """A platform call failed. Messages carry no credentials."""


# --- OAuth 1.0a (RFC 5849) -------------------------------------------------------------------
def pct(value: str) -> str:
    """RFC 3986 percent-encoding, as OAuth 1.0a requires."""
    return quote(value, safe="-._~")


def _base_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))


def oauth1_signature(
    method: str, url: str, params: dict[str, str], consumer_secret: str, token_secret: str
) -> str:
    """HMAC-SHA1 signature over the method, base URL and all (oauth + request) parameters."""
    pairs = sorted((pct(k), pct(v)) for k, v in params.items())
    param_string = "&".join(f"{k}={v}" for k, v in pairs)
    base = "&".join((method.upper(), pct(_base_url(url)), pct(param_string)))
    key = f"{pct(consumer_secret)}&{pct(token_secret)}"
    digest = hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def oauth1_header(
    method: str,
    url: str,
    *,
    consumer_key: str,
    consumer_secret: str,
    token: str,
    token_secret: str,
    request_params: dict[str, str] | None = None,
    nonce: str | None = None,
    timestamp: str | None = None,
) -> str:
    """The ``Authorization`` header. JSON bodies are not signed (only query/form params are)."""
    oauth = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp or str(int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    oauth["oauth_signature"] = oauth1_signature(
        method, url, {**(request_params or {}), **oauth}, consumer_secret, token_secret
    )
    return "OAuth " + ", ".join(f'{pct(k)}="{pct(v)}"' for k, v in sorted(oauth.items()))


# --- helpers ---------------------------------------------------------------------------------
def _json(resp: httpx.Response, what: str) -> dict[str, Any]:
    if resp.status_code >= 400:
        detail = ""
        try:
            body = resp.json()
            if isinstance(body, dict):
                b = cast(dict[str, Any], body)
                detail = str(b.get("message") or b.get("detail") or b.get("error")
                             or b.get("title") or "")[:200]
        except ValueError:
            pass
        raise SocialError(f"{what}: HTTP {resp.status_code} {detail}".strip())
    try:
        data = resp.json()
    except ValueError as exc:
        raise SocialError(f"{what}: non-JSON response") from exc
    return cast(dict[str, Any], data) if isinstance(data, dict) else {}


def compose(text: str, link: str | None) -> str:
    return f"{text.rstrip()}\n{link}" if link else text


# --- Bluesky ---------------------------------------------------------------------------------
async def bluesky_session(http: httpx.AsyncClient, values: dict[str, str]) -> dict[str, Any]:
    resp = await http.post(f"{BSKY_BASE}/com.atproto.server.createSession", json={
        "identifier": values["handle"], "password": values["appPassword"]})
    return _json(resp, "Bluesky createSession")


def bluesky_link_facet(text: str, link: str) -> dict[str, Any] | None:
    start_char = text.rfind(link)
    if start_char < 0:
        return None
    start = len(text[:start_char].encode("utf-8"))
    end = start + len(link.encode("utf-8"))
    return {"index": {"byteStart": start, "byteEnd": end},
            "features": [{"$type": "app.bsky.richtext.facet#link", "uri": link}]}


async def post_bluesky(
    http: httpx.AsyncClient, values: dict[str, str], text: str, link: str | None
) -> str:
    body = compose(text, link)
    if len(body) > BSKY_MAX_CHARS:
        raise SocialError(f"Bluesky posts are limited to {BSKY_MAX_CHARS} characters")
    session = await bluesky_session(http, values)
    did, handle = str(session.get("did", "")), str(session.get("handle") or values["handle"])
    record: dict[str, Any] = {
        "$type": "app.bsky.feed.post", "text": body,
        "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    if link and (facet := bluesky_link_facet(body, link)):
        record["facets"] = [facet]
    resp = await http.post(
        f"{BSKY_BASE}/com.atproto.repo.createRecord",
        headers={"Authorization": f"Bearer {session.get('accessJwt', '')}"},
        json={"repo": did, "collection": "app.bsky.feed.post", "record": record},
    )
    uri = str(_json(resp, "Bluesky createRecord").get("uri", ""))
    rkey = uri.rsplit("/", 1)[-1]
    return f"https://bsky.app/profile/{handle}/post/{rkey}"


# --- X ---------------------------------------------------------------------------------------
def _x_auth(values: dict[str, str], method: str, url: str) -> dict[str, str]:
    return {"Authorization": oauth1_header(
        method, url, consumer_key=values["apiKey"], consumer_secret=values["apiSecret"],
        token=values["accessToken"], token_secret=values["accessSecret"])}


async def x_me(http: httpx.AsyncClient, values: dict[str, str]) -> dict[str, Any]:
    url = f"{X_BASE}/users/me"
    data = _json(await http.get(url, headers=_x_auth(values, "GET", url)), "X users/me")
    return cast(dict[str, Any], data.get("data") or {})


async def post_x(
    http: httpx.AsyncClient, values: dict[str, str], text: str, link: str | None
) -> str:
    body = compose(text, link)
    url = f"{X_BASE}/tweets"
    resp = await http.post(url, headers=_x_auth(values, "POST", url), json={"text": body})
    data = cast(dict[str, Any], _json(resp, "X create post").get("data") or {})
    post_id = str(data.get("id", ""))
    if not post_id:
        raise SocialError("X did not return a post id")
    return f"https://x.com/i/status/{post_id}"


# --- Reddit ----------------------------------------------------------------------------------
def _reddit_ua(values: dict[str, str]) -> str:
    return f"{USER_AGENT} (by /u/{values['username']})"


async def reddit_token(http: httpx.AsyncClient, values: dict[str, str]) -> str:
    resp = await http.post(
        REDDIT_TOKEN_URL,
        auth=(values["clientId"], values["clientSecret"]),
        data={"grant_type": "password", "username": values["username"],
              "password": values["password"]},
        headers={"User-Agent": _reddit_ua(values)},
    )
    data = _json(resp, "Reddit token")
    token = data.get("access_token")
    if not token:
        raise SocialError(f"Reddit token: {str(data.get('error', 'no access token'))[:100]}")
    return str(token)


async def reddit_me(http: httpx.AsyncClient, values: dict[str, str]) -> dict[str, Any]:
    token = await reddit_token(http, values)
    resp = await http.get(f"{REDDIT_API}/api/v1/me", headers={
        "Authorization": f"Bearer {token}", "User-Agent": _reddit_ua(values)})
    return _json(resp, "Reddit me")


async def post_reddit(
    http: httpx.AsyncClient, values: dict[str, str], *, subreddit: str, title: str, text: str,
    link: str | None,
) -> str:
    token = await reddit_token(http, values)
    form = {"sr": subreddit, "title": title, "api_type": "json", "resubmit": "true"}
    if link:
        form.update({"kind": "link", "url": link})
    else:
        form.update({"kind": "self", "text": text})
    resp = await http.post(f"{REDDIT_API}/api/submit", data=form, headers={
        "Authorization": f"Bearer {token}", "User-Agent": _reddit_ua(values)})
    payload = cast(dict[str, Any], _json(resp, "Reddit submit").get("json") or {})
    errors = cast(list[Any], payload.get("errors") or [])
    if errors:
        raise SocialError(f"Reddit submit: {str(errors[0])[:200]}")
    data = cast(dict[str, Any], payload.get("data") or {})
    url = str(data.get("url") or "")
    if not url:
        raise SocialError("Reddit did not return a post URL")
    return url


# --- dispatch --------------------------------------------------------------------------------
async def post(
    http: httpx.AsyncClient, platform: str, values: dict[str, str], *, text: str,
    link: str | None, subreddit: str | None, title: str | None,
) -> str:
    if platform == "bluesky":
        return await post_bluesky(http, values, text, link)
    if platform == "x":
        return await post_x(http, values, text, link)
    if platform == "reddit":
        if not subreddit or not title:
            raise SocialError("reddit needs subreddit and title")
        return await post_reddit(http, values, subreddit=subreddit, title=title, text=text,
                                 link=link)
    raise SocialError(f"unknown platform {platform}")


async def test(http: httpx.AsyncClient, platform: str, values: dict[str, str]) -> str:
    if platform == "bluesky":
        session = await bluesky_session(http, values)
        return f"Bluesky login ok as {session.get('handle') or values['handle']}"
    if platform == "x":
        me = await x_me(http, values)
        return f"X credentials ok for @{me.get('username', '?')}"
    if platform == "reddit":
        me = await reddit_me(http, values)
        return f"Reddit login ok as u/{me.get('name', values['username'])}"
    raise SocialError(f"unknown platform {platform}")

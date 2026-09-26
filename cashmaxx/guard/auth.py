"""Guard authentication: agent token, owner PIN (scrypt) and in-memory owner sessions.

- The agent token is stored as a sha256 hex digest and compared with ``hmac.compare_digest``.
- The owner PIN is stored as ``scrypt$n$r$p$salt_hex$hash_hex``.
- Owner sessions live only in this process's memory (12h TTL); a guard restart logs the owner out.
- ``POST /owner/session`` is rate limited per remote address and globally.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

Clock = Callable[[], datetime]

SESSION_TTL = timedelta(hours=12)
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --- agent token ---------------------------------------------------------------------------------
def new_agent_token() -> str:
    return secrets.token_urlsafe(32)


def hash_agent_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def check_agent_token(token: str | None, stored_hash: str) -> bool:
    if not token or not stored_hash:
        return False
    return hmac.compare_digest(hash_agent_token(token), stored_hash.strip().lower())


# --- owner PIN -----------------------------------------------------------------------------------
def hash_pin(pin: str, *, n: int = _SCRYPT_N, r: int = _SCRYPT_R, p: int = _SCRYPT_P) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(pin.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_SCRYPT_DKLEN)
    return f"scrypt${n}${r}${p}${salt.hex()}${digest.hex()}"


def verify_pin(pin: str, stored: str) -> bool:
    try:
        scheme, n_s, r_s, p_s, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(hash_hex)
    except ValueError:
        return False
    if not (2 <= n <= 2**20 and 1 <= r <= 32 and 1 <= p <= 16):
        return False
    digest = hashlib.scrypt(
        pin.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected), maxmem=2**27
    )
    return hmac.compare_digest(digest, expected)


# --- owner sessions ------------------------------------------------------------------------------
class OwnerSessions:
    """Opaque session tokens kept in memory only."""

    def __init__(self, clock: Clock = _utcnow, ttl: timedelta = SESSION_TTL) -> None:
        self._clock = clock
        self._ttl = ttl
        self._sessions: dict[str, datetime] = {}

    def create(self) -> tuple[str, datetime]:
        self._purge()
        token = secrets.token_urlsafe(32)
        expires = self._clock() + self._ttl
        self._sessions[hashlib.sha256(token.encode()).hexdigest()] = expires
        return token, expires

    def check(self, token: str | None) -> bool:
        if not token:
            return False
        key = hashlib.sha256(token.encode()).hexdigest()
        expires = self._sessions.get(key)
        if expires is None:
            return False
        if expires <= self._clock():
            self._sessions.pop(key, None)
            return False
        return True

    def revoke_all(self) -> None:
        self._sessions.clear()

    def _purge(self) -> None:
        now = self._clock()
        for key in [k for k, exp in self._sessions.items() if exp <= now]:
            del self._sessions[key]


# --- rate limiting -------------------------------------------------------------------------------
class RateLimiter:
    """Sliding-window limiter: ``per_key`` attempts per window per key and ``total`` overall."""

    def __init__(
        self,
        *,
        per_key: int = 5,
        total: int = 5,
        window: timedelta = timedelta(minutes=1),
        clock: Clock = _utcnow,
    ) -> None:
        self._per_key = per_key
        self._total = total
        self._window = window
        self._clock = clock
        self._by_key: dict[str, deque[datetime]] = {}
        self._all: deque[datetime] = deque()

    def _trim(self, q: deque[datetime], now: datetime) -> None:
        while q and q[0] <= now - self._window:
            q.popleft()

    def allow(self, key: str) -> bool:
        """Record an attempt; return False (without recording) when over either limit."""
        now = self._clock()
        self._trim(self._all, now)
        q = self._by_key.setdefault(key, deque())
        self._trim(q, now)
        if len(q) >= self._per_key or len(self._all) >= self._total:
            return False
        q.append(now)
        self._all.append(now)
        for k in [k for k, v in self._by_key.items() if not v]:
            del self._by_key[k]
        return True

from __future__ import annotations

from datetime import timedelta

from guard_testlib import FakeClock

from cashmaxx.guard.auth import (
    OwnerSessions,
    RateLimiter,
    check_agent_token,
    hash_agent_token,
    hash_pin,
    new_agent_token,
    verify_pin,
)


def test_agent_token_hash_roundtrip() -> None:
    token = new_agent_token()
    stored = hash_agent_token(token)
    assert token not in stored and len(stored) == 64
    assert check_agent_token(token, stored)
    assert not check_agent_token(token + "x", stored)
    assert not check_agent_token(None, stored)
    assert not check_agent_token(token, "")


def test_pin_hash_format_and_verify() -> None:
    stored = hash_pin("123456", n=2**10)
    scheme, n, r, p, salt, digest = stored.split("$")
    assert (scheme, n, r, p) == ("scrypt", "1024", "8", "1")
    assert len(salt) == 32 and len(digest) == 64
    assert verify_pin("123456", stored)
    assert not verify_pin("123457", stored)
    assert hash_pin("123456", n=2**10) != stored  # salted
    assert not verify_pin("123456", "garbage")
    assert not verify_pin("123456", "bcrypt$1$2$3$aa$bb")


def test_owner_sessions_expire_after_12h() -> None:
    clock = FakeClock()
    sessions = OwnerSessions(clock=clock)
    token, expires = sessions.create()
    assert expires - clock() == timedelta(hours=12)
    assert sessions.check(token)
    assert not sessions.check("nope") and not sessions.check(None)
    clock.advance(hours=11, minutes=59)
    assert sessions.check(token)
    clock.advance(minutes=2)
    assert not sessions.check(token)


def test_rate_limiter_per_key_and_global() -> None:
    clock = FakeClock()
    limiter = RateLimiter(per_key=5, total=8, clock=clock)
    assert all(limiter.allow("a") for _ in range(5))
    assert not limiter.allow("a")
    assert all(limiter.allow("b") for _ in range(3))
    assert not limiter.allow("c")  # global budget spent
    clock.advance(seconds=61)
    assert limiter.allow("a") and limiter.allow("c")

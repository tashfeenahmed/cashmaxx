from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from guard_testlib import AGENT_TOKEN, Env, EnvFactory, FakeClock, RecordingNotifier, make_config

from cashmaxx.config import GuardConfig
from cashmaxx.guard.app import GUARD_KEY, WATCHERS_KEY, create_app
from cashmaxx.guard.wallet.fake import FakeWallet


@pytest.fixture
async def make_env(tmp_path: Path) -> AsyncIterator[EnvFactory]:
    envs: list[Env] = []

    async def factory(
        *, wallet: FakeWallet | None = None, config: GuardConfig | None = None, **kwargs: Any
    ) -> Env:
        idx = len(envs)
        config = config or make_config(**kwargs.pop("settings", {}))
        clock = kwargs.pop("clock", None) or FakeClock()
        notifier = RecordingNotifier()
        wallet = wallet or FakeWallet()
        config_path = tmp_path / f"guard{idx}.json"
        app = create_app(
            config, config_path=config_path, db_path=tmp_path / f"guard{idx}.sqlite3",
            wallet=wallet, notifier=notifier, clock=clock, watch_interval_s=None, **kwargs,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        env = Env(
            app=app, client=client, guard=app[GUARD_KEY], watchers=app[WATCHERS_KEY],
            wallet=wallet, notifier=notifier, clock=clock, config_path=config_path,
            agent={"Authorization": f"Bearer {AGENT_TOKEN}"},
        )
        envs.append(env)
        return env

    yield factory
    for env in envs:
        await env.client.close()


@pytest.fixture
async def env(make_env: EnvFactory) -> Env:
    return await make_env()

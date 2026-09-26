"""Fake guard for the Cashmaxx plugin tests (an ``httpx.MockTransport`` handler)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

AGENT_TOKEN = "agent-token-123"


@dataclass
class FakeGuard:
    responses: dict[tuple[str, str], tuple[int, dict[str, Any]]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    down: bool = False

    def set(self, method: str, path: str, body: dict[str, Any], status: int = 200) -> None:
        self.responses[(method, path)] = (status, body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        key = (request.method, request.url.path)
        if key not in self.responses:
            return httpx.Response(404, json={"error": "not_found", "message": str(key)})
        status, body = self.responses[key]
        return httpx.Response(status, json=body)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def last_json(self) -> dict[str, Any]:
        return json.loads(self.last.content or b"{}")

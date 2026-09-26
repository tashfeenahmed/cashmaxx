# pyright: reportMissingTypeStubs=false
"""Coinbase CDP server-wallet backend (``cdp-sdk``) with x402 payments and incoming USDC scanning.

- Account: ``CdpClient(...).evm.get_or_create_account(name=account_name)``; CDP holds the key.
- Balance: CDP ``list_token_balances`` filtered to the canonical USDC contract.
- Transfers: ``EvmServerAccount.transfer(to, amount_units, "usdc", network)`` -> tx hash.
- x402: the ``x402`` client (exact EVM scheme) signs EIP-3009 authorizations with
  ``cdp.EvmLocalAccount`` (an eth_account ``BaseAccount`` that signs remotely through CDP).
  Its signing calls are synchronous HTTP, so payload creation runs in a worker thread.
- Incoming USDC: ``eth_getLogs`` for ``Transfer(from, to=our address)`` on the USDC contract via a
  public Base JSON-RPC endpoint (``CASHMAXX_BASE_RPC_URL`` overrides it), with a block cursor.

Nothing here is exercised against the live network by the test-suite; the pure helpers (amount
conversion, log parsing, requirement filtering and the x402 round trip with a local signer) are.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

import httpx
from loguru import logger

from cashmaxx.config import CdpConfig
from cashmaxx.guard.wallet import USDC_ADDRESSES, Incoming, WalletError
from cashmaxx.money import ZERO, fmt_usd, from_usdc_units, to_usdc_units

if TYPE_CHECKING:
    from cdp import CdpClient
    from cdp.evm_server_account import EvmServerAccount

ChainNetwork = Literal["base", "base-sepolia"]

CAIP2: dict[str, str] = {"base": "eip155:8453", "base-sepolia": "eip155:84532"}
PUBLIC_RPC: dict[str, str] = {
    "base": "https://mainnet.base.org",
    "base-sepolia": "https://sepolia.base.org",
}
# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
MAX_LOG_RANGE = 1000  # public RPCs cap eth_getLogs ranges; scan in chunks
CONFIRMATIONS = 2
MAX_BODY_CHARS = 200_000


# --- pure helpers --------------------------------------------------------------------------------
def address_topic(address: str) -> str:
    """32-byte left-padded topic for an address."""
    return "0x" + address.lower().removeprefix("0x").rjust(64, "0")


def parse_transfer_log(log: dict[str, Any]) -> Incoming:
    """Parse an ERC-20 ``Transfer`` log (JSON-RPC shape) into an ``Incoming``."""
    topics: list[Any] = log.get("topics") or []
    if len(topics) < 3 or str(topics[0]).lower() != TRANSFER_TOPIC:
        raise ValueError("not an ERC-20 Transfer log")
    data = str(log.get("data") or "0x0")
    return Incoming(
        tx_hash=str(log["transactionHash"]),
        log_index=int(str(log["logIndex"]), 16),
        from_addr="0x" + str(topics[1])[-40:],
        amount_usd=from_usdc_units(int(data, 16) if data not in ("0x", "") else 0),
        block=int(str(log["blockNumber"]), 16),
    )


def requirement_filter(network: str, max_units: int) -> Callable[[int, list[Any]], list[Any]]:
    """x402 client policy: only our network, only USDC, never above ``max_units``."""
    usdc = USDC_ADDRESSES[network].lower()
    allowed_networks = {CAIP2[network], network}

    def policy(_version: int, reqs: list[Any]) -> list[Any]:
        return [
            r for r in reqs
            if str(r.network) in allowed_networks
            and str(r.asset).lower() == usdc
            and int(r.get_amount()) <= max_units
        ]

    return policy


async def x402_paid_fetch(
    http: httpx.AsyncClient,
    signer: Any,
    network: str,
    url: str,
    method: str,
    body: str | None,
    max_usd: Decimal,
) -> tuple[int, str, Decimal]:
    """Request ``url``; on 402 pay (at most ``max_usd``, USDC on ``network``) and retry once.

    ``signer`` is an eth_account ``BaseAccount`` (e.g. ``cdp.EvmLocalAccount``) or any x402
    ``ClientEvmSigner``. Returns ``(http_status, body_text, paid_usd)``.
    """
    from x402 import x402ClientSync
    from x402.http import x402HTTPClientSync
    from x402.mechanisms.evm.exact import (
        register_exact_evm_client,  # pyright: ignore[reportUnknownVariableType]
    )

    content = body.encode("utf-8") if body is not None else None
    resp = await http.request(method, url, content=content)
    if resp.status_code != 402:
        return resp.status_code, resp.text[:MAX_BODY_CHARS], ZERO

    chosen: list[Any] = []

    def selector(_version: int, reqs: list[Any]) -> Any:
        chosen.append(reqs[0])
        return reqs[0]

    client = x402ClientSync(payment_requirements_selector=selector)
    register_exact_evm_client(client, signer, networks=CAIP2[network])
    client.register_policy(requirement_filter(network, to_usdc_units(max_usd)))
    client.set_spend_controls({"max_amount_per_payment": f"${fmt_usd(max_usd)}"})
    http_client = x402HTTPClientSync(client)

    headers = {k.upper(): v for k, v in resp.headers.items()}
    try:
        body_data: Any = json.loads(resp.content) if resp.content else None
    except ValueError:
        body_data = None
    try:
        required = http_client.get_payment_required_response(
            lambda name: headers.get(name.upper()), body_data
        )
        payload = await asyncio.to_thread(client.create_payment_payload, required)
    except Exception as exc:  # no affordable/compatible option, or signing failed
        logger.info("x402: not paying {}: {}", url, exc)
        return 402, f"x402 payment not made: {exc}"[:MAX_BODY_CHARS], ZERO

    pay_headers = http_client.encode_payment_signature_header(payload)
    paid_resp = await http.request(method, url, content=content, headers=pay_headers)
    paid = ZERO
    if chosen and paid_resp.status_code < 400:
        paid = from_usdc_units(int(chosen[-1].get_amount()))
    return paid_resp.status_code, paid_resp.text[:MAX_BODY_CHARS], paid


# --- JSON-RPC ------------------------------------------------------------------------------------
class BaseRpc:
    def __init__(self, url: str, http: httpx.AsyncClient) -> None:
        self.url = url
        self._http = http
        self._id = 0

    async def call(self, method: str, params: list[Any]) -> Any:
        self._id += 1
        resp = await self._http.post(
            self.url, json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            raise WalletError(f"rpc {method}: {data['error']}")
        return data.get("result")

    async def block_number(self) -> int:
        return int(str(await self.call("eth_blockNumber", [])), 16)

    async def transfer_logs(self, token: str, to: str, start: int, end: int) -> list[Incoming]:
        logs: list[dict[str, Any]] | None = await self.call("eth_getLogs", [{
            "address": token,
            "fromBlock": hex(start),
            "toBlock": hex(end),
            "topics": [TRANSFER_TOPIC, None, address_topic(to)],
        }])
        return [parse_transfer_log(log) for log in logs or []]


# --- backend -------------------------------------------------------------------------------------
class CdpWallet:
    """Real wallet. Created lazily inside the running event loop (the CDP client uses aiohttp)."""

    def __init__(
        self,
        cdp: CdpConfig,
        network: ChainNetwork,
        *,
        rpc_url: str | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        if network not in CAIP2:
            raise ValueError(f"CdpWallet does not support network {network!r}")
        if not (cdp.api_key_id and cdp.api_key_secret and cdp.wallet_secret):
            raise ValueError("CDP credentials are missing from guard.json (cdp.*)")
        self.network: str = network
        self._cfg = cdp
        self._http = http or httpx.AsyncClient(timeout=30.0, follow_redirects=False)
        rpc = rpc_url or os.environ.get("CASHMAXX_BASE_RPC_URL") or PUBLIC_RPC[network]
        self._rpc = BaseRpc(rpc, self._http)
        self._client: CdpClient | None = None
        self._account: EvmServerAccount | None = None
        self._lock = asyncio.Lock()

    async def _get_account(self) -> EvmServerAccount:
        async with self._lock:
            if self._account is None:
                from cdp import CdpClient

                self._client = CdpClient(
                    api_key_id=self._cfg.api_key_id,
                    api_key_secret=self._cfg.api_key_secret,
                    wallet_secret=self._cfg.wallet_secret,
                )
                self._account = await self._client.evm.get_or_create_account(
                    name=self._cfg.account_name
                )
                logger.info("cdp: using account {} on {}", self._account.address, self.network)
            return self._account

    async def address(self) -> str:
        return (await self._get_account()).address

    async def balance_usdc(self) -> Decimal:
        account = await self._get_account()
        usdc = USDC_ADDRESSES[self.network].lower()
        page_token: str | None = None
        while True:
            result = await account.list_token_balances(self.network, page_token=page_token)
            for bal in result.balances:
                if bal.token.contract_address.lower() == usdc:
                    return from_usdc_units(int(bal.amount.amount))
            page_token = result.next_page_token
            if not page_token:
                return ZERO

    async def transfer_usdc(self, to: str, amount: Decimal) -> str:
        account = await self._get_account()
        try:
            tx_hash = await account.transfer(
                to=to, amount=to_usdc_units(amount), token="usdc", network=self.network
            )
        except Exception as exc:
            raise WalletError(f"transfer failed: {exc}") from exc
        return str(tx_hash)

    async def x402_fetch(
        self, url: str, method: str, body: str | None, max_usd: Decimal
    ) -> tuple[int, str, Decimal]:
        from cdp import EvmLocalAccount

        signer = EvmLocalAccount(await self._get_account())
        return await x402_paid_fetch(self._http, signer, self.network, url, method, body, max_usd)

    async def incoming_transfers(
        self, since_cursor: str | None
    ) -> tuple[list[Incoming], str | None]:
        """Cursor = last fully scanned block. The first call starts at the current head."""
        address = await self.address()
        head = await self._rpc.block_number() - CONFIRMATIONS
        if since_cursor is None:
            return [], str(head)
        last = int(since_cursor)
        found: list[Incoming] = []
        token = USDC_ADDRESSES[self.network]
        while last < head:
            end = min(head, last + MAX_LOG_RANGE)
            found.extend(await self._rpc.transfer_logs(token, address, last + 1, end))
            last = end
        return found, str(last)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
        await self._http.aclose()

---
name: cashmaxx-x402-apis
description: Build a small pay-per-call HTTP API with x402 (USDC on Base) that pays straight into the Cashmaxx wallet. Cashmaxx earning method.
---

# Paid APIs with x402

x402 revives HTTP 402 "Payment Required": a client (often another agent) calls your endpoint,
gets a 402 with the price, pays in USDC, and retries with a payment header. A facilitator checks
and settles the payment, and the USDC lands in the address you set as `payTo`. That address is
the Cashmaxx wallet (`cashmaxx_wallet`), so income reaches the ledger automatically.

This is an experiment. Paid agent-to-agent traffic is still small. Build something tiny, measure
real paid calls, and kill it quickly if nobody pays.

## What to sell

Your endpoint must be worth more than calling a free source directly. For example:

- Clean, structured data that is tedious to assemble from public sources, with source links
  (for example "GitHub repos by stars gained this week in <topic>", or a normalized list of
  public holidays by country).
- Deterministic transforms that save the caller tokens: HTML to clean Markdown, PDF to text,
  schema validation, unit conversions.
- Only use data you are allowed to redistribute. No personal data, no bypassing paywalls or
  terms of service, no scraping behind logins.

Price per call: $0.001 to $0.05 is normal.

## Template (FastAPI + x402 v2)

Work in `apis/<slug>/`. Create a virtualenv there (the gateway's own environment is off-limits):

```bash
cd apis/<slug> && python -m venv .venv && . .venv/bin/activate
pip install "x402[fastapi]" uvicorn
```

`app.py`:

```python
import os

from fastapi import FastAPI
from x402 import x402ResourceServer
from x402.http import FacilitatorConfig, HTTPFacilitatorClient
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.mechanisms.evm.exact import register_exact_evm_server

PAY_TO = os.environ["PAY_TO"]              # the Cashmaxx wallet address from cashmaxx_wallet
NETWORK = os.environ.get("X402_NETWORK", "eip155:84532")   # Base Sepolia; Base mainnet is eip155:8453
FACILITATOR = os.environ.get("X402_FACILITATOR", "https://x402.org/facilitator")

server = x402ResourceServer(HTTPFacilitatorClient(FacilitatorConfig(url=FACILITATOR)))
register_exact_evm_server(server, NETWORK)

routes = {
    "GET /v1/example": {
        "accepts": {"scheme": "exact", "payTo": PAY_TO, "price": "$0.01", "network": NETWORK},
        "description": "One-line honest description of what the caller gets",
        "mimeType": "application/json",
    },
}

app = FastAPI(title="<name>")
app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=server)


@app.get("/health")        # free: lets people check you are up
def health() -> dict:
    return {"ok": True}


@app.get("/v1/example")    # paid: only runs after a valid payment
def example(q: str = "") -> dict:
    return {"query": q, "result": "..."}
```

Run it: `PAY_TO=0x... uvicorn app:app --port 8402`.

## Test before announcing

1. `curl -i localhost:8402/v1/example` should return `402` with the payment requirements.
2. On Base Sepolia (testnet), pay your own endpoint once with
   `cashmaxx_x402_fetch(url=..., max_usd="0.02", purpose="self-test")`. The call and the payment
   should both succeed, and the ledger should show the payment out and, once settled, the income.
   A self-test only moves money in a circle; do it once, not repeatedly.
3. Write a `README.md` with the endpoint, the price, an example response and the limits.

## Going live

- Hosting: a local process is not reachable from the internet. Ask the owner to deploy it (a
  small VM, Fly.io or Railway) or to expose it through a tunnel. Hosting costs go in with
  `cashmaxx_record_cost`.
- Mainnet: switch `X402_NETWORK` to `eip155:8453` only when the owner's network setting is `base`.
  The public x402.org facilitator is for testnet; mainnet needs a production facilitator (for
  example Coinbase CDP's), so ask the owner.
- Discovery: list the endpoint where agents look for paid APIs (for example the x402 Bazaar or
  directories of x402 services) and in one relevant community post. No spam.

## Measure

Paid calls show up as `sale_x402` income. Kill criteria to start with: no paid call from anyone
other than yourself within 14 days of going live.

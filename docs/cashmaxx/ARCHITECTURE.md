# Cashmaxx architecture

Cashmaxx is an agent that tries to earn more than it costs to run, holding a guarded USDC wallet on Base.
It is built on a fork of [HKUDS/nanobot](https://github.com/HKUDS/nanobot) (MIT).

## Ground rules

1. **Stay merge-friendly with upstream.** All Cashmaxx code lives in the top-level `cashmaxx/` package,
   `webui/src/cashmaxx/`, and `docs/cashmaxx/`. Upstream `nanobot/` files change only at the few
   hook points listed in "Upstream touch points". Keep every one of those edits small and marked with
   `# cashmaxx:` comments.
2. **The agent never holds the money keys.** CDP credentials, the Stripe key, the guard Telegram bot token
   and the owner PIN hash live only in the guard's config. The agent process has one *agent token*, and
   that token can read state and *request* spending. It can never approve, change policy or unfreeze.
3. **Approvals come only from the owner**: through the guard's own Telegram bot (a separate bot token
   from the agent's chat bot, with the owner's chat id checked), or through the WebUI with the owner PIN.
   The agent's WebUI process only proxies those calls. It never stores the PIN or a session secret the
   agent could reuse.
4. **The owner chooses the rules.** Budget, thresholds, safety rules, earning methods and the compute
   payment mode are chosen during onboarding and can be changed in settings. Nothing about the user's
   choices is hard-coded. What *is* hard-coded is the protection layer: the key separation, the approval
   channel, the ledger, and the fact that the agent cannot edit the guard, its config or its rules.
5. **Testnet first.** The default network is `base-sepolia`. `base` (mainnet) must be selected explicitly.
6. **Fail closed.** If the guard is unreachable, the wallet tools return errors, and the agent treats that
   as "no spending".

## Processes

```
┌──────────────── agent process (nanobot gateway) ───────────────┐      ┌──────── guard process ───────────────┐
│ AgentLoop + tools                                               │ HTTP │ aiohttp server on 127.0.0.1:18799    │
│  cashmaxx.plugin.tools  ──── agent token ──────────────────────────▶│  policy engine                        │
│  /pause /resume /cashmaxx commands                              │      │  approvals queue (sqlite)             │
│  WebUI (React) → /api/cashmaxx/* proxy ─── owner PIN passthrough ─▶│  ledger (sqlite)                      │
└─────────────────────────────────────────────────────────────────┘      │  wallet backend: cdp | fake           │
                                                                           │  stripe client (restricted key)       │
 Owner's phone ◀──── guard Telegram bot (own token) ───────────────────────│  watchers: chain in, stripe, openrouter│
                                                                           │  public P&L page (opt-in)             │
                                                                           └───────────────────────────────────────┘
```

In production they run as separate OS users or containers so the agent's `exec` tool cannot read
`~/.cashmaxx/guard.json`. For local development both run as the same user, and `restrictToWorkspace` plus the
exec sandbox are the only barrier. The onboarding prints a warning about this.

## Files on disk

| Path | Owner | Mode | Contents |
|---|---|---|---|
| `~/.cashmaxx/guard.json` | guard | 0600 | `GuardConfig`: secrets and the user's settings |
| `~/.cashmaxx/guard.sqlite3` | guard | 0600 | ledger, approvals, payments, events, key-value state |
| `~/.nanobot/config.json` | agent | 0600 | nanobot config. Holds `cashmaxx.guardUrl` and `cashmaxx.agentToken` only |
| `<workspace>/HEARTBEAT.md`, `SOUL.md`, `skills/cashmaxx-*` | agent | | mission, loop and earning skills |

The config dir can be overridden with the `CASHMAXX_HOME` env var (tests use a tmp dir).

## `cashmaxx/` package layout

```
cashmaxx/
  __init__.py
  config.py            GuardConfig + CashmaxxSettings (pydantic), load/save (atomic, 0600), paths
  money.py             Decimal helpers; USDC has 6 decimals; all amounts are Decimal USD strings on the wire
  guard/
    __init__.py
    app.py             aiohttp app factory `create_app(config, *, wallet=None, clock=None)` + `run()`
    auth.py            agent token check, owner PIN verification (scrypt hash), owner session tokens
    policy.py          pure functions: evaluate(request, settings, state) -> Decision
    store.py           sqlite schema + typed accessors (ledger, approvals, payments, events, kv)
    ledger.py          P&L math over the store: totals per window (7/30/all), by category
    wallet/
      __init__.py      WalletBackend Protocol
      fake.py          in-memory backend for tests + `--network fake`
      cdp.py           Coinbase CDP server wallet (cdp-sdk), USDC transfers + x402 signing
    stripe_client.py   restricted-key calls: products, prices, payment links, list checkout sessions
    compute.py         compute-payment modes (virtual | reimburse | owner_topup | x402_gateway)
    watchers.py        periodic tasks: incoming USDC, Stripe revenue, OpenRouter spend, loss stop
    telegram_bot.py    guard's own bot: approval cards with buttons, /status /freeze /unfreeze /pnl
    public.py          read-only public P&L HTML + JSON (only when settings.public_pnl is true)
    client.py          GuardClient: async httpx client used by agent tools and the WebUI proxy
  plugin/
    __init__.py
    tools.py           nanobot Tool subclasses (entry points in the `nanobot.tools` group)
    commands.py        /cashmaxx, /pause, /resume command handlers for nanobot's CommandRouter
    webui_routes.py    /api/cashmaxx/* proxy routes for the nanobot WebUI server
    workspace.py       install mission templates + skills into the workspace (idempotent)
  cli.py               `cashmaxx` Typer app: onboard, guard, status, freeze, unfreeze, settings
  onboarding.py        questionary wizard for Cashmaxx-specific steps (called by `cashmaxx onboard`)
  templates/           HEARTBEAT.md, SOUL.md addition, RULES.md (rendered from the settings)
  skills/              cashmaxx-core, digital-products, x402-apis, bounties, agent-marketplaces (SKILL.md)
tests/cashmaxx/        pytest (asyncio_mode=auto), mirrors the package
```

## Settings (`CashmaxxSettings`, user-chosen)

```python
class CashmaxxSettings(BaseModel):   # camelCase aliases on disk, like nanobot's schema
    network: Literal["base-sepolia", "base", "fake"] = "base-sepolia"
    budget_usd: Decimal                    # onboarding presets 10 / 50 / 100 / custom
    per_tx_approval_usd: Decimal           # payments above this need owner approval
    daily_cap_usd: Decimal                 # hard cap on outgoing spend per UTC day (auto + approved)
    new_recipient_needs_approval: bool = True
    max_payments_per_hour: int = 20        # stops a runaway loop
    allowlist: list[str] = []              # addresses / hosts that skip the new-recipient approval
    earning_methods: set[Literal["digital_products", "x402_apis", "bounties", "agent_marketplaces"]]
    rules: set[Literal[
        "no_trading",            # guard refuses swaps/contract calls; prompt forbids speculation
        "no_spam",               # outbound post/DM rate limits + no impersonation
        "bounty_review",         # bounty/marketplace submissions need owner approval
        "loss_stop",             # auto-freeze when 7-day net < -loss_stop_usd
    ]]
    loss_stop_usd: Decimal = Decimal("10")
    compute_payment_mode: Literal["virtual", "reimburse", "owner_topup", "x402_gateway"] = "virtual"
    owner_wallet: str | None = None        # required for reimburse
    reimburse_interval_hours: int = 24
    public_pnl: bool = False
    frozen: bool = False                   # kill switch state (only the owner can clear it)
```

`GuardConfig` = `settings` + secrets: `agent_token_hash`, `owner_pin_hash`, `cdp: {api_key_id, api_key_secret,
wallet_secret, account_name}`, `stripe_restricted_key`, `openrouter_api_key` (read-only use: spend polling),
`telegram: {bot_token, owner_chat_id}`, `host`, `port`, `public_base_url`.

### Compute payment modes

Both the OpenRouter account and the wallet belong to the owner. The agent only controls the wallet. The mode
decides how compute spend reaches the agent's budget:

| Mode | What happens |
|---|---|
| `virtual` (default) | OpenRouter spend is polled and recorded as a `compute` cost in the ledger. It lowers the agent's *available budget* (budget − spent − compute). No money moves. |
| `reimburse` | As `virtual`, and every `reimburse_interval_hours` the guard sends the unreimbursed compute spend in USDC to `owner_wallet`. This goes through the normal policy (caps, freeze), but the owner wallet counts as allowlisted. |
| `owner_topup` | When OpenRouter credit falls below a threshold, the guard's Telegram bot asks the owner to top up, with the amount and a checkout link. The spend is recorded as a cost. |
| `x402_gateway` | Inference is bought per request from an x402 gateway using the wallet. Each call is a normal policy-checked payment. Optional and experimental. |

## Policy engine (`policy.evaluate`)

Input: `SpendRequest{kind: "transfer"|"x402", to, amount_usd, purpose, category, idempotency_key}`, the
settings, and the state (spent today, payments in the last hour, known recipients, available budget, frozen).
It is checked in this order and the first match wins:

1. `frozen` → `deny("frozen")`
2. `amount_usd <= 0` or bad address → `deny("invalid")`
3. `kind` not in the supported set (only USDC transfer and x402) → `deny("unsupported")`. Swaps and
   arbitrary contract calls are never supported; the `no_trading` rule is also enforced in prompts.
4. `amount > available_budget` → `deny("over_budget")`
5. `spent_today + amount > daily_cap` → `deny("daily_cap")`
6. payments in the last hour ≥ `max_payments_per_hour` → `deny("rate_limited")`
7. `amount > per_tx_approval_usd` → `needs_approval("over_threshold")`
8. `new_recipient_needs_approval` and `to` is not in known ∪ allowlist → `needs_approval("new_recipient")`
9. otherwise → `allow`

When a pending approval is approved, **steps 1–6 are re-checked at execution time.**
Idempotency: the same `idempotency_key` returns the original result and never pays twice.

## Guard HTTP API (JSON, `127.0.0.1:18799`)

Auth: `Authorization: Bearer <agent token>` (scope **agent**), or `X-Cashmaxx-Owner: <owner session>`
(scope **owner**, obtained from `POST /owner/session` with the PIN, 12h TTL, kept in memory only).
Amounts are decimal strings (`"1.50"`), times are ISO-8601 UTC, and every error is `{"error": code, "message"}`.

| Method + path | Scope | Purpose |
|---|---|---|
| `GET /health` | none | `{ok, network, frozen, version}` |
| `GET /wallet` | agent, owner | `{address, network, balance_usdc, available_budget_usd}` |
| `POST /spend` | agent | body `SpendRequest` → `{status: "paid"|"pending"|"denied", payment_id, approval_id?, tx_hash?, reason?}` |
| `GET /spend/{payment_id}` | agent, owner | payment status (the agent polls this after `pending`) |
| `POST /x402/fetch` | agent | `{url, method, body?, max_usd, purpose}` → guard makes the request, pays up to `max_usd` via policy, and returns `{status, http_status, body_text, paid_usd, payment_id}` |
| `GET /ledger?window=7d|30d|all` | agent, owner | `{income, costs, net, by_category, entries[]}` |
| `POST /ledger/cost` | agent | agent-reported costs only (e.g. domain bought by the owner). Marked `source=agent_reported` |
| `GET /approvals?status=pending` | agent (read-only), owner | list |
| `POST /approvals/{id}/approve` | **owner** | executes the payment after re-checking policy |
| `POST /approvals/{id}/deny` | **owner** | |
| `POST /stripe/product` | agent | `{name, description, price_usd}` → `{product_id, price_id, payment_link_url}` (requires Stripe) |
| `GET /settings` | agent (redacted), owner | settings with no secrets |
| `PATCH /settings` | **owner** | partial update, validated |
| `POST /freeze` | agent, owner | anyone can freeze (the kill switch is always safe to pull) |
| `POST /unfreeze` | **owner** | |
| `POST /owner/session` | none | `{pin}` → `{session, expires_at}`. Rate-limited to 5/min, constant-time compare |
| `GET /events?since=` | agent, owner | audit events (spend requests, decisions, approvals, freezes, watcher runs) |
| `GET /public/pnl`, `GET /public/pnl.json` | none | only when `public_pnl` is true, otherwise 404 |

### sqlite schema (`store.py`)

- `ledger(id, ts, direction: income|cost, category, amount_usd TEXT, source, ref, note, verified INT)`.
  Categories: `sale_stripe`, `sale_x402`, `transfer_in`, `bounty`, `marketplace`, `payment_out`,
  `compute`, `reimbursement`, `fees`, `other`. `verified=1` when the value comes from the chain or Stripe.
- `payments(id, idempotency_key UNIQUE, ts, kind, to_addr, amount_usd, purpose, category, status: pending|paid|denied|failed, reason, approval_id, tx_hash)`
- `approvals(id, ts, payment_id, reason, status: pending|approved|denied|expired, decided_ts, decided_via: telegram|webui)`.
  Expires after 24h by default.
- `events(id, ts, actor: agent|owner|guard, type, data_json)`
- `kv(key PRIMARY KEY, value)` for watcher cursors (last block, last Stripe session, last OpenRouter usage)

## Agent tools (`cashmaxx/plugin/tools.py`, registered via entry points)

All of them fail closed with a clear error if `cashmaxx.guardUrl` is not configured or the guard is down.

| Tool name | Params | Calls |
|---|---|---|
| `cashmaxx_wallet` | — | `GET /wallet` |
| `cashmaxx_pay` | `to, amount_usd, purpose, category` | `POST /spend` (idempotency key derived from session + args) |
| `cashmaxx_payment_status` | `payment_id` | `GET /spend/{id}` |
| `cashmaxx_x402_fetch` | `url, method, body, max_usd, purpose` | `POST /x402/fetch` |
| `cashmaxx_ledger` | `window` | `GET /ledger` |
| `cashmaxx_record_cost` | `amount_usd, category, note` | `POST /ledger/cost` |
| `cashmaxx_sell_product` | `name, description, price_usd` | `POST /stripe/product` |
| `cashmaxx_settings` | — | `GET /settings` (redacted) |
| `cashmaxx_freeze` | `reason` | `POST /freeze` |

A `pending` result tells the model: "Waiting for owner approval (id …). Do other work and check later with
`cashmaxx_payment_status`." It never tells the model to retry the payment.

## Upstream touch points (keep minimal)

1. `pyproject.toml`: the `cashmaxx` optional extra (`cdp-sdk`, `x402`, `stripe`, `aiohttp`), the `cashmaxx`
   script, `nanobot.tools` entry points, and `cashmaxx/**` in the hatch includes.
2. `nanobot/config/schema.py`: optional `cashmaxx: CashmaxxAgentConfig | None` on the root `Config`
   (`guard_url`, `agent_token`). Just the field. The model lives in `cashmaxx/`.
3. `nanobot/cli/gateway_runtime.py`: one call `cashmaxx.plugin.install(agent, cron, config)` when
   `config.cashmaxx` is set. It registers the commands and the WebUI proxy routes.
4. `nanobot/webui/settings_routes.py` (or the gateway HTTP dispatch): mount `/api/cashmaxx/*` from
   `cashmaxx.plugin.webui_routes`.
5. `webui/src`: a sidebar entry and route to `webui/src/cashmaxx/` pages (Wallet & P&L, Approvals,
   Cashmaxx settings).

## WebUI (`webui/src/cashmaxx/`)

- **Overview:** wallet address (copy button, and a QR to fund it), balance, available budget, the 7/30/all P&L
  (income, costs, net, by category), frozen badge, and a Freeze button.
- **Approvals:** pending cards (amount, recipient, purpose, reason) with Approve/Deny. The first action asks
  for the owner PIN, which gets a session kept in memory in the tab (never in localStorage). It stays in sync
  with Telegram because both read the guard's queue.
- **Settings:** every `CashmaxxSettings` field, grouped as Budget, Approvals, Safety rules, Earning methods,
  Compute payments, and Public P&L. Saving needs the owner PIN.
- The UI follows the existing settings components, i18n pattern and Tailwind tokens.

## Testing

- `pytest tests/cashmaxx` uses the fake wallet backend, a tmp `CASHMAXX_HOME`, an aiohttp test client,
  and a stubbed Telegram, Stripe and OpenRouter. No network.
- Required coverage: every policy branch, idempotency, re-check on approve, the owner-only endpoints
  rejecting the agent token, PIN rate limit, the freeze/unfreeze permissions, and the ledger math, including
  compute modes and the reimbursement cycle.
- `cd webui && bun run test` for the new pages.

"""The Cashmaxx integrations the owner can connect, shared by the guard, the WebUI proxy and the CLI.

Two kinds:

- ``guard``: the secrets live only in ``guard.json`` and the guard does the work (payments,
  email, social posts, public hosting). The agent never sees these credentials; it asks the guard,
  which enforces the caps.
- ``agent``: tools the agent runs itself through nanobot MCP servers (browser, GitHub, search).
  Their settings go into nanobot's config (``tools.mcpServers``), reusing nanobot's MCP presets.

Secret field values are never returned by any API: listings say only whether they are set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

IntegrationKind = Literal["guard", "agent"]
Category = Literal["money", "compute", "approvals", "email", "social", "hosting", "browser",
                   "code", "search"]


@dataclass(frozen=True)
class FieldSpec:
    name: str  # camelCase, the key in IntegrationConfig.fields and the API
    label: str
    secret: bool = False
    required: bool = True
    placeholder: str = ""
    choices: tuple[str, ...] = ()  # non-empty: a select


@dataclass(frozen=True)
class IntegrationSpec:
    id: str
    label: str
    kind: IntegrationKind
    category: Category
    summary: str  # one line: what the agent can do with it
    fields: tuple[FieldSpec, ...] = ()
    docs_url: str = ""
    mcp_preset: str | None = None  # agent kind: the nanobot MCP preset it enables
    # Guard settings that switch the capability on, shown next to the integration.
    related_settings: tuple[str, ...] = field(default_factory=tuple)


INTEGRATIONS: tuple[IntegrationSpec, ...] = (
    # --- guard: money and control (fields map onto the existing GuardConfig attributes) --------
    IntegrationSpec(
        "openrouter", "OpenRouter", "guard", "compute",
        "Runs the agent's model; the guard counts its spend as compute cost. Use a key only "
        "Cashmaxx uses.",
        (FieldSpec("apiKey", "API key", secret=True, placeholder="sk-or-v1-..."),),
        "https://openrouter.ai/settings/keys",
    ),
    IntegrationSpec(
        "cdp", "Coinbase CDP wallet", "guard", "money",
        "The USDC wallet on Base. Only the guard can move money.",
        (FieldSpec("apiKeyId", "API key id"),
         FieldSpec("apiKeySecret", "API key secret", secret=True),
         FieldSpec("walletSecret", "Wallet secret", secret=True)),
        "https://portal.cdp.coinbase.com",
    ),
    IntegrationSpec(
        "stripe", "Stripe", "guard", "money",
        "Products and payment links for digital products. Restricted key: no payouts.",
        (FieldSpec("restrictedKey", "Restricted key", secret=True, placeholder="rk_live_..."),),
        "https://dashboard.stripe.com/apikeys",
    ),
    IntegrationSpec(
        "telegram", "Guard Telegram bot", "guard", "approvals",
        "Approvals and alerts on your phone. Must be a different bot from the agent's chat bot.",
        (FieldSpec("botToken", "Bot token", secret=True, placeholder="123456:ABC..."),
         FieldSpec("ownerChatId", "Your chat id")),
        "https://t.me/BotFather",
    ),
    # --- guard: email --------------------------------------------------------------------------
    IntegrationSpec(
        "gmail", "Gmail", "guard", "email",
        "The agent's own Gmail address: read the inbox and send within the daily cap. New "
        "accounts warm up over 4 weeks.",
        (FieldSpec("address", "Gmail address", placeholder="agent@example.com"),
         FieldSpec("appPassword", "App password", secret=True,
                   placeholder="16 characters, needs 2-Step Verification")),
        "https://myaccount.google.com/apppasswords",
        related_settings=("emailProvider", "emailDailyCap", "emailWarmup"),
    ),
    IntegrationSpec(
        "agentmail", "AgentMail", "guard", "email",
        "An inbox made for agents (agentmail.to). Read and send within the daily cap.",
        (FieldSpec("apiKey", "API key", secret=True),
         FieldSpec("inboxId", "Inbox address", required=False,
                   placeholder="leave empty to create cashmaxx@agentmail.to")),
        "https://console.agentmail.to",
        related_settings=("emailProvider", "emailDailyCap"),
    ),
    # --- guard: social -------------------------------------------------------------------------
    IntegrationSpec(
        "bluesky", "Bluesky", "guard", "social",
        "Post to Bluesky within the daily social cap.",
        (FieldSpec("handle", "Handle", placeholder="name.bsky.social"),
         FieldSpec("appPassword", "App password", secret=True)),
        "https://bsky.app/settings/app-passwords",
        related_settings=("socialDailyCap",),
    ),
    IntegrationSpec(
        "x", "X (Twitter)", "guard", "social",
        "Post to X through the API within the daily social cap.",
        (FieldSpec("apiKey", "API key", secret=True),
         FieldSpec("apiSecret", "API secret", secret=True),
         FieldSpec("accessToken", "Access token", secret=True),
         FieldSpec("accessSecret", "Access token secret", secret=True)),
        "https://developer.x.com/en/portal/dashboard",
        related_settings=("socialDailyCap",),
    ),
    IntegrationSpec(
        "reddit", "Reddit", "guard", "social",
        "Post to subreddits that allow it, through a Reddit script app, within the daily cap.",
        (FieldSpec("clientId", "Client id"),
         FieldSpec("clientSecret", "Client secret", secret=True),
         FieldSpec("username", "Username"),
         FieldSpec("password", "Password", secret=True)),
        "https://www.reddit.com/prefs/apps",
        related_settings=("socialDailyCap",),
    ),
    # --- guard: hosting ------------------------------------------------------------------------
    IntegrationSpec(
        "hosting", "Public hosting", "guard", "hosting",
        "Expose a service the agent runs (for example a paid x402 API) on a public URL through "
        "a Cloudflare tunnel. The guard and gateway ports can never be exposed.",
        (FieldSpec("provider", "Provider", choices=("cloudflare_quick", "cloudflare_token")),
         FieldSpec("tunnelToken", "Tunnel token", secret=True, required=False,
                   placeholder="only for cloudflare_token")),
        "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/",
        related_settings=("hostingEnabled",),
    ),
    # --- agent: MCP tools ----------------------------------------------------------------------
    IntegrationSpec(
        "browser", "Browser", "agent", "browser",
        "A real browser for research and sign-ups: local Playwright (free, own profile, never "
        "your Chrome) or Browserbase (cloud, for sites that block bots).",
        (FieldSpec("provider", "Provider", choices=("playwright", "browserbase")),
         FieldSpec("browserbaseApiKey", "Browserbase API key", secret=True, required=False),
         FieldSpec("browserbaseProjectId", "Browserbase project id", required=False)),
        "https://playwright.dev/docs/getting-started-mcp",
        mcp_preset="playwright",
    ),
    IntegrationSpec(
        "github", "GitHub", "agent", "code",
        "The agent's GitHub account for bounties: issues, forks and pull requests. Bounty "
        "review still applies.",
        (FieldSpec("token", "Fine-grained token", secret=True, placeholder="github_pat_..."),),
        "https://github.com/settings/personal-access-tokens",
        mcp_preset="github",
    ),
    IntegrationSpec(
        "search", "Web search", "agent", "search",
        "Better search and scraping than the free default: Brave, Exa or Firecrawl.",
        (FieldSpec("provider", "Provider", choices=("brave-search", "exa", "firecrawl")),
         FieldSpec("apiKey", "API key", secret=True, required=False,
                   placeholder="Brave needs one; Exa and Firecrawl work without")),
        "https://brave.com/search/api/",
    ),
)

BY_ID: dict[str, IntegrationSpec] = {spec.id: spec for spec in INTEGRATIONS}

# The four integrations whose values live in the original GuardConfig attributes, not in
# ``GuardConfig.integrations``: (integration id, field name) -> dotted GuardConfig attribute.
LEGACY_FIELDS: dict[tuple[str, str], str] = {
    ("openrouter", "apiKey"): "openrouter_api_key",
    ("cdp", "apiKeyId"): "cdp.api_key_id",
    ("cdp", "apiKeySecret"): "cdp.api_key_secret",
    ("cdp", "walletSecret"): "cdp.wallet_secret",
    ("stripe", "restrictedKey"): "stripe_restricted_key",
    ("telegram", "botToken"): "telegram.bot_token",
    ("telegram", "ownerChatId"): "telegram.owner_chat_id",
}


def get_spec(integration_id: str) -> IntegrationSpec:
    try:
        return BY_ID[integration_id]
    except KeyError:
        raise KeyError(f"unknown integration {integration_id!r}") from None


def gmail_warmup_cap(days_since_connected: int) -> int:
    """Gmail warm-up ramp: genuine low volume first, roughly doubling weekly for four weeks.

    The effective cap is ``min(settings.email_daily_cap, gmail_warmup_cap(days))`` while
    ``email_warmup`` is on. Day 0 is the day the account was connected.
    """
    if days_since_connected < 7:
        return 10
    if days_since_connected < 14:
        return 20
    if days_since_connected < 21:
        return 40
    if days_since_connected < 28:
        return 80
    return 400  # under Gmail's 500/day hard limit for free accounts

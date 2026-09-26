"""``cashmaxx onboard``: the Cashmaxx-specific setup steps.

The file is split in two:

- **Pure functions** (``budget_for``, ``build_settings``, ``build_guard_config``,
  ``apply_nanobot_config``, ``check_guard_bot_token``, ``hash_pin`` and friends) turn an
  :class:`OnboardAnswers` into the guard config and the nanobot config. They do no I/O and are
  unit-tested.
- **Prompts** (``collect_answers``) ask the questions through a small :class:`Prompter` protocol
  backed by questionary. Tests drive it with a scripted prompter.

``run_onboarding`` wires them together and writes the files.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

import httpx

from cashmaxx.config import (
    ALL_EARNING_METHODS,
    ALL_RULES,
    BUDGET_PRESETS,
    DEFAULT_GUARD_PORT,
    CashmaxxAgentConfig,
    CashmaxxSettings,
    CdpConfig,
    ComputePaymentMode,
    EarningMethod,
    GuardConfig,
    GuardTelegramConfig,
    Network,
    Rule,
)

if TYPE_CHECKING:
    from nanobot.config.schema import Config

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
# First match wins. Cheap, capable, tool-calling models; the owner can pick anything.
PREFERRED_MODELS: tuple[str, ...] = (
    "google/gemini-3.8-flash",
    "google/gemini-3.7-flash",
    "google/gemini-3-flash-preview",
    "google/gemini-2.5-flash",
    "openai/gpt-5-mini",
    "anthropic/claude-haiku-4.5",
)
FALLBACK_MODEL = "google/gemini-2.5-flash"
MIN_PIN_LENGTH = 6
_CENT = Decimal("0.01")

EARNING_METHOD_HELP: dict[str, str] = {
    "digital_products": "Digital products: make templates/guides/datasets and sell them via Stripe links",
    "x402_apis": "Paid APIs: build small pay-per-call HTTP APIs paid in USDC (x402)",
    "bounties": "Bounties: complete publicly offered paid tasks (open-source issues etc.)",
    "agent_marketplaces": "Agent marketplaces: take paid tasks on platforms that hire AI agents",
}
RULE_HELP: dict[str, str] = {
    "no_trading": "No trading: no speculation, swaps, DeFi or bets (the guard also refuses swaps)",
    "no_spam": "No spam: strict limits on outbound posts/DMs, always honest about being an AI",
    "bounty_review": "Bounty review: you approve every bounty/marketplace submission first",
    "loss_stop": "Loss stop: auto-freeze spending when the 7-day net loss passes a threshold",
}
COMPUTE_MODE_HELP: dict[str, str] = {
    "virtual": "virtual: OpenRouter spend is counted as a cost; no money moves (recommended)",
    "reimburse": "reimburse: as virtual, and the guard periodically pays it back to your wallet",
    "owner_topup": "owner_topup: you top up OpenRouter when it runs low; counted as a cost",
    "x402_gateway": "x402_gateway: buy inference per request from an x402 gateway (experimental)",
}
STRIPE_PERMISSIONS = (
    "Products: Write",
    "Prices: Write",
    "Payment Links: Write",
    "Checkout Sessions: Read",
)


# --- answers -----------------------------------------------------------------------------------


@dataclass
class OnboardAnswers:
    budget_usd: Decimal
    per_tx_approval_usd: Decimal
    daily_cap_usd: Decimal
    network: Network = "base-sepolia"
    openrouter_api_key: str = ""
    model: str = FALLBACK_MODEL
    earning_methods: set[EarningMethod] = field(default_factory=lambda: set(ALL_EARNING_METHODS))
    rules: set[Rule] = field(default_factory=lambda: set(ALL_RULES))
    loss_stop_usd: Decimal = Decimal("10")
    compute_payment_mode: ComputePaymentMode = "virtual"
    owner_wallet: str | None = None
    x402_gateway_url: str | None = None
    cdp: CdpConfig = field(default_factory=CdpConfig)
    stripe_restricted_key: str = ""
    guard_bot_token: str = ""
    owner_chat_id: str = ""
    owner_pin: str = ""
    agent_token: str = ""
    guard_host: str = "127.0.0.1"
    guard_port: int = DEFAULT_GUARD_PORT

    @property
    def guard_url(self) -> str:
        return f"http://{self.guard_host}:{self.guard_port}"


# --- pure functions ----------------------------------------------------------------------------


def parse_decimal(value: str) -> Decimal:
    try:
        amount = Decimal(value.strip().lstrip("$"))
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"not a number: {value!r}") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("must be a positive number")
    return amount.quantize(_CENT, rounding=ROUND_DOWN)


def budget_for(preset: str, custom_budget: Decimal | None = None) -> tuple[Decimal, Decimal, Decimal]:
    """(budget, per-tx approval threshold, daily cap) for a preset, or suggested for custom.

    Custom budgets scale like the presets: approval above 10% of the budget, daily cap 25%.
    """
    if preset in BUDGET_PRESETS:
        return BUDGET_PRESETS[preset]
    if custom_budget is None or custom_budget <= 0:
        raise ValueError("a custom budget needs a positive amount")
    per_tx = max((custom_budget / 10).quantize(_CENT, rounding=ROUND_DOWN), _CENT)
    daily = max((custom_budget / 4).quantize(_CENT, rounding=ROUND_DOWN), per_tx)
    return custom_budget, per_tx, daily


def build_settings(answers: OnboardAnswers) -> CashmaxxSettings:
    return CashmaxxSettings(
        network=answers.network,
        budget_usd=answers.budget_usd,
        per_tx_approval_usd=answers.per_tx_approval_usd,
        daily_cap_usd=answers.daily_cap_usd,
        earning_methods=set(answers.earning_methods),
        rules=set(answers.rules),
        loss_stop_usd=answers.loss_stop_usd,
        compute_payment_mode=answers.compute_payment_mode,
        owner_wallet=answers.owner_wallet or None,
        x402_gateway_url=answers.x402_gateway_url or None,
    )


def generate_agent_token() -> str:
    try:
        from cashmaxx.guard.auth import new_agent_token

        return new_agent_token()
    except ImportError:
        return secrets.token_urlsafe(32)


def _scrypt_pin(pin: str) -> str:
    n, r, p = 2**14, 8, 1
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(pin.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${digest.hex()}"


def hash_pin(pin: str) -> str:
    """``scrypt$n$r$p$salt_hex$hash_hex``: the guard's function when present, else the same format."""
    try:
        from cashmaxx.guard.auth import hash_pin as guard_hash_pin
    except ImportError:
        return _scrypt_pin(pin)
    return guard_hash_pin(pin)


def hash_agent_token(token: str) -> str:
    """Hash the agent token exactly as the guard checks it (sha256 hex)."""
    try:
        from cashmaxx.guard.auth import hash_agent_token as guard_hash_agent_token
    except ImportError:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()
    return guard_hash_agent_token(token)


def validate_pin(pin: str, again: str) -> str | None:
    if len(pin) < MIN_PIN_LENGTH:
        return f"The PIN must be at least {MIN_PIN_LENGTH} characters."
    if pin != again:
        return "The two PINs do not match."
    return None


def agent_telegram_token(config: Config) -> str:
    telegram = getattr(config.channels, "telegram", None)
    if isinstance(telegram, dict):
        return str(cast(dict[str, Any], telegram).get("token") or "").strip()
    return str(getattr(telegram, "token", "") or "").strip()


def check_guard_bot_token(guard_token: str, config: Config) -> str | None:
    """The guard's bot must not be the agent's chat bot, or a hijacked agent chat could approve."""
    token = guard_token.strip()
    if not token:
        return "The guard needs its own Telegram bot token."
    if ":" not in token:
        return "That does not look like a Telegram bot token (expected 123456:ABC...)."
    if token == agent_telegram_token(config):
        return (
            "This is the same token as the agent's Telegram channel. Create a second bot with "
            "@BotFather for the guard."
        )
    return None


def build_guard_config(answers: OnboardAnswers, existing: GuardConfig | None = None) -> GuardConfig:
    """New guard config. Keeps the host/port/public URL/CDP account name of an existing one."""
    base = existing or GuardConfig()
    cdp = answers.cdp.model_copy()
    if existing is not None and cdp.account_name == CdpConfig().account_name:
        cdp.account_name = existing.cdp.account_name
    return GuardConfig(
        settings=build_settings(answers),
        host=answers.guard_host or base.host,
        port=answers.guard_port or base.port,
        public_base_url=base.public_base_url,
        agent_token_hash=hash_agent_token(answers.agent_token),
        owner_pin_hash=hash_pin(answers.owner_pin),
        cdp=cdp,
        stripe_restricted_key=answers.stripe_restricted_key.strip(),
        openrouter_api_key=answers.openrouter_api_key.strip(),
        telegram=GuardTelegramConfig(
            bot_token=answers.guard_bot_token.strip(), owner_chat_id=answers.owner_chat_id.strip()
        ),
    )


def telegram_configured(config: Config) -> bool:
    telegram = getattr(config.channels, "telegram", None)
    if isinstance(telegram, dict):
        data = cast(dict[str, Any], telegram)
        return bool(data.get("enabled") and data.get("token"))
    return bool(getattr(telegram, "enabled", False) and getattr(telegram, "token", ""))


def apply_nanobot_config(config: Config, answers: OnboardAnswers) -> Config:
    """Write the agent-side settings into nanobot's config (in place; also returned)."""
    if answers.openrouter_api_key:
        config.providers.openrouter.api_key = answers.openrouter_api_key.strip()
    if answers.model:
        defaults = config.agents.defaults
        defaults.model = answers.model
        defaults.provider = "openrouter"
        # An active preset would shadow agents.defaults.model; fall back to the defaults.
        defaults.model_preset = None
    config.cashmaxx = CashmaxxAgentConfig(guard_url=answers.guard_url, agent_token=answers.agent_token)
    config.tools.restrict_to_workspace = True
    apply_exec_sandbox(config)
    if telegram_configured(config):
        telegram = getattr(config.channels, "telegram")
        if isinstance(telegram, dict):
            cast(dict[str, Any], telegram)["inlineKeyboards"] = True
        else:
            telegram.inline_keyboards = True
    return config


HOMEBREW_PREFIX = Path("/opt/homebrew")


def exec_sandbox_backend() -> str:
    """The OS sandbox for the agent's shell, or "" when this machine has none.

    restrictToWorkspace alone only pattern-matches commands (``cd .. && cat ...`` gets past
    it), so guard.json with the CDP secrets needs a real OS boundary.
    """
    if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").exists():
        return "seatbelt"
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return "bwrap"
    return ""


def apply_exec_sandbox(config: Config) -> None:
    """Turn on the exec sandbox unless the owner already picked one."""
    exec_cfg = config.tools.exec
    if exec_cfg.sandbox:
        return
    exec_cfg.sandbox = exec_sandbox_backend()
    # Seatbelt denies everything outside the workspace; keep Homebrew tools usable (read-only).
    if exec_cfg.sandbox == "seatbelt" and HOMEBREW_PREFIX.is_dir():
        if str(HOMEBREW_PREFIX) not in exec_cfg.sandbox_ro_binds:
            exec_cfg.sandbox_ro_binds.append(str(HOMEBREW_PREFIX))
        if not exec_cfg.path_append:
            exec_cfg.path_append = str(HOMEBREW_PREFIX / "bin")


def parse_model_ids(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return []
    data = cast(dict[str, Any], payload).get("data")
    if not isinstance(data, list):
        return []
    ids: list[str] = []
    for item in cast(list[object], data):
        if isinstance(item, dict):
            model_id: object = cast(dict[str, object], item).get("id")
            if model_id:
                ids.append(str(model_id))
    return sorted(set(ids))


def default_model(model_ids: Sequence[str]) -> str:
    available = set(model_ids)
    for candidate in PREFERRED_MODELS:
        if candidate in available:
            return candidate
    flash = sorted(m for m in model_ids if m.startswith("google/") and "flash" in m)
    if flash:
        return flash[-1]
    return model_ids[0] if model_ids else FALLBACK_MODEL


def owner_chat_id_from_updates(payload: object) -> str | None:
    """Latest private chat that messaged the bot (the owner after sending /start)."""
    if not isinstance(payload, dict):
        return None
    result = cast(dict[str, Any], payload).get("result")
    if not isinstance(result, list):
        return None
    for update in reversed(cast(list[object], result)):
        if not isinstance(update, dict):
            continue
        upd = cast(dict[str, object], update)
        message = upd.get("message") or upd.get("edited_message")
        if not isinstance(message, dict):
            continue
        chat: object = cast(dict[str, object], message).get("chat")
        if not isinstance(chat, dict):
            continue
        chat_data = cast(dict[str, object], chat)
        if chat_data.get("type") == "private" and chat_data.get("id") is not None:
            return str(chat_data["id"])
    return None


def stripe_key_problem(key: str) -> str | None:
    key = key.strip()
    if not key:
        return None
    if key.startswith(("sk_live_", "sk_test_")):
        return "That is a full secret key. Create a restricted key (rk_...) with only the listed permissions."
    if not key.startswith(("rk_live_", "rk_test_")):
        return "A Stripe restricted key starts with rk_live_ or rk_test_."
    return None


# --- network helpers (thin, injectable) --------------------------------------------------------


def fetch_openrouter_models(api_key: str, *, transport: httpx.BaseTransport | None = None) -> list[str]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    with httpx.Client(timeout=20.0, transport=transport) as client:
        resp = client.get(OPENROUTER_MODELS_URL, headers=headers)
        resp.raise_for_status()
        return parse_model_ids(resp.json())


def fetch_telegram_owner_chat_id(
    bot_token: str, *, transport: httpx.BaseTransport | None = None
) -> str | None:
    with httpx.Client(timeout=20.0, transport=transport) as client:
        resp = client.get(f"https://api.telegram.org/bot{bot_token}/getUpdates")
        resp.raise_for_status()
        return owner_chat_id_from_updates(resp.json())


async def create_or_load_cdp_account(cdp: CdpConfig, *, faucet_network: str | None = None) -> str:
    """Return the address of the CDP server account (created on first use)."""
    from cdp import CdpClient  # pyright: ignore[reportMissingTypeStubs]

    async with CdpClient(
        api_key_id=cdp.api_key_id, api_key_secret=cdp.api_key_secret, wallet_secret=cdp.wallet_secret
    ) as client:
        account = await client.evm.get_or_create_account(name=cdp.account_name)
        if faucet_network:
            await client.evm.request_faucet(address=account.address, network=faucet_network,
                                            token="usdc")
        return str(account.address)


# --- prompts -----------------------------------------------------------------------------------


class Prompter(Protocol):
    def say(self, text: str) -> None: ...
    def select(self, message: str, choices: list[tuple[str, str]], default: str | None = None) -> str: ...
    def checkbox(self, message: str, choices: list[tuple[str, str]], checked: set[str]) -> list[str]: ...
    def text(self, message: str, default: str = "", validate: Callable[[str], str | None] | None = None) -> str: ...
    def secret(self, message: str) -> str: ...
    def confirm(self, message: str, default: bool = True) -> bool: ...
    def autocomplete(self, message: str, choices: list[str], default: str) -> str: ...


class QuestionaryPrompter:
    """Prompter backed by questionary + rich (the same stack as ``nanobot onboard``)."""

    def __init__(self) -> None:
        import questionary
        from rich.console import Console

        self._q = questionary
        self._console = Console()

    def _ask(self, question: Any) -> Any:
        answer = question.ask()
        if answer is None:  # Ctrl-C
            raise KeyboardInterrupt
        return answer

    def say(self, text: str) -> None:
        self._console.print(text)

    def select(self, message: str, choices: list[tuple[str, str]], default: str | None = None) -> str:
        opts = [self._q.Choice(title=title, value=value) for value, title in choices]
        return str(self._ask(self._q.select(message, choices=opts, default=default)))

    def checkbox(self, message: str, choices: list[tuple[str, str]], checked: set[str]) -> list[str]:
        opts = [self._q.Choice(title=t, value=v, checked=v in checked) for v, t in choices]
        return list(self._ask(self._q.checkbox(message, choices=opts)))

    def text(self, message: str, default: str = "", validate: Callable[[str], str | None] | None = None) -> str:
        def _check(value: str) -> bool | str:
            if validate is None:
                return True
            problem = validate(value)
            return True if problem is None else problem

        return str(self._ask(self._q.text(message, default=default, validate=_check)))

    def secret(self, message: str) -> str:
        return str(self._ask(self._q.password(message)))

    def confirm(self, message: str, default: bool = True) -> bool:
        return bool(self._ask(self._q.confirm(message, default=default)))

    def autocomplete(self, message: str, choices: list[str], default: str) -> str:
        known = set(choices)

        def _check(value: str) -> bool | str:
            if not value.strip():
                return "Pick a model"
            if known and value.strip() not in known:
                return "Not a model id from the list (type to search, arrow keys to pick)"
            return True

        return str(self._ask(self._q.autocomplete(
            message, choices=choices, default=default, match_middle=True, validate=_check,
        )))


def _decimal_problem(value: str) -> str | None:
    try:
        parse_decimal(value)
    except ValueError as exc:
        return str(exc)
    return None


@dataclass
class Services:
    """Network side effects, swappable in tests."""

    list_models: Callable[[str], list[str]] = fetch_openrouter_models
    detect_chat_id: Callable[[str], str | None] = fetch_telegram_owner_chat_id
    cdp_address: Callable[[CdpConfig, str | None], str] = field(
        default=lambda cdp, faucet: asyncio.run(
            create_or_load_cdp_account(cdp, faucet_network=faucet)
        )
    )


@dataclass
class CollectResult:
    answers: OnboardAnswers
    wallet_address: str | None = None
    warnings: list[str] = field(default_factory=list)


def collect_answers(p: Prompter, config: Config, services: Services | None = None) -> CollectResult:
    """Ask every Cashmaxx question (steps 2-12). Returns answers; writes nothing."""
    services = services or Services()
    warnings: list[str] = []

    # 2. Budget
    p.say("\n[bold]Budget[/bold]: the most the agent may spend in total, compute included.")
    preset_choices = [
        (k, f"${k}: approval above ${BUDGET_PRESETS[k][1]}, daily cap ${BUDGET_PRESETS[k][2]}")
        for k in BUDGET_PRESETS
    ] + [("custom", "Custom amount")]
    preset = p.select("Budget", preset_choices, default="50")
    custom = None
    if preset == "custom":
        custom = parse_decimal(p.text("Budget in USD", "25", validate=_decimal_problem))
    budget, per_tx, daily = budget_for(preset, custom)
    if preset == "custom" or p.confirm(
        f"Adjust the limits (approval above ${per_tx}, daily cap ${daily})?", default=False
    ):
        per_tx = parse_decimal(p.text("Payments above this need your approval (USD)", str(per_tx),
                                      validate=_decimal_problem))
        daily = parse_decimal(p.text("Daily spending cap (USD)", str(daily),
                                     validate=_decimal_problem))

    # 3. Network
    network = cast(Network, p.select("Network", [
        ("base-sepolia", "base-sepolia: testnet, free test USDC (recommended to start)"),
        ("base", "base: mainnet, REAL money"),
        ("fake", "fake: simulated wallet, no chain at all"),
    ], default="base-sepolia"))
    if network == "base" and not p.confirm("Mainnet uses real USDC. Continue with base?", False):
        network = "base-sepolia"

    # 4. OpenRouter
    p.say("\n[bold]OpenRouter[/bold] runs the agent's model and lets the guard track compute spend. "
          "Use a key only Cashmaxx uses: the guard counts all spend on it as the agent's cost.")
    api_key = p.secret("OpenRouter API key (sk-or-...)").strip()
    models: list[str] = []
    try:
        models = services.list_models(api_key)
    except Exception as exc:  # offline or bad key: let the user type a model id
        warnings.append(f"Could not fetch OpenRouter models ({exc}).")
    model = p.autocomplete("Main model (type to search)", models, default_model(models))

    # 5. Earning methods
    methods = p.checkbox("Earning methods the agent may try",
                         [(m, EARNING_METHOD_HELP[m]) for m in ALL_EARNING_METHODS],
                         checked=set(ALL_EARNING_METHODS))

    # 6. Safety rules
    rules = p.checkbox("Safety rules (you choose; the guard's limits apply regardless)",
                       [(r, RULE_HELP[r]) for r in ALL_RULES], checked=set(ALL_RULES))
    loss_stop = Decimal("10")
    if "loss_stop" in rules:
        loss_stop = parse_decimal(p.text("Freeze when the 7-day net loss exceeds (USD)",
                                         str(min(budget, Decimal("10"))), validate=_decimal_problem))

    # 7. Compute payment mode
    p.say(
        "\nBoth the OpenRouter account and the wallet are yours. The agent only controls the "
        "wallet, through the guard. The mode decides how compute spend is counted."
    )
    mode = cast(ComputePaymentMode, p.select(
        "Compute payment mode", [(m, h) for m, h in COMPUTE_MODE_HELP.items()], default="virtual",
    ))
    owner_wallet = x402_url = None
    if mode == "reimburse":
        owner_wallet = p.text("Your wallet address for reimbursements (0x...)",
                              validate=lambda v: None if v.startswith("0x") and len(v) == 42
                              else "Expected a 0x address (42 characters)")
    elif mode == "x402_gateway":
        x402_url = p.text("x402 inference gateway URL",
                          validate=lambda v: None if v.startswith("https://") else "Use an https URL")

    # 8. CDP wallet
    cdp = CdpConfig()
    wallet_address: str | None = None
    if network == "fake":
        warnings.append("Network is 'fake': no real wallet was created.")
    else:
        p.say("\n[bold]Coinbase CDP[/bold] server wallet (portal.cdp.coinbase.com > API keys). "
              "These secrets go only into the guard's config.")
        key_id = p.text("CDP API key id (leave empty to skip)").strip()
        if key_id:
            cdp = CdpConfig(api_key_id=key_id, api_key_secret=p.secret("CDP API key secret"),
                            wallet_secret=p.secret("CDP wallet secret"))
            faucet = None
            if network == "base-sepolia" and p.confirm("Request free testnet USDC from the CDP faucet?"):
                faucet = "base-sepolia"
            try:
                wallet_address = services.cdp_address(cdp, faucet)
            except Exception as exc:
                warnings.append(f"Could not create or load the CDP account ({exc}). "
                                "The guard will retry on start.")
        else:
            warnings.append("No CDP credentials: the guard cannot move money until you add them "
                            "to guard.json.")

    # 9. Stripe
    stripe_key = ""
    p.say("\n[bold]Stripe[/bold] (optional) lets the agent sell products with payment links. "
          "Create a restricted key with only: " + ", ".join(STRIPE_PERMISSIONS) + ".")
    while True:
        stripe_key = p.secret("Stripe restricted key (rk_..., empty to skip)").strip()
        problem = stripe_key_problem(stripe_key)
        if problem is None:
            break
        p.say(f"[red]{problem}[/red]")
    if not stripe_key and "digital_products" in methods:
        warnings.append("Digital products are enabled but Stripe is not set up; the agent can't "
                        "create payment links yet.")

    # 10. Guard Telegram bot
    p.say("\n[bold]Guard Telegram bot[/bold]: approvals come to you here. Create a NEW bot with "
          "@BotFather; it must not be the agent's chat bot.")
    bot_token = p.text("Guard bot token", validate=lambda v: check_guard_bot_token(v, config))
    chat_id = ""
    if p.confirm("Detect your chat id? (send /start to the new bot first)"):
        try:
            chat_id = services.detect_chat_id(bot_token.strip()) or ""
        except Exception as exc:
            warnings.append(f"Could not read Telegram updates ({exc}).")
        if chat_id:
            p.say(f"Found chat id {chat_id}.")
    if not chat_id:
        p.say("Your chat id is the number @userinfobot replies with.")
        chat_id = p.text("Your Telegram chat id",
                         validate=lambda v: None if v.strip().lstrip("-").isdigit()
                         else "A chat id is a number")

    # 11. Owner PIN
    p.say("\n[bold]Owner PIN[/bold] for approving in the WebUI and changing settings. "
          "It is stored only as a scrypt hash in the guard's config.")
    while True:
        pin = p.secret("Owner PIN")
        problem = validate_pin(pin, p.secret("Repeat the PIN"))
        if problem is None:
            break
        p.say(f"[red]{problem}[/red]")

    answers = OnboardAnswers(
        budget_usd=budget, per_tx_approval_usd=per_tx, daily_cap_usd=daily, network=network,
        openrouter_api_key=api_key, model=model.strip(),
        earning_methods=cast(set[EarningMethod], set(methods)),
        rules=cast(set[Rule], set(rules)), loss_stop_usd=loss_stop,
        compute_payment_mode=mode, owner_wallet=owner_wallet, x402_gateway_url=x402_url,
        cdp=cdp, stripe_restricted_key=stripe_key, guard_bot_token=bot_token.strip(),
        owner_chat_id=chat_id.strip(), owner_pin=pin,
        agent_token=generate_agent_token(),  # 12.
    )
    build_settings(answers)  # validate the combination before anything is written
    return CollectResult(answers=answers, wallet_address=wallet_address, warnings=warnings)


# --- orchestration ---------------------------------------------------------------------------


SAME_MACHINE_WARNING = (
    "The agent and the guard run on this machine as the same user. restrictToWorkspace and the "
    "exec sandbox are then the only barrier between the agent and guard.json. For real money, "
    "run the guard as a separate OS user or container."
)


NO_SANDBOX_WARNING = (
    "No OS sandbox (seatbelt/bwrap) for the agent's shell on this machine, so it can read "
    "guard.json. Install bubblewrap or run the guard in a container before using real money."
)


@dataclass
class OnboardOutcome:
    guard_config_path: Path
    nanobot_config_path: Path
    workspace: Path
    wallet_address: str | None
    warnings: list[str]
    workspace_changes: list[str]
    skipped_files: list[str]


def write_everything(
    answers: OnboardAnswers,
    config: Config,
    *,
    nanobot_config_path: Path,
    guard_path: Path | None = None,
    wallet_address: str | None = None,
    warnings: list[str] | None = None,
    force_workspace: bool = False,
) -> OnboardOutcome:
    """Steps 13-14: write guard.json, nanobot's config and the workspace files."""
    from cashmaxx.config import guard_config_path, load_guard_config, save_guard_config
    from cashmaxx.plugin.workspace import install_workspace
    from nanobot.config.loader import save_config

    guard_path = guard_path or guard_config_path()
    existing = None
    if guard_path.exists():
        try:
            existing = load_guard_config(guard_path)
        except Exception:
            existing = None
    guard_cfg = build_guard_config(answers, existing)
    save_guard_config(guard_cfg, guard_path)

    apply_nanobot_config(config, answers)
    save_config(config, nanobot_config_path)
    try:
        nanobot_config_path.chmod(0o600)
    except OSError:
        pass

    report = install_workspace(config.workspace_path, guard_cfg.settings, force=force_workspace)
    warnings = list(warnings or [])
    if not config.tools.exec.sandbox:
        warnings.append(NO_SANDBOX_WARNING)
    return OnboardOutcome(
        guard_config_path=guard_path,
        nanobot_config_path=nanobot_config_path,
        workspace=config.workspace_path,
        wallet_address=wallet_address,
        warnings=warnings,
        workspace_changes=report.changed,
        skipped_files=report.skipped,
    )


def summary_lines(outcome: OnboardOutcome, answers: OnboardAnswers) -> list[str]:
    lines = [
        "",
        "Cashmaxx is set up.",
        f"  Guard config:   {outcome.guard_config_path} (secrets, mode 0600)",
        f"  nanobot config: {outcome.nanobot_config_path}",
        f"  Workspace:      {outcome.workspace}",
        f"  Network:        {answers.network}",
        f"  Budget:         ${answers.budget_usd} (approval above ${answers.per_tx_approval_usd}, "
        f"daily cap ${answers.daily_cap_usd})",
        f"  Model:          openrouter {answers.model}",
    ]
    if outcome.wallet_address:
        lines.append(f"  Fund this address with USDC on {answers.network}: {outcome.wallet_address}")
    if outcome.skipped_files:
        lines.append("  Left alone (you edited them; rerun with --force to overwrite): "
                     + ", ".join(outcome.skipped_files))
    steps = ["Terminal 1: cashmaxx guard", "Terminal 2: nanobot gateway"]
    if answers.network != "fake":  # the fake wallet starts with simulated USDC
        steps.append("Fund the wallet address"
                     + (" (testnet USDC from faucet.circle.com)"
                        if answers.network == "base-sepolia" else ""))
    steps.append("Check it with /cashmaxx in chat or `cashmaxx status`")
    lines += ["", "Next steps:"] + [f"  {i}. {step}" for i, step in enumerate(steps, 1)]
    for w in outcome.warnings:
        lines.append(f"Warning: {w}")
    lines.append(f"Warning: {SAME_MACHINE_WARNING}")
    return lines


def run_onboarding(
    *,
    nanobot_config_path: Path | None = None,
    prompter: Prompter | None = None,
    services: Services | None = None,
    force_workspace: bool = False,
    run_nanobot_onboard: Callable[[Path], None] | None = None,
) -> OnboardOutcome:
    """The full interactive flow used by ``cashmaxx onboard``."""
    from nanobot.config.loader import get_config_path, load_config, set_config_path

    path = (nanobot_config_path or get_config_path()).expanduser()
    if nanobot_config_path is not None:
        set_config_path(path)
    p = prompter or QuestionaryPrompter()

    # 1. nanobot's own onboarding when there is no config yet
    if not path.exists():
        p.say("No nanobot config yet; running nanobot's setup first.")
        (run_nanobot_onboard or _nanobot_onboard)(path)
    config = load_config(path)

    collected = collect_answers(p, config, services)
    outcome = write_everything(
        collected.answers, config, nanobot_config_path=path, wallet_address=collected.wallet_address,
        warnings=collected.warnings, force_workspace=force_workspace,
    )
    for line in summary_lines(outcome, collected.answers):  # 15.
        p.say(line)
    return outcome


def _nanobot_onboard(path: Path) -> None:
    """Reuse ``nanobot onboard`` (creates config + workspace; the wizard is optional)."""
    import questionary

    from nanobot.cli.commands import onboard

    wizard = bool(questionary.confirm(
        "Open nanobot's full setup wizard too (chat channels such as Telegram)?", default=False,
    ).ask())
    onboard(workspace=None, config=str(path), wizard=wizard, non_interactive_refresh=False)

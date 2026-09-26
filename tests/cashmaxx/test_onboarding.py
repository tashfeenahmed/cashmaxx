from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from cashmaxx import onboarding as ob
from cashmaxx.config import BUDGET_PRESETS, CdpConfig, GuardConfig, load_guard_config
from nanobot.config.schema import Config


def _answers(**kw: Any) -> ob.OnboardAnswers:
    base: dict[str, Any] = dict(
        budget_usd=Decimal("50"), per_tx_approval_usd=Decimal("5"), daily_cap_usd=Decimal("10"),
        openrouter_api_key="sk-or-test", model="google/gemini-2.5-flash",
        guard_bot_token="111:guard", owner_chat_id="4242", owner_pin="123456",
        agent_token="agent-tok",
    )
    base.update(kw)
    return ob.OnboardAnswers(**base)


# --- budget ---------------------------------------------------------------------------------


@pytest.mark.parametrize("preset", list(BUDGET_PRESETS))
def test_presets_come_from_config(preset: str) -> None:
    assert ob.budget_for(preset) == BUDGET_PRESETS[preset]


def test_custom_budget_scales_like_presets() -> None:
    assert ob.budget_for("custom", Decimal("200")) == (Decimal("200"), Decimal("20.00"),
                                                       Decimal("50.00"))
    budget, per_tx, daily = ob.budget_for("custom", Decimal("0.05"))
    assert per_tx == Decimal("0.01") and daily >= per_tx
    with pytest.raises(ValueError):
        ob.budget_for("custom", None)


def test_parse_decimal() -> None:
    assert ob.parse_decimal("$12.345") == Decimal("12.34")
    for bad in ("abc", "0", "-3", "nan"):
        with pytest.raises(ValueError):
            ob.parse_decimal(bad)


# --- secrets ---------------------------------------------------------------------------------


def test_pin_hash_format_and_verify() -> None:
    from cashmaxx.guard.auth import verify_pin

    stored = ob.hash_pin("123456")
    scheme, n, r, p, salt, digest = stored.split("$")
    assert (scheme, n, r, p) == ("scrypt", str(2**14), "8", "1")
    assert len(bytes.fromhex(salt)) == 16 and len(bytes.fromhex(digest)) == 32
    assert verify_pin("123456", stored) and not verify_pin("654321", stored)


def test_local_scrypt_fallback_matches_guard_format() -> None:
    from cashmaxx.guard.auth import verify_pin

    stored = ob._scrypt_pin("hunter22")
    assert stored.split("$")[:4] == ["scrypt", "16384", "8", "1"]
    assert verify_pin("hunter22", stored)


def test_fallbacks_when_guard_auth_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "cashmaxx.guard.auth":
            raise ImportError("not yet")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert ob.hash_pin("123456").startswith("scrypt$16384$8$1$")
    assert ob.hash_agent_token("tok") == hashlib.sha256(b"tok").hexdigest()
    assert len(ob.generate_agent_token()) >= 40


def test_agent_token_hash_matches_guard_check() -> None:
    from cashmaxx.guard.auth import check_agent_token

    token = ob.generate_agent_token()
    assert check_agent_token(token, ob.hash_agent_token(token))


def test_validate_pin() -> None:
    assert ob.validate_pin("123", "123") is not None
    assert ob.validate_pin("123456", "123457") is not None
    assert ob.validate_pin("123456", "123456") is None


# --- tokens ------------------------------------------------------------------------------------


def test_guard_bot_token_must_differ_from_agent_channel() -> None:
    config = Config.model_validate({"channels": {"telegram": {"enabled": True, "token": "111:agent"}}})
    assert ob.check_guard_bot_token("111:agent", config) is not None
    assert "same token" in (ob.check_guard_bot_token(" 111:agent ", config) or "")
    assert ob.check_guard_bot_token("", config) is not None
    assert ob.check_guard_bot_token("nocolon", config) is not None
    assert ob.check_guard_bot_token("222:guard", config) is None
    assert ob.check_guard_bot_token("222:guard", Config()) is None


def test_owner_chat_id_from_updates() -> None:
    payload = {"ok": True, "result": [
        {"message": {"chat": {"id": 1, "type": "private"}}},
        {"message": {"chat": {"id": -100, "type": "group"}}},
        {"message": {"chat": {"id": 777, "type": "private"}}},
        {"my_chat_member": {}},
    ]}
    assert ob.owner_chat_id_from_updates(payload) == "777"
    assert ob.owner_chat_id_from_updates({"result": []}) is None
    assert ob.owner_chat_id_from_updates("junk") is None


def test_stripe_key_problem() -> None:
    assert ob.stripe_key_problem("") is None
    assert ob.stripe_key_problem("rk_test_abc") is None
    assert "restricted" in (ob.stripe_key_problem("sk_live_abc") or "")
    assert ob.stripe_key_problem("pk_test_abc") is not None


# --- models ------------------------------------------------------------------------------------


def test_models_parse_and_default() -> None:
    ids = ob.parse_model_ids({"data": [{"id": "b/x"}, {"id": "a/y"}, {"id": "a/y"}, {"nope": 1}]})
    assert ids == ["a/y", "b/x"]
    assert ob.default_model(["x/y", "google/gemini-2.5-flash"]) == "google/gemini-2.5-flash"
    assert ob.default_model(["x/y", "google/gemini-9-flash"]) == "google/gemini-9-flash"
    assert ob.default_model(["x/y"]) == "x/y"
    assert ob.default_model([]) == ob.FALLBACK_MODEL


def test_fetch_models_uses_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "google/gemini-2.5-flash"}]})

    assert ob.fetch_openrouter_models("sk-or-1", transport=httpx.MockTransport(handler)) == [
        "google/gemini-2.5-flash"
    ]
    assert seen[0].headers["authorization"] == "Bearer sk-or-1"


# --- config building --------------------------------------------------------------------------


def test_build_guard_config() -> None:
    answers = _answers(stripe_restricted_key="rk_test_1",
                       cdp=CdpConfig(api_key_id="id", api_key_secret="s", wallet_secret="w"))
    guard = ob.build_guard_config(answers)
    assert guard.settings.budget_usd == Decimal("50")
    assert guard.settings.network == "base-sepolia"
    assert guard.agent_token_hash == hashlib.sha256(b"agent-tok").hexdigest()
    assert guard.owner_pin_hash.startswith("scrypt$")
    assert guard.telegram.bot_token == "111:guard" and guard.telegram.owner_chat_id == "4242"
    assert guard.openrouter_api_key == "sk-or-test"
    assert guard.cdp.api_key_id == "id" and guard.stripe_restricted_key == "rk_test_1"
    assert "agent-tok" not in guard.model_dump_json() and "123456" not in guard.model_dump_json()


def test_build_guard_config_keeps_existing_account_and_url() -> None:
    existing = GuardConfig.model_validate({"publicBaseUrl": "https://pnl.example",
                                           "cdp": {"accountName": "mine"}})
    guard = ob.build_guard_config(_answers(), existing)
    assert guard.public_base_url == "https://pnl.example" and guard.cdp.account_name == "mine"


def test_build_settings_validates_modes() -> None:
    with pytest.raises(ValueError):
        ob.build_settings(_answers(compute_payment_mode="reimburse"))
    s = ob.build_settings(_answers(compute_payment_mode="reimburse", owner_wallet="0x" + "a" * 40,
                                   earning_methods={"bounties"}, rules={"no_spam"}))
    assert s.earning_methods == {"bounties"} and s.rules == {"no_spam"}


def test_apply_nanobot_config() -> None:
    config = Config.model_validate({
        "channels": {"telegram": {"enabled": True, "token": "111:agent"}},
        "modelPresets": {"primary": {"model": "x", "provider": "openai"}},
        "agents": {"defaults": {"modelPreset": "primary"}},
    })
    ob.apply_nanobot_config(config, _answers())
    assert config.providers.openrouter.api_key == "sk-or-test"
    assert config.agents.defaults.model == "google/gemini-2.5-flash"
    assert config.agents.defaults.provider == "openrouter"
    assert config.agents.defaults.model_preset is None
    assert config.cashmaxx is not None and config.cashmaxx.agent_token == "agent-tok"
    assert config.cashmaxx.guard_url == "http://127.0.0.1:18790"
    assert config.tools.restrict_to_workspace is True
    assert config.channels.telegram["inlineKeyboards"] is True  # type: ignore[attr-defined]
    dumped = config.model_dump(mode="json", by_alias=True)
    assert dumped["cashmaxx"]["agentToken"] == "agent-tok"
    assert Config.model_validate(dumped).cashmaxx == config.cashmaxx


def test_apply_without_telegram_leaves_channels_alone() -> None:
    config = Config()
    ob.apply_nanobot_config(config, _answers())
    assert getattr(config.channels, "telegram", None) is None


def test_write_everything(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    config = Config.model_validate({"agents": {"defaults": {"workspace": str(ws)}}})
    cfg_path = tmp_path / "nanobot" / "config.json"
    guard_path = tmp_path / "cmx" / "guard.json"
    outcome = ob.write_everything(_answers(earning_methods={"x402_apis"}), config,
                                  nanobot_config_path=cfg_path, guard_path=guard_path,
                                  wallet_address="0xfund")
    assert (guard_path.stat().st_mode & 0o777) == 0o600
    assert (cfg_path.stat().st_mode & 0o777) == 0o600
    assert load_guard_config(guard_path).settings.earning_methods == {"x402_apis"}
    saved = json.loads(cfg_path.read_text())
    assert saved["cashmaxx"]["agentToken"] == "agent-tok"
    assert saved["tools"]["restrictToWorkspace"] is True
    assert "ownerPin" not in cfg_path.read_text() and "111:guard" not in cfg_path.read_text()
    assert (ws / "skills" / "cashmaxx-x402-apis" / "SKILL.md").exists()
    assert not (ws / "skills" / "cashmaxx-bounties").exists()
    lines = "\n".join(ob.summary_lines(outcome, _answers()))
    assert "cashmaxx guard" in lines and "nanobot gateway" in lines and "0xfund" in lines
    assert "separate OS user" in lines


# --- the prompt flow with a scripted prompter ------------------------------------------------


class ScriptedPrompter:
    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.said: list[str] = []
        self.asked: list[str] = []

    def _take(self, message: str, default: Any = None) -> Any:
        self.asked.append(message)
        for key in sorted(self.answers, key=len, reverse=True):  # most specific key wins
            value = self.answers[key]
            if key in message:
                if isinstance(value, list) and value and isinstance(value[0], _Seq):
                    return value.pop(0).value
                return value
        return default

    def say(self, text: str) -> None:
        self.said.append(text)

    def select(self, message: str, choices: list[tuple[str, str]], default: str | None = None) -> str:
        value = self._take(message, default)
        assert value in [c[0] for c in choices]
        return value

    def checkbox(self, message: str, choices: list[tuple[str, str]], checked: set[str]) -> list[str]:
        return list(self._take(message, sorted(checked)))

    def text(self, message: str, default: str = "", validate: Callable[[str], str | None] | None = None) -> str:
        value = self._take(message, default)
        if validate is not None:
            assert validate(value) is None, (message, value)
        return value

    def secret(self, message: str) -> str:
        return self._take(message, "")

    def confirm(self, message: str, default: bool = True) -> bool:
        return self._take(message, default)

    def autocomplete(self, message: str, choices: list[str], default: str) -> str:
        return self._take(message, default)


class _Seq:
    def __init__(self, value: Any) -> None:
        self.value = value


def _script(**over: Any) -> dict[str, Any]:
    script: dict[str, Any] = {
        "Budget": "10",
        "Adjust the limits": False,
        "Network": "base-sepolia",
        "OpenRouter API key": "sk-or-abc",
        "Main model": "",  # take the default
        "Earning methods": ["bounties", "x402_apis"],
        "Safety rules": ["no_trading", "loss_stop"],
        "Freeze when": "5",
        "Compute payment mode": "virtual",
        "CDP API key id": "key-id",
        "CDP API key secret": "secret",
        "CDP wallet secret": "wallet-secret",
        "faucet": False,
        "Stripe restricted key": "",
        "Guard bot token": "222:guard",
        "Detect your chat id": True,
        "Repeat the PIN": "123456",
        "Owner PIN": "123456",
    }
    script.update(over)
    return script


def _services(models: list[str] | None = None, chat: str | None = "999",
              address: str = "0xnew") -> ob.Services:
    return ob.Services(
        list_models=lambda key: models if models is not None else ["google/gemini-2.5-flash"],
        detect_chat_id=lambda token: chat,
        cdp_address=lambda cdp, faucet: address,
    )


def test_collect_answers_happy_path() -> None:
    script = _script()
    script.pop("Main model")
    result = ob.collect_answers(ScriptedPrompter(script), Config(), _services())
    a = result.answers
    assert (a.budget_usd, a.per_tx_approval_usd, a.daily_cap_usd) == BUDGET_PRESETS["10"]
    assert a.model == "google/gemini-2.5-flash"
    assert a.earning_methods == {"bounties", "x402_apis"}
    assert a.rules == {"no_trading", "loss_stop"} and a.loss_stop_usd == Decimal("5.00")
    assert a.owner_chat_id == "999" and a.owner_pin == "123456"
    assert a.cdp.api_key_id == "key-id" and result.wallet_address == "0xnew"
    assert len(a.agent_token) >= 40


def test_collect_answers_fake_network_skips_wallet_with_warning() -> None:
    result = ob.collect_answers(
        ScriptedPrompter(_script(Network="fake", **{"Main model": "x/y"})), Config(),
        _services(models=[]),
    )
    assert result.answers.cdp.api_key_id == ""
    assert result.wallet_address is None
    assert any("fake" in w for w in result.warnings)


def test_collect_answers_pin_mismatch_retries() -> None:
    script = _script(**{
        "Repeat the PIN": [_Seq("000000"), _Seq("123456")],
        "Owner PIN": [_Seq("123456"), _Seq("123456")],
    })
    prompter = ScriptedPrompter(script)
    result = ob.collect_answers(prompter, Config(), _services())
    assert result.answers.owner_pin == "123456"
    assert any("do not match" in s for s in prompter.said)


def test_collect_answers_custom_budget_and_reimburse() -> None:
    script = _script(**{
        "Budget in USD": "200",
        "Payments above this": "15",
        "Daily spending cap": "40",
        "Compute payment mode": "reimburse",
        "Your wallet address": "0x" + "b" * 40,
    })
    script["Budget"] = "custom"
    result = ob.collect_answers(ScriptedPrompter(script), Config(), _services())
    a = result.answers
    assert (a.budget_usd, a.per_tx_approval_usd, a.daily_cap_usd) == (
        Decimal("200.00"), Decimal("15.00"), Decimal("40.00"))
    assert a.compute_payment_mode == "reimburse" and a.owner_wallet == "0x" + "b" * 40


def test_run_onboarding_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import nanobot.config.loader as loader

    monkeypatch.setattr(loader, "_current_config_path", loader._current_config_path)
    monkeypatch.setenv("CASHMAXX_HOME", str(tmp_path / "cmx"))
    cfg_path = tmp_path / "nb" / "config.json"
    ws = tmp_path / "ws"

    def fake_nanobot_onboard(path: Path) -> None:
        from nanobot.config.loader import save_config

        save_config(Config.model_validate({"agents": {"defaults": {"workspace": str(ws)}}}), path)

    outcome = ob.run_onboarding(
        nanobot_config_path=cfg_path, prompter=ScriptedPrompter(_script()), services=_services(),
        run_nanobot_onboard=fake_nanobot_onboard,
    )
    assert outcome.guard_config_path == tmp_path / "cmx" / "guard.json"
    assert json.loads(cfg_path.read_text())["cashmaxx"]["guardUrl"] == "http://127.0.0.1:18790"
    assert (ws / "HEARTBEAT.md").exists() and (ws / "RULES.md").exists()

"""Integration values in ``GuardConfig``: listing, merge updates, clearing and test results.

Pure functions over the config object; the caller holds the guard's config lock and saves the
file. The four original integrations (openrouter, cdp, stripe, telegram) keep their values in the
old ``GuardConfig`` attributes (``LEGACY_FIELDS``); their ``connected_at`` and last test result
live in ``GuardConfig.integrations[id]`` with empty ``fields``.

Secret values never leave this module except through ``values()`` (for the guard's own clients).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from cashmaxx.config import GuardConfig, IntegrationConfig
from cashmaxx.guard.errors import ApiError
from cashmaxx.guard.store import iso
from cashmaxx.integrations_catalog import (
    BY_ID,
    INTEGRATIONS,
    LEGACY_FIELDS,
    FieldSpec,
    IntegrationSpec,
)

Scope = Literal["agent", "owner"]
MAX_FIELD_LEN = 4000
EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
REDACTED = "***"


def get_guard_spec(integration_id: str) -> IntegrationSpec:
    """The spec for a guard-kind integration: 404 unknown, 400 ``agent_integration``."""
    spec = BY_ID.get(integration_id)
    if spec is None:
        raise ApiError(404, "not_found", f"unknown integration {integration_id!r}")
    if spec.kind != "guard":
        raise ApiError(400, "agent_integration",
                       f"{integration_id} is an agent integration; configure it through the "
                       "WebUI or CLI (nanobot MCP config), not the guard")
    return spec


def _get_attr(config: GuardConfig, dotted: str) -> str:
    obj: Any = config
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return str(obj or "")


def _set_attr(config: GuardConfig, dotted: str, value: str) -> None:
    *parents, last = dotted.split(".")
    obj: Any = config
    for part in parents:
        obj = getattr(obj, part)
    setattr(obj, last, value)


def is_legacy(spec: IntegrationSpec) -> bool:
    return any((spec.id, f.name) in LEGACY_FIELDS for f in spec.fields)


def values(config: GuardConfig, spec: IntegrationSpec) -> dict[str, str]:
    """Current field values (secrets included). Unset fields are ``""``."""
    out: dict[str, str] = {}
    stored = config.integrations.get(spec.id)
    for f in spec.fields:
        attr = LEGACY_FIELDS.get((spec.id, f.name))
        if attr is not None:
            out[f.name] = _get_attr(config, attr)
        else:
            out[f.name] = stored.fields.get(f.name, "") if stored else ""
    return out


def _required(spec: IntegrationSpec, vals: dict[str, str]) -> list[str]:
    """Names of required fields, including conditional ones."""
    names = [f.name for f in spec.fields if f.required]
    if spec.id == "hosting" and vals.get("provider") == "cloudflare_token":
        names.append("tunnelToken")
    return names


def missing_required(spec: IntegrationSpec, vals: dict[str, str]) -> list[str]:
    return [name for name in _required(spec, vals) if not vals.get(name)]


def is_connected(config: GuardConfig, spec: IntegrationSpec) -> bool | None:
    """``None`` for agent-kind entries (the WebUI proxy fills those in)."""
    if spec.kind != "guard":
        return None
    return not missing_required(spec, values(config, spec))


def secret_values(config: GuardConfig) -> list[str]:
    """Every secret the guard holds, for scrubbing error messages."""
    out: list[str] = []
    for spec in INTEGRATIONS:
        if spec.kind != "guard":
            continue
        vals = values(config, spec)
        out.extend(vals[f.name] for f in spec.fields if f.secret and len(vals[f.name]) >= 4)
    return out


def redact(text: str, config: GuardConfig) -> str:
    for secret in sorted(secret_values(config), key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return text


def _meta(config: GuardConfig, spec: IntegrationSpec) -> IntegrationConfig | None:
    return config.integrations.get(spec.id)


def agent_item(config: GuardConfig, spec: IntegrationSpec) -> dict[str, Any]:
    return {"id": spec.id, "label": spec.label, "kind": spec.kind, "category": spec.category,
            "connected": is_connected(config, spec)}


def _field_json(spec: IntegrationSpec, f: FieldSpec, value: str) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": f.name, "label": f.label, "secret": f.secret, "required": f.required,
        "placeholder": f.placeholder, "choices": list(f.choices), "set": bool(value),
    }
    if not f.secret:
        item["value"] = value
    return item


def owner_item(config: GuardConfig, spec: IntegrationSpec) -> dict[str, Any]:
    item = agent_item(config, spec)
    guard_kind = spec.kind == "guard"
    vals = values(config, spec) if guard_kind else {f.name: "" for f in spec.fields}
    meta = _meta(config, spec) if guard_kind else None
    last_test: dict[str, Any] | None = None
    if meta is not None and meta.last_test_at is not None:
        last_test = {"ok": bool(meta.last_test_ok), "message": meta.last_test_message or "",
                     "at": meta.last_test_at}
    item.update({
        "summary": spec.summary,
        "docs_url": spec.docs_url,
        "related_settings": list(spec.related_settings),
        "fields": [_field_json(spec, f, vals[f.name]) for f in spec.fields],
        "connected_at": meta.connected_at if meta else None,
        "last_test": last_test,
    })
    return item


def listing(config: GuardConfig, scope: Scope) -> list[dict[str, Any]]:
    build = owner_item if scope == "owner" else agent_item
    return [build(config, spec) for spec in INTEGRATIONS]


def _validate_value(spec: IntegrationSpec, f: FieldSpec, value: str) -> None:
    if len(value) > MAX_FIELD_LEN:
        raise ApiError(422, "invalid", f"{f.name} is too long")
    if any(ch in value for ch in "\r\n\0"):
        raise ApiError(422, "invalid", f"{f.name} must be a single line")
    if not value:
        return
    if f.choices and value not in f.choices:
        raise ApiError(422, "invalid", f"{f.name} must be one of {list(f.choices)}")
    if spec.id == "gmail" and f.name == "address" and not EMAIL_RE.match(value):
        raise ApiError(422, "invalid", "address must be an email address")
    if spec.id == "telegram" and f.name == "ownerChatId" and not re.fullmatch(r"-?\d+", value):
        raise ApiError(422, "invalid", "ownerChatId must be a number")


def apply_update(
    config: GuardConfig, spec: IntegrationSpec, fields: object, now: datetime
) -> list[str]:
    """Merge ``fields`` into the config. Returns the names of the fields that changed.

    ``""`` for a secret keeps the stored value; ``""`` for a plain field clears it. After the merge
    every required field must be set. ``connected_at`` is set when the integration becomes
    connected or a non-secret field (the account identity) changes; rotating only a secret keeps it.
    """
    if not isinstance(fields, dict):
        raise ApiError(422, "invalid", "fields must be an object of name -> string")
    by_name = {f.name: f for f in spec.fields}
    current = values(config, spec)
    merged = dict(current)
    for raw_name, raw_value in fields.items():  # pyright: ignore[reportUnknownVariableType]
        name = str(raw_name)  # pyright: ignore[reportUnknownArgumentType]
        f = by_name.get(name)
        if f is None:
            raise ApiError(422, "invalid", f"unknown field {name!r} for {spec.id}")
        if not isinstance(raw_value, str):
            raise ApiError(422, "invalid", f"{name} must be a string")
        value = raw_value.strip()
        if f.secret and value == "":
            continue  # keep the stored secret
        _validate_value(spec, f, value)
        merged[name] = value
    missing = missing_required(spec, merged)
    if missing:
        raise ApiError(422, "invalid", f"missing required fields: {', '.join(missing)}")
    changed = [name for name in merged if merged[name] != current[name]]
    was_connected = not missing_required(spec, current)
    identity_changed = any(not by_name[n].secret for n in changed)

    meta = config.integrations.get(spec.id) or IntegrationConfig()
    for name, value in merged.items():
        attr = LEGACY_FIELDS.get((spec.id, name))
        if attr is not None:
            _set_attr(config, attr, value)
        elif value:
            meta.fields[name] = value
        else:
            meta.fields.pop(name, None)
    if not was_connected or identity_changed or meta.connected_at is None:
        meta.connected_at = iso(now)
    if changed:
        meta.last_test_ok = meta.last_test_message = meta.last_test_at = None
    config.integrations[spec.id] = meta
    return changed


def clear(config: GuardConfig, spec: IntegrationSpec) -> None:
    for f in spec.fields:
        attr = LEGACY_FIELDS.get((spec.id, f.name))
        if attr is not None:
            _set_attr(config, attr, "")
    config.integrations.pop(spec.id, None)


def set_field(config: GuardConfig, spec: IntegrationSpec, name: str, value: str) -> None:
    """Guard-side write of a non-legacy field (e.g. the AgentMail inbox it created)."""
    meta = config.integrations.get(spec.id) or IntegrationConfig()
    meta.fields[name] = value
    config.integrations[spec.id] = meta


def record_test(
    config: GuardConfig, spec: IntegrationSpec, *, ok: bool, message: str, now: datetime
) -> dict[str, Any]:
    meta = config.integrations.get(spec.id) or IntegrationConfig()
    meta.last_test_ok = ok
    meta.last_test_message = message
    meta.last_test_at = iso(now)
    config.integrations[spec.id] = meta
    return {"ok": ok, "message": message, "at": meta.last_test_at}


def connected_at(config: GuardConfig, integration_id: str) -> str | None:
    meta = config.integrations.get(integration_id)
    return meta.connected_at if meta else None

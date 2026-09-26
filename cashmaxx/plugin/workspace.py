"""Install Cashmaxx's mission templates and skills into a nanobot workspace (idempotent).

What gets written:

- ``HEARTBEAT.md``: a managed block holding the money loop under ``## Active Tasks``. nanobot's
  heartbeat only runs tasks under an "Active Tasks" heading, so ``/pause`` renames the block's
  heading to ``## Paused Tasks ...`` and ``/resume`` renames it back. That pauses only the money
  loop (other heartbeat tasks keep running), survives restarts, and needs no cron changes.
- ``SOUL.md``: a managed block appended after the owner's own soul text.
- ``RULES.md``: a whole managed file rendered from ``CashmaxxSettings``.
- ``skills/cashmaxx-*/SKILL.md``: the core skill plus one per *enabled* earning method.

User edits are never overwritten without ``force``. Each managed region carries the sha256 of the
content we wrote. If the current content no longer matches that hash, someone edited it, and we
leave it alone.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from typing import Literal

from jinja2 import Environment, StrictUndefined

from cashmaxx.config import ALL_EARNING_METHODS, CashmaxxSettings, EarningMethod
from cashmaxx.money import fmt_usd

Outcome = Literal["created", "updated", "unchanged", "skipped", "removed"]

ACTIVE_HEADING = "## Active Tasks"
PAUSED_HEADING = "## Paused Tasks (Cashmaxx money loop is paused. Send /resume to restart it.)"
LOOP_BLOCK = "money-loop"
SOUL_BLOCK = "soul"

METHOD_LABELS: dict[str, str] = {
    "digital_products": "Digital products (Stripe payment links)",
    "x402_apis": "Paid APIs with x402",
    "bounties": "Bounties",
    "agent_marketplaces": "Agent marketplaces",
}

# installed skill name -> (package source dir, earning method gate or None for always)
SKILLS: dict[str, tuple[str, EarningMethod | None]] = {
    "cashmaxx-core": ("cashmaxx-core", None),
    "cashmaxx-digital-products": ("digital-products", "digital_products"),
    "cashmaxx-x402-apis": ("x402-apis", "x402_apis"),
    "cashmaxx-bounties": ("bounties", "bounties"),
    "cashmaxx-agent-marketplaces": ("agent-marketplaces", "agent_marketplaces"),
}

_FILE_MARKER = re.compile(r"\n?<!-- cashmaxx:managed sha256=([0-9a-f]{16}) -->\s*\Z")


def _block_re(name: str) -> re.Pattern[str]:
    return re.compile(
        rf"<!-- cashmaxx:begin {re.escape(name)} sha256=([0-9a-f]{{16}}) -->\n"
        rf"(.*?)\n?<!-- cashmaxx:end {re.escape(name)} -->",
        re.DOTALL,
    )


@dataclass
class InstallReport:
    results: dict[str, Outcome] = field(default_factory=dict)

    def add(self, path: str, outcome: Outcome) -> None:
        self.results[path] = outcome

    @property
    def skipped(self) -> list[str]:
        return [p for p, o in self.results.items() if o == "skipped"]

    @property
    def changed(self) -> list[str]:
        return [p for p, o in self.results.items() if o in {"created", "updated", "removed"}]


# --- rendering --------------------------------------------------------------------------------


def _package_text(*parts: str) -> str:
    node = files("cashmaxx")
    for part in parts:
        node = node / part
    return node.read_text(encoding="utf-8")


def _usd(value: Decimal) -> str:
    return fmt_usd(value)


def _env() -> Environment:
    return Environment(undefined=StrictUndefined, keep_trailing_newline=True, autoescape=False)


def _render(template: str, settings: CashmaxxSettings) -> str:
    enabled = [METHOD_LABELS[m] for m in ALL_EARNING_METHODS if m in settings.earning_methods]
    text = _env().from_string(_package_text("templates", template)).render(
        s=settings,
        usd=_usd,
        method_labels=METHOD_LABELS,
        all_methods=ALL_EARNING_METHODS,
        earning_methods_text=", ".join(enabled) if enabled else "none enabled; ask the owner",
    )
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def render_rules(settings: CashmaxxSettings) -> str:
    return _render("RULES.md", settings)


def render_heartbeat_block(settings: CashmaxxSettings) -> str:
    return _render("HEARTBEAT.md", settings)


def render_soul_block(settings: CashmaxxSettings) -> str:
    return _render("SOUL.md", settings)


def enabled_skills(settings: CashmaxxSettings) -> list[str]:
    return [
        name for name, (_src, gate) in SKILLS.items()
        if gate is None or gate in settings.earning_methods
    ]


# --- managed regions -------------------------------------------------------------------------


def _normalize(body: str) -> str:
    # The pause state is not a user edit: hash the loop with its heading in the active form.
    return body.replace(PAUSED_HEADING, ACTIVE_HEADING).strip()


def _digest(body: str) -> str:
    return hashlib.sha256(_normalize(body).encode("utf-8")).hexdigest()[:16]


def _block(name: str, body: str) -> str:
    body = body.strip()
    return (
        f"<!-- cashmaxx:begin {name} sha256={_digest(body)} -->\n"
        f"{body}\n<!-- cashmaxx:end {name} -->"
    )


def _write_managed_file(path: Path, body: str, *, force: bool) -> Outcome:
    """Whole file owned by Cashmaxx; the hash marker sits on the last line."""
    body = body.rstrip() + "\n"
    content = f"{body}\n<!-- cashmaxx:managed sha256={_digest(body)} -->\n"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return "created"
    current = path.read_text(encoding="utf-8")
    if current == content:
        return "unchanged"
    match = _FILE_MARKER.search(current)
    pristine = match is not None and match.group(1) == _digest(current[: match.start()])
    if not pristine and not force:
        return "skipped"
    path.write_text(content, encoding="utf-8")
    return "updated"


def _upsert_block(path: Path, name: str, body: str, *, force: bool, header: str = "") -> Outcome:
    """A marked block inside a file the owner also edits. Appended when missing."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{header}{_block(name, body)}\n", encoding="utf-8")
        return "created"
    current = path.read_text(encoding="utf-8")
    match = _block_re(name).search(current)
    if match is None:
        sep = "" if current.endswith("\n\n") else ("\n" if current.endswith("\n") else "\n\n")
        path.write_text(f"{current}{sep}{_block(name, body)}\n", encoding="utf-8")
        return "updated"
    new_block = _block(name, body)
    if match.group(0) == new_block:
        return "unchanged"
    pristine = match.group(1) == _digest(match.group(2))
    if not pristine and not force:
        return "skipped"
    path.write_text(current[: match.start()] + new_block + current[match.end():], encoding="utf-8")
    return "updated"


def _remove_managed_file(path: Path, *, force: bool) -> Outcome | None:
    if not path.exists():
        return None
    current = path.read_text(encoding="utf-8")
    match = _FILE_MARKER.search(current)
    pristine = match is not None and match.group(1) == _digest(current[: match.start()])
    if not pristine and not force:
        return "skipped"
    path.unlink()
    try:
        path.parent.rmdir()
    except OSError:
        pass
    return "removed"


# --- pause / resume --------------------------------------------------------------------------


def loop_paused(workspace: Path) -> bool | None:
    """True/False for the money loop's state, or None when HEARTBEAT.md has no Cashmaxx block."""
    path = workspace / "HEARTBEAT.md"
    if not path.exists():
        return None
    match = _block_re(LOOP_BLOCK).search(path.read_text(encoding="utf-8"))
    if match is None:
        return None
    return PAUSED_HEADING in match.group(2)


def set_loop_paused(workspace: Path, paused: bool) -> bool:
    """Pause or resume the money loop. Returns True when the file changed.

    Raises ``LookupError`` when the workspace has no Cashmaxx loop block.
    """
    path = workspace / "HEARTBEAT.md"
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    match = _block_re(LOOP_BLOCK).search(current)
    if match is None:
        raise LookupError("HEARTBEAT.md has no Cashmaxx money loop; run `cashmaxx onboard`")
    old, new = (ACTIVE_HEADING, PAUSED_HEADING) if paused else (PAUSED_HEADING, ACTIVE_HEADING)
    body = match.group(2)
    lines = body.split("\n")
    changed = False
    for i, line in enumerate(lines):
        if line.strip() == old:
            lines[i] = new
            changed = True
            break
    if not changed:
        return False
    # Keep the original hash so a paused-but-unedited block still counts as pristine.
    block = match.group(0).replace(body, "\n".join(lines), 1)
    path.write_text(current[: match.start()] + block + current[match.end():], encoding="utf-8")
    return True


# --- installer -------------------------------------------------------------------------------


def install_workspace(
    workspace: Path, settings: CashmaxxSettings, *, force: bool = False
) -> InstallReport:
    """Write HEARTBEAT/SOUL/RULES and the enabled skills. Safe to run repeatedly."""
    workspace = workspace.expanduser()
    workspace.mkdir(parents=True, exist_ok=True)
    report = InstallReport()

    was_paused = loop_paused(workspace) is True
    loop = render_heartbeat_block(settings)
    if was_paused:
        loop = loop.replace(ACTIVE_HEADING, PAUSED_HEADING, 1)
    report.add(
        "HEARTBEAT.md",
        _upsert_block(
            workspace / "HEARTBEAT.md", LOOP_BLOCK, loop, force=force,
            header="# Heartbeat Tasks\n\n",
        ),
    )
    soul_path = workspace / "SOUL.md"
    soul_header = ""
    if not soul_path.exists():
        soul_header = _nanobot_soul_template()
    report.add(
        "SOUL.md",
        _upsert_block(soul_path, SOUL_BLOCK, render_soul_block(settings), force=force,
                      header=soul_header),
    )
    report.add("RULES.md", _write_managed_file(workspace / "RULES.md", render_rules(settings),
                                               force=force))
    (workspace / "cashmaxx").mkdir(exist_ok=True)
    log = workspace / "cashmaxx" / "experiments.md"
    if not log.exists():
        log.write_text("# Cashmaxx experiments\n\nNo experiments yet.\n", encoding="utf-8")
        report.add("cashmaxx/experiments.md", "created")

    wanted = set(enabled_skills(settings))
    for name, (src, _gate) in SKILLS.items():
        rel = f"skills/{name}/SKILL.md"
        path = workspace / rel
        if name in wanted:
            report.add(rel, _write_managed_file(path, _package_text("skills", src, "SKILL.md"),
                                                force=force))
        else:
            outcome = _remove_managed_file(path, force=force)
            if outcome is not None:
                report.add(rel, outcome)
    return report


def _nanobot_soul_template() -> str:
    try:
        text = files("nanobot").joinpath("templates", "SOUL.md").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""
    return text.rstrip() + "\n\n"

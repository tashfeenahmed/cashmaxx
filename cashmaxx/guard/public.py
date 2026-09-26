"""Read-only public P&L (``/public/pnl`` HTML, ``/public/pnl.json``). Opt-in via ``public_pnl``.

Shows totals and categories only: no entries, notes, recipients or purposes.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any

from cashmaxx.config import CashmaxxSettings
from cashmaxx.guard import ledger
from cashmaxx.guard.store import Store, iso
from cashmaxx.guard.wallet import WalletBackend

BASESCAN = {"base": "https://basescan.org", "base-sepolia": "https://sepolia.basescan.org"}


def basescan_url(network: str, address: str) -> str | None:
    base = BASESCAN.get(network)
    return f"{base}/address/{address}" if base else None


async def public_pnl(
    store: Store, settings: CashmaxxSettings, wallet: WalletBackend, now: datetime
) -> dict[str, Any]:
    address = await wallet.address()
    windows = {
        w: await ledger.pnl(store, w, now, include_entries=False) for w in ("7d", "30d", "all")
    }
    return {
        "network": wallet.network,
        "address": address,
        "basescan_url": basescan_url(wallet.network, address),
        "frozen": settings.frozen,
        "budget_usd": str(settings.budget_usd),
        "generated_at": iso(now),
        "windows": windows,
    }


def _money(amount: str) -> str:
    return f"-${escape(amount[1:])}" if amount.startswith("-") else f"${escape(amount)}"


def render_pnl_html(data: dict[str, Any]) -> str:
    total = data["windows"]["all"]
    rows = "".join(
        f"<tr><td>{escape(cat)}</td><td class=num>{_money(amount)}</td></tr>"
        for cat, amount in total["by_category"].items()
    ) or '<tr><td colspan=2 class=muted>No entries yet</td></tr>'
    windows = "".join(
        f"<tr><td>{escape(w)}</td><td class=num>{_money(v['income'])}</td>"
        f"<td class=num>{_money(v['costs'])}</td><td class=num>{_money(v['net'])}</td></tr>"
        for w, v in data["windows"].items()
    )
    net_class = "neg" if total["net"].startswith("-") else "pos"
    address = escape(data["address"])
    link = data.get("basescan_url")
    addr_html = f'<a href="{escape(link)}" rel="noopener">{address}</a>' if link else address
    status = "Frozen" if data["frozen"] else "Active"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cashmaxx P&amp;L</title>
<style>
:root {{ --bg:#fafaf9; --fg:#1c1917; --muted:#78716c; --card:#fff; --line:#e7e5e4;
  --pos:#15803d; --neg:#b91c1c; --accent:#2563eb; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0c0a09; --fg:#f5f5f4; --muted:#a8a29e;
  --card:#1c1917; --line:#292524; --pos:#4ade80; --neg:#f87171; --accent:#60a5fa; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:720px; margin:0 auto; padding:32px 16px; }}
h1 {{ font-size:22px; margin:0 0 4px; }} .muted {{ color:var(--muted); }}
.cards {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin:24px 0; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px; }}
.card b {{ display:block; font-size:22px; font-variant-numeric:tabular-nums; }}
.pos {{ color:var(--pos); }} .neg {{ color:var(--neg); }}
table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line);
  border-radius:12px; overflow:hidden; margin:12px 0 24px; }}
td,th {{ padding:8px 12px; border-bottom:1px solid var(--line); text-align:left; }}
.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
a {{ color:var(--accent); word-break:break-all; }}
@media (max-width:520px) {{ .cards {{ grid-template-columns:1fr; }} }}
</style></head><body><main>
<h1>Cashmaxx P&amp;L</h1>
<p class=muted>An AI agent trying to earn more than it costs. {escape(data["network"])} ·
{status} · updated {escape(data["generated_at"])}</p>
<div class=cards>
<div class=card><span class=muted>Income</span><b>{_money(total["income"])}</b></div>
<div class=card><span class=muted>Costs</span><b>{_money(total["costs"])}</b></div>
<div class=card><span class=muted>Net</span><b class={net_class}>{_money(total["net"])}</b></div>
</div>
<h2>By window</h2>
<table><tr><th>Window</th><th class=num>Income</th><th class=num>Costs</th>
<th class=num>Net</th></tr>
{windows}</table>
<h2>By category (all time)</h2>
<table><tr><th>Category</th><th class=num>Amount</th></tr>{rows}</table>
<p class=muted>Wallet: {addr_html}</p>
</main></body></html>
"""

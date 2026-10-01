# Cashmaxx website

Static landing page, the live P&L page (`/pnl/`), plus `/privacy/`, `/terms/`, and `/support/` for
https://cashmaxx.neu.so. Styled on the
README cover (white grid, emerald `#059669`, ink `#0b1220`) with light and dark modes. The screenshots in
`public/img` are WebP copies of `images/cashmaxx/webui-*.png`; refresh them when those change.

`/pnl/` reads a guard's `/public/pnl.json` (opt-in `public_pnl`, CORS `*`) straight from the browser:
totals and categories only, with income split into verified (chain/Stripe) vs reported. The guard URL
comes from `?guard=`, localStorage, or a baked `window.CASHMAXX_GUARD_URL` (set it in an inline script
before `app.js` when deploying against a fixed guard). With none of those it shows the empty state.

No build step. Preview with `python3 -m http.server 4193 -d site/public`.

Deploy (Cloudflare Pages project `cashmaxx`, custom domain `cashmaxx.neu.so`):

```bash
cd site && npx wrangler pages deploy public --project-name cashmaxx --branch main
```

This folder is Cashmaxx-only and is not part of upstream nanobot.

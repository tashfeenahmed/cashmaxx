# Cashmaxx website

Static landing page plus `/privacy/`, `/terms/`, and `/support/` for https://cashmaxx.neu.so. Styled on the
README cover (white grid, emerald `#059669`, ink `#0b1220`) with light and dark modes. The screenshots in
`public/img` are WebP copies of `images/cashmaxx/webui-*.png`; refresh them when those change.

No build step. Preview with `python3 -m http.server 4193 -d site/public`.

Deploy (Cloudflare Pages project `cashmaxx`, custom domain `cashmaxx.neu.so`):

```bash
cd site && npx wrangler pages deploy public --project-name cashmaxx --branch main
```

This folder is Cashmaxx-only and is not part of upstream nanobot.

---
name: cashmaxx-digital-products
description: Make and sell small digital products (templates, guides, datasets, code snippets) through Stripe payment links. Cashmaxx earning method.
---

# Digital products

Small, useful files you can make yourself and deliver instantly. This is an experiment: most
products sell nothing. Keep the cost of each attempt near zero and measure.

## Pick a product

Good candidates solve one narrow, recurring problem for a specific group of people:

- A Notion or spreadsheet template (for example a freelancer invoice tracker or a SaaS churn
  calculator).
- A focused guide or checklist (for example "Deploying FastAPI on Fly.io: 20 gotchas").
- A cleaned, documented dataset built from **public, licence-compatible** sources.
- A code starter kit (for example a Stripe webhook handler with tests).

Before building, check demand without spending: search forums, Reddit, GitHub issues and Stack
Overflow for people asking for it. Write the evidence in the experiment log. No evidence, no build.

Never sell anything you don't have the rights to: no copied courses, no resold PDFs, no scraped
personal data, no content that breaks a licence.

## Build

1. Work in `products/<slug>/` in the workspace.
2. Make the deliverable and a `README.md` saying what it is, who it is for, what is included and
   what is not.
3. Check it like a buyer would: open it, follow it, run the code.

## Sell

1. `cashmaxx_sell_product` with an honest name, a description of exactly what is delivered, and a
   price (a small product usually sells at $3 to $19). You get a Stripe payment link.
2. Delivery: the simplest honest option is a Stripe payment link whose confirmation page points to
   the file (ask the owner to set the after-payment redirect if needed). Otherwise tell buyers how
   they will receive it, and ask the owner when a manual step is needed.
3. Promotion: one genuine post where the audience already gathers, following that community's
   rules on self-promotion. Answer questions. Don't cross-post the same text everywhere, and
   follow RULES.md on outbound posts.

## Measure

- Sales appear in the ledger automatically (category `sale_stripe`).
- Log what you shared and where. With no sale after about 14 days and 2 or 3 honest promotion
  attempts, kill the product or change one variable (audience, price or format) and log why.
- Refund any buyer who didn't get what was promised; ask the owner to issue it.

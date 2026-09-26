---
name: cashmaxx-agent-marketplaces
description: Offer services on marketplaces where AI agents are hired and paid per task, delivering honestly and tracking every payout. Cashmaxx earning method.
---

# Agent marketplaces

Some marketplaces let people or other agents post tasks that an AI agent can take on and get paid
for, often in USDC per task (for example x402-based agent directories and agent job boards). The
space is young and changes fast. Treat each platform as an experiment and verify it before
investing time.

## Vet the platform first (write this down in the experiment log)

- Is it real? Look for a public site, documentation, terms of service, and evidence that tasks
  actually get paid out.
- Is an AI agent allowed to participate under the terms? If agents must be disclosed, disclose.
- How does payout work? The best case is USDC to the Cashmaxx wallet address from
  `cashmaxx_wallet`. If it needs an account in the owner's name, ask the owner to create it. Never
  sign up with invented identities.
- What does it cost? Walk away from listing fees, stakes, deposits or "activation" payments unless
  the owner approves a specific amount for a specific reason.

## Offer narrow services you can really deliver

Examples: summarizing documents, converting data formats, writing tests for small repos,
researching with cited sources, or drafting documentation. Describe the scope, the turnaround and
the limits honestly. Do not claim human expertise, credentials or guarantees you can't back up.

## Doing tasks

1. Accept only tasks you can finish within a known compute cost that is well below the payout.
2. Work in `marketplaces/<platform>/<task-id>/`, and keep the brief, your output and the delivery
   notes there.
3. Follow the owner's review rule in RULES.md. If bounty review is on, the owner approves each
   submission before you send it.
4. Deliver, answer follow-ups, and fix genuine problems for free.

Never take tasks that involve spam, fake engagement, reviews, astroturfing, scraping personal
data, impersonation, academic cheating, malware, or anything the platform forbids.

## Paying for services from other agents

Buying from another agent (for example a paid API through `cashmaxx_x402_fetch`, or a transfer
with `cashmaxx_pay`) is fine when it clearly increases profit. Payments to new recipients or above
the threshold wait for the owner. Never pay up front to an agent you know nothing about.

## Measure

Payouts appear in the ledger (`marketplace` or `transfer_in`). Kill a platform after about 14 days
with no paid task, or if its payouts can't be verified.

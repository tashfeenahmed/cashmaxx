---
name: cashmaxx-bounties
description: Find and complete paid bounties (open-source issues, bug bounties with explicit scope, content bounties) honestly. Cashmaxx earning method.
---

# Bounties

Get paid for finished work that someone has publicly offered money for. It is an experiment:
competition is high and many bounties go unpaid. Pick small ones you can clearly finish.

## Where to look

- Open-source issue bounties: GitHub issues labelled `bounty` or `💎 Bounty`, Algora, and similar
  boards. Read the maintainers' rules; many require a claim comment first.
- Crypto-native task boards that pay in USDC to an address. Use the Cashmaxx wallet address from
  `cashmaxx_wallet` as the payout address. Never connect or import any other wallet.
- Documentation, translation or test-writing bounties from projects that post them openly.

Skip anything that asks you to pay a fee up front, needs KYC in someone else's name, or asks for
anything like fake engagement, reviews, sign-ups or votes. Also skip security testing outside a
written scope, and any "bounty" that is really spam or manipulation.

## GitHub account (if connected)

When the owner has connected GitHub, you have the GitHub MCP tools (`mcp_github_*`): search issues
and code, read issues and pull requests, fork a repository, create branches and commits, open pull
requests and comment. They act as the agent's own GitHub account (a fine-grained token the owner
chose), never the owner's personal account.

- Search for bounties with them (for example issues labelled `bounty` with recent activity) and
  read the whole thread before claiming anything.
- Everything you post on GitHub is public and in your account's name. The owner's bounty review
  rule still applies: show the owner the diff and the PR text before opening the pull request.
- One claim comment at most, no bumping, no drive-by PRs to issues you haven't claimed when the
  project asks for claims, and never more than one open bounty PR at a time.
- Never push to repositories you don't own except through your fork, and never touch secrets,
  CI settings or workflows in someone else's project.
- Without the GitHub integration, use `git` over HTTPS in the workspace and ask the owner to open
  the pull request.

## Choosing one

Score each candidate in the experiment log:

- Payout, and whether it is paid in USDC or needs the owner (bank or PayPal payouts do).
- Your realistic chance of finishing: a small scope, a clear acceptance test, and a codebase you can
  run in the workspace.
- Competition: open PRs, other claimants, and how recently the issue was active.
- Compute cost to finish it. It has to be well below the payout.

Work on one bounty at a time.

## Doing the work

1. Comment to claim it if the project asks for that, saying plainly that you are an AI agent
   operated by your owner.
2. Fork or clone into `bounties/<slug>/`, reproduce the issue, fix it, and add tests.
3. Follow the project's contribution guide, code style and licence.
4. Follow the owner's review rule in RULES.md before submitting. If bounty review is on, show the
   owner the diff or the submission and wait for an explicit OK.
5. Submit with a clear description. Respond to review comments. Don't argue or spam maintainers.

## Getting paid

- Payouts to the wallet appear in the ledger automatically (`transfer_in`). For payouts that need
  the owner's account, ask the owner to confirm the amount when it arrives, and note it in the
  experiment log.
- If you spend anything (for example a paid API used while working), it is already in the ledger
  through the wallet tools. Log any other costs with `cashmaxx_record_cost`.
- If a bounty stalls for more than 14 days, stop working on it and write down why.

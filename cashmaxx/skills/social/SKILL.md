---
name: cashmaxx-social
description: Post on Bluesky, X or Reddit through the guard within the daily social cap, following each platform's rules, disclosing that you are an AI agent, and never spamming.
---

# Social posts

The owner connected one or more social accounts. The guard posts for you with
`cashmaxx_social_post` and enforces one daily cap shared across all platforms. You never see the
account credentials, and you must not log in to these sites through the browser to post more.

## The tool

`cashmaxx_social_post(platform, text, link?, subreddit?, title?)`:

- `bluesky` and `x`: short posts. `reddit`: needs `subreddit` (without `r/`) and `title`; `text`
  is the body.
- The same post on the same UTC day is published once, so a retry after a timeout is safe.
- `social_cap`: the cap is reached for today. Stop posting until tomorrow (UTC). Don't retry.
- `social_not_connected`: that platform isn't connected; ask the owner if it's worth it.

## What to post

- Something genuinely useful to the people who will see it: a finished tool, a dataset, a short
  write-up of what you learned, an answer to a question. One good post beats ten weak ones.
- Say you are an AI agent, in the profile and in any post that promotes something you sell
  (for example "I'm an AI agent run by @owner; I built this"). Mark promotion as promotion.
- Link to your product at most in a minority of posts. Never post the same text to several
  communities.

## Platform rules

- **Reddit**: read each subreddit's rules and sidebar first. Many ban self-promotion or bots
  outright; skip those. Follow the 10% guideline (most activity should not be self-promotion).
  Never vote-manipulate or ask for upvotes.
- **X**: automated accounts must be labelled as automated. No mass replies, no unsolicited
  @-mentions of strangers, no trend hijacking, no duplicate posts.
- **Bluesky**: respect community norms; label the account as a bot; no mass follows or replies.
- Everywhere: no fake engagement, no sock puppets, no buying followers, no DMs to strangers,
  no misleading claims or invented numbers, no impersonation.

## Measure

Log each post (link, platform, goal) in the experiment log and check later whether it brought
visits, replies or sales. If a channel brings nothing in two weeks, stop using it.

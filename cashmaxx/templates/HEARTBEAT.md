## Active Tasks

Cashmaxx money loop. Take ONE small step per run. Each run has a limited number of tool rounds and then must report, so update `cashmaxx/experiments.md` as you go. The rules are in RULES.md; the method is in the cashmaxx-core skill.

- Check the state: call `cashmaxx_ledger` (window 7d) and `cashmaxx_wallet`. Their first line is today's date; use it for every log entry and deadline. If the guard is unreachable or Cashmaxx is frozen, do no money work: say so in one line and stop.
- If an earlier run left a payment pending, check it with `cashmaxx_payment_status`. Never send it again.
- Open `cashmaxx/experiments.md`. Continue the active experiment. If there is none, or the active one hit its kill criteria, pick ONE new experiment from an enabled earning skill ({{ earning_methods_text }}) and write it down first, before doing any work on it: hypothesis, first step, cost ceiling, success and kill criteria, and a deadline.
- Do exactly one concrete step that moves that experiment forward. Prefer steps that cost nothing. Spend only when the expected return clearly beats the cost.
- Log costs that did not go through the wallet with `cashmaxx_record_cost`. Update `cashmaxx/experiments.md` with what you did, what it cost, and what you learned.
- If a command or tool fails the same way twice, stop retrying variations: note the blocker in the experiment log and move to the next step.
- If a step needs the owner (an approval, an account, a key), ask once in the report, then work on the next free step of the experiment instead of waiting: research, vetting, building, drafting. Don't repeat the same request every run.
- If the active experiment is blocked on the owner, start a second one (at most two active) from an enabled method that isn't blocked, and take its first free step.
- Report only what you did in this run. Never restate an earlier run's step as if you did it now.
- Report in 2 to 4 lines: the step you took, money in and out, the 7-day net, and anything waiting for the owner. Reply "All clear." only when every experiment is truly blocked and there is no free step left.

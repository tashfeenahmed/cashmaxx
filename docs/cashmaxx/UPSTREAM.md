# Syncing with upstream nanobot

Cashmaxx starts from a snapshot of [HKUDS/nanobot](https://github.com/HKUDS/nanobot) instead of
carrying its full history, so this repository only contains Cashmaxx commits. Because there is no
shared history, `git merge upstream/main` does not work. Apply upstream changes as a diff instead.

**Current base:** `f62e0da9f6ac7a02fdd056bb2c19c58365952956` (nanobot 0.3.5). Update this line on
every sync.

```bash
git remote add upstream https://github.com/HKUDS/nanobot.git   # once; keep pushes disabled:
git remote set-url --push upstream DISABLED
git fetch upstream

BASE=f62e0da9f6ac7a02fdd056bb2c19c58365952956                     # the "Current base" above
NEW=$(git rev-parse upstream/main)
git log --oneline "$BASE..$NEW"                                     # what changed upstream

git switch -c sync/nanobot-${NEW:0:8}
git diff --binary "$BASE" "$NEW" | git apply --3way                 # conflicts show as markers
# resolve conflicts (keep the `# cashmaxx:` hooks), then run the full checks:
pytest -q -n auto --dist loadfile && ruff check nanobot tests conftest.py && basedpyright
(cd webui && bun run test && bun run lint && bun run build)
```

Then update **Current base** above to `$NEW` and commit everything as one commit:

```text
Sync nanobot <version> (HKUDS/nanobot@<NEW first 12 chars>)
```

Keep Cashmaxx changes to upstream files minimal and marked with `# cashmaxx:` so syncs stay easy.
The touch points are listed in [ARCHITECTURE.md](./ARCHITECTURE.md#upstream-touch-points-keep-minimal).

# Git hooks

Local guards for the mistakes that CI cannot see until it is too late. They are
committed to the repo but git ignores them until you point it here — once per
clone, on the HOP:

```bash
deploy/install-hooks.sh
```

(which is just `git config core.hooksPath githooks`, plus a check that it took).

## What runs

| Hook | Refuses | Escape |
|---|---|---|
| `pre-commit` | a direct commit on `main` | `git commit --no-verify` |
| `pre-push` | pushing `main` with a merge that left out already-pushed work | `git push --no-verify` |

Both are escapable on purpose. A guard you cannot get past when you genuinely
need to gets uninstalled, and then it guards nothing.

**Hooks are not the primary defence.** They are per-clone and skippable, so the
same check runs in CI (`merge-freshness` in `.github/workflows/ci.yml`) using
the identical script, `scripts/check_merge_freshness.py`. The hook is there to
catch it while the mistake is still private and `git reset --hard` fixes it
completely; CI is there because the hook might not be installed.

## The failure these were written for

`v0.10.0` was tagged against a tree whose `package.json` still said `0.9.2`.
The release commit had been pushed to `origin` six minutes earlier. The merge
was:

```bash
git pull origin main
git merge --no-ff claude/readme-requirements
```

`git pull origin main` updates `main` and **only** `main`. It moves neither the
local branch being merged nor that branch's remote-tracking ref. So the merge
took a local ref one commit behind `origin`, silently dropped the release
commit, and looked right afterwards — both refs on the machine agreed with each
other, because both were equally stale.

Nothing that reads the resulting tree could catch it: the tree was internally
consistent. Version, lockfile and CHANGELOG all agreed on `0.9.2`, and every CI
job passed, correctly. The discrepancy is only visible by comparing the merge
against the **remote** refs — which is what `check_merge_freshness.py` does —
or, at release time, against the tag, which is the `release` CI job.

## Two things learned building this

**`pre-merge-commit` was the obvious hook and does not work.** On a clean
`git merge --no-ff`, git creates the merge commit directly and never writes
`MERGE_HEAD`, so a hook that reads `MERGE_HEAD` sees nothing and passes
silently. Hence `pre-push`, which inspects the finished merge commit and gets
both parents from it.

**The check must name the branch from the merge message, not infer it.** The
first version asked "which remote branches contain this merge's side parent?"
That fired on the real case — and on two innocent branches that had simply been
forked *from* that commit, since they are descended from it too. A check that
cries wolf on ordinary branching gets switched off within a week.

## Known gap

A **fast-forward** merge creates no merge commit, so there is nothing for
either the hook or CI to inspect. That case is much less damaging — a stale
fast-forward just doesn't advance the branch, rather than producing a merge
commit that claims work it doesn't contain — but it is not covered.

The habit that avoids the whole class is to merge the ref that was actually
pushed, which is what `deploy/merge-to-main.sh` does.

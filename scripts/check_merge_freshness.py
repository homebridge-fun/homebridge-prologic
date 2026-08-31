#!/usr/bin/env python3
"""Detect a merge that took a stale copy of the branch it claims to merge.

This is the v0.10.0 failure, made checkable. The release commit had been pushed
to origin six minutes before the merge, but the merge was run as

    git pull origin main            # updates main, and ONLY main
    git merge --no-ff claude/readme-requirements

and `git pull origin main` moves neither the local branch being merged nor that
branch's remote-tracking ref. The merge took a local ref one commit behind what
had already been pushed, dropped the release commit, and looked correct
afterwards -- both refs on the machine agreed with each other, because both were
equally stale.

Nothing in the resulting tree could reveal it: version, lockfile and CHANGELOG
all agreed on 0.9.2, and CI passed, correctly. The discrepancy is only visible
by comparing the merge against the REMOTE refs, which is what this does.

The check: for each merge commit, take its side parent(s) -- what was merged in
-- and ask whether any remote branch containing that parent has since-pushed
commits the merge left out. That is precisely "you merged branch X, but
origin/X had more".

Usage:
    check_merge_freshness.py                 # merge commits on HEAD not on origin/main
    check_merge_freshness.py <range>         # e.g. origin/main..HEAD, or a single commit
    check_merge_freshness.py --fetch         # refresh remote refs first

Exit 0 clean, 1 if a stale merge is found, 2 on a usage/git error.
"""
from __future__ import annotations

import re
import subprocess
import sys


def git(*args: str) -> str:
    r = subprocess.run(('git',) + args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f'git {" ".join(args)}: {r.stderr.strip()}')
    return r.stdout.strip()


def is_ancestor(a: str, b: str) -> bool:
    return subprocess.run(['git', 'merge-base', '--is-ancestor', a, b],
                          capture_output=True).returncode == 0


def remote_branches(remote: str = 'origin') -> dict[str, str]:
    """{branch name -> sha} for the remote-tracking refs, minus HEAD."""
    out = {}
    listing = git('for-each-ref', '--format=%(refname:short) %(objectname)',
                  f'refs/remotes/{remote}/')
    for line in listing.splitlines():
        name, _, sha = line.partition(' ')
        short = name[len(remote) + 1:]
        if short and short != 'HEAD':
            out[short] = sha
    return out


# Which branch a merge claims to have merged. git writes this itself for both
# `git merge <branch>` and `git merge origin/<branch>`.
#
# The branch has to come from the MESSAGE, not from "which branches contain the
# side parent". The first version of this asked the latter and produced two
# false positives on the real case: branches forked FROM the bad merge point are
# also descended from it, so they looked like stale merges of themselves. A
# check that cries wolf on ordinary branching would be switched off inside a
# week, and then it guards nothing.
_MERGE_SUBJECT = re.compile(
    r"^Merge (?:remote-tracking )?branch '(?:origin/)?([^']+)'", re.I)


def stale_merges(rev_range: str) -> list[str]:
    problems: list[str] = []
    branches = remote_branches()

    merges = git('rev-list', '--merges', rev_range).split()
    for merge in merges:
        subject = git('log', '--format=%s', '-1', merge)
        m = _MERGE_SUBJECT.match(subject)
        if not m:
            # A hand-written merge message: nothing names the branch, so there
            # is nothing to compare against. deploy/merge-to-main.sh covers
            # this path by fetching before it merges.
            continue
        named = m.group(1)
        parents = git('rev-list', '--parents', '-n', '1', merge).split()[1:]
        for side in parents[1:]:          # parent 1 is the branch merged INTO
            for branch, tip in branches.items():
                if branch != named:
                    continue
                if tip == side:
                    continue              # merged exactly what was pushed
                if not is_ancestor(side, tip):
                    continue              # the branch has moved elsewhere
                if is_ancestor(tip, merge):
                    continue              # the merge already contains the tip
                missing = git('log', '--format=  %h %s', f'{side}..{tip}')
                problems.append(
                    f'{git("log", "--format=%h %s", "-1", merge)}\n'
                    f'  merged origin/{branch} at {side[:9]}, but '
                    f'origin/{branch} is now {tip[:9]}.\n'
                    f'  Pushed commits this merge does NOT include:\n'
                    f'{missing}')
    return problems


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != '--fetch']
    if '--fetch' in argv[1:]:
        subprocess.run(['git', 'fetch', '--quiet', '--prune', 'origin'],
                       capture_output=True)

    rev_range = args[0] if args else 'origin/main..HEAD'
    try:
        problems = stale_merges(rev_range)
    except RuntimeError as e:
        print(f'check_merge_freshness: {e}', file=sys.stderr)
        return 2

    if not problems:
        print('merge freshness: ok')
        return 0

    print('\nSTALE MERGE -- this merge left out work that was already pushed:\n',
          file=sys.stderr)
    for p in problems:
        print(p + '\n', file=sys.stderr)
    print('This is how v0.10.0 was tagged against a 0.9.2 tree. Redo the merge\n'
          'against the ref that was actually pushed:\n\n'
          '    git fetch origin\n'
          '    git merge --no-ff --no-edit origin/<branch>\n', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))

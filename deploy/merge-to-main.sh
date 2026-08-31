#!/usr/bin/env bash
# Merge a feature branch into main, safely.
#
# Usage, on the HOP:
#     deploy/merge-to-main.sh claude/some-branch [v0.11.0]
#
# The habit this replaces:
#
#     git pull origin main
#     git merge --no-ff claude/some-branch
#
# which dropped the v0.10.0 release commit. `git pull origin main` updates main
# and only main -- not the branch being merged, and not that branch's
# remote-tracking ref -- so the merge took a local ref one commit behind what
# had already been pushed. See githooks/README.md.
#
# This fetches first and merges origin/<branch>: the ref that was actually
# pushed, which is the ref everyone else can see.
set -euo pipefail

branch="${1:-}"
tag="${2:-}"
if [ -z "$branch" ]; then
    echo "usage: $0 <branch> [tag]" >&2
    exit 2
fi
branch="${branch#origin/}"

cd "$(git rev-parse --show-toplevel)"

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "working tree is dirty -- commit or stash first" >&2
    exit 1
fi

echo "==> fetching"
git fetch --prune origin

git rev-parse -q --verify "refs/remotes/origin/$branch" >/dev/null || {
    echo "origin/$branch does not exist -- has the branch been pushed?" >&2
    exit 1
}

# If a local copy exists and is ahead of origin, there is unpushed work that
# this merge would silently leave out. Fail loudly rather than pick one.
if git rev-parse -q --verify "refs/heads/$branch" >/dev/null; then
    ahead="$(git rev-list --count "origin/$branch..$branch")"
    if [ "$ahead" -gt 0 ]; then
        echo >&2
        echo "local '$branch' is $ahead commit(s) AHEAD of origin/$branch:" >&2
        git log --format='  %h %s' "origin/$branch..$branch" >&2
        echo >&2
        echo "Push them first, or they will not be in this merge:" >&2
        echo "    git push origin $branch" >&2
        exit 1
    fi
fi

echo "==> merging origin/$branch into main"
git checkout main
git merge --ff-only origin/main
git merge --no-ff --no-edit "origin/$branch"

# Belt and braces: the same check CI runs, before anything is pushed. At this
# point a bad merge is still private and `git reset --hard origin/main` undoes
# it completely.
python3 scripts/check_merge_freshness.py "origin/main..HEAD" || {
    echo "main has NOT been pushed. Undo with: git reset --hard origin/main" >&2
    exit 1
}

if [ -n "$tag" ]; then
    version="$(node -p 'require("./package.json").version')"
    if [ "$tag" != "v$version" ]; then
        echo >&2
        echo "REFUSING to tag: $tag, but package.json says $version." >&2
        echo "The release commit is probably not in this merge. main has NOT" >&2
        echo "been pushed; inspect with 'git log --oneline -5' and fix." >&2
        exit 1
    fi
    if ! grep -qE "^##[[:space:]]+$version( |$)" CHANGELOG.md; then
        echo "REFUSING to tag: CHANGELOG.md has no '## $version' heading." >&2
        exit 1
    fi
    git tag "$tag"
fi

echo "==> pushing"
git push -u origin main
[ -n "$tag" ] && git push origin "$tag"

echo
git log --oneline -3
echo
echo "done. main and ${tag:-no tag} pushed."

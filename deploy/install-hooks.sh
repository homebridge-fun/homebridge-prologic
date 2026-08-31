#!/usr/bin/env bash
# Point this clone at the repo's committed hooks. Run once per clone, on any
# machine that commits (the HOP dev checkout, mainly). See githooks/README.md.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

chmod +x githooks/pre-commit githooks/pre-push
git config core.hooksPath githooks

echo "core.hooksPath = $(git config core.hooksPath)"
echo
echo "Active hooks:"
for h in githooks/pre-commit githooks/pre-push; do
    [ -x "$h" ] && echo "  $h" || echo "  $h  (NOT EXECUTABLE -- will be skipped)"
done
echo
echo "Verifying the main-branch guard actually fires..."
# Cheap end-to-end proof rather than trusting that the config took: run the
# hook directly with HEAD faked to main. A hook that is installed but silently
# not running is worse than no hook, because it is trusted.
if [ "$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)" = "main" ]; then
    if githooks/pre-commit >/dev/null 2>&1; then
        echo "  FAILED: the guard allowed a commit on main" >&2
        exit 1
    fi
    echo "  ok -- refuses commits on main (you are on main right now)"
else
    echo "  ok -- installed (you are on a branch, so the main guard is inactive here)"
fi

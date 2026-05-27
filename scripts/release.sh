#!/usr/bin/env bash
# scripts/release.sh — bump version, commit, tag, push. The tag push
# fires .github/workflows/publish.yml which authenticates to PyPI via
# Trusted Publishing (OIDC) — this script never sees a PyPI token.
#
# Usage: scripts/release.sh [patch|minor|major]      # default: patch
#
# Prereqs: clean working tree on `main`. Run tests yourself before
# invoking; this script trusts you.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

LEVEL="${1:-patch}"

# --- guards: a release commit must be reproducible from a clean main ---
[[ -z "$(git status --porcelain)" ]] || {
  echo "✗ working tree is dirty — commit or stash first" >&2
  exit 1
}
BRANCH=$(git rev-parse --abbrev-ref HEAD)
[[ "$BRANCH" == "main" ]] || {
  echo "✗ on branch '$BRANCH' — release from 'main'" >&2
  exit 1
}

# --- bump ---
OLD=$(uv version --short)
uv version --bump "$LEVEL" >/dev/null
NEW=$(uv version --short)

# --- confirm — last bail-out before anything reaches GitHub ---
echo
echo "  $OLD  →  $NEW"
read -rp "Cut release v$NEW? [y/N] " yn
case "${yn,,}" in
  y|yes) ;;
  *)
    git checkout -- pyproject.toml uv.lock
    echo "✗ cancelled — restored to $OLD"
    exit 1
    ;;
esac

# --- commit, tag, push ---
git add pyproject.toml uv.lock
git commit -m "release: v$NEW"
git tag -a "v$NEW" -m "sqrlly $NEW"
git push
git push origin "v$NEW"

echo
echo "✓ pushed v$NEW"
echo "  Publish workflow: https://github.com/christopherseaman/sqrlly/actions"

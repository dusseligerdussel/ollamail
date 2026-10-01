#!/usr/bin/env bash
# Uploads PR screenshots to the shared `pr-screenshots` branch and prints Markdown
# that embeds them in a pull request description (see CLAUDE.md, section 4a).
#
# Usage: scripts/pr-screenshots.sh <directory-with-png-files>
#
# Screenshots never land in `main`: they live on the orphan branch `pr-screenshots`,
# in a folder named after the current feature branch. Images are linked by commit SHA,
# so the links stay stable even when the folder is updated later.
set -euo pipefail

SRC_DIR="${1:?usage: scripts/pr-screenshots.sh <directory-with-png-files>}"
BRANCH="pr-screenshots"
REMOTE="origin"

shopt -s nullglob
files=("$SRC_DIR"/*.png "$SRC_DIR"/*.jpg "$SRC_DIR"/*.jpeg "$SRC_DIR"/*.webp)
if [ ${#files[@]} -eq 0 ]; then
  echo "error: no images found in $SRC_DIR" >&2
  exit 1
fi

current="$(git branch --show-current)"
if [ -z "$current" ] || [ "$current" = "main" ]; then
  echo "error: run this from your feature branch, not from main or a detached HEAD" >&2
  exit 1
fi
slug="$(printf '%s' "$current" | tr '/' '-' | tr -c 'A-Za-z0-9._-' '-')"

repo_url="$(git remote get-url "$REMOTE")"
repo_path="$(printf '%s' "$repo_url" | sed -E 's#^(https?://[^/]+/|git@[^:]+:)##; s#\.git$##; s#^git/##')"
# Proxied remotes may carry extra path segments; keep only owner/repo.
repo_path="$(printf '%s' "$repo_path" | awk -F/ '{print $(NF-1)"/"$NF}')"

work="$(mktemp -d)"
trap 'git worktree remove --force "$work" >/dev/null 2>&1 || rm -rf "$work"' EXIT

if git ls-remote --exit-code --heads "$REMOTE" "$BRANCH" >/dev/null 2>&1; then
  git fetch -q "$REMOTE" "$BRANCH"
  git worktree add -q --detach "$work" FETCH_HEAD
else
  git worktree add -q --detach "$work"
  git -C "$work" checkout -q --orphan "$BRANCH"
  git -C "$work" rm -rq --cached . >/dev/null 2>&1 || true
  find "$work" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
  printf '# PR screenshots\n\nOrphan branch holding screenshots embedded in pull requests. Never merge into main.\n' > "$work/README.md"
fi

mkdir -p "$work/$slug"
for f in "${files[@]}"; do cp "$f" "$work/$slug/"; done
git -C "$work" add -A
if git -C "$work" diff --cached --quiet; then
  echo "note: screenshots unchanged, reusing existing commit" >&2
else
  git -C "$work" commit -q -m "chore(screenshots): update $slug"
fi

for attempt in 1 2 3 4; do
  if git -C "$work" push -q "$REMOTE" "HEAD:refs/heads/$BRANCH" 2>/dev/null; then
    break
  fi
  [ "$attempt" -eq 4 ] && { echo "error: push to $BRANCH failed" >&2; exit 1; }
  # Another agent pushed in between: replay our commit on top (folders never overlap).
  git -C "$work" fetch -q "$REMOTE" "$BRANCH"
  git -C "$work" rebase -q FETCH_HEAD
  sleep $((attempt * 2))
done

sha="$(git -C "$work" rev-parse HEAD)"
echo "## Screenshots"
echo
for f in "${files[@]}"; do
  name="$(basename "$f")"
  label="${name%.*}"
  echo "**${label}**"
  echo
  echo "![${label}](https://github.com/${repo_path}/blob/${sha}/${slug}/${name}?raw=true)"
  echo
done

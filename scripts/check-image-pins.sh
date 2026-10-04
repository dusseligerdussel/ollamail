#!/usr/bin/env bash
# Checks that every copy of the pinned third-party images matches deploy/compose.yaml.
#
# Usage: scripts/check-image-pins.sh [--fix]
#
# Dependabot (docker-compose ecosystem) updates only the defaults in deploy/compose.yaml
# (`${POSTGRES_IMAGE:-…}`, `${OLLAMA_IMAGE:-…}`). The same tags are repeated in files it does
# not touch; this script fails if one of them differs and, with --fix, rewrites them to the
# compose value. Runs in the compose smoke test of .github/workflows/ci.yml.
set -euo pipefail

cd "$(dirname "$0")/.."

FIX=false
case "${1:-}" in
  "") ;;
  --fix) FIX=true ;;
  *)
    echo "usage: scripts/check-image-pins.sh [--fix]" >&2
    exit 2
    ;;
esac

COMPOSE=deploy/compose.yaml
TAG_CHARS='A-Za-z0-9._-'

compose_default() {
  local value
  value="$(sed -n "s/.*\${$1:-\([^}]*\)}.*/\1/p" "$COMPOSE" | head -n 1)"
  if [ -z "$value" ]; then
    echo "error: no default for $1 found in $COMPOSE" >&2
    exit 1
  fi
  printf '%s' "$value"
}

POSTGRES="$(compose_default POSTGRES_IMAGE)"
OLLAMA="$(compose_default OLLAMA_IMAGE)"

failed=false

# check_ref <file> <expected image:tag>: every reference to the image in the file must
# carry the expected tag, and there must be at least one.
check_ref() {
  local file="$1" expected="$2" image found ref
  image="${expected%:*}"
  found="$(grep -o "${image}:[${TAG_CHARS}]*" "$file" || true)"
  if [ -z "$found" ]; then
    echo "error: $file does not reference $image (expected $expected)" >&2
    failed=true
    return
  fi
  while IFS= read -r ref; do
    [ "$ref" = "$expected" ] && continue
    if $FIX; then
      sed -i "s|${image}:[${TAG_CHARS}]*|${expected}|g" "$file"
      echo "fixed: $file: $ref -> $expected"
    else
      echo "error: $file: $ref, expected $expected (from $COMPOSE)" >&2
      failed=true
    fi
  done <<<"$found"
}

# The Helm chart splits the Ollama image into repository and tag (ollama.image).
check_helm_ollama() {
  local file=deploy/helm/ollamail/values.yaml expected_tag="${OLLAMA##*:}" found
  found="$(awk '/repository: ollama\/ollama$/ { getline; gsub(/[ "]|tag:/, ""); print; exit }' "$file")"
  if [ -z "$found" ]; then
    echo "error: $file: no tag after 'repository: ollama/ollama'" >&2
    failed=true
  elif [ "$found" != "$expected_tag" ]; then
    if $FIX; then
      sed -i "/repository: ollama\/ollama$/{n;s|tag: \"[^\"]*\"|tag: \"${expected_tag}\"|}" "$file"
      echo "fixed: $file: ollama.image.tag $found -> $expected_tag"
    else
      echo "error: $file: ollama.image.tag is $found, expected $expected_tag (from $COMPOSE)" >&2
      failed=true
    fi
  fi
}

check_ref deploy/.env.example "$POSTGRES"
check_ref deploy/.env.example "$OLLAMA"
check_ref deploy/helm/ci/postgres.yaml "$POSTGRES"
check_ref .github/workflows/model-evals.yml "$OLLAMA"
check_helm_ollama

if $failed; then
  echo "Run scripts/check-image-pins.sh --fix and commit the result." >&2
  exit 1
fi
echo "Image pins match $COMPOSE: $POSTGRES, $OLLAMA"

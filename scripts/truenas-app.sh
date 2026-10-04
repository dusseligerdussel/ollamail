#!/usr/bin/env bash
# Validates and renders the TrueNAS catalog app in deploy/truenas/app with the tooling of
# truenas/apps (docs/operations/truenas.md, section 8).
#
# Usage: scripts/truenas-app.sh <out-dir> [values-file ...]
#
# Copies the app into a pinned checkout of truenas/apps as ix-dev/community/ollamail, adds the
# library named by lib_version in app.yaml, validates app.yaml, questions.yaml and the file set
# with truenas/apps_validation and renders templates/docker-compose.yaml once per test values
# file (default: all files in templates/test_values). Writes <out-dir>/<values-name>.yaml and
# checks each with `docker compose config`. Nothing is pushed or published.
#
# TRUENAS_APPS_DIR / TRUENAS_APPS_VALIDATION_DIR: existing checkouts instead of fresh clones.
# Runs in the compose smoke test of .github/workflows/ci.yml.
set -euo pipefail

cd "$(dirname "$0")/.."

# Bump together after checking that the library version and the CONTRIBUTIONS.md rules still fit.
APPS_REF=fcb435722d2a50f84ce5457b3288b72b8d170ac5
VALIDATION_REF=d0cb4cbd76b4327c9827cb161234a49ea977a371

if [ "$#" -lt 1 ]; then
  echo "usage: scripts/truenas-app.sh <out-dir> [values-file ...]" >&2
  exit 2
fi
OUT="$(mkdir -p "$1" && cd "$1" && pwd)"
shift
APP_SRC="$PWD/deploy/truenas/app"
if [ "$#" -eq 0 ]; then
  set -- "$APP_SRC"/templates/test_values/*.yaml
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

checkout() {
  local repo="$1" ref="$2" dir="$3"
  git init -q "$dir"
  git -C "$dir" fetch -q --depth 1 "https://github.com/$repo" "$ref"
  git -C "$dir" -c advice.detachedHead=false checkout -q FETCH_HEAD
}

APPS="${TRUENAS_APPS_DIR:-}"
if [ -z "$APPS" ]; then
  APPS="$WORK/apps"
  checkout truenas/apps "$APPS_REF" "$APPS"
fi
VALIDATION="${TRUENAS_APPS_VALIDATION_DIR:-}"
if [ -z "$VALIDATION" ]; then
  VALIDATION="$WORK/apps_validation"
  checkout truenas/apps_validation "$VALIDATION_REF" "$VALIDATION"
fi

# The library modules import bcrypt, docker and pydantic (truenas/apps devbox.json).
python3 -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install --quiet --disable-pip-version-check "$VALIDATION" bcrypt docker pydantic

# apps_validation imports middlewared (TrueNAS itself) only to compare min_scale_version with
# max_scale_version; app.yaml sets no max_scale_version, so a stub is enough.
mkdir -p "$WORK/stubs/middlewared/plugins/update_"
touch "$WORK/stubs/middlewared/__init__.py" "$WORK/stubs/middlewared/plugins/__init__.py" \
  "$WORK/stubs/middlewared/plugins/update_/__init__.py"
cat > "$WORK/stubs/middlewared/plugins/update_/utils.py" <<'STUB'
def can_update(old_version, new_version):
    raise NotImplementedError("stub: set no max_scale_version in app.yaml")
STUB

# Work on a copy: the checkout stays untouched.
CATALOG="$WORK/catalog"
mkdir -p "$CATALOG/ix-dev/community"
cp -r "$APPS/library" "$CATALOG/library"
cp -r "$APP_SRC" "$CATALOG/ix-dev/community/ollamail"

FAKE_ENV=1 PYTHONPATH="$WORK/stubs" "$WORK/venv/bin/python" - "$CATALOG" "$OUT" "$@" <<'PY'
import os
import shutil
import sys

import yaml
from apps_validation.validate_app_version import validate_catalog_item_version
from apps_validation.validate_dev_directory import validate_app
from catalog_reader.app_utils import get_values
from catalog_reader.names import get_base_library_dir_name_from_version
from catalog_templating.render import render_templates

catalog, out, values_files = sys.argv[1], sys.argv[2], sys.argv[3:]
app = os.path.join(catalog, "ix-dev", "community", "ollamail")
meta = yaml.safe_load(open(os.path.join(app, "app.yaml")))

# What `devbox run copy-lib` does: the base library of lib_version, and its hash.
lib_version = meta["lib_version"]
hashes = yaml.safe_load(open(os.path.join(catalog, "library", "hashes.yaml")))
if hashes.get(lib_version) != meta["lib_version_hash"]:
    sys.exit(f"app.yaml: lib_version_hash does not match library/hashes.yaml for {lib_version}")
shutil.copytree(
    os.path.join(catalog, "library", lib_version),
    os.path.join(app, "templates", "library", get_base_library_dir_name_from_version(lib_version)),
)

validate_app(app, "dev.community.ollamail", "community")
print("validated: app.yaml, questions.yaml, files")

defaults = get_values(os.path.join(app, "ix_values.yaml"))
for values_file in values_files:
    rendered = render_templates(app, get_values(values_file) | defaults)["docker-compose.yaml"]
    name = os.path.basename(values_file)
    with open(os.path.join(out, name), "w") as f:
        f.write(rendered.strip() + "\n")
    print(f"rendered: {name}")
PY

for values_file in "$@"; do
  docker compose -p ollamail-truenas-check -f "$OUT/$(basename "$values_file")" config --quiet
  echo "compose config ok: $(basename "$values_file")"
done

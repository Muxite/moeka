#!/usr/bin/env bash
# Run an accepted held-out suite (tests/heldout/<feature>) against a throwaway copy of a ref.
# Held-out suites start processes and write files in the tree they run from, so never run
# them in a live checkout. Usage: scripts/run-heldout.sh <feature> [ref]
set -euo pipefail
feat=${1:?feature dir under tests/heldout}; ref=${2:-HEAD}
root=$(git rev-parse --show-toplevel)
copy=$(mktemp -d "${TMPDIR:-/tmp}/moeka-heldout-XXXXXX"); run=$(mktemp -d "${TMPDIR:-/tmp}/moeka-heldout-run-XXXXXX")
trap 'rm -rf -- "$copy" "$run"' EXIT
git -C "$root" archive "$ref" | tar -x -C "$copy"
uv sync --project "$copy" --extra dev -q
cd "$run"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$copy" timeout 5400 uv run --project "$copy" --extra dev \
  pytest "$copy/tests/heldout/$feat" -q -p no:cacheprovider

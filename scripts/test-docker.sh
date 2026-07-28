#!/usr/bin/env bash
# Build the moeka test image and run the test suite inside Docker.
# Extra args replace the default command, e.g.:
#   scripts/test-docker.sh pytest tests/agent/test_vec_store.py -v
#   scripts/test-docker.sh ruff check nanobot --select F
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
docker build -f Dockerfile.test -t moeka-test .
exec docker run --rm -t moeka-test "$@"

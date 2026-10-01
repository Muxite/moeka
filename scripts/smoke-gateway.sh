#!/usr/bin/env bash
# Offline gateway smoke test in a throwaway container: `moeka` import + import boundary,
# `nanobot gateway` health endpoint, one mock chat turn through the CLI agent and the
# HTTP API server. Needs the test image (scripts/test-docker.sh builds it); touches no
# real config, keys or ports of a live install. Env: MOEKA_TEST_IMAGE (default moeka-test).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec timeout 600 docker run --rm \
  -v "$PWD:/app:ro" -w /app "${MOEKA_TEST_IMAGE:-moeka-test}" \
  bash -c 'mkdir -p /tmp/smoke && cp /app/scripts/smoke/* /tmp/smoke/ && timeout 400 bash /tmp/smoke/smoke.sh'

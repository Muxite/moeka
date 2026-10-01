#!/usr/bin/env bash
# Build the moeka test image and run the test suite inside Docker.
# Env: MOEKA_TEST_IMAGE (image tag, default moeka-test), MOEKA_TEST_TIMEOUT (seconds, default 3000).
# Extra args replace the default command, e.g.:
#   scripts/test-docker.sh pytest tests/agent/test_vec_store.py -v
#   scripts/test-docker.sh ruff check nanobot --select F
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
IMAGE="${MOEKA_TEST_IMAGE:-moeka-test}"
# Wall-clock cap for the whole run (seconds); per-test caps live in pyproject.toml.
LIMIT="${MOEKA_TEST_TIMEOUT:-3000}"
NAME="moeka-test-$$"
docker build -f Dockerfile.test -t "$IMAGE" .
# --add-host: this sandbox has no outbound DNS/network egress at all, but a
# handful of tests (SSRF-guard behavior in nanobot/channels/dingtalk/tests/
# test_dingtalk_channel.py and tests/tools/test_tool_validation.py) only need
# example.com/example.org to *resolve* -- validate_url_target()'s DNS check,
# not an actual network round-trip; every real HTTP call in those tests goes
# through a fake in-test HTTP client. Baking a fixed hosts(5) entry into
# Dockerfile.test itself does not work: Docker regenerates each container's
# /etc/hosts at `docker run` time regardless of what a build-time RUN wrote
# there. --add-host is the mechanism Docker actually supports for this, so
# it lives here instead. The addresses are TEST-NET-3 (RFC 5737, permanently
# reserved for documentation/testing, never routable) -- not in
# nanobot/security/network.py's _BLOCKED_NETWORKS list, so the SSRF guard
# correctly treats them as ordinary public addresses, exactly like a real
# public hostname would resolve.
# Not `exec`: on a wall-clock timeout the named container is killed explicitly,
# since killing the docker client alone would leave the tests running.
trap 'docker kill "$NAME" >/dev/null 2>&1 || true' INT TERM
set +e
timeout --signal=TERM "$LIMIT" docker run --rm -t --name "$NAME" \
  --add-host example.com:203.0.113.10 \
  --add-host example.org:203.0.113.11 \
  "$IMAGE" "$@"
status=$?
if [ "$status" -eq 124 ]; then
  echo "test-docker.sh: timed out after ${LIMIT}s; killing $NAME" >&2
  docker kill "$NAME" >/dev/null 2>&1 || true
fi
exit "$status"

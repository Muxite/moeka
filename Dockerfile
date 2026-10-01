# syntax=docker/dockerfile:1.7
# moeka gateway image (spec 005): one instance per container, state on /data.
#
#   docker build -t moeka .
#   docker run --rm -e MOEKA_TOKEN_ISSUE_SECRET=... -p 127.0.0.1:18790:18790 \
#       -v moeka-data:/data moeka
#
# Runs as UID/GID 1000. The instance root is /data/ws (config, memory, skills) and
# its sessions live in /data/ws-sessions. No secret is baked in: provider keys and
# bot tokens come from the environment (compose.yaml `env_file`). The entrypoint
# refuses to start without MOEKA_TOKEN_ISSUE_SECRET, because the gateway and the
# WebSocket channel listen on 0.0.0.0 inside the container.
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    NANOBOT_SKIP_WEBUI_BUILD=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Dependencies first (cached on the manifests only). EXTRAS: e.g. "vec".
ARG EXTRAS=""
COPY pyproject.toml uv.lock README.md LICENSE THIRD_PARTY_NOTICES.md hatch_build.py ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project ${EXTRAS:+--extra ${EXTRAS}}

COPY nanobot/ nanobot/
COPY moeka/ moeka/
COPY templates/ templates/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable ${EXTRAS:+--extra ${EXTRAS}}

COPY scripts/container-entrypoint.sh /usr/local/bin/moeka-entrypoint
RUN chmod 0755 /usr/local/bin/moeka-entrypoint \
    && groupadd -g 1000 moeka \
    && useradd -u 1000 -g 1000 -m -d /home/moeka -s /usr/sbin/nologin moeka \
    && mkdir -p /data \
    && chown 1000:1000 /data

ENV HOME=/home/moeka \
    MOEKA_WORKSPACE=/data/ws \
    MOEKA_TEMPLATE_DIR=/app/templates/workspace
VOLUME ["/data"]
EXPOSE 18790 8765
USER 1000:1000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:18790/health', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["/usr/local/bin/moeka-entrypoint"]
CMD ["gateway", "--config", "/data/ws/config.json", "--workspace", "/data/ws"]

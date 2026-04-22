# syntax=docker/dockerfile:1.7
#
# Moeka image — layered for maximum cache reuse across rebuilds.
#
# Layers, coldest (top) to hottest (bottom):
#
#   1. FROM ........................ base image (changes: ~once per uv release)
#   2. apt-get install ............. system deps + node 20 (changes: ~rare)
#   3. useradd + setcap nsenter .... user + caps (changes: ~never)
#   4. uv pip install .............. Python deps (changes: on pyproject.toml)
#   5. npm install bridge .......... node deps (changes: on bridge/package.json)
#   6. COPY nanobot/ + bridge/ ..... source (changes: every code commit)
#   7. register package + tsc ...... quick rebuild step
#
# A source-only change (the common case) rebuilds only layers 6-7.
# BuildKit cache mounts keep apt/uv/npm downloads warm even when a dep
# layer needs to rebuild.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# ---- 2. System deps -------------------------------------------------------
# Drop apt's auto-clean hook so cache mounts actually survive between builds.
RUN rm -f /etc/apt/apt.conf.d/docker-clean

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg git jq \
        bubblewrap openssh-client util-linux libcap2-bin && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get purge -y gnupg && apt-get autoremove -y

# ---- 3. Runtime user + nsenter caps (layer never changes) -----------------
# Matching host uid 1000 keeps bind-mounted files owned correctly outside
# the container. File caps on nsenter let the non-root user enter host
# namespaces when MOEKA_EXEC_ON_HOST=1 (see docker-compose.yml cap_add).
# Git URL rewrites let npm fetch git-hosted packages over HTTPS without SSH
# keys — harmless if no such deps exist.
RUN useradd -m -u 1000 -s /bin/bash nanobot && \
    mkdir -p /home/nanobot/.nanobot && \
    setcap cap_sys_admin,cap_sys_ptrace,cap_sys_chroot+ep /usr/bin/nsenter && \
    git config --system --add url."https://github.com/".insteadOf ssh://git@github.com/ && \
    git config --system --add url."https://github.com/".insteadOf git@github.com:

WORKDIR /app

# ---- 4. Python deps (cached while pyproject.toml unchanged) ---------------
# Copy *only* pyproject.toml + LICENSE. README.md is referenced by pyproject
# metadata (`readme = {file = …}`), so we stub it with an empty placeholder —
# this keeps README.md edits from invalidating the (slow) dep install.
# A minimal `nanobot/__init__.py` makes the project importable so uv can
# resolve it.
COPY pyproject.toml LICENSE ./
# Stub out package roots so hatchling's wheel builder is happy. pyproject
# declares `packages = ["nanobot"]` and force-includes `bridge/`, so both
# paths must exist — contents don't matter, they're overlaid in layer 6.
RUN mkdir -p nanobot bridge && \
    touch nanobot/__init__.py bridge/.stub README.md

RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system ".[discord,api]"

# ---- 5. Node deps (cached while bridge/package.json unchanged) ------------
COPY bridge/package.json ./bridge/
RUN --mount=type=cache,target=/root/.npm \
    cd bridge && npm install --no-audit --no-fund --loglevel=error

# ---- 6. Source (hot path — rebuilds on every code change) -----------------
# Using --chown here avoids a later `chown -R /app`, which would have to
# rewrite every file on every source change.
COPY --chown=nanobot:nanobot nanobot/ ./nanobot/
COPY --chown=nanobot:nanobot bridge/ ./bridge/
COPY --chown=nanobot:nanobot README.md ./

# ---- 7. Re-register package + compile bridge (fast) -----------------------
# --no-deps + --reinstall tells uv to swap in the real source without
# re-resolving or re-downloading any wheel from PyPI.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system --no-deps --reinstall .

# tsc → bridge/dist; node_modules from layer 5 is still intact because the
# COPY above only adds files the host has (node_modules is dockerignored).
RUN cd bridge && npm run build

# One-shot ownership fixup for files produced above as root (bridge/dist,
# nanobot.egg-info). Fast because it's already a narrow subtree.
RUN chown -R nanobot:nanobot /app /home/nanobot

# ---- Entrypoint (tiny, stays near the bottom) -----------------------------
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && \
    chmod +x /usr/local/bin/entrypoint.sh

USER nanobot
ENV HOME=/home/nanobot
WORKDIR /home/nanobot

EXPOSE 18790
ENTRYPOINT ["entrypoint.sh"]
CMD ["status"]

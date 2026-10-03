# Moeka conversion plan: Docker -> UV + venv

> **STATUS: DONE (historical).** The migration below made the host venv the live
> setup: `bin/moeka.sh` is venv-based, and the systemd units (`scripts/moeka.service`,
> the template `scripts/moeka@.service`) and the `./bin/moeka.sh enable/disable` flow
> are what runs the live bot. This document is kept as the record of the migration
> design and rollback commits, not as an active plan; for current setup see
> [`README.md`](../README.md), [`docs/SYSTEMD.md`](SYSTEMD.md) and
> [`docs/deployment.md`](deployment.md).
>
> Repository facts as of 2026-10-03 (spec 010 FR-005), which differ from what this
> plan deleted:
>
> - a `Dockerfile` and a `compose.yaml` exist again (spec 005 FR-058, commit
>   `d9287a2e`): an optional container image for running one gateway instance per
>   container with its state on a `/data` volume, entrypoint
>   `scripts/container-entrypoint.sh`. It is not the old Docker mode: no nsenter
>   host bridge, no capability escalation, and the live bot does not use it;
> - `Dockerfile.test` runs the test suite in isolation (`scripts/test-docker.sh`);
> - there is still no `docker-compose.yml` and no root `entrypoint.sh`;
> - `tools.exec.allowSudo` still defaults to false (section 4).

## Why

Moeka is being repositioned as **"the nanobot for server management"** — a native agent for managing homelabs, Docker infrastructure, and Linux servers. Running moeka *inside* Docker to manage Docker on the host created unnecessary complexity (nsenter host-bridge, capability escalation, PID namespace sharing). Moeka should run directly on the host where it has natural access to everything it needs to manage.

## What changes

### 1. Remove Docker entirely

Delete:
- `Dockerfile`
- `docker-compose.yml`
- `entrypoint.sh`
- `.dockerized` flag references

Remove from code:
- `_host_bridge_enabled()` and nsenter logic in `nanobot/agent/tools/shell.py`
- `_apply_contained_mode()` in `nanobot/config/loader.py`
- Docker mode detection and docker-compose calls in `moeka.sh`

### 2. UV + venv as the only runtime

`moeka.sh` becomes venv-only:
- `install` -> `uv venv .venv && uv pip install -e .` (falls back to plain pip if uv absent)
- `start` -> `exec .venv/bin/nanobot gateway --config <path>`
- `stop` / `restart` / `status` / `logs` -> systemd or pkill
- `shell` -> activate the venv
- `doctor` -> check uv, python, venv, config, keys, and systemd status

### 3. pyproject.toml

- Rename project from `nanobot-ai` to `moeka`
- Keep all deps, build system, and tooling as-is

### 4. Configurable sudo (default disabled)

Implemented as a direct Nanobot-style exec policy:
- Controlled by `tools.exec.allowSudo` in config.json
- Disabled by default; commands containing `sudo` return one concise policy error
- When enabled, sudo commands run directly through the normal exec safety guards
- Host sudo policy is managed outside `moeka.sh`

### 5. Easy boot setup

- `./bin/moeka.sh enable` -> copies moeka.service, enables + starts via systemd
- `./bin/moeka.sh disable` -> stops + disables the service
- `scripts/install-service.sh` remains as a standalone alternative
- `loginctl enable-linger` documented for headless servers

### 6. Documentation rewrite

All docs updated to reflect:
- Native UV/venv runtime (no Docker)
- "Nanobot for server management" positioning
- Two permission tiers: non-sudo (default) and sudo (opt-in)
- Simplified quick start

## New architecture

```
User installs moeka:
  git clone ... && cd moeka
  cp keys.env.example keys.env && $EDITOR keys.env
  ./bin/moeka.sh install          # uv venv + uv pip install -e .
  ./bin/moeka.sh start            # exec nanobot gateway

Boot setup:
  ./bin/moeka.sh enable           # systemd user service + linger

Permissions:
  Non-sudo (default)          # runs as current user, no sudo
  Sudo (opt-in)               # config flag + host sudo policy
```

## Rollback

This plan requires reverting the Docker-related changes from commits:
- `59f93b0` (safe moeka features)
- `105c1c5` (super dangerous moeka)
- `c9cc583` (dockerized)
- `b9f2e91` (feat(docker): promote workspace to standalone path)

Rather than git-reverting (which risks merge conflicts), changes are applied as direct edits to the current codebase, resulting in a clean commit.

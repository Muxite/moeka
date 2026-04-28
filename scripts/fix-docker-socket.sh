#!/usr/bin/env bash
# Fix Docker socket access for the current user.
#
# Adds the running user to the `docker` group (persistent across reboots)
# and installs a udev rule so /var/run/docker.sock stays group-writable even
# after the Docker daemon restarts.
#
# Run once as root or via sudo:
#   sudo bash scripts/fix-docker-socket.sh
#
# After the group change you must log out and back in (or run
# `newgrp docker` in your current shell) for membership to take effect.

set -euo pipefail

DOCKER_SOCKET=/var/run/docker.sock
DOCKER_GROUP=docker
TARGET_USER="${SUDO_USER:-$USER}"
UDEV_RULE=/etc/udev/rules.d/99-docker-socket.rules

_need_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "error: must be run as root (use: sudo $0)" >&2
        exit 1
    fi
}
_need_root

echo "[fix-docker] adding '${TARGET_USER}' to group '${DOCKER_GROUP}'"
if ! getent group "$DOCKER_GROUP" >/dev/null 2>&1; then
    groupadd "$DOCKER_GROUP"
    echo "[fix-docker] created group '${DOCKER_GROUP}'"
fi
usermod -aG "$DOCKER_GROUP" "$TARGET_USER"

echo "[fix-docker] installing udev rule → ${UDEV_RULE}"
cat > "$UDEV_RULE" <<'EOF'
SUBSYSTEM=="unix", GROUP="docker", KERNEL=="docker.sock", MODE="0660"
EOF
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger              2>/dev/null || true

if [[ -S "$DOCKER_SOCKET" ]]; then
    chown root:"$DOCKER_GROUP" "$DOCKER_SOCKET"
    chmod 660 "$DOCKER_SOCKET"
    echo "[fix-docker] socket permissions set: $(stat -c '%A %G' "$DOCKER_SOCKET")"
fi

echo "[fix-docker] done — log out and back in (or run 'newgrp docker') for the group to activate"

# Running Moeka as a systemd user service

## Install / enable

```sh
./bin/moeka.sh enable
```

This copies `moeka.service` to `~/.config/systemd/user/`, disables the legacy
`nanobot.service` if present, enables user lingering (required for boot
autostart on headless systems — without it, systemd tears down the user
manager at logout and moeka does not come up on the next boot), and starts
Moeka immediately. The linger step uses `sudo loginctl enable-linger $USER`;
if `sudo` is unavailable, the installer prints the exact command to run.

Alternatively, run the install script directly:

```sh
bash scripts/install-service.sh
```

### Upgrading from an older layout (one-time)

The launcher moved from the repo root (`moeka.sh`) to `bin/moeka.sh`, and the
unit's `ExecStart` was updated to match. If you have a unit installed from
before this change, refresh it once so systemd points at the new path:

```sh
./bin/moeka.sh enable    # re-copies moeka.service and restarts the service
```

Until you do this, `systemctl --user restart moeka` will fail because the old
`ExecStart` still references the removed root `moeka.sh`.

## Disable

```sh
./bin/moeka.sh disable
```

## Common commands

```sh
# Status / logs
systemctl --user status moeka
journalctl --user -u moeka -f

# Lifecycle
systemctl --user start moeka
systemctl --user stop moeka
systemctl --user restart moeka

# Auto-start on boot (keeps service running after logout):
loginctl enable-linger "$USER"
```

## Self-restart from inside Moeka

```sh
~/projects/moeka/scripts/restart-nanobot.sh
```

The script targets `moeka.service` when available and falls back to the legacy
`nanobot.service` otherwise.

## Flags & overrides

`moeka.sh` accepts flags before the command:

| Flag               | Effect                                                       |
|--------------------|--------------------------------------------------------------|
| `--config PATH`    | Override the config.json path                                |
| `--workspace PATH` | Override `MOEKA_WORKSPACE` (instance dir, default `~/.nanobot`) |

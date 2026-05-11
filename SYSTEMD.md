# Running Moeka as a systemd user service

## Install / enable

```sh
./moeka.sh enable
```

This copies `moeka.service` to `~/.config/systemd/user/`, disables the legacy
`nanobot.service` if present, enables user lingering (required for boot
autostart on headless systems — without it, systemd tears down the user
manager at logout and moeka does not come up on the next boot), and starts
Moeka immediately. The linger step uses `sudo loginctl enable-linger $USER`;
if `sudo` is unavailable, the installer prints the exact command to run.

Alternatively, run the install script directly:

```sh
bash install-service.sh
```

## Disable

```sh
./moeka.sh disable
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
~/projects/moeka/restart-nanobot.sh
```

The script targets `moeka.service` when available and falls back to the legacy
`nanobot.service` otherwise.

## Flags & overrides

`moeka.sh` accepts flags before the command:

| Flag               | Effect                                                       |
|--------------------|--------------------------------------------------------------|
| `--config PATH`    | Override the config.json path                                |
| `--workspace PATH` | Override `MOEKA_WORKSPACE` (instance dir, default `~/.nanobot`) |

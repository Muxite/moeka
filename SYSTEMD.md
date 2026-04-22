# Running Moeka as a systemd user service

The preferred unit is `moeka.service`. It invokes `./moeka.sh start`, which
auto-detects direct vs. docker mode based on your environment.

## Install / enable

```sh
bash install-service.sh
```

That copies `moeka.service` to `~/.config/systemd/user/`, disables the legacy
`nanobot.service` if present, and starts Moeka immediately.

## Common commands

```sh
# Status / logs
systemctl --user status moeka
journalctl --user -u moeka -f

# Lifecycle
systemctl --user start moeka
systemctl --user stop moeka
systemctl --user restart moeka

# Auto-start on boot (needs lingering if you log out):
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

| Flag              | Effect                                                    |
|-------------------|-----------------------------------------------------------|
| `--docker`        | Force docker-compose mode                                 |
| `--direct`        | Force host-venv mode                                      |
| `--config PATH`   | Override the config.json path                             |
| `--state PATH`    | Override `MOEKA_STATE` (state dir, default `~/.nanobot`)  |

Example — switching the systemd unit to docker mode:

```sh
systemctl --user edit moeka
# then add:
#   [Service]
#   ExecStart=
#   ExecStart=%h/projects/moeka/moeka.sh --docker start
```

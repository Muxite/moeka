#!/usr/bin/env python3
"""Interactive Telegram bot pairing for moeka.

1. Prompt for bot token (or accept via TELEGRAM_TOKEN env / first argv).
2. Verify token via getMe.
3. Write TELEGRAM_TOKEN to keys.env.
4. Poll getUpdates briefly; first incoming message's from.id is appended to
   config.json -> channels.telegram.allowFrom, and telegram is enabled.

Stdlib only.

Usage: telegram_pair.py KEYS_ENV CONFIG_JSON [TOKEN]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


API = "https://api.telegram.org/bot{token}/{method}"


def call(token: str, method: str, **params) -> dict:
    url = API.format(token=token, method=method)
    data = urllib.parse.urlencode(params).encode() if params else None
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=35) as r:
        return json.loads(r.read())


def write_keys_env(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text().splitlines() if path.exists() else []
    out, replaced = [], False
    for ln in lines:
        if ln.lstrip().startswith("TELEGRAM_TOKEN="):
            out.append(f"TELEGRAM_TOKEN={token}")
            replaced = True
        else:
            out.append(ln)
    if not replaced:
        out.append(f"TELEGRAM_TOKEN={token}")
    path.write_text("\n".join(out) + "\n")


def update_config(path: Path, user_id: int) -> None:
    cfg = json.loads(path.read_text())
    tg = cfg.setdefault("channels", {}).setdefault("telegram", {})
    tg["enabled"] = True
    tg.setdefault("token", "${TELEGRAM_TOKEN}")
    allow = tg.setdefault("allowFrom", [])
    sid = str(user_id)
    if sid not in allow:
        allow.append(sid)
    path.write_text(json.dumps(cfg, indent=2) + "\n")


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: telegram_pair.py KEYS_ENV CONFIG_JSON [TOKEN]", file=sys.stderr)
        return 2
    keys_env = Path(argv[1])
    config_json = Path(argv[2])
    token = argv[3] if len(argv) > 3 else ""
    if not token:
        token = input("Telegram bot token (from @BotFather): ").strip()
    if not token:
        print("no token provided", file=sys.stderr)
        return 2

    try:
        info = call(token, "getMe")
    except urllib.error.URLError as e:
        print(f"could not reach Telegram API: {e}", file=sys.stderr)
        return 1
    if not info.get("ok"):
        print(f"invalid token: {info.get('description', 'unknown error')}", file=sys.stderr)
        return 1
    bot = info["result"]
    print(f"bot OK: @{bot.get('username')} (id={bot.get('id')})")

    write_keys_env(keys_env, token)
    print(f"saved TELEGRAM_TOKEN to {keys_env}")

    print("\nNow open Telegram and send ANY message to your bot.")
    print("Waiting up to 120s for the first message...")

    # Clear any backlog so we capture a fresh send.
    try:
        baseline = call(token, "getUpdates", timeout=0)
        max_id = 0
        for u in baseline.get("result", []):
            max_id = max(max_id, u.get("update_id", 0))
        offset = max_id + 1 if max_id else 0
    except Exception:
        offset = 0

    deadline = time.time() + 120
    user_id = None
    while time.time() < deadline:
        try:
            resp = call(token, "getUpdates", offset=offset, timeout=20)
        except urllib.error.URLError:
            time.sleep(1)
            continue
        for u in resp.get("result", []):
            offset = u["update_id"] + 1
            msg = u.get("message") or u.get("edited_message") or u.get("channel_post")
            if not msg:
                continue
            frm = msg.get("from") or {}
            user_id = frm.get("id")
            if user_id:
                print(f"got message from {frm.get('username') or frm.get('first_name')} (id={user_id})")
                break
        if user_id:
            break

    if not user_id:
        print("timed out waiting for a message. Re-run telegram-pair after sending one.", file=sys.stderr)
        return 1

    update_config(config_json, user_id)
    print(f"added {user_id} to channels.telegram.allowFrom in {config_json}")
    print("telegram channel enabled")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

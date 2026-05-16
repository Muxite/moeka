#!/usr/bin/env python3
"""Idempotently set KEY=VALUE lines in a keys.env file.

Usage: edit_keys_env.py PATH KEY VALUE
"""
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: edit_keys_env.py PATH KEY VALUE", file=sys.stderr)
        return 2
    path, key, value = Path(argv[1]), argv[2], argv[3]
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text().splitlines() if path.exists() else []
    out, replaced = [], False
    for ln in lines:
        stripped = ln.lstrip()
        if stripped.startswith(f"{key}="):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(ln)
    if not replaced:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n")
    print(f"wrote {key} to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

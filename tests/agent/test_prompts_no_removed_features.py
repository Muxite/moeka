"""Prompt templates and built-in skills must not describe removed features.

The slim core no longer ships the cron/scheduler, the ``message`` (channel
delivery) tool, the heartbeat, CLI apps, chat channels, the gateway, the WebUI,
pairing/trigger commands or the one-shot ``nanobot`` CLI commands other than
``agent``/``status``/``sessions``/``provider``. Telling the model about them
invites calls to tools that do not exist.

The phrases are deliberately specific (regex, case-insensitive) so that
ordinary English is not flagged. Allowed exceptions are documented inline.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOTS = (REPO / "nanobot" / "templates", REPO / "nanobot" / "skills")
TEXT_SUFFIXES = {".md", ".txt", ".sh", ".json", ".yaml", ".yml", ".j2", ".jinja"}

FORBIDDEN = {
    # scheduler / heartbeat
    "cron": r"\bcron\b",
    "heartbeat": r"heartbeat",
    "scheduled reminder": r"scheduled reminders?",
    "notification gate": r"notification gate|evaluate_notification",
    # message (channel delivery) tool
    "message tool": r"message`? tool|`message`|\bmessage\(",
    "send_message": r"\bsend_message\b",
    # CLI apps
    "cli apps": r"cli_apps?\b|run_cli_app|CLI App",
    # removed commands / surfaces
    "/pairing": r"/pairing",
    "/trigger": r"/trigger\b",
    "webui": r"web-?ui",
    "gateway": r"\bgateway\b",
    "removed nanobot CLI commands": r"nanobot (onboard|gateway|serve|cron|webui|channels)\b",
    # chat channels (names of removed integrations)
    "channel names": (
        r"\b(telegram|discord|slack|feishu|lark|matrix|whatsapp|wechat|weixin|wecom|"
        r"dingtalk|mochat|msteams|qq|sms)\b"
    ),
    # "channel" in the chat-integration sense; skill text about unrelated
    # channels (none today) would need an entry in ALLOWED below.
    "channel": r"\bchannels?\b",
}

# (relative path, forbidden key) pairs that are legitimate. Keep empty unless
# a hit is ordinary English unrelated to a removed feature; explain each one.
ALLOWED: set[tuple[str, str]] = {
    # ``channel`` is the Jinja variable naming the session origin (only ``cli``
    # gets a format hint now); it is not a chat-integration reference.
    ("nanobot/templates/agent/identity.md", "channel"),
    # The ``my`` skill lists "channel" among the request-routing metadata that
    # the runtime context still carries (ToolContext.channel); the metadata
    # remains even though the chat integrations are gone.
    ("nanobot/skills/my/SKILL.md", "channel"),
}


def _text_files():
    for root in ROOTS:
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in TEXT_SUFFIXES and "__pycache__" not in path.parts:
                yield path


def test_prompts_and_skills_do_not_mention_removed_features():
    hits = []
    for path in _text_files():
        rel = path.relative_to(REPO).as_posix()
        text = path.read_text(encoding="utf-8")
        for key, pattern in FORBIDDEN.items():
            if (rel, key) in ALLOWED:
                continue
            for m in re.finditer(pattern, text, flags=re.IGNORECASE):
                line = text.count("\n", 0, m.start()) + 1
                hits.append(f"{rel}:{line}: [{key}] {m.group(0)!r}")
    assert hits == [], "removed features still mentioned:\n" + "\n".join(hits)


def test_scan_covers_templates_and_skills():
    files = {p.name for p in _text_files()}
    assert {"identity.md", "tool_contract.md", "AGENTS.md"} <= files
    assert "SKILL.md" in files


# Python modules that hold model-facing prompt or tool-result text.
PYTHON_PROMPT_SOURCES = (
    "nanobot/agent/context.py",
    "nanobot/agent/tools/ask.py",
    "nanobot/agent/tools/mcp.py",
    "nanobot/utils/artifacts.py",
)


def test_python_prompt_strings_do_not_reference_message_tool():
    pattern = re.compile(r"message`? tool|`message`|'message' tool", re.IGNORECASE)
    hits = [
        f"{rel}: {m.group(0)!r}"
        for rel in PYTHON_PROMPT_SOURCES
        for m in pattern.finditer((REPO / rel).read_text(encoding="utf-8"))
    ]
    assert hits == []

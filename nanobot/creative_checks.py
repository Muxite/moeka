"""Host-side checks on a file the creative bridge is about to deliver (spec 014, self-check gate).

Wonhee's delivery text used to say "Full file check: not yet run." and a re-render that changed
nothing was reported as a fix. These pure helpers let the bridge measure what it can, compare the
file with earlier deliveries, and say plainly what was and was not checked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nanobot.utils import media_probe

# Shown to the agent with every project context (a few tokens per turn).
EVIDENCE_RULES = (
    "Say a change worked only if recent_deliveries shows a NEW file (different sha, no duplicate_of) "
    "and you checked it with view_media against what the creator asked. If duplicate_of is set, "
    "tell the creator nothing changed. Report: what ran, ids and paths, what you checked, what you "
    "did not, and the next step. Never say 'fixed' about a file you have not viewed."
)

_STALE_CHECK = "Full file check: not yet run."


async def facts_for(path: Path) -> dict[str, Any] | None:
    """Measured facts for a video (resolution, duration, audio loudness); ``None`` if unavailable."""
    if media_probe.media_kind(path) != "video" or not media_probe.tools_available():
        return None
    facts = await media_probe.probe(path)
    if facts is None:
        return None
    out: dict[str, Any] = {
        "duration_s": round(facts.duration_s, 1) if facts.duration_s else None,
        "width": facts.width, "height": facts.height, "fps": facts.fps, "has_audio": facts.has_audio,
    }
    if facts.has_audio:
        loud = await media_probe.loudness(path)
        if loud:
            out["loudness"] = loud
    return out


def find_duplicate(sha: str, delivery_id: object, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The newest EARLIER delivery with identical bytes (wonhee stores outputs by content hash)."""
    older = [
        r for r in rows
        if isinstance(r.get("id"), int) and r.get("ref") == sha
        and (not isinstance(delivery_id, int) or r["id"] < delivery_id)
        and r.get("id") != delivery_id
    ]
    if not older:
        return None
    newest = max(older, key=lambda r: r["id"])
    return {"id": newest["id"], "file": newest.get("filename"), "at": newest.get("created_at")}


def measured_text(check: dict[str, Any]) -> str:
    facts = check.get("facts")
    if not facts:
        return f"{check.get('mb', '?')} MB"
    parts = []
    if facts.get("width") and facts.get("height"):
        parts.append(f"{facts['width']}x{facts['height']}")
    if facts.get("duration_s"):
        parts.append(f"{facts['duration_s']} s")
    if facts.get("fps"):
        parts.append(f"{facts['fps']} fps")
    loud = facts.get("loudness")
    if loud:
        audio = f"audio {loud['integrated_lufs']} LUFS"
        if "true_peak_db" in loud:
            audio += f" (peak {loud['true_peak_db']} dB)"
        parts.append(audio)
    elif facts.get("has_audio") is False:
        parts.append("no audio")
    return ", ".join(parts) or f"{check.get('mb', '?')} MB"


def compose_report(content: str, check: dict[str, Any]) -> str:
    """Wonhee's delivery text plus measured facts, a duplicate warning, and what was NOT checked."""
    lines = [content.replace(_STALE_CHECK, "").strip() or "Your wonhee result is ready."]
    duplicate = check.get("duplicate_of")
    if duplicate:
        lines.append(
            f"WARNING: byte-identical to delivery {duplicate['id']} ({duplicate.get('file')}). "
            "The output did not change, so whatever you asked to change did not take effect."
        )
    lines.append(f"Measured: {measured_text(check)}. File {str(check.get('sha', ''))[:8]}.")
    lines.append("Not checked: caption legibility, timing and motion. Ask me to look at it.")
    return "\n".join(lines)

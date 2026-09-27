"""Shared path helpers for workspace-scoped tools."""

from pathlib import Path

from nanobot.security.workspace_policy import resolve_allowed_path


def _legacy_media_dir() -> Path:
    """Legacy media root (env-less fallback), via the ambient adapter (I1)."""
    from nanobot.kernel.legacy import legacy_media_dir

    return legacy_media_dir()


def resolve_workspace_path(
    path: str,
    workspace: Path | None = None,
    allowed_dir: Path | None = None,
    extra_allowed_dirs: list[Path] | None = None,
    extra_allowed_files: list[Path] | None = None,
    include_media_dir: bool = True,
    media_dir: Path | None = None,
) -> Path:
    """Resolve path against workspace and enforce allowed directory containment.

    *media_dir* is the host's media root (``env.paths.media_dir``); ``None`` falls
    back to the legacy media dir.
    """
    if include_media_dir:
        media_roots = [media_dir if media_dir is not None else _legacy_media_dir()]
    else:
        media_roots = []
    extra_roots = [*media_roots, *(extra_allowed_dirs or [])] if allowed_dir else None
    return resolve_allowed_path(
        path,
        workspace=workspace,
        allowed_root=allowed_dir,
        extra_allowed_roots=extra_roots,
        extra_allowed_files=extra_allowed_files,
    )

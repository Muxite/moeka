"""``python -m examples.resume_interviewer`` (run from the repo root)."""

from __future__ import annotations

from loguru import logger

from .cli import main

logger.disable("nanobot")  # the agent loop logs every turn stage at DEBUG
raise SystemExit(main())

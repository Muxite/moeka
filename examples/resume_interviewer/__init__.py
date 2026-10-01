"""Resume interviewer: a moeka-kernel agent that drives the awork-resume ``awr`` CLI.

It builds the resume, reads ``report.json``, asks the user at most five questions
that would strengthen it, writes the answers to a dated notes doc in the workspace,
rebuilds and diffs. See ``README.md`` in this directory.
"""

from .host import Interviewer, RunState, Settings
from .questions import KINDS, MAX_QUESTIONS, Question

__all__ = ["KINDS", "MAX_QUESTIONS", "Interviewer", "Question", "RunState", "Settings"]

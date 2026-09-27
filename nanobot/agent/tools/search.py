"""Search tools: file discovery and grep."""

# pyright: reportIncompatibleMethodOverride=false, reportPrivateUsage=false

from __future__ import annotations

import asyncio
import fnmatch
import heapq
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Iterable, Iterator, TypeVar

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.filesystem import ListDirTool, _FsTool
from nanobot.security.protected_paths import ProtectedFloor
from nanobot.utils.document import (
    LocatedDocumentLine,
    PdfPageRangeError,
    open_document_line_source,
)

if TYPE_CHECKING:
    from nanobot.agent.tools.context import ToolContext

_DEFAULT_HEAD_LIMIT = 250
_DEFAULT_FILE_HEAD_LIMIT = 200
_DOCUMENT_EXTENSIONS = frozenset({".pdf", ".docx", ".xlsx", ".pptx"})
T = TypeVar("T")
_TYPE_GLOB_MAP = {
    "py": ("*.py", "*.pyi"),
    "python": ("*.py", "*.pyi"),
    "js": ("*.js", "*.jsx", "*.mjs", "*.cjs"),
    "ts": ("*.ts", "*.tsx", "*.mts", "*.cts"),
    "tsx": ("*.tsx",),
    "jsx": ("*.jsx",),
    "json": ("*.json",),
    "md": ("*.md", "*.mdx"),
    "markdown": ("*.md", "*.mdx"),
    "go": ("*.go",),
    "rs": ("*.rs",),
    "rust": ("*.rs",),
    "java": ("*.java",),
    "sh": ("*.sh", "*.bash"),
    "yaml": ("*.yaml", "*.yml"),
    "yml": ("*.yaml", "*.yml"),
    "toml": ("*.toml",),
    "sql": ("*.sql",),
    "html": ("*.html", "*.htm"),
    "css": ("*.css", "*.scss", "*.sass"),
}


@dataclass(slots=True)
class _PendingContextMatch:
    lines: list[LocatedDocumentLine]
    match_index: int
    match_start: int
    remaining_after: int


@dataclass(slots=True)
class _FindFilesEntry:
    path: Path
    rel_path: str
    display_path: str
    name: str
    is_dir: bool


class _FindFilesCancelledError(Exception):
    """Stop a worker scan after its owning async task was cancelled."""


class _FindFilesBudgetExceededError(Exception):
    """Stop an unbounded filesystem scan at its configured budget."""


@dataclass(slots=True)
class _FindFilesBudget:
    cancelled: threading.Event
    deadline: float
    max_paths: int
    scanned_paths: int = 0

    def checkpoint(self) -> None:
        if self.cancelled.is_set():
            raise _FindFilesCancelledError
        if time.monotonic() >= self.deadline:
            raise _FindFilesBudgetExceededError("time")

    def visit_path(self) -> None:
        self.checkpoint()
        self.scanned_paths += 1
        if self.scanned_paths > self.max_paths:
            raise _FindFilesBudgetExceededError("paths")


def _normalize_pattern(pattern: str) -> str:
    return pattern.strip().replace("\\", "/")


def _match_glob(rel_path: str, name: str, pattern: str) -> bool:
    normalized = _normalize_pattern(pattern)
    if not normalized:
        return False
    if "/" in normalized or normalized.startswith("**"):
        pattern_parts = PurePosixPath(normalized).parts
        if "**" in pattern_parts:
            path_parts = PurePosixPath(rel_path).parts
            # Keep relative patterns suffix-matched, as with PurePath.match.
            matched = [True] * (len(path_parts) + 1)
            for part in pattern_parts:
                if part == "**":
                    # A globstar consumes zero or more complete path segments.
                    for index in range(1, len(matched)):
                        matched[index] = matched[index] or matched[index - 1]
                else:
                    matched = [False] + [
                        matched[index] and fnmatch.fnmatchcase(path_part, part)
                        for index, path_part in enumerate(path_parts)
                    ]
            return matched[-1]
        return PurePosixPath(rel_path).match(normalized)
    return fnmatch.fnmatch(name, normalized)


def _is_binary(raw: bytes) -> bool:
    if b"\x00" in raw:
        return True
    sample = raw[:4096]
    if not sample:
        return False
    non_text = sum(byte < 9 or 13 < byte < 32 for byte in sample)
    return (non_text / len(sample)) > 0.2


def _excel_column(index: int) -> str:
    """Return a 1-indexed spreadsheet column label without importing openpyxl."""
    label = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        label = chr(ord("A") + remainder) + label
    return label


def _paginate(items: list[T], limit: int | None, offset: int) -> tuple[list[T], bool]:
    if limit is None:
        return items[offset:], False
    sliced = items[offset : offset + limit]
    truncated = len(items) > offset + limit
    return sliced, truncated


def _pagination_note(limit: int | None, offset: int, truncated: bool) -> str | None:
    if truncated:
        if limit is None:
            return f"(pagination: offset={offset})"
        return f"(pagination: limit={limit}, offset={offset})"
    if offset > 0:
        return f"(pagination: offset={offset})"
    return None


def _matches_type(name: str, file_type: str | None) -> bool:
    if not file_type:
        return True
    lowered = file_type.strip().lower()
    if not lowered:
        return True
    patterns = _TYPE_GLOB_MAP.get(lowered, (f"*.{lowered}",))
    return any(fnmatch.fnmatch(name.lower(), pattern.lower()) for pattern in patterns)


def _matches_query(rel_path: str, query: str | None) -> bool:
    if not query:
        return True
    haystack = rel_path.lower()
    terms = [part for part in query.lower().split() if part]
    return all(term in haystack for term in terms)


class _SearchTool(_FsTool):
    _IGNORE_DIRS = set(ListDirTool._IGNORE_DIRS)

    def _display_path(self, target: Path, root: Path) -> str:
        workspace = self._display_workspace()
        if workspace:
            with suppress(ValueError):
                return target.relative_to(workspace).as_posix()
        return target.relative_to(root).as_posix()

    def _iter_files(self, root: Path) -> Iterable[Path]:
        if root.is_file():
            yield root
            return

        floor = self._protected_floor()
        for dirpath, dirnames, filenames in os.walk(root):
            current = Path(dirpath)
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in self._IGNORE_DIRS and not self._floor_hides(floor, current / d)
            )
            for filename in sorted(filenames):
                candidate = current / filename
                if self._floor_hides(floor, candidate):
                    continue
                yield candidate


class FindFilesTool(_SearchTool):
    """Find files by path fragment, glob, or type."""
    _scopes = {"core", "subagent"}
    _MAX_SCAN_PATHS = 500_000
    _MAX_SCAN_SECONDS = 30.0

    @property
    def name(self) -> str:
        return "find_files"

    @property
    def description(self) -> str:
        return (
            "Find workspace paths by name, glob, or file type. "
            "Returns relative paths and skips dependency/build directories."
        )

    @property
    def read_only(self) -> bool:
        return True

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Search root (default '.')",
                },
                "query": {
                    "type": "string",
                    "description": "Case-insensitive path terms; all must match",
                },
                "glob": {
                    "type": "string",
                    "description": "Path filter, e.g. '*.py' or 'tests/**/test_*.py'",
                },
                "type": {
                    "type": "string",
                    "description": "File type, e.g. 'py', 'ts', 'md', or 'json'",
                },
                "include_dirs": {
                    "type": "boolean",
                    "description": "Include directories (default false)",
                },
                "sort": {
                    "type": "string",
                    "enum": ["path", "modified"],
                    "description": "Sort order (default path)",
                },
                "head_limit": {
                    "type": "integer",
                    "description": "Maximum paths (default 200; 0 for all)",
                    "minimum": 0,
                    "maximum": 1000,
                },
                "offset": {
                    "type": "integer",
                    "description": "Paths to skip before head_limit",
                    "minimum": 0,
                    "maximum": 100000,
                },
            },
        }

    def _entry(self, path: Path, root: Path, *, is_dir: bool) -> _FindFilesEntry:
        display_path = self._display_path(path, root)
        return _FindFilesEntry(
            path=path,
            rel_path=path.relative_to(root).as_posix(),
            display_path=display_path,
            name=path.name,
            is_dir=is_dir,
        )

    def _push_directory_entries(
        self,
        directory: Path,
        root: Path,
        frontier: list[tuple[str, int, _FindFilesEntry]],
        sequence: int,
        budget: _FindFilesBudget,
        floor: ProtectedFloor | None = None,
    ) -> int:
        budget.checkpoint()
        if floor is None:
            floor = self._protected_floor()
        try:
            with os.scandir(directory) as entries:
                for raw_entry in entries:
                    budget.visit_path()
                    try:
                        is_dir = raw_entry.is_dir(follow_symlinks=False)
                        # os.walk yields special files and broken file symlinks,
                        # but does not descend into directory symlinks by default.
                        if not is_dir and raw_entry.is_symlink() and raw_entry.is_dir():
                            continue
                    except OSError:
                        continue
                    if is_dir and raw_entry.name in self._IGNORE_DIRS:
                        continue
                    try:
                        is_link = raw_entry.is_symlink()
                    except OSError:
                        is_link = True
                    if floor.matches(Path(raw_entry.path), write=False, resolve=is_link):
                        continue

                    entry = self._entry(Path(raw_entry.path), root, is_dir=is_dir)
                    sort_path = entry.display_path + ("/" if is_dir else "")
                    heapq.heappush(frontier, (sort_path, sequence, entry))
                    sequence += 1
        except OSError:
            # os.walk silently skips directories that cannot be listed. Preserve
            # that behavior while still allowing cancellation and budget errors
            # to propagate from the explicit checkpoints above.
            pass
        return sequence

    def _iter_paths(
        self,
        root: Path,
        *,
        include_dirs: bool,
        budget: _FindFilesBudget,
    ) -> Iterable[_FindFilesEntry]:
        budget.checkpoint()
        if root.is_file():
            budget.visit_path()
            yield self._entry(root, root.parent, is_dir=False)
            return

        if include_dirs:
            yield self._entry(root, root, is_dir=True)

        floor = self._protected_floor()
        frontier: list[tuple[str, int, _FindFilesEntry]] = []
        sequence = self._push_directory_entries(root, root, frontier, 0, budget, floor)
        while frontier:
            budget.checkpoint()
            _, _, entry = heapq.heappop(frontier)
            if entry.is_dir:
                if include_dirs:
                    yield entry
                sequence = self._push_directory_entries(
                    entry.path,
                    root,
                    frontier,
                    sequence,
                    budget,
                    floor,
                )
            else:
                yield entry

    @staticmethod
    def _matches_entry(
        entry: _FindFilesEntry,
        *,
        query: str | None,
        glob: str | None,
        file_type: str | None,
    ) -> bool:
        if glob and not _match_glob(entry.rel_path, entry.name, glob):
            return False
        if entry.is_dir:
            if file_type:
                return False
        elif not _matches_type(entry.name, file_type):
            return False
        return _matches_query(entry.display_path, query)

    async def execute(
        self,
        path: str = ".",
        query: str | None = None,
        glob: str | None = None,
        type: str | None = None,
        include_dirs: bool = False,
        sort: str = "path",
        head_limit: int | None = None,
        offset: int = 0,
        **kwargs: Any,
    ) -> str:
        cancelled = threading.Event()
        try:
            return await asyncio.to_thread(
                self._execute_sync,
                path=path,
                query=query,
                glob=glob,
                file_type=type,
                include_dirs=include_dirs,
                sort=sort,
                head_limit=head_limit,
                offset=offset,
                cancelled=cancelled,
            )
        except asyncio.CancelledError:
            cancelled.set()
            raise
        except PermissionError as e:
            return ToolResult.error(f"Error: {e}")
        except Exception as e:
            return ToolResult.error(f"Error finding files: {e}")

    def _execute_sync(
        self,
        *,
        path: str,
        query: str | None,
        glob: str | None,
        file_type: str | None,
        include_dirs: bool,
        sort: str,
        head_limit: int | None,
        offset: int,
        cancelled: threading.Event,
    ) -> str:
        started_at = time.monotonic()
        if cancelled.is_set():
            raise _FindFilesCancelledError
        target = self._resolve(path or ".")
        if not target.exists():
            return ToolResult.error(f"Error: Path not found: {path}")
        if not (target.is_dir() or target.is_file()):
            return ToolResult.error(f"Error: Unsupported path: {path}")

        if sort not in {"path", "modified"}:
            return ToolResult.error("Error: sort must be 'path' or 'modified'")

        limit = (
            _DEFAULT_FILE_HEAD_LIMIT
            if head_limit is None
            else None if head_limit == 0 else head_limit
        )
        budget = _FindFilesBudget(
            cancelled=cancelled,
            deadline=started_at + self._MAX_SCAN_SECONDS,
            max_paths=self._MAX_SCAN_PATHS,
        )

        def matching_entries() -> Iterator[tuple[str, float]]:
            for entry in self._iter_paths(
                target,
                include_dirs=include_dirs,
                budget=budget,
            ):
                if not self._matches_entry(
                    entry,
                    query=query,
                    glob=glob,
                    file_type=file_type,
                ):
                    continue
                mtime = 0.0
                if sort == "modified":
                    try:
                        mtime = entry.path.stat().st_mtime
                    except OSError:
                        pass
                suffix = "/" if entry.is_dir else ""
                yield entry.display_path + suffix, mtime

        matches: list[tuple[str, float]]
        try:
            if sort == "modified":
                if limit is None:
                    matches = sorted(matching_entries(), key=lambda item: (-item[1], item[0]))
                else:
                    selection_size = offset + limit + 1
                    matches = heapq.nsmallest(
                        selection_size,
                        matching_entries(),
                        key=lambda item: (-item[1], item[0]),
                    )
            else:
                selection_size = None if limit is None else offset + limit + 1
                matches = []
                for match in matching_entries():
                    matches.append(match)
                    if selection_size is not None and len(matches) >= selection_size:
                        break
            budget.checkpoint()
        except _FindFilesBudgetExceededError as exc:
            if str(exc) == "paths":
                detail = f"{self._MAX_SCAN_PATHS} paths"
            else:
                detail = f"{self._MAX_SCAN_SECONDS:g} seconds"
            return ToolResult.error(
                f"Error: find_files scan exceeded {detail}; "
                "narrow path, query, glob, or type and retry."
            )

        paths = [item[0] for item in matches]
        paged, truncated = _paginate(paths, limit, offset)
        if not paged:
            return "No files found"

        result = "\n".join(paged)
        note = _pagination_note(limit, offset, truncated)
        if note:
            result += "\n\n" + note
        return result


_GREP_DEADLINE_CHECK_EVERY = 64
_GREP_LONG_LINE_CHARS = 10_000
_GREP_BIG_REPEAT = 100  # bounded repeats with a larger max are treated as unbounded
_GREP_WORKER_BATCH_CHARS = 4 * 1024 * 1024
_GREP_WORKER_SCRIPT = Path(__file__).with_name("_grep_worker.py")


class _GrepAbortError(Exception):
    """Base for exceptions that must unwind the whole scan (never swallowed per file)."""


class _GrepTimeoutError(_GrepAbortError):
    """The cooperative deadline passed, or the regex worker was killed on timeout."""


class _GrepWorkerError(_GrepAbortError):
    """The regex worker could not start or returned nothing usable."""


def _regex_may_backtrack_badly(pattern: str, flags: int) -> bool:
    """Cheap static screen for patterns prone to super-linear backtracking.

    Flags a repeat whose body holds another repeat, an alternation under a repeat
    whose branches do not start with distinct literals (``(a+)+$``, ``(a|aa)*``), and
    two or more large repeats (unbounded, or bounded with max > 100) anywhere on one
    path, including inside capturing groups (``a*a*b``, ``(a*)(a*)b``,
    ``a{0,2000}a{0,2000}b``, ``.*error.*timeout``: polynomial, quadratic or worse),
    looking inside atomic groups, possessive repeats and conditionals too, and any
    backreference combined with a large repeat (``(a*)\\1b``).
    Over-approximate on purpose: a flagged pattern only costs a worker process,
    never a wrong answer.
    """
    try:
        import re._parser as sre_parse  # Python 3.11+
        parsed = sre_parse.parse(pattern, flags)
    except Exception:
        return False  # invalid patterns are reported by re.compile as usual

    # Opcodes are compared by name so an unknown/renamed opcode never crashes the screen.
    def kind(op: Any) -> str:
        return getattr(op, "name", str(op))

    repeat_kinds = {"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"}
    groupref_kinds = {"GROUPREF", "GROUPREF_IGNORE", "GROUPREF_LOC_IGNORE", "GROUPREF_UNI_IGNORE"}

    def children(op: Any, av: Any) -> list[Any]:
        """Sub-sequences of a container opcode that are alternatives of each other or nested."""
        name = kind(op)
        if name in repeat_kinds:
            return [av[2]]
        if name == "BRANCH":
            return list(av[1])
        if name == "SUBPATTERN":
            return [av[3]]
        if name in ("ASSERT", "ASSERT_NOT"):
            return [av[1]]
        if name == "ATOMIC_GROUP":
            return [av]
        if name == "GROUPREF_EXISTS":
            return [seq for seq in (av[1], av[2]) if seq is not None]
        return []

    def distinct_literal_starts(alternatives: list[Any]) -> bool:
        firsts: list[Any] = []
        for alt in alternatives:
            if not len(alt) or kind(alt[0][0]) != "LITERAL":
                return False
            firsts.append(alt[0][1])
        return len(set(firsts)) == len(firsts)

    def big_repeats(seq: Any) -> int:
        """Most large repeats met along one path, descending into every container."""
        total = 0
        for op, av in seq:
            subs = children(op, av)
            if kind(op) in repeat_kinds:
                total += (1 if av[1] > _GREP_BIG_REPEAT else 0) + big_repeats(av[2])
            elif kind(op) in ("BRANCH", "GROUPREF_EXISTS"):
                total += max((big_repeats(sub) for sub in subs), default=0)
            else:
                total += sum(big_repeats(sub) for sub in subs)
        return total

    def has_groupref(seq: Any) -> bool:
        for op, av in seq:
            if kind(op) in groupref_kinds:
                return True
            if any(has_groupref(sub) for sub in children(op, av)):
                return True
        return False

    def walk(seq: Any, in_repeat: bool) -> bool:
        for op, av in seq:
            name = kind(op)
            if name in repeat_kinds:
                iterates = av[1] > 1
                if iterates and in_repeat:
                    return True
                if walk(av[2], in_repeat or iterates):
                    return True
            elif name == "BRANCH":
                if in_repeat and not distinct_literal_starts(av[1]):
                    return True
                if any(walk(alt, in_repeat) for alt in av[1]):
                    return True
            elif any(walk(sub, in_repeat) for sub in children(op, av)):
                return True
        return False

    repeats_total = big_repeats(parsed)
    # A backreference makes matching cubic or worse once a repeat feeds it.
    if repeats_total >= 2 or (repeats_total >= 1 and has_groupref(parsed)):
        return True
    return walk(parsed, False)


class _Hit:
    def __init__(self, start: int) -> None:
        self._start = start

    def start(self) -> int:
        return self._start


class _PrecomputedRegex:
    """Stand-in for a compiled pattern: replays match starts computed by the worker.

    Callers search searchable lines in the same order the starts were computed in.
    """

    def __init__(self, starts: list[int]) -> None:
        self._starts = iter(starts)

    def search(self, text: str) -> _Hit | None:
        start = next(self._starts)
        return _Hit(start) if start >= 0 else None


class _RegexWorker:
    """Regex-only child process, started lazily, killable from any thread.

    The worker gets no cwd on ``sys.path`` (``-I``), a pinned cwd, an empty
    environment, and only text over stdin. It is killed on timeout, cancellation
    and ``close()``, and every kill is followed by ``wait()``.
    """

    def __init__(
        self,
        pattern: str,
        flags: int,
        deadline: float | None,
        base_env: Mapping[str, str] | None = None,
    ) -> None:
        # Host exec base env (env.exec_base_env); ``None`` = legacy process env.
        self._base_env = base_env
        self._pattern = pattern
        self._flags = flags
        self._deadline = deadline
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._closed = False
        self.timed_out = False

    def _minimal_env(self) -> dict[str, str]:
        if sys.platform == "win32":
            base = self._base_env
            if base is None:
                from nanobot.kernel.legacy import process_env_snapshot

                base = process_env_snapshot()
            return {k: v for k, v in base.items() if k.upper() in {"SYSTEMROOT", "PATH"}}
        return {}

    def _start(self) -> subprocess.Popen[bytes]:
        with self._lock:
            if self._closed:
                raise _GrepWorkerError("worker closed")
            if self._proc is None:
                try:
                    self._proc = subprocess.Popen(
                        [sys.executable, "-I", "-S", str(_GREP_WORKER_SCRIPT)],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        cwd=tempfile.gettempdir(),
                        env=self._minimal_env(),
                    )
                except OSError as exc:
                    raise _GrepWorkerError(f"cannot start worker: {exc}") from exc
                setup = json.dumps({"pattern": self._pattern, "flags": self._flags})
                self._write(self._proc, setup.encode("ascii") + b"\n")
            return self._proc

    @staticmethod
    def _write(proc: subprocess.Popen[bytes], data: bytes) -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise _GrepWorkerError(f"worker stdin failed: {exc}") from exc

    def kill(self) -> None:
        with self._lock:
            self._closed = True
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def _on_timeout(self) -> None:
        self.timed_out = True
        self.kill()

    def close(self) -> None:
        self.kill()
        proc = self._proc
        if proc is not None:
            for stream in (proc.stdin, proc.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except (OSError, ValueError):
                    pass
            proc.wait()

    def match_starts(self, texts: list[str]) -> list[int]:
        """Return the match start (or -1) of every text; raise on timeout or failure.

        Texts go out in bounded batches (about ``_GREP_WORKER_BATCH_CHARS`` chars per
        request); each reply is read in full before the next request is written.
        """
        starts: list[int] = []
        batch: list[str] = []
        size = 0
        for text in texts:
            if batch and size + len(text) > _GREP_WORKER_BATCH_CHARS:
                starts.extend(self._match_batch(batch))
                batch, size = [], 0
            batch.append(text)
            size += len(text)
        if batch or not texts:
            starts.extend(self._match_batch(batch))
        return starts

    def _match_batch(self, texts: list[str]) -> list[int]:
        remaining = None if self._deadline is None else self._deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise _GrepTimeoutError
        timer = None
        try:
            proc = self._start()
            if remaining is not None:
                timer = threading.Timer(remaining, self._on_timeout)
                timer.daemon = True
                timer.start()
            request = json.dumps({"texts": texts}, ensure_ascii=False)
            self._write(proc, request.encode("utf-8", "surrogatepass") + b"\n")
            assert proc.stdout is not None
            line = proc.stdout.readline()
        except (_GrepWorkerError, OSError, ValueError) as exc:
            # A closed pipe (kill on timeout or cancellation) surfaces as ValueError/OSError.
            if self.timed_out:
                raise _GrepTimeoutError from None
            if isinstance(exc, _GrepWorkerError):
                raise
            raise _GrepWorkerError(f"worker pipe failed: {exc}") from exc
        finally:
            if timer is not None:
                timer.cancel()
        if self.timed_out:
            raise _GrepTimeoutError
        try:
            reply = json.loads(line)
            starts = reply["starts"]
            if not (
                isinstance(starts, list)
                and len(starts) == len(texts)
                and all(isinstance(v, int) for v in starts)
            ):
                raise ValueError("bad reply shape")
        except (ValueError, KeyError, TypeError) as exc:
            raise _GrepWorkerError(f"unusable worker reply: {exc}") from exc
        return starts


def _any_long_line(texts: Iterable[str]) -> bool:
    return any(len(text) > _GREP_LONG_LINE_CHARS for text in texts)


def _with_deadline(
    lines: Iterable[LocatedDocumentLine], deadline: float | None
) -> Iterator[LocatedDocumentLine]:
    """Yield ``lines``, raising ``_GrepTimeoutError`` once the deadline has passed."""
    if deadline is None:
        yield from lines
        return
    for index, line in enumerate(lines):
        if index % _GREP_DEADLINE_CHECK_EVERY == 0 and time.monotonic() > deadline:
            raise _GrepTimeoutError
        yield line


class GrepTool(_SearchTool):
    """Search text and document contents using a regex-like pattern."""
    _scopes = {"core", "subagent"}

    _MAX_RESULT_CHARS = 128_000
    _MAX_RENDERED_LINE_CHARS = 2_000
    _MAX_FILE_BYTES = 2_000_000
    _MAX_EXPLICIT_FILE_BYTES = 100_000_000

    def __init__(
        self,
        *args: Any,
        regex_timeout_s: float = 10.0,
        base_env: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._regex_timeout_s = regex_timeout_s
        # Host exec base env for the regex worker (Windows SYSTEMROOT/PATH).
        self._base_env = base_env

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        tool = super().create(ctx)
        if ctx.env is not None and isinstance(tool, GrepTool):
            tool._base_env = ctx.env.exec_base_env
        return tool

    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return (
            "Search text, PDF, DOCX, XLSX, and PPTX content. "
            "Returns matches with five context lines and source locators by default."
        )

    @property
    def read_only(self) -> bool:
        return True

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex, or literal text when fixed_strings=true",
                    "minLength": 1,
                },
                "path": {
                    "type": "string",
                    "description": "Search root (default '.')",
                },
                "glob": {
                    "type": "string",
                    "description": "Path filter, e.g. '*.py' or 'tests/**/test_*.py'",
                },
                "type": {
                    "type": "string",
                    "description": "File type, e.g. 'py', 'ts', 'md', or 'json'",
                },
                "pages": {
                    "type": "string",
                    "description": "PDF page number or range, e.g. '7' or '101-200' (max 100 pages)",
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Ignore case (default false)",
                },
                "fixed_strings": {
                    "type": "boolean",
                    "description": "Treat pattern literally (default false)",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "description": (
                        "content: matches with context (default); "
                        "files_with_matches: paths; count: matches per file"
                    ),
                },
                "context_before": {
                    "type": "integer",
                    "description": "Context lines before a match (default 5)",
                    "minimum": 0,
                    "maximum": 20,
                },
                "context_after": {
                    "type": "integer",
                    "description": "Context lines after a match (default 5)",
                    "minimum": 0,
                    "maximum": 20,
                },
                "head_limit": {
                    "type": "integer",
                    "description": "Maximum matches or file entries (default 250; 0 for all)",
                    "minimum": 0,
                    "maximum": 1000,
                },
                "offset": {
                    "type": "integer",
                    "description": "Matches or file entries to skip before head_limit",
                    "minimum": 0,
                    "maximum": 100000,
                },
            },
            "required": ["pattern"],
        }

    @staticmethod
    def _clip_rendered_line(text: str, match_start: int | None = None) -> str:
        limit = GrepTool._MAX_RENDERED_LINE_CHARS
        if len(text) <= limit:
            return text

        marker = "..."
        available = limit - len(marker)
        if match_start is None:
            return text[:available] + marker

        start = max(0, match_start - available // 3)
        start = min(start, len(text) - available)
        end = start + available
        prefix = marker if start else ""
        suffix = marker if end < len(text) else ""
        visible = text[start:end]
        if prefix and suffix:
            visible = visible[: available - len(marker)]
        return prefix + visible + suffix

    @staticmethod
    def _matching_contexts(
        lines: Iterable[LocatedDocumentLine],
        regex: re.Pattern[str] | _PrecomputedRegex,
        before: int,
        after: int,
    ) -> Iterable[tuple[list[LocatedDocumentLine], int, int]]:
        history: deque[LocatedDocumentLine] = deque(maxlen=before)
        pending: list[_PendingContextMatch] = []

        for line in lines:
            if not line.searchable:
                continue

            still_pending: list[_PendingContextMatch] = []
            for item in pending:
                item.lines.append(line)
                item.remaining_after -= 1
                if item.remaining_after == 0:
                    yield item.lines, item.match_index, item.match_start
                else:
                    still_pending.append(item)
            pending = still_pending

            match = regex.search(line.text)
            if match is not None:
                context_lines = [*history, line]
                item = _PendingContextMatch(
                    lines=context_lines,
                    match_index=len(context_lines) - 1,
                    match_start=match.start(),
                    remaining_after=after,
                )
                if after == 0:
                    yield item.lines, item.match_index, item.match_start
                else:
                    pending.append(item)
            history.append(line)

        for item in pending:
            yield item.lines, item.match_index, item.match_start

    @staticmethod
    def _format_block(
        display_path: str,
        lines: list[LocatedDocumentLine],
        match_index: int,
        match_start: int = 0,
    ) -> str:
        match_line = lines[match_index]
        source_line = match_line.extracted_line
        match_locator = match_line.locator
        if match_locator.startswith("sheet="):
            column = _excel_column(match_line.text[:match_start].count("\t") + 1)
            row_match = re.search(r",row=(\d+)$", match_locator)
            if row_match:
                match_locator += f",cell={column}{row_match.group(1)}"
        suffix = f" [{match_locator}]" if match_locator else ""
        block = [f"{display_path}:{source_line}{suffix}"]
        for index, line in enumerate(lines):
            is_match = index == match_index
            marker = ">" if is_match else " "
            coordinate = str(line.extracted_line)
            if line.locator:
                coordinate += f" [{line.locator}]"
            rendered = GrepTool._clip_rendered_line(
                line.text,
                match_start if is_match else None,
            )
            block.append(f"{marker} {coordinate}| {rendered}")
        return "\n".join(block)

    def _timeout_error(self) -> str:
        return ToolResult.error(
            f"Error: grep timed out after {self._regex_timeout_s:g}s (pattern too expensive); "
            "use a simpler pattern or narrow the search path."
        )

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        glob: str | None = None,
        type: str | None = None,
        pages: str | None = None,
        case_insensitive: bool = False,
        fixed_strings: bool = False,
        output_mode: str = "content",
        context_before: int = 5,
        context_after: int = 5,
        max_matches: int | None = None,
        max_results: int | None = None,
        head_limit: int | None = None,
        offset: int = 0,
        **kwargs: Any,
    ) -> str:
        scan_kwargs: dict[str, Any] = dict(
            pattern=pattern, path=path, glob=glob, type=type, pages=pages,
            case_insensitive=case_insensitive, fixed_strings=fixed_strings,
            output_mode=output_mode, context_before=context_before,
            context_after=context_after, max_matches=max_matches, max_results=max_results,
            head_limit=head_limit, offset=offset,
        )
        deadline = time.monotonic() + self._regex_timeout_s
        worker: _RegexWorker | None = None
        if not fixed_strings:
            flags = re.IGNORECASE if case_insensitive else 0
            worker = _RegexWorker(pattern, flags, deadline, self._base_env)
        try:
            return await asyncio.to_thread(
                self._execute_sync, deadline=deadline, worker=worker, **scan_kwargs
            )
        except asyncio.CancelledError:
            if worker is not None:
                worker.kill()
            raise
        except _GrepTimeoutError:
            return self._timeout_error()
        except _GrepWorkerError as exc:
            logger.warning("grep regex worker unavailable ({}); failing closed", exc)
            return ToolResult.error(
                "Error: grep could not isolate an expensive pattern; "
                "use a simpler pattern or narrower path."
            )
        finally:
            if worker is not None:
                worker.close()

    def _execute_sync(
        self,
        *,
        pattern: str,
        path: str = ".",
        glob: str | None = None,
        type: str | None = None,
        pages: str | None = None,
        case_insensitive: bool = False,
        fixed_strings: bool = False,
        output_mode: str = "content",
        context_before: int = 5,
        context_after: int = 5,
        max_matches: int | None = None,
        max_results: int | None = None,
        head_limit: int | None = None,
        offset: int = 0,
        deadline: float | None = None,
        worker: _RegexWorker | None = None,
    ) -> str:
        try:
            target = self._resolve(path or ".")
            if not target.exists():
                return ToolResult.error(f"Error: Path not found: {path}")
            if not (target.is_dir() or target.is_file()):
                return ToolResult.error(f"Error: Unsupported path: {path}")

            flags = re.IGNORECASE if case_insensitive else 0
            try:
                needle = re.escape(pattern) if fixed_strings else pattern
                regex: re.Pattern[str] | _PrecomputedRegex = re.compile(needle, flags)
            except re.error as e:
                return ToolResult.error(f"Error: invalid regex pattern: {e}")

            if head_limit is not None:
                limit = None if head_limit == 0 else head_limit
            elif output_mode == "content" and max_matches is not None:
                limit = max_matches
            elif output_mode != "content" and max_results is not None:
                limit = max_results
            else:
                limit = _DEFAULT_HEAD_LIMIT
            blocks: list[str] = []
            result_chars = 0
            seen_content_matches = 0
            truncated = False
            size_truncated = False
            skipped_binary = 0
            skipped_large = 0
            document_errors: list[str] = []
            document_continuations: list[str] = []
            matching_files: list[str] = []
            counts: dict[str, int] = {}
            file_mtimes: dict[str, float] = {}
            root = target if target.is_dir() else target.parent
            max_file_bytes = (
                self._MAX_EXPLICIT_FILE_BYTES if target.is_file() else self._MAX_FILE_BYTES
            )
            # Expensive patterns (and any regex over very long lines) are matched by the
            # killable regex-only worker; everything else stays in-process.
            screened = worker is not None and _regex_may_backtrack_badly(
                pattern, re.IGNORECASE if case_insensitive else 0
            )

            for file_path in self._iter_files(target):
                if deadline is not None and time.monotonic() > deadline:
                    raise _GrepTimeoutError
                rel_path = file_path.relative_to(root).as_posix()
                if glob and not _match_glob(rel_path, file_path.name, glob):
                    continue
                if not _matches_type(file_path.name, type):
                    continue
                display_path = self._display_path(file_path, root)

                try:
                    file_size = file_path.stat().st_size
                except OSError:
                    skipped_binary += 1
                    continue
                if file_size > max_file_bytes:
                    skipped_large += 1
                    continue
                try:
                    mtime = file_path.stat().st_mtime
                except OSError:
                    mtime = 0.0
                source_iterator: Iterator[LocatedDocumentLine] | None = None
                is_document = file_path.suffix.lower() in _DOCUMENT_EXTENSIONS
                long_line = False
                file_regex = regex
                try:
                    if is_document:
                        source = open_document_line_source(file_path, pages=pages)
                        if source is None:
                            skipped_binary += 1
                            continue
                        source_iterator = source.lines
                        source_lines: Iterable[LocatedDocumentLine] = source_iterator
                        if worker is not None:
                            # Long-line check needs the whole document text.
                            source_lines = list(source_iterator)
                            long_line = _any_long_line(ln.text for ln in source_lines)
                        if source.continuation:
                            document_continuations.append(
                                f"({display_path}: continue PDF search with "
                                f"{source.continuation})"
                            )
                    else:
                        with file_path.open("rb") as file:
                            raw = file.read(max_file_bytes + 1)
                        if _is_binary(raw):
                            skipped_binary += 1
                            continue
                        try:
                            content = raw.decode("utf-8")
                        except UnicodeDecodeError:
                            skipped_binary += 1
                            continue
                        text_lines = content.splitlines()
                        long_line = worker is not None and _any_long_line(text_lines)
                        source_lines = (
                            LocatedDocumentLine(text, line_no, "")
                            for line_no, text in enumerate(text_lines, 1)
                        )

                    if worker is not None and (screened or long_line):
                        located = list(source_lines)
                        file_regex = _PrecomputedRegex(
                            worker.match_starts([ln.text for ln in located if ln.searchable])
                        )
                        source_lines = iter(located)
                    source_lines = _with_deadline(source_lines, deadline)
                    file_had_match = False
                    if output_mode == "content":
                        contexts = self._matching_contexts(
                            source_lines,
                            file_regex,
                            context_before,
                            context_after,
                        )
                        for context_lines, match_index, match_start in contexts:
                            file_had_match = True
                            seen_content_matches += 1
                            if seen_content_matches <= offset:
                                continue
                            if limit is not None and len(blocks) >= limit:
                                truncated = True
                                break
                            block = self._format_block(
                                display_path,
                                context_lines,
                                match_index,
                                match_start,
                            )
                            extra_sep = 2 if blocks else 0
                            if result_chars + extra_sep + len(block) > self._MAX_RESULT_CHARS:
                                size_truncated = True
                                break
                            blocks.append(block)
                            result_chars += extra_sep + len(block)
                    else:
                        for line in source_lines:
                            if not line.searchable or file_regex.search(line.text) is None:
                                continue
                            file_had_match = True
                            if output_mode == "count":
                                counts[display_path] = counts.get(display_path, 0) + 1
                                continue
                            if display_path not in matching_files:
                                matching_files.append(display_path)
                                file_mtimes[display_path] = mtime
                            break
                except Exception as e:
                    if not is_document or isinstance(e, _GrepAbortError):
                        raise
                    if target.is_file():
                        if isinstance(e, PdfPageRangeError):
                            return ToolResult.error(
                                f"Error: Invalid PDF page range '{pages}': {e!s}."
                            )
                        return ToolResult.error(
                            f"Error searching document {display_path}: {e!s}"
                        )
                    skipped_binary += 1
                    document_errors.append(f"{display_path}: {e!s}")
                    continue
                finally:
                    close = getattr(source_iterator, "close", None)
                    if close is not None:
                        close()
                if output_mode == "count" and file_had_match:
                    if display_path not in matching_files:
                        matching_files.append(display_path)
                        file_mtimes[display_path] = mtime
                if output_mode in {"count", "files_with_matches"} and file_had_match:
                    continue
                if truncated or size_truncated:
                    break

            if output_mode == "files_with_matches":
                if not matching_files:
                    result = f"No matches found for pattern '{pattern}' in {path}"
                else:
                    ordered_files = sorted(
                        matching_files,
                        key=lambda name: (-file_mtimes.get(name, 0.0), name),
                    )
                    paged, truncated = _paginate(ordered_files, limit, offset)
                    result = "\n".join(paged)
            elif output_mode == "count":
                if not counts:
                    result = f"No matches found for pattern '{pattern}' in {path}"
                else:
                    ordered_files = sorted(
                        matching_files,
                        key=lambda name: (-file_mtimes.get(name, 0.0), name),
                    )
                    ordered, truncated = _paginate(ordered_files, limit, offset)
                    count_lines = [f"{name}: {counts[name]}" for name in ordered]
                    result = "\n".join(count_lines)
            else:
                if not blocks:
                    result = f"No matches found for pattern '{pattern}' in {path}"
                else:
                    result = "\n\n".join(blocks)

            notes: list[str] = []
            if output_mode == "content" and truncated:
                notes.append(
                    f"(pagination: limit={limit}, offset={offset}; "
                    f"use offset={offset + len(blocks)} to continue)"
                )
            elif output_mode == "content" and size_truncated:
                notes.append(
                    "(output truncated due to size; "
                    f"use offset={offset + len(blocks)} to continue)"
                )
            elif truncated and output_mode in {"count", "files_with_matches"}:
                notes.append(
                    f"(pagination: limit={limit}, offset={offset})"
                )
            elif output_mode in {"count", "files_with_matches"} and offset > 0:
                notes.append(f"(pagination: offset={offset})")
            elif output_mode == "content" and offset > 0 and blocks:
                notes.append(f"(pagination: offset={offset})")
            if skipped_binary:
                notes.append(f"(skipped {skipped_binary} binary/unreadable files)")
            if skipped_large:
                notes.append(f"(skipped {skipped_large} large files)")
            if document_errors:
                notes.append(f"(first document error: {document_errors[0]})")
            notes.extend(document_continuations[:10])
            if output_mode == "count" and counts:
                notes.append(
                    f"(total matches: {sum(counts.values())} in {len(counts)} files)"
                )
            if notes:
                result += "\n\n" + "\n".join(notes)
            return result
        except _GrepAbortError:
            raise
        except PermissionError as e:
            return ToolResult.error(f"Error: {e}")
        except Exception as e:
            return ToolResult.error(f"Error searching files: {e}")

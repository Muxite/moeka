"""Canvas LMS channel — polls inbox conversations and monitors course activity."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx
from loguru import logger
from pydantic import Field

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Base


class CanvasConfig(Base):
    """Canvas LMS channel configuration."""

    enabled: bool = False
    api_url: str = ""  # e.g. https://canvas.instructure.com
    api_token: str = ""

    monitor_inbox: bool = True  # Poll conversations/inbox for new messages
    monitor_announcements: bool = True  # Forward new course announcements
    monitor_grades: bool = False  # Notify on new grade postings

    poll_interval_seconds: int = 60
    # Max ids to remember per stream (prevents unbounded memory growth).
    dedupe_max: int = 2000
    allow_from: list[str] = Field(default_factory=list)


def _mark_seen(seen: dict[str, None], item_id: str, max_size: int) -> bool:
    """
    Record *item_id* in *seen* with FIFO eviction.

    :param seen: insertion-ordered dict acting as a bounded set.
    :param item_id: stringified Canvas resource id.
    :param max_size: maximum number of ids kept before the oldest is evicted.
    :returns: True if *item_id* was not previously present (i.e. is new).
    """
    if item_id in seen:
        return False
    seen[item_id] = None
    if len(seen) > max_size:
        seen.pop(next(iter(seen)))
    return True


class CanvasChannel(BaseChannel):
    """
    Canvas LMS channel.

    Inbound:
    - Polls the Canvas inbox for new conversation messages.
    - Optionally monitors course announcements and grade postings.

    Outbound:
    - Replies to Canvas conversations on behalf of the authenticated user.

    Bootstrap behavior:
    - On the first poll of each stream (inbox / announcements / grades) the
      channel records every id it sees without forwarding to the agent. This
      prevents the initial connection from flooding the bus with historical
      items. Subsequent polls only forward ids not yet seen.
    """

    name = "canvas"
    display_name = "Canvas LMS"

    _HTTP_TIMEOUT = 30.0

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return CanvasConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus) -> None:
        if isinstance(config, dict):
            config = CanvasConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: CanvasConfig = config
        self._seen_message_ids: dict[str, None] = {}
        self._seen_announcement_ids: dict[str, None] = {}
        self._seen_submission_ids: dict[str, None] = {}
        self._bootstrapped_inbox = False
        self._bootstrapped_announcements = False
        self._bootstrapped_grades = False
        self._self_id: str | None = None
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.api_token}"}

    def _url(self, path: str) -> str:
        base = self.config.api_url.rstrip("/")
        return f"{base}{path}"

    def _ensure_client(self) -> httpx.AsyncClient:
        """Lazily create a shared AsyncClient so polls reuse connections."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self._HTTP_TIMEOUT,
                headers=self._headers(),
            )
        return self._client

    async def _get(self, path: str, params: dict | None = None) -> Any:
        """
        Fetch a single Canvas API page.

        :param path: API path beginning with ``/api/v1/…``.
        :param params: optional query parameters.
        :returns: parsed JSON (dict, list, or scalar).
        """
        client = self._ensure_client()
        resp = await client.get(self._url(path), params=params or {})
        resp.raise_for_status()
        return resp.json()

    async def _get_all(self, path: str, params: dict | None = None) -> list:
        """
        Walk a Canvas paginated endpoint via the ``Link: rel="next"`` header.

        :param path: API path beginning with ``/api/v1/…``.
        :param params: query params sent on the first request (next URLs carry them).
        :returns: concatenated list across all pages.
        """
        results: list = []
        url: str | None = self._url(path)
        p = params or {}
        client = self._ensure_client()
        while url:
            resp = await client.get(url, params=p)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                results.extend(data)
            else:
                results.append(data)
            url = None
            p = {}  # params are baked into the next URL
            link_header = resp.headers.get("Link", "")
            for part in link_header.split(","):
                part = part.strip()
                if 'rel="next"' in part:
                    url = part.split(";")[0].strip().strip("<>")
                    break
        return results

    async def _post(self, path: str, data: dict) -> Any:
        """POST form-encoded data (Canvas default for conversations/messages)."""
        client = self._ensure_client()
        resp = await client.post(self._url(path), data=data)
        resp.raise_for_status()
        return resp.json()

    async def _ensure_self_id(self) -> str | None:
        """
        Return the authenticated user's Canvas id, caching after first lookup.

        :returns: stringified Canvas user id, or None if the lookup fails.
        """
        if self._self_id is not None:
            return self._self_id
        try:
            me = await self._get("/api/v1/users/self")
            self._self_id = str(me.get("id", ""))
            return self._self_id
        except Exception as e:
            logger.warning("canvas: failed to fetch self user id: {}", e)
            return None

    # ------------------------------------------------------------------
    # Polling helpers
    # ------------------------------------------------------------------

    async def _poll_inbox(self) -> None:
        """
        Look for new conversation messages.

        First call marks everything seen without publishing; subsequent calls
        publish only truly-new messages from senders we did not author.
        """
        self_id = await self._ensure_self_id()
        bootstrap = not self._bootstrapped_inbox
        try:
            convos = await self._get_all(
                "/api/v1/conversations",
                {"scope": "inbox", "per_page": 100},
            )
        except Exception as e:
            logger.warning("canvas: inbox poll failed: {}", e)
            return

        for convo in convos:
            convo_id = str(convo.get("id", ""))
            if not convo_id or not convo.get("last_message"):
                continue

            try:
                full = await self._get(f"/api/v1/conversations/{convo_id}")
            except Exception as e:
                logger.warning("canvas: failed to fetch conversation {}: {}", convo_id, e)
                continue

            messages = full.get("messages", [])
            participants = {
                str(p["id"]): p.get("name", "Unknown")
                for p in full.get("participants", [])
            }

            for msg in messages:
                msg_id = str(msg.get("id", ""))
                if not msg_id:
                    continue
                is_new = _mark_seen(
                    self._seen_message_ids, msg_id, self.config.dedupe_max,
                )
                if not is_new or bootstrap:
                    continue

                author_id = str(msg.get("author_id", ""))
                if self_id and author_id == self_id:
                    continue  # don't react to our own replies

                body = (msg.get("body") or "").strip()
                if not body:
                    continue

                subject = convo.get("subject", "")
                author_name = participants.get(author_id, author_id)
                content = (
                    f"[Canvas Inbox — {subject}]\n"
                    f"From: {author_name}\n\n"
                    f"{body}"
                )

                await self._handle_message(
                    sender_id=author_id,
                    chat_id=convo_id,
                    content=content,
                    metadata={
                        "canvas_type": "inbox",
                        "conversation_id": convo_id,
                        "message_id": msg_id,
                    },
                    session_key=f"canvas:convo:{convo_id}",
                )

        self._bootstrapped_inbox = True

    async def _poll_announcements(self) -> None:
        """Forward new course announcements to the bus (bootstraps silently)."""
        bootstrap = not self._bootstrapped_announcements
        try:
            courses = await self._get_all(
                "/api/v1/courses",
                {"enrollment_state": "active", "per_page": 100},
            )
        except Exception as e:
            logger.warning("canvas: failed to fetch courses: {}", e)
            return

        for course in courses:
            course_id = course.get("id")
            course_name = course.get("name", f"Course {course_id}")
            try:
                announcements = await self._get_all(
                    "/api/v1/announcements",
                    {"context_codes[]": f"course_{course_id}", "per_page": 100},
                )
            except Exception as e:
                logger.debug("canvas: announcements fetch failed for {}: {}", course_id, e)
                continue

            for ann in announcements:
                ann_id = str(ann.get("id", ""))
                if not ann_id:
                    continue
                is_new = _mark_seen(
                    self._seen_announcement_ids, ann_id, self.config.dedupe_max,
                )
                if not is_new or bootstrap:
                    continue

                title = ann.get("title", "Announcement")
                message = re.sub(r"<[^>]+>", "", ann.get("message", "") or "").strip()
                author_obj = ann.get("author") or {}
                author = author_obj.get("display_name", "Instructor")
                content = (
                    f"[Canvas Announcement — {course_name}]\n"
                    f"{title}\n"
                    f"From: {author}\n\n"
                    f"{message}"
                )

                await self._handle_message(
                    sender_id=str(author_obj.get("id", "canvas")),
                    chat_id=f"course:{course_id}:announcement:{ann_id}",
                    content=content,
                    metadata={
                        "canvas_type": "announcement",
                        "course_id": course_id,
                        "announcement_id": ann_id,
                    },
                    session_key=f"canvas:course:{course_id}",
                )

        self._bootstrapped_announcements = True

    async def _poll_grades(self) -> None:
        """Notify on newly-posted grades (bootstraps silently)."""
        bootstrap = not self._bootstrapped_grades
        try:
            courses = await self._get_all(
                "/api/v1/courses",
                {"enrollment_state": "active", "per_page": 100},
            )
        except Exception as e:
            logger.warning("canvas: failed to fetch courses for grade poll: {}", e)
            return

        for course in courses:
            course_id = course.get("id")
            course_name = course.get("name", f"Course {course_id}")
            try:
                submissions = await self._get_all(
                    f"/api/v1/courses/{course_id}/students/submissions",
                    {
                        "student_ids[]": "self",
                        "include[]": "assignment",
                        "per_page": 100,
                    },
                )
            except Exception as e:
                logger.debug("canvas: grade poll failed for {}: {}", course_id, e)
                continue

            for sub in submissions:
                sub_id = str(sub.get("id", ""))
                score = sub.get("score")
                if not sub_id or score is None:
                    continue
                is_new = _mark_seen(
                    self._seen_submission_ids, sub_id, self.config.dedupe_max,
                )
                if not is_new or bootstrap:
                    continue

                assignment = sub.get("assignment") or {}
                assignment_name = assignment.get("name") or sub.get("assignment_id", "")
                graded_at = sub.get("graded_at", "")
                content = (
                    f"[Canvas Grade — {course_name}]\n"
                    f"Assignment: {assignment_name}\n"
                    f"Score: {score}\n"
                    f"Graded at: {graded_at}"
                )

                await self._handle_message(
                    sender_id="canvas",
                    chat_id=f"course:{course_id}:submission:{sub_id}",
                    content=content,
                    metadata={
                        "canvas_type": "grade",
                        "course_id": course_id,
                        "submission_id": sub_id,
                    },
                    session_key=f"canvas:course:{course_id}",
                )

        self._bootstrapped_grades = True

    # ------------------------------------------------------------------
    # BaseChannel interface
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the Canvas polling loop."""
        if not self.config.api_url or not self.config.api_token:
            logger.warning("canvas: api_url or api_token not configured — channel disabled")
            return

        self._running = True
        logger.info("Starting Canvas LMS channel (polling every {}s)...",
                    max(15, int(self.config.poll_interval_seconds)))

        poll_seconds = max(15, int(self.config.poll_interval_seconds))
        first = True
        try:
            while self._running:
                if not first:
                    await asyncio.sleep(poll_seconds)
                first = False

                if self.config.monitor_inbox:
                    await self._poll_inbox()
                if self.config.monitor_announcements:
                    await self._poll_announcements()
                if self.config.monitor_grades:
                    await self._poll_grades()
        finally:
            await self._close_client()

    async def stop(self) -> None:
        """Stop the polling loop and close the HTTP client."""
        self._running = False
        await self._close_client()

    async def _close_client(self) -> None:
        """Close the shared AsyncClient if one is open."""
        if self._client is not None and not self._client.is_closed:
            try:
                await self._client.aclose()
            except Exception as e:
                logger.debug("canvas: error closing http client: {}", e)
        self._client = None

    async def send(self, msg: OutboundMessage) -> None:
        """
        Route an outbound message.

        Replies to inbox conversations are posted back to Canvas. Messages
        tied to announcements or grades have no natural reply target and are
        logged (the agent can still use the Canvas skill for discussion posts).

        :param msg: outbound payload from the agent loop. ``metadata.canvas_type``
            (``inbox`` / ``announcement`` / ``grade``) drives routing; a missing
            type falls back to a conservative heuristic.
        """
        chat_id = msg.chat_id
        canvas_type = (msg.metadata or {}).get("canvas_type")

        is_inbox = canvas_type == "inbox" or (
            canvas_type is None and chat_id and not chat_id.startswith("course:")
        )
        if not is_inbox:
            logger.info(
                "canvas: outbound message for {} has no reply target ({}): {}",
                chat_id, canvas_type or "unknown", msg.content[:100],
            )
            return

        convo_id = (msg.metadata or {}).get("conversation_id") or chat_id
        try:
            await self._post(
                f"/api/v1/conversations/{convo_id}/add_message",
                {"body": msg.content},
            )
            logger.info("canvas: replied to conversation {}", convo_id)
        except Exception as e:
            logger.error("canvas: failed to reply to conversation {}: {}", convo_id, e)
            raise

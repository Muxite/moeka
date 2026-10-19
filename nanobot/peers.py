"""Owner-paired local peer delivery over authenticated Unix domain sockets."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import ssl
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from nanobot.bus.events import InboundMessage, OutboundMessage

if TYPE_CHECKING:
    from nanobot.bus.queue import MessageBus
    from nanobot.config.schema import PeerConfig, PeerListenerConfig

MAX_PEER_CONTENT_CHARS = 12_000
MAX_REPLY_HOPS = 3
MAX_ATTEMPTS = 8


class PeerDeliveryError(ValueError):
    """A paired-peer request could not be queued or delivered."""


@dataclass(frozen=True)
class PairedPeer:
    id: str
    socket_path: str | None
    url: str | None
    secret: str


class PeerService:
    """Durable, acknowledged peer work queue for one agent instance."""

    def __init__(
        self,
        *,
        instance_id: str,
        peers: list[PeerConfig],
        listener: PeerListenerConfig | None = None,
        state_dir: Path,
        socket_path: Path,
        bus: MessageBus,
    ) -> None:
        self.instance_id = instance_id
        self.peers = {
            peer.id: PairedPeer(
                peer.id,
                str(Path(peer.socket_path).expanduser().resolve()) if peer.socket_path else None,
                peer.url,
                self._validated_secret(peer.secret),
            )
            for peer in peers
        }
        self.listener = listener
        self.state_dir = state_dir
        self.socket_path = socket_path
        self.db_path = state_dir / "outbox.sqlite3"
        self.bus = bus
        self._server: asyncio.AbstractServer | None = None
        self._https_server: asyncio.AbstractServer | None = None
        self._retry_task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(state_dir, 0o700)
        self._init_db()

    @staticmethod
    def _validated_secret(secret: str) -> str:
        if "${" in secret or len(secret) < 32:
            raise PeerDeliveryError(
                "paired peer secret is unresolved or too short; configure at least 32 characters",
            )
        return secret

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _init_db(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, peer_id TEXT NOT NULL, content TEXT NOT NULL,
                reply_to TEXT, hops INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                created REAL NOT NULL, last_error TEXT, kind TEXT NOT NULL DEFAULT 'work',
                return_channel TEXT, return_chat_id TEXT, return_session_key TEXT, result TEXT
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS inbox (
                id TEXT PRIMARY KEY, sender_id TEXT NOT NULL, content TEXT NOT NULL,
                reply_to TEXT, hops INTEGER NOT NULL, received REAL NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'received',
                kind TEXT NOT NULL DEFAULT 'work'
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS peer_results (
                id TEXT PRIMARY KEY, reply_to TEXT NOT NULL, sender_id TEXT NOT NULL,
                content TEXT NOT NULL, received REAL NOT NULL, published INTEGER NOT NULL DEFAULT 0
            )""")
            self._add_columns(
                db,
                "outbox",
                {
                    "kind": "TEXT NOT NULL DEFAULT 'work'",
                    "return_channel": "TEXT",
                    "return_chat_id": "TEXT",
                    "return_session_key": "TEXT",
                    "result": "TEXT",
                },
            )
            self._add_columns(
                db,
                "inbox",
                {
                    "delivered": "INTEGER NOT NULL DEFAULT 0",
                    "state": "TEXT NOT NULL DEFAULT 'received'",
                    "kind": "TEXT NOT NULL DEFAULT 'work'",
                },
            )
            # Old releases marked a row delivered as soon as it entered the
            # volatile bus. Treat those as potentially started work after upgrade.
            db.execute(
                "UPDATE inbox SET state='started' WHERE delivered=1 AND state='received'",
            )

    @staticmethod
    def _add_columns(db: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        existing = {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
        for name, declaration in columns.items():
            if name not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    async def start(self) -> None:
        local_peers = any(peer.socket_path is not None for peer in self.peers.values())
        if local_peers:
            if not hasattr(asyncio, "start_unix_server"):
                raise RuntimeError("paired local peers require Unix domain socket support")
            self.socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.socket_path.parent, 0o700)
            try:
                existing_mode = self.socket_path.lstat().st_mode
            except FileNotFoundError:
                existing_mode = None
            if existing_mode is not None:
                if not stat.S_ISSOCK(existing_mode):
                    raise RuntimeError(
                        f"refusing to replace non-socket peer path {self.socket_path}"
                    )
                self.socket_path.unlink()
            self._server = await asyncio.start_unix_server(
                self._receive, path=str(self.socket_path)
            )
            os.chmod(self.socket_path, 0o600)
        if self.listener is not None:
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(self.listener.cert_file, self.listener.key_file)
            self._https_server = await asyncio.start_server(
                self._receive_https,
                self.listener.host,
                self.listener.port,
                ssl=tls,
            )
        for row in await asyncio.to_thread(self._pending_inbox):
            if row["state"] == "started":
                await self.complete_inbox(
                    str(row["id"]),
                    "This peer request was interrupted during a restart. It was not rerun because it may have completed tool side effects. Please resubmit if you still need it.",
                )
            else:
                await self._publish_inbox(row)
        await self._publish_pending_results()
        self._retry_task = asyncio.create_task(self._retry_loop(), name="moeka-peer-outbox")

    async def stop(self) -> None:
        if self._retry_task is not None:
            self._retry_task.cancel()
            try:
                await self._retry_task
            except asyncio.CancelledError:
                pass
            self._retry_task = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._https_server is not None:
            self._https_server.close()
            await self._https_server.wait_closed()
            self._https_server = None
        try:
            if stat.S_ISSOCK(self.socket_path.lstat().st_mode):
                self.socket_path.unlink()
        except FileNotFoundError:
            pass

    async def enqueue(
        self,
        peer_id: str,
        content: str,
        *,
        reply_to: str | None = None,
        hops: int = 0,
        return_channel: str | None = None,
        return_chat_id: str | None = None,
        return_session_key: str | None = None,
    ) -> str:
        if peer_id not in self.peers:
            raise PeerDeliveryError(f"peer {peer_id!r} is not paired by the owner")
        text = content.strip()
        if not text or len(text) > MAX_PEER_CONTENT_CHARS:
            raise PeerDeliveryError(f"content must contain 1..{MAX_PEER_CONTENT_CHARS} characters")
        if hops >= MAX_REPLY_HOPS:
            raise PeerDeliveryError(f"peer reply limit reached ({MAX_REPLY_HOPS} hops)")
        msg_id = uuid4().hex
        with self._connect() as db:
            db.execute(
                "INSERT INTO outbox(id,peer_id,content,reply_to,hops,next_attempt,created,"
                "return_channel,return_chat_id,return_session_key) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    msg_id,
                    peer_id,
                    text,
                    reply_to,
                    hops + 1,
                    time.time(),
                    time.time(),
                    return_channel,
                    return_chat_id,
                    return_session_key,
                ),
            )
        self._wake.set()
        return msg_id

    async def _receive(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5)
            if len(raw) > MAX_PEER_CONTENT_CHARS + 4096:
                raise PeerDeliveryError("peer frame is too large")
            envelope_raw: object = json.loads(raw)
            envelope = _object_mapping(envelope_raw)
            if envelope is None:
                raise PeerDeliveryError("invalid peer request")
            signature = envelope.pop("signature", None)
            result = await self._handle_envelope(envelope, signature)
            await self._write_reply(writer, result)
        except Exception as exc:
            await self._write_reply(writer, {"error": str(exc)[:200]})
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _handle_envelope(self, envelope: object, signature: object) -> dict[str, str]:
        data = _object_mapping(envelope)
        if data is None:
            raise PeerDeliveryError("invalid peer request")
        peer_id = data.get("sender")
        peer = self.peers.get(peer_id) if isinstance(peer_id, str) else None
        if (
            peer is None
            or not isinstance(signature, str)
            or not hmac.compare_digest(
                signature,
                _sign(peer.secret, data),
            )
        ):
            raise PeerDeliveryError("peer authentication failed")
        if data.get("recipient") != self.instance_id:
            raise PeerDeliveryError("peer request names a different recipient")
        if not isinstance(peer_id, str):
            raise PeerDeliveryError("peer sender is invalid")
        msg_id = data.get("id")
        content = data.get("content")
        hops = data.get("hops")
        kind = data.get("kind", "work")
        if (
            not isinstance(msg_id, str)
            or len(msg_id) > 128
            or not isinstance(content, str)
            or not content.strip()
            or len(content) > MAX_PEER_CONTENT_CHARS
            or not isinstance(hops, int)
            or isinstance(hops, bool)
            or not 1 <= hops <= MAX_REPLY_HOPS
            or kind not in {"work", "result"}
        ):
            raise PeerDeliveryError("invalid peer request")
        reply_to = data.get("reply_to")
        if reply_to is not None and (not isinstance(reply_to, str) or len(reply_to) > 128):
            raise PeerDeliveryError("invalid reply reference")
        if kind == "result":
            if not isinstance(reply_to, str):
                raise PeerDeliveryError("peer result has no correlated request")
            await self._accept_result(msg_id, peer_id, reply_to, content)
            return {"ack": msg_id}
        created = await asyncio.to_thread(
            self._store_inbox,
            msg_id,
            peer_id,
            content,
            reply_to,
            hops,
        )
        row = await asyncio.to_thread(self._get_inbox, msg_id) if created else None
        if row is not None and row["state"] == "received":
            await self._publish_inbox(row)
        return {"ack": msg_id}

    async def _receive_https(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        status, response = 400, {"error": "bad request"}
        try:
            request_line = (await asyncio.wait_for(reader.readline(), 5)).decode("ascii").strip()
            if len(request_line) > 1024:
                raise PeerDeliveryError("HTTP request line is too large")
            method, path, version = request_line.split(" ", 2)
            headers: dict[str, str] = {}
            for _ in range(32):
                line = await asyncio.wait_for(reader.readline(), 5)
                if line in {b"\r\n", b"\n", b""}:
                    break
                if len(line) > 4096:
                    raise PeerDeliveryError("HTTP header is too large")
                key, _, value = line.decode("latin1").partition(":")
                if not _:
                    raise PeerDeliveryError("malformed HTTP header")
                headers[key.strip().casefold()] = value.strip()
            else:
                raise PeerDeliveryError("too many HTTP headers")
            length = int(headers.get("content-length", "-1"))
            if method != "POST" or path != "/peer/v1/inbox" or version != "HTTP/1.1":
                status = 404
                response = {"error": "not found"}
            elif length < 0 or length > MAX_PEER_CONTENT_CHARS + 4096:
                status = 413
                response = {"error": "invalid request size"}
            else:
                raw_envelope: object = json.loads(
                    await asyncio.wait_for(reader.readexactly(length), 5)
                )
                envelope = _object_mapping(raw_envelope)
                if envelope is None:
                    raise PeerDeliveryError("invalid peer request")
                response = await self._handle_envelope(envelope, headers.get("x-moeka-signature"))
                status = 200
        except PeerDeliveryError as exc:
            status, response = 401, {"error": str(exc)[:200]}
        except Exception as exc:
            status, response = 400, {"error": str(exc)[:200]}
        body = json.dumps(response, separators=(",", ":")).encode()
        reason = {
            200: "OK",
            400: "Bad Request",
            401: "Unauthorized",
            404: "Not Found",
            413: "Payload Too Large",
        }.get(status, "Error")
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n".encode()
            + body
        )
        try:
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    def _store_inbox(
        self,
        msg_id: str,
        sender: str,
        content: str,
        reply_to: str | None,
        hops: int,
    ) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO inbox(id,sender_id,content,reply_to,hops,received,delivered) "
                "VALUES(?,?,?,?,?,?,0)",
                (msg_id, sender, content, reply_to, hops, time.time()),
            )
            return cursor.rowcount == 1

    def _get_inbox(self, msg_id: str) -> sqlite3.Row | None:
        with self._connect() as db:
            return db.execute("SELECT * FROM inbox WHERE id=?", (msg_id,)).fetchone()

    def _pending_inbox(self) -> list[sqlite3.Row]:
        with self._connect() as db:
            return list(
                db.execute(
                    "SELECT * FROM inbox WHERE state IN ('received','queued','started') ORDER BY received",
                )
            )

    async def _publish_inbox(self, row: sqlite3.Row) -> None:
        peer_id, msg_id = str(row["sender_id"]), str(row["id"])
        with self._connect() as db:
            db.execute(
                "UPDATE inbox SET state='queued' WHERE id=? AND state IN ('received','queued')",
                (msg_id,),
            )
        await self.bus.publish_inbound(
            InboundMessage(
                channel="peer",
                sender_id=peer_id,
                chat_id=msg_id,
                content=f"Paired peer @{peer_id} requests work (delivery {msg_id}):\n{row['content']}",
                session_key_override=f"peer:{peer_id}",
                input_role="user",
                metadata={
                    "peer": {
                        "id": peer_id,
                        "delivery_id": msg_id,
                        "reply_to": row["reply_to"],
                        "hops": row["hops"],
                    }
                },
            )
        )

    async def begin_inbox(self, msg_id: str) -> bool:
        """Claim work durably before tools run; false means it was already claimed."""
        return await asyncio.to_thread(self._begin_inbox, msg_id)

    def _begin_inbox(self, msg_id: str) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE inbox SET state='started' WHERE id=? AND state='queued'",
                (msg_id,),
            )
            return cursor.rowcount == 1

    async def complete_inbox(self, msg_id: str, content: str) -> None:
        row = await asyncio.to_thread(self._get_inbox, msg_id)
        if row is None or row["kind"] != "work":
            return
        text = (
            content.strip()[:MAX_PEER_CONTENT_CHARS]
            or "The peer task completed without a response."
        )
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE inbox SET state='completed',delivered=1 WHERE id=?", (msg_id,))
            if str(row["sender_id"]) in self.peers:
                result_id = "r-" + hashlib.sha256(msg_id.encode()).hexdigest()[:32]
                db.execute(
                    "INSERT OR IGNORE INTO outbox(id,peer_id,content,reply_to,hops,next_attempt,created,kind) "
                    "VALUES(?,?,?,?,?,?,?,'result')",
                    (
                        result_id,
                        row["sender_id"],
                        text,
                        msg_id,
                        row["hops"],
                        time.time(),
                        time.time(),
                    ),
                )
        self._wake.set()

    async def _accept_result(self, msg_id: str, sender: str, reply_to: str, content: str) -> None:
        with self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO peer_results(id,reply_to,sender_id,content,received) VALUES(?,?,?,?,?)",
                (msg_id, reply_to, sender, content[:MAX_PEER_CONTENT_CHARS], time.time()),
            )
            if cursor.rowcount:
                db.execute(
                    "UPDATE outbox SET state='completed',result=? WHERE id=? AND peer_id=?",
                    (content[:MAX_PEER_CONTENT_CHARS], reply_to, sender),
                )
        await self._publish_result(msg_id)

    async def _publish_pending_results(self) -> None:
        with self._connect() as db:
            rows = list(
                db.execute("SELECT id FROM peer_results WHERE published=0 ORDER BY received")
            )
        for row in rows:
            await self._publish_result(str(row["id"]))

    async def _publish_result(self, msg_id: str) -> None:
        with self._connect() as db:
            row = db.execute(
                "SELECT r.*,o.return_channel,o.return_chat_id FROM peer_results r "
                "LEFT JOIN outbox o ON o.id=r.reply_to WHERE r.id=? AND r.published=0",
                (msg_id,),
            ).fetchone()
        if row is None:
            return
        content = f"Peer @{row['sender_id']} completed your request:\n{row['content']}"
        if row["return_channel"] == "peer":
            session_key = row["return_session_key"] or f"peer:{row['sender_id']}"
            await self.bus.publish_inbound(
                InboundMessage(
                    channel="system",
                    sender_id="peer-result",
                    chat_id=f"{row['return_channel']}:{row['return_chat_id']}",
                    content=content,
                    session_key_override=str(session_key),
                    metadata={"peer_result": {"delivery_id": row["reply_to"], "result_id": msg_id}},
                )
            )
        elif row["return_channel"] and row["return_chat_id"]:
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=str(row["return_channel"]),
                    chat_id=str(row["return_chat_id"]),
                    content=content,
                    metadata={"peer_result": {"delivery_id": row["reply_to"], "result_id": msg_id}},
                )
            )
        else:
            return
        with self._connect() as db:
            db.execute("UPDATE peer_results SET published=1 WHERE id=?", (msg_id,))

    @staticmethod
    async def _write_reply(writer: asyncio.StreamWriter, data: dict[str, str]) -> None:
        writer.write((json.dumps(data, separators=(",", ":")) + "\n").encode())
        await writer.drain()

    async def _retry_loop(self) -> None:
        while True:
            rows = await asyncio.to_thread(self._pending_rows)
            for row in rows:
                await self._deliver(row)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1)
            except TimeoutError:
                pass

    def _pending_rows(self) -> list[sqlite3.Row]:
        with self._connect() as db:
            return list(
                db.execute(
                    "SELECT * FROM outbox WHERE state='pending' AND next_attempt<=? ORDER BY created LIMIT 20",
                    (time.time(),),
                )
            )

    async def _deliver(self, row: sqlite3.Row) -> None:
        peer = self.peers.get(str(row["peer_id"]))
        if peer is None:
            await asyncio.to_thread(self._mark_failed, str(row["id"]), "peer pairing removed")
            return
        envelope: dict[str, Any] = {
            "id": row["id"],
            "sender": self.instance_id,
            "recipient": peer.id,
            "content": row["content"],
            "reply_to": row["reply_to"],
            "hops": row["hops"],
            "kind": row["kind"],
        }
        envelope["signature"] = _sign(peer.secret, envelope)
        try:
            if peer.url is not None:
                reply = await self._deliver_https(peer, envelope)
            elif peer.socket_path is not None:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(peer.socket_path),
                    timeout=2,
                )
                writer.write((json.dumps(envelope, separators=(",", ":")) + "\n").encode())
                await writer.drain()
                raw_reply: object = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))
                reply = _object_mapping(raw_reply)
                if reply is None:
                    raise PeerDeliveryError("peer response is invalid")
                writer.close()
                await writer.wait_closed()
            else:
                raise PeerDeliveryError("paired peer has no transport endpoint")
            if reply.get("ack") != row["id"]:
                raise PeerDeliveryError(str(reply.get("error", "missing acknowledgement")))
            await asyncio.to_thread(self._mark_acked, str(row["id"]))
        except Exception as exc:
            await asyncio.to_thread(
                self._mark_retry, str(row["id"]), str(exc)[:200], int(row["attempts"])
            )

    @staticmethod
    async def _deliver_https(peer: PairedPeer, envelope: dict[str, Any]) -> dict[str, Any]:
        import httpx

        from nanobot.security.network import validate_url_target

        assert peer.url is not None
        allowed, reason = validate_url_target(peer.url)
        if not allowed:
            raise PeerDeliveryError(f"peer URL blocked by network policy: {reason}")
        if not peer.url.endswith("/peer/v1/inbox"):
            raise PeerDeliveryError("peer HTTPS URL must end with /peer/v1/inbox")
        signature = envelope.pop("signature")
        try:
            async with httpx.AsyncClient(
                timeout=5,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(
                    peer.url,
                    json=envelope,
                    headers={"X-Moeka-Signature": signature},
                )
            response.raise_for_status()
            result: object = response.json()
            normalized = _object_mapping(result)
            if normalized is None:
                raise PeerDeliveryError("peer HTTPS response is invalid")
            return normalized
        finally:
            envelope["signature"] = signature

    def _mark_acked(self, msg_id: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE outbox SET state='acked',last_error=NULL WHERE id=?", (msg_id,))

    def _mark_failed(self, msg_id: str, error: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE outbox SET state='failed',last_error=? WHERE id=?", (error, msg_id))

    def _mark_retry(self, msg_id: str, error: str, attempts: int) -> None:
        new_attempts = attempts + 1
        state = "failed" if new_attempts >= MAX_ATTEMPTS else "pending"
        delay = min(60, 2 ** min(new_attempts, 6))
        with self._connect() as db:
            db.execute(
                "UPDATE outbox SET state=?,attempts=?,next_attempt=?,last_error=? WHERE id=?",
                (state, new_attempts, time.time() + delay, error, msg_id),
            )


def _sign(secret: str, envelope: dict[str, Any]) -> str:
    canonical = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def _object_mapping(value: object) -> dict[str, object] | None:
    """Validate the keys of decoded JSON before values cross the trust boundary."""
    if not isinstance(value, dict):
        return None
    raw_dict = cast(dict[object, object], value)
    result: dict[str, object] = {}
    for key, item in raw_dict.items():
        if not isinstance(key, str):
            return None
        result[key] = item
    return result

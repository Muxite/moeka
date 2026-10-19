from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from nanobot.config.loader import load_config, resolve_config_env_vars
from nanobot.config.schema import Config, PeerConfig
from nanobot.peers import MAX_REPLY_HOPS, PeerDeliveryError, PeerService, _sign


class Bus:
    def __init__(self) -> None:
        self.messages = []
        self.outbound = []

    async def publish_inbound(self, message) -> None:
        self.messages.append(message)

    async def publish_outbound(self, message) -> None:
        self.outbound.append(message)


@pytest.mark.asyncio
async def test_paired_delivery_ack_retry_and_deduplication(tmp_path: Path) -> None:
    secret = "owner-shared-secret-0123456789abcdef"
    socket_a, socket_b = tmp_path / "a.sock", tmp_path / "b.sock"
    bus_a, bus_b = Bus(), Bus()
    a = PeerService(
        instance_id="a" * 32,
        peers=[PeerConfig(id="b" * 32, socket_path=str(socket_b), secret=secret)],
        state_dir=tmp_path / "a",
        socket_path=socket_a,
        bus=bus_a,
    )
    b = PeerService(
        instance_id="b" * 32,
        peers=[PeerConfig(id="a" * 32, socket_path=str(socket_a), secret=secret)],
        state_dir=tmp_path / "b",
        socket_path=socket_b,
        bus=bus_b,
    )
    await a.start()
    delivery_id = await a.enqueue("b" * 32, "Please inspect the release notes")
    await b.start()
    try:
        for _ in range(50):
            with sqlite3.connect(a.db_path) as db:
                row = db.execute("SELECT state FROM outbox WHERE id=?", (delivery_id,)).fetchone()
            if row and row[0] == "acked":
                break
            await asyncio.sleep(0.05)
        assert row == ("acked",)
        assert len(bus_b.messages) == 1
        inbound = bus_b.messages[0]
        assert inbound.channel == "peer"
        assert inbound.session_key == "peer:" + "a" * 32
        assert inbound.metadata["peer"]["delivery_id"] == delivery_id

        envelope = {
            "id": delivery_id,
            "sender": "a" * 32,
            "recipient": "b" * 32,
            "content": "Please inspect the release notes",
            "reply_to": None,
            "hops": 1,
        }
        from nanobot.peers import _sign

        envelope["signature"] = _sign(secret, {key: val for key, val in envelope.items()})
        reader, writer = await asyncio.open_unix_connection(socket_b)
        writer.write((__import__("json").dumps(envelope) + "\n").encode())
        await writer.drain()
        assert __import__("json").loads(await reader.readline())["ack"] == delivery_id
        writer.close()
        await writer.wait_closed()
        assert len(bus_b.messages) == 1
    finally:
        await a.stop()
        await b.stop()


@pytest.mark.asyncio
async def test_peer_reply_chain_is_bounded_and_unpaired_peers_rejected(tmp_path: Path) -> None:
    service = PeerService(
        instance_id="a" * 32,
        peers=[PeerConfig(id="b" * 32, socket_path=str(tmp_path / "b.sock"), secret="x" * 32)],
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "a.sock",
        bus=Bus(),
    )
    with pytest.raises(PeerDeliveryError, match="not paired"):
        await service.enqueue("unknown", "hello")
    with pytest.raises(PeerDeliveryError, match="reply limit"):
        await service.enqueue("b" * 32, "hello", hops=MAX_REPLY_HOPS)


def test_peer_secret_environment_reference_resolves_after_config_load(
    monkeypatch,
    tmp_path: Path,
) -> None:
    secret = "owner-shared-secret-0123456789abcdef"
    monkeypatch.setenv("MOEKA_PEER_SECRET", secret)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "peers": [
                    {
                        "id": "b" * 32,
                        "socketPath": str(tmp_path / "b.sock"),
                        "secret": "${MOEKA_PEER_SECRET}",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    raw = load_config(config_path)
    assert raw.peers[0].secret == "${MOEKA_PEER_SECRET}"
    resolved = resolve_config_env_vars(raw)
    assert resolved.peers[0].secret == secret
    service = PeerService(
        instance_id="a" * 32,
        peers=resolved.peers,
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "a.sock",
        bus=Bus(),
    )
    assert service.peers["b" * 32].secret == secret


@pytest.mark.parametrize("env_value", [None, "short"])
def test_peer_service_rejects_unresolved_or_weak_secret(
    monkeypatch,
    tmp_path: Path,
    env_value: str | None,
) -> None:
    if env_value is None:
        monkeypatch.delenv("MISSING_MOEKA_PEER_SECRET", raising=False)
    else:
        monkeypatch.setenv("MISSING_MOEKA_PEER_SECRET", env_value)
    config = Config.model_validate(
        {
            "peers": [
                {
                    "id": "b" * 32,
                    "socketPath": str(tmp_path / "b.sock"),
                    "secret": "${MISSING_MOEKA_PEER_SECRET}",
                }
            ],
        }
    )
    resolved = resolve_config_env_vars(config)
    with pytest.raises(PeerDeliveryError, match="unresolved or too short"):
        PeerService(
            instance_id="a" * 32,
            peers=resolved.peers,
            state_dir=tmp_path / "state",
            socket_path=tmp_path / "a.sock",
            bus=Bus(),
        )


@pytest.mark.asyncio
async def test_https_peer_delivery_keeps_tls_verification_and_signs_request(
    monkeypatch,
    tmp_path: Path,
) -> None:
    secret = "owner-shared-secret-0123456789abcdef"
    peer = PeerConfig(
        id="b" * 32,
        url="https://peer.example/peer/v1/inbox",
        secret=secret,
    )
    service = PeerService(
        instance_id="a" * 32,
        peers=[peer],
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "peer.sock",
        bus=Bus(),
    )
    received: dict[str, object] = {}
    client_options: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"ack": "delivery-1"}

    class Client:
        def __init__(self, **kwargs) -> None:
            client_options.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, url, *, json, headers):
            received.update({"url": url, "body": dict(json), "headers": dict(headers)})
            return Response()

    def validate(_url: str) -> tuple[bool, str]:
        return True, ""

    monkeypatch.setattr("httpx.AsyncClient", Client)
    monkeypatch.setattr("nanobot.security.network.validate_url_target", validate)
    payload = {
        "id": "delivery-1",
        "sender": "a" * 32,
        "recipient": "b" * 32,
        "content": "Work",
        "reply_to": None,
        "hops": 1,
        "kind": "work",
    }
    envelope = {**payload, "signature": _sign(secret, payload)}
    assert await service._deliver_https(service.peers["b" * 32], envelope) == {"ack": "delivery-1"}
    assert client_options.get("verify", True) is True
    assert client_options["follow_redirects"] is False
    assert received["url"] == "https://peer.example/peer/v1/inbox"
    assert received["body"] == payload
    assert received["headers"] == {"X-Moeka-Signature": envelope["signature"]}


@pytest.mark.asyncio
async def test_queued_inbox_recovers_but_started_work_is_reported_not_replayed(
    tmp_path: Path,
) -> None:
    secret = "owner-shared-secret-0123456789abcdef"
    sender_id, recipient_id = "a" * 32, "b" * 32
    recipient_peer = PeerConfig(id=sender_id, socket_path=str(tmp_path / "a.sock"), secret=secret)
    first_bus = Bus()
    first = PeerService(
        instance_id=recipient_id,
        peers=[recipient_peer],
        state_dir=tmp_path / "recipient",
        socket_path=tmp_path / "b.sock",
        bus=first_bus,
    )
    payload = {
        "id": "task-1",
        "sender": sender_id,
        "recipient": recipient_id,
        "content": "Run a bounded check",
        "reply_to": None,
        "hops": 1,
        "kind": "work",
    }
    await first.start()
    try:
        ack = await first._handle_envelope(payload, _sign(secret, payload))
        assert ack == {"ack": "task-1"}
        assert len(first_bus.messages) == 1
        with sqlite3.connect(first.db_path) as db:
            assert db.execute("SELECT state FROM inbox WHERE id='task-1'").fetchone() == ("queued",)
    finally:
        await first.stop()

    restarted_bus = Bus()
    restarted = PeerService(
        instance_id=recipient_id,
        peers=[recipient_peer],
        state_dir=tmp_path / "recipient",
        socket_path=tmp_path / "b.sock",
        bus=restarted_bus,
    )
    await restarted.start()
    try:
        assert len(restarted_bus.messages) == 1
        assert await restarted.begin_inbox("task-1")
        assert not await restarted.begin_inbox("task-1")
    finally:
        await restarted.stop()

    recovered_bus = Bus()
    recovered = PeerService(
        instance_id=recipient_id,
        peers=[recipient_peer],
        state_dir=tmp_path / "recipient",
        socket_path=tmp_path / "b.sock",
        bus=recovered_bus,
    )
    await recovered.start()
    try:
        assert recovered_bus.messages == []  # never executes potentially repeated effects
        with sqlite3.connect(recovered.db_path) as db:
            assert db.execute("SELECT state FROM inbox WHERE id='task-1'").fetchone() == (
                "completed",
            )
            assert db.execute("SELECT kind,reply_to FROM outbox").fetchone() == ("result", "task-1")
    finally:
        await recovered.stop()


@pytest.mark.asyncio
async def test_correlated_peer_result_is_delivered_to_requesting_channel(tmp_path: Path) -> None:
    secret = "owner-shared-secret-0123456789abcdef"
    socket_a, socket_b = tmp_path / "a.sock", tmp_path / "b.sock"
    bus_a, bus_b = Bus(), Bus()
    a = PeerService(
        instance_id="a" * 32,
        peers=[PeerConfig(id="b" * 32, socket_path=str(socket_b), secret=secret)],
        state_dir=tmp_path / "a",
        socket_path=socket_a,
        bus=bus_a,
    )
    b = PeerService(
        instance_id="b" * 32,
        peers=[PeerConfig(id="a" * 32, socket_path=str(socket_a), secret=secret)],
        state_dir=tmp_path / "b",
        socket_path=socket_b,
        bus=bus_b,
    )
    await a.start()
    await b.start()
    try:
        work_id = await a.enqueue(
            "b" * 32,
            "Check the release notes",
            return_channel="telegram",
            return_chat_id="owner-chat",
            return_session_key="telegram:owner-chat",
        )
        for _ in range(50):
            with sqlite3.connect(a.db_path) as db:
                row = db.execute("SELECT state FROM outbox WHERE id=?", (work_id,)).fetchone()
            if row and row[0] == "acked":
                break
            await asyncio.sleep(0.05)
        assert row == ("acked",)
        request = bus_b.messages[0]
        peer_meta = request.metadata["peer"]
        assert await b.begin_inbox(peer_meta["delivery_id"])
        await b.complete_inbox(peer_meta["delivery_id"], "The notes mention the API change.")
        for _ in range(50):
            if bus_a.outbound:
                break
            await asyncio.sleep(0.05)
        assert len(bus_a.outbound) == 1
        assert bus_a.outbound[0].channel == "telegram"
        assert bus_a.outbound[0].chat_id == "owner-chat"
        assert "API change" in bus_a.outbound[0].content
        with sqlite3.connect(a.db_path) as db:
            assert db.execute(
                "SELECT state,result FROM outbox WHERE id=?", (work_id,)
            ).fetchone() == (
                "completed",
                "The notes mention the API change.",
            )
        assert bus_a.messages == []  # results never become new peer work
    finally:
        await a.stop()
        await b.stop()


def _single_peer_service(tmp_path: Path, bus: Bus) -> PeerService:
    return PeerService(
        instance_id="b" * 32,
        peers=[
            PeerConfig(
                id="a" * 32,
                socket_path=str(tmp_path / "a.sock"),
                secret="owner-shared-secret-0123456789abcdef",
            )
        ],
        state_dir=tmp_path / "b",
        socket_path=tmp_path / "b.sock",
        bus=bus,
    )


@pytest.mark.asyncio
async def test_concurrent_publishes_of_one_received_row_reach_the_bus_once(
    tmp_path: Path,
) -> None:
    bus = Bus()
    service = _single_peer_service(tmp_path, bus)
    assert service._store_inbox("delivery-1", "a" * 32, "Check the logs", None, 1)
    row = service._get_inbox("delivery-1")

    # Startup recovery and the live receive path can both hold the same
    # 'received' row; only the one that claims it may publish.
    await asyncio.gather(service._publish_inbox(row), service._publish_inbox(row))

    assert len(bus.messages) == 1


@pytest.mark.asyncio
async def test_startup_recovery_scans_inbox_before_accepting_connections(
    tmp_path: Path,
) -> None:
    service = _single_peer_service(tmp_path, Bus())
    listening_during_scan: list[bool] = []
    scan = service._pending_inbox

    def recording_scan():
        listening_during_scan.append(service._server is not None)
        return scan()

    service._pending_inbox = recording_scan
    await service.start()
    try:
        assert listening_during_scan == [False]
    finally:
        await service.stop()

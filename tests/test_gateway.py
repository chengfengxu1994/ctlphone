import json
import threading
from pathlib import Path

import pytest

from phone_ctl.adb import UINode
from phone_ctl.gateway import DeviceGateway, GatewayRPCError, GatewayRequestHandler, ThreadingUnixServer
from phone_ctl.gateway_client import GatewayClient, GatewayError, GatewayPhone


class FakePhone:
    def __init__(self): self.taps = []
    def devices(self): return ["test-device"]
    def tap(self, x, y): self.taps.append((x, y))
    def ui_dump(self): return [UINode(0, "Play", "", "Button", True, (0, 0, 20, 20))]


def test_conflict_reports_owner_and_audit_omits_params(tmp_path):
    gateway = DeviceGateway(FakePhone(), tmp_path / "audit.jsonl")
    first = gateway.acquire("project-a", 30, 10, "soak")
    with pytest.raises(GatewayRPCError) as caught:
        gateway.acquire("project-b", 30, 11, "other")
    assert caught.value.code == "DEVICE_BUSY"
    assert caught.value.details["owner"]["project"] == "project-a"
    gateway.execute(first["token"], "tap", {"x": 123, "y": 456, "private_note": "redacted-value"})
    text = (tmp_path / "audit.jsonl").read_text()
    assert '"operation":"tap"' in text
    assert "redacted-value" not in text and "123" not in text


def test_expired_lease_can_be_replaced(tmp_path):
    gateway = DeviceGateway(FakePhone(), tmp_path / "audit.jsonl")
    gateway.acquire("old", 5, None, "")
    gateway._lease.expires_at = 0
    result = gateway.acquire("new", 5, None, "")
    assert result["lease"]["project"] == "new"


def test_socket_client_and_proxy(tmp_path):
    socket_path = tmp_path / "gateway.sock"
    fake = FakePhone()
    server = ThreadingUnixServer(str(socket_path), GatewayRequestHandler)
    server.gateway = DeviceGateway(fake, tmp_path / "audit.jsonl")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = GatewayClient(socket_path, auto_start=False)
        phone = GatewayPhone(project="tests", client=client)
        phone.tap(4, 7)
        assert fake.taps == [(4, 7)]
        assert phone.ui_dump()[0].text == "Play"
        phone.close()
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_client_busy_message_contains_owner(tmp_path):
    gateway = DeviceGateway(FakePhone(), tmp_path / "audit.jsonl")
    gateway.acquire("owner", 30, None, "test")
    response = gateway.handle({"id": "1", "version": 1, "method": "acquire", "params": {
        "project": "waiter", "ttl_seconds": 30,
    }})
    assert response["error"]["code"] == "DEVICE_BUSY"
    assert response["error"]["details"]["owner"]["project"] == "owner"


def test_broker_claim_blocks_execute_and_hides_device(tmp_path):
    import fcntl

    claim_dir = tmp_path / "claim"
    claim_dir.mkdir()
    phone = FakePhone()
    gateway = DeviceGateway(phone, tmp_path / "audit.jsonl", claim_dir)
    held = open(claim_dir / "broker.lock", "a+")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX)
    try:
        token = gateway.acquire("agent", 30, None, "")["token"]
        with pytest.raises(GatewayRPCError) as caught:
            gateway.execute(token, "tap", {"x": 1, "y": 2})
        assert caught.value.code == "DEVICE_BUSY"
        assert caught.value.details["owner"]["project"] == "phonebroker"
        assert caught.value.details["retry_after_seconds"] == 2
        status = gateway.status()
        assert status["broker_active"] is True
        assert status["device_count"] == 0
        assert phone.taps == []
    finally:
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()
    assert gateway.status()["broker_active"] is False
    gateway.execute(token, "tap", {"x": 1, "y": 2})
    assert phone.taps == [(1, 2)]

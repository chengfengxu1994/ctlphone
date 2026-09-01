import os

from phone_ctl.device_claim import (
    BROKER_MARKER_NAME,
    BrokerDeviceClaim,
    GatewayDeviceClaim,
    broker_claim_active,
)
import time


def test_claim_roundtrip(tmp_path):
    assert broker_claim_active(tmp_path) is False
    assert not (tmp_path / BROKER_MARKER_NAME).exists()
    with BrokerDeviceClaim(tmp_path) as claim:
        assert claim is not None
        assert broker_claim_active(tmp_path) is True
        assert (tmp_path / BROKER_MARKER_NAME).exists()
    assert broker_claim_active(tmp_path) is False
    assert not (tmp_path / BROKER_MARKER_NAME).exists()


def test_claim_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("PHONE_CLAIM_DIR", str(tmp_path / "custom"))
    from phone_ctl.device_claim import claim_dir

    assert claim_dir() == tmp_path / "custom"
    assert broker_claim_active() is False
    assert os.listdir(tmp_path / "custom")


def test_broker_waits_for_gateway_shared_claim_and_times_out(tmp_path):
    with GatewayDeviceClaim(tmp_path) as gateway:
        assert gateway is not None
        assert broker_claim_active(tmp_path) is False
        started = time.monotonic()
        with BrokerDeviceClaim(tmp_path, timeout=0.05) as broker:
            assert broker is None
        assert time.monotonic() - started >= 0.04
    with BrokerDeviceClaim(tmp_path, timeout=0.05) as broker:
        assert broker is not None


def test_claim_rejects_world_writable_directory_and_symlink(tmp_path):
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    assert broker_claim_active(unsafe) is None
    safe = tmp_path / "safe"
    safe.mkdir()
    target = tmp_path / "target"
    target.write_text("")
    (safe / "broker.lock").symlink_to(target)
    assert broker_claim_active(safe) is None

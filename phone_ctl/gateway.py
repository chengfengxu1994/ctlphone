"""Single-owner Unix-socket gateway for every Android control operation."""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import secrets
import signal
import socket
import socketserver
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .adb import ADBError, Phone
from .device_claim import GatewayDeviceClaim, broker_claim_active
from .device_unlock import UnlockError, lock_state, unlock_pattern
from .log_capture import capture_app_logs

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
MIN_LEASE_SECONDS = 5.0
MAX_LEASE_SECONDS = 3600.0
DEFAULT_LEASE_SECONDS = 30.0
PROJECT_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,120}$")
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]*$")


def gateway_runtime_dir() -> Path:
    runtime_root = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime_root) if runtime_root else Path(f"/tmp/ctlphone-{os.getuid()}")
    return base / "ctlphone"


def gateway_socket_path() -> Path:
    configured = os.environ.get("PHONE_GATEWAY_SOCKET")
    return Path(configured) if configured else gateway_runtime_dir() / "gateway.sock"


def gateway_audit_path() -> Path:
    configured = os.environ.get("PHONE_GATEWAY_AUDIT")
    if configured:
        return Path(configured)
    state_root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    return state_root / "ctlphone" / "gateway-audit.jsonl"


class GatewayRPCError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass
class Lease:
    token: str
    project: str
    acquired_at: float
    expires_at: float
    ttl_seconds: float
    client_pid: int | None = None
    purpose: str = ""
    last_operation: str = ""
    operation_count: int = 0
    cleanups: list[dict[str, Any]] = field(default_factory=list)

    def public(self, now: float | None = None) -> dict[str, Any]:
        current = time.time() if now is None else now
        return {
            "project": self.project,
            "purpose": self.purpose,
            "client_pid": self.client_pid,
            "acquired_at": self.acquired_at,
            "expires_at": self.expires_at,
            "expires_in_seconds": round(max(0.0, self.expires_at - current), 3),
            "ttl_seconds": self.ttl_seconds,
            "last_operation": self.last_operation,
            "operation_count": self.operation_count,
        }


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def write(self, event: str, **fields: Any) -> None:
        record = {"timestamp": time.time(), "event": event, **fields}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            os.chmod(self.path, 0o600)

    def tail(self, limit: int) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
        result = []
        for line in lines:
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    result.append(value)
            except json.JSONDecodeError:
                continue
        return result


class DeviceGateway:
    def __init__(self, phone: Phone | None = None, audit_path: Path | None = None,
                 claim_directory: Path | None = None):
        self.phone = phone or Phone()
        self.audit = AuditLog(audit_path or gateway_audit_path())
        self.started_at = time.time()
        self._lease: Lease | None = None
        self._state_lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._claim_dir = claim_directory
        self._watcher_stop = threading.Event()

    def _expire_if_needed(self) -> None:
        with self._state_lock:
            if self._lease and self._lease.expires_at <= time.time():
                expired = self._lease
                self._lease = None
                self.audit.write("lease_expired", project=expired.project,
                                 last_operation=expired.last_operation)

    def _lease_details(self) -> dict[str, Any] | None:
        self._expire_if_needed()
        with self._state_lock:
            return self._lease.public() if self._lease else None

    def _broker_active(self) -> bool:
        """True while phonebroker holds the device claim.

        The claim probe itself is fail-closed: when the shared claim
        location is unusable we treat the credential boundary as active so
        gateway-side adb calls can never yank the device mid-login.
        """
        return broker_claim_active(self._claim_dir) is not False

    def _require_device_available(self) -> None:
        if self._broker_active():
            raise GatewayRPCError(
                "DEVICE_BUSY",
                "phonebroker credential boundary owns the phone",
                {"owner": {"project": "phonebroker", "purpose": "broker request",
                           "expires_in_seconds": 2},
                 "retry_after_seconds": 2},
            )

    @contextmanager
    def _gateway_claim(self):
        with GatewayDeviceClaim(self._claim_dir) as claim:
            if claim is None:
                self._require_device_available()
                raise GatewayRPCError("DEVICE_BUSY", "phone device claim is unavailable",
                                      {"retry_after_seconds": 2})
            yield claim

    def _kill_local_adb_server(self) -> None:
        adb_path = getattr(self.phone, "adb_path", "adb")
        try:
            subprocess.run([adb_path, "kill-server"], check=False, timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass

    def _watch_broker_claims(self) -> None:
        """Yield the USB device to phonebroker while its claim is held.

        Killing the local adb server is serialized behind _operation_lock so
        it can never interrupt an in-flight gateway operation; while the
        claim is active, execute()/status() refuse to touch adb at all, so
        no new local server starts until the broker releases.
        """
        state: bool | None = None
        while not self._watcher_stop.wait(0.5):
            claimed = self._broker_active()
            if claimed:
                with self._operation_lock:
                    self._kill_local_adb_server()
            if claimed != state:
                state = claimed
                self.audit.write("broker_claimed" if claimed else "broker_released")

    def start_broker_watch(self) -> None:
        thread = threading.Thread(target=self._watch_broker_claims,
                                  name="broker-claim-watch", daemon=True)
        thread.start()

    def stop_broker_watch(self) -> None:
        self._watcher_stop.set()

    def status(self) -> dict[str, Any]:
        devices: list[str] = []
        device_error = ""
        broker_active = self._broker_active()
        if not broker_active:
            try:
                with self._operation_lock, self._gateway_claim():
                    devices = self.phone.devices()
            except Exception as exc:
                device_error = str(exc)
        return {
            "protocol_version": PROTOCOL_VERSION,
            "gateway_pid": os.getpid(),
            "started_at": self.started_at,
            "uptime_seconds": round(time.time() - self.started_at, 3),
            "socket": str(gateway_socket_path()),
            "device_count": len(devices),
            "devices": devices,
            "device_error": device_error,
            "broker_active": broker_active,
            "lease": self._lease_details(),
        }

    def acquire(self, project: str, ttl_seconds: float, client_pid: int | None,
                purpose: str) -> dict[str, Any]:
        if not PROJECT_RE.fullmatch(project):
            raise GatewayRPCError("INVALID_PROJECT", "project must use safe identifier characters")
        requested_ttl = float(ttl_seconds)
        if not math.isfinite(requested_ttl):
            raise GatewayRPCError("INVALID_TTL", "lease TTL must be a finite number")
        ttl = max(MIN_LEASE_SECONDS, min(MAX_LEASE_SECONDS, requested_ttl))
        now = time.time()
        self._expire_if_needed()
        with self._state_lock:
            if self._lease:
                raise GatewayRPCError(
                    "DEVICE_BUSY",
                    f"phone is controlled by project {self._lease.project!r}",
                    {"owner": self._lease.public(now), "retry_after_seconds":
                     round(max(0.0, self._lease.expires_at - now), 3)},
                )
            self._lease = Lease(
                token=secrets.token_urlsafe(24),
                project=project,
                acquired_at=now,
                expires_at=now + ttl,
                ttl_seconds=ttl,
                client_pid=client_pid,
                purpose=purpose[:200],
            )
            self.audit.write("lease_acquired", project=project, purpose=purpose[:200],
                             client_pid=client_pid, ttl_seconds=ttl)
            return {"token": self._lease.token, "lease": self._lease.public(now)}

    def _require_lease(self, token: str) -> Lease:
        self._expire_if_needed()
        with self._state_lock:
            if not self._lease:
                raise GatewayRPCError("LEASE_EXPIRED", "no active phone-control lease")
            if not secrets.compare_digest(self._lease.token, token):
                raise GatewayRPCError("INVALID_TOKEN", "lease token does not own the phone",
                                      {"owner": self._lease.public()})
            return self._lease

    def renew(self, token: str, ttl_seconds: float | None = None) -> dict[str, Any]:
        with self._state_lock:
            lease = self._require_lease(token)
            if ttl_seconds is not None:
                lease.ttl_seconds = max(MIN_LEASE_SECONDS,
                                        min(MAX_LEASE_SECONDS, float(ttl_seconds)))
            lease.expires_at = time.time() + lease.ttl_seconds
            return {"lease": lease.public()}

    def release(self, token: str) -> dict[str, Any]:
        with self._state_lock:
            lease = self._require_lease(token)
            public = lease.public()
            self._lease = None
            self.audit.write("lease_released", project=lease.project,
                             operations=lease.operation_count,
                             last_operation=lease.last_operation)
            return {"released": True, "lease": public}

    def execute(self, token: str, operation: str, params: dict[str, Any]) -> Any:
        if not isinstance(params, dict):
            raise GatewayRPCError("INVALID_REQUEST", "operation_params must be an object")
        lease = self._require_lease(token)
        started = time.perf_counter()
        success = False
        try:
            with self._operation_lock, self._gateway_claim():
                lease = self._require_lease(token)
                lease.expires_at = time.time() + lease.ttl_seconds
                result = self._dispatch(operation, params)
                lease.last_operation = operation
                lease.operation_count += 1
                lease.expires_at = time.time() + lease.ttl_seconds
                success = True
                return result
        except GatewayRPCError:
            raise
        except (ADBError, UnlockError, ValueError, TypeError, TimeoutError) as exc:
            raise GatewayRPCError("OPERATION_FAILED", str(exc)) from exc
        finally:
            self.audit.write(
                "operation",
                project=lease.project,
                operation=operation,
                success=success,
                elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
            )

    def _dispatch(self, operation: str, params: dict[str, Any]) -> Any:
        phone = self.phone
        if operation == "devices":
            return phone.devices()
        if operation == "screenshot_png":
            return {"base64": base64.b64encode(phone.screenshot_png()).decode("ascii")}
        if operation == "screen_size":
            return list(phone.screen_size())
        if operation == "display_size":
            return list(phone.display_size())
        if operation == "tap":
            phone.tap(int(params["x"]), int(params["y"]))
            return None
        if operation == "long_press":
            phone.long_press(int(params["x"]), int(params["y"]), int(params.get("duration_ms", 1000)))
            return None
        if operation == "swipe":
            phone.swipe(int(params["x1"]), int(params["y1"]), int(params["x2"]),
                        int(params["y2"]), int(params.get("duration_ms", 300)))
            return None
        if operation == "input_text":
            phone.input_text(str(params["text"]))
            return None
        if operation == "press_key":
            phone.press_key(params["key"])
            return None
        if operation == "ui_dump":
            return [asdict(node) for node in phone.ui_dump()]
        if operation == "tap_text":
            return asdict(phone.tap_text(str(params["query"]), int(params.get("index", 0))))
        if operation == "launch_app":
            phone.launch_app(str(params["package"]))
            return None
        if operation == "current_app":
            return phone.current_app()
        if operation == "list_apps":
            return phone.list_apps(str(params.get("keyword", "")),
                                   bool(params.get("third_party_only", True)))
        if operation == "shell":
            return phone.shell(str(params["command"]), timeout=int(params.get("timeout", 60)))
        if operation == "lock_state":
            return lock_state(phone)
        if operation == "unlock_pattern":
            # The pattern is intentionally used only here and is never audited.
            return unlock_pattern(phone, str(params["pattern"]))
        if operation == "capture_app_logs":
            return capture_app_logs(
                phone,
                str(params["package"]),
                limit=int(params.get("limit", 500)),
                min_priority=str(params.get("min_priority", "V")),
                include_crash=bool(params.get("include_crash", True)),
            )
        raise GatewayRPCError("UNKNOWN_OPERATION", f"unsupported operation {operation!r}")

    def doctor(self) -> dict[str, Any]:
        result = self.status()
        if result["device_count"] == 1:
            try:
                with self._operation_lock, self._gateway_claim():
                    result["model"] = self.phone.shell("getprop ro.product.model").strip()
                    result["android_api"] = self.phone.shell("getprop ro.build.version.sdk").strip()
                    result["display_size"] = list(self.phone.display_size())
                    result["lock_state"] = lock_state(self.phone)
            except Exception as exc:
                result["device_error"] = str(exc)
        return result

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        try:
            if request.get("version") != PROTOCOL_VERSION:
                raise GatewayRPCError("PROTOCOL_MISMATCH", "unsupported gateway protocol version")
            method = request.get("method")
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise GatewayRPCError("INVALID_REQUEST", "params must be an object")
            if method == "status":
                result = self.status()
            elif method == "doctor":
                result = self.doctor()
            elif method == "audit_tail":
                result = self.audit.tail(max(1, min(500, int(params.get("limit", 50)))))
            elif method == "acquire":
                result = self.acquire(str(params.get("project", "")),
                                      float(params.get("ttl_seconds", DEFAULT_LEASE_SECONDS)),
                                      params.get("client_pid"), str(params.get("purpose", "")))
            elif method == "renew":
                result = self.renew(str(params.get("token", "")), params.get("ttl_seconds"))
            elif method == "release":
                result = self.release(str(params.get("token", "")))
            elif method == "execute":
                result = self.execute(str(params.get("token", "")),
                                      str(params.get("operation", "")),
                                      params.get("operation_params", {}))
            else:
                raise GatewayRPCError("UNKNOWN_METHOD", f"unsupported method {method!r}")
            return {"id": request_id, "ok": True, "result": result}
        except GatewayRPCError as exc:
            return {"id": request_id, "ok": False, "error": {
                "code": exc.code, "message": str(exc), "details": exc.details,
            }}
        except Exception as exc:
            return {"id": request_id, "ok": False, "error": {
                "code": "INTERNAL_ERROR", "message": str(exc), "details": {},
            }}


class GatewayRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline(MAX_MESSAGE_BYTES + 1)
        if len(line) > MAX_MESSAGE_BYTES:
            response = {"id": None, "ok": False, "error": {
                "code": "REQUEST_TOO_LARGE", "message": "request exceeds size limit", "details": {},
            }}
        else:
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("request must be an object")
                response = self.server.gateway.handle(request)  # type: ignore[attr-defined]
            except Exception as exc:
                response = {"id": None, "ok": False, "error": {
                    "code": "INVALID_JSON", "message": str(exc), "details": {},
                }}
        self.wfile.write(json.dumps(response, ensure_ascii=False,
                                    separators=(",", ":")).encode() + b"\n")


class ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


def serve(socket_path: Path | None = None) -> None:
    path = socket_path or gateway_socket_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    if path.exists():
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.3)
            probe.connect(str(path))
            raise RuntimeError(f"gateway already running at {path}")
        except (ConnectionRefusedError, FileNotFoundError, socket.timeout):
            path.unlink()
        finally:
            probe.close()
    gateway = DeviceGateway()
    gateway.start_broker_watch()
    with ThreadingUnixServer(str(path), GatewayRequestHandler) as server:
        server.gateway = gateway  # type: ignore[attr-defined]
        os.chmod(path, 0o600)
        gateway.audit.write("gateway_started", pid=os.getpid(), socket=str(path))
        def stop_handler(_signum, _frame) -> None:
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, stop_handler)
        signal.signal(signal.SIGINT, stop_handler)
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            gateway.stop_broker_watch()
            gateway.audit.write("gateway_stopped", pid=os.getpid())
            if path.exists():
                path.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ctlphone single-owner gateway")
    parser.add_argument("--socket", type=Path)
    args = parser.parse_args(argv)
    serve(args.socket)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

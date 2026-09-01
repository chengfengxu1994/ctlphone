"""Client and Phone-compatible proxy for the ctlphone gateway."""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .adb import ADBError, UINode
from .gateway import DEFAULT_LEASE_SECONDS, PROTOCOL_VERSION, gateway_socket_path


class GatewayError(ADBError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def __str__(self) -> str:
        if self.code == "DEVICE_BUSY" and self.details.get("owner"):
            owner = self.details["owner"]
            return (
                f"DEVICE_BUSY: project {owner.get('project')!r} owns the phone; "
                f"purpose={owner.get('purpose')!r}, last_operation={owner.get('last_operation')!r}, "
                f"retry in {self.details.get('retry_after_seconds')}s"
            )
        return f"{self.code}: {super().__str__()}"


class GatewayClient:
    def __init__(self, socket_path: Path | None = None, *, auto_start: bool = True):
        self.socket_path = socket_path or gateway_socket_path()
        self.auto_start = auto_start

    def _request(self, method: str, params: dict[str, Any] | None = None,
                 timeout: float = 70.0) -> Any:
        request = {
            "id": uuid.uuid4().hex[:16],
            "version": PROTOCOL_VERSION,
            "method": method,
            "params": params or {},
        }
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(timeout)
                connection.connect(str(self.socket_path))
                connection.sendall(json.dumps(request, ensure_ascii=False,
                                              separators=(",", ":")).encode() + b"\n")
                stream = connection.makefile("rb")
                line = stream.readline(16 * 1024 * 1024 + 1)
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            if self.auto_start:
                self.ensure_running()
                return self._request(method, params, timeout)
            raise GatewayError("GATEWAY_UNAVAILABLE",
                               f"gateway is not running at {self.socket_path}") from exc
        except socket.timeout as exc:
            raise GatewayError("GATEWAY_TIMEOUT", f"gateway request {method!r} timed out") from exc
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GatewayError("INVALID_RESPONSE", "gateway returned invalid JSON") from exc
        if not response.get("ok"):
            error = response.get("error", {})
            raise GatewayError(str(error.get("code", "GATEWAY_ERROR")),
                               str(error.get("message", "gateway request failed")),
                               error.get("details", {}))
        return response.get("result")

    def ensure_running(self, timeout: float = 4.0) -> dict[str, Any]:
        try:
            return self.status(auto_start=False)
        except GatewayError:
            pass
        managed = subprocess.run(
            ["systemctl", "--user", "start", "ctlphone-gateway.service"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
        if not managed:
            subprocess.Popen(
                [sys.executable, "-m", "phone_ctl.gateway"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
            )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                return self.status(auto_start=False)
            except GatewayError:
                time.sleep(0.05)
        raise GatewayError("GATEWAY_START_FAILED", f"gateway did not start at {self.socket_path}")

    def status(self, *, auto_start: bool | None = None) -> dict[str, Any]:
        previous = self.auto_start
        if auto_start is not None:
            self.auto_start = auto_start
        try:
            return self._request("status", timeout=5)
        finally:
            self.auto_start = previous

    def doctor(self) -> dict[str, Any]:
        return self._request("doctor", timeout=15)

    def audit_tail(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._request("audit_tail", {"limit": limit}, timeout=5)

    def acquire(self, project: str, ttl_seconds: float = DEFAULT_LEASE_SECONDS,
                purpose: str = "") -> dict[str, Any]:
        return self._request("acquire", {
            "project": project,
            "ttl_seconds": ttl_seconds,
            "client_pid": os.getpid(),
            "purpose": purpose,
        }, timeout=10)

    def renew(self, token: str, ttl_seconds: float | None = None) -> dict[str, Any]:
        return self._request("renew", {"token": token, "ttl_seconds": ttl_seconds}, timeout=5)

    def release(self, token: str) -> dict[str, Any]:
        return self._request("release", {"token": token}, timeout=5)

    def execute(self, token: str, operation: str, params: dict[str, Any] | None = None,
                timeout: float = 70.0) -> Any:
        return self._request("execute", {
            "token": token,
            "operation": operation,
            "operation_params": params or {},
        }, timeout=timeout)


class GatewayPhone:
    """Phone API proxy whose every device operation is lease-protected."""

    def __init__(self, serial: str | None = None, *, project: str | None = None,
                 purpose: str = "", ttl_seconds: float = DEFAULT_LEASE_SECONDS,
                 client: GatewayClient | None = None):
        del serial  # Device selection belongs exclusively to the gateway service.
        raw_project = project or os.environ.get("PHONE_PROJECT") or Path.cwd().name
        self.project = "".join(c if c.isalnum() or c in "_.:/-" else "_" for c in raw_project)[:120]
        self.purpose = purpose
        self.ttl_seconds = ttl_seconds
        self.client = client or GatewayClient()
        self._token: str | None = None

    def acquire(self) -> dict[str, Any]:
        result = self.client.acquire(self.project, self.ttl_seconds, self.purpose)
        self._token = result["token"]
        return result

    def close(self) -> None:
        if not self._token:
            return
        token, self._token = self._token, None
        try:
            self.client.release(token)
        except GatewayError as exc:
            if exc.code not in {"LEASE_EXPIRED", "INVALID_TOKEN"}:
                raise

    def _call(self, operation: str, params: dict[str, Any] | None = None,
              timeout: float = 70.0) -> Any:
        if not self._token:
            self.acquire()
        try:
            return self.client.execute(self._token or "", operation, params, timeout)
        except GatewayError as exc:
            if exc.code not in {"LEASE_EXPIRED", "INVALID_TOKEN"}:
                raise
            self._token = None
            self.acquire()
            return self.client.execute(self._token or "", operation, params, timeout)

    def devices(self) -> list[str]:
        return list(self._call("devices", timeout=10))

    def screenshot_png(self) -> bytes:
        result = self._call("screenshot_png", timeout=40)
        return base64.b64decode(result["base64"])

    def screen_size(self) -> tuple[int, int]:
        return tuple(self._call("screen_size", timeout=10))  # type: ignore[return-value]

    def display_size(self) -> tuple[int, int]:
        return tuple(self._call("display_size", timeout=10))  # type: ignore[return-value]

    def tap(self, x: int, y: int) -> None:
        self._call("tap", {"x": x, "y": y})

    def long_press(self, x: int, y: int, duration_ms: int = 1000) -> None:
        self._call("long_press", {"x": x, "y": y, "duration_ms": duration_ms},
                   timeout=max(10, duration_ms / 1000 + 5))

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              duration_ms: int = 300) -> None:
        self._call("swipe", {"x1": x1, "y1": y1, "x2": x2, "y2": y2,
                             "duration_ms": duration_ms},
                   timeout=max(10, duration_ms / 1000 + 5))

    def input_text(self, text: str) -> None:
        self._call("input_text", {"text": text})

    def press_key(self, key: str | int) -> None:
        self._call("press_key", {"key": key})

    def shell(self, command: str, timeout: int = 60) -> str:
        return str(self._call("shell", {"command": command, "timeout": timeout},
                              timeout=timeout + 10))

    def ui_dump(self) -> list[UINode]:
        values = self._call("ui_dump", timeout=40)
        return [self._node_from(value) for value in values]

    @staticmethod
    def _node_from(value: dict) -> UINode:
        return UINode(
            bounds=tuple(value["bounds"]),
            index=value["index"],
            text=value["text"],
            desc=value["desc"],
            cls=value["cls"],
            clickable=value["clickable"],
            # 旧版 gateway 可能不带这些状态字段
            checked=value.get("checked", False),
            selected=value.get("selected", False),
            focused=value.get("focused", False),
            enabled=value.get("enabled", True),
        )

    def find_by_text(self, query: str) -> list[UINode]:
        lowered = query.lower()
        return [node for node in self.ui_dump()
                if lowered in node.text.lower() or lowered in node.desc.lower()]

    def tap_text(self, query: str, index: int = 0) -> UINode:
        value = self._call("tap_text", {"query": query, "index": index}, timeout=40)
        return self._node_from(value)

    def launch_app(self, package: str) -> None:
        self._call("launch_app", {"package": package})

    def current_app(self) -> str:
        return str(self._call("current_app", timeout=10))

    def list_apps(self, keyword: str = "", third_party_only: bool = True) -> list[str]:
        return list(self._call("list_apps", {"keyword": keyword,
                                             "third_party_only": third_party_only}, timeout=20))

    def lock_state(self) -> dict[str, Any]:
        return self._call("lock_state", timeout=15)

    def unlock_pattern(self, pattern: str) -> dict[str, Any]:
        return self._call("unlock_pattern", {"pattern": pattern}, timeout=45)

    def capture_app_logs(self, package: str, *, limit: int = 500,
                         min_priority: str = "V", include_crash: bool = True) -> dict[str, Any]:
        return self._call("capture_app_logs", {
            "package": package,
            "limit": limit,
            "min_priority": min_priority,
            "include_crash": include_crash,
        }, timeout=70)

"""Host-side client for the debug APK semantic game-test bridge."""

from __future__ import annotations

import base64
import json
import re
import shlex
import time
import uuid
from typing import Any

from .adb import ADBError, Phone

PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]*$")
BRIDGE_ENABLE = "game_test_bridge_enabled"
BRIDGE_COMMAND = "game_test_command.json"
BRIDGE_RESPONSE = "game_test_response.json"
BRIDGE_STATE = "game_test_state.json"
BRIDGE_BOT = "game_test_bot.json"


class GameBridgeError(RuntimeError):
    pass


class GameBridgeClient:
    """Exchange atomic JSON messages through an Android debug app's files dir."""

    def __init__(self, phone: Phone, package: str):
        if not PACKAGE_RE.fullmatch(package):
            raise GameBridgeError(f"invalid Android package name {package!r}")
        self.phone = phone
        self.package = package

    def _run_as(self, command: str, timeout: int = 10) -> str:
        try:
            return self.phone.shell(
                f"run-as {shlex.quote(self.package)} sh -c {shlex.quote(command)}",
                timeout=timeout,
            )
        except ADBError as exc:
            raise GameBridgeError(
                f"cannot access {self.package!r} debug data; ensure a debuggable APK is installed: {exc}"
            ) from exc

    def probe(self) -> dict[str, Any]:
        output = self._run_as("id")
        return {
            "package": self.package,
            "run_as": bool(output.strip()),
            "foreground": self.phone.current_app().startswith(self.package + "/"),
            "enabled": self._run_as(f"test -f files/{BRIDGE_ENABLE} && echo yes || echo no").strip()
            == "yes",
        }

    def enable(self, relaunch: bool = True) -> dict[str, Any]:
        self._run_as(
            f": > files/{BRIDGE_ENABLE}; "
            f"rm -f files/{BRIDGE_COMMAND} files/{BRIDGE_RESPONSE}"
        )
        if relaunch:
            self.phone.shell(f"am force-stop {shlex.quote(self.package)}")
            self.phone.launch_app(self.package)
        return self.probe()

    def disable(self, relaunch: bool = True) -> dict[str, Any]:
        try:
            self.command("stop_bot", timeout=2)
        except GameBridgeError:
            pass
        self._run_as(
            "rm -f "
            + " ".join(
                f"files/{name}"
                for name in (
                    BRIDGE_ENABLE,
                    BRIDGE_COMMAND,
                    BRIDGE_RESPONSE,
                    BRIDGE_STATE,
                    BRIDGE_BOT,
                )
            )
        )
        if relaunch:
            self.phone.shell(f"am force-stop {shlex.quote(self.package)}")
            self.phone.launch_app(self.package)
        return self.probe()

    def _read_json(self, filename: str) -> dict[str, Any]:
        text = self._run_as(f"cat files/{filename}")
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GameBridgeError(f"invalid {filename}: {exc}") from exc
        if not isinstance(value, dict):
            raise GameBridgeError(f"{filename} is not a JSON object")
        return value

    def state(self) -> dict[str, Any]:
        return self._read_json(BRIDGE_STATE)

    def command(
        self,
        operation: str,
        args: dict[str, Any] | None = None,
        *,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        command_id = uuid.uuid4().hex[:16]
        payload = json.dumps(
            {"id": command_id, "op": operation, "args": args or {}},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        encoded = base64.b64encode(payload).decode("ascii")
        shell = (
            f"printf %s {shlex.quote(encoded)} | base64 -d > files/{BRIDGE_COMMAND}.tmp "
            f"&& mv files/{BRIDGE_COMMAND}.tmp files/{BRIDGE_COMMAND}"
        )
        self._run_as(shell)
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                response = self._read_json(BRIDGE_RESPONSE)
                if response.get("id") == command_id:
                    if not response.get("ok", False):
                        raise GameBridgeError(str(response.get("error", "bridge command failed")))
                    return response
            except GameBridgeError as exc:
                last_error = exc
            time.sleep(0.05)
        suffix = f": {last_error}" if last_error else ""
        raise GameBridgeError(f"bridge command {operation!r} timed out{suffix}")

    def start_bot(
        self,
        *,
        profile: str = "side_scroller",
        max_seconds: float = 300,
        auto_restart: bool = True,
        auto_equip: bool = True,
        attack_interval_ticks: int = 8,
        skill_interval_ticks: int = 150,
    ) -> dict[str, Any]:
        return self.command(
            "start_bot",
            {
                "profile": profile,
                "max_seconds": max_seconds,
                "auto_restart": auto_restart,
                "auto_equip": auto_equip,
                "attack_interval_ticks": attack_interval_ticks,
                "skill_interval_ticks": skill_interval_ticks,
            },
        )

    def stop_bot(self) -> dict[str, Any]:
        return self.command("stop_bot")

    def restart_scene(self, timeout: float = 8.0) -> dict[str, Any]:
        """Reload the current game scene and wait for a fresh bridge instance."""
        response = self.command("restart", timeout=timeout)
        old_timestamp = float(response.get("state", {}).get("timestamp_unix", 0))
        deadline = time.monotonic() + timeout
        last_state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                last_state = self.state()
                bridge = last_state.get("bridge", {})
                if (
                    float(last_state.get("timestamp_unix", 0)) > old_timestamp
                    and not bridge.get("last_command_id")
                ):
                    return last_state
            except GameBridgeError:
                pass
            time.sleep(0.05)
        raise GameBridgeError(f"scene restart did not expose a fresh bridge state: {last_state}")

    def run_soak(
        self,
        *,
        profile: str = "side_scroller",
        max_seconds: float = 300,
        sample_seconds: float = 2,
        warmup_seconds: float = 3,
        auto_restart: bool = True,
        auto_equip: bool = True,
        minimum_fps: float | None = None,
        maximum_physics_ms: float | None = None,
        required_floor: int | None = None,
        fresh_restart: bool = True,
    ) -> dict[str, Any]:
        """Run and observe a semantic bot, returning a reproducible soak report."""
        if max_seconds <= 0 or max_seconds > 86400:
            raise GameBridgeError("max_seconds must be in 0..86400")
        if sample_seconds < 0.25 or sample_seconds > 60:
            raise GameBridgeError("sample_seconds must be in 0.25..60")
        if warmup_seconds < 0 or warmup_seconds > max_seconds:
            raise GameBridgeError("warmup_seconds must be in 0..max_seconds")
        if fresh_restart:
            try:
                current = self.state()
                if current.get("bridge", {}).get("bot_active"):
                    self.stop_bot()
            except GameBridgeError:
                pass
            self.restart_scene()
        started_at = time.time()
        started = time.monotonic()
        samples: list[dict[str, Any]] = []
        observed_events: list[dict[str, Any]] = []
        event_keys: set[tuple[Any, ...]] = set()
        restarts = 0
        deaths = 0
        total_defeated = 0
        previous_elapsed: float | None = None
        previous_defeated = 0
        previous_state = ""
        maximum_floor = 0
        maximum_wave = -1
        maximum_combo = 0
        maximum_enemies = 0
        minimum_health: float | None = None
        minimum_sampled_fps: float | None = None
        maximum_physics_sample = 0.0
        maximum_draw_calls = 0
        highest_rarity_tier = -1
        maximum_affix_count = 0
        maximum_attack_speed = 0.0
        maximum_cooldown_reduction = 0.0
        performance_samples = 0
        violations: list[str] = []
        active = False
        try:
            self.start_bot(
                profile=profile,
                max_seconds=max_seconds,
                auto_restart=auto_restart,
                auto_equip=auto_equip,
            )
            active = True
            while time.monotonic() - started <= max_seconds + max(5.0, sample_seconds * 2):
                state = self.state()
                host_elapsed = time.monotonic() - started
                sample = {"host_elapsed_seconds": round(host_elapsed, 3), **state}
                samples.append(sample)
                room = state.get("room", {})
                player = state.get("player", {})
                combat = state.get("combat", {})
                performance = state.get("performance", {})
                bridge = state.get("bridge", {})
                elapsed = float(room.get("elapsed_seconds", 0))
                defeated = int(room.get("defeated", 0))
                room_state = str(room.get("state", ""))
                if previous_elapsed is not None and elapsed + 0.5 < previous_elapsed:
                    restarts += 1
                    total_defeated += previous_defeated
                if room_state == "FAILED" and previous_state != "FAILED":
                    deaths += 1
                previous_elapsed = elapsed
                previous_defeated = defeated
                previous_state = room_state
                maximum_floor = max(maximum_floor, int(room.get("floor", 0)))
                maximum_wave = max(maximum_wave, int(room.get("absolute_wave_index", -1)))
                maximum_enemies = max(maximum_enemies, int(room.get("active_enemies", 0)))
                maximum_combo = max(maximum_combo, int(combat.get("maximum_combo", 0)))
                health = float(player.get("health", 0))
                minimum_health = health if minimum_health is None else min(minimum_health, health)
                stats = player.get("stats", {})
                maximum_attack_speed = max(
                    maximum_attack_speed, float(stats.get("attack_speed_percent", 0))
                )
                maximum_cooldown_reduction = max(
                    maximum_cooldown_reduction,
                    float(stats.get("cooldown_reduction_percent", 0)),
                )
                if host_elapsed >= warmup_seconds:
                    performance_samples += 1
                    fps = float(performance.get("fps", 0))
                    if fps > 0:
                        minimum_sampled_fps = fps if minimum_sampled_fps is None else min(
                            minimum_sampled_fps, fps
                        )
                    maximum_physics_sample = max(
                        maximum_physics_sample, float(performance.get("physics_ms", 0))
                    )
                    maximum_draw_calls = max(
                        maximum_draw_calls, int(performance.get("draw_calls", 0))
                    )
                for item in list(state.get("inventory", [])) + [
                    value for value in state.get("equipment", {}).values() if value
                ]:
                    highest_rarity_tier = max(
                        highest_rarity_tier, int(item.get("rarity_tier", -1))
                    )
                    maximum_affix_count = max(
                        maximum_affix_count, int(item.get("affix_count", 0))
                    )
                for event in state.get("events", []):
                    key = (
                        event.get("timestamp_unix"),
                        event.get("tick"),
                        event.get("type"),
                    )
                    if key not in event_keys:
                        event_keys.add(key)
                        observed_events.append(event)
                active = bool(bridge.get("bot_active", False))
                if not active:
                    break
                time.sleep(sample_seconds)
        finally:
            if active:
                try:
                    self.stop_bot()
                except GameBridgeError:
                    pass
        total_defeated += previous_defeated
        if minimum_fps is not None and (
            minimum_sampled_fps is None or minimum_sampled_fps < minimum_fps
        ):
            violations.append(
                f"minimum sampled FPS {minimum_sampled_fps} < required {minimum_fps}"
            )
        if maximum_physics_ms is not None and maximum_physics_sample > maximum_physics_ms:
            violations.append(
                f"maximum sampled physics {maximum_physics_sample:.3f}ms > allowed {maximum_physics_ms:.3f}ms"
            )
        if required_floor is not None and maximum_floor < required_floor:
            violations.append(f"maximum floor {maximum_floor} < required {required_floor}")
        return {
            "schema_version": 1,
            "status": "failed_thresholds" if violations else "completed",
            "package": self.package,
            "profile": profile,
            "fresh_restart": fresh_restart,
            "warmup_seconds": warmup_seconds,
            "started_at": started_at,
            "ended_at": time.time(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "summary": {
                "samples": len(samples),
                "performance_samples_after_warmup": performance_samples,
                "restarts": restarts,
                "deaths": deaths,
                "total_defeated": total_defeated,
                "maximum_floor": maximum_floor,
                "maximum_absolute_wave": maximum_wave,
                "maximum_combo": maximum_combo,
                "maximum_active_enemies": maximum_enemies,
                "minimum_health": minimum_health,
                "minimum_sampled_fps": minimum_sampled_fps,
                "maximum_sampled_physics_ms": maximum_physics_sample,
                "maximum_draw_calls": maximum_draw_calls,
                "highest_rarity_tier": highest_rarity_tier,
                "maximum_affix_count": maximum_affix_count,
                "maximum_attack_speed_percent": maximum_attack_speed,
                "maximum_cooldown_reduction_percent": maximum_cooldown_reduction,
            },
            "violations": violations,
            "events": observed_events,
            "samples": samples,
        }

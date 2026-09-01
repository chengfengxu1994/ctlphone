"""Declarative, cancellable action runner for real-time Android games.

The runner deliberately uses only stock ADB commands.  Parallel tracks are
useful for issuing movement and combat commands concurrently, but stock ADB
does not guarantee true multi-pointer touch injection on every Android build.
Plans that need strict multi-touch should use a dedicated injector instead.
"""

from __future__ import annotations

import copy
import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .adb import ADBError, Phone

MAX_DEPTH = 8
MAX_PARALLEL_TRACKS = 8
MAX_REPEAT_COUNT = 100_000
MIN_INTERVAL_SECONDS = 0.02
MAX_GESTURE_CHUNK_MS = 1500
DEFAULT_MAX_SECONDS = 300.0
HARD_MAX_SECONDS = 86_400.0


class PlanError(ValueError):
    """Raised when a game plan is invalid."""


class MacroStopped(RuntimeError):
    """Raised internally when a macro is cancelled or reaches its deadline."""


@dataclass
class RunResult:
    name: str
    status: str
    started_at: float
    ended_at: float
    elapsed_seconds: float
    actions_completed: int
    adb_calls: int
    source_size: tuple[int, int]
    display_size: tuple[int, int]
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_plan(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"cannot load plan {path}: {exc}") from exc
    validate_plan(value)
    return value


def parse_plan_json(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlanError(f"invalid plan JSON: {exc}") from exc
    validate_plan(value)
    return value


def _number(value: Any, path: str, *, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanError(f"{path} must be a number")
    result = float(value)
    if result < minimum:
        raise PlanError(f"{path} must be >= {minimum}")
    return result


def _coord(action: dict[str, Any], key: str, path: str) -> None:
    _number(action.get(key), f"{path}.{key}")


def _validate_actions(actions: Any, path: str, depth: int) -> None:
    if depth > MAX_DEPTH:
        raise PlanError(f"{path} exceeds maximum nesting depth {MAX_DEPTH}")
    if not isinstance(actions, list) or not actions:
        raise PlanError(f"{path} must be a non-empty list")
    for index, action in enumerate(actions):
        item_path = f"{path}[{index}]"
        if not isinstance(action, dict):
            raise PlanError(f"{item_path} must be an object")
        kind = action.get("type")
        if kind == "tap":
            _coord(action, "x", item_path)
            _coord(action, "y", item_path)
            if "jitter_px" in action:
                _number(action["jitter_px"], f"{item_path}.jitter_px")
        elif kind in {"swipe", "drag_hold"}:
            for key in ("x1", "y1", "x2", "y2"):
                _coord(action, key, item_path)
            _number(action.get("ms", 300), f"{item_path}.ms", minimum=1)
        elif kind == "hold":
            _coord(action, "x", item_path)
            _coord(action, "y", item_path)
            _number(action.get("ms", 1000), f"{item_path}.ms", minimum=1)
        elif kind == "key":
            if not isinstance(action.get("key"), (str, int)):
                raise PlanError(f"{item_path}.key must be a string or integer")
        elif kind == "sleep":
            _number(action.get("seconds"), f"{item_path}.seconds")
        elif kind == "assert_foreground":
            if not isinstance(action.get("package"), str) or not action["package"]:
                raise PlanError(f"{item_path}.package must be a non-empty string")
        elif kind == "repeat":
            has_count = "count" in action
            has_duration = "duration_seconds" in action
            if has_count == has_duration:
                raise PlanError(
                    f"{item_path} requires exactly one of count or duration_seconds"
                )
            if has_count:
                count = _number(action["count"], f"{item_path}.count", minimum=1)
                if not count.is_integer() or count > MAX_REPEAT_COUNT:
                    raise PlanError(
                        f"{item_path}.count must be an integer <= {MAX_REPEAT_COUNT}"
                    )
            else:
                _number(
                    action["duration_seconds"],
                    f"{item_path}.duration_seconds",
                    minimum=MIN_INTERVAL_SECONDS,
                )
            if "interval_seconds" in action:
                _number(
                    action["interval_seconds"],
                    f"{item_path}.interval_seconds",
                    minimum=MIN_INTERVAL_SECONDS,
                )
            _validate_actions(action.get("actions"), f"{item_path}.actions", depth + 1)
        elif kind == "parallel":
            tracks = action.get("tracks")
            if not isinstance(tracks, list) or not (2 <= len(tracks) <= MAX_PARALLEL_TRACKS):
                raise PlanError(
                    f"{item_path}.tracks must contain 2..{MAX_PARALLEL_TRACKS} tracks"
                )
            for track_index, track in enumerate(tracks):
                _validate_actions(track, f"{item_path}.tracks[{track_index}]", depth + 1)
        elif kind == "random_choice":
            choices = action.get("choices")
            if not isinstance(choices, list) or not choices:
                raise PlanError(f"{item_path}.choices must be a non-empty list")
            for choice_index, choice in enumerate(choices):
                _validate_actions(
                    choice, f"{item_path}.choices[{choice_index}]", depth + 1
                )
        else:
            raise PlanError(f"{item_path}.type has unsupported value {kind!r}")


def validate_plan(plan: Any) -> None:
    if not isinstance(plan, dict):
        raise PlanError("plan must be a JSON object")
    if plan.get("version") != 1:
        raise PlanError("plan.version must be 1")
    name = plan.get("name")
    if not isinstance(name, str) or not name.strip():
        raise PlanError("plan.name must be a non-empty string")
    space = plan.get("coordinate_space")
    if not isinstance(space, dict):
        raise PlanError("plan.coordinate_space must be an object")
    width = _number(space.get("width"), "plan.coordinate_space.width", minimum=1)
    height = _number(space.get("height"), "plan.coordinate_space.height", minimum=1)
    if not width.is_integer() or not height.is_integer():
        raise PlanError("coordinate space width and height must be integers")
    if "max_seconds" in plan:
        maximum = _number(plan["max_seconds"], "plan.max_seconds", minimum=0.1)
        if maximum > HARD_MAX_SECONDS:
            raise PlanError(f"plan.max_seconds must be <= {HARD_MAX_SECONDS:g}")
    if "package" in plan and (
        not isinstance(plan["package"], str) or not plan["package"].strip()
    ):
        raise PlanError("plan.package must be a non-empty string")
    _validate_actions(plan.get("actions"), "plan.actions", 1)


class MacroRunner:
    """Execute a validated game plan against a :class:`Phone`."""

    def __init__(
        self,
        phone: Phone,
        *,
        stop_event: threading.Event | None = None,
        max_seconds: float | None = None,
        seed: int | None = None,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.phone = phone
        self.stop_event = stop_event or threading.Event()
        self.max_seconds = max_seconds
        self.random = random.Random(seed)
        self.progress = progress
        self._deadline = 0.0
        self._source_size = (1, 1)
        self._display_size = (1, 1)
        self._actions_completed = 0
        self._adb_calls = 0
        self._lock = threading.Lock()

    def run(self, plan: dict[str, Any]) -> RunResult:
        plan = copy.deepcopy(plan)
        validate_plan(plan)
        source = plan["coordinate_space"]
        self._source_size = (source["width"], source["height"])
        started_wall = time.time()
        started = time.monotonic()
        status = "completed"
        error = ""
        warnings = [
            "stock ADB parallel tracks are concurrent commands, not guaranteed true multi-touch"
        ]
        try:
            self._display_size = self.phone.display_size()
            limit = self.max_seconds
            if limit is None:
                limit = float(plan.get("max_seconds", DEFAULT_MAX_SECONDS))
            limit = min(float(limit), HARD_MAX_SECONDS)
            if limit <= 0:
                raise PlanError("max_seconds must be positive")
            self._deadline = started + limit
            package = plan.get("package")
            if package:
                self._assert_foreground(package)
            self._run_actions(plan["actions"])
        except MacroStopped as exc:
            status = "stopped" if self.stop_event.is_set() else "timed_out"
            error = str(exc)
        except Exception as exc:
            status = "failed"
            error = str(exc)
            self.stop_event.set()
        ended = time.monotonic()
        result = RunResult(
            name=plan["name"],
            status=status,
            started_at=started_wall,
            ended_at=time.time(),
            elapsed_seconds=round(ended - started, 3),
            actions_completed=self._actions_completed,
            adb_calls=self._adb_calls,
            source_size=self._source_size,
            display_size=self._display_size,
            warnings=warnings,
            error=error,
        )
        self._emit({"event": "finished", "result": result.to_dict()})
        return result

    def _emit(self, event: dict[str, Any]) -> None:
        if self.progress:
            self.progress(event)

    def _check_running(self) -> None:
        if self.stop_event.is_set():
            raise MacroStopped("stop requested")
        if self._deadline and time.monotonic() >= self._deadline:
            raise MacroStopped("maximum runtime reached")

    def _sleep(self, seconds: float) -> None:
        self._check_running()
        remaining = min(seconds, max(0.0, self._deadline - time.monotonic()))
        if self.stop_event.wait(remaining):
            raise MacroStopped("stop requested")
        self._check_running()

    def _point(self, x: float, y: float, jitter: float = 0) -> tuple[int, int]:
        if jitter:
            x += self.random.uniform(-jitter, jitter)
            y += self.random.uniform(-jitter, jitter)
        source_w, source_h = self._source_size
        display_w, display_h = self._display_size
        scaled_x = round(max(0, min(source_w - 1, x)) * display_w / source_w)
        scaled_y = round(max(0, min(source_h - 1, y)) * display_h / source_h)
        return min(display_w - 1, scaled_x), min(display_h - 1, scaled_y)

    def _adb(self, call: Callable[..., Any], *args: Any) -> Any:
        self._check_running()
        result = call(*args)
        with self._lock:
            self._adb_calls += 1
        return result

    def _done(self, kind: str) -> None:
        with self._lock:
            self._actions_completed += 1
            completed = self._actions_completed
        self._emit({"event": "action", "type": kind, "completed": completed})

    def _assert_foreground(self, package: str) -> None:
        current = self._adb(self.phone.current_app)
        if not current.startswith(package + "/"):
            raise ADBError(f"expected foreground package {package!r}, got {current!r}")

    def _chunked_gesture(
        self, start: tuple[int, int], end: tuple[int, int], duration_ms: int
    ) -> None:
        remaining = duration_ms
        while remaining > 0:
            self._check_running()
            chunk = min(remaining, MAX_GESTURE_CHUNK_MS)
            self._adb(self.phone.swipe, *start, *end, chunk)
            remaining -= chunk

    def _run_actions(self, actions: list[dict[str, Any]]) -> None:
        for action in actions:
            self._check_running()
            kind = action["type"]
            if kind == "tap":
                point = self._point(
                    action["x"], action["y"], float(action.get("jitter_px", 0))
                )
                self._adb(self.phone.tap, *point)
            elif kind == "swipe":
                start = self._point(action["x1"], action["y1"])
                end = self._point(action["x2"], action["y2"])
                self._adb(self.phone.swipe, *start, *end, int(action.get("ms", 300)))
            elif kind == "hold":
                point = self._point(action["x"], action["y"])
                self._chunked_gesture(point, point, int(action.get("ms", 1000)))
            elif kind == "drag_hold":
                start = self._point(action["x1"], action["y1"])
                end = self._point(action["x2"], action["y2"])
                self._chunked_gesture(start, end, int(action.get("ms", 1000)))
            elif kind == "key":
                self._adb(self.phone.press_key, action["key"])
            elif kind == "sleep":
                self._sleep(float(action["seconds"]))
            elif kind == "assert_foreground":
                self._assert_foreground(action["package"])
            elif kind == "repeat":
                self._run_repeat(action)
            elif kind == "parallel":
                self._run_parallel(action["tracks"])
            elif kind == "random_choice":
                self._run_actions(self.random.choice(action["choices"]))
            else:  # validated plans make this unreachable
                raise PlanError(f"unsupported action type {kind!r}")
            self._done(kind)

    def _run_repeat(self, action: dict[str, Any]) -> None:
        interval = float(action.get("interval_seconds", 0))
        if "count" in action:
            count = int(action["count"])
            for index in range(count):
                self._run_actions(action["actions"])
                if interval and index + 1 < count:
                    self._sleep(interval)
            return
        repeat_deadline = min(
            self._deadline, time.monotonic() + float(action["duration_seconds"])
        )
        while time.monotonic() < repeat_deadline:
            self._run_actions(action["actions"])
            if interval and time.monotonic() < repeat_deadline:
                self._sleep(min(interval, repeat_deadline - time.monotonic()))

    def _run_parallel(self, tracks: list[list[dict[str, Any]]]) -> None:
        first_error: BaseException | None = None
        with ThreadPoolExecutor(max_workers=len(tracks), thread_name_prefix="game-track") as pool:
            futures = [pool.submit(self._run_actions, track) for track in tracks]
            for future in as_completed(futures):
                try:
                    future.result()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
                    self.stop_event.set()
        if first_error:
            raise first_error

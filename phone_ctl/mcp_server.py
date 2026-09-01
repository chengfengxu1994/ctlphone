"""MCP server exposing phone control tools to AI agents (Kimi Code / Codex).

Run:  python -m phone_ctl.mcp_server        (stdio transport)
Config: see .kimi-code/mcp.json and README.md.
"""

from __future__ import annotations

import base64
import io
import json
import os

from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent

from .adb import ADBError
from .gateway_client import GatewayPhone
from .game_jobs import GameJobManager
from .game_macro import PlanError, parse_plan_json
from .game_bridge import GameBridgeClient, GameBridgeError

server = MCPServer("phone", instructions=(
    "Tools to control an Android phone (Redmi K60 Ultra, 1220x2712 px) via adb. "
    "Typical flow: ui_dump() or screenshot() to observe, then tap/tap_text/swipe to act. "
    "Coordinates are physical pixels. Prefer tap_text() over raw coordinates when possible. "
    "Caveats: WebView-heavy pages may expose few or no accessibility nodes (use screenshot); "
    "FLAG_SECURE pages (banking/trade login) screenshot as black (use ui_dump instead); "
    "tap/tap_text only confirm the event was dispatched — verify the effect with ui_dump() "
    "before assuming it worked."
))

_phone = GatewayPhone(project=os.environ.get("PHONE_PROJECT", "ctlphone-mcp"),
                      purpose="MCP phone control", ttl_seconds=60)
_game_jobs = GameJobManager(lambda: _phone)


def _err(e: Exception) -> str:
    return f"ERROR: {e}"


@server.tool()
def control_status() -> str:
    """Show the gateway process, connected device and current owning project."""
    try:
        return json.dumps(_phone.client.status(), ensure_ascii=False)
    except ADBError as e:
        return _err(e)


@server.tool()
def device_lock_state() -> str:
    """Read keyguard, screen and foreground-app state through the exclusive gateway."""
    try:
        return json.dumps(_phone.lock_state(), ensure_ascii=False)
    except ADBError as e:
        return _err(e)


@server.tool()
def collect_app_logs(package: str, limit: int = 500, min_priority: str = "V",
                     findings_only: bool = True) -> str:
    """Collect bounded, package/PID-filtered logcat without screenshots.

    Reports crashes, ANRs, script/resource errors and warnings. Credentials,
    authorization values, device serials and URL query strings are redacted.
    findings_only defaults true to avoid returning unrelated verbose app lines.
    """
    try:
        report = _phone.capture_app_logs(package, limit=limit, min_priority=min_priority)
        if findings_only:
            report.pop("logs", None)
        return json.dumps(report, ensure_ascii=False)
    except ADBError as e:
        return _err(e)


@server.tool()
def screenshot(scale: float = 0.5) -> list[ImageContent]:
    """Take a screenshot of the phone screen.

    scale: downscale factor to keep the image small for the model (default 0.5
    -> 610x1356). Use 1.0 for full resolution 1220x2712.
    Note: FLAG_SECURE pages (login/payment windows) capture as a black image;
    use ui_dump() there instead.
    """
    try:
        data = _phone.screenshot_png()
        if scale != 1.0:
            from PIL import Image

            img = Image.open(io.BytesIO(data))
            img = img.resize((round(img.width * scale), round(img.height * scale)))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            data = buf.getvalue()
        return [ImageContent(
            type="image",
            data=base64.b64encode(data).decode(),
            mimeType="image/png",
        )]
    except ADBError as e:
        raise RuntimeError(_err(e)) from e


@server.tool()
def ui_dump(max_nodes: int = 200) -> str:
    """Dump the current screen's UI elements as a compact list.

    Each line: [index] Class "text-or-description" center=(x,y) flags
    Flags: clickable, plus checked/selected/focused/disabled when applicable
    (use these to tell e.g. whether a checkbox is actually ticked).
    Use the center coordinates with tap(), or use tap_text() directly.
    Note: WebView content often has no accessibility nodes — if the dump is
    unexpectedly empty, fall back to screenshot(). On FLAG_SECURE pages
    (login/payment) screenshots are black but this dump still works.
    """
    try:
        nodes = _phone.ui_dump()
        lines = [n.one_line() for n in nodes[:max_nodes]]
        if len(nodes) > max_nodes:
            lines.append(f"... ({len(nodes) - max_nodes} more nodes)")
        return "\n".join(lines) or "(no visible UI nodes)"
    except ADBError as e:
        return _err(e)


@server.tool()
def tap(x: int, y: int) -> str:
    """Tap the screen at physical pixel coordinates (x, y).

    The return only confirms the event was dispatched, not that the app
    reacted. When the outcome matters, verify with ui_dump()/screenshot()
    afterwards and retry or adjust if nothing changed.
    """
    try:
        _phone.tap(x, y)
        return f"tapped ({x}, {y})"
    except ADBError as e:
        return _err(e)


@server.tool()
def tap_text(text: str, index: int = 0) -> str:
    """Find a visible UI element whose text/description contains `text` and tap it.

    More robust than coordinates. index selects among multiple matches (default 0).
    """
    try:
        node = _phone.tap_text(text, index)
        return f"tapped {node.one_line()}"
    except ADBError as e:
        return _err(e)


@server.tool()
def long_press(x: int, y: int, duration_ms: int = 1000) -> str:
    """Long-press at (x, y) for duration_ms milliseconds."""
    try:
        _phone.long_press(x, y, duration_ms)
        return f"long-pressed ({x}, {y}) for {duration_ms}ms"
    except ADBError as e:
        return _err(e)


@server.tool()
def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> str:
    """Swipe from (x1, y1) to (x2, y2). Scroll down: swipe from lower half to upper half."""
    try:
        _phone.swipe(x1, y1, x2, y2, duration_ms)
        return f"swiped ({x1},{y1}) -> ({x2},{y2})"
    except ADBError as e:
        return _err(e)


@server.tool()
def input_text(text: str) -> str:
    """Type text into the focused input field. ASCII only (Chinese needs ADBKeyboard)."""
    try:
        _phone.input_text(text)
        return f"typed {len(text)} chars"
    except ADBError as e:
        return _err(e)


@server.tool()
def press_key(key: str) -> str:
    """Press a hardware/system key: BACK, HOME, RECENT, POWER, ENTER, DEL,
    VOLUME_UP, VOLUME_DOWN, TAB, SPACE, DPAD_UP/DOWN/LEFT/RIGHT/CENTER, or a numeric keycode."""
    try:
        _phone.press_key(key)
        return f"pressed {key}"
    except ADBError as e:
        return _err(e)


@server.tool()
def launch_app(package: str) -> str:
    """Launch an app by package name, e.g. com.tencent.mm (WeChat). Use list_apps to find names."""
    try:
        _phone.launch_app(package)
        return f"launched {package}"
    except ADBError as e:
        return _err(e)


@server.tool()
def current_app() -> str:
    """Return the foreground app as 'package/activity'."""
    try:
        return _phone.current_app() or "(unknown)"
    except ADBError as e:
        return _err(e)


@server.tool()
def list_apps(keyword: str = "", third_party_only: bool = True) -> str:
    """List installed package names, optionally filtered by keyword."""
    try:
        pkgs = _phone.list_apps(keyword, third_party_only)
        return "\n".join(pkgs) or "(none)"
    except ADBError as e:
        return _err(e)


@server.tool()
def shell(command: str) -> str:
    """Run a raw adb shell command on the phone and return its output. Use with care."""
    try:
        return _phone.shell(command) or "(no output)"
    except ADBError as e:
        return _err(e)


@server.tool()
def game_validate(plan_json: str) -> str:
    """Validate a declarative real-time game plan without sending input.

    Supported actions: tap, swipe, hold, drag_hold, key, sleep,
    assert_foreground, repeat, parallel and random_choice. Plans require a
    version, name, coordinate_space and actions list.
    """
    try:
        plan = parse_plan_json(plan_json)
        return json.dumps({"valid": True, "name": plan["name"]}, ensure_ascii=False)
    except PlanError as e:
        return json.dumps({"valid": False, "error": str(e)}, ensure_ascii=False)


@server.tool()
def game_start(plan_json: str, max_seconds: float = 300, seed: int | None = None) -> str:
    """Start a long-running game macro in the background and return a job id.

    Only one game job can run at once. max_seconds is a mandatory remote safety
    limit (default five minutes, maximum 24 hours). Use game_status while it
    runs and game_stop to cancel it. Stock ADB parallel input is not guaranteed
    to be true multi-touch on every Android build.
    """
    try:
        plan = parse_plan_json(plan_json)
        job = _game_jobs.start(plan, max_seconds=max_seconds, seed=seed)
        return json.dumps(job, ensure_ascii=False)
    except (PlanError, RuntimeError, ValueError) as e:
        return _err(e)


@server.tool()
def game_status(job_id: str = "") -> str:
    """Return progress/result for a game job; empty job_id means active/latest."""
    try:
        return json.dumps(_game_jobs.status(job_id), ensure_ascii=False)
    except KeyError as e:
        return _err(e)


@server.tool()
def game_stop(job_id: str = "") -> str:
    """Request a running game job to stop; empty job_id means the active job."""
    try:
        return json.dumps(_game_jobs.stop(job_id), ensure_ascii=False)
    except KeyError as e:
        return _err(e)


@server.tool()
def game_bridge_enable(package: str) -> str:
    """Enable the screenshot-free semantic test bridge and relaunch a debug APK.

    The APK must contain GameTestBridge and be debuggable so Android run-as can
    access its private JSON mailbox. This force-stops and relaunches only the
    named package; app data is preserved.
    """
    try:
        return json.dumps(GameBridgeClient(_phone, package).enable(), ensure_ascii=False)
    except GameBridgeError as e:
        return _err(e)


@server.tool()
def game_bridge_state(package: str) -> str:
    """Read structured combat/player/equipment/performance state without a screenshot."""
    try:
        return json.dumps(GameBridgeClient(_phone, package).state(), ensure_ascii=False)
    except GameBridgeError as e:
        return _err(e)


@server.tool()
def game_bot_start(
    package: str,
    profile: str = "side_scroller",
    max_seconds: float = 300,
    auto_restart: bool = True,
    auto_equip: bool = True,
) -> str:
    """Start an in-game semantic bot using real movement/combat/skill input paths.

    profile is side_scroller or legend. The bot targets live enemies from game
    state, handles upgrade choices, can equip loot and can restart after death.
    It does not inject damage or depend on screenshots/touch coordinates.
    """
    try:
        result = GameBridgeClient(_phone, package).start_bot(
            profile=profile,
            max_seconds=max_seconds,
            auto_restart=auto_restart,
            auto_equip=auto_equip,
        )
        return json.dumps(result, ensure_ascii=False)
    except GameBridgeError as e:
        return _err(e)


@server.tool()
def game_bot_stop(package: str) -> str:
    """Stop the in-game semantic bot and release all virtual input."""
    try:
        return json.dumps(GameBridgeClient(_phone, package).stop_bot(), ensure_ascii=False)
    except GameBridgeError as e:
        return _err(e)


@server.tool()
def game_bridge_action(package: str, action: str) -> str:
    """Send one semantic action: attack/jump/dodge/skill_1..skill_4/pause."""
    try:
        result = GameBridgeClient(_phone, package).command("action", {"name": action})
        return json.dumps(result, ensure_ascii=False)
    except GameBridgeError as e:
        return _err(e)


@server.tool()
def game_bridge_move(
    package: str, x: float, y: float, duration_seconds: float = 0.0
) -> str:
    """Set a normalized semantic move vector; optional duration auto-releases it."""
    try:
        result = GameBridgeClient(_phone, package).command("move", {
            "x": x, "y": y, "duration_seconds": duration_seconds,
        })
        return json.dumps(result, ensure_ascii=False)
    except GameBridgeError as e:
        return _err(e)


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()

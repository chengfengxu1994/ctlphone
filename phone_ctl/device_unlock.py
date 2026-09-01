"""Safe lock-state inspection and explicitly authorized pattern unlock."""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from typing import Any

from .adb import ADBError, Phone

PATTERN_RESOURCE_ID = "com.android.systemui:id/lockPatternView"
UI_DUMP_PATH = "/data/local/tmp/phone_ctl_unlock_ui.xml"


class UnlockError(ADBError):
    pass


def lock_state(phone: Phone) -> dict[str, Any]:
    policy = phone.shell("dumpsys window policy")
    showing_match = re.search(r"(?:keyguardShowing|showing)=(true|false)", policy, re.I)
    if not showing_match:
        showing_match = re.search(r"isStatusBarKeyguard=(true|false)", policy, re.I)
    showing = showing_match.group(1).lower() == "true" if showing_match else None
    screen_on = bool(re.search(
        r"(?:mScreenOnFully=true|screenState=SCREEN_STATE_ON|interactiveState=INTERACTIVE_STATE_AWAKE)",
        policy,
        re.I,
    ))
    return {
        "keyguard_showing": showing,
        "screen_on": screen_on,
        "foreground_app": phone.current_app(),
    }


def _pattern_bounds(phone: Phone) -> tuple[int, int, int, int]:
    phone.shell(f"uiautomator dump {UI_DUMP_PATH}", timeout=30)
    xml_text = phone._adb("exec-out", "cat", UI_DUMP_PATH, timeout=30)  # gateway-internal
    root = ET.fromstring(xml_text)
    for node in root.iter("node"):
        if node.get("resource-id") != PATTERN_RESOURCE_ID:
            continue
        if node.get("enabled", "false") != "true":
            raise UnlockError("lock pattern view is present but disabled")
        match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
        if not match:
            raise UnlockError("cannot parse lock pattern view bounds")
        return tuple(int(value) for value in match.groups())  # type: ignore[return-value]
    raise UnlockError(f"enabled {PATTERN_RESOURCE_ID} node not found")


def _pattern_points(pattern: str, bounds: tuple[int, int, int, int]) -> list[tuple[int, int]]:
    if not re.fullmatch(r"[1-9]{4,9}", pattern) or len(set(pattern)) != len(pattern):
        raise UnlockError("pattern must contain 4..9 unique digits from 1 through 9")
    left, top, right, bottom = bounds
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        raise UnlockError("invalid lock pattern view bounds")
    points: list[tuple[int, int]] = []
    for character in pattern:
        digit = int(character)
        column = (digit - 1) % 3
        row = (digit - 1) // 3
        points.append((
            round(left + (column + 0.5) * width / 3),
            round(top + (row + 0.5) * height / 3),
        ))
    return points


def unlock_pattern(phone: Phone, pattern: str) -> dict[str, Any]:
    """Perform one authorized pattern attempt without persisting the credential."""
    devices = phone.devices()
    if len(devices) != 1:
        raise UnlockError(f"exactly one authorized device required, found {len(devices)}")
    before = lock_state(phone)
    if before["keyguard_showing"] is False:
        return {"status": "already_unlocked", "before": before, "after": before}
    if before["keyguard_showing"] is None:
        raise UnlockError("cannot determine keyguard state; refusing to guess")
    if not before["screen_on"]:
        phone.press_key("POWER")
        time.sleep(0.4)
    phone.press_key("MENU")
    time.sleep(0.35)
    bounds = _pattern_bounds(phone)
    points = _pattern_points(pattern, bounds)
    first_x, first_y = points[0]
    lines = [
        f"input touchscreen motionevent CANCEL {first_x} {first_y}",
        "sleep 0.20",
        f"input touchscreen motionevent DOWN {first_x} {first_y}",
        "sleep 0.12",
    ]
    for x, y in points[1:]:
        lines.extend([
            f"input touchscreen motionevent MOVE {x} {y}",
            "sleep 0.12",
        ])
    last_x, last_y = points[-1]
    lines.append(f"input touchscreen motionevent UP {last_x} {last_y}")
    phone.shell("\n".join(lines), timeout=15)
    time.sleep(0.7)
    after = lock_state(phone)
    if after["keyguard_showing"] is False:
        return {
            "status": "unlocked",
            "pattern_bounds": list(bounds),
            "before": before,
            "after": after,
        }
    # Inspect once for an explicit rejection/lockout; never retry automatically.
    phone.shell(f"uiautomator dump {UI_DUMP_PATH}", timeout=30)
    xml_text = phone._adb("exec-out", "cat", UI_DUMP_PATH, timeout=30)
    labels = " ".join(
        filter(None, re.findall(r'(?:text|content-desc)="([^"]+)"', xml_text))
    )
    rejection = bool(re.search(r"wrong|incorrect|try again|错误|重试|秒后|次数", labels, re.I))
    reason = "pattern rejected or device locked out" if rejection else "unlock gesture did not unlock device"
    raise UnlockError(reason)

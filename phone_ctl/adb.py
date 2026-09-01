"""Minimal ADB wrapper for controlling an Android phone from Linux.

Designed for a Redmi K60 Ultra (HyperOS / Android 16, 1220x2712) but works
with any single adb-connected device. All coordinates are physical pixels.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass

SERIAL_ENV = "PHONE_SERIAL"

# UI 临时文件放在 /data/local/tmp，不需要额外权限
_UI_DUMP_PATH = "/data/local/tmp/phone_ctl_ui.xml"

_KEYCODES = {
    "HOME": 3,
    "BACK": 4,
    "RECENT": 187,
    "POWER": 26,
    "ENTER": 66,
    "DEL": 67,
    "VOLUME_UP": 24,
    "VOLUME_DOWN": 25,
    "MENU": 82,
    "SEARCH": 84,
    "DPAD_UP": 19,
    "DPAD_DOWN": 20,
    "DPAD_LEFT": 21,
    "DPAD_RIGHT": 22,
    "DPAD_CENTER": 23,
    "TAB": 61,
    "SPACE": 62,
}


class ADBError(RuntimeError):
    pass


@dataclass
class UINode:
    index: int
    text: str
    desc: str
    cls: str
    clickable: bool
    bounds: tuple[int, int, int, int]  # x1, y1, x2, y2
    checked: bool = False
    selected: bool = False
    focused: bool = False
    enabled: bool = True

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    def one_line(self) -> str:
        label = self.text or self.desc
        cls_short = self.cls.rsplit(".", 1)[-1]
        cx, cy = self.center
        flags = "clickable" if self.clickable else ""
        if self.checked:
            flags += " checked"
        if self.selected:
            flags += " selected"
        if self.focused:
            flags += " focused"
        if not self.enabled:
            flags += " disabled"
        return f"[{self.index}] {cls_short} \"{label}\" center=({cx},{cy}) {flags}".rstrip()


class Phone:
    """Thin wrapper around the adb CLI."""

    def __init__(self, serial: str | None = None, adb_path: str = "adb"):
        self.serial = serial or os.environ.get(SERIAL_ENV) or None
        self.adb_path = adb_path

    # ------------------------------------------------------------------ adb

    def _adb(self, *args: str, timeout: int = 60, binary: bool = False):
        cmd = [self.adb_path]
        if self.serial:
            cmd += ["-s", self.serial]
        cmd += list(args)
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if p.returncode != 0:
            err = p.stderr.decode(errors="replace").strip()
            raise ADBError(f"adb failed ({' '.join(args)}): {err}")
        return p.stdout if binary else p.stdout.decode(errors="replace")

    def shell(self, command: str, timeout: int = 60) -> str:
        """Run an arbitrary adb shell command, return stdout."""
        return self._adb("shell", command, timeout=timeout)

    def devices(self) -> list[str]:
        out = self._adb("devices")
        return [
            line.split()[0]
            for line in out.splitlines()[1:]
            if line.strip() and line.split()[1] == "device"
        ]

    # ------------------------------------------------------------- 屏幕截图

    def screenshot_png(self) -> bytes:
        """Return raw PNG bytes of the current screen."""
        data = self._adb("exec-out", "screencap", "-p", binary=True, timeout=30)
        if not data.startswith(b"\x89PNG"):
            raise ADBError("screencap did not return a PNG")
        return data

    def screen_size(self) -> tuple[int, int]:
        out = self.shell("wm size")
        m = re.search(r"(\d+)x(\d+)", out)
        if not m:
            raise ADBError(f"cannot parse screen size: {out!r}")
        return int(m.group(1)), int(m.group(2))

    def display_size(self) -> tuple[int, int]:
        """Return the current logical display size, accounting for rotation.

        ``wm size`` reports the panel's natural dimensions on some Android
        versions even while an app is landscape. Input coordinates, however,
        use the rotated logical viewport. ``dumpsys input`` exposes that
        viewport without taking a slow screenshot.
        """
        out = self.shell("dumpsys input")
        matches = re.findall(
            r"logicalFrame=(?:Rect\(0,\s*0\s*-|\[0,\s*0,)\s*"
            r"(\d+),\s*(\d+)[\]\)]",
            out,
        )
        if matches:
            width, height = matches[0]
            return int(width), int(height)
        return self.screen_size()

    # ---------------------------------------------------------------- 输入

    def tap(self, x: int, y: int) -> None:
        self.shell(f"input tap {int(x)} {int(y)}")

    def long_press(self, x: int, y: int, duration_ms: int = 1000) -> None:
        # input swipe 起止点相同即为长按
        self.shell(f"input swipe {int(x)} {int(y)} {int(x)} {int(y)} {int(duration_ms)}")

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self.shell(
            f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}"
        )

    def input_text(self, text: str) -> None:
        """Type ASCII text. Non-ASCII (e.g. Chinese) needs ADBKeyboard — see README."""
        if not text.isascii():
            raise ADBError(
                "input text 仅支持 ASCII；中文输入请安装 ADBKeyboard（见 README）"
            )
        escaped = text.replace("%", "%25").replace(" ", "%s")
        self.shell(f"input text {shlex.quote(escaped)}")

    def press_key(self, key: str | int) -> None:
        """Press a key by name (BACK/HOME/ENTER/...) or numeric keycode."""
        if isinstance(key, str):
            name = key.upper()
            if name.isdigit():
                code = int(name)
            elif name in _KEYCODES:
                code = _KEYCODES[name]
            else:
                raise ADBError(
                    f"unknown key {key!r}; known: {', '.join(sorted(_KEYCODES))}"
                )
        else:
            code = int(key)
        self.shell(f"input keyevent {code}")

    # -------------------------------------------------------------- UI 树

    def ui_dump(self) -> list[UINode]:
        """Dump the current window hierarchy and return useful nodes."""
        self.shell(f"uiautomator dump {_UI_DUMP_PATH}", timeout=30)
        xml_text = self._adb("exec-out", "cat", _UI_DUMP_PATH, timeout=30)
        return self.parse_ui_xml(xml_text)

    @staticmethod
    def parse_ui_xml(xml_text: str) -> list[UINode]:
        nodes: list[UINode] = []
        root = ET.fromstring(xml_text)
        for el in root.iter("node"):
            text = el.get("text", "")
            desc = el.get("content-desc", "")
            clickable = el.get("clickable", "false") == "true"
            if not (text or desc or clickable):
                continue
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", el.get("bounds", ""))
            if not m:
                continue
            nodes.append(
                UINode(
                    index=len(nodes),
                    text=text,
                    desc=desc,
                    cls=el.get("class", ""),
                    clickable=clickable,
                    bounds=tuple(int(g) for g in m.groups()),  # type: ignore[arg-type]
                    checked=el.get("checked", "false") == "true",
                    selected=el.get("selected", "false") == "true",
                    focused=el.get("focused", "false") == "true",
                    enabled=el.get("enabled", "true") == "true",
                )
            )
        return nodes

    def find_by_text(self, query: str) -> list[UINode]:
        """Find visible nodes whose text or content-desc contains query."""
        q = query.lower()
        return [
            n
            for n in self.ui_dump()
            if q in n.text.lower() or q in n.desc.lower()
        ]

    def tap_text(self, query: str, index: int = 0) -> UINode:
        """Find a node containing query and tap its center."""
        matches = self.find_by_text(query)
        if not matches:
            raise ADBError(f"no visible node matching {query!r}")
        if index >= len(matches):
            raise ADBError(
                f"only {len(matches)} node(s) matching {query!r}, index {index} out of range"
            )
        node = matches[index]
        self.tap(*node.center)
        return node

    # ---------------------------------------------------------------- 应用

    def launch_app(self, package: str) -> None:
        self.shell(f"monkey -p {shlex.quote(package)} -c android.intent.category.LAUNCHER 1")

    def current_app(self) -> str:
        """Return 'package/activity' of the foreground app."""
        out = self.shell(
            "dumpsys activity activities | grep -E 'topResumedActivity|ResumedActivity' | head -1"
        )
        m = re.search(r"([a-zA-Z0-9_.]+/[a-zA-Z0-9_./$]+)", out)
        if m:
            return m.group(1)
        out = self.shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp' | head -1")
        m = re.search(r"([a-zA-Z0-9_.]+/[a-zA-Z0-9_./$]+)", out)
        return m.group(1) if m else ""

    def list_apps(self, keyword: str = "", third_party_only: bool = True) -> list[str]:
        args = "pm list packages" + (" -3" if third_party_only else "")
        out = self.shell(args)
        pkgs = sorted(line.removeprefix("package:").strip() for line in out.splitlines())
        if keyword:
            pkgs = [p for p in pkgs if keyword.lower() in p.lower()]
        return pkgs

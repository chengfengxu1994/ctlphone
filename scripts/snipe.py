#!/usr/bin/env python3
"""Timed action runner — 蹲点抢购/抢票/抢券 的简单执行器。

在指定时间点精确执行一串动作（点击坐标、按文字点击、按键等），
支持 tap_text 失败重试，适合 "到点狂点购买按钮" 这类场景。

用法:
    python scripts/snipe.py --at 20:00:00 plan.json
    python scripts/snipe.py plan.json            # 立即执行（演练）

plan.json 示例:
{
  "actions": [
    {"type": "tap_text", "text": "立即购买", "retry": 20, "interval": 0.15},
    {"type": "sleep", "seconds": 0.3},
    {"type": "tap", "x": 600, "y": 2400},
    {"type": "key", "key": "BACK"},
    {"type": "shell", "command": "input tap 600 1200"}
  ]
}

注意: 到点执行的精度取决于系统时钟。建议先用 `chronyc tracking` 或
`timedatectl` 确认本机时间已 NTP 同步，必要时提前几分钟打开手机页面。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phone_ctl.adb import ADBError
from phone_ctl.gateway_client import GatewayPhone


def wait_until(target: datetime) -> None:
    """Sleep until target; busy-wait the final 0.5s for precision."""
    ts = target.timestamp()
    while True:
        remain = ts - time.time()
        if remain <= 0:
            return
        if remain > 0.5:
            print(f"waiting... {remain:.1f}s", end="\r", flush=True)
            time.sleep(min(remain - 0.5, 5))
        # else: busy spin for sub-50ms precision


def run_actions(phone: GatewayPhone, actions: list[dict]) -> None:
    for i, act in enumerate(actions):
        t = act["type"]
        if t == "sleep":
            time.sleep(act["seconds"])
        elif t == "tap":
            phone.tap(act["x"], act["y"])
        elif t == "swipe":
            phone.swipe(act["x1"], act["y1"], act["x2"], act["y2"], act.get("ms", 300))
        elif t == "key":
            phone.press_key(act["key"])
        elif t == "text":
            phone.input_text(act["text"])
        elif t == "shell":
            phone.shell(act["command"])
        elif t == "tap_text":
            retry = act.get("retry", 1)
            interval = act.get("interval", 0.3)
            for attempt in range(1, retry + 1):
                try:
                    node = phone.tap_text(act["text"], act.get("index", 0))
                    print(f"[{i}] tapped {node.one_line()} (attempt {attempt})")
                    break
                except ADBError:
                    if attempt == retry:
                        raise
                    time.sleep(interval)
            else:
                raise ADBError(f"tap_text {act['text']!r} failed after {retry} attempts")
        else:
            raise ValueError(f"unknown action type: {t!r}")
        print(f"[{i}] {t} done @ {time.time():.3f}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Timed phone action runner")
    ap.add_argument("plan", help="plan JSON file")
    ap.add_argument("--at", help="target time, e.g. 20:00:00 or 2026-08-06T20:00:00 "
                                 "(default: run immediately)")
    ap.add_argument("--serial", "-s", help="adb device serial")
    args = ap.parse_args()

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    actions = plan["actions"]
    phone = GatewayPhone(serial=args.serial, project="ctlphone-snipe", purpose="timed action script")

    if args.at:
        now = datetime.now()
        try:
            target = datetime.fromisoformat(args.at)
        except ValueError:
            h, m, s = (args.at.split(":") + ["0"])[:3]
            target = now.replace(hour=int(h), minute=int(m), second=int(float(s)),
                                 microsecond=0)
            if target < now:
                target += timedelta(days=1)  # 时间已过则视为明天
        print(f"target: {target:%Y-%m-%d %H:%M:%S} ({(target - now).total_seconds():.1f}s from now)")
        wait_until(target)

    start = time.perf_counter()
    try:
        run_actions(phone, actions)
        print(f"all actions done in {time.perf_counter() - start:.3f}s")
    finally:
        phone.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

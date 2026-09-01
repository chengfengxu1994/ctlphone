"""Command line interface: phonectl <command> ...

Examples:
    phonectl screenshot out.png --scale 0.5
    phonectl dump
    phonectl tap 540 2100
    phonectl tap-text "立即抢购"
    phonectl swipe 600 2000 600 800
    phonectl key BACK
    phonectl text hello123
    phonectl launch com.tencent.mm
    phonectl current
    phonectl apps 淘宝
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import threading
from pathlib import Path

from .adb import ADBError
from .gateway_client import GatewayClient, GatewayPhone
from .game_macro import MacroRunner, PlanError, load_plan
from .game_bridge import GameBridgeClient, GameBridgeError


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="phonectl", description="Control the phone via adb")
    p.add_argument("--serial", "-s", help="adb device serial (default: $PHONE_SERIAL or auto)")
    p.add_argument("--project", default=None, help="stable project identity for gateway ownership")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("screenshot", help="save a screenshot to a file")
    sp.add_argument("out", help="output PNG path")
    sp.add_argument("--scale", type=float, default=1.0, help="downscale factor, e.g. 0.5")

    sub.add_parser("dump", help="print simplified UI node list")
    sub.add_parser("size", help="print screen resolution")
    sub.add_parser("devices", help="list connected devices")

    sp = sub.add_parser("tap"); sp.add_argument("x", type=int); sp.add_argument("y", type=int)
    sp = sub.add_parser("tap-text"); sp.add_argument("text"); sp.add_argument("--index", type=int, default=0)
    sp = sub.add_parser("long-press"); sp.add_argument("x", type=int); sp.add_argument("y", type=int)
    sp.add_argument("--ms", type=int, default=1000)
    sp = sub.add_parser("swipe")
    sp.add_argument("x1", type=int); sp.add_argument("y1", type=int)
    sp.add_argument("x2", type=int); sp.add_argument("y2", type=int)
    sp.add_argument("--ms", type=int, default=300)

    sp = sub.add_parser("text"); sp.add_argument("text")
    sp = sub.add_parser("key"); sp.add_argument("key")

    sp = sub.add_parser("launch"); sp.add_argument("package")
    sub.add_parser("current", help="print foreground package/activity")
    sp = sub.add_parser("apps"); sp.add_argument("keyword", nargs="?", default="")
    sp.add_argument("--all", action="store_true", help="include system packages")

    sp = sub.add_parser("shell"); sp.add_argument("command")

    sp = sub.add_parser("logs", help="collect bounded, package-filtered and redacted logcat")
    sp.add_argument("package", help="Android application package name")
    sp.add_argument("--limit", type=int, default=500, help="lines requested per source (20..2000)")
    sp.add_argument("--priority", choices=("V", "D", "I", "W", "E", "F"), default="V")
    sp.add_argument("--no-crash", action="store_true", help="skip the crash buffer")
    sp.add_argument("--summary-only", action="store_true", help="omit normal log lines")
    sp.add_argument("--output", help="write JSON report with mode 0600")

    game = sub.add_parser("game", help="validate or run a long-running game macro")
    game_sub = game.add_subparsers(dest="game_cmd", required=True)
    sp = game_sub.add_parser("validate", help="validate a game plan without touching the phone")
    sp.add_argument("plan", help="game plan JSON file")
    sp = game_sub.add_parser("probe", help="show live display and foreground app information")
    sp = game_sub.add_parser("run", help="run a game plan in the foreground; Ctrl-C stops it")
    sp.add_argument("plan", help="game plan JSON file")
    sp.add_argument("--max-seconds", type=float, help="override the plan runtime safety limit")
    sp.add_argument("--seed", type=int, help="repeatable seed for random_choice and tap jitter")
    sp.add_argument("--report", help="write the final JSON result to this path")
    for command, help_text in (
        ("bridge-enable", "enable the semantic bridge and relaunch the debug APK"),
        ("bridge-disable", "disable the semantic bridge and relaunch the debug APK"),
        ("bridge-state", "read structured game state without a screenshot"),
        ("bot-stop", "stop the in-game semantic test bot"),
    ):
        sp = game_sub.add_parser(command, help=help_text)
        sp.add_argument("package", help="debug APK package name")
    sp = game_sub.add_parser("bot-start", help="start screenshot-free in-game combat automation")
    sp.add_argument("package", help="debug APK package name")
    sp.add_argument("--profile", choices=("side_scroller", "legend"), default="side_scroller")
    sp.add_argument("--max-seconds", type=float, default=300)
    sp.add_argument("--no-restart", action="store_true")
    sp.add_argument("--no-auto-equip", action="store_true")
    sp.add_argument("--attack-interval-ticks", type=int, default=8)
    sp.add_argument("--skill-interval-ticks", type=int, default=150)
    sp = game_sub.add_parser("bot-watch", help="run a bot and produce a structured soak report")
    sp.add_argument("package", help="debug APK package name")
    sp.add_argument("--profile", choices=("side_scroller", "legend"), default="side_scroller")
    sp.add_argument("--max-seconds", type=float, default=300)
    sp.add_argument("--sample-seconds", type=float, default=2)
    sp.add_argument("--warmup-seconds", type=float, default=3,
                    help="exclude cold-start samples from performance thresholds")
    sp.add_argument("--no-restart", action="store_true")
    sp.add_argument("--no-auto-equip", action="store_true")
    sp.add_argument("--continue-current", action="store_true",
                    help="do not restart the current scene before measurement")
    sp.add_argument("--min-fps", type=float)
    sp.add_argument("--max-physics-ms", type=float)
    sp.add_argument("--require-floor", type=int)
    sp.add_argument("--report", help="write full JSON samples and event timeline")
    sp = game_sub.add_parser("bridge-action", help="send one semantic action without coordinates")
    sp.add_argument("package")
    sp.add_argument("action", choices=("attack", "jump", "dodge", "skill_1", "skill_2", "skill_3", "skill_4", "pause"))
    sp = game_sub.add_parser("bridge-move", help="set a semantic movement vector")
    sp.add_argument("package")
    sp.add_argument("x", type=float)
    sp.add_argument("y", type=float)
    sp.add_argument("--seconds", type=float, default=0.0, help="auto-release after this duration")
    gateway = sub.add_parser("gateway", help="manage the single phone-control gateway")
    gateway_sub = gateway.add_subparsers(dest="gateway_cmd", required=True)
    for command in ("start", "status", "doctor", "lock-state", "unlock"):
        gateway_sub.add_parser(command)
    sp = gateway_sub.add_parser("audit")
    sp.add_argument("--limit", type=int, default=50)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    client = GatewayClient()
    if args.cmd == "gateway":
        try:
            if args.gateway_cmd == "start": output = client.ensure_running()
            elif args.gateway_cmd == "status": output = client.status()
            elif args.gateway_cmd == "doctor": output = client.doctor()
            elif args.gateway_cmd == "audit": output = client.audit_tail(args.limit)
            else:
                gateway_phone = GatewayPhone(project=args.project, purpose=f"cli:{args.gateway_cmd}", client=client)
                try:
                    output = (gateway_phone.lock_state() if args.gateway_cmd == "lock-state" else
                              gateway_phone.unlock_pattern(getpass.getpass("Current unlock pattern (not stored): ")))
                finally:
                    gateway_phone.close()
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0
        except (ADBError, OSError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    phone = GatewayPhone(serial=args.serial, project=args.project, purpose=f"cli:{args.cmd}", client=client)
    try:
        if args.cmd == "screenshot":
            data = phone.screenshot_png()
            if args.scale != 1.0:
                import io
                from PIL import Image

                img = Image.open(io.BytesIO(data))
                img = img.resize((round(img.width * args.scale), round(img.height * args.scale)))
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                data = buf.getvalue()
            with open(args.out, "wb") as f:
                f.write(data)
            print(f"saved {args.out} ({len(data)} bytes)")
        elif args.cmd == "dump":
            for n in phone.ui_dump():
                print(n.one_line())
        elif args.cmd == "size":
            print("%dx%d" % phone.screen_size())
        elif args.cmd == "devices":
            print("\n".join(phone.devices()))
        elif args.cmd == "tap":
            phone.tap(args.x, args.y)
        elif args.cmd == "tap-text":
            node = phone.tap_text(args.text, args.index)
            print(f"tapped {node.one_line()}")
        elif args.cmd == "long-press":
            phone.long_press(args.x, args.y, args.ms)
        elif args.cmd == "swipe":
            phone.swipe(args.x1, args.y1, args.x2, args.y2, args.ms)
        elif args.cmd == "text":
            phone.input_text(args.text)
        elif args.cmd == "key":
            phone.press_key(args.key)
        elif args.cmd == "launch":
            phone.launch_app(args.package)
        elif args.cmd == "current":
            print(phone.current_app())
        elif args.cmd == "apps":
            print("\n".join(phone.list_apps(args.keyword, not args.all)))
        elif args.cmd == "shell":
            print(phone.shell(args.command))
        elif args.cmd == "logs":
            output = phone.capture_app_logs(
                args.package, limit=args.limit, min_priority=args.priority,
                include_crash=not args.no_crash,
            )
            if args.summary_only:
                output = {key: value for key, value in output.items() if key != "logs"}
            payload = json.dumps(output, ensure_ascii=False, indent=2)
            if args.output:
                report_path = Path(args.output)
                report_path.write_text(payload + "\n", encoding="utf-8")
                os.chmod(report_path, 0o600)
                print(json.dumps({"saved": str(report_path), "line_count": output["line_count"],
                                  "finding_count": output["finding_count"],
                                  "counts": output["counts"]}, ensure_ascii=False, indent=2))
            else:
                print(payload)
        elif args.cmd == "game":
            if args.game_cmd == "validate":
                plan = load_plan(args.plan)
                print(
                    f"valid: {plan['name']} "
                    f"({plan['coordinate_space']['width']}x{plan['coordinate_space']['height']})"
                )
            elif args.game_cmd == "probe":
                width, height = phone.display_size()
                print(json.dumps({
                    "display_size": [width, height],
                    "orientation": "landscape" if width > height else "portrait",
                    "foreground_app": phone.current_app(),
                    "adb_multitouch": "not_guaranteed",
                }, ensure_ascii=False, indent=2))
            elif args.game_cmd == "run":
                plan = load_plan(args.plan)
                stop_event = threading.Event()
                runner = MacroRunner(
                    phone,
                    stop_event=stop_event,
                    max_seconds=args.max_seconds,
                    seed=args.seed,
                )
                try:
                    result = runner.run(plan)
                except KeyboardInterrupt:
                    stop_event.set()
                    print("stop requested", file=sys.stderr)
                    return 130
                payload = json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
                print(payload)
                if args.report:
                    Path(args.report).write_text(payload + "\n", encoding="utf-8")
                return 0 if result.status in {"completed", "timed_out"} else 1
            else:
                bridge = GameBridgeClient(phone, args.package)
                if args.game_cmd == "bridge-enable":
                    output = bridge.enable()
                elif args.game_cmd == "bridge-disable":
                    output = bridge.disable()
                elif args.game_cmd == "bridge-state":
                    output = bridge.state()
                elif args.game_cmd == "bot-start":
                    output = bridge.start_bot(
                        profile=args.profile,
                        max_seconds=args.max_seconds,
                        auto_restart=not args.no_restart,
                        auto_equip=not args.no_auto_equip,
                        attack_interval_ticks=args.attack_interval_ticks,
                        skill_interval_ticks=args.skill_interval_ticks,
                    )
                elif args.game_cmd == "bot-watch":
                    output = bridge.run_soak(
                        profile=args.profile,
                        max_seconds=args.max_seconds,
                        sample_seconds=args.sample_seconds,
                        warmup_seconds=args.warmup_seconds,
                        auto_restart=not args.no_restart,
                        auto_equip=not args.no_auto_equip,
                        minimum_fps=args.min_fps,
                        maximum_physics_ms=args.max_physics_ms,
                        required_floor=args.require_floor,
                        fresh_restart=not args.continue_current,
                    )
                    if args.report:
                        Path(args.report).write_text(
                            json.dumps(output, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
                    summary = {key: output[key] for key in (
                        "schema_version", "status", "package", "profile",
                        "elapsed_seconds", "summary", "violations",
                    )}
                    print(json.dumps(summary, ensure_ascii=False, indent=2))
                    return 0 if output["status"] == "completed" else 2
                elif args.game_cmd == "bot-stop":
                    output = bridge.stop_bot()
                elif args.game_cmd == "bridge-action":
                    output = bridge.command("action", {"name": args.action})
                elif args.game_cmd == "bridge-move":
                    output = bridge.command("move", {
                        "x": args.x,
                        "y": args.y,
                        "duration_seconds": args.seconds,
                    })
                print(json.dumps(output, ensure_ascii=False, indent=2))
    except (ADBError, GameBridgeError, PlanError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        phone.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

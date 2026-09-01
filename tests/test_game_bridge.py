"""Tests for the host-side semantic debug bridge protocol."""

import base64
import json
import re
import unittest
from unittest.mock import patch

from phone_ctl.game_bridge import GameBridgeClient, GameBridgeError


class FakeBridgePhone:
    def __init__(self):
        self.enabled = False
        self.launched = []
        self.last_command = None
        self.response = {}

    def current_app(self):
        return "com.example.game/.Main"

    def launch_app(self, package):
        self.launched.append(package)

    def shell(self, command, timeout=60):
        if " sh -c " not in command:
            return ""
        if " sh -c id" in command:
            return "uid=10123(u0_a123)"
        if "test -f files/game_test_bridge_enabled" in command:
            return "yes\n" if self.enabled else "no\n"
        if ": > files/game_test_bridge_enabled" in command:
            self.enabled = True
            return ""
        if "rm -f files/game_test_bridge_enabled" in command:
            self.enabled = False
            return ""
        if "cat files/game_test_response.json" in command:
            return json.dumps(self.response)
        if "cat files/game_test_state.json" in command:
            return json.dumps({"schema_version": 1, "room": {"state": "FIGHTING"}})
        match = re.search(r"printf %s ([A-Za-z0-9+/=]+) \| base64", command)
        if match:
            self.last_command = json.loads(base64.b64decode(match.group(1)))
            self.response = {
                "id": self.last_command["id"],
                "ok": True,
                "op": self.last_command["op"],
            }
        return ""


class GameBridgeClientTest(unittest.TestCase):
    def test_rejects_shell_metacharacters_in_package(self):
        with self.assertRaises(GameBridgeError):
            GameBridgeClient(FakeBridgePhone(), "com.example;rm")

    def test_enable_probes_and_relaunches(self):
        phone = FakeBridgePhone()
        result = GameBridgeClient(phone, "com.example.game").enable()
        self.assertTrue(result["enabled"])
        self.assertEqual(phone.launched, ["com.example.game"])

    def test_reads_structured_state(self):
        state = GameBridgeClient(FakeBridgePhone(), "com.example.game").state()
        self.assertEqual(state["schema_version"], 1)
        self.assertEqual(state["room"]["state"], "FIGHTING")

    def test_start_bot_sends_semantic_configuration(self):
        phone = FakeBridgePhone()
        result = GameBridgeClient(phone, "com.example.game").start_bot(
            profile="legend", max_seconds=42, auto_equip=False
        )
        self.assertTrue(result["ok"])
        self.assertEqual(phone.last_command["op"], "start_bot")
        self.assertEqual(phone.last_command["args"]["profile"], "legend")
        self.assertEqual(phone.last_command["args"]["max_seconds"], 42)
        self.assertFalse(phone.last_command["args"]["auto_equip"])

    def test_soak_report_aggregates_progress_and_thresholds(self):
        client = GameBridgeClient(FakeBridgePhone(), "com.example.game")
        client.start_bot = lambda **_kwargs: {"ok": True}  # type: ignore[method-assign]
        states = iter([
            {
                "room": {"elapsed_seconds": 10, "defeated": 4, "floor": 2,
                         "absolute_wave_index": 4, "active_enemies": 3, "state": "FIGHTING"},
                "player": {"health": 80, "stats": {"attack_speed_percent": 5,
                                                     "cooldown_reduction_percent": 3}},
                "combat": {"maximum_combo": 12},
                "performance": {"fps": 60, "physics_ms": 8, "draw_calls": 180},
                "inventory": [], "equipment": {}, "events": [],
                "bridge": {"bot_active": True},
            },
            {
                "room": {"elapsed_seconds": 20, "defeated": 9, "floor": 3,
                         "absolute_wave_index": 7, "active_enemies": 1, "state": "FIGHTING"},
                "player": {"health": 55, "stats": {"attack_speed_percent": 9,
                                                     "cooldown_reduction_percent": 7}},
                "combat": {"maximum_combo": 20},
                "performance": {"fps": 52, "physics_ms": 30, "draw_calls": 200},
                "inventory": [], "equipment": {}, "events": [],
                "bridge": {"bot_active": False},
            },
        ])
        client.state = lambda: next(states)  # type: ignore[method-assign]
        with patch("phone_ctl.game_bridge.time.sleep"):
            report = client.run_soak(
                max_seconds=10, sample_seconds=0.25, warmup_seconds=0,
                minimum_fps=55, required_floor=3, fresh_restart=False
            )
        self.assertEqual(report["status"], "failed_thresholds")
        self.assertEqual(report["summary"]["total_defeated"], 9)
        self.assertEqual(report["summary"]["maximum_floor"], 3)
        self.assertEqual(report["summary"]["minimum_sampled_fps"], 52)
        self.assertEqual(report["summary"]["maximum_attack_speed_percent"], 9)


if __name__ == "__main__":
    unittest.main()

"""Tests for validation, scaling, cancellation and background game jobs."""

import threading
import time
import unittest

from phone_ctl.game_jobs import GameJobManager
from phone_ctl.game_macro import MacroRunner, PlanError, validate_plan


class FakePhone:
    def __init__(self):
        self.calls = []
        self.app = "com.example.game/.Main"

    def display_size(self):
        return 1356, 610

    def current_app(self):
        self.calls.append(("current",))
        return self.app

    def tap(self, x, y):
        self.calls.append(("tap", x, y))

    def swipe(self, x1, y1, x2, y2, ms):
        self.calls.append(("swipe", x1, y1, x2, y2, ms))

    def press_key(self, key):
        self.calls.append(("key", key))


def plan(actions, **extra):
    return {
        "version": 1,
        "name": "test-plan",
        "coordinate_space": {"width": 2712, "height": 1220},
        "actions": actions,
        **extra,
    }


class ValidationTest(unittest.TestCase):
    def test_accepts_nested_game_actions(self):
        value = plan([{
            "type": "parallel",
            "tracks": [
                [{"type": "repeat", "count": 2, "actions": [{"type": "tap", "x": 1, "y": 2}]}],
                [{"type": "random_choice", "choices": [[{"type": "sleep", "seconds": 0.1}]]}],
            ],
        }])
        validate_plan(value)

    def test_rejects_unbounded_repeat_and_unknown_actions(self):
        with self.assertRaisesRegex(PlanError, "exactly one"):
            validate_plan(plan([{"type": "repeat", "actions": [{"type": "sleep", "seconds": 1}]}]))
        with self.assertRaisesRegex(PlanError, "unsupported"):
            validate_plan(plan([{"type": "shell", "command": "anything"}]))

    def test_rejects_too_fast_explicit_interval(self):
        with self.assertRaisesRegex(PlanError, "interval_seconds"):
            validate_plan(plan([{
                "type": "repeat",
                "count": 2,
                "interval_seconds": 0.001,
                "actions": [{"type": "tap", "x": 1, "y": 2}],
            }]))


class RunnerTest(unittest.TestCase):
    def test_scales_coordinates_and_repeats(self):
        phone = FakePhone()
        result = MacroRunner(phone, max_seconds=2).run(plan([{
            "type": "repeat",
            "count": 2,
            "actions": [{"type": "tap", "x": 1356, "y": 610}],
        }]))
        self.assertEqual(result.status, "completed")
        self.assertEqual(phone.calls, [("tap", 678, 305), ("tap", 678, 305)])
        self.assertEqual(result.adb_calls, 2)

    def test_foreground_guard_fails_closed(self):
        phone = FakePhone()
        phone.app = "com.other/.Main"
        result = MacroRunner(phone, max_seconds=2).run(
            plan([{"type": "tap", "x": 1, "y": 1}], package="com.example.game")
        )
        self.assertEqual(result.status, "failed")
        self.assertIn("expected foreground", result.error)
        self.assertFalse(any(call[0] == "tap" for call in phone.calls))

    def test_external_stop_interrupts_sleep(self):
        phone = FakePhone()
        stopped = threading.Event()
        timer = threading.Timer(0.03, stopped.set)
        timer.start()
        result = MacroRunner(phone, stop_event=stopped, max_seconds=2).run(
            plan([{"type": "sleep", "seconds": 1}])
        )
        timer.join()
        self.assertEqual(result.status, "stopped")
        self.assertLess(result.elapsed_seconds, 0.5)

    def test_parallel_tracks_both_execute(self):
        phone = FakePhone()
        result = MacroRunner(phone, max_seconds=2).run(plan([{
            "type": "parallel",
            "tracks": [
                [{"type": "tap", "x": 100, "y": 100}],
                [{"type": "key", "key": "SPACE"}],
            ],
        }]))
        self.assertEqual(result.status, "completed")
        self.assertIn(("tap", 50, 50), phone.calls)
        self.assertIn(("key", "SPACE"), phone.calls)


class JobManagerTest(unittest.TestCase):
    def test_job_lifecycle_and_single_active_guard(self):
        manager = GameJobManager(FakePhone)
        value = plan([{"type": "sleep", "seconds": 0.2}])
        started = manager.start(value, max_seconds=2)
        with self.assertRaisesRegex(RuntimeError, "already"):
            manager.start(value, max_seconds=2)
        manager.stop(started["job_id"])
        deadline = time.time() + 1
        while time.time() < deadline:
            status = manager.status(started["job_id"])
            if status["state"] not in {"starting", "running", "stopping"}:
                break
            time.sleep(0.01)
        self.assertEqual(status["state"], "stopped")


if __name__ == "__main__":
    unittest.main()

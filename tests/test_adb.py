"""Unit tests for phone_ctl.adb (adb subprocess calls are mocked)."""

import unittest
from unittest.mock import MagicMock, patch

from phone_ctl.adb import ADBError, Phone, UINode

UI_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout"
        content-desc="" clickable="false" bounds="[0,0][1220,2712]">
    <node index="0" text="立即购买" resource-id="com.x:id/buy"
          class="android.widget.Button" content-desc="" clickable="true"
          bounds="[100,2400][1120,2560]"/>
    <node index="1" text="" resource-id="" class="android.widget.ImageView"
          content-desc="返回" clickable="true" bounds="[20,60][140,180]"/>
    <node index="2" text="价格 ¥1999" resource-id="" class="android.widget.TextView"
          content-desc="" clickable="false" bounds="[100,2200][800,2300]"/>
  </node>
</hierarchy>
"""


def fake_completed(stdout=b"", returncode=0, stderr=b""):
    m = MagicMock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


class ParseUiXmlTest(unittest.TestCase):
    def test_parse_filters_and_centers(self):
        nodes = Phone.parse_ui_xml(UI_XML)
        self.assertEqual(len(nodes), 3)
        buy = nodes[0]
        self.assertEqual(buy.text, "立即购买")
        self.assertTrue(buy.clickable)
        self.assertEqual(buy.center, (610, 2480))
        self.assertEqual(nodes[1].desc, "返回")
        self.assertIn("立即购买", buy.one_line())

    def test_skips_empty_nodes(self):
        xml = '<hierarchy><node text="" content-desc="" clickable="false" bounds="[0,0][1,1]"/></hierarchy>'
        self.assertEqual(Phone.parse_ui_xml(xml), [])


class CommandBuildingTest(unittest.TestCase):
    def setUp(self):
        self.phone = Phone(serial="TESTSERIAL")

    def _run(self, stdout=b""):
        return patch("phone_ctl.adb.subprocess.run", return_value=fake_completed(stdout))

    def test_serial_prefix(self):
        with self._run() as run:
            self.phone.tap(100, 200)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[:3], ["adb", "-s", "TESTSERIAL"])
        self.assertEqual(cmd[3:], ["shell", "input tap 100 200"])

    def test_swipe_and_long_press(self):
        with self._run() as run:
            self.phone.swipe(1, 2, 3, 4, 500)
            self.phone.long_press(10, 20, 800)
        calls = [c[0][0][-1] for c in run.call_args_list]
        self.assertEqual(calls[0], "input swipe 1 2 3 4 500")
        self.assertEqual(calls[1], "input swipe 10 20 10 20 800")

    def test_input_text_escapes_spaces(self):
        with self._run() as run:
            self.phone.input_text("hello world 100%")
        cmd = run.call_args[0][0][-1]
        self.assertIn("hello%sworld%s100%25", cmd)

    def test_input_text_rejects_non_ascii(self):
        with self.assertRaises(ADBError):
            self.phone.input_text("你好")

    def test_press_key_names_and_codes(self):
        with self._run() as run:
            self.phone.press_key("back")
            self.phone.press_key(66)
        codes = [c[0][0][-1] for c in run.call_args_list]
        self.assertEqual(codes, ["input keyevent 4", "input keyevent 66"])
        with self.assertRaises(ADBError):
            self.phone.press_key("NOPE")

    def test_screenshot_requires_png(self):
        with self._run(b"\x89PNG\r\n\x1a\n...") as run:
            self.assertTrue(self.phone.screenshot_png().startswith(b"\x89PNG"))
        self.assertEqual(run.call_args[0][0][-3:], ["exec-out", "screencap", "-p"])
        with self._run(b"not a png"):
            with self.assertRaises(ADBError):
                self.phone.screenshot_png()

    def test_current_app_parses_resumed(self):
        out = b"  topResumedActivity=ActivityRecord{u0 com.tencent.mm/.ui.LauncherUI t123}\n"
        with self._run(out):
            self.assertEqual(self.phone.current_app(), "com.tencent.mm/.ui.LauncherUI")

    def test_display_size_uses_rotated_logical_viewport(self):
        out = b"mViewports=[DisplayViewport{logicalFrame=Rect(0, 0 - 2712, 1220)}]"
        with self._run(out):
            self.assertEqual(self.phone.display_size(), (2712, 1220))

    def test_display_size_supports_android_16_viewport_format(self):
        out = b"Viewport INTERNAL: isActive=[1], logicalFrame=[0, 0, 2712, 1220]"
        with self._run(out):
            self.assertEqual(self.phone.display_size(), (2712, 1220))

    def test_tap_text_flow(self):
        with patch("phone_ctl.adb.subprocess.run") as run:
            run.side_effect = [fake_completed(b"UI hierchary dumped"),  # uiautomator dump
                               fake_completed(UI_XML.encode()),        # cat xml
                               fake_completed(b"")]                    # input tap
            node = self.phone.tap_text("立即购买")
        self.assertEqual(node.center, (610, 2480))
        self.assertEqual(run.call_args_list[2][0][0][-1], "input tap 610 2480")

    def test_adb_error_raises(self):
        with patch("phone_ctl.adb.subprocess.run",
                   return_value=fake_completed(returncode=1, stderr=b"device offline")):
            with self.assertRaises(ADBError):
                self.phone.tap(1, 1)


if __name__ == "__main__":
    unittest.main()

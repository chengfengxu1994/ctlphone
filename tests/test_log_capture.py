from phone_ctl.log_capture import _parse_lines, capture_app_logs


class FakePhone:
    def __init__(self):
        self.commands = []

    def devices(self):
        return ["SERIAL-SECRET"]

    def shell(self, command, timeout=60):
        self.commands.append(command)
        if command.startswith("pidof "):
            return "123\n"
        if "-b crash" in command:
            return (
                "08-31 12:00:02.000  999  999 F AndroidRuntime: Process: other.app, PID: 999\n"
                "08-31 12:00:02.001  999  999 F AndroidRuntime: FATAL EXCEPTION: main\n"
                "08-31 12:00:03.000  123  123 F AndroidRuntime: Process: com.example.game, PID: 123\n"
                "08-31 12:00:03.001  123  123 F AndroidRuntime: FATAL EXCEPTION: main\n"
            )
        return (
            "08-31 12:00:00.000  123  124 E godot   : SCRIPT ERROR: bad call token=abc123\n"
            "08-31 12:00:01.000  123  124 W godot   : request https://example.test/a?q=private SERIAL-SECRET\n"
            "08-31 12:00:01.100  123  124 E net     : Authorization: Bearer very-private-value\n"
        )


def test_capture_filters_classifies_and_redacts():
    phone = FakePhone()
    report = capture_app_logs(phone, "com.example.game", limit=100)
    messages = "\n".join(line["message"] for line in report["logs"])
    assert report["pids"] == [123]
    assert report["requested_limit"] == 100
    assert report["counts"]["script_error"] == 1
    assert report["counts"]["crash"] >= 1
    assert "other.app" not in messages
    assert "abc123" not in messages
    assert "private" not in messages
    assert "SERIAL-SECRET" not in messages
    assert "very-private-value" not in messages
    assert "[REDACTED]" in messages and "[DEVICE]" in messages
    pid_command = next(command for command in phone.commands if "--pid=123" in command)
    assert "| tail -n 100" in pid_command
    assert "-t 100" not in pid_command


def test_invalid_package_is_rejected():
    try:
        capture_app_logs(FakePhone(), "bad;package")
    except Exception as exc:
        assert "package" in str(exc)
    else:
        raise AssertionError("unsafe package unexpectedly accepted")

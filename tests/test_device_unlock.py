import pytest

from phone_ctl.device_unlock import UnlockError, _pattern_points, unlock_pattern


def test_pattern_points_use_view_geometry():
    assert _pattern_points("12369", (0, 300, 900, 1200)) == [
        (150, 450), (450, 450), (750, 450), (750, 750), (750, 1050)
    ]


@pytest.mark.parametrize("pattern", ["123", "1111", "0123", "1234567891"])
def test_pattern_rejects_unsafe_values(pattern):
    with pytest.raises(UnlockError): _pattern_points(pattern, (0, 0, 900, 900))


class UnlockedPhone:
    def devices(self): return ["one"]
    def shell(self, command, timeout=60):
        if command == "dumpsys window policy": return "keyguardShowing=false mScreenOnFully=true"
        if "dumpsys activity" in command: return ""
        return ""
    def current_app(self): return "game/.Main"


def test_already_unlocked_never_sends_pattern():
    result = unlock_pattern(UnlockedPhone(), "1236")
    assert result["status"] == "already_unlocked"

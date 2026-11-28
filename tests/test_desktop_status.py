import datetime
import tempfile
from pathlib import Path
import unittest

from battery_monitor import BatteryMonitor
from desktop_ui import (
    DesktopPanel,
    _nightcord_clock_str,
    create_tray_icon_image,
    evaluate_edge_suppression,
    render_nightcord_emblem,
)
from server import _validated_sample_rate


def _make_pkt(
    model_id="0x2014",
    model="AirPods Pro 2",
    address="AA:AA:AA:AA:AA:01",
    color=0,
    left=80,
    right=80,
    case=60,
    rssi=-50,
    name="",
):
    return {
        "modelId": model_id,
        "model": model,
        "address": address,
        "color": color,
        "name": name,
        "isSingle": False,
        "left": left,
        "right": right,
        "case": case,
        "single": None,
        "leftCharging": False,
        "rightCharging": False,
        "caseCharging": False,
        "singleCharging": False,
        "leftInEar": True,
        "rightInEar": True,
        "rssi": rssi,
    }


class DesktopStatusTests(unittest.TestCase):
    def test_validated_sample_rate_rejects_invalid_values(self):
        self.assertEqual(_validated_sample_rate(48000), 48000)
        self.assertEqual(_validated_sample_rate("44100"), 44100)
        self.assertIsNone(_validated_sample_rate(None))
        self.assertIsNone(_validated_sample_rate("unknown"))
        self.assertIsNone(_validated_sample_rate(1000))

    def test_airpods_snapshot_contains_advertised_name(self):
        monitor = BatteryMonitor()
        packet = _make_pkt(model_id="0x2027", model="AirPods Pro 3", name="My AirPods", right=70)

        monitor._on_airpods_packet(packet)
        self.assertEqual(monitor.get_snapshot()["airpods"]["name"], "My AirPods")

        packet["name"] = ""
        monitor._on_airpods_packet(packet)
        self.assertEqual(monitor.get_snapshot()["airpods"]["name"], "My AirPods")

    def test_bystander_different_or_same_model_airpods_do_not_hijack(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / ".airpods_binding.json"
            monitor = BatteryMonitor(state_file=state_file)

            # Distant neighbor (-72 dBm) is ignored before binding
            monitor._on_airpods_packet(
                _make_pkt(model_id="0x2019", model="AirPods 4", address="11:22:33:44:55:66", left=30, right=30, rssi=-72)
            )
            self.assertFalse(monitor.get_snapshot()["airpods"]["online"])

            # User opens their own AirPods Pro 2 at desk (-50 dBm) -> auto-binds
            monitor._on_airpods_packet(
                _make_pkt(model_id="0x2014", model="AirPods Pro 2", address="AA:AA:AA:AA:AA:01", left=90, right=80, rssi=-50)
            )
            ap = monitor.get_snapshot()["airpods"]
            self.assertTrue(ap["locked"])
            self.assertEqual(ap["left"], 90)
            self.assertEqual(ap["model"], "AirPods Pro 2")

            # Bystander right next to PC (-46 dBm) opens AirPods 4 -> blocked by model lock
            monitor._on_airpods_packet(
                _make_pkt(model_id="0x2019", model="AirPods 4", address="22:22:22:22:22:22", left=20, right=20, rssi=-46)
            )
            self.assertEqual(monitor.get_snapshot()["airpods"]["model"], "AirPods Pro 2")
            self.assertEqual(monitor.get_snapshot()["airpods"]["left"], 90)

            # Second bystander has the EXACT same AirPods Pro 2 (different MAC) -> blocked while active
            monitor._on_airpods_packet(
                _make_pkt(model_id="0x2014", model="AirPods Pro 2", address="BB:BB:BB:BB:BB:02", left=30, right=40, rssi=-56)
            )
            self.assertEqual(monitor.get_snapshot()["airpods"]["left"], 90)

            # Simulate user's AirPods going silent for 60s (lid closed in ears); bystander with discontinuous battery still blocked
            monitor._airpods["updatedAt"] -= 60.0
            monitor._on_airpods_packet(
                _make_pkt(model_id="0x2014", model="AirPods Pro 2", address="BB:BB:BB:BB:BB:02", left=30, right=40, rssi=-62)
            )
            self.assertEqual(monitor.get_snapshot()["airpods"]["left"], 90)

            # Simulate user's own AirPods rotating BLE RPA MAC with continuous battery (80%/80%) -> accepted
            monitor._on_airpods_packet(
                _make_pkt(model_id="0x2014", model="AirPods Pro 2", address="CC:CC:CC:CC:CC:03", left=80, right=80, rssi=-58)
            )
            self.assertEqual(monitor.get_snapshot()["airpods"]["left"], 80)
            self.assertEqual(monitor.get_snapshot()["airpods"]["activeKey"], "CC:CC:CC:CC:CC:03")

            # Restarting BatteryMonitor reloads persistent lock and still blocks other models before lid open
            monitor2 = BatteryMonitor(state_file=state_file)
            monitor2._on_airpods_packet(
                _make_pkt(model_id="0x2019", model="AirPods 4", address="33:33:33:33:33:33", left=50, right=50, rssi=-48)
            )
            self.assertFalse(monitor2.get_snapshot()["airpods"]["online"])
            self.assertEqual(monitor2.get_snapshot()["airpods"]["model"], "AirPods Pro 2")

    def test_reject_current_airpods_blacklists_and_switches_to_other_candidate(self):
        monitor = BatteryMonitor()
        # Suppose roommate's pair was acquired first, and user's pair is also nearby
        monitor._on_airpods_packet(
            _make_pkt(model_id="0x2019", model="AirPods 4", address="11:11:11:11:11:11", left=40, right=40, rssi=-52)
        )
        monitor._on_airpods_packet(
            _make_pkt(model_id="0x2014", model="AirPods Pro 2", address="AA:AA:AA:AA:AA:01", left=90, right=90, rssi=-54)
        )
        self.assertEqual(monitor.get_snapshot()["airpods"]["model"], "AirPods 4")

        # User clicks "这不是我的耳机" -> blacklists AirPods 4 MAC and switches to AirPods Pro 2
        monitor.reject_current_airpods()
        ap = monitor.get_snapshot()["airpods"]
        self.assertEqual(ap["model"], "AirPods Pro 2")
        self.assertEqual(ap["left"], 90)

    def test_battery_text_does_not_add_wearing_ear_icon(self):
        text, _ = DesktopPanel._format_pod_value(None, 85, False, True)
        self.assertEqual(text, "85%")

    def test_nightcord_clock_and_icons(self):
        self.assertEqual(
            _nightcord_clock_str(datetime.datetime(2026, 11, 27, 1, 5)),
            "25:05",
        )
        self.assertEqual(
            _nightcord_clock_str(datetime.datetime(2026, 11, 27, 20, 15)),
            "25:00 · 20:15",
        )
        emblem = render_nightcord_emblem(48)
        self.assertEqual(emblem.size, (48, 48))
        self.assertEqual(emblem.mode, "RGBA")
        self.assertEqual(emblem.getpixel((0, 0))[3], 0)

        tray = create_tray_icon_image(connected=True, low_battery=False)
        self.assertEqual(tray.size, (64, 64))
        self.assertEqual(tray.mode, "RGBA")

    def test_game_and_camera_lock_suppresses_right_edge_drawer(self):
        # Normal maximized desktop window (e.g. JetBrains IDE / Chrome) -> allowed
        base_desktop = {
            "cursor_info_ok": True,
            "cursor_flags": 1,
            "mouse_button_down": False,
            "clip_rect": (0, 0, 2048, 1152),
            "virtual_screen": (0, 0, 2048, 1152),
            "qstate": 5,
            "fg_is_self": False,
            "fg_class": "SunAwtFrame",
            "fg_rect": (-7, -7, 2055, 1111),
            "fg_monitor_rect": (0, 0, 2048, 1152),
            "fg_style": 0x17CF0000,
        }
        self.assertEqual(evaluate_edge_suppression(base_desktop), (False, ""))

        # Bare Windows Desktop (WorkerW covering full monitor) -> allowed
        bare_desktop = {
            **base_desktop,
            "fg_class": "WorkerW",
            "fg_rect": (0, 0, 2048, 1152),
            "fg_style": 0x16000000,
        }
        self.assertEqual(evaluate_edge_suppression(bare_desktop), (False, ""))

        # 3D camera-rotation game hiding cursor (even in small windowed mode) -> suppressed
        fps_camera = {**base_desktop, "cursor_flags": 0}
        self.assertEqual(evaluate_edge_suppression(fps_camera), (True, "cursor_hidden"))

        # Right-click / Left-click camera drag in windowed RPG -> suppressed
        rpg_drag = {**base_desktop, "mouse_button_down": True}
        self.assertEqual(evaluate_edge_suppression(rpg_drag), (True, "mouse_button_down"))

        # Windowed game trapping mouse via ClipCursor -> suppressed
        clipped_game = {**base_desktop, "clip_rect": (200, 120, 1800, 1020)}
        self.assertEqual(evaluate_edge_suppression(clipped_game), (True, "cursor_clipped"))

        # Borderless-fullscreen game (WS_POPUP covering full monitor, cursor visible) -> suppressed
        borderless_game = {
            **base_desktop,
            "fg_class": "UnityWndClass",
            "fg_rect": (0, 0, 2048, 1152),
            "fg_style": 0x96000000,
        }
        self.assertEqual(
            evaluate_edge_suppression(borderless_game), (True, "fullscreen_window")
        )

        # Exclusive D3D fullscreen -> suppressed
        d3d_game = {**base_desktop, "qstate": 3}
        self.assertEqual(evaluate_edge_suppression(d3d_game), (True, "d3d_fullscreen"))


if __name__ == "__main__":
    unittest.main()
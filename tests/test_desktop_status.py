import unittest

from battery_monitor import BatteryMonitor
from desktop_ui import DesktopPanel
from server import _validated_sample_rate


class DesktopStatusTests(unittest.TestCase):
    def test_validated_sample_rate_rejects_invalid_values(self):
        self.assertEqual(_validated_sample_rate(48000), 48000)
        self.assertEqual(_validated_sample_rate("44100"), 44100)
        self.assertIsNone(_validated_sample_rate(None))
        self.assertIsNone(_validated_sample_rate("unknown"))
        self.assertIsNone(_validated_sample_rate(1000))

    def test_airpods_snapshot_contains_advertised_name(self):
        monitor = BatteryMonitor()
        packet = {
            "model": "AirPods Pro 3",
            "modelId": "0x2027",
            "name": "My AirPods",
            "isSingle": False,
            "left": 80,
            "right": 70,
            "case": 60,
            "lastCase": 60,
            "single": None,
            "leftCharging": False,
            "rightCharging": False,
            "caseCharging": False,
            "singleCharging": False,
            "leftInEar": True,
            "rightInEar": True,
            "rssi": -50,
        }

        monitor._on_airpods_packet(packet)
        self.assertEqual(monitor.get_snapshot()["airpods"]["name"], "My AirPods")

        packet["name"] = ""
        monitor._on_airpods_packet(packet)
        self.assertEqual(monitor.get_snapshot()["airpods"]["name"], "My AirPods")

    def test_battery_text_does_not_add_wearing_ear_icon(self):
        text, _ = DesktopPanel._format_pod_value(None, 85, False, True)
        self.assertEqual(text, "85%")


if __name__ == "__main__":
    unittest.main()
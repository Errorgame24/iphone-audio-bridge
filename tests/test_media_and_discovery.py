import asyncio
import json
import unittest
from unittest.mock import patch

from lan_discovery import (
    DISCOVER_REQUEST_PREFIX,
    DISCOVER_RESPONSE_MAGIC,
    LanDiscoveryAdvertiser,
    _UdpDiscoveryProtocol,
    get_all_lan_ipv4s,
)
from media_control import (
    VK_MEDIA_NEXT_TRACK,
    VK_MEDIA_PLAY_PAUSE,
    VK_MEDIA_PREV_TRACK,
    WindowsMediaController,
    _clean_app_name,
)


class _DummyTransport:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))


class MediaAndDiscoveryTests(unittest.TestCase):
    def test_clean_app_name_strips_exe_and_publisher_id(self):
        self.assertEqual(_clean_app_name("Spotify.exe"), "Spotify")
        self.assertEqual(
            _clean_app_name("AppleInc.AppleMusicWin_nzyj5cx40ttqa!App"),
            "AppleMusicWin",
        )

    def test_media_controller_fallback_keys_without_gsmtc_session(self):
        pressed = []
        ctrl = WindowsMediaController(key_sender=lambda vk: pressed.append(vk) or True)

        async def _run():
            with patch.object(ctrl, "_get_manager", return_value=None):
                res_pause = await ctrl.execute_command("pause")
                self.assertTrue(res_pause["ok"])
                self.assertFalse(res_pause["nowPlaying"]["playing"])

                res_play = await ctrl.execute_command("play")
                self.assertTrue(res_play["ok"])
                self.assertTrue(res_play["nowPlaying"]["playing"])

                res_next = await ctrl.execute_command("next")
                self.assertTrue(res_next["ok"])

                res_prev = await ctrl.execute_command("prev")
                self.assertTrue(res_prev["ok"])

                res_bad = await ctrl.execute_command("invalid-cmd")
                self.assertFalse(res_bad["ok"])

        asyncio.run(_run())
        self.assertEqual(
            pressed,
            [
                VK_MEDIA_PLAY_PAUSE,
                VK_MEDIA_PLAY_PAUSE,
                VK_MEDIA_NEXT_TRACK,
                VK_MEDIA_PREV_TRACK,
            ],
        )

    def test_udp_discovery_protocol_replies_to_valid_probe(self):
        advertiser = LanDiscoveryAdvertiser(port=8443, hostname="Nightcord-PC")
        advertiser._primary_ip = "10.58.10.204"
        advertiser._device_name_getter = lambda: "CABLE Input"

        proto = _UdpDiscoveryProtocol(advertiser.build_discovery_payload)
        transport = _DummyTransport()
        proto.connection_made(transport)

        # Unrelated packet should be ignored
        proto.datagram_received(b"HELLO", ("10.58.10.55", 51234))
        self.assertEqual(len(transport.sent), 0)

        # Valid probe gets JSON reply
        proto.datagram_received(
            DISCOVER_REQUEST_PREFIX + b"_V1", ("10.58.10.55", 51234)
        )
        self.assertEqual(len(transport.sent), 1)
        raw, addr = transport.sent[0]
        self.assertEqual(addr, ("10.58.10.55", 51234))
        decoded = json.loads(raw.decode("utf-8"))
        self.assertEqual(decoded["magic"], DISCOVER_RESPONSE_MAGIC)
        self.assertEqual(decoded["hostname"], "Nightcord-PC")
        self.assertEqual(decoded["port"], 8443)
        self.assertIn("10.58.10.204", decoded["ips"])
        self.assertEqual(decoded["deviceName"], "CABLE Input")

    def test_get_all_lan_ipv4s_prioritizes_primary_and_filters_loopback(self):
        ips = get_all_lan_ipv4s("192.168.50.10")
        self.assertEqual(ips[0], "192.168.50.10")
        self.assertNotIn("127.0.0.1", ips)


if __name__ == "__main__":
    unittest.main()

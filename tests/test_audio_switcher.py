import tempfile
import unittest
from pathlib import Path

from audio_switcher import AudioDeviceSwitcher, E_CAPTURE, E_RENDER, ROLES


class FakeAudioBackend:
    def __init__(self):
        self.render_devices = [
            {"id": "render-cable", "name": "CABLE Input (VB-Audio Virtual Cable)"},
            {"id": "render-cable16", "name": "CABLE In 16ch (VB-Audio Virtual Cable)"},
            {"id": "render-pico", "name": "扬声器 (Pico Streaming Speaker)"},
            {"id": "render-realtek", "name": "扬声器 (Realtek(R) Audio)"},
        ]
        self.capture_devices = [
            {"id": "cap-cable", "name": "CABLE Output (VB-Audio Virtual Cable)"},
            {"id": "cap-realtek", "name": "麦克风阵列 (Realtek(R) Audio)"},
        ]
        self.default_render = {r: "render-realtek" for r in ROLES}
        self.default_capture = {r: "cap-realtek" for r in ROLES}

    def list_active_endpoints(self, data_flow: int) -> list:
        if data_flow == E_RENDER:
            return list(self.render_devices)
        return list(self.capture_devices)

    def get_default_endpoint_roles(self, data_flow: int) -> dict:
        if data_flow == E_RENDER:
            return dict(self.default_render)
        return dict(self.default_capture)

    def get_default_endpoint_id(self, data_flow: int, role: int = 0):
        if data_flow == E_RENDER:
            return self.default_render.get(role)
        return self.default_capture.get(role)

    def set_default_endpoint_roles(self, role_to_device_id: dict) -> bool:
        render_ids = {d["id"] for d in self.render_devices}
        capture_ids = {d["id"] for d in self.capture_devices}
        for role, did in role_to_device_id.items():
            if did in render_ids:
                self.default_render[role] = did
            elif did in capture_ids:
                self.default_capture[role] = did
            else:
                return False
        return True


class AudioDeviceSwitcherTests(unittest.TestCase):
    def test_startup_switch_manual_change_one_click_back_and_exit_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / ".audio_switcher_state.json"
            backend = FakeAudioBackend()
            switcher = AudioDeviceSwitcher(state_path, backend=backend)

            # 1. Startup switches to isolated virtual speaker + CABLE Output
            st = switcher.switch_on_startup()
            self.assertTrue(st["isOnVirtual"])
            self.assertEqual(backend.default_render[0], "render-pico")
            self.assertEqual(backend.default_capture[0], "cap-cable")
            self.assertEqual(st["originalRenderName"], "扬声器 (Realtek(R) Audio)")
            self.assertEqual(st["originalCaptureName"], "麦克风阵列 (Realtek(R) Audio)")

            # 2. Simulate user manually switching Windows output back to Realtek
            backend.default_render[0] = "render-realtek"
            st = switcher.refresh_status()
            self.assertFalse(st["isOnVirtual"])
            self.assertEqual(st["currentRenderName"], "扬声器 (Realtek(R) Audio)")

            # 3. One-click switch back to virtual sound card
            st = switcher.switch_to_virtual()
            self.assertTrue(st["isOnVirtual"])
            self.assertEqual(backend.default_render[0], "render-pico")
            self.assertEqual(backend.default_capture[0], "cap-cable")

            # 4. Exit restores original Realtek playback & mic for all roles
            switcher.restore_on_exit()
            for role in ROLES:
                self.assertEqual(backend.default_render[role], "render-realtek")
                self.assertEqual(backend.default_capture[role], "cap-realtek")

    def test_crash_recovery_preserves_original_physical_devices(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / ".audio_switcher_state.json"
            backend = FakeAudioBackend()

            # First session switches to virtual, then crashes without calling restore_on_exit()
            first = AudioDeviceSwitcher(state_path, backend=backend)
            first.switch_on_startup()
            self.assertEqual(backend.default_render[0], "render-pico")
            self.assertEqual(backend.default_capture[0], "cap-cable")

            # Second session starts while Windows is still stuck on virtual devices
            second = AudioDeviceSwitcher(state_path, backend=backend)
            st = second.switch_on_startup()
            self.assertEqual(st["originalRenderName"], "扬声器 (Realtek(R) Audio)")
            self.assertEqual(st["originalCaptureName"], "麦克风阵列 (Realtek(R) Audio)")

            # Clean exit on second session restores Realtek
            second.restore_on_exit()
            self.assertEqual(backend.default_render[0], "render-realtek")
            self.assertEqual(backend.default_capture[0], "cap-realtek")

    def test_preferred_virtual_render_choice_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / ".audio_switcher_state.json"
            backend = FakeAudioBackend()
            switcher = AudioDeviceSwitcher(state_path, backend=backend)
            switcher.switch_on_startup()

            st = switcher.switch_to_virtual(preferred_render_id="render-cable16")
            self.assertTrue(st["isOnVirtual"])
            self.assertEqual(backend.default_render[0], "render-cable16")
            switcher.restore_on_exit()

            # Next session remembers preferred virtual render card
            switcher2 = AudioDeviceSwitcher(state_path, backend=backend)
            st2 = switcher2.switch_on_startup()
            self.assertEqual(backend.default_render[0], "render-cable16")
            self.assertEqual(st2["virtualRenderId"], "render-cable16")
            switcher2.restore_on_exit()


if __name__ == "__main__":
    unittest.main()

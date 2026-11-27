from pathlib import Path
import tempfile
import unittest

from autostart import AutoStartManager, resolve_windowless_python


class FakeRegistryBackend:
    def __init__(self):
        self.store = {}

    def get_value(self, name: str):
        return self.store.get(name)

    def set_value(self, name: str, command: str) -> bool:
        self.store[name] = command
        return True

    def delete_value(self, name: str) -> bool:
        self.store.pop(name, None)
        return True


class AutoStartTests(unittest.TestCase):
    def test_prefers_pythonw_alongside_python_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            scripts = Path(tmp) / "Scripts"
            scripts.mkdir()
            py = scripts / "python.exe"
            pyw = scripts / "pythonw.exe"
            py.write_bytes(b"")
            pyw.write_bytes(b"")

            resolved = resolve_windowless_python(py)
            self.assertEqual(resolved.name.lower(), "pythonw.exe")

    def test_enable_toggle_and_disable_autostart(self):
        backend = FakeRegistryBackend()
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "server.py"
            pyw = Path(tmp) / "pythonw.exe"
            script.write_text("# server", encoding="utf-8")
            pyw.write_bytes(b"")

            mgr = AutoStartManager(
                script_path=script,
                python_exe=pyw,
                backend=backend,
            )
            self.assertFalse(mgr.is_enabled())

            self.assertTrue(mgr.set_enabled(True))
            self.assertTrue(mgr.is_enabled())
            self.assertEqual(
                backend.get_value("NightcordAudioBridge"),
                f'"{pyw.absolute()}" "{script.resolve()}"',
            )

            # Stale path to another folder reports disabled
            backend.set_value("NightcordAudioBridge", '"C:\\Other\\pythonw.exe" "C:\\Other\\server.py"')
            self.assertFalse(mgr.is_enabled())

            # Toggle re-enables with current path, second toggle removes
            self.assertTrue(mgr.toggle())
            self.assertTrue(mgr.is_enabled())
            self.assertFalse(mgr.toggle())
            self.assertFalse(mgr.is_enabled())


if __name__ == "__main__":
    unittest.main()

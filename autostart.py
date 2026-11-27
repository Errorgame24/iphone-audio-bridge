from pathlib import Path
import sys

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "NightcordAudioBridge"


class WindowsRegistryRunBackend:
    """Thin wrapper around HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run."""

    def get_value(self, name: str):
        if sys.platform != "win32":
            return None
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ
            ) as key:
                val, _reg_type = winreg.QueryValueEx(key, name)
                return str(val)
        except FileNotFoundError:
            return None
        except OSError:
            return None

    def set_value(self, name: str, command: str) -> bool:
        if sys.platform != "win32":
            return False
        import winreg

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
        return True

    def delete_value(self, name: str) -> bool:
        if sys.platform != "win32":
            return False
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, name)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False


def resolve_windowless_python(python_exe: Path = None) -> Path:
    """Prefer pythonw.exe in the active virtualenv so startup uses venv packages with no console."""
    if python_exe is not None:
        exe = Path(python_exe).absolute()
        if exe.name.lower() == "python.exe":
            pyw = exe.with_name("pythonw.exe")
            if pyw.exists():
                return pyw
        return exe

    venv_pyw = Path(sys.prefix).absolute() / "Scripts" / "pythonw.exe"
    if venv_pyw.exists():
        return venv_pyw

    exe = Path(sys.executable).absolute()
    if exe.name.lower() == "python.exe":
        pyw = exe.with_name("pythonw.exe")
        if pyw.exists():
            return pyw
    return exe


def build_autostart_command(script_path: Path = None, python_exe: Path = None) -> str:
    script = (
        Path(script_path).resolve()
        if script_path is not None
        else (Path(__file__).resolve().parent / "server.py")
    )
    py_bin = resolve_windowless_python(python_exe)
    return f'"{py_bin}" "{script}"'


class AutoStartManager:
    def __init__(
        self,
        script_path: Path = None,
        python_exe: Path = None,
        backend=None,
        value_name: str = RUN_VALUE_NAME,
    ):
        self._script_path = (
            Path(script_path).resolve()
            if script_path is not None
            else (Path(__file__).resolve().parent / "server.py")
        )
        self._python_exe = python_exe
        self._backend = backend or WindowsRegistryRunBackend()
        self._value_name = value_name

    @property
    def command(self) -> str:
        return build_autostart_command(self._script_path, self._python_exe)

    def is_enabled(self) -> bool:
        raw = self._backend.get_value(self._value_name)
        if not raw:
            return False
        return str(self._script_path).lower() in raw.lower()

    def set_enabled(self, enabled: bool) -> bool:
        if enabled:
            self._backend.set_value(self._value_name, self.command)
        else:
            self._backend.delete_value(self._value_name)
        return self.is_enabled()

    def toggle(self) -> bool:
        return self.set_enabled(not self.is_enabled())

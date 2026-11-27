import ctypes
import json
import sys
import threading
from ctypes import POINTER, HRESULT, Structure, Union, byref, c_uint, c_ulonglong, c_void_p, c_wchar_p
from ctypes.wintypes import BYTE, DWORD, WORD
from pathlib import Path


E_RENDER = 0
E_CAPTURE = 1
ROLES = (0, 1, 2)  # eConsole, eMultimedia, eCommunications
DEVICE_STATE_ACTIVE = 0x00000001
CLSCTX_ALL = 23
STGM_READ = 0
VT_LPWSTR = 31

# Virtual playback devices isolated from VB-Cable's CABLE Output microphone pipe
_ISOLATED_VIRTUAL_RENDER_KEYWORDS = (
    "pico streaming speaker",
    "streaming speaker",
    "steam streaming",
    "virtual desktop audio",
    "cable-a input",
    "cable-b input",
    "cable-c input",
    "cable-d input",
    "hi-fi cable input",
    "voicemeeter",
    "vaio",
    "virtual audio",
    "virtual speaker",
)

# Standard single VB-Cable playback endpoints (fallback if no isolated virtual speaker is installed)
_VBCABLE_RENDER_KEYWORDS = (
    "cable in 16ch",
    "cable input",
)

# Virtual capture endpoint fed by server.py's VB-Cable microphone output
_VIRTUAL_CAPTURE_KEYWORDS = (
    "cable output",
)


def classify_render_virtual_tier(name: str) -> int:
    """
    Return virtual speaker priority tier:
      1 = isolated virtual speaker (preferred, no crosstalk with VB-Cable mic)
      2 = standard VB-Cable input
      0 = physical / normal audio output device
    """
    lowered = (name or "").lower()
    if any(k in lowered for k in _ISOLATED_VIRTUAL_RENDER_KEYWORDS):
        return 1
    if any(k in lowered for k in _VBCABLE_RENDER_KEYWORDS):
        return 2
    return 0


def is_virtual_capture_device(name: str) -> bool:
    lowered = (name or "").lower()
    return any(k in lowered for k in _VIRTUAL_CAPTURE_KEYWORDS)


class _GUID(Structure):
    _fields_ = [
        ("Data1", DWORD),
        ("Data2", WORD),
        ("Data3", WORD),
        ("Data4", BYTE * 8),
    ]

    def __init__(self, l, w1, w2, *b):
        super().__init__(l, w1, w2, (BYTE * 8)(*b))


class _PROPERTYKEY(Structure):
    _fields_ = [
        ("fmtid", _GUID),
        ("pid", DWORD),
    ]


class _PROPVARIANT_UNION(Union):
    _fields_ = [
        ("pwszVal", c_wchar_p),
        ("_pad", c_ulonglong * 2),
    ]


class _PROPVARIANT(Structure):
    _fields_ = [
        ("vt", WORD),
        ("wReserved1", WORD),
        ("wReserved2", WORD),
        ("wReserved3", WORD),
        ("u", _PROPVARIANT_UNION),
    ]


_CLSID_MMDeviceEnumerator = _GUID(
    0xBCDE0395, 0xE52F, 0x467C, 0x8E, 0x3D, 0xC4, 0x57, 0x92, 0x91, 0x69, 0x2E
)
_IID_IMMDeviceEnumerator = _GUID(
    0xA95664D2, 0x9614, 0x4F35, 0xA7, 0x46, 0xDE, 0x8D, 0xB6, 0x36, 0x17, 0xE6
)
_PKEY_Device_FriendlyName = _PROPERTYKEY(
    _GUID(0xA45C254E, 0xDF1C, 0x4EFD, 0x80, 0x20, 0x67, 0xD1, 0x46, 0xA8, 0x50, 0xE0),
    14,
)

_CLSID_PolicyConfigClient = _GUID(
    0x870AF99C, 0x171D, 0x4F9E, 0xAF, 0x0D, 0xE6, 0x3D, 0xF4, 0x0C, 0x2B, 0xC9
)
_POLICY_CONFIG_IIDS = (
    # IPolicyConfig (Windows 7 / 8 / 10 / 11)
    _GUID(0xF8679F50, 0x850A, 0x41CF, 0x9C, 0x72, 0x43, 0x0F, 0x29, 0x02, 0x90, 0xC8),
    # IPolicyConfigVista fallback
    _GUID(0x568B9108, 0x44BF, 0x40B4, 0x90, 0x06, 0x86, 0xAF, 0xE5, 0xB5, 0xA6, 0x20),
)


def _com_release(ptr: c_void_p):
    if ptr and ptr.value:
        vt = ctypes.cast(ctypes.cast(ptr, POINTER(c_void_p))[0], POINTER(c_void_p))
        ctypes.WINFUNCTYPE(c_uint, c_void_p)(vt[2])(ptr)
        ptr.value = None


class WindowsAudioPolicyBackend:
    """Low-level COM wrapper around IMMDeviceEnumerator and IPolicyConfig."""

    def _co_init(self):
        if sys.platform != "win32":
            return False, False
        ole32 = ctypes.windll.ole32
        hr = ole32.CoInitializeEx(None, 0)
        if hr in (0, 1):
            return True, True
        # RPC_E_CHANGED_MODE (0x80010106): COM already initialized on this thread as STA
        if (hr & 0xFFFFFFFF) == 0x80010106:
            return True, False
        return False, False

    def _create_enumerator(self):
        ole32 = ctypes.windll.ole32
        enum_ptr = c_void_p()
        hr = ole32.CoCreateInstance(
            byref(_CLSID_MMDeviceEnumerator),
            None,
            CLSCTX_ALL,
            byref(_IID_IMMDeviceEnumerator),
            byref(enum_ptr),
        )
        if hr < 0 or not enum_ptr.value:
            return None
        return enum_ptr

    def _get_device_id(self, dev_ptr: c_void_p):
        ole32 = ctypes.windll.ole32
        dev_vt = ctypes.cast(ctypes.cast(dev_ptr, POINTER(c_void_p))[0], POINTER(c_void_p))
        get_id = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_wchar_p))(dev_vt[5])
        raw_id = c_wchar_p()
        try:
            if get_id(dev_ptr, byref(raw_id)) < 0 or not raw_id.value:
                return None
            return str(raw_id.value)
        finally:
            if raw_id.value:
                ole32.CoTaskMemFree(raw_id)

    def _get_device_friendly_name(self, dev_ptr: c_void_p) -> str:
        ole32 = ctypes.windll.ole32
        dev_vt = ctypes.cast(ctypes.cast(dev_ptr, POINTER(c_void_p))[0], POINTER(c_void_p))
        open_store = ctypes.WINFUNCTYPE(HRESULT, c_void_p, DWORD, POINTER(c_void_p))(dev_vt[4])
        store_ptr = c_void_p()
        if open_store(dev_ptr, STGM_READ, byref(store_ptr)) < 0 or not store_ptr.value:
            return ""
        try:
            store_vt = ctypes.cast(
                ctypes.cast(store_ptr, POINTER(c_void_p))[0], POINTER(c_void_p)
            )
            get_val = ctypes.WINFUNCTYPE(
                HRESULT, c_void_p, POINTER(_PROPERTYKEY), POINTER(_PROPVARIANT)
            )(store_vt[5])
            pv = _PROPVARIANT()
            if get_val(store_ptr, byref(_PKEY_Device_FriendlyName), byref(pv)) < 0:
                return ""
            try:
                if pv.vt == VT_LPWSTR and pv.u.pwszVal:
                    return str(pv.u.pwszVal)
                return ""
            finally:
                ole32.PropVariantClear(byref(pv))
        finally:
            _com_release(store_ptr)

    def list_active_endpoints(self, data_flow: int) -> list:
        ok, need_uninit = self._co_init()
        if not ok:
            return []
        ole32 = ctypes.windll.ole32
        enum_ptr = None
        coll_ptr = c_void_p()
        try:
            enum_ptr = self._create_enumerator()
            if not enum_ptr:
                return []
            enum_vt = ctypes.cast(
                ctypes.cast(enum_ptr, POINTER(c_void_p))[0], POINTER(c_void_p)
            )
            enum_endpoints = ctypes.WINFUNCTYPE(
                HRESULT, c_void_p, c_uint, DWORD, POINTER(c_void_p)
            )(enum_vt[3])
            if (
                enum_endpoints(enum_ptr, data_flow, DEVICE_STATE_ACTIVE, byref(coll_ptr)) < 0
                or not coll_ptr.value
            ):
                return []

            coll_vt = ctypes.cast(
                ctypes.cast(coll_ptr, POINTER(c_void_p))[0], POINTER(c_void_p)
            )
            get_count = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_uint))(coll_vt[3])
            get_item = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_uint, POINTER(c_void_p))(
                coll_vt[4]
            )

            count = c_uint()
            if get_count(coll_ptr, byref(count)) < 0:
                return []

            devices = []
            for idx in range(count.value):
                dev_ptr = c_void_p()
                if get_item(coll_ptr, idx, byref(dev_ptr)) < 0 or not dev_ptr.value:
                    continue
                try:
                    dev_id = self._get_device_id(dev_ptr)
                    name = self._get_device_friendly_name(dev_ptr)
                    if dev_id:
                        devices.append({"id": dev_id, "name": name or dev_id})
                finally:
                    _com_release(dev_ptr)
            return devices
        except Exception:
            return []
        finally:
            _com_release(coll_ptr)
            if enum_ptr:
                _com_release(enum_ptr)
            if need_uninit:
                ole32.CoUninitialize()

    def get_default_endpoint_roles(self, data_flow: int) -> dict:
        """Return {role_int: endpoint_id} for eConsole(0), eMultimedia(1), eCommunications(2)."""
        ok, need_uninit = self._co_init()
        if not ok:
            return {}
        ole32 = ctypes.windll.ole32
        enum_ptr = None
        try:
            enum_ptr = self._create_enumerator()
            if not enum_ptr:
                return {}
            enum_vt = ctypes.cast(
                ctypes.cast(enum_ptr, POINTER(c_void_p))[0], POINTER(c_void_p)
            )
            get_default = ctypes.WINFUNCTYPE(
                HRESULT, c_void_p, c_uint, c_uint, POINTER(c_void_p)
            )(enum_vt[4])

            result = {}
            for role in ROLES:
                dev_ptr = c_void_p()
                if get_default(enum_ptr, data_flow, role, byref(dev_ptr)) >= 0 and dev_ptr.value:
                    try:
                        dev_id = self._get_device_id(dev_ptr)
                        if dev_id:
                            result[role] = dev_id
                    finally:
                        _com_release(dev_ptr)
            return result
        except Exception:
            return {}
        finally:
            if enum_ptr:
                _com_release(enum_ptr)
            if need_uninit:
                ole32.CoUninitialize()

    def get_default_endpoint_id(self, data_flow: int, role: int = 0):
        ok, need_uninit = self._co_init()
        if not ok:
            return None
        ole32 = ctypes.windll.ole32
        enum_ptr = None
        dev_ptr = c_void_p()
        try:
            enum_ptr = self._create_enumerator()
            if not enum_ptr:
                return None
            enum_vt = ctypes.cast(
                ctypes.cast(enum_ptr, POINTER(c_void_p))[0], POINTER(c_void_p)
            )
            get_default = ctypes.WINFUNCTYPE(
                HRESULT, c_void_p, c_uint, c_uint, POINTER(c_void_p)
            )(enum_vt[4])
            if get_default(enum_ptr, data_flow, role, byref(dev_ptr)) < 0 or not dev_ptr.value:
                return None
            return self._get_device_id(dev_ptr)
        except Exception:
            return None
        finally:
            _com_release(dev_ptr)
            if enum_ptr:
                _com_release(enum_ptr)
            if need_uninit:
                ole32.CoUninitialize()

    def set_default_endpoint_roles(self, role_to_device_id: dict) -> bool:
        """Set default endpoint per role ({0: id0, 1: id1, 2: id2})."""
        if not role_to_device_id:
            return False
        ok, need_uninit = self._co_init()
        if not ok:
            return False
        ole32 = ctypes.windll.ole32
        pc_ptr = c_void_p()
        try:
            for iid in _POLICY_CONFIG_IIDS:
                hr = ole32.CoCreateInstance(
                    byref(_CLSID_PolicyConfigClient),
                    None,
                    CLSCTX_ALL,
                    byref(iid),
                    byref(pc_ptr),
                )
                if hr >= 0 and pc_ptr.value:
                    break
            if not pc_ptr.value:
                return False

            pc_vt = ctypes.cast(
                ctypes.cast(pc_ptr, POINTER(c_void_p))[0], POINTER(c_void_p)
            )
            # vtable[13] = SetDefaultEndpoint(PCWSTR wszDeviceId, ERole eRole)
            set_default = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, c_uint)(
                pc_vt[13]
            )

            all_ok = True
            for role, dev_id in role_to_device_id.items():
                if not dev_id:
                    continue
                hr = set_default(pc_ptr, str(dev_id), int(role))
                if hr < 0:
                    all_ok = False
            return all_ok
        except Exception:
            return False
        finally:
            _com_release(pc_ptr)
            if need_uninit:
                ole32.CoUninitialize()


class AudioDeviceSwitcher:
    """
    Automatically switches Windows default playback & microphone endpoints to virtual audio devices
    on startup, restores the user's previous devices on exit, and supports one-click re-switching.
    """

    def __init__(self, state_file: Path, backend=None):
        self._state_file = Path(state_file)
        self._backend = backend or WindowsAudioPolicyBackend()
        self._lock = threading.RLock()

        self._original_render_roles = {}
        self._original_render_name = ""
        self._original_capture_roles = {}
        self._original_capture_name = ""

        self._preferred_virtual_render_id = ""
        self._started = False
        self._restored = False

        self._cached_status = {
            "supported": sys.platform == "win32",
            "virtualAvailable": False,
            "isOnVirtual": False,
            "renderOnVirtual": False,
            "captureOnVirtual": False,
            "virtualRenderName": "",
            "virtualRenderId": "",
            "virtualCaptureName": "",
            "virtualCaptureId": "",
            "currentRenderName": "",
            "currentCaptureName": "",
            "originalRenderName": "",
            "originalCaptureName": "",
            "virtualRenderChoices": [],
        }
        self._load_persisted_preferences()

    def _load_persisted_file(self) -> dict:
        try:
            if self._state_file.exists():
                data = json.loads(self._state_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def _load_persisted_preferences(self):
        data = self._load_persisted_file()
        pref = data.get("preferredVirtualRenderId")
        if isinstance(pref, str):
            self._preferred_virtual_render_id = pref

    def _save_persisted_state(self, active_session: bool):
        payload = {
            "activeSession": bool(active_session),
            "preferredVirtualRenderId": self._preferred_virtual_render_id,
            "originalRenderRoles": {
                str(k): v for k, v in self._original_render_roles.items() if v
            },
            "originalRenderName": self._original_render_name,
            "originalCaptureRoles": {
                str(k): v for k, v in self._original_capture_roles.items() if v
            },
            "originalCaptureName": self._original_capture_name,
        }
        try:
            self._state_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    @staticmethod
    def _parse_roles_dict(raw) -> dict:
        if not isinstance(raw, dict):
            return {}
        out = {}
        for k, v in raw.items():
            try:
                role = int(k)
            except (TypeError, ValueError):
                continue
            if role in ROLES and isinstance(v, str) and v:
                out[role] = v
        return out

    def _pick_virtual_render(self, render_devices: list):
        virtuals = []
        for dev in render_devices:
            tier = classify_render_virtual_tier(dev["name"])
            if tier > 0:
                virtuals.append({**dev, "tier": tier})

        virtuals.sort(key=lambda d: (d["tier"], d["name"].lower()))
        if not virtuals:
            return None, []

        choices = [{"id": d["id"], "name": d["name"], "tier": d["tier"]} for d in virtuals]
        if self._preferred_virtual_render_id:
            for d in virtuals:
                if d["id"] == self._preferred_virtual_render_id:
                    return d, choices
        return virtuals[0], choices

    @staticmethod
    def _pick_virtual_capture(capture_devices: list):
        for dev in capture_devices:
            if is_virtual_capture_device(dev["name"]):
                return dev
        return None

    @staticmethod
    def _resolve_original_roles(
        current_roles: dict,
        persisted_roles: dict,
        devices: list,
        is_virtual_fn,
    ):
        """
        Determine the user's non-virtual ('original') device roles to restore on exit.
        Handles crash recovery if the previous process terminated while virtual audio was still active.
        """
        by_id = {d["id"]: d["name"] for d in devices}
        console_id = current_roles.get(0, "")
        console_name = by_id.get(console_id, "")

        # Normal startup: current default endpoint is a physical/non-virtual device
        if console_id and not is_virtual_fn(console_name):
            return dict(current_roles), console_name

        # Crash-recovery startup: current default is already virtual; try persisted original roles
        valid_persisted = {
            r: did for r, did in persisted_roles.items() if did in by_id and not is_virtual_fn(by_id[did])
        }
        if 0 in valid_persisted:
            filled = {r: valid_persisted.get(r, valid_persisted[0]) for r in ROLES}
            return filled, by_id.get(filled[0], "")

        # Final fallback: first active physical/non-virtual endpoint
        for dev in devices:
            if not is_virtual_fn(dev["name"]):
                fallback_roles = {r: dev["id"] for r in ROLES}
                return fallback_roles, dev["name"]

        return {}, ""

    def switch_on_startup(self) -> dict:
        """Record previous non-virtual endpoints and switch Windows defaults to virtual endpoints."""
        with self._lock:
            self._restored = False
            render_devs = self._backend.list_active_endpoints(E_RENDER)
            capture_devs = self._backend.list_active_endpoints(E_CAPTURE)
            cur_render_roles = self._backend.get_default_endpoint_roles(E_RENDER)
            cur_capture_roles = self._backend.get_default_endpoint_roles(E_CAPTURE)

            persisted = self._load_persisted_file()
            persisted_render = self._parse_roles_dict(persisted.get("originalRenderRoles"))
            persisted_capture = self._parse_roles_dict(persisted.get("originalCaptureRoles"))

            self._original_render_roles, self._original_render_name = self._resolve_original_roles(
                cur_render_roles,
                persisted_render,
                render_devs,
                lambda name: classify_render_virtual_tier(name) > 0,
            )
            self._original_capture_roles, self._original_capture_name = self._resolve_original_roles(
                cur_capture_roles,
                persisted_capture,
                capture_devs,
                is_virtual_capture_device,
            )

            self._started = True
            self._save_persisted_state(active_session=True)

            target_render, _ = self._pick_virtual_render(render_devs)
            target_capture = self._pick_virtual_capture(capture_devs)

            if target_render:
                ok = self._backend.set_default_endpoint_roles(
                    {r: target_render["id"] for r in ROLES}
                )
                if ok:
                    print(
                        f"✓ Auto-switched Windows playback to virtual device: {target_render['name']}"
                        + (
                            f" (will restore: {self._original_render_name})"
                            if self._original_render_name
                            else ""
                        )
                    )
            else:
                print("! No virtual playback device found for auto-switch.")

            if target_capture:
                ok = self._backend.set_default_endpoint_roles(
                    {r: target_capture["id"] for r in ROLES}
                )
                if ok:
                    print(
                        f"✓ Auto-switched Windows microphone to virtual device: {target_capture['name']}"
                        + (
                            f" (will restore: {self._original_capture_name})"
                            if self._original_capture_name
                            else ""
                        )
                    )

            return self.refresh_status()

    def switch_to_virtual(self, preferred_render_id: str = None) -> dict:
        """One-click switch (or re-switch) Windows playback & microphone to virtual devices."""
        with self._lock:
            if preferred_render_id is not None:
                self._preferred_virtual_render_id = preferred_render_id

            render_devs = self._backend.list_active_endpoints(E_RENDER)
            capture_devs = self._backend.list_active_endpoints(E_CAPTURE)

            # If user had no recorded original device yet and is currently on a physical device, capture it
            if not self._original_render_roles:
                cur_render = self._backend.get_default_endpoint_roles(E_RENDER)
                self._original_render_roles, self._original_render_name = self._resolve_original_roles(
                    cur_render,
                    {},
                    render_devs,
                    lambda name: classify_render_virtual_tier(name) > 0,
                )
            if not self._original_capture_roles:
                cur_capture = self._backend.get_default_endpoint_roles(E_CAPTURE)
                self._original_capture_roles, self._original_capture_name = self._resolve_original_roles(
                    cur_capture,
                    {},
                    capture_devs,
                    is_virtual_capture_device,
                )

            target_render, _ = self._pick_virtual_render(render_devs)
            target_capture = self._pick_virtual_capture(capture_devs)

            if target_render:
                self._backend.set_default_endpoint_roles(
                    {r: target_render["id"] for r in ROLES}
                )
            if target_capture:
                self._backend.set_default_endpoint_roles(
                    {r: target_capture["id"] for r in ROLES}
                )

            self._save_persisted_state(active_session=True)
            return self.refresh_status()

    def restore_previous(self, mark_session_ended: bool = False) -> dict:
        """Switch Windows playback & microphone back to the user's previous non-virtual devices."""
        with self._lock:
            if mark_session_ended and self._restored:
                return dict(self._cached_status)

            render_devs = {d["id"]: d["name"] for d in self._backend.list_active_endpoints(E_RENDER)}
            capture_devs = {
                d["id"]: d["name"] for d in self._backend.list_active_endpoints(E_CAPTURE)
            }

            valid_render = {
                r: did for r, did in self._original_render_roles.items() if did in render_devs
            }
            if valid_render:
                ok = self._backend.set_default_endpoint_roles(valid_render)
                if ok and mark_session_ended:
                    print(
                        f"✓ Restored Windows playback device: "
                        f"{render_devs.get(valid_render.get(0, ''), self._original_render_name)}"
                    )

            valid_capture = {
                r: did for r, did in self._original_capture_roles.items() if did in capture_devs
            }
            if valid_capture:
                ok = self._backend.set_default_endpoint_roles(valid_capture)
                if ok and mark_session_ended:
                    print(
                        f"✓ Restored Windows microphone device: "
                        f"{capture_devs.get(valid_capture.get(0, ''), self._original_capture_name)}"
                    )

            if mark_session_ended:
                self._restored = True
                self._save_persisted_state(active_session=False)
                return dict(self._cached_status)

            return self.refresh_status()

    def restore_on_exit(self):
        with self._lock:
            if not self._started or self._restored:
                return
            self.restore_previous(mark_session_ended=True)

    def get_current_default_ids(self) -> tuple:
        """Fast lightweight poll returning (default_render_id, default_capture_id)."""
        return (
            self._backend.get_default_endpoint_id(E_RENDER, 0),
            self._backend.get_default_endpoint_id(E_CAPTURE, 0),
        )

    def refresh_status(self) -> dict:
        with self._lock:
            render_devs = self._backend.list_active_endpoints(E_RENDER)
            capture_devs = self._backend.list_active_endpoints(E_CAPTURE)
            render_by_id = {d["id"]: d["name"] for d in render_devs}
            capture_by_id = {d["id"]: d["name"] for d in capture_devs}

            cur_render_id = self._backend.get_default_endpoint_id(E_RENDER, 0) or ""
            cur_capture_id = self._backend.get_default_endpoint_id(E_CAPTURE, 0) or ""

            target_render, choices = self._pick_virtual_render(render_devs)
            target_capture = self._pick_virtual_capture(capture_devs)

            render_on_virtual = bool(target_render and cur_render_id == target_render["id"])
            capture_on_virtual = (
                bool(cur_capture_id == target_capture["id"]) if target_capture else True
            )
            virtual_available = bool(target_render or target_capture)
            is_on_virtual = bool(
                virtual_available
                and (render_on_virtual if target_render else True)
                and (capture_on_virtual if target_capture else True)
            )

            orig_render_id = self._original_render_roles.get(0, "")
            orig_capture_id = self._original_capture_roles.get(0, "")

            self._cached_status = {
                "supported": sys.platform == "win32",
                "virtualAvailable": virtual_available,
                "isOnVirtual": is_on_virtual,
                "renderOnVirtual": render_on_virtual,
                "captureOnVirtual": capture_on_virtual,
                "virtualRenderName": target_render["name"] if target_render else "",
                "virtualRenderId": target_render["id"] if target_render else "",
                "virtualCaptureName": target_capture["name"] if target_capture else "",
                "virtualCaptureId": target_capture["id"] if target_capture else "",
                "currentRenderName": render_by_id.get(cur_render_id, ""),
                "currentCaptureName": capture_by_id.get(cur_capture_id, ""),
                "originalRenderName": render_by_id.get(
                    orig_render_id, self._original_render_name
                ),
                "originalCaptureName": capture_by_id.get(
                    orig_capture_id, self._original_capture_name
                ),
                "virtualRenderChoices": choices,
            }
            return dict(self._cached_status)

    def get_cached_status(self) -> dict:
        with self._lock:
            return dict(self._cached_status)

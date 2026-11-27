import asyncio
import concurrent.futures
import ctypes
import sys
import threading
from ctypes import wintypes


VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1

# GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING == 4
_GSMTC_STATUS_PLAYING = 4


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("ki", _KEYBDINPUT),
        ("mi", _MOUSEINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", _INPUT_UNION),
    ]


def send_virtual_media_key(vk_code: int) -> bool:
    """Send a hardware-equivalent Windows multimedia key press + release via SendInput."""
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        inputs = (_INPUT * 2)()
        inputs[0].type = INPUT_KEYBOARD
        inputs[0].union.ki = _KEYBDINPUT(
            wVk=vk_code,
            wScan=0,
            dwFlags=KEYEVENTF_EXTENDEDKEY,
            time=0,
            dwExtraInfo=0,
        )
        inputs[1].type = INPUT_KEYBOARD
        inputs[1].union.ki = _KEYBDINPUT(
            wVk=vk_code,
            wScan=0,
            dwFlags=KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP,
            time=0,
            dwExtraInfo=0,
        )
        sent = user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(_INPUT))
        if sent == 2:
            return True
        user32.keybd_event(vk_code, 0, KEYEVENTF_EXTENDEDKEY, 0)
        user32.keybd_event(vk_code, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
        return True
    except Exception:
        return False


def _clean_app_name(source_id: str) -> str:
    if not source_id:
        return ""
    base = source_id.split("!")[0]
    if base.lower().endswith(".exe"):
        base = base[:-4]
    parts = base.replace("_", ".").split(".")
    meaningful = [
        p
        for p in parts
        if p and not (len(p) >= 10 and p.isalnum() and any(c.isdigit() for c in p))
    ]
    return meaningful[-1] if meaningful else base


def _init_mta_worker():
    if sys.platform == "win32":
        try:
            # COINIT_MULTITHREADED = 0 so WinRT IAsyncOperation.get() never deadlocks with PyAudio/Tkinter STA
            ctypes.windll.ole32.CoInitializeEx(None, 0)
        except Exception:
            pass


class WindowsMediaController:
    """
    Bridge between iOS MPRemoteCommandCenter / MPNowPlayingInfoCenter and Windows GSMTC
    (GlobalSystemMediaTransportControlsSessionManager) with Win32 VK_MEDIA_* fallback.
    Runs WinRT calls on a dedicated MTA worker thread so PyAudio/Tkinter STA threads never block.
    """

    VALID_ACTIONS = {"play", "pause", "toggle", "next", "prev", "stop"}

    def __init__(self, key_sender=send_virtual_media_key):
        self._lock = threading.Lock()
        self._key_sender = key_sender
        self._manager = None
        self._fallback_playing = True
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="gsmtc-mta",
            initializer=_init_mta_worker,
        )
        self._snapshot = {
            "hasSession": False,
            "playing": True,
            "title": "",
            "artist": "",
            "album": "",
            "app": "",
        }

    def get_cached_snapshot(self) -> dict:
        with self._lock:
            return dict(self._snapshot)

    def _get_manager_sync(self):
        if sys.platform != "win32":
            return None
        if self._manager is not None:
            return self._manager
        try:
            from winrt.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager,
            )

            self._manager = (
                GlobalSystemMediaTransportControlsSessionManager.request_async().get()
            )
            return self._manager
        except Exception:
            self._manager = None
            return None

    async def _get_manager(self):
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(self._executor, self._get_manager_sync),
                timeout=1.5,
            )
        except Exception:
            return None

    def _pick_session(self, manager):
        if manager is None:
            return None
        try:
            current = manager.get_current_session()
            if current is not None:
                return current
        except Exception:
            pass
        try:
            sessions = list(manager.get_sessions() or [])
            for sess in sessions:
                try:
                    info = sess.get_playback_info()
                    if info and int(info.playback_status) == _GSMTC_STATUS_PLAYING:
                        return sess
                except Exception:
                    continue
            return sessions[0] if sessions else None
        except Exception:
            return None

    def _read_session_snapshot_sync(self, session) -> dict:
        playing = False
        title = ""
        artist = ""
        album = ""
        app_name = ""

        try:
            info = session.get_playback_info()
            if info is not None:
                playing = int(info.playback_status) == _GSMTC_STATUS_PLAYING
        except Exception:
            with self._lock:
                playing = self._fallback_playing

        try:
            app_name = _clean_app_name(str(session.source_app_user_model_id or ""))
        except Exception:
            app_name = ""

        try:
            props = session.try_get_media_properties_async().get()
            if props is not None:
                title = str(props.title or "").strip()
                artist = str(props.artist or "").strip()
                album = str(props.album_title or "").strip()
        except Exception:
            pass

        return {
            "hasSession": True,
            "playing": playing,
            "title": title,
            "artist": artist,
            "album": album,
            "app": app_name,
        }

    async def poll_now_playing(self) -> tuple[dict, bool]:
        """
        Query Windows GSMTC for active media metadata & playback status.
        Returns (snapshot_dict, changed_bool).
        """
        manager = await self._get_manager()
        session = self._pick_session(manager)

        if session is None:
            with self._lock:
                next_snap = {
                    "hasSession": False,
                    "playing": self._fallback_playing,
                    "title": "",
                    "artist": "",
                    "album": "",
                    "app": "",
                }
                changed = next_snap != self._snapshot
                self._snapshot = next_snap
                return dict(next_snap), changed

        loop = asyncio.get_running_loop()
        try:
            next_snap = await asyncio.wait_for(
                loop.run_in_executor(
                    self._executor, self._read_session_snapshot_sync, session
                ),
                timeout=1.5,
            )
        except Exception:
            return self.get_cached_snapshot(), False

        with self._lock:
            self._fallback_playing = next_snap["playing"]
            changed = next_snap != self._snapshot
            self._snapshot = next_snap
            return dict(next_snap), changed

    @staticmethod
    def _invoke_session_action_sync(session, normalized: str) -> bool:
        if normalized == "play":
            return bool(session.try_play_async().get())
        if normalized == "pause":
            return bool(session.try_pause_async().get())
        if normalized == "toggle":
            return bool(session.try_toggle_play_pause_async().get())
        if normalized == "next":
            return bool(session.try_skip_next_async().get())
        if normalized == "prev":
            return bool(session.try_skip_previous_async().get())
        if normalized == "stop":
            return bool(session.try_stop_async().get())
        return False

    async def execute_command(self, action: str) -> dict:
        """
        Execute a remote media transport action ('play', 'pause', 'toggle', 'next', 'prev', 'stop').
        Prefers idempotent GSMTC session calls, falling back to Win32 multimedia keys.
        """
        normalized = str(action or "").strip().lower()
        if normalized not in self.VALID_ACTIONS:
            return {"ok": False, "error": f"Unsupported action: {normalized}"}

        manager = await self._get_manager()
        session = self._pick_session(manager)
        handled_by_gsmtc = False

        if session is not None:
            loop = asyncio.get_running_loop()
            try:
                handled_by_gsmtc = await asyncio.wait_for(
                    loop.run_in_executor(
                        self._executor,
                        self._invoke_session_action_sync,
                        session,
                        normalized,
                    ),
                    timeout=1.5,
                )
            except Exception:
                handled_by_gsmtc = False

        used_fallback_key = False
        if not handled_by_gsmtc:
            if normalized in ("play", "pause", "toggle"):
                with self._lock:
                    if normalized == "play":
                        should_press = not self._fallback_playing or session is None
                        self._fallback_playing = True
                    elif normalized == "pause":
                        should_press = self._fallback_playing or session is None
                        self._fallback_playing = False
                    else:
                        should_press = True
                        self._fallback_playing = not self._fallback_playing
                if should_press:
                    used_fallback_key = await asyncio.to_thread(
                        self._key_sender, VK_MEDIA_PLAY_PAUSE
                    )
            elif normalized == "next":
                used_fallback_key = await asyncio.to_thread(
                    self._key_sender, VK_MEDIA_NEXT_TRACK
                )
            elif normalized == "prev":
                used_fallback_key = await asyncio.to_thread(
                    self._key_sender, VK_MEDIA_PREV_TRACK
                )
            elif normalized == "stop":
                with self._lock:
                    self._fallback_playing = False
                used_fallback_key = await asyncio.to_thread(
                    self._key_sender, VK_MEDIA_STOP
                )

        await asyncio.sleep(0.08)
        snapshot, _ = await self.poll_now_playing()

        if normalized == "play" and not snapshot["playing"]:
            snapshot["playing"] = True
            with self._lock:
                self._snapshot["playing"] = True
        elif normalized == "pause" and snapshot["playing"]:
            snapshot["playing"] = False
            with self._lock:
                self._snapshot["playing"] = False

        return {
            "ok": bool(handled_by_gsmtc or used_fallback_key),
            "method": "gsmtc" if handled_by_gsmtc else "vk_media",
            "nowPlaying": snapshot,
        }

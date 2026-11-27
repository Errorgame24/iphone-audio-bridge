import asyncio
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
        # Fallback to keybd_event if SendInput was blocked
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
    # Filter hex/publisher hashes when possible
    meaningful = [p for p in parts if p and not (len(p) >= 10 and p.isalnum() and any(c.isdigit() for c in p))]
    return meaningful[-1] if meaningful else base


class WindowsMediaController:
    """
    Bridge between iOS MPRemoteCommandCenter / MPNowPlayingInfoCenter and Windows GSMTC
    (GlobalSystemMediaTransportControlsSessionManager) with Win32 VK_MEDIA_* fallback.
    """

    VALID_ACTIONS = {"play", "pause", "toggle", "next", "prev", "stop"}

    def __init__(self, key_sender=send_virtual_media_key):
        self._lock = threading.Lock()
        self._key_sender = key_sender
        self._manager = None
        self._fallback_playing = True
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

    async def _get_manager(self):
        if sys.platform != "win32":
            return None
        if self._manager is not None:
            return self._manager
        try:
            from winrt.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager,
            )

            self._manager = await asyncio.wait_for(
                GlobalSystemMediaTransportControlsSessionManager.request_async(),
                timeout=2.0,
            )
            return self._manager
        except Exception:
            self._manager = None
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
            playing = self._fallback_playing

        try:
            app_name = _clean_app_name(str(session.source_app_user_model_id or ""))
        except Exception:
            app_name = ""

        try:
            props = await asyncio.wait_for(
                session.try_get_media_properties_async(),
                timeout=1.2,
            )
            if props is not None:
                title = str(props.title or "").strip()
                artist = str(props.artist or "").strip()
                album = str(props.album_title or "").strip()
        except Exception:
            pass

        with self._lock:
            self._fallback_playing = playing
            next_snap = {
                "hasSession": True,
                "playing": playing,
                "title": title,
                "artist": artist,
                "album": album,
                "app": app_name,
            }
            changed = next_snap != self._snapshot
            self._snapshot = next_snap
            return dict(next_snap), changed

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
            try:
                if normalized == "play":
                    handled_by_gsmtc = bool(
                        await asyncio.wait_for(session.try_play_async(), timeout=1.5)
                    )
                elif normalized == "pause":
                    handled_by_gsmtc = bool(
                        await asyncio.wait_for(session.try_pause_async(), timeout=1.5)
                    )
                elif normalized == "toggle":
                    handled_by_gsmtc = bool(
                        await asyncio.wait_for(
                            session.try_toggle_play_pause_async(), timeout=1.5
                        )
                    )
                elif normalized == "next":
                    handled_by_gsmtc = bool(
                        await asyncio.wait_for(
                            session.try_skip_next_async(), timeout=1.5
                        )
                    )
                elif normalized == "prev":
                    handled_by_gsmtc = bool(
                        await asyncio.wait_for(
                            session.try_skip_previous_async(), timeout=1.5
                        )
                    )
                elif normalized == "stop":
                    handled_by_gsmtc = bool(
                        await asyncio.wait_for(session.try_stop_async(), timeout=1.5)
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

        # Give target media player a brief moment to update GSMTC playback status
        await asyncio.sleep(0.08)
        snapshot, _ = await self.poll_now_playing()

        # Optimistically reflect explicit play/pause if GSMTC status lags slightly
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

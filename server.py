import asyncio
import atexit
import ctypes
import datetime
import ipaddress
import json
import queue
import signal
import socket
import ssl
import subprocess
import sys
import threading
from ctypes import POINTER, HRESULT, byref, c_uint, c_void_p, c_wchar_p
from ctypes.wintypes import DWORD
from pathlib import Path

import pyaudiowpatch as pyaudio
from aiohttp import web

from audio_switcher import AudioDeviceSwitcher
from autostart import AutoStartManager, resolve_windowless_python
from battery_monitor import BatteryMonitor
from lan_discovery import LanDiscoveryAdvertiser
from media_control import WindowsMediaController


HOST = "0.0.0.0"
PORT = 8443  # HTTPS is required for microphone access on iOS

CHANNELS = 2
CHUNK = 960
DEVICE_POLL_INTERVAL = 1.2
SILENCE_KEEPALIVE_INTERVAL = 0.20
SILENCE_KEEPALIVE_FRAMES = 240
SERVER_HOSTNAME = socket.gethostname() or "Windows-PC"

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
_LOG_FILE_HANDLE = None


def _ensure_windowless_stdio():
    """Redirect None stdout/stderr under pythonw.exe to a local UTF-8 log file."""
    global _LOG_FILE_HANDLE
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass

    if sys.stdout is None or sys.stderr is None:
        try:
            _LOG_FILE_HANDLE = open(
                BASE_DIR / ".audio_bridge.log",
                mode="w",
                encoding="utf-8",
                buffering=1,
                errors="replace",
            )
            if sys.stdout is None:
                sys.stdout = _LOG_FILE_HANDLE
            if sys.stderr is None:
                sys.stderr = _LOG_FILE_HANDLE
        except Exception:
            pass


_ensure_windowless_stdio()

clients = set()
client_audio_info = {}
client_mic_info = {}
client_info_lock = threading.Lock()
active_mic_ws = None
audio_queue = asyncio.Queue(maxsize=5)

loop = None
async_stop_event = None
pa = None
audio_stream = None
mic_stream = None
active_render_id = None
ssl_context_ref = None
bound_lan_ip = "127.0.0.1"
stream_lock = threading.Lock()
shutdown_event = threading.Event()

stream_rate = 48000
stream_channels = 2
stream_device_name = ""
server_url = f"https://127.0.0.1:{PORT}"

# iPhone microphone -> virtual cable on the PC.
mic_queue = queue.Queue(maxsize=10)
mic_info = {"available": False, "rate": 48000, "channels": 2, "deviceName": ""}

battery_monitor = BatteryMonitor(state_file=BASE_DIR / ".airpods_binding.json")
audio_switcher = AudioDeviceSwitcher(BASE_DIR / ".audio_switcher_state.json")
autostart_manager = AutoStartManager(script_path=BASE_DIR / "server.py")
media_controller = WindowsMediaController()
lan_advertiser = LanDiscoveryAdvertiser(port=PORT, hostname=SERVER_HOSTNAME)
_console_ctrl_handler_ref = None


def _install_console_exit_handler():
    global _console_ctrl_handler_ref
    if sys.platform != "win32" or _console_ctrl_handler_ref is not None:
        return
    try:
        handler_type = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, DWORD)

        def _handler(ctrl_type):
            # 2=CTRL_CLOSE_EVENT, 5=CTRL_LOGOFF_EVENT, 6=CTRL_SHUTDOWN_EVENT
            if ctrl_type in (2, 5, 6):
                try:
                    shutdown_audio_devices()
                except Exception:
                    pass
                try:
                    audio_switcher.restore_on_exit()
                except Exception:
                    pass
            return False

        _console_ctrl_handler_ref = handler_type(_handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_ctrl_handler_ref, True)
    except Exception:
        pass


def _validated_sample_rate(value):
    try:
        rate = int(value)
    except (TypeError, ValueError):
        return None
    return rate if 8000 <= rate <= 384000 else None


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", DWORD),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, l, w1, w2, *b):
        super().__init__(l, w1, w2, (ctypes.c_ubyte * 8)(*b))


_CLSID_MMDeviceEnumerator = _GUID(
    0xBCDE0395, 0xE52F, 0x467C, 0x8E, 0x3D, 0xC4, 0x57, 0x92, 0x91, 0x69, 0x2E
)
_IID_IMMDeviceEnumerator = _GUID(
    0xA95664D2, 0x9614, 0x4F35, 0xA7, 0x46, 0xDE, 0x8D, 0xB6, 0x36, 0x17, 0xE6
)


def get_windows_default_render_id():
    """Query the current Windows default playback endpoint ID via IMMDeviceEnumerator."""
    if sys.platform != "win32":
        return None

    ole32 = ctypes.windll.ole32
    hr = ole32.CoInitializeEx(None, 0)
    # 0 = S_OK, 1 = S_FALSE, -2147417850 (0x80010106) = RPC_E_CHANGED_MODE (already initialized)
    need_uninit = hr in (0, 1)

    enum_ptr = c_void_p()
    dev_ptr = c_void_p()
    dev_id = c_wchar_p()

    try:
        if (
            ole32.CoCreateInstance(
                byref(_CLSID_MMDeviceEnumerator),
                None,
                23,  # CLSCTX_ALL
                byref(_IID_IMMDeviceEnumerator),
                byref(enum_ptr),
            )
            < 0
            or not enum_ptr.value
        ):
            return None

        enum_vt = ctypes.cast(
            ctypes.cast(enum_ptr, POINTER(c_void_p))[0], POINTER(c_void_p)
        )
        get_default_endpoint = ctypes.WINFUNCTYPE(
            HRESULT, c_void_p, c_uint, c_uint, POINTER(c_void_p)
        )(enum_vt[4])

        # eRender = 0, eConsole = 0
        if get_default_endpoint(enum_ptr, 0, 0, byref(dev_ptr)) < 0 or not dev_ptr.value:
            return None

        dev_vt = ctypes.cast(
            ctypes.cast(dev_ptr, POINTER(c_void_p))[0], POINTER(c_void_p)
        )
        get_id = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_wchar_p))(dev_vt[5])

        if get_id(dev_ptr, byref(dev_id)) < 0 or not dev_id.value:
            return None

        return dev_id.value
    except Exception:
        return None
    finally:
        if dev_id.value:
            ole32.CoTaskMemFree(dev_id)
        if dev_ptr.value:
            dev_vt = ctypes.cast(
                ctypes.cast(dev_ptr, POINTER(c_void_p))[0], POINTER(c_void_p)
            )
            ctypes.WINFUNCTYPE(c_uint, c_void_p)(dev_vt[2])(dev_ptr)
        if enum_ptr.value:
            enum_vt = ctypes.cast(
                ctypes.cast(enum_ptr, POINTER(c_void_p))[0], POINTER(c_void_p)
            )
            ctypes.WINFUNCTYPE(c_uint, c_void_p)(enum_vt[2])(enum_ptr)
        if need_uninit:
            ole32.CoUninitialize()


def get_local_ip():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return "127.0.0.1"


def ensure_cert(ip):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    cert_path = BASE_DIR / "cert.pem"
    key_path = BASE_DIR / "key.pem"

    now = datetime.datetime.now(datetime.timezone.utc)
    target_ip = ipaddress.ip_address(ip)
    loopback_ip = ipaddress.ip_address("127.0.0.1")

    preserved_ips = []
    key = None

    if key_path.exists():
        try:
            key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        except Exception:
            key = None

    if cert_path.exists() and key is not None:
        try:
            existing_cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            san = existing_cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value
            existing_ips = san.get_values_for_type(x509.IPAddress)
            existing_dns = san.get_values_for_type(x509.DNSName)
            total_days = (
                existing_cert.not_valid_after_utc - existing_cert.not_valid_before_utc
            ).days

            valid_time = (
                existing_cert.not_valid_before_utc
                <= now
                <= existing_cert.not_valid_after_utc - datetime.timedelta(days=7)
                and total_days <= 398
            )

            if (
                valid_time
                and target_ip in existing_ips
                and loopback_ip in existing_ips
                and "localhost" in existing_dns
            ):
                return cert_path, key_path

            preserved_ips = [addr for addr in existing_ips if addr != loopback_ip][-8:]
            if not valid_time:
                print("! Existing certificate is expired or exceeds Apple 398-day limit, regenerating...")
            else:
                print(f"! Updating certificate SAN for {ip} / 127.0.0.1...")
        except Exception:
            print("! Existing certificate could not be validated, regenerating...")

    if key is None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    all_ips = []
    for addr in [*preserved_ips, target_ip, loopback_ip]:
        if addr not in all_ips:
            all_ips.append(addr)

    san_entries = [x509.IPAddress(addr) for addr in all_ips]
    san_entries.append(x509.DNSName("localhost"))

    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "audio-bridge")])

    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=396))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .sign(key, hashes.SHA256())
    )

    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    print("✓ Certificate ready:", cert_path)
    return cert_path, key_path


def _clean_device_name(name):
    return name.replace(" [Loopback]", "").strip()


def _is_virtual_cable(name):
    lowered = name.lower()
    return "cable input" in lowered or "cable in " in lowered


def find_loopback_device(pa_instance, verbose=True):
    candidates = list(pa_instance.get_loopback_device_info_generator())
    if not candidates:
        raise RuntimeError("No WASAPI loopback devices found.")

    if verbose:
        print("Available WASAPI loopback devices:")
        for dev in candidates:
            print(f"  [{dev['index']}] {dev['name']}")
        print()

    # Primary: query WASAPI's native default output loopback directly
    try:
        default_loopback = pa_instance.get_default_wasapi_loopback()
        if _is_virtual_cable(default_loopback["name"]) and verbose:
            print("! Note: Windows default output is VB-Cable; capturing its loopback.")
        return default_loopback
    except (OSError, LookupError):
        pass

    non_cable = [d for d in candidates if not _is_virtual_cable(d["name"])]
    pool = non_cable if non_cable else candidates

    # Fallback: match against default output device name
    try:
        output_name = pa_instance.get_default_output_device_info()["name"].lower()
        for dev in pool:
            if output_name in dev["name"].lower():
                return dev

        output_parts = output_name.split()
        for dev in pool:
            dev_name = dev["name"].lower()
            if any(part in dev_name for part in output_parts if len(part) >= 2):
                return dev
    except Exception:
        pass

    return pool[0]


def find_cable_input(pa_instance):
    try:
        wasapi = pa_instance.get_host_api_info_by_type(pyaudio.paWASAPI)["index"]
    except OSError:
        return None

    for i in range(pa_instance.get_device_count()):
        info = pa_instance.get_device_info_by_index(i)
        if (
            info["hostApi"] == wasapi
            and info["maxOutputChannels"] > 0
            and "cable input" in info["name"].lower()
        ):
            return info

    return None


def audio_callback(in_data, frame_count, time_info, status, _pa_continue=pyaudio.paContinue):
    if loop is not None and shutdown_event is not None and not shutdown_event.is_set():
        try:
            loop.call_soon_threadsafe(put_audio, in_data)
        except Exception:
            pass

    return (None, _pa_continue)


def put_audio(data):
    try:
        audio_queue.put_nowait(data)
    except asyncio.QueueFull:
        try:
            audio_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            audio_queue.put_nowait(data)
        except asyncio.QueueFull:
            pass


def _close_streams_unlocked():
    global audio_stream, mic_stream, pa

    if audio_stream is not None:
        try:
            audio_stream.stop_stream()
            audio_stream.close()
        except Exception:
            pass
        audio_stream = None

    if mic_stream is not None:
        try:
            mic_stream.stop_stream()
            mic_stream.close()
        except Exception:
            pass
        mic_stream = None

    if pa is not None:
        try:
            pa.terminate()
        except Exception:
            pass
        pa = None


def setup_audio_devices(verbose=True):
    """Initialize or re-initialize WASAPI loopback capture and VB-Cable mic output."""
    global pa, audio_stream, mic_stream, active_render_id, stream_rate, stream_channels, stream_device_name

    with stream_lock:
        _close_streams_unlocked()

        active_render_id = get_windows_default_render_id()
        pa = pyaudio.PyAudio()

        # 1. System audio loopback capture
        device = find_loopback_device(pa, verbose=verbose)
        channels = min(CHANNELS, int(device["maxInputChannels"]))
        if channels < 1:
            raise RuntimeError("Loopback device has no input channels.")

        sample_rate = int(device.get("defaultSampleRate", 48000))
        stream_rate = sample_rate
        stream_channels = channels
        stream_device_name = _clean_device_name(device["name"])

        audio_stream = pa.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=sample_rate,
            input=True,
            input_device_index=device["index"],
            frames_per_buffer=CHUNK,
            stream_callback=audio_callback,
        )
        audio_stream.start_stream()

        print(f"✓ Capturing audio from: {stream_device_name} ({sample_rate} Hz, {channels} ch)")

        # 2. VB-Cable virtual microphone output
        cable_info = find_cable_input(pa)
        if cable_info is None:
            mic_info.update(available=False, deviceName="")
            if verbose:
                print("! VB-Cable not found - microphone disabled.")
                print("  Install VB-Cable (vb-audio.com/Cable) and reboot.")
        else:
            mic_ch = min(2, int(cable_info["maxOutputChannels"]))
            mic_rate = int(cable_info["defaultSampleRate"])
            mic_stream = pa.open(
                format=pyaudio.paInt16,
                channels=mic_ch,
                rate=mic_rate,
                output=True,
                output_device_index=cable_info["index"],
            )
            mic_info.update(
                available=True,
                rate=mic_rate,
                channels=mic_ch,
                deviceName=cable_info["name"],
            )
            if verbose:
                print(f"✓ Microphone routed to: {cable_info['name']} ({mic_rate} Hz)")

        if verbose:
            print()


def shutdown_audio_devices():
    shutdown_event.set()
    try:
        mic_queue.put_nowait(None)
    except queue.Full:
        pass

    with stream_lock:
        _close_streams_unlocked()


# atexit runs in LIFO order: shutdown_audio_devices runs first, then restore_on_exit
atexit.register(audio_switcher.restore_on_exit)
atexit.register(shutdown_audio_devices)


def mic_writer():
    while not shutdown_event.is_set():
        data = mic_queue.get()
        if data is None:
            break

        with stream_lock:
            if mic_stream is not None:
                try:
                    mic_stream.write(data)
                except Exception as e:
                    print("Microphone output error:", e)


def build_stream_config_message():
    return {
        "type": "config",
        "sampleRate": stream_rate,
        "channels": stream_channels,
        "deviceName": stream_device_name,
        "hostname": SERVER_HOSTNAME,
        "nowPlaying": media_controller.get_cached_snapshot(),
    }


def build_now_playing_message(snapshot=None):
    snap = snapshot if snapshot is not None else media_controller.get_cached_snapshot()
    return {
        "type": "now-playing",
        "hostname": SERVER_HOSTNAME,
        "deviceName": stream_device_name,
        **snap,
    }


async def notify_clients_config():
    if not clients:
        return

    payload = build_stream_config_message()
    snapshot = list(clients)
    results = await asyncio.gather(
        *(client.send_json(payload) for client in snapshot),
        return_exceptions=True,
    )
    for client, res in zip(snapshot, results):
        if isinstance(res, Exception):
            clients.discard(client)


async def notify_clients_now_playing(snapshot=None):
    if not clients:
        return

    payload = build_now_playing_message(snapshot)
    targets = list(clients)
    results = await asyncio.gather(
        *(client.send_json(payload) for client in targets),
        return_exceptions=True,
    )
    for client, res in zip(targets, results):
        if isinstance(res, Exception):
            clients.discard(client)


async def media_monitor_loop():
    """Poll Windows GSMTC Now Playing info and push live updates to connected iPhones."""
    while not shutdown_event.is_set():
        try:
            snap, changed = await media_controller.poll_now_playing()
            if changed and clients:
                await notify_clients_now_playing(snap)
        except Exception:
            pass
        await asyncio.sleep(1.2)


def _sync_loopback_after_endpoint_switch():
    """Immediately re-bind WASAPI loopback if Windows default playback device changed."""
    if shutdown_event.is_set():
        return
    current_id = get_windows_default_render_id()
    if not current_id or current_id == active_render_id:
        return
    try:
        setup_audio_devices(verbose=False)
        if loop is not None:
            asyncio.run_coroutine_threadsafe(notify_clients_config(), loop)
    except Exception as e:
        print("Failed to refresh loopback after audio switch:", e)


def trigger_switch_to_virtual(preferred_render_id=None):
    def _worker():
        audio_switcher.switch_to_virtual(preferred_render_id=preferred_render_id)
        _sync_loopback_after_endpoint_switch()

    threading.Thread(target=_worker, daemon=True).start()


def trigger_restore_previous_audio():
    def _worker():
        audio_switcher.restore_previous(mark_session_ended=False)
        _sync_loopback_after_endpoint_switch()

    threading.Thread(target=_worker, daemon=True).start()


async def device_monitor_loop():
    """Watch Windows default audio devices and LAN IP changes (e.g. Wi-Fi connecting after boot)."""
    global bound_lan_ip, server_url
    _, last_capture_id = await asyncio.to_thread(audio_switcher.get_current_default_ids)
    ip_tick = 0

    while not shutdown_event.is_set():
        await asyncio.sleep(DEVICE_POLL_INTERVAL)

        ip_tick += 1
        if ip_tick >= 3:
            ip_tick = 0
            latest_ip = await asyncio.to_thread(get_local_ip)
            if latest_ip and latest_ip != bound_lan_ip:
                bound_lan_ip = latest_ip
                server_url = f"https://{latest_ip}:{PORT}"
                print(f"[~] LAN IP updated to {latest_ip} -> {server_url}")
                if ssl_context_ref is not None:
                    try:
                        cert_p, key_p = await asyncio.to_thread(ensure_cert, latest_ip)
                        ssl_context_ref.load_cert_chain(cert_p, key_p)
                    except Exception as e:
                        print("Failed to hot-reload TLS certificate:", e)
                try:
                    await lan_advertiser.update_ip(latest_ip)
                except Exception as e:
                    print("Failed to refresh LAN discovery IP:", e)

        current_render_id, current_capture_id = await asyncio.to_thread(
            audio_switcher.get_current_default_ids
        )
        render_changed = bool(current_render_id and current_render_id != active_render_id)
        capture_changed = current_capture_id != last_capture_id

        if not render_changed and not capture_changed:
            continue

        last_capture_id = current_capture_id
        if render_changed:
            print("\n[~] Windows default playback device changed, switching loopback...")
            try:
                await asyncio.to_thread(setup_audio_devices, False)
                await notify_clients_config()
            except Exception as e:
                print("Failed to switch playback device:", e)

        await asyncio.to_thread(audio_switcher.refresh_status)


async def websocket_handler(request):
    battery_monitor.register_client_ip(request.remote)
    ws = web.WebSocketResponse(heartbeat=15.0)
    await ws.prepare(request)

    await ws.send_json(build_stream_config_message())
    await ws.send_json(build_now_playing_message())
    clients.add(ws)

    print(f"[+] iPhone connected (clients: {len(clients)})")

    try:
        async for message in ws:
            if message.type != web.WSMsgType.TEXT:
                continue
            try:
                payload = json.loads(message.data)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue

            msg_type = payload.get("type")
            if msg_type == "audio-info":
                with client_info_lock:
                    client_audio_info[ws] = {
                        "playbackSampleRate": _validated_sample_rate(
                            payload.get("playbackSampleRate")
                        )
                    }
            elif msg_type == "media":
                action = str(payload.get("action") or "").strip().lower()
                if action:
                    result = await media_controller.execute_command(action)
                    await notify_clients_now_playing(result.get("nowPlaying"))
    except Exception as e:
        print("WebSocket error:", e)
    finally:
        clients.discard(ws)
        with client_info_lock:
            client_audio_info.pop(ws, None)
        print(f"[-] iPhone disconnected (clients: {len(clients)})")

    return ws


async def mic_handler(request):
    global active_mic_ws

    battery_monitor.register_client_ip(request.remote)
    ws = web.WebSocketResponse(heartbeat=15.0)
    await ws.prepare(request)

    if active_mic_ws is not None and not active_mic_ws.closed:
        await active_mic_ws.close()
    active_mic_ws = ws

    print("[+] iPhone microphone on")

    try:
        async for message in ws:
            if message.type == web.WSMsgType.TEXT:
                try:
                    payload = json.loads(message.data)
                except (TypeError, ValueError):
                    continue
                if isinstance(payload, dict) and payload.get("type") == "audio-info":
                    with client_info_lock:
                        client_mic_info[ws] = {
                            "micInputSampleRate": _validated_sample_rate(
                                payload.get("micInputSampleRate")
                            ),
                            "micContextSampleRate": _validated_sample_rate(
                                payload.get("micContextSampleRate")
                            ),
                        }
                continue
            if message.type != web.WSMsgType.BINARY or not mic_info["available"]:
                continue

            try:
                mic_queue.put_nowait(message.data)
            except queue.Full:
                try:
                    mic_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    mic_queue.put_nowait(message.data)
                except queue.Full:
                    pass
    finally:
        with client_info_lock:
            client_mic_info.pop(ws, None)
        if active_mic_ws is ws:
            active_mic_ws = None
        print("[-] iPhone microphone off")

    return ws


async def _send_bytes_safe(client, data):
    try:
        await client.send_bytes(data)
        return None
    except Exception as e:
        return e


async def audio_broadcast_loop():
    """
    Broadcast WASAPI loopback PCM frames to connected iPhones.
    When Windows audio output is completely silent/paused, WASAPI loopback stops firing
    callbacks; we emit a lightweight silent PCM keepalive frame every 200ms so NAT/Wi-Fi
    and iOS background URLSessionWebSocketTask never stall or idle-timeout.
    """
    while not shutdown_event.is_set():
        try:
            data = await asyncio.wait_for(
                audio_queue.get(), timeout=SILENCE_KEEPALIVE_INTERVAL
            )
        except asyncio.TimeoutError:
            if not clients:
                continue
            ch = max(1, min(2, int(stream_channels or 2)))
            data = b"\x00" * (SILENCE_KEEPALIVE_FRAMES * ch * 2)

        if not clients:
            continue

        snapshot = list(clients)
        results = await asyncio.gather(
            *(_send_bytes_safe(client, data) for client in snapshot)
        )

        for client, err in zip(snapshot, results):
            if err is not None:
                clients.discard(client)


async def index_handler(request):
    battery_monitor.register_client_ip(request.remote)
    return web.FileResponse(
        WEB_DIR / "index.html",
        headers={"Cache-Control": "no-store"},
    )


async def worklet_handler(request):
    return web.FileResponse(
        WEB_DIR / "worklet.js",
        headers={
            "Cache-Control": "no-store",
            "Content-Type": "application/javascript",
        },
    )


async def config_handler(request):
    battery_monitor.register_client_ip(request.remote)
    return web.json_response(
        {
            "sampleRate": stream_rate,
            "channels": stream_channels,
            "deviceName": stream_device_name,
            "hostname": SERVER_HOSTNAME,
            "port": PORT,
            "micAvailable": mic_info["available"],
            "micRate": mic_info["rate"],
            "micChannels": mic_info["channels"],
            "micDeviceName": mic_info["deviceName"],
            "nowPlaying": media_controller.get_cached_snapshot(),
        }
    )


async def media_post_handler(request):
    """HTTP fallback endpoint for remote media playback controls from iPhone."""
    battery_monitor.register_client_ip(request.remote)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

    action = str(payload.get("action") or "").strip().lower()
    if not action:
        return web.json_response({"ok": False, "error": "Missing 'action'"}, status=400)

    result = await media_controller.execute_command(action)
    status_code = 200 if "error" not in result else 400
    if "nowPlaying" in result:
        await notify_clients_now_playing(result["nowPlaying"])
    return web.json_response(result, status=status_code)


async def battery_get_handler(request):
    return web.json_response(battery_monitor.get_snapshot())


async def battery_post_handler(request):
    """Optional webhook endpoint for iOS Shortcuts to push iPhone battery level."""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    if "level" not in payload:
        return web.json_response({"error": "Missing 'level' field"}, status=400)

    try:
        level = int(float(payload["level"]))
    except (TypeError, ValueError):
        return web.json_response({"error": "Invalid 'level' value"}, status=400)

    charging = payload.get("charging")
    name = str(payload.get("name") or "iPhone")
    battery_monitor.update_iphone_from_http(level=level, charging=charging, name=name)
    return web.json_response({"ok": True})


def get_desktop_state():
    with client_info_lock:
        audio_info = next(reversed(list(client_audio_info.values())), {})
        phone_mic_info = next(reversed(list(client_mic_info.values())), {})
    return {
        "url": server_url,
        "hostname": SERVER_HOSTNAME,
        "deviceName": stream_device_name,
        "sampleRate": stream_rate,
        "channels": stream_channels,
        "micRate": mic_info["rate"],
        "micChannels": mic_info["channels"],
        "micDeviceName": mic_info["deviceName"],
        "iphonePlaybackRate": audio_info.get("playbackSampleRate"),
        "iphoneMicInputRate": phone_mic_info.get("micInputSampleRate"),
        "iphoneMicContextRate": phone_mic_info.get("micContextSampleRate"),
        "clients": len(clients),
        "micActive": active_mic_ws is not None and not active_mic_ws.closed,
        "micAvailable": mic_info["available"],
        "battery": battery_monitor.get_snapshot(),
        "audioSwitch": audio_switcher.get_cached_status(),
        "autostart": autostart_manager.is_enabled(),
        "nowPlaying": media_controller.get_cached_snapshot(),
    }


def request_server_stop():
    shutdown_event.set()
    battery_monitor.stop()
    if loop is not None and async_stop_event is not None:
        try:
            loop.call_soon_threadsafe(async_stop_event.set)
        except RuntimeError:
            pass


async def main():
    global loop, async_stop_event, server_url, ssl_context_ref, bound_lan_ip

    loop = asyncio.get_running_loop()
    async_stop_event = asyncio.Event()
    runner = None
    broadcast_task = None
    monitor_task = None
    media_task = None

    _install_console_exit_handler()

    try:
        battery_monitor.start_background()
        if "--no-auto-switch" in sys.argv:
            await asyncio.to_thread(audio_switcher.refresh_status)
        else:
            await asyncio.to_thread(audio_switcher.switch_on_startup)
        setup_audio_devices(verbose=True)
        threading.Thread(target=mic_writer, daemon=True).start()
        await media_controller.poll_now_playing()

        app = web.Application()
        app.router.add_get("/", index_handler)
        app.router.add_get("/worklet.js", worklet_handler)
        app.router.add_get("/config", config_handler)
        app.router.add_post("/media", media_post_handler)
        app.router.add_get("/battery", battery_get_handler)
        app.router.add_post("/battery", battery_post_handler)
        app.router.add_get("/audio", websocket_handler)
        app.router.add_get("/mic", mic_handler)

        runner = web.AppRunner(app)
        await runner.setup()

        ip = get_local_ip()
        bound_lan_ip = ip
        server_url = f"https://{ip}:{PORT}"
        cert_path, key_path = ensure_cert(ip)

        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(cert_path, key_path)
        ssl_context_ref = ssl_context

        site = web.TCPSite(runner, HOST, PORT, ssl_context=ssl_context)
        await site.start()

        await lan_advertiser.start(
            primary_ip=ip,
            device_name_getter=lambda: stream_device_name,
        )

        print()
        print("=" * 60)
        print(" iPhone Audio Bridge")
        print("=" * 60)
        print()
        print("Open on your iPhone:")
        print()
        print(f"    {server_url}")
        print()
        print("Safari will warn about the certificate:")
        print("Show Details -> visit this website.")
        print()
        print("=" * 60)

        broadcast_task = asyncio.create_task(audio_broadcast_loop())
        monitor_task = asyncio.create_task(device_monitor_loop())
        media_task = asyncio.create_task(media_monitor_loop())

        await async_stop_event.wait()
    finally:
        ssl_context_ref = None
        battery_monitor.stop()
        await lan_advertiser.stop()
        for task in (broadcast_task, monitor_task, media_task):
            if task is not None:
                task.cancel()
        for ws in list(clients):
            await ws.close()
        if active_mic_ws is not None and not active_mic_ws.closed:
            await active_mic_ws.close()
        if runner is not None:
            await runner.cleanup()
        shutdown_audio_devices()
        await asyncio.to_thread(audio_switcher.restore_on_exit)


def _acquire_single_instance_handles():
    """
    Ensure only one GUI instance runs at a time on Windows.
    If another instance is already active, signal its ShowEvent so its right-edge
    Nightcord drawer slides out, and return (None, None, True).
    """
    if sys.platform != "win32":
        return None, None, False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateEventW.restype = c_void_p
        kernel32.CreateEventW.argtypes = [c_void_p, ctypes.c_int, ctypes.c_int, c_wchar_p]
        kernel32.CreateMutexW.restype = c_void_p
        kernel32.CreateMutexW.argtypes = [c_void_p, ctypes.c_int, c_wchar_p]
        kernel32.SetEvent.argtypes = [c_void_p]
        kernel32.CloseHandle.argtypes = [c_void_p]

        show_evt = kernel32.CreateEventW(
            None, 0, 0, "Local\\NightcordAudioBridge_ShowEvent"
        )
        mutex = kernel32.CreateMutexW(
            None, 0, "Local\\NightcordAudioBridge_Mutex"
        )
        already_running = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
        if already_running:
            if show_evt:
                kernel32.SetEvent(show_evt)
                kernel32.CloseHandle(show_evt)
            if mutex:
                kernel32.CloseHandle(mutex)
            return None, None, True
        return mutex, show_evt, False
    except Exception:
        return None, None, False


def _close_win32_handle(handle):
    if sys.platform == "win32" and handle:
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [c_void_p]
            kernel32.CloseHandle(handle)
        except Exception:
            pass


def run_with_gui():
    from desktop_ui import DesktopPanel

    mutex_h, show_evt_h, already_running = _acquire_single_instance_handles()
    if already_running:
        print("Nightcord Audio Bridge is already running; signaled existing drawer.")
        return

    panel_ref = {}
    handles = {"mutex": mutex_h, "show_evt": show_evt_h}

    def release_instance_handles():
        _close_win32_handle(handles.get("mutex"))
        handles["mutex"] = None
        _close_win32_handle(handles.get("show_evt"))
        handles["show_evt"] = None

    if sys.platform == "win32" and show_evt_h:
        def _watch_show_signal():
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.WaitForSingleObject.argtypes = [c_void_p, DWORD]
            kernel32.WaitForSingleObject.restype = DWORD
            while not shutdown_event.is_set() and handles.get("show_evt"):
                rc = kernel32.WaitForSingleObject(handles["show_evt"], 400)
                if rc == 0:  # WAIT_OBJECT_0
                    p = panel_ref.get("panel")
                    if p is not None:
                        p.request_show()

        threading.Thread(target=_watch_show_signal, daemon=True).start()

    def server_worker():
        try:
            asyncio.run(main())
        except Exception as e:
            print("Server error:", e)
            panel = panel_ref.get("panel")
            if panel is not None:
                panel.request_quit()

    server_thread = threading.Thread(target=server_worker, daemon=True)
    server_thread.start()

    def on_quit():
        request_server_stop()
        server_thread.join(timeout=2.5)
        audio_switcher.restore_on_exit()
        release_instance_handles()

    def on_restart():
        def restart_worker():
            request_server_stop()
            server_thread.join(timeout=10)
            if server_thread.is_alive():
                print("Restart aborted: server did not stop in time.")
                panel.request_restart_failed()
                return

            audio_switcher.restore_on_exit()
            release_instance_handles()

            try:
                py_bin = (
                    str(resolve_windowless_python())
                    if sys.platform == "win32"
                    else sys.executable
                )
                creation_flags = (
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    if sys.platform == "win32"
                    else 0
                )
                subprocess.Popen(
                    [py_bin, str(Path(__file__).resolve()), *sys.argv[1:]],
                    cwd=str(BASE_DIR),
                    creationflags=creation_flags,
                )
            except Exception as e:
                print("Failed to restart server:", e)
                panel.request_restart_failed()
                return

            panel.request_quit()

        threading.Thread(target=restart_worker, daemon=True).start()

    panel = DesktopPanel(
        get_state_fn=get_desktop_state,
        on_refresh_iphone_fn=battery_monitor.trigger_refresh,
        on_quit_fn=on_quit,
        on_pair_iphone_fn=battery_monitor.trigger_ble_pair,
        on_restart_fn=on_restart,
        on_switch_virtual_fn=trigger_switch_to_virtual,
        on_restore_audio_fn=trigger_restore_previous_audio,
        on_select_airpods_fn=battery_monitor.select_airpods_candidate,
        on_reject_airpods_fn=battery_monitor.reject_current_airpods,
        on_unlock_airpods_fn=battery_monitor.unlock_airpods_binding,
        on_toggle_autostart_fn=autostart_manager.toggle,
    )
    panel_ref["panel"] = panel

    signal.signal(signal.SIGINT, lambda *_: panel.request_quit())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: panel.request_quit())
    try:
        panel.run()
    finally:
        release_instance_handles()


if __name__ == "__main__":
    if "--no-gui" in sys.argv:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            print("\nShutting down...")
        finally:
            audio_switcher.restore_on_exit()
    else:
        try:
            run_with_gui()
        except KeyboardInterrupt:
            print("\nShutting down...")
            request_server_stop()
        finally:
            audio_switcher.restore_on_exit()



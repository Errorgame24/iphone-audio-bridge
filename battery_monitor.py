import asyncio
import json
import os
import plistlib
import re
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from uuid import UUID

APPLE_COMPANY_ID = 0x004C
LOCKDOWN_PORT = 62078
USBMUXD_ADDR = ("127.0.0.1", 27015)
LOCKDOWN_DIR = os.path.join(
    os.environ.get("ProgramData", r"C:\ProgramData"), "Apple", "Lockdown"
)

# Apple Continuity Proximity Pairing (0x07) 16-bit little-endian model IDs
# Value tuple: (Display Name, is_single_battery_headphone)
APPLE_AUDIO_MODELS = {
    0x2002: ("AirPods (1代)", False),
    0x200F: ("AirPods (2代)", False),
    0x2013: ("AirPods (3代)", False),
    0x2019: ("AirPods 4", False),
    0x201B: ("AirPods 4 (ANC)", False),
    0x200E: ("AirPods Pro", False),
    0x2014: ("AirPods Pro 2", False),
    0x2024: ("AirPods Pro 2 (USB-C)", False),
    0x2027: ("AirPods Pro 3", False),
    0x200A: ("AirPods Max", True),
    0x201F: ("AirPods Max (USB-C)", True),
    0x2003: ("Powerbeats 3", True),
    0x2005: ("BeatsX", True),
    0x2006: ("Beats Solo3", True),
    0x2009: ("Beats Studio3", True),
    0x200B: ("Powerbeats Pro", False),
    0x200C: ("Beats Solo Pro", True),
    0x2010: ("Beats Flex", True),
    0x2011: ("Beats Studio Buds", False),
    0x2012: ("Beats Fit Pro", False),
    0x2016: ("Beats Studio Buds +", False),
    0x2017: ("Beats Studio Pro", True),
    0x201D: ("Beats Solo 4", True),
    0x2025: ("Powerbeats Pro 2", False),
}

BATTERY_SERVICE_UUID = UUID("0000180f-0000-1000-8000-00805f9b34fb")
BATTERY_LEVEL_UUID = UUID("00002a19-0000-1000-8000-00805f9b34fb")
BATTERY_POWER_STATE_UUID = UUID("00002a1a-0000-1000-8000-00805f9b34fb")

APPLE_ANCS_UUID = UUID("7905f431-b5ce-4e99-a40f-4b1e122d00d0")
APPLE_CONTINUITY_SERVICE_UUID = UUID("d0611e78-bbb4-4591-a5f8-487910ae4366")
DEVICE_INFO_SERVICE_UUID = UUID("0000180a-0000-1000-8000-00805f9b34fb")
MANUFACTURER_NAME_UUID = UUID("00002a29-0000-1000-8000-00805f9b34fb")
MODEL_NUMBER_UUID = UUID("00002a24-0000-1000-8000-00805f9b34fb")

AIRPODS_STALE_SECONDS = 35.0
IPHONE_POLL_INTERVAL = 20.0


def _send_lockdown_plist(sock: socket.socket, obj: dict):
    raw = plistlib.dumps(obj)
    sock.sendall(struct.pack(">I", len(raw)) + raw)


def _recv_lockdown_plist(sock: socket.socket) -> dict:
    hdr = b""
    while len(hdr) < 4:
        chunk = sock.recv(4 - len(hdr))
        if not chunk:
            raise EOFError("Lockdown connection closed")
        hdr += chunk
    length = struct.unpack(">I", hdr)[0]
    if length <= 0 or length > 4 * 1024 * 1024:
        raise ValueError(f"Invalid Lockdown frame length: {length}")
    body = b""
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            raise EOFError("Truncated Lockdown frame")
        body += chunk
    return plistlib.loads(body)


def _nibble_to_percent(raw: int):
    if 0 <= raw <= 10:
        return min(100, raw * 10)
    return None


def parse_airpods_advertisement(payload: bytes, rssi: int):
    """
    Decode Apple Continuity Proximity Pairing (type 0x07) BLE manufacturer payload.
    `payload` is manufacturer data for company 0x004C (without the 2-byte 0x004C header).
    """
    if len(payload) < 11 or payload[0] != 0x07 or payload[1] < 0x13:
        return None

    model_id = (payload[4] << 8) | payload[3]
    model_name, is_single = APPLE_AUDIO_MODELS.get(
        model_id, (f"AirPods / Beats (0x{model_id:04X})", False)
    )

    status = payload[5]
    pods_byte = payload[6]
    flags_case = payload[7]

    # Bit 0x20 of byte 5 indicates whether Right (1 -> not flipped) or Left (0 -> flipped) is primary
    is_flipped = (status & 0x20) == 0

    pod_a = _nibble_to_percent((pods_byte >> 4) & 0x0F)
    pod_b = _nibble_to_percent(pods_byte & 0x0F)

    left = pod_a if is_flipped else pod_b
    right = pod_b if is_flipped else pod_a
    case = _nibble_to_percent(flags_case & 0x0F)

    charge_flags = (flags_case >> 4) & 0x0F
    left_charging = bool(charge_flags & (0x02 if is_flipped else 0x01))
    right_charging = bool(charge_flags & (0x01 if is_flipped else 0x02))
    case_charging = bool(charge_flags & 0x04)

    in_ear_flags = status & 0x0F
    left_in_ear = (not left_charging) and bool(in_ear_flags & (0x08 if is_flipped else 0x02))
    right_in_ear = (not right_charging) and bool(in_ear_flags & (0x02 if is_flipped else 0x08))

    single = None
    single_charging = False
    if is_single:
        single = left if left is not None else right
        single_charging = left_charging or right_charging

    return {
        "modelId": f"0x{model_id:04X}",
        "model": model_name,
        "color": int(payload[9]),
        "isSingle": is_single,
        "left": left,
        "right": right,
        "case": case,
        "single": single,
        "leftCharging": left_charging,
        "rightCharging": right_charging,
        "caseCharging": case_charging,
        "singleCharging": single_charging,
        "leftInEar": left_in_ear,
        "rightInEar": right_in_ear,
        "rssi": rssi,
    }


class BatteryMonitor:
    """
    Background battery monitor on Windows:
    1. Passively listens for Apple Continuity 0x07 BLE broadcasts from AirPods / Beats,
       locking onto the user's headphones to prevent nearby bystander headphones from hijacking.
    2. Reads iPhone battery silently via:
       - Tier 1: Apple Lockdown (usbmuxd USB / Wi-Fi Sync + Hotspot/LAN TCP 62078, 1% precision)
       - Tier 2: Windows BLE GATT Battery Service (0x180F / 0x2A19)
       - Tier 3: Optional HTTP push webhook (/battery)
    """

    def __init__(self, state_file: Path = None):
        self._lock = threading.Lock()
        self._state_file = Path(state_file) if state_file else None
        self._stop_event = False
        self._stop_async_event = None
        self._refresh_event = None
        self._loop = None
        self._thread = None

        self._ignored_ble_ids = set()
        self._verified_iphone_ids = set()
        self._nearby_iphone_ble = {}  # mac_str -> (rssi, timestamp)
        self._pairing_in_progress = False
        self._pair_hint_override = ("", 0.0)

        # Apple Lockdown (USB / Hotspot / Wi-Fi) state
        self._client_ips = []
        self._last_lockdown_ip = ""
        self._preferred_host_id = ""
        self._ssl_ctx_cache = {}  # host_id -> ssl.SSLContext

        # AirPods identity lock & nearby candidate table
        self._bound_model_id = ""
        self._bound_color = None
        self._bound_model = ""
        self._bound_name = ""
        self._bound_is_single = False
        self._active_airpods_key = ""
        self._ignored_airpods_keys = set()
        self._airpods_candidates = {}  # cand_key -> candidate dict

        self._airpods = self._empty_airpods_dict()
        self._load_airpods_binding()

        self._iphone = {
            "state": "unpaired",  # unpaired | needs_ble_pair | pairing | disconnected | connected | shortcut | bt_off
            "name": "",
            "level": None,
            "charging": None,
            "source": "",
            "updatedAt": None,
            "hint": "正在自动检测连接的 iPhone...",
        }

    @staticmethod
    def _empty_airpods_dict():
        return {
            "online": False,
            "model": "",
            "name": "",
            "modelId": "",
            "color": None,
            "isSingle": False,
            "left": None,
            "right": None,
            "case": None,
            "lastCase": None,
            "single": None,
            "leftCharging": False,
            "rightCharging": False,
            "caseCharging": False,
            "singleCharging": False,
            "leftInEar": False,
            "rightInEar": False,
            "rssi": None,
            "updatedAt": None,
        }

    def _load_airpods_binding(self):
        if not self._state_file:
            return
        try:
            if not self._state_file.exists():
                return
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            model_id = str(data.get("boundModelId") or "").strip()
            if not model_id:
                return
            self._bound_model_id = model_id
            raw_color = data.get("boundColor")
            self._bound_color = int(raw_color) if isinstance(raw_color, int) else None
            self._bound_model = str(data.get("boundModel") or "").strip()
            self._bound_name = str(data.get("boundName") or "").strip()
            self._bound_is_single = bool(data.get("boundIsSingle", False))
            self._airpods.update(
                modelId=self._bound_model_id,
                model=self._bound_model,
                name=self._bound_name,
                color=self._bound_color,
                isSingle=self._bound_is_single,
            )
        except Exception:
            pass

    def _save_airpods_binding_unlocked(self):
        if not self._state_file:
            return
        payload = {
            "boundModelId": self._bound_model_id,
            "boundColor": self._bound_color,
            "boundModel": self._bound_model,
            "boundName": self._bound_name,
            "boundIsSingle": self._bound_is_single,
        }
        try:
            self._state_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def _build_candidates_snapshot_unlocked(self, now: float) -> list:
        cutoff = now - 75.0
        stale_keys = [
            k for k, v in self._airpods_candidates.items() if v["updatedAt"] < cutoff
        ]
        for k in stale_keys:
            self._airpods_candidates.pop(k, None)

        items = []
        for k, c in self._airpods_candidates.items():
            items.append(
                {
                    "key": k,
                    "modelId": c["modelId"],
                    "model": c["model"],
                    "name": c["name"],
                    "color": c["color"],
                    "isSingle": c["isSingle"],
                    "left": c["left"],
                    "right": c["right"],
                    "case": c["case"],
                    "single": c["single"],
                    "rssi": int(round(c["rssi"])) if c["rssi"] is not None else None,
                    "ignored": k in self._ignored_airpods_keys,
                    "isCurrent": bool(self._active_airpods_key and k == self._active_airpods_key),
                }
            )
        items.sort(
            key=lambda x: (
                0 if x["isCurrent"] else (2 if x["ignored"] else 1),
                -(x["rssi"] if x["rssi"] is not None else -100),
            )
        )
        return items

    def get_snapshot(self):
        now = time.time()
        with self._lock:
            ap = dict(self._airpods)
            if ap["updatedAt"] is not None and (now - ap["updatedAt"]) > AIRPODS_STALE_SECONDS:
                ap["online"] = False
            ap["locked"] = bool(self._bound_model_id)
            ap["lockedModel"] = self._bound_model or ap["model"]
            ap["activeKey"] = self._active_airpods_key
            ap["candidates"] = self._build_candidates_snapshot_unlocked(now)
            ph = dict(self._iphone)
        return {"airpods": ap, "iphone": ph, "timestamp": now}

    def register_client_ip(self, remote_ip: str):
        """Record an HTTP/WebSocket client IP from server.py to speed up LAN Lockdown discovery."""
        if not remote_ip:
            return
        ip = remote_ip.strip()
        if ip.startswith("::ffff:"):
            ip = ip[7:]
        if not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip) or ip.startswith("127."):
            return

        should_refresh = False
        with self._lock:
            if ip in self._client_ips:
                self._client_ips.remove(ip)
            self._client_ips.insert(0, ip)
            del self._client_ips[8:]
            if self._iphone["level"] is None:
                should_refresh = True

        if should_refresh:
            self.trigger_refresh()

    def trigger_refresh(self):
        """Request an immediate iPhone battery poll."""
        if self._loop is not None and self._refresh_event is not None:
            try:
                self._loop.call_soon_threadsafe(self._refresh_event.set)
            except RuntimeError:
                pass

    def trigger_ble_pair(self):
        """Initiate Windows BLE CustomPairing directly with the nearby iPhone."""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self._pair_nearby_iphone_ble())
        )

    def update_iphone_from_http(self, level: int, charging=None, name: str = "iPhone"):
        level = max(0, min(100, int(level)))
        with self._lock:
            self._iphone.update(
                state="shortcut",
                name=name or self._iphone["name"] or "iPhone",
                level=level,
                charging=bool(charging) if charging is not None else None,
                source="快捷指令推送",
                updatedAt=time.time(),
                hint="通过快捷指令 Webhook 同步",
            )

    def _bind_to_candidate_unlocked(self, cand: dict):
        self._bound_model_id = cand["modelId"]
        self._bound_color = cand["color"]
        self._bound_model = cand["model"]
        if cand["name"]:
            self._bound_name = cand["name"]
        self._bound_is_single = cand["isSingle"]
        self._active_airpods_key = cand["key"]
        self._save_airpods_binding_unlocked()

    def _apply_candidate_to_airpods_unlocked(self, cand: dict, now: float):
        prev_time = self._airpods["updatedAt"]
        prev_model = self._airpods["modelId"]
        same_device = cand["modelId"] == prev_model

        last_case = (
            cand["case"]
            if cand["case"] is not None
            else (self._airpods["lastCase"] if same_device else None)
        )

        # Keep previous L/R reading briefly if one pod momentarily stops advertising
        left = cand["left"]
        right = cand["right"]
        if left is None and prev_time and (now - prev_time) < 8.0 and same_device:
            left = self._airpods["left"]
        if right is None and prev_time and (now - prev_time) < 8.0 and same_device:
            right = self._airpods["right"]

        resolved_name = cand["name"] or (
            self._airpods["name"] if same_device else self._bound_name
        )
        if resolved_name and cand["modelId"] == self._bound_model_id:
            if resolved_name != self._bound_name:
                self._bound_name = resolved_name
                self._save_airpods_binding_unlocked()

        self._airpods.update(
            online=True,
            model=cand["model"],
            name=resolved_name,
            modelId=cand["modelId"],
            color=cand["color"],
            isSingle=cand["isSingle"],
            left=left,
            right=right,
            case=cand["case"],
            lastCase=last_case,
            single=cand["single"],
            leftCharging=cand["leftCharging"],
            rightCharging=cand["rightCharging"],
            caseCharging=cand["caseCharging"],
            singleCharging=cand["singleCharging"],
            leftInEar=cand["leftInEar"],
            rightInEar=cand["rightInEar"],
            rssi=int(round(cand["rssi"])) if cand["rssi"] is not None else None,
            updatedAt=now,
        )

    def _max_pod_jump_unlocked(self, cand: dict) -> int:
        jumps = []
        for field in ("left", "right", "single"):
            old_v = self._airpods.get(field)
            new_v = cand.get(field)
            if old_v is not None and new_v is not None:
                jumps.append(abs(int(new_v) - int(old_v)))
        return max(jumps) if jumps else 0

    def _should_accept_airpods_unlocked(self, cand: dict, now: float) -> bool:
        key = cand["key"]
        rssi = cand["rssi"]

        if key in self._ignored_airpods_keys:
            return False

        # Case 1: Not bound to any headphone yet -> acquire only close, consistent desk-range signals
        if not self._bound_model_id:
            if rssi is not None and rssi < -66:
                return False
            if cand["address"] and rssi is not None and rssi < -58 and cand["hits"] < 2:
                return False
            # Defer if a noticeably stronger nearby headphone is broadcasting at the same moment
            if rssi is not None:
                for other_k, other in self._airpods_candidates.items():
                    if (
                        other_k != key
                        and other_k not in self._ignored_airpods_keys
                        and (now - other["updatedAt"]) < 4.0
                        and other["rssi"] is not None
                        and other["rssi"] > rssi + 6
                    ):
                        return False
            self._bind_to_candidate_unlocked(cand)
            return True

        # Case 2: Bound to the user's headphone -> strictly enforce model & color
        if cand["modelId"] != self._bound_model_id:
            return False
        if (
            self._bound_color is not None
            and cand["color"] is not None
            and cand["color"] != self._bound_color
        ):
            return False
        if rssi is not None and rssi < -76:
            return False

        # Same active BLE MAC session -> accept directly
        if not self._active_airpods_key or key == self._active_airpods_key:
            self._active_airpods_key = key
            return True

        # Different BLE MAC with identical model & color:
        # Either a bystander has the exact same headphone model, or our AirPods rotated RPA MAC (~15m)
        prev_time = self._airpods["updatedAt"]
        prev_rssi = self._airpods["rssi"]

        if prev_time is not None and (now - prev_time) < 40.0:
            # Our active MAC is still broadcasting live; block concurrent bystander MACs
            if (
                cand["hits"] >= 3
                and rssi is not None
                and prev_rssi is not None
                and rssi >= -52
                and rssi >= prev_rssi + 15
            ):
                self._active_airpods_key = key
                return True
            return False

        # Previous MAC went silent (>= 40s): verify desk proximity + battery continuity before RPA handover
        if rssi is not None and rssi < -68:
            return False
        if prev_time is not None and (now - prev_time) < 900.0:
            if self._max_pod_jump_unlocked(cand) > 20:
                if not (rssi is not None and rssi >= -52 and cand["hits"] >= 2):
                    return False

        self._active_airpods_key = key
        return True

    def select_airpods_candidate(self, key: str):
        """Manually lock onto a specific nearby AirPods/Beats candidate chosen by the user."""
        if not key:
            return
        now = time.time()
        with self._lock:
            self._ignored_airpods_keys.discard(key)
            cand = self._airpods_candidates.get(key)
            if not cand:
                return
            self._airpods = self._empty_airpods_dict()
            self._bind_to_candidate_unlocked(cand)
            self._apply_candidate_to_airpods_unlocked(cand, now)

    def reject_current_airpods(self):
        """Blacklist the currently displayed bystander headphone and switch/re-scan for the user's own."""
        now = time.time()
        with self._lock:
            if self._active_airpods_key:
                self._ignored_airpods_keys.add(self._active_airpods_key)
            self._bound_model_id = ""
            self._bound_color = None
            self._bound_model = ""
            self._bound_name = ""
            self._bound_is_single = False
            self._active_airpods_key = ""
            self._airpods = self._empty_airpods_dict()
            self._save_airpods_binding_unlocked()

            # Immediately pick the strongest remaining desk-range candidate if one is already present
            remaining = [
                c
                for k, c in self._airpods_candidates.items()
                if k not in self._ignored_airpods_keys
                and (now - c["updatedAt"]) < 25.0
                and (c["rssi"] is None or c["rssi"] >= -66)
            ]
            if remaining:
                remaining.sort(
                    key=lambda c: c["rssi"] if c["rssi"] is not None else -100,
                    reverse=True,
                )
                best = remaining[0]
                self._bind_to_candidate_unlocked(best)
                self._apply_candidate_to_airpods_unlocked(best, now)

    def unlock_airpods_binding(self):
        """Clear persistent headphone lock so a newly paired headphone model can be auto-detected."""
        with self._lock:
            self._bound_model_id = ""
            self._bound_color = None
            self._bound_model = ""
            self._bound_name = ""
            self._bound_is_single = False
            self._active_airpods_key = ""
            self._ignored_airpods_keys.clear()
            self._airpods = self._empty_airpods_dict()
            self._save_airpods_binding_unlocked()

    def _on_airpods_packet(self, parsed: dict):
        now = time.time()
        raw_rssi = parsed.get("rssi")

        # Filter out very weak background packets from distant rooms
        if raw_rssi is not None and raw_rssi < -78:
            return

        address = (parsed.get("address") or "").strip().upper()
        color = parsed.get("color")
        cand_key = address or f"{parsed['modelId']}:{color if color is not None else 'x'}"

        with self._lock:
            prev_cand = self._airpods_candidates.get(cand_key)
            if prev_cand and (now - prev_cand["updatedAt"]) <= 12.0:
                hits = prev_cand["hits"] + 1
                if raw_rssi is not None and prev_cand["rssi"] is not None:
                    smoothed_rssi = 0.6 * raw_rssi + 0.4 * prev_cand["rssi"]
                else:
                    smoothed_rssi = raw_rssi if raw_rssi is not None else prev_cand["rssi"]
            else:
                hits = 1
                smoothed_rssi = float(raw_rssi) if raw_rssi is not None else None

            adv_name = (parsed.get("name") or "").strip()
            if not adv_name and prev_cand:
                adv_name = prev_cand["name"]

            cand = {
                "key": cand_key,
                "address": address,
                "modelId": parsed["modelId"],
                "model": parsed["model"],
                "color": color,
                "name": adv_name,
                "isSingle": parsed["isSingle"],
                "left": parsed["left"],
                "right": parsed["right"],
                "case": parsed["case"],
                "single": parsed["single"],
                "leftCharging": parsed["leftCharging"],
                "rightCharging": parsed["rightCharging"],
                "caseCharging": parsed["caseCharging"],
                "singleCharging": parsed["singleCharging"],
                "leftInEar": parsed["leftInEar"],
                "rightInEar": parsed["rightInEar"],
                "rssi": smoothed_rssi,
                "hits": hits,
                "updatedAt": now,
            }
            self._airpods_candidates[cand_key] = cand

            if len(self._airpods_candidates) > 24:
                self._build_candidates_snapshot_unlocked(now)

            if not self._should_accept_airpods_unlocked(cand, now):
                return

            self._apply_candidate_to_airpods_unlocked(cand, now)

    async def _scanner_loop(self):
        from bleak import BleakScanner

        def detection_callback(device, advertisement_data):
            mfg = advertisement_data.manufacturer_data
            if APPLE_COMPANY_ID not in mfg:
                return
            payload = mfg[APPLE_COMPANY_ID]
            if not payload:
                return

            if payload[0] == 0x07:
                parsed = parse_airpods_advertisement(payload, advertisement_data.rssi)
                if parsed is not None:
                    parsed["address"] = getattr(device, "address", "") or ""
                    parsed["name"] = (
                        getattr(advertisement_data, "local_name", None)
                        or getattr(device, "name", None)
                        or ""
                    )
                    self._on_airpods_packet(parsed)
            elif payload[0] == 0x10 and advertisement_data.rssi is not None and advertisement_data.rssi >= -78:
                now = time.time()
                with self._lock:
                    self._nearby_iphone_ble[device.address] = (advertisement_data.rssi, now)
                    if len(self._nearby_iphone_ble) > 24:
                        cutoff = now - 60.0
                        self._nearby_iphone_ble = {
                            k: v for k, v in self._nearby_iphone_ble.items() if v[1] >= cutoff
                        }

        while not self._stop_event:
            scanner = None
            try:
                scanner = BleakScanner(detection_callback=detection_callback)
                await scanner.start()
                await self._stop_async_event.wait()
                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                print("BLE scanner warning:", e)
                with self._lock:
                    if self._iphone["state"] == "unpaired" and "radio" in str(e).lower():
                        self._iphone["state"] = "bt_off"
                        self._iphone["hint"] = "电脑蓝牙未开启，请打开系统蓝牙"
                try:
                    await asyncio.wait_for(self._stop_async_event.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
            finally:
                if scanner is not None:
                    try:
                        await scanner.stop()
                    except Exception:
                        pass

    async def _read_gatt_string(self, service, char_uuid, bt_mod, gatt_mod, streams_mod):
        try:
            res = await service.get_characteristics_for_uuid_async(
                char_uuid, bt_mod.BluetoothCacheMode.CACHED
            )
            if res.status != gatt_mod.GattCommunicationStatus.SUCCESS or not res.characteristics:
                res = await service.get_characteristics_for_uuid_async(
                    char_uuid, bt_mod.BluetoothCacheMode.UNCACHED
                )
            if res.status != gatt_mod.GattCommunicationStatus.SUCCESS or not res.characteristics:
                return ""

            val = await res.characteristics[0].read_value_async(bt_mod.BluetoothCacheMode.CACHED)
            if val.status != gatt_mod.GattCommunicationStatus.SUCCESS:
                val = await res.characteristics[0].read_value_async(bt_mod.BluetoothCacheMode.UNCACHED)
            if val.status != gatt_mod.GattCommunicationStatus.SUCCESS or val.value is None:
                return ""

            reader = streams_mod.DataReader.from_buffer(val.value)
            length = reader.unconsumed_buffer_length
            if length == 0:
                return ""
            raw = bytearray(length)
            reader.read_bytes(raw)
            return raw.decode("utf-8", errors="ignore").strip("\x00 ")
        except Exception:
            return ""

    async def _is_apple_iphone_ble(self, dev_id: str, dev_name: str, ble_dev, bt_mod, gatt_mod, streams_mod):
        if dev_id in self._verified_iphone_ids:
            return True
        if dev_id in self._ignored_ble_ids:
            return False

        lowered = (dev_name or "").lower()
        if "iphone" in lowered:
            self._verified_iphone_ids.add(dev_id)
            return True

        # Ignore obvious non-phone peripherals immediately by name
        for skip_word in ("mouse", "maus", "keyboard", "keychron", "logi", "xbox", "inzone", "airpods", "buds"):
            if skip_word in lowered:
                self._ignored_ble_ids.add(dev_id)
                return False

        # Check Apple ANCS or Continuity GATT service UUIDs
        for apple_uuid in (APPLE_ANCS_UUID, APPLE_CONTINUITY_SERVICE_UUID):
            for mode in (bt_mod.BluetoothCacheMode.CACHED, bt_mod.BluetoothCacheMode.UNCACHED):
                try:
                    svc = await asyncio.wait_for(
                        ble_dev.get_gatt_services_for_uuid_async(apple_uuid, mode),
                        timeout=2.5,
                    )
                    if svc.status == gatt_mod.GattCommunicationStatus.SUCCESS and svc.services:
                        for s in svc.services:
                            try:
                                s.close()
                            except Exception:
                                pass
                        self._verified_iphone_ids.add(dev_id)
                        return True
                except Exception:
                    pass

        # Check Device Information Service (0x180A) Manufacturer / Model
        try:
            dis = await ble_dev.get_gatt_services_for_uuid_async(
                DEVICE_INFO_SERVICE_UUID, bt_mod.BluetoothCacheMode.CACHED
            )
            if dis.status == gatt_mod.GattCommunicationStatus.SUCCESS and dis.services:
                svc0 = dis.services[0]
                mfg = await self._read_gatt_string(svc0, MANUFACTURER_NAME_UUID, bt_mod, gatt_mod, streams_mod)
                model = await self._read_gatt_string(svc0, MODEL_NUMBER_UUID, bt_mod, gatt_mod, streams_mod)
                if "apple" in mfg.lower() and ("iphone" in model.lower() or not model):
                    self._verified_iphone_ids.add(dev_id)
                    return True
                if mfg and "apple" not in mfg.lower():
                    self._ignored_ble_ids.add(dev_id)
                    return False
        except Exception:
            pass

        return False

    async def _read_battery_from_ble_device(self, ble_dev, bt_mod, gatt_mod, streams_mod):
        """Return (level: int | None, charging: bool | None) from BLE Battery Service 0x180F."""
        svc_res = None
        for cache_mode in (bt_mod.BluetoothCacheMode.UNCACHED, bt_mod.BluetoothCacheMode.CACHED):
            try:
                res = await asyncio.wait_for(
                    ble_dev.get_gatt_services_for_uuid_async(BATTERY_SERVICE_UUID, cache_mode),
                    timeout=4.0,
                )
                if res.status == gatt_mod.GattCommunicationStatus.SUCCESS and res.services:
                    svc_res = res
                    break
            except Exception:
                continue

        if svc_res is None or not svc_res.services:
            return None, None

        service = svc_res.services[0]
        try:
            char_res = await asyncio.wait_for(
                service.get_characteristics_for_uuid_async(
                    BATTERY_LEVEL_UUID, bt_mod.BluetoothCacheMode.CACHED
                ),
                timeout=3.0,
            )
            if char_res.status != gatt_mod.GattCommunicationStatus.SUCCESS or not char_res.characteristics:
                char_res = await asyncio.wait_for(
                    service.get_characteristics_for_uuid_async(
                        BATTERY_LEVEL_UUID, bt_mod.BluetoothCacheMode.UNCACHED
                    ),
                    timeout=3.0,
                )
            if char_res.status != gatt_mod.GattCommunicationStatus.SUCCESS or not char_res.characteristics:
                return None, None

            char = char_res.characteristics[0]

            # Prefer live UNCACHED read, fallback to CACHED (e.g. when Phone Link holds GATT subscription)
            read_res = None
            for mode in (bt_mod.BluetoothCacheMode.UNCACHED, bt_mod.BluetoothCacheMode.CACHED):
                try:
                    r = await asyncio.wait_for(char.read_value_async(mode), timeout=3.5)
                    if r.status == gatt_mod.GattCommunicationStatus.SUCCESS and r.value is not None:
                        read_res = r
                        break
                except Exception:
                    continue

            if read_res is None:
                return None, None

            reader = streams_mod.DataReader.from_buffer(read_res.value)
            if reader.unconsumed_buffer_length < 1:
                return None, None
            level = int(reader.read_byte())

            # Optional Battery Power State (0x2A1A): bits 4..5 represent charging state (3 = charging)
            charging = None
            try:
                ps_res = await service.get_characteristics_for_uuid_async(
                    BATTERY_POWER_STATE_UUID, bt_mod.BluetoothCacheMode.CACHED
                )
                if ps_res.status == gatt_mod.GattCommunicationStatus.SUCCESS and ps_res.characteristics:
                    ps_val = await ps_res.characteristics[0].read_value_async(
                        bt_mod.BluetoothCacheMode.UNCACHED
                    )
                    if ps_val.status == gatt_mod.GattCommunicationStatus.SUCCESS and ps_val.value is not None:
                        ps_reader = streams_mod.DataReader.from_buffer(ps_val.value)
                        if ps_reader.unconsumed_buffer_length >= 1:
                            state_byte = ps_reader.read_byte()
                            chg_bits = (state_byte >> 4) & 0x03
                            if chg_bits in (2, 3):
                                charging = chg_bits == 3
            except Exception:
                pass

            if 0 <= level <= 100:
                return level, charging
            return None, None
        finally:
            try:
                service.close()
            except Exception:
                pass

    async def _find_classic_paired_phone_name(self, bt, enum_mod):
        """Find custom smartphone name (e.g. 'Empurple') from Classic Bluetooth paired phones."""
        cl_selector = bt.BluetoothDevice.get_device_selector_from_pairing_state(True)
        cl_devices = await enum_mod.DeviceInformation.find_all_async_aqs_filter(cl_selector)

        fallback_name = ""
        for info in cl_devices:
            name = (info.name or "").strip()
            cl_dev = None
            try:
                cl_dev = await asyncio.wait_for(
                    bt.BluetoothDevice.from_id_async(info.id), timeout=2.5
                )
                if cl_dev is None:
                    continue
                # BluetoothMajorClass.PHONE == 2
                is_phone = int(cl_dev.class_of_device.major_class) == 2 or "iphone" in name.lower()
                if not is_phone:
                    continue

                if int(cl_dev.connection_status) == 1 and name:
                    return name
                if name and not fallback_name:
                    fallback_name = name
            except Exception:
                pass
            finally:
                if cl_dev is not None:
                    try:
                        cl_dev.close()
                    except Exception:
                        pass

        return fallback_name

    def _pick_display_name(self, ble_name: str, classic_phone_name: str):
        ble_clean = (ble_name or "").strip()
        if classic_phone_name and (
            not ble_clean
            or ble_clean.lower() == "iphone"
            or ble_clean.lower().startswith("bluetooth ")
        ):
            return classic_phone_name
        return ble_clean or classic_phone_name or "iPhone"

    def _get_ssl_context_for_record(self, host_id: str, cert_bytes: bytes, key_bytes: bytes):
        cached = self._ssl_ctx_cache.get(host_id)
        if cached is not None:
            return cached

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile("wb", delete=False, suffix=".pem") as tf:
                tf.write(cert_bytes + b"\n" + key_bytes)
                tmp_path = tf.name

            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ctx.load_cert_chain(certfile=tmp_path)
            self._ssl_ctx_cache[host_id] = ctx
            return ctx
        except Exception:
            return None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _load_lockdown_records(self):
        records = []
        seen_hosts = set()
        default_buid = ""

        if os.path.isdir(LOCKDOWN_DIR):
            sys_cfg_path = os.path.join(LOCKDOWN_DIR, "SystemConfiguration.plist")
            if os.path.isfile(sys_cfg_path):
                try:
                    with open(sys_cfg_path, "rb") as f:
                        default_buid = plistlib.load(f).get("SystemBUID", "")
                except Exception:
                    pass

            try:
                filenames = os.listdir(LOCKDOWN_DIR)
            except OSError:
                filenames = []

            for fn in filenames:
                if not fn.endswith(".plist") or fn == "SystemConfiguration.plist":
                    continue
                fpath = os.path.join(LOCKDOWN_DIR, fn)
                try:
                    with open(fpath, "rb") as f:
                        rec = plistlib.load(f)
                except Exception:
                    continue

                host_id = rec.get("HostID")
                cert = rec.get("HostCertificate")
                key = rec.get("HostPrivateKey")
                buid = rec.get("SystemBUID") or default_buid
                if not (host_id and cert and key and buid) or host_id in seen_hosts:
                    continue
                seen_hosts.add(host_id)
                records.append(
                    {
                        "host_id": host_id,
                        "system_buid": buid,
                        "cert": cert,
                        "key": key,
                    }
                )

        pref = self._preferred_host_id
        if pref:
            records.sort(key=lambda r: 0 if r["host_id"] == pref else 1)
        return records

    def _usbmuxd_roundtrip(self, payload: dict, keep_open: bool = False, timeout: float = 0.5):
        sock = socket.create_connection(USBMUXD_ADDR, timeout=timeout)
        try:
            body = plistlib.dumps(payload)
            pkt = struct.pack("<IIII", 16 + len(body), 1, 8, 1) + body
            sock.sendall(pkt)

            hdr = b""
            while len(hdr) < 16:
                chunk = sock.recv(16 - len(hdr))
                if not chunk:
                    raise EOFError()
                hdr += chunk
            total_len = struct.unpack("<I", hdr[:4])[0]
            resp_bytes = b""
            while len(resp_bytes) < total_len - 16:
                chunk = sock.recv(total_len - 16 - len(resp_bytes))
                if not chunk:
                    raise EOFError()
                resp_bytes += chunk

            resp = plistlib.loads(resp_bytes)
            if keep_open:
                return sock, resp
            sock.close()
            return None, resp
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
            raise

    def _query_lockdown_socket(self, sock: socket.socket, records: list):
        """
        Query iOS lockdownd over an open TCP/usbmuxd socket.
        Returns (level: int | None, charging: bool | None, device_name: str, trusted: bool).
        """
        sock.settimeout(2.2)
        try:
            _send_lockdown_plist(
                sock, {"Label": "AudioBridge", "Request": "GetValue", "Key": "DeviceName"}
            )
            name_resp = _recv_lockdown_plist(sock)
        except Exception:
            return None, None, "", False

        if not isinstance(name_resp, dict) or name_resp.get("Request") != "GetValue":
            return None, None, "", False

        device_name = str(name_resp.get("Value") or "").strip()

        for rec in records:
            try:
                _send_lockdown_plist(
                    sock,
                    {
                        "Label": "AudioBridge",
                        "Request": "StartSession",
                        "HostID": rec["host_id"],
                        "SystemBUID": rec["system_buid"],
                    },
                )
                sess = _recv_lockdown_plist(sock)
            except Exception:
                break

            sid = sess.get("SessionID")
            if not sid:
                continue

            self._preferred_host_id = rec["host_id"]
            active_sock = sock
            try:
                if sess.get("EnableSessionSSL"):
                    ctx = self._get_ssl_context_for_record(
                        rec["host_id"], rec["cert"], rec["key"]
                    )
                    if ctx is None:
                        return None, None, device_name, False
                    active_sock = ctx.wrap_socket(sock)
                    active_sock.settimeout(2.2)

                _send_lockdown_plist(
                    active_sock,
                    {
                        "Label": "AudioBridge",
                        "Request": "GetValue",
                        "Domain": "com.apple.mobile.battery",
                    },
                )
                bat_resp = _recv_lockdown_plist(active_sock)
                bat = bat_resp.get("Value") if isinstance(bat_resp, dict) else None

                if not device_name:
                    _send_lockdown_plist(
                        active_sock,
                        {"Label": "AudioBridge", "Request": "GetValue", "Key": "DeviceName"},
                    )
                    dn_resp = _recv_lockdown_plist(active_sock)
                    device_name = str(dn_resp.get("Value") or "").strip()

                try:
                    _send_lockdown_plist(
                        active_sock,
                        {"Label": "AudioBridge", "Request": "StopSession", "SessionID": sid},
                    )
                    _recv_lockdown_plist(active_sock)
                except Exception:
                    pass

                if isinstance(bat, dict) and "BatteryCurrentCapacity" in bat:
                    level = int(bat["BatteryCurrentCapacity"])
                    charging = bool(
                        bat.get("BatteryIsCharging")
                        or (bat.get("ExternalConnected") and bat.get("FullyCharged"))
                    )
                    if 0 <= level <= 100:
                        return level, charging, device_name or "iPhone", True
            except Exception:
                pass
            finally:
                if active_sock is not sock:
                    try:
                        active_sock.close()
                    except Exception:
                        pass
            break

        return None, None, device_name, False

    def _get_local_ipv4(self) -> str:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except Exception:
            return ""
        finally:
            s.close()

    def _discover_arp_lockdown_ips(self, local_ip: str, exclude_ips: set):
        if not local_ip or "." not in local_ip:
            return []
        prefix = ".".join(local_ip.split(".")[:3]) + "."
        try:
            out = subprocess.check_output(
                ["arp", "-a"],
                text=True,
                errors="ignore",
                timeout=1.5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            return []

        neighbors = []
        for ip in re.findall(r"(\d+\.\d+\.\d+\.\d+)", out):
            if (
                ip.startswith(prefix)
                and not ip.endswith(".255")
                and ip != local_ip
                and ip not in exclude_ips
                and ip not in neighbors
            ):
                neighbors.append(ip)

        if not neighbors:
            return []

        from concurrent.futures import ThreadPoolExecutor

        def _probe(target_ip: str):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.35)
            try:
                if s.connect_ex((target_ip, LOCKDOWN_PORT)) == 0:
                    return target_ip
            except Exception:
                pass
            finally:
                s.close()
            return None

        with ThreadPoolExecutor(max_workers=min(12, len(neighbors))) as pool:
            return [ip for ip in pool.map(_probe, neighbors) if ip]

    def _poll_iphone_lockdown_sync(self):
        """
        Try reading iPhone battery via Apple Lockdown:
        1. USB / Wi-Fi devices reported by local usbmuxd (port 27015)
        2. Direct TCP 62078 on cached IP, client IPs, Hotspot gateway (.1), or ARP subnet
        """
        records = self._load_lockdown_records()
        mux_ips = []
        untrusted_name = ""

        # 1. Check usbmuxd (USB cable or iTunes Wi-Fi sync)
        try:
            _, mux_resp = self._usbmuxd_roundtrip(
                {
                    "MessageType": "ListDevices",
                    "ProgName": "AudioBridge",
                    "ClientVersionString": "1.0",
                },
                keep_open=False,
                timeout=0.4,
            )
            dev_list = mux_resp.get("DeviceList", []) if isinstance(mux_resp, dict) else []
        except Exception:
            dev_list = []

        for entry in dev_list:
            props = entry.get("Properties", {})
            conn_type = props.get("ConnectionType")
            dev_id = entry.get("DeviceID")
            serial = props.get("SerialNumber")

            if serial:
                try:
                    _, pr_resp = self._usbmuxd_roundtrip(
                        {
                            "MessageType": "ReadPairRecord",
                            "ProgName": "AudioBridge",
                            "ClientVersionString": "1.0",
                            "PairRecordID": serial,
                        },
                        keep_open=False,
                        timeout=0.4,
                    )
                    pr_data = pr_resp.get("PairRecordData")
                    if pr_data:
                        rec = plistlib.loads(pr_data)
                        hid = rec.get("HostID")
                        if hid and all(r["host_id"] != hid for r in records):
                            buid = rec.get("SystemBUID") or (
                                records[0]["system_buid"] if records else ""
                            )
                            if buid and rec.get("HostCertificate") and rec.get("HostPrivateKey"):
                                records.insert(
                                    0,
                                    {
                                        "host_id": hid,
                                        "system_buid": buid,
                                        "cert": rec["HostCertificate"],
                                        "key": rec["HostPrivateKey"],
                                    },
                                )
                except Exception:
                    pass

            if conn_type == "USB" and dev_id is not None:
                mux_sock = None
                try:
                    mux_sock, conn_resp = self._usbmuxd_roundtrip(
                        {
                            "MessageType": "Connect",
                            "ProgName": "AudioBridge",
                            "ClientVersionString": "1.0",
                            "DeviceID": dev_id,
                            "PortNumber": socket.htons(LOCKDOWN_PORT),
                        },
                        keep_open=True,
                        timeout=0.8,
                    )
                    if isinstance(conn_resp, dict) and conn_resp.get("Number") == 0:
                        lvl, chg, name, trusted = self._query_lockdown_socket(mux_sock, records)
                        if lvl is not None:
                            return {
                                "level": lvl,
                                "charging": chg,
                                "name": name,
                                "source": "USB 同步",
                                "hint": "Apple Lockdown USB 静默同步中 (精确到 1%)",
                                "trusted": True,
                            }
                        if name and not trusted:
                            untrusted_name = name
                except Exception:
                    pass
                finally:
                    if mux_sock is not None:
                        try:
                            mux_sock.close()
                        except Exception:
                            pass
            elif conn_type == "Network":
                net_addr = props.get("NetworkAddress")
                if isinstance(net_addr, (bytes, bytearray)) and len(net_addr) >= 8:
                    family = struct.unpack("<H", net_addr[:2])[0]
                    if family == socket.AF_INET:
                        ip_str = socket.inet_ntoa(net_addr[4:8])
                        if ip_str not in mux_ips:
                            mux_ips.append(ip_str)

        # 2. Check Hotspot / Wi-Fi LAN TCP 62078
        local_ip = self._get_local_ipv4()
        with self._lock:
            client_ips = list(self._client_ips)
            last_ip = self._last_lockdown_ip

        fast_candidates = []
        for candidate in [last_ip, *client_ips, *mux_ips]:
            if candidate and candidate not in fast_candidates:
                fast_candidates.append(candidate)

        if local_ip and "." in local_ip:
            gw_guess = ".".join(local_ip.split(".")[:3]) + ".1"
            if gw_guess != local_ip and gw_guess not in fast_candidates:
                fast_candidates.append(gw_guess)

        def _try_ip(ip_addr: str):
            nonlocal untrusted_name
            sock = None
            try:
                sock = socket.create_connection((ip_addr, LOCKDOWN_PORT), timeout=0.5)
                lvl, chg, name, trusted = self._query_lockdown_socket(sock, records)
                if lvl is not None:
                    with self._lock:
                        self._last_lockdown_ip = ip_addr
                    return {
                        "level": lvl,
                        "charging": chg,
                        "name": name,
                        "source": "无线同步",
                        "hint": "Apple Lockdown 无线静默同步中 (精确到 1%)",
                        "trusted": True,
                    }
                if name and not trusted:
                    untrusted_name = name
            except Exception:
                pass
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass
            return None

        for ip_addr in fast_candidates:
            res = _try_ip(ip_addr)
            if res is not None:
                return res

        # Only scan ARP neighbors if none of the fast candidates yielded an iPhone
        if not untrusted_name:
            arp_ips = self._discover_arp_lockdown_ips(local_ip, set(fast_candidates))
            for ip_addr in arp_ips:
                res = _try_ip(ip_addr)
                if res is not None:
                    return res

        if untrusted_name:
            return {"level": None, "name": untrusted_name, "trusted": False}
        return None

    async def _poll_iphone_once(self):
        if self._pairing_in_progress:
            return

        # Tier 1: Silent Apple Lockdown over USB / Personal Hotspot / Wi-Fi (1% precision, zero popups)
        ld_res = await asyncio.to_thread(self._poll_iphone_lockdown_sync)
        if ld_res is not None and ld_res.get("level") is not None:
            with self._lock:
                self._pair_hint_override = ("", 0.0)
                self._iphone.update(
                    state="connected",
                    name=ld_res["name"] or self._iphone["name"] or "iPhone",
                    level=ld_res["level"],
                    charging=ld_res["charging"],
                    source=ld_res["source"],
                    updatedAt=time.time(),
                    hint=ld_res["hint"],
                )
            return

        untrusted_lockdown_name = (
            ld_res.get("name", "") if isinstance(ld_res, dict) else ""
        )

        # Tier 2: Windows BLE GATT Battery Service (0x180F / 0x2A19)
        import winrt.windows.devices.bluetooth as bt
        import winrt.windows.devices.bluetooth.genericattributeprofile as gatt
        import winrt.windows.devices.enumeration as enum_mod
        import winrt.windows.storage.streams as streams

        classic_phone_name = await self._find_classic_paired_phone_name(bt, enum_mod)
        best_known_name = untrusted_lockdown_name or classic_phone_name
        found_ble_paired_name = ""

        # Check paired BLE devices (handles iOS RPA rotation automatically via IRK)
        le_selector = bt.BluetoothLEDevice.get_device_selector_from_pairing_state(True)
        le_devices = await enum_mod.DeviceInformation.find_all_async_aqs_filter(le_selector)

        for info in le_devices:
            if info.id in self._ignored_ble_ids:
                continue

            ble_dev = None
            try:
                ble_dev = await asyncio.wait_for(
                    bt.BluetoothLEDevice.from_id_async(info.id), timeout=3.0
                )
                if ble_dev is None:
                    continue

                is_iphone = await self._is_apple_iphone_ble(
                    info.id, info.name, ble_dev, bt, gatt, streams
                )
                if not is_iphone:
                    continue

                display_name = self._pick_display_name(
                    info.name or ble_dev.name, best_known_name
                )
                found_ble_paired_name = display_name

                level, charging = await self._read_battery_from_ble_device(
                    ble_dev, bt, gatt, streams
                )
                if level is not None:
                    with self._lock:
                        self._pair_hint_override = ("", 0.0)
                        self._iphone.update(
                            state="connected",
                            name=display_name,
                            level=level,
                            charging=charging,
                            source="蓝牙 BLE",
                            updatedAt=time.time(),
                            hint="蓝牙低功耗自动静默同步中",
                        )
                    return
            except Exception:
                pass
            finally:
                if ble_dev is not None:
                    try:
                        ble_dev.close()
                    except Exception:
                        pass

        # Update diagnostic state if neither Lockdown nor BLE yielded a reading
        now = time.time()
        with self._lock:
            if self._pairing_in_progress:
                return

            # Don't overwrite a recent HTTP Shortcut push (< 5 min old)
            if (
                self._iphone["state"] == "shortcut"
                and self._iphone["updatedAt"]
                and (now - self._iphone["updatedAt"]) < 300
            ):
                return

            override_msg, override_until = self._pair_hint_override
            has_nearby_ble = any((now - ts) < 45.0 for _, ts in self._nearby_iphone_ble.values())

            if found_ble_paired_name:
                self._iphone["state"] = "disconnected"
                self._iphone["name"] = found_ble_paired_name
                self._iphone["hint"] = "BLE 已配对，等待手机靠近或亮屏唤醒"
            elif best_known_name or has_nearby_ble:
                self._iphone["state"] = "needs_ble_pair"
                if best_known_name:
                    self._iphone["name"] = best_known_name
                if override_msg and now < override_until:
                    self._iphone["hint"] = override_msg
                elif untrusted_lockdown_name:
                    self._iphone["hint"] = (
                        "用数据线插一次电脑并在手机点「信任」，之后热点/Wi-Fi 即可永久静默显示电量"
                    )
                elif classic_phone_name:
                    self._iphone["hint"] = (
                        "经典蓝牙已连：可插一次 USB 点「信任」开启无线同步，或先删旧蓝牙记录再点「一键授权」"
                    )
                else:
                    self._iphone["hint"] = (
                        "已发现附近 iPhone，请点右侧「一键授权」并在手机弹窗点“配对”"
                    )
            else:
                self._iphone["state"] = "unpaired"
                self._iphone["hint"] = (
                    override_msg
                    if (override_msg and now < override_until)
                    else "连接热点/Wi-Fi（曾 USB 信任过本机）或点「一键授权」即可显示电量"
                )

    async def _pair_nearby_iphone_ble(self):
        if self._pairing_in_progress:
            return

        # First check if Apple Lockdown already works right now without needing BLE pairing!
        ld_res = await asyncio.to_thread(self._poll_iphone_lockdown_sync)
        if ld_res is not None and ld_res.get("level") is not None:
            with self._lock:
                self._pair_hint_override = ("", 0.0)
                self._iphone.update(
                    state="connected",
                    name=ld_res["name"] or self._iphone["name"] or "iPhone",
                    level=ld_res["level"],
                    charging=ld_res["charging"],
                    source=ld_res["source"],
                    updatedAt=time.time(),
                    hint=ld_res["hint"],
                )
            return

        self._pairing_in_progress = True
        import winrt.windows.devices.bluetooth as bt
        import winrt.windows.devices.bluetooth.genericattributeprofile as gatt
        import winrt.windows.devices.enumeration as enum_mod
        import winrt.windows.storage.streams as streams

        with self._lock:
            self._iphone["state"] = "pairing"
            self._iphone["hint"] = "正在呼叫 iPhone，请保持手机亮屏并在弹窗中点击「配对」..."

        try:
            classic_phone_name = await self._find_classic_paired_phone_name(bt, enum_mod)

            for _ in range(20):
                now = time.time()
                with self._lock:
                    candidates = [
                        (mac, rssi)
                        for mac, (rssi, ts) in self._nearby_iphone_ble.items()
                        if (now - ts) < 45.0
                    ]
                if candidates:
                    break
                await asyncio.sleep(0.15)

            candidates.sort(key=lambda item: -item[1])
            if not candidates:
                msg = "未搜到附近亮屏的 iPhone BLE 广播，请解锁手机屏幕后再点「一键授权」"
                with self._lock:
                    self._pair_hint_override = (msg, time.time() + 30.0)
                    self._iphone["state"] = "needs_ble_pair"
                    self._iphone["hint"] = msg
                return

            paired_ok = False
            for mac, _rssi in candidates:
                addr = int(mac.replace(":", "").replace("-", ""), 16)

                # Step 1: Verify candidate is an iPhone, then close that unencrypted GATT handle
                probe_dev = None
                is_iphone = False
                try:
                    probe_dev = await asyncio.wait_for(
                        bt.BluetoothLEDevice.from_bluetooth_address_async(addr),
                        timeout=4.0,
                    )
                    if probe_dev is None:
                        continue
                    ancs = await asyncio.wait_for(
                        probe_dev.get_gatt_services_for_uuid_async(
                            APPLE_ANCS_UUID, bt.BluetoothCacheMode.UNCACHED
                        ),
                        timeout=3.5,
                    )
                    if ancs.status == gatt.GattCommunicationStatus.SUCCESS and ancs.services:
                        is_iphone = True
                        for s in ancs.services:
                            try:
                                s.close()
                            except Exception:
                                pass
                    else:
                        dis = await asyncio.wait_for(
                            probe_dev.get_gatt_services_for_uuid_async(
                                DEVICE_INFO_SERVICE_UUID, bt.BluetoothCacheMode.UNCACHED
                            ),
                            timeout=3.5,
                        )
                        if dis.status == gatt.GattCommunicationStatus.SUCCESS and dis.services:
                            svc0 = dis.services[0]
                            model = await self._read_gatt_string(
                                svc0, MODEL_NUMBER_UUID, bt, gatt, streams
                            )
                            try:
                                svc0.close()
                            except Exception:
                                pass
                            if "iphone" in model.lower():
                                is_iphone = True
                except Exception:
                    pass
                finally:
                    if probe_dev is not None:
                        try:
                            probe_dev.close()
                        except Exception:
                            pass

                if not is_iphone:
                    continue

                await asyncio.sleep(0.25)

                # Step 2: Open fresh BLE handle and request authenticated SMP pairing
                ble_dev = None
                try:
                    ble_dev = await asyncio.wait_for(
                        bt.BluetoothLEDevice.from_bluetooth_address_async(addr),
                        timeout=4.0,
                    )
                    if ble_dev is None:
                        continue

                    pairing = ble_dev.device_information.pairing
                    if not pairing.is_paired:
                        custom = pairing.custom
                        kinds = (
                            enum_mod.DevicePairingKinds.CONFIRM_ONLY
                            | enum_mod.DevicePairingKinds.CONFIRM_PIN_MATCH
                            | enum_mod.DevicePairingKinds.DISPLAY_PIN
                        )

                        def _on_pairing_requested(_sender, args):
                            try:
                                args.accept()
                            except Exception:
                                pass

                        token = custom.add_pairing_requested(_on_pairing_requested)
                        try:
                            pair_res = await asyncio.wait_for(
                                custom.pair_with_protection_level_async(
                                    kinds,
                                    enum_mod.DevicePairingProtectionLevel.ENCRYPTION_AND_AUTHENTICATION,
                                ),
                                timeout=25.0,
                            )
                        finally:
                            try:
                                custom.remove_pairing_requested(token)
                            except Exception:
                                pass

                        status_code = int(pair_res.status)
                        if status_code not in (0, 3):
                            if status_code == 19 and classic_phone_name:
                                msg = (
                                    f"Windows 已有「{classic_phone_name}」经典蓝牙配对记录，系统拦截了重复 BLE 弹窗；"
                                    "请先在电脑和手机蓝牙里忽略对方再点授权，或插一次数据线点「信任」开启无线同步"
                                )
                            else:
                                msg = f"BLE 配对未完成 (状态码 {status_code})，请保持手机亮屏再试，或插一次数据线点「信任」"
                            with self._lock:
                                self._pair_hint_override = (msg, time.time() + 40.0)
                                self._iphone["state"] = "needs_ble_pair"
                                self._iphone["hint"] = msg
                            return

                    dev_id = (
                        ble_dev.device_device_id
                        if hasattr(ble_dev, "device_device_id")
                        else ble_dev.device_id
                    )
                    self._verified_iphone_ids.add(dev_id)
                    await asyncio.sleep(0.5)
                    level, charging = await self._read_battery_from_ble_device(
                        ble_dev, bt, gatt, streams
                    )
                    display_name = self._pick_display_name(ble_dev.name, classic_phone_name)

                    if level is not None:
                        with self._lock:
                            self._pair_hint_override = ("", 0.0)
                            self._iphone.update(
                                state="connected",
                                name=display_name,
                                level=level,
                                charging=charging,
                                source="蓝牙 BLE",
                                updatedAt=time.time(),
                                hint="蓝牙低功耗自动静默同步中",
                            )
                        paired_ok = True
                        break

                    paired_ok = True
                    break
                except asyncio.TimeoutError:
                    msg = "等待手机确认超时，请保持手机亮屏并在弹窗点击「配对」"
                    with self._lock:
                        self._pair_hint_override = (msg, time.time() + 30.0)
                        self._iphone["state"] = "needs_ble_pair"
                        self._iphone["hint"] = msg
                    return
                except Exception as e:
                    print("BLE pair attempt warning:", e)
                finally:
                    if ble_dev is not None:
                        try:
                            ble_dev.close()
                        except Exception:
                            pass

            if not paired_ok:
                msg = "未能完成 BLE 授权，建议插一次数据线点「信任」自动无线同步"
                with self._lock:
                    self._pair_hint_override = (msg, time.time() + 30.0)
                    self._iphone["state"] = "needs_ble_pair"
                    self._iphone["hint"] = msg
        finally:
            self._pairing_in_progress = False
            self.trigger_refresh()

    async def _iphone_poll_loop(self):
        self._refresh_event = asyncio.Event()

        while not self._stop_event:
            try:
                await self._poll_iphone_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print("iPhone battery poll warning:", e)

            try:
                await asyncio.wait_for(self._refresh_event.wait(), timeout=IPHONE_POLL_INTERVAL)
                self._refresh_event.clear()
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break

    async def run(self):
        self._loop = asyncio.get_running_loop()
        self._stop_event = False
        self._stop_async_event = asyncio.Event()
        scanner_task = asyncio.create_task(self._scanner_loop())
        iphone_task = asyncio.create_task(self._iphone_poll_loop())
        try:
            await asyncio.gather(scanner_task, iphone_task)
        except asyncio.CancelledError:
            pass
        finally:
            self._stop_event = True
            scanner_task.cancel()
            iphone_task.cancel()

    def start_background(self):
        """Run the BLE event loop on a dedicated clean MTA thread (unpolluted by PyAudio/GUI STA)."""
        if self._thread is not None and self._thread.is_alive():
            return

        def _worker():
            try:
                asyncio.run(self.run())
            except Exception as e:
                print("Battery monitor thread error:", e)

        self._thread = threading.Thread(target=_worker, name="ble-battery-monitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event = True
        if self._loop is not None:
            try:
                if self._stop_async_event is not None:
                    self._loop.call_soon_threadsafe(self._stop_async_event.set)
                if self._refresh_event is not None:
                    self._loop.call_soon_threadsafe(self._refresh_event.set)
            except RuntimeError:
                pass
        if self._thread is not None and self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=1.5)

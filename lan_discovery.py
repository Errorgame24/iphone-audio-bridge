import asyncio
import ipaddress
import json
import socket


DISCOVER_REQUEST_PREFIX = b"AUDIOBRIDGE_DISCOVER"
DISCOVER_RESPONSE_MAGIC = "AUDIOBRIDGE_HERE_V1"
BONJOUR_SERVICE_TYPE = "_audiobridge._tcp.local."


def get_all_lan_ipv4s(primary_ip: str = "") -> list[str]:
    """Collect active private/LAN IPv4 addresses on this machine, prioritizing primary_ip."""
    ordered: list[str] = []

    def _add(candidate: str):
        if not candidate:
            return
        try:
            addr = ipaddress.ip_address(candidate)
        except ValueError:
            return
        if (
            addr.version != 4
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_unspecified
        ):
            return
        text = str(addr)
        if text not in ordered:
            ordered.append(text)

    _add(primary_ip)

    try:
        import ifaddr

        for adapter in ifaddr.get_adapters():
            nice = (adapter.nice_name or "").lower()
            # Deprioritize virtual Hyper-V / WSL / Docker / VPN NAT bridges behind real NICs
            if any(tag in nice for tag in ("wsl", "hyper-v", "vethernet", "docker", "loopback")):
                continue
            for ip_obj in adapter.ips:
                if isinstance(ip_obj.ip, str):
                    _add(ip_obj.ip)
    except Exception:
        pass

    try:
        _, _, host_ips = socket.gethostbyname_ex(socket.gethostname())
        for candidate in host_ips:
            _add(candidate)
    except Exception:
        pass

    return ordered


class _UdpDiscoveryProtocol(asyncio.DatagramProtocol):
    """Lightweight UDP discovery responder sharing port 8443 for networks that filter mDNS."""

    def __init__(self, build_payload_fn):
        self._build_payload_fn = build_payload_fn
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        if not data or not data.startswith(DISCOVER_REQUEST_PREFIX):
            return
        if self.transport is None:
            return
        try:
            payload = self._build_payload_fn()
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.transport.sendto(raw, addr)
        except Exception:
            pass


class LanDiscoveryAdvertiser:
    """
    Advertises Audio Bridge on the local network via:
      1. Bonjour / mDNS (_audiobridge._tcp.local.) for instant zero-config iOS NWBrowser discovery.
      2. UDP port 8443 unicast/broadcast responder as fallback when Wi-Fi APs block multicast 5353.
    """

    def __init__(self, port: int = 8443, hostname: str | None = None):
        self.port = int(port)
        self.hostname = (hostname or socket.gethostname() or "Windows-PC").strip()
        self._primary_ip = "127.0.0.1"
        self._device_name_getter = lambda: ""
        self._aiozc = None
        self._service_info = None
        self._udp_transport = None

    def build_discovery_payload(self) -> dict:
        ips = get_all_lan_ipv4s(self._primary_ip)
        primary = ips[0] if ips else self._primary_ip
        return {
            "magic": DISCOVER_RESPONSE_MAGIC,
            "hostname": self.hostname,
            "ip": primary,
            "ips": ips,
            "port": self.port,
            "deviceName": str(self._device_name_getter() or ""),
            "version": "1.1",
        }

    async def start(self, primary_ip: str, device_name_getter=None):
        self._primary_ip = primary_ip or "127.0.0.1"
        if device_name_getter is not None:
            self._device_name_getter = device_name_getter

        await self._start_udp_responder()
        await self._register_mdns()

    async def update_ip(self, primary_ip: str):
        if not primary_ip or primary_ip == self._primary_ip:
            return
        self._primary_ip = primary_ip
        await self._unregister_mdns()
        await self._register_mdns()

    async def stop(self):
        if self._udp_transport is not None:
            try:
                self._udp_transport.close()
            except Exception:
                pass
            self._udp_transport = None
        await self._unregister_mdns()

    async def _start_udp_responder(self):
        if self._udp_transport is not None:
            return
        try:
            loop = asyncio.get_running_loop()
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _UdpDiscoveryProtocol(self.build_discovery_payload),
                local_addr=("0.0.0.0", self.port),
            )
            self._udp_transport = transport
        except Exception as e:
            print(f"! UDP discovery responder unavailable on port {self.port}: {e}")

    async def _register_mdns(self):
        ips = get_all_lan_ipv4s(self._primary_ip)
        if not ips:
            return
        try:
            from zeroconf import IPVersion, ServiceInfo
            from zeroconf.asyncio import AsyncZeroconf

            safe_label = "".join(
                ch if (ch.isalnum() or ch in ("-", "_")) else "-"
                for ch in self.hostname
            ).strip("-") or "Windows-PC"

            properties = {
                "hostname": self.hostname,
                "ip": ips[0],
                "ips": ",".join(ips[:6]),
                "port": str(self.port),
                "version": "1.1",
            }
            info = ServiceInfo(
                type_=BONJOUR_SERVICE_TYPE,
                name=f"{safe_label}.{BONJOUR_SERVICE_TYPE}",
                addresses=[socket.inet_aton(ip) for ip in ips[:6]],
                port=self.port,
                properties=properties,
                server=f"{safe_label}.local.",
            )
            aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)
            await aiozc.async_register_service(info, allow_name_change=True)
            self._aiozc = aiozc
            self._service_info = info
            print(f"✓ LAN discovery active: {self.hostname} ({', '.join(ips)}) : {self.port}")
        except Exception as e:
            print(f"! Bonjour/mDNS advertisement skipped: {e}")

    async def _unregister_mdns(self):
        aiozc = self._aiozc
        info = self._service_info
        self._aiozc = None
        self._service_info = None
        if aiozc is not None:
            try:
                if info is not None:
                    await aiozc.async_unregister_service(info)
            except Exception:
                pass
            try:
                await aiozc.async_close()
            except Exception:
                pass

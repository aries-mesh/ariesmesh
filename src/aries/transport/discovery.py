"""mDNS service advertisement and peer discovery using zeroconf.

Spec reference: §9.

zeroconf is an optional dependency: the package isn't reliably installable
on Termux/Android. When it's missing, ``DiscoveryService.start()`` logs a
warning and returns; the daemon still works — peers must be added manually
via ``aries connect <ip:port>``.
"""
from __future__ import annotations

import asyncio
import logging
import socket
from typing import Callable, Optional

try:
    from zeroconf import IPVersion, ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
    ZEROCONF_AVAILABLE = True
except ImportError:  # pragma: no cover — Android/Termux only
    IPVersion = None  # type: ignore[assignment]
    ServiceStateChange = None  # type: ignore[assignment]
    AsyncServiceBrowser = None  # type: ignore[assignment]
    AsyncServiceInfo = None  # type: ignore[assignment]
    AsyncZeroconf = None  # type: ignore[assignment]
    ZEROCONF_AVAILABLE = False

from .peer import PeerInfo

logger = logging.getLogger(__name__)


SERVICE_TYPE = "_aries._tcp.local."


PeerCallback = Callable[[PeerInfo], None]


class DiscoveryService:
    def __init__(
        self,
        device_did: str,
        device_name: str,
        household_tag: str,
        port: int,
        capabilities: Optional[list[str]] = None,
    ) -> None:
        self.device_did = device_did
        self.device_name = device_name
        self.household_tag = household_tag
        self.port = port
        self.capabilities = list(capabilities or [])

        self._zc: Optional[AsyncZeroconf] = None
        self._browser: Optional[AsyncServiceBrowser] = None
        self._service_name: Optional[str] = None
        self.peers: dict[str, PeerInfo] = {}
        self._on_peer_found: Optional[PeerCallback] = None
        self._on_peer_lost: Optional[PeerCallback] = None

    def on_peer_found(self, cb: PeerCallback) -> None:
        self._on_peer_found = cb

    def on_peer_lost(self, cb: PeerCallback) -> None:
        self._on_peer_lost = cb

    async def start(self) -> None:
        if not ZEROCONF_AVAILABLE:
            logger.warning(
                "zeroconf not available — mDNS discovery disabled. "
                "Use `aries connect <ip:port>` to add peers manually, "
                "or install with `pip install aries-mesh[full]`."
            )
            return
        from zeroconf import ServiceInfo  # local to keep cold-import cheap

        self._zc = AsyncZeroconf(ip_version=IPVersion.V4Only)
        local_ip = self._get_local_ip()

        self._service_name = f"aries-{self.device_name}._aries._tcp.local."
        info = ServiceInfo(
            type_=SERVICE_TYPE,
            name=self._service_name,
            addresses=[socket.inet_aton(local_ip)],
            port=self.port,
            properties={
                "did": self.device_did,
                "household": self.household_tag,
                "proto": "v0.1",
                "cap": ",".join(self.capabilities),
                "name": self.device_name,
            },
            server=socket.gethostname() + ".local.",
        )
        await self._zc.async_register_service(info)

        self._browser = AsyncServiceBrowser(
            self._zc.zeroconf,
            SERVICE_TYPE,
            handlers=[self._on_service_state_change],
        )

    def _on_service_state_change(
        self,
        zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        if state_change is ServiceStateChange.Added:
            asyncio.ensure_future(self._resolve_service(service_type, name))
        elif state_change is ServiceStateChange.Removed:
            self._handle_removal(name)

    def _handle_removal(self, name: str) -> None:
        lost: list[str] = []
        for did, peer in self.peers.items():
            if peer.name == name:
                lost.append(did)
        for did in lost:
            removed = self.peers.pop(did, None)
            if removed and self._on_peer_lost:
                try:
                    self._on_peer_lost(removed)
                except Exception:
                    pass

    async def _resolve_service(self, service_type: str, name: str) -> None:
        if self._zc is None:
            return
        info = AsyncServiceInfo(service_type, name)
        ok = await info.async_request(self._zc.zeroconf, 3000)
        if not ok:
            return

        props = info.decoded_properties or {}
        peer_did = props.get("did")
        peer_household = props.get("household")
        if not peer_did or not peer_household:
            return
        if peer_household != self.household_tag:
            return
        if peer_did == self.device_did:
            return

        addresses = info.parsed_scoped_addresses() or info.parsed_addresses()
        if not addresses:
            return
        host = addresses[0]
        port = info.port or 0
        if port == 0:
            return

        peer = PeerInfo(
            device_did=peer_did,
            name=props.get("name", name),
            host=host,
            port=port,
            household_tag=peer_household,
            capabilities=[c for c in (props.get("cap") or "").split(",") if c],
        )
        self.peers[peer_did] = peer
        if self._on_peer_found:
            try:
                self._on_peer_found(peer)
            except Exception:
                pass

    @staticmethod
    def _get_local_ip() -> str:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        except Exception:
            ip = "127.0.0.1"
        finally:
            s.close()
        return ip

    async def stop(self) -> None:
        if not ZEROCONF_AVAILABLE:
            return
        if self._browser is not None:
            await self._browser.async_cancel()
        if self._zc is not None:
            await self._zc.async_unregister_all_services()
            await self._zc.async_close()

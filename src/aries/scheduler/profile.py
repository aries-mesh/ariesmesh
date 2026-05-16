"""Hardware monitoring via psutil; periodic DeviceHealth snapshots.

Spec reference: §11.
"""
from __future__ import annotations

import asyncio
import platform
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Callable, Optional

import psutil

from .router import DeviceHealth


PROFILE_INTERVAL_S = 10
HealthCallback = Callable[[DeviceHealth], None]


class DeviceProfiler:
    def __init__(self, device_did: str) -> None:
        self.device_did = device_did
        self._running = False
        self._task: Optional[asyncio.Task[None]] = None
        self._latest: Optional[DeviceHealth] = None
        self._on_update: list[HealthCallback] = []

    @property
    def latest(self) -> Optional[DeviceHealth]:
        return self._latest

    def on_update(self, cb: HealthCallback) -> None:
        self._on_update.append(cb)

    def snapshot(self) -> DeviceHealth:
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()

        battery_pct: Optional[float] = None
        charging = True
        try:
            bat = psutil.sensors_battery()
            if bat is not None:
                battery_pct = float(bat.percent)
                charging = bool(bat.power_plugged)
        except (AttributeError, NotImplementedError):
            battery_pct = None

        thermal = "nominal"
        try:
            temps = psutil.sensors_temperatures() or {}
            max_temp = 0.0
            for entries in temps.values():
                for entry in entries:
                    if entry.current is not None and entry.current > max_temp:
                        max_temp = entry.current
            if max_temp > 90:
                thermal = "throttled"
            elif max_temp > 75:
                thermal = "warm"
        except (AttributeError, NotImplementedError, OSError):
            pass

        network_type = "wifi"
        bandwidth_mbps = 0.0
        try:
            stats = psutil.net_if_stats()
            for name, info in stats.items():
                lname = name.lower()
                if "eth" in lname:
                    network_type = "ethernet"
                elif "wi-fi" in lname or "wlan" in lname or "wifi" in lname:
                    if network_type != "ethernet":
                        network_type = "wifi"
                if info.speed and info.speed > bandwidth_mbps:
                    bandwidth_mbps = float(info.speed)
        except (AttributeError, NotImplementedError):
            pass

        vram_available_gb = 0.0
        if platform.system() == "Darwin":
            # unified memory on Apple Silicon
            vram_available_gb = mem.available / (1024 ** 3)

        return DeviceHealth(
            device_did=self.device_did,
            cpu_percent=cpu,
            ram_available_gb=mem.available / (1024 ** 3),
            ram_total_gb=mem.total / (1024 ** 3),
            gpu_utilization=0.0,
            vram_available_gb=vram_available_gb,
            battery_pct=battery_pct,
            charging=charging,
            thermal=thermal,
            network_type=network_type,
            bandwidth_mbps=bandwidth_mbps,
            last_updated=time.time(),
        )

    def static_info(self) -> dict[str, object]:
        try:
            disk = shutil.disk_usage(Path("/").as_posix() if platform.system() != "Windows" else "C:\\")
        except Exception:
            disk = None
        info: dict[str, object] = {
            "platform": platform.system().lower(),
            "arch": platform.machine(),
            "cpu_cores": psutil.cpu_count(logical=False) or 0,
            "cpu_threads": psutil.cpu_count(logical=True) or 0,
            "ram_total_gb": psutil.virtual_memory().total / (1024 ** 3),
            "hostname": socket.gethostname(),
            "python": sys.version.split()[0],
        }
        if disk:
            info["disk_total_gb"] = disk.total / (1024 ** 3)
            info["disk_free_gb"] = disk.free / (1024 ** 3)
        return info

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._latest = self.snapshot()
        for cb in self._on_update:
            try:
                cb(self._latest)
            except Exception:
                pass
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(PROFILE_INTERVAL_S)
                if not self._running:
                    break
                snap = self.snapshot()
                self._latest = snap
                for cb in self._on_update:
                    try:
                        cb(snap)
                    except Exception:
                        pass
        except asyncio.CancelledError:
            pass



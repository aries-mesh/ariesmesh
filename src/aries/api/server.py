"""Read-only HTTP API + SSE event stream that powers the web dashboard.

Designed to bind to 127.0.0.1 only — there is no auth layer, because there is
no exposure beyond loopback. If the chosen port is busy we log a warning and
the rest of the daemon keeps running; the dashboard is non-essential.

Endpoints
---------
GET  /api/status        node identity + uptime
GET  /api/health        DeviceProfiler snapshot
GET  /api/peers         connected peers
GET  /api/agents        registered agents
GET  /api/memory        namespace key counts + Lamport clock
GET  /api/inference     feasible inference configs + active session
GET  /api/tasks         most recent receipts (latest 20)
GET  /api/events        Server-Sent Events stream
GET  /, /<path>         React frontend (falls back to fallback HTML)
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover — minimal install only
    web = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

from ..identity.did import did_short

if TYPE_CHECKING:
    from ..node import AriesNode

logger = logging.getLogger(__name__)

DEFAULT_PORT = 7272
DEFAULT_HOST = "127.0.0.1"


def _get_dashboard_dir() -> Path:
    """Locate the bundled dashboard ``dist/`` directory.

    The dashboard build output is shipped inside the ``aries.dashboard``
    sub-package (Phase 5 Addendum) so end users never have to run ``npm``.
    This helper resolves the right location across three environments:

      * **editable install / source checkout** — relative to this file
        (``src/aries/api/server.py`` → ``src/aries/dashboard/dist``).
      * **wheel install** — same package-relative path; setuptools bundled
        ``dist/**/*`` as package data.
      * **PyInstaller binary** — the bundle is unpacked at ``sys._MEIPASS``;
        we look under ``aries/dashboard/dist`` inside that temp tree.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "aries" / "dashboard" / "dist"
    # src/aries/api/server.py → src/aries/dashboard/dist
    return Path(__file__).resolve().parent.parent / "dashboard" / "dist"


_DASHBOARD_DIST = _get_dashboard_dir()


class DashboardAPI:
    """aiohttp-based read-only API for the Aries web dashboard."""

    def __init__(
        self,
        node: "AriesNode",
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
    ) -> None:
        self.node = node
        self.host = host
        self.port = port
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        # One queue per connected SSE client. Events are pushed in fan-out
        # via `push_event`; the per-client receive loop drains its own queue.
        self._sse_clients: list[asyncio.Queue[dict[str, Any]]] = []
        self._started = False

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> bool:
        """Bring up the aiohttp server. Returns True on success, False if the
        port is unavailable or aiohttp isn't installed (caller can choose to
        retry or skip)."""
        if not AIOHTTP_AVAILABLE:
            logger.warning(
                "aiohttp not available — web dashboard disabled. "
                "Install with `pip install aries-mesh[full]`."
            )
            return False
        app = web.Application()

        app.router.add_get("/api/status", self._handle_status)
        app.router.add_get("/api/health", self._handle_health)
        app.router.add_get("/api/peers", self._handle_peers)
        app.router.add_get("/api/agents", self._handle_agents)
        app.router.add_get("/api/memory", self._handle_memory)
        app.router.add_get("/api/inference", self._handle_inference)
        app.router.add_get("/api/tasks", self._handle_tasks)
        app.router.add_get("/api/events", self._handle_events)

        # Static frontend (React build) — falls back to a placeholder page
        # if the build hasn't been run yet. We require index.html specifically
        # so an empty `dist/` directory (e.g. a half-finished build) doesn't
        # silently swallow the no-frontend message.
        if _DASHBOARD_DIST.exists() and (_DASHBOARD_DIST / "index.html").exists():
            app.router.add_get("/", self._serve_frontend_root)
            app.router.add_get("/{path:.*}", self._serve_frontend)
        else:
            app.router.add_get("/", self._handle_no_frontend)

        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            site = web.TCPSite(runner, self.host, self.port)
            await site.start()
        except OSError as exc:
            logger.warning(
                "Dashboard API port %s unavailable (%s); skipping dashboard server. "
                "Pass --api-port to choose another port.",
                self.port,
                exc,
            )
            await runner.cleanup()
            return False

        # If port was 0 (test mode), record the OS-assigned port.
        for server in site._server.sockets if site._server else []:  # type: ignore[attr-defined]
            try:
                self.port = server.getsockname()[1]
                break
            except (OSError, IndexError):
                pass

        self._app = app
        self._runner = runner
        self._site = site
        self._started = True
        return True

    async def stop(self) -> None:
        if not self._started:
            return
        # Disconnect all SSE clients so their loops break promptly.
        for q in list(self._sse_clients):
            try:
                q.put_nowait({"_close": True})
            except asyncio.QueueFull:
                pass
        self._sse_clients.clear()
        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:
                pass
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
        self._started = False

    # ------------------------------------------------------------------ events

    def push_event(self, event_type: str, description: str) -> None:
        """Fan out an event to all connected SSE clients."""
        payload = {
            "type": event_type,
            "description": description,
            "timestamp": time.time(),
        }
        for q in list(self._sse_clients):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # Client is too slow — drop the event for that client.
                pass

    # ------------------------------------------------------------------ handlers

    async def _handle_status(self, request: web.Request) -> web.Response:
        node = self.node
        household = getattr(node, "household", None)
        device_did = household.device_did if household else ""
        household_did = household.user_root_did if household else ""
        transport = getattr(node, "transport", None)
        port = transport.port if transport is not None else 0
        start = getattr(node, "_start_time", 0.0) or 0.0
        uptime = max(time.time() - start, 0.0) if start else 0.0

        device_name = ""
        platform = ""
        if household is not None:
            rec = household.devices.get(device_did or "")
            if rec is not None:
                device_name = rec.name
                platform = getattr(rec, "platform", "") or ""

        return _json({
            "device_did": device_did or "",
            "device_did_short": did_short(device_did) if device_did else "",
            "device_name": device_name or "aries-node",
            "platform": platform,
            "port": port,
            "uptime_seconds": round(uptime, 2),
            "protocol_version": "v0.2",
            "household_did": household_did or "",
            "household_did_short": did_short(household_did) if household_did else "",
            "encrypted_transport": True,
        })

    async def _handle_health(self, request: web.Request) -> web.Response:
        profiler = getattr(self.node, "profiler", None)
        snap = profiler.latest if profiler is not None else None
        if snap is None:
            return _json({
                "cpu_percent": 0.0,
                "ram_available_gb": 0.0,
                "ram_total_gb": 0.0,
                "gpu_utilization": 0.0,
                "vram_available_gb": 0.0,
                "battery_pct": None,
                "charging": True,
                "thermal": "unknown",
                "network_type": "unknown",
                "bandwidth_mbps": 0.0,
                "health_score": 0.0,
            })
        return _json({
            "cpu_percent": snap.cpu_percent,
            "ram_available_gb": snap.ram_available_gb,
            "ram_total_gb": snap.ram_total_gb,
            "gpu_utilization": snap.gpu_utilization,
            "vram_available_gb": snap.vram_available_gb,
            "battery_pct": snap.battery_pct,
            "charging": snap.charging,
            "thermal": snap.thermal,
            "network_type": snap.network_type,
            "bandwidth_mbps": snap.bandwidth_mbps,
            "health_score": snap.health_score,
        })

    async def _handle_peers(self, request: web.Request) -> web.Response:
        transport = getattr(self.node, "transport", None)
        household = getattr(self.node, "household", None)
        peers: list[dict[str, Any]] = []
        if transport is not None:
            now = time.time()
            for conn in transport._connections.values():
                p = conn.peer
                # Pull platform from the household manifest if we have one;
                # peers we haven't paired with yet won't have a record.
                platform = ""
                if household is not None:
                    rec = household.devices.get(p.device_did)
                    if rec is not None:
                        platform = getattr(rec, "platform", "") or ""
                peers.append({
                    "device_did": p.device_did,
                    "device_did_short": did_short(p.device_did) if p.device_did else "",
                    "name": p.name or "",
                    "platform": platform,
                    "host": p.host,
                    "port": p.port,
                    "latency_ms": p.latency_ms,
                    "last_seen_seconds_ago": round(now - p.last_seen, 1) if p.last_seen else None,
                    "connected": conn.is_connected,
                })
        return _json({"peers": peers, "total": len(peers)})

    async def _handle_agents(self, request: web.Request) -> web.Response:
        household = getattr(self.node, "household", None)
        records = list(household.agents.values()) if household is not None else []
        agents = [
            {
                "agent_did": a.agent_did,
                "agent_did_short": did_short(a.agent_did) if a.agent_did else "",
                "name": a.name,
                "vendor": a.vendor,
                "model": a.model or "",
                "locality": a.locality,
                "cost_class": a.cost_class,
                "capabilities": list(a.capabilities),
                "context_window": a.context_window,
                "registered_at": a.registered_at,
            }
            for a in records
        ]
        return _json({"agents": agents, "total": len(agents)})

    async def _handle_memory(self, request: web.Request) -> web.Response:
        memory = getattr(self.node, "memory", None)
        if memory is None:
            return _json({
                "total_keys": 0,
                "total_logs": 0,
                "by_namespace": {"context": 0, "memory": 0, "cache": 0},
                "lamport_clock": 0,
                "log_entries": 0,
            })
        keys = list(memory._registers.keys())
        by_ns = {
            "context": sum(1 for k in keys if k.startswith("aries:context://")),
            "memory": sum(1 for k in keys if k.startswith("aries:memory://")),
            "cache": sum(1 for k in keys if k.startswith("aries:cache://")),
        }
        log_entries = sum(len(log) for log in memory._logs.values())
        return _json({
            "total_keys": len(keys),
            "total_logs": len(memory._logs),
            "by_namespace": by_ns,
            "lamport_clock": int(memory.clock),
            "log_entries": log_entries,
        })

    async def _handle_inference(self, request: web.Request) -> web.Response:
        registry = getattr(self.node, "_inference_registry", None)
        if registry is None:
            return _json({
                "configurations": [],
                "active_inference": None,
                "total_configurations": 0,
            })
        configs = registry.get_configs()
        out = []
        for c in configs:
            out.append({
                "config_id": c.config_id,
                "model_name": c.model_name,
                "config_type": c.config_type,
                "devices": [d.device_did for d in c.devices],
                "estimated_tok_s": round(c.estimated_tok_s, 1),
                "privacy_score": round(c.privacy_score, 2),
                "capability_score": round(c.capability_score, 2),
                "cost_score": round(c.cost_score, 2),
                "weighted_score": round(c.weighted_score(), 3),
            })
        out.sort(key=lambda x: x["weighted_score"], reverse=True)

        coordinator = getattr(self.node, "_inference_coordinator", None)
        active = None
        if coordinator is not None and coordinator.is_active:
            active = {
                "config_id": coordinator.config.config_id,
                "model_name": coordinator.config.model_name,
                "config_type": coordinator.config.config_type,
                "workers_ready": coordinator.workers_ready,
            }
        return _json({
            "configurations": out,
            "active_inference": active,
            "total_configurations": len(out),
        })

    async def _handle_tasks(self, request: web.Request) -> web.Response:
        chains = getattr(self.node, "_receipt_chains", {}) or {}
        tasks: list[dict[str, Any]] = []
        for task_id, chain in chains.items():
            receipts = getattr(chain, "receipts", [])
            if not receipts:
                continue
            last = receipts[-1]
            tasks.append({
                "task_id": task_id,
                "action": getattr(last, "action", ""),
                "status": getattr(last, "status", "ok"),
                "model_used": getattr(last, "model_used", "") or "",
                "tokens_used": int(getattr(last, "tokens_used", 0) or 0),
                "latency_ms": float(getattr(last, "latency_ms", 0.0) or 0.0),
                "summary": getattr(last, "summary", ""),
                "agent_did": getattr(last, "agent_did", "") or "",
                "completed_at": float(getattr(last, "timestamp", 0.0) or 0.0),
            })
        tasks.sort(key=lambda t: t["completed_at"], reverse=True)
        return _json({"tasks": tasks[:20], "total": len(tasks)})

    async def _handle_events(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )
        await response.prepare(request)

        # Send a comment line so the client connection-open fires immediately.
        await response.write(b": connected\n\n")

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._sse_clients.append(queue)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # Heartbeat: keeps proxies / browsers from closing the channel.
                    try:
                        await response.write(b": heartbeat\n\n")
                    except (ConnectionResetError, ConnectionAbortedError):
                        break
                    continue
                if event.get("_close"):
                    break
                line = f"data: {json.dumps(event)}\n\n".encode("utf-8")
                try:
                    await response.write(line)
                except (ConnectionResetError, ConnectionAbortedError):
                    break
        finally:
            if queue in self._sse_clients:
                self._sse_clients.remove(queue)
        return response

    # ------------------------------------------------------------------ frontend

    async def _serve_frontend_root(self, request: web.Request) -> web.Response:
        index = _DASHBOARD_DIST / "index.html"
        if index.exists():
            return web.FileResponse(index)
        return await self._handle_no_frontend(request)

    async def _serve_frontend(self, request: web.Request) -> web.Response:
        path = request.match_info.get("path", "").lstrip("/")
        if path.startswith("api/"):
            return web.Response(status=404)
        candidate = (_DASHBOARD_DIST / path).resolve()
        # Path-traversal guard
        try:
            candidate.relative_to(_DASHBOARD_DIST.resolve())
        except ValueError:
            return web.Response(status=403)
        if candidate.is_file():
            return web.FileResponse(candidate)
        # SPA fallback: any non-asset path returns index.html for client routing.
        index = _DASHBOARD_DIST / "index.html"
        if index.exists():
            return web.FileResponse(index)
        return await self._handle_no_frontend(request)

    async def _handle_no_frontend(self, request: web.Request) -> web.Response:
        html = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Aries Mesh — Dashboard</title>
  <style>
    body { background: #0F1923; color: #C8D6E5; font-family: -apple-system, system-ui, sans-serif;
           margin: 0; padding: 4rem 2rem; max-width: 720px; margin: 0 auto; line-height: 1.6; }
    h1 { color: #00D4AA; font-weight: 600; letter-spacing: -0.02em; }
    code { background: #162235; padding: 0.2em 0.4em; border-radius: 4px; color: #00D4AA; }
    pre { background: #162235; padding: 1rem; border-radius: 8px; border: 1px solid #1E3450; overflow-x: auto; }
    a { color: #00D4AA; }
    .api-list li { font-family: monospace; padding: 0.25rem 0; }
    .dim { color: #6B7F99; }
  </style>
</head>
<body>
  <h1>Aries Mesh</h1>
  <p>The web dashboard hasn't been built yet. To enable the visual UI:</p>
  <pre>cd dashboard
npm install
npm run build</pre>
  <p>Then restart the Aries daemon. The dashboard will be served from this URL.</p>
  <p class="dim">The JSON API is already live without the frontend:</p>
  <ul class="api-list">
    <li><a href="/api/status">/api/status</a></li>
    <li><a href="/api/health">/api/health</a></li>
    <li><a href="/api/peers">/api/peers</a></li>
    <li><a href="/api/agents">/api/agents</a></li>
    <li><a href="/api/memory">/api/memory</a></li>
    <li><a href="/api/inference">/api/inference</a></li>
    <li><a href="/api/tasks">/api/tasks</a></li>
    <li>/api/events <span class="dim">— Server-Sent Events stream</span></li>
  </ul>
</body>
</html>
""".strip()
        return web.Response(text=html, content_type="text/html")


def _json(payload: Any) -> web.Response:
    return web.json_response(payload)

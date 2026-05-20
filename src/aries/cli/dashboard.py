"""Live terminal dashboard for `aries start`.

A Rich Live display arranged into seven panels — node identity, device health,
connected peers, registered agents, memory namespace counts, available
inference configs, and a scrolling activity log. The dashboard reads from the
running ``AriesNode``'s existing state; the node pushes events into the
activity log via ``AriesNode._emit_event``.

Designed to render gracefully when data is missing (no peers yet, no
profiler, no inference registry) — the panels show "(none)" rows instead of
crashing. Falls back to a single-column layout on terminals narrower than
100 columns.
"""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from ..node import AriesNode


REFRESH_S = 2.0
EVENT_BUFFER_SIZE = 20


class TerminalDashboard:
    """Rich Live dashboard shown by `aries start`."""

    def __init__(self, node: "AriesNode") -> None:
        self.node = node
        self._events: list[dict[str, Any]] = []
        self._max_events = EVENT_BUFFER_SIZE
        self._running = False

    # --- public API -----------------------------------------------------

    def add_event(self, event_type: str, description: str) -> None:
        """Append to the ring buffer; oldest entries are evicted at capacity."""
        self._events.append(
            {
                "type": event_type,
                "description": description,
                "timestamp": time.time(),
            }
        )
        if len(self._events) > self._max_events:
            del self._events[0 : len(self._events) - self._max_events]

    async def run(self) -> None:
        """Render the dashboard until cancellation (Ctrl-C)."""
        self._running = True
        console = Console()
        # `screen=False` plays nicest with cmd.exe / legacy Windows terminals;
        # the dashboard still updates in place via Live's transient region.
        with Live(
            self._build_renderable(console),
            refresh_per_second=1,
            screen=False,
            console=console,
        ) as live:
            try:
                while self._running:
                    await asyncio.sleep(REFRESH_S)
                    live.update(self._build_renderable(console))
            except asyncio.CancelledError:
                self._running = False
                raise

    def stop(self) -> None:
        self._running = False

    # --- panel builders -------------------------------------------------

    def _build_node_panel(self) -> Panel:
        household = getattr(self.node, "household", None)
        device_name = ""
        if household is not None:
            dev = household.devices.get(household.device_did or "")
            device_name = dev.name if dev else ""
        did = (household.device_did if household is not None else "") or "(none)"
        short_did = (did[:24] + "...") if len(did) > 24 else did

        uptime = ""
        start = getattr(self.node, "_start_time", 0.0)
        if start:
            secs = int(time.time() - start)
            uptime = f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"

        transport = getattr(self.node, "transport", None)
        port = getattr(transport, "port", "?") if transport is not None else "?"

        body = Text()
        body.append(f"{device_name or 'aries-node'}\n", style="bold cyan")
        body.append(f"{short_did}\n", style="dim")
        body.append(f"Uptime:    {uptime or '(starting)'}\n")
        body.append(f"Port:      {port}\n")
        body.append("Protocol:  v0.2 (Noise XX)")
        return Panel(body, title="Node", border_style="cyan")

    def _build_health_panel(self) -> Panel:
        profiler = getattr(self.node, "profiler", None)
        snap = profiler.latest if profiler is not None else None
        if snap is None:
            return Panel(Text("(profiler not running)", style="dim"), title="Health")

        ram_used = max(snap.ram_total_gb - snap.ram_available_gb, 0.0)
        ram_pct = ram_used / snap.ram_total_gb if snap.ram_total_gb else 0.0
        cpu_bar = ProgressBar(total=100, completed=snap.cpu_percent, width=24)
        ram_bar = ProgressBar(total=100, completed=ram_pct * 100, width=24)

        battery_text = "(plugged)" if snap.battery_pct is None else (
            f"{snap.battery_pct:.0f}%" + (" ⚡ charging" if snap.charging else "")
        )
        thermal_style = (
            "yellow" if snap.thermal in ("warm", "throttled") else "green"
        )

        tbl = Table.grid(padding=(0, 1))
        tbl.add_column(style="bold")
        tbl.add_column()
        tbl.add_row("CPU:", cpu_bar)
        tbl.add_row("", Text(f"{snap.cpu_percent:.0f}%"))
        tbl.add_row("RAM:", ram_bar)
        tbl.add_row("", Text(f"{ram_used:.1f}/{snap.ram_total_gb:.1f} GB"))
        tbl.add_row("Battery:", Text(battery_text))
        tbl.add_row("Thermal:", Text(snap.thermal, style=thermal_style))
        tbl.add_row("Network:", Text(f"{snap.network_type} · {snap.bandwidth_mbps:.0f} MB/s"))
        return Panel(tbl, title="Health", border_style="cyan")

    def _build_peers_panel(self) -> Panel:
        tbl = Table(show_header=True, header_style="bold cyan", expand=True)
        tbl.add_column("Name")
        tbl.add_column("DID")
        tbl.add_column("Latency", justify="right")
        tbl.add_column("Last seen", justify="right")
        tbl.add_column("Status", justify="right")

        transport = getattr(self.node, "transport", None)
        peers = list(transport._connections.values()) if transport is not None else []
        if not peers:
            tbl.add_row("(none connected)", "", "", "", "")
        else:
            now = time.time()
            for conn in peers:
                p = conn.peer
                last = f"{int(now - p.last_seen)}s ago" if p.last_seen else "—"
                latency = f"{p.latency_ms:.0f} ms" if p.latency_ms is not None else "—"
                status = Text("● online", style="green") if conn.is_connected else Text("● offline", style="red")
                tbl.add_row(p.name or "—", p.device_did[:18] + "..." if p.device_did else "—",
                            latency, last, status)
        return Panel(tbl, title="Peers", border_style="cyan")

    def _build_agents_panel(self) -> Panel:
        tbl = Table(show_header=True, header_style="bold cyan", expand=True)
        tbl.add_column("Name")
        tbl.add_column("Vendor")
        tbl.add_column("Model")
        tbl.add_column("Cost")
        tbl.add_column("Locality")

        household = getattr(self.node, "household", None)
        agents = list(household.agents.values()) if household is not None else []
        if not agents:
            tbl.add_row("(none registered)", "", "", "", "")
        else:
            for a in agents:
                tbl.add_row(a.name, a.vendor, a.model or "—", a.cost_class, a.locality)
        return Panel(tbl, title="Agents", border_style="cyan")

    def _build_memory_panel(self) -> Panel:
        store = getattr(self.node, "memory", None)
        body = Text()
        if store is None:
            body.append("(memory not started)", style="dim")
            return Panel(body, title="Memory", border_style="cyan")
        keys = list(store._registers.keys())
        ctx_n = sum(1 for k in keys if k.startswith("aries:context://"))
        mem_n = sum(1 for k in keys if k.startswith("aries:memory://"))
        cache_n = sum(1 for k in keys if k.startswith("aries:cache://"))
        body.append(f"context: {ctx_n}\n")
        body.append(f"memory:  {mem_n}\n")
        body.append(f"cache:   {cache_n}\n")
        body.append(f"clock:   {int(store.clock)}\n")
        body.append(f"logs:    {len(store._logs)}")
        return Panel(body, title="Memory", border_style="cyan")

    def _build_inference_panel(self) -> Panel:
        registry = getattr(self.node, "_inference_registry", None)
        body = Text()
        if registry is None:
            body.append("(inference registry not initialized)", style="dim")
            return Panel(body, title="Inference", border_style="cyan")
        configs = registry.get_configs()
        if not configs:
            body.append("(no models available — install llama.cpp + GGUF files)", style="dim")
            return Panel(body, title="Inference", border_style="cyan")
        configs = sorted(configs, key=lambda c: c.weighted_score(), reverse=True)[:6]
        tbl = Table.grid(padding=(0, 2))
        tbl.add_column(style="bold")
        tbl.add_column()
        tbl.add_column(justify="right")
        for c in configs:
            tbl.add_row(c.model_name, c.config_type, f"{c.estimated_tok_s:.1f} tok/s")
        active = self.node._inference_coordinator
        status = "active" if (active is not None and active.is_active) else "idle"
        return Panel(Group(tbl, Text(f"\nStatus: {status}", style="dim")),
                     title="Inference", border_style="cyan")

    def _build_activity_panel(self) -> Panel:
        if not self._events:
            return Panel(Text("(no activity yet)", style="dim"),
                         title="Activity", border_style="cyan")
        tbl = Table.grid(padding=(0, 1))
        tbl.add_column(style="dim", width=10)
        tbl.add_column()
        for ev in self._events[-self._max_events:]:
            stamp = time.strftime("%H:%M:%S", time.localtime(ev["timestamp"]))
            tbl.add_row(stamp, ev["description"])
        return Panel(tbl, title="Activity", border_style="cyan")

    # --- layout ----------------------------------------------------------

    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="top", size=10),
            Layout(name="peers", size=9),
            Layout(name="middle", size=10),
            Layout(name="inference", size=10),
            Layout(name="activity"),
        )
        layout["top"].split_row(
            Layout(self._build_node_panel(), name="node"),
            Layout(self._build_health_panel(), name="health"),
        )
        layout["peers"].update(self._build_peers_panel())
        layout["middle"].split_row(
            Layout(self._build_agents_panel(), name="agents"),
            Layout(self._build_memory_panel(), name="memory"),
        )
        layout["inference"].update(self._build_inference_panel())
        layout["activity"].update(self._build_activity_panel())
        return layout

    def _build_renderable(self, console: Console) -> Any:
        """Pick a layout based on terminal width; narrow terminals get a stack."""
        try:
            if console.size.width < 100:
                return Group(
                    self._build_node_panel(),
                    self._build_health_panel(),
                    self._build_peers_panel(),
                    self._build_agents_panel(),
                    self._build_memory_panel(),
                    self._build_inference_panel(),
                    self._build_activity_panel(),
                )
            return self._build_layout()
        except Exception:
            # Last-ditch fallback if Rich layout fails on this terminal.
            return Group(
                self._build_node_panel(),
                self._build_activity_panel(),
            )

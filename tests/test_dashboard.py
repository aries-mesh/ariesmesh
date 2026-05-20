"""Tests for the live terminal dashboard (Feature 4 / Phase 4)."""
from __future__ import annotations

import time
from types import SimpleNamespace

from rich.console import Console

from aries.cli.dashboard import TerminalDashboard
from aries.scheduler.router import DeviceHealth


def _make_mock_node(
    *,
    start_time: float = 0.0,
    snapshot: bool = False,
    peers: int = 0,
    agents: int = 0,
) -> SimpleNamespace:
    """Build a duck-typed AriesNode with just enough surface for the dashboard."""
    household = SimpleNamespace(
        device_did="did:key:z6Mk-mock-device",
        household_tag="test-household",
        devices={},
        agents={},
    )

    profiler = None
    if snapshot:
        snap = DeviceHealth(
            device_did=household.device_did,
            cpu_percent=42.0,
            ram_available_gb=4.0,
            ram_total_gb=16.0,
            battery_pct=78.0,
            charging=True,
            thermal="nominal",
            network_type="wifi",
            bandwidth_mbps=42.0,
        )
        profiler = SimpleNamespace(latest=snap)

    transport = SimpleNamespace(port=47291, _connections={})

    return SimpleNamespace(
        household=household,
        transport=transport,
        profiler=profiler,
        memory=None,
        _inference_registry=None,
        _inference_coordinator=None,
        _start_time=start_time,
    )


# ---------------------------------------------------------------------------
# Test 5 — layout builds without crash on a minimally-populated mock node
# ---------------------------------------------------------------------------


def test_dashboard_builds_layout_without_crash() -> None:
    node = _make_mock_node(start_time=time.time() - 30, snapshot=True)
    dashboard = TerminalDashboard(node)  # type: ignore[arg-type]

    # Wide terminal → grid layout
    wide_console = Console(width=120, force_terminal=True)
    renderable = dashboard._build_renderable(wide_console)
    assert renderable is not None

    # Narrow terminal → vertical stack (still must not crash)
    narrow_console = Console(width=80, force_terminal=True)
    stacked = dashboard._build_renderable(narrow_console)
    assert stacked is not None

    # Empty-node case: no profiler, no transport, no household
    empty = SimpleNamespace(
        household=None,
        transport=None,
        profiler=None,
        memory=None,
        _inference_registry=None,
        _inference_coordinator=None,
        _start_time=0.0,
    )
    empty_dashboard = TerminalDashboard(empty)  # type: ignore[arg-type]
    assert empty_dashboard._build_renderable(wide_console) is not None


# ---------------------------------------------------------------------------
# Test 6 — activity ring buffer caps at EVENT_BUFFER_SIZE
# ---------------------------------------------------------------------------


def test_dashboard_event_ring_buffer() -> None:
    node = _make_mock_node()
    dashboard = TerminalDashboard(node)  # type: ignore[arg-type]

    for i in range(30):
        dashboard.add_event("evt", f"event {i}")

    assert len(dashboard._events) == 20
    # Most recent event should be event 29 (the last one added).
    assert dashboard._events[-1]["description"] == "event 29"
    # First retained event should be event 10 (oldest 10 were evicted).
    assert dashboard._events[0]["description"] == "event 10"


# ---------------------------------------------------------------------------
# Test 7 — node panel shows uptime when _start_time is set
# ---------------------------------------------------------------------------


def test_dashboard_node_panel_shows_uptime() -> None:
    # Set start_time to one hour ago. Uptime should render as "01:00:..".
    node = _make_mock_node(start_time=time.time() - 3600)
    dashboard = TerminalDashboard(node)  # type: ignore[arg-type]
    panel = dashboard._build_node_panel()

    # Render to a string buffer to inspect the actual text.
    console = Console(width=80, record=True, force_terminal=False)
    console.print(panel)
    output = console.export_text()
    assert "01:00:" in output, f"Expected uptime '01:00:' in panel output:\n{output}"

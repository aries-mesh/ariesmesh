"""`aries` command-line entry point.

Spec reference: §19. Extended with `pair`, `resume`, `mandate` per plan.
"""
from __future__ import annotations

import asyncio
import json
import platform as _platform
import statistics
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

from ..adapters.base import BaseAdapter, InvokeRequest, Message
from ..adapters.litellm_adapter import (
    anthropic_adapter,
    custom_adapter,
    google_adapter,
    ollama_adapter,
    openai_adapter,
)
from ..adapters.mock_adapter import MockAdapter
from ..identity.household import AgentRecord, Household
from ..inference.coordinator import InferenceCoordinator
from ..inference.registry import InferenceConfig
from ..memory.store import MemoryStore
from ..node import AriesNode
from ..scheduler.profile import DeviceProfiler
from ..scheduler.router import load_mandates_from_yaml


console = Console()
DEFAULT_DATA_DIR = "~/.aries"
# How long the inference commands wait for mDNS discovery plus the capability
# exchange before concluding a model has no configuration.
PEER_SETTLE_S = 10.0


def run_async(coro):
    try:
        import uvloop  # type: ignore
        uvloop.install()
    except Exception:
        pass
    return asyncio.run(coro)


def _platform_id() -> str:
    sys = _platform.system().lower()
    if sys == "darwin":
        return "macos"
    return sys


# ---------------------------------------------------------------------------
# Click group
# ---------------------------------------------------------------------------

@click.group()
@click.option("--data-dir", default=DEFAULT_DATA_DIR, show_default=True, help="Data directory")
@click.pass_context
def cli(ctx: click.Context, data_dir: str) -> None:
    """Aries Mesh - personal compute fabric for your devices."""
    ctx.ensure_object(dict)
    ctx.obj["data_dir"] = data_dir


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--name", required=True, help="Device name (e.g. macbook-pro)")
@click.pass_context
def init(ctx: click.Context, name: str) -> None:
    """Initialize a fresh household on this device."""
    async def _go() -> None:
        node = AriesNode(data_dir=ctx.obj["data_dir"])
        info = await node.initialize(device_name=name, platform=_platform_id())
        console.print(
            Panel.fit(
                f"[bold green]Household initialized[/bold green]\n"
                f"User root DID:  {info['user_root_did']}\n"
                f"Device DID:     {info['device_did']}\n"
                f"Household tag:  {info['household_tag']}\n"
                f"Device name:    {info['device_name']}",
                title="aries init",
            )
        )

    run_async(_go())


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------

@cli.command()
@click.option(
    "--no-dashboard",
    is_flag=True,
    help="Disable the live terminal dashboard and run with a plain status line.",
)
@click.option(
    "--no-api",
    is_flag=True,
    help="Don't start the web dashboard HTTP API.",
)
@click.option(
    "--api-port",
    default=7272,
    show_default=True,
    type=int,
    help="Port for the web dashboard API (localhost only).",
)
@click.pass_context
def start(ctx: click.Context, no_dashboard: bool, no_api: bool, api_port: int) -> None:
    """Start the Aries daemon (Ctrl+C to stop)."""
    async def _go() -> None:
        node = AriesNode(data_dir=ctx.obj["data_dir"])
        await node.start(enable_api=not no_api, api_port=api_port)
        if node._api is not None:
            console.print(
                f"[dim]Dashboard:[/dim] [bold green]http://localhost:{node._api.port}[/bold green]"
            )

        if no_dashboard:
            console.print(
                Panel.fit(
                    f"[bold]Aries daemon running[/bold]\n"
                    f"Device DID:    {node.household.device_did}\n"
                    f"Household tag: {node.household.household_tag}\n"
                    f"TCP port:      {node.transport.port if node.transport else '?'}",
                    title="aries start",
                )
            )
            try:
                while True:
                    await asyncio.sleep(3600)
            except (KeyboardInterrupt, asyncio.CancelledError):
                console.print("[yellow]Shutting down...[/yellow]")
            finally:
                await node.stop()
            return

        # Live dashboard mode (default).
        from .dashboard import TerminalDashboard

        dashboard = TerminalDashboard(node)
        node._dashboard = dashboard
        node._emit_event("node_start", f"Aries daemon started on port {node.transport.port if node.transport else '?'}")
        try:
            await dashboard.run()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            node._dashboard = None
            await node.stop()
            console.print("[yellow]Shutting down...[/yellow]")

    run_async(_go())


# ---------------------------------------------------------------------------
# connect — manual peer connection (fallback when mDNS is unavailable)
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("address")
@click.option(
    "--no-dashboard",
    is_flag=True,
    help="Disable the live terminal dashboard and run with a plain status line.",
)
@click.option(
    "--no-api",
    is_flag=True,
    help="Don't start the web dashboard HTTP API.",
)
@click.option(
    "--api-port",
    default=7272,
    show_default=True,
    type=int,
    help="Port for the web dashboard API (localhost only).",
)
@click.pass_context
def connect(
    ctx: click.Context,
    address: str,
    no_dashboard: bool,
    no_api: bool,
    api_port: int,
) -> None:
    """Start the daemon and manually connect to a peer at HOST:PORT.

    Equivalent to `aries start` plus an explicit `connect_to_peer` call —
    used on Termux or any other environment where mDNS discovery is
    unavailable. ADDRESS is the peer's TCP endpoint, for example:

        aries connect 192.168.1.42:47291
    """
    from ..transport.peer import PeerInfo

    if ":" not in address:
        console.print(f"[red]Invalid address {address!r}: expected `host:port`.[/red]")
        raise click.Abort()
    host, port_str = address.rsplit(":", 1)
    try:
        port = int(port_str)
    except ValueError:
        console.print(f"[red]Invalid port in {address!r}: {port_str}[/red]")
        raise click.Abort() from None

    async def _go() -> None:
        node = AriesNode(data_dir=ctx.obj["data_dir"])
        await node.start(enable_api=not no_api, api_port=api_port)
        if node._api is not None:
            console.print(
                f"[dim]Dashboard:[/dim] [bold green]http://localhost:{node._api.port}[/bold green]"
            )

        # `device_did="unknown"` matches the server-side `_handle_connection`
        # convention — the receive loop will fill it in from the peer's first
        # signed message and register the connection by DID at that point.
        peer = PeerInfo(
            device_did="unknown",
            name=address,
            host=host,
            port=port,
            household_tag=(node.household.household_tag if node.household else ""),
        )
        try:
            await node._connect_and_announce(peer)
            console.print(
                f"[green]Connected to {address}[/green] "
                "[dim](peer DID will resolve on next ANNOUNCE)[/dim]"
            )
        except Exception as exc:
            console.print(f"[red]Failed to connect to {address}: {exc}[/red]")

        if no_dashboard:
            console.print(
                Panel.fit(
                    f"[bold]Aries daemon running[/bold]\n"
                    f"Device DID:    {node.household.device_did}\n"
                    f"Household tag: {node.household.household_tag}\n"
                    f"TCP port:      {node.transport.port if node.transport else '?'}\n"
                    f"Manual peer:   {address}",
                    title="aries connect",
                )
            )
            try:
                while True:
                    await asyncio.sleep(3600)
            except (KeyboardInterrupt, asyncio.CancelledError):
                console.print("[yellow]Shutting down...[/yellow]")
            finally:
                await node.stop()
            return

        from .dashboard import TerminalDashboard
        dashboard = TerminalDashboard(node)
        node._dashboard = dashboard
        node._emit_event(
            "node_start",
            f"Aries daemon started on port {node.transport.port if node.transport else '?'}",
        )
        node._emit_event("peer_manual_connect", f"Manual peer requested: {address}")
        try:
            await dashboard.run()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            node._dashboard = None
            await node.stop()
            console.print("[yellow]Shutting down...[/yellow]")

    run_async(_go())


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show node identity, hardware health, and agent count."""
    household = Household(data_dir=ctx.obj["data_dir"])
    if not household.is_initialized:
        console.print("[red]Household not initialized. Run `aries init --name <name>` first.[/red]")
        return
    household.load()
    profiler = DeviceProfiler(device_did=household.device_did or "")
    snap = profiler.snapshot()
    static = profiler.static_info()
    body = (
        f"User root DID:   {household.user_root_did}\n"
        f"Device DID:      {household.device_did}\n"
        f"Household tag:   {household.household_tag}\n"
        f"Devices in mesh: {len(household.devices)}\n"
        f"Agents:          {len(household.agents)}\n"
        f"\n"
        f"Platform:        {static.get('platform')} / {static.get('arch')}\n"
        f"CPU:             {static.get('cpu_cores')}c / {static.get('cpu_threads')}t - load {snap.cpu_percent:.0f}%\n"
        f"RAM:             {snap.ram_available_gb:.1f} GB free / {snap.ram_total_gb:.1f} GB\n"
        f"Battery:         {snap.battery_pct if snap.battery_pct is not None else 'n/a (plugged)'}\n"
        f"Thermal:         {snap.thermal}\n"
    )
    console.print(Panel.fit(body, title="aries status"))


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------

@cli.command()
@click.pass_context
def agents(ctx: click.Context) -> None:
    """List agents registered on this device."""
    household = Household(data_dir=ctx.obj["data_dir"])
    if not household.is_initialized:
        console.print("[red]Household not initialized.[/red]")
        return
    household.load()

    table = Table(title="Registered agents")
    table.add_column("Name", style="bold")
    table.add_column("Vendor")
    table.add_column("Model")
    table.add_column("Locality")
    table.add_column("Cost")
    table.add_column("Capabilities")
    for agent in household.agents.values():
        table.add_row(
            agent.name,
            agent.vendor,
            agent.model or "",
            agent.locality,
            agent.cost_class,
            ", ".join(agent.capabilities),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--vendor", required=True, type=click.Choice(["ollama", "anthropic", "openai", "google", "mock", "custom"]))
@click.option("--model", required=True)
@click.option("--api-key", default=None, help="API key for cloud vendors")
@click.option("--api-base", default=None, help="Base URL for ollama/custom")
@click.option("--name", default=None, help="Override agent display name")
@click.pass_context
def register(ctx: click.Context, vendor: str, model: str, api_key: Optional[str], api_base: Optional[str], name: Optional[str]) -> None:
    """Register an LLM adapter as an agent in the household."""
    if vendor == "mock":
        adapter = MockAdapter(model=model)
    elif vendor == "ollama":
        adapter = ollama_adapter(model=model, api_base=api_base or "http://localhost:11434")
    elif vendor == "anthropic":
        adapter = anthropic_adapter(model=model, api_key=api_key)
    elif vendor == "openai":
        adapter = openai_adapter(model=model, api_key=api_key)
    elif vendor == "google":
        adapter = google_adapter(model=model, api_key=api_key)
    else:
        if not api_base:
            console.print("[red]--api-base is required for vendor=custom[/red]")
            return
        adapter = custom_adapter(model=model, api_base=api_base, api_key=api_key)

    node = AriesNode(data_dir=ctx.obj["data_dir"])
    node.household = Household(data_dir=ctx.obj["data_dir"])
    if not node.household.is_initialized:
        console.print("[red]Household not initialized. Run `aries init` first.[/red]")
        return
    node.household.load()
    record = node.register_agent(adapter, name=name)
    console.print(
        Panel.fit(
            f"[bold green]Agent registered[/bold green]\n"
            f"Name:     {record.name}\n"
            f"DID:      {record.agent_did}\n"
            f"Vendor:   {record.vendor}\n"
            f"Locality: {record.locality}",
            title="aries register",
        )
    )


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("key")
@click.option("--value", default=None, help="If given, set; otherwise read")
@click.option("--list", "list_keys", is_flag=True, help="List keys with this prefix")
@click.pass_context
def memory(ctx: click.Context, key: str, value: Optional[str], list_keys: bool) -> None:
    """Read or write a memory key (context://, memory://, cache://)."""
    household = Household(data_dir=ctx.obj["data_dir"])
    if not household.is_initialized:
        console.print("[red]Household not initialized.[/red]")
        return
    household.load()
    store = MemoryStore(
        device_did=household.device_did or "",
        persist_dir=Path(ctx.obj["data_dir"]).expanduser() / "memory",
    )
    if list_keys:
        for k in store.keys(prefix=key):
            console.print(k)
        return
    if value is None:
        result = store.get(key)
        console.print(json.dumps(result, indent=2) if result is not None else "[dim]<not set>[/dim]")
    else:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
        store.set(key, parsed)
        console.print(f"[green]set[/green] {key}")


# ---------------------------------------------------------------------------
# household
# ---------------------------------------------------------------------------

@cli.command()
@click.pass_context
def household(ctx: click.Context) -> None:
    """Show the household tree (root -> devices -> agents)."""
    h = Household(data_dir=ctx.obj["data_dir"])
    if not h.is_initialized:
        console.print("[red]Household not initialized.[/red]")
        return
    h.load()
    tree = Tree(f"[bold]Household[/bold] {h.user_root_did}")
    for device in h.devices.values():
        marker = "*" if device.is_self else " "
        d_node = tree.add(f"{marker} {device.name} [{device.platform}] {device.device_did[:24]}...")
        for agent in h.agents.values():
            d_node.add(f"{agent.name} ({agent.vendor}/{agent.model or ''}) - {', '.join(agent.capabilities)}")
    console.print(tree)


# ---------------------------------------------------------------------------
# pair
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--invite", is_flag=True, help="On an existing device, print an invitation code")
@click.option("--code", default=None, help="On a new device, paste an invitation code")
@click.option("--name", default=None, help="Device name (joiner only)")
@click.pass_context
def pair(ctx: click.Context, invite: bool, code: Optional[str], name: Optional[str]) -> None:
    """Pair two devices into one household."""
    data_dir = ctx.obj["data_dir"]
    if invite:
        h = Household(data_dir=data_dir)
        if not h.is_initialized:
            console.print("[red]Household not initialized.[/red]")
            return
        h.load()
        offer = h.start_pairing()
        console.print(
            Panel.fit(
                f"[bold]Pairing code (valid 5 minutes):[/bold]\n\n"
                f"[bold green]{offer.code}[/bold green]\n\n"
                f"Run on the new device:\n"
                f"  aries pair --code \"{offer.code}\" --name <new-device-name>",
                title="aries pair --invite",
            )
        )
        console.print("[yellow]Keep this terminal running so the daemon can accept the request.[/yellow]")
        console.print("[dim]Start `aries start` in another terminal if not already running.[/dim]")
        return

    if not code:
        console.print("[red]Provide either --invite or --code.[/red]")
        return
    if not name:
        console.print("[red]--name is required when joining.[/red]")
        return

    async def _go() -> None:
        node = AriesNode(data_dir=data_dir)
        try:
            info = await node.pair_with_invitation(code=code, device_name=name, platform=_platform_id())
            console.print(
                Panel.fit(
                    f"[bold green]Joined household[/bold green]\n"
                    f"User root DID: {info['user_root_did']}\n"
                    f"Device DID:    {info['device_did']}\n"
                    f"Household tag: {info['household_tag']}",
                    title="aries pair --code",
                )
            )
        except Exception as exc:
            console.print(f"[red]Pairing failed:[/red] {exc}")
        finally:
            if node.transport is not None:
                await node.transport.stop()

    run_async(_go())


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("task_id")
@click.pass_context
def resume(ctx: click.Context, task_id: str) -> None:
    """Manually resume a task (re-runs the scheduler against stored history)."""
    async def _go() -> None:
        node = AriesNode(data_dir=ctx.obj["data_dir"])
        await node.start()
        try:
            resp = await node.resume_task(task_id)
            console.print(Panel.fit(resp.content, title=f"resumed {task_id}"))
        finally:
            await node.stop()

    run_async(_go())


# ---------------------------------------------------------------------------
# mandate
# ---------------------------------------------------------------------------

@cli.command()
@click.pass_context
def mandate(ctx: click.Context) -> None:
    """List mandates from ~/.aries/mandates.yaml."""
    path = Path(ctx.obj["data_dir"]).expanduser() / "mandates.yaml"
    mandates = load_mandates_from_yaml(path)
    if not mandates:
        console.print(f"[dim]No mandates loaded (looked at {path}).[/dim]")
        return
    table = Table(title=f"Mandates from {path}")
    table.add_column("Name", style="bold")
    table.add_column("Default")
    table.add_column("When tags")
    table.add_column("When time")
    table.add_column("Enforce locality")
    table.add_column("Enforce cost")
    for m in mandates:
        table.add_row(
            m.name,
            "yes" if m.is_default else "",
            ",".join(m.when_tags),
            m.when_time or "",
            m.enforce_locality or "",
            m.enforce_cost_class or "",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# invoke — primary scheduler-driven invocation, with streaming output
# ---------------------------------------------------------------------------

@cli.command()
@click.option("-m", "--message", required=True, help="Prompt message")
@click.option("--stream/--no-stream", default=True, help="Stream tokens as they arrive")
@click.option("--capability", default="text.qa", help="Required capability")
@click.option("--system-prompt", default=None, help="System prompt")
@click.option(
    "--locality",
    default="household",
    type=click.Choice(["local-only", "household", "any"]),
)
@click.option("--tag", "tags", multiple=True, help="Task tag (for mandate matching)")
@click.pass_context
def invoke(
    ctx: click.Context,
    message: str,
    stream: bool,
    capability: str,
    system_prompt: Optional[str],
    locality: str,
    tags: tuple[str, ...],
) -> None:
    """Send a prompt to the mesh. The scheduler picks the best model."""
    import time as _time
    from ..scheduler.router import Locality

    async def _go() -> None:
        node = AriesNode(data_dir=ctx.obj["data_dir"])
        await node.start()
        try:
            # Agent records outlive the process; their adapters don't. Rebuild
            # whatever we can so a registered agent is actually callable.
            _reattach_adapters(node)

            msgs = [Message(role="user", content=message)]
            kwargs = dict(
                messages=msgs,
                capability=capability,
                system_prompt=system_prompt,
                locality=Locality(locality),
                tags=list(tags) if tags else None,
            )

            if stream:
                started = _time.perf_counter()
                ttft_ms: Optional[float] = None
                token_count = 0
                model_name = "(unknown)"
                try:
                    async for token in node.invoke_stream(**kwargs):
                        if ttft_ms is None:
                            ttft_ms = (_time.perf_counter() - started) * 1000.0
                        console.print(token, end="", style=None, highlight=False)
                        token_count += 1
                    elapsed = _time.perf_counter() - started
                    tok_s = token_count / elapsed if elapsed > 0 else 0.0
                    console.print(
                        f"\n\n[dim]{model_name} · {token_count} tokens · "
                        f"{tok_s:.1f} tok/s · {ttft_ms or 0:.0f}ms TTFT[/dim]"
                    )
                except RuntimeError as exc:
                    console.print(f"[red]{exc}[/red]")
                    console.print(
                        "[dim]Hint: register an agent first, e.g. "
                        "`aries register --vendor mock --model demo`[/dim]"
                    )
            else:
                try:
                    resp = await node.invoke(**kwargs)
                    console.print(
                        Panel.fit(
                            resp.content,
                            title=f"{resp.model} ({resp.latency_ms:.0f} ms)",
                        )
                    )
                except RuntimeError as exc:
                    console.print(f"[red]{exc}[/red]")
                    console.print(
                        "[dim]Hint: register an agent first, e.g. "
                        "`aries register --vendor mock --model demo`[/dim]"
                    )
        finally:
            await node.stop()

    run_async(_go())


# ---------------------------------------------------------------------------
# inference (Feature 2)
# ---------------------------------------------------------------------------

@cli.group()
@click.pass_context
def inference(ctx: click.Context) -> None:
    """Distributed inference commands."""
    pass


async def _await_peer_capabilities(node: AriesNode, wait_s: float = PEER_SETTLE_S) -> int:
    """Give peers a chance to publish capabilities; return how many devices we know.

    Returns as soon as any peer has answered, so the common case doesn't pay the
    full window. A solo device waits it out once.
    """
    registry = node._inference_registry
    if registry is None:
        return 0
    deadline = time.perf_counter() + max(wait_s, 0.0)
    while time.perf_counter() < deadline:
        if len(registry._device_capabilities) > 1:
            break
        await asyncio.sleep(0.25)
    return len(registry._device_capabilities)


@inference.command("status")
@click.pass_context
def inference_status(ctx: click.Context) -> None:
    """Show all currently-feasible inference configurations across the mesh."""

    async def _go() -> None:
        node = AriesNode(data_dir=ctx.obj["data_dir"])
        # A full node, not a bare probe: distributed configurations only exist
        # once peers have connected and published what they can contribute.
        await node.start(enable_api=False)
        try:
            console.print("[dim]Scanning the mesh...[/dim]")
            devices = await _await_peer_capabilities(node)
            local_cap = node._local_inference_capability
            registry = node._inference_registry
            configs = registry.get_configs() if registry is not None else []

            if not configs:
                console.print(
                    Panel.fit(
                        f"No inference configurations available.\n\n"
                        f"llama.cpp on this device: "
                        f"{'yes' if local_cap and local_cap.llama_cpp_available else 'no'}\n"
                        f"Models discovered:        "
                        f"{len(local_cap.available_models) if local_cap else 0}\n"
                        f"Backend:                  "
                        f"{local_cap.backend if local_cap else 'unknown'}\n"
                        f"Devices published:        {devices}\n\n"
                        f"Put GGUF files in ~/.aries/models and make sure "
                        f"llama-server is on PATH.",
                        title="aries inference status",
                    )
                )
                return

            table = Table(
                title="Inference configurations",
                caption=f"{devices} device(s) publishing capabilities",
            )
            table.add_column("Model", style="bold")
            table.add_column("Type")
            table.add_column("Devices")
            table.add_column("Est. tok/s")
            table.add_column("Score", justify="right")
            for c in sorted(configs, key=lambda c: c.weighted_score(), reverse=True):
                table.add_row(
                    c.model_name,
                    c.config_type,
                    ",".join(d.device_did[:10] for d in c.devices),
                    f"{c.estimated_tok_s:.1f}",
                    f"{c.weighted_score():.2f}",
                )
            console.print(table)
        finally:
            await node.stop()

    run_async(_go())


async def _resolve_inference_config(
    node: AriesNode, model: str, wait_s: float = PEER_SETTLE_S
) -> Optional[InferenceConfig]:
    """Best-scoring configuration for ``model``, or None having explained why.

    Waits up to ``wait_s`` for peers to appear. A node that just started knows
    only its own hardware; a distributed configuration cannot exist until mDNS
    has found a peer and that peer has answered with its capability, which is
    a couple of round trips after `start()` returns.
    """
    registry = node._inference_registry
    if registry is None:
        console.print("[red]Inference registry not initialized.[/red]")
        return None

    deadline = time.perf_counter() + max(wait_s, 0.0)
    waited = False
    while True:
        configs = [c for c in registry.get_configs() if c.model_name == model]
        if configs:
            return max(configs, key=lambda c: c.weighted_score())
        if time.perf_counter() >= deadline:
            break
        if not waited:
            console.print("[dim]Waiting for peers to publish their capabilities...[/dim]")
            waited = True
        await asyncio.sleep(0.25)

    console.print(
        f"[red]No configuration for model {model!r}.[/red] "
        f"[dim]`aries inference status` lists what this device can see.[/dim]"
    )
    peers = len(node.transport.connected_peers()) if node.transport is not None else 0
    if peers == 0:
        console.print(
            "[dim]No peers are connected, so only single-device configurations "
            "are possible. Pair a second device, or use `aries connect <ip:port>` "
            "if mDNS is unavailable.[/dim]"
        )
    return None


def _report_inference_failure(exc: Exception) -> None:
    """Both inference commands name a model explicitly, so quietly falling back
    to whatever else the scheduler likes would answer a different question."""
    console.print(f"[red]{exc}[/red]")
    console.print(
        "[dim]Hint: install llama.cpp so `llama-server` is on PATH, "
        "or use `aries invoke` to let the scheduler choose.[/dim]"
    )


def _adapter_from_record(rec: AgentRecord) -> Optional[BaseAdapter]:
    """Rebuild a live adapter for a persisted agent record.

    Agent records survive restarts but adapter instances do not, so a fresh
    process holds records it has no way to call. Vendors whose credentials live
    in the environment rebuild cleanly — litellm reads ANTHROPIC_API_KEY /
    OPENAI_API_KEY / GEMINI_API_KEY itself when no key is passed. Returns None
    for `openai-compatible`, whose api_base was never persisted.
    """
    model = rec.model or ""
    if rec.vendor == "mock":
        return MockAdapter(model=model or "mock-1")
    if rec.vendor == "ollama":
        return ollama_adapter(model=model)
    if rec.vendor == "anthropic":
        return anthropic_adapter(model=model)
    if rec.vendor == "openai":
        return openai_adapter(model=model)
    if rec.vendor == "google":
        return google_adapter(model=model)
    return None


def _reattach_adapters(node: AriesNode) -> None:
    """Give every reconstructable agent record a live adapter on this node."""
    if node.household is None:
        return
    for did, rec in node.household.agents.items():
        if did in node._adapters:
            continue
        adapter = _adapter_from_record(rec)
        if adapter is not None:
            node.attach_adapter(did, adapter)


def _find_agent(node: AriesNode, needle: str) -> Optional[AgentRecord]:
    """Look an agent up by exact name, exact DID, or DID prefix."""
    if node.household is not None:
        for rec in node.household.agents.values():
            if needle in (rec.name, rec.agent_did) or rec.agent_did.startswith(needle):
                return rec
    console.print(f"[red]No registered agent matching {needle!r}. Try `aries agents`.[/red]")
    return None


async def _timed_runs(
    make_stream: Callable[[], AsyncIterator[str]],
    runs: int,
    warmup: int,
) -> list[dict[str, float]]:
    """Drive a token stream `warmup + runs` times, timing only the measured runs.

    Per run it records:
      ``ttft_s``        request start → first token (prefill + any setup on the
                        request path; model load is deliberately hoisted out by
                        the caller so it lands in warmup, not here)
      ``decode_tok_s``  (n-1) / (last token − first token). Excluding prefill is
                        what makes this comparable across arms — a distributed
                        run pays its network cost per decoded token, and folding
                        prefill in would dilute exactly the effect under test.
      ``total_s``       request start → last token
    """
    records: list[dict[str, float]] = []
    for i in range(warmup + runs):
        started = time.perf_counter()
        first_at: Optional[float] = None
        n = 0
        async for _token in make_stream():
            if first_at is None:
                first_at = time.perf_counter()
            n += 1
        ended = time.perf_counter()

        if i < warmup:
            console.print(f"  [dim]warmup {i + 1}/{warmup}: {n} tokens[/dim]")
            continue
        if n == 0 or first_at is None:
            console.print("  [yellow]run produced no tokens; excluded[/yellow]")
            continue

        decode_s = ended - first_at
        rec = {
            "ttft_s": first_at - started,
            "decode_tok_s": (n - 1) / decode_s if n > 1 and decode_s > 0 else 0.0,
            "total_s": ended - started,
            "tokens": float(n),
        }
        records.append(rec)
        console.print(
            f"  run {i - warmup + 1}/{runs}: {rec['decode_tok_s']:.2f} tok/s · "
            f"{rec['ttft_s'] * 1000:.0f} ms TTFT · {n} tokens"
        )
    return records


def _report_runs(title: str, caption: str, records: list[dict[str, float]]) -> None:
    if not records:
        console.print("[yellow]No successful runs; nothing to report.[/yellow]")
        return

    def stats(key: str) -> tuple[float, float]:
        xs = sorted(r[key] for r in records)
        return statistics.median(xs), xs[min(int(len(xs) * 0.95), len(xs) - 1)]

    table = Table(title=title, caption=caption)
    table.add_column("Metric")
    table.add_column("Median", justify="right")
    table.add_column("p95", justify="right")
    tok_med, tok_p95 = stats("decode_tok_s")
    ttft_med, ttft_p95 = stats("ttft_s")
    total_med, total_p95 = stats("total_s")
    table.add_row("decode tok/s", f"{tok_med:.2f}", f"{tok_p95:.2f}")
    table.add_row("TTFT (ms)", f"{ttft_med * 1000:.0f}", f"{ttft_p95 * 1000:.0f}")
    table.add_row("total (s)", f"{total_med:.2f}", f"{total_p95:.2f}")
    table.add_row("tokens", f"{statistics.median([r['tokens'] for r in records]):.0f}", "")
    console.print(table)


@inference.command("run")
@click.option("--model", required=True, help="Model name (matches a GGUF file)")
@click.option("--prompt", "-m", required=True, help="Prompt text")
@click.option("--stream/--no-stream", default=True, help="Stream tokens as they arrive")
@click.option("--max-tokens", default=4096)
@click.pass_context
def inference_run(
    ctx: click.Context, model: str, prompt: str, stream: bool, max_tokens: int
) -> None:
    """Run inference using the best available configuration for ``--model``."""
    async def _go() -> None:
        node = AriesNode(data_dir=ctx.obj["data_dir"])
        await node.start()
        try:
            best = await _resolve_inference_config(node, model)
            if best is None:
                return
            console.print(
                f"Running {model} via [bold]{best.config_type}[/bold] "
                f"(estimated {best.estimated_tok_s:.1f} tok/s)"
            )
            try:
                resp = await node.invoke(
                    messages=[Message(role="user", content=prompt)],
                    inference_config=best,
                    max_tokens=max_tokens,
                    stream=stream,
                )
            except RuntimeError as exc:
                _report_inference_failure(exc)
                return
            console.print(
                Panel.fit(resp.content, title=f"{resp.model} ({resp.latency_ms:.0f} ms)")
            )
        finally:
            await node.stop()

    run_async(_go())


@inference.command("benchmark")
@click.option("--model", default=None, help="GGUF model name (a local or distributed config)")
@click.option(
    "--agent",
    default=None,
    help="Registered agent name or DID instead of --model (e.g. a cloud model)",
)
@click.option("--prompt-tokens", default=128, help="Approximate prompt length in tokens")
@click.option("--gen-tokens", default=64, help="Token budget per run")
@click.option("--runs", default=5, help="Measured runs")
@click.option(
    "--warmup",
    default=1,
    help="Untimed runs first, so cache-warming doesn't land in the medians.",
)
@click.pass_context
def inference_benchmark(
    ctx: click.Context,
    model: Optional[str],
    agent: Optional[str],
    prompt_tokens: int,
    gen_tokens: int,
    runs: int,
    warmup: int,
) -> None:
    """Measure decode throughput and TTFT for one execution mode.

    Exactly one of --model (a GGUF config, local or distributed) or --agent (a
    registered adapter, including cloud) selects what to measure. Both arms are
    driven through the same streaming timer, so their numbers are comparable.

    Model loading happens once, before the timed runs — a benchmark that reloads
    weights per iteration measures disk, not inference.
    """
    if bool(model) == bool(agent):
        console.print("[red]Pass exactly one of --model or --agent.[/red]")
        raise click.Abort()
    if runs < 1:
        console.print("[red]--runs must be at least 1.[/red]")
        raise click.Abort()

    async def _go() -> None:
        node = AriesNode(data_dir=ctx.obj["data_dir"])
        await node.start()
        prompt = " ".join(["benchmark"] * prompt_tokens)
        caption = (
            f"n={runs} (+{warmup} warmup) · ~{prompt_tokens} prompt tokens · "
            f"{gen_tokens} token budget"
        )
        try:
            if model:
                config = await _resolve_inference_config(node, model)
                if config is None:
                    return
                devices = len(config.devices)
                console.print(
                    f"Benchmarking [bold]{model}[/bold] via {config.config_type} "
                    f"across {devices} device(s) — loading weights..."
                )
                coordinator = InferenceCoordinator(node=node, config=config)
                if not await coordinator.setup():
                    console.print(
                        f"[red]Inference setup failed for config {config.config_id}.[/red]"
                    )
                    return
                try:
                    records = await _timed_runs(
                        lambda: coordinator.generate(
                            prompt=prompt, max_tokens=gen_tokens, stream=True
                        ),
                        runs,
                        warmup,
                    )
                except RuntimeError as exc:
                    _report_inference_failure(exc)
                    return
                finally:
                    await coordinator.teardown()
                _report_runs(
                    f"{model} — {config.config_type} ({devices} device(s))", caption, records
                )
                return

            _reattach_adapters(node)
            record = _find_agent(node, agent or "")
            if record is None:
                return
            adapter = node._adapters.get(record.agent_did)
            if adapter is None:
                console.print(
                    f"[red]Agent {record.name} ({record.vendor}) has no live adapter.[/red]"
                )
                console.print(
                    "[dim]Cloud vendors read their key from the environment "
                    "(ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY); "
                    "openai-compatible agents can't be rebuilt because their "
                    "--api-base was never persisted.[/dim]"
                )
                return

            console.print(
                f"Benchmarking [bold]{record.name}[/bold] "
                f"({record.vendor}/{record.model or '—'}, {record.locality})"
            )
            req = InvokeRequest(
                messages=[Message(role="user", content=prompt)],
                max_tokens=gen_tokens,
                stream=True,
            )
            try:
                records = await _timed_runs(lambda: adapter.invoke_stream(req), runs, warmup)
            except (RuntimeError, ImportError) as exc:
                console.print(f"[red]{exc}[/red]")
                return
            _report_runs(f"{record.name} — {record.locality}", caption, records)
        finally:
            await node.stop()

    run_async(_go())


if __name__ == "__main__":  # pragma: no cover
    cli()

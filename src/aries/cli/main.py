"""`aries` command-line entry point.

Spec reference: §19. Extended with `pair`, `resume`, `mandate` per plan.
"""
from __future__ import annotations

import asyncio
import json
import platform as _platform
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

from ..adapters.base import Message
from ..adapters.litellm_adapter import (
    anthropic_adapter,
    custom_adapter,
    google_adapter,
    ollama_adapter,
    openai_adapter,
)
from ..adapters.mock_adapter import MockAdapter
from ..identity.household import Household
from ..memory.store import MemoryStore
from ..node import AriesNode
from ..scheduler.profile import DeviceProfiler
from ..scheduler.router import load_mandates_from_yaml


console = Console()
DEFAULT_DATA_DIR = "~/.aries"


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
            # Re-attach mock adapters transparently (same convenience as before).
            for did, rec in node.household.agents.items():  # type: ignore[union-attr]
                if rec.vendor == "mock" and did not in node._adapters:
                    node.attach_adapter(did, MockAdapter(model=rec.model or "mock-1"))

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


@inference.command("status")
@click.pass_context
def inference_status(ctx: click.Context) -> None:
    """Show all currently-feasible inference configurations."""
    from ..inference.capability import probe_inference_capability
    from ..inference.registry import InferenceRegistry

    async def _go() -> None:
        h = Household(data_dir=ctx.obj["data_dir"])
        if not h.is_initialized:
            console.print("[red]Household not initialized.[/red]")
            return
        h.load()
        registry = InferenceRegistry()
        local_cap = await probe_inference_capability(
            h.device_did or "", Path(ctx.obj["data_dir"]).expanduser()
        )
        registry.update_device(h.device_did or "", local_cap)
        configs = registry.get_configs()

        if not configs:
            console.print(
                Panel.fit(
                    f"No inference configurations available.\n\n"
                    f"llama.cpp on this device: "
                    f"{'yes' if local_cap.llama_cpp_available else 'no'}\n"
                    f"Models discovered:        {len(local_cap.available_models)}\n"
                    f"Backend:                  {local_cap.backend}\n",
                    title="aries inference status",
                )
            )
            return

        table = Table(title="Inference configurations")
        table.add_column("Model", style="bold")
        table.add_column("Type")
        table.add_column("Devices")
        table.add_column("Est. tok/s")
        table.add_column("Score", justify="right")
        for c in configs:
            table.add_row(
                c.model_name,
                c.config_type,
                ",".join(d.device_did[:10] for d in c.devices),
                f"{c.estimated_tok_s:.1f}",
                f"{c.weighted_score():.2f}",
            )
        console.print(table)

    run_async(_go())


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
            registry = node._inference_registry
            if registry is None:
                console.print("[red]Inference registry not initialized.[/red]")
                return
            configs = [c for c in registry.get_configs() if c.model_name == model]
            if not configs:
                console.print(
                    f"[red]No configuration for model {model!r}. "
                    f"Run `aries inference status` to see available models.[/red]"
                )
                return
            best = max(configs, key=lambda c: c.weighted_score())
            console.print(
                f"Running {model} via [bold]{best.config_type}[/bold] "
                f"(estimated {best.estimated_tok_s:.1f} tok/s)"
            )
            resp = await node.invoke(
                messages=[Message(role="user", content=prompt)],
                stream=stream,
            )
            console.print(
                Panel.fit(resp.content, title=f"{resp.model} ({resp.latency_ms:.0f} ms)")
            )
        finally:
            await node.stop()

    run_async(_go())


@inference.command("benchmark")
@click.option("--model", required=True)
@click.option("--prompt-tokens", default=128)
@click.option("--gen-tokens", default=64)
@click.option("--runs", default=5)
@click.pass_context
def inference_benchmark(
    ctx: click.Context, model: str, prompt_tokens: int, gen_tokens: int, runs: int
) -> None:
    """Benchmark inference for a given model across ``--runs`` iterations."""
    import statistics
    import time

    async def _go() -> None:
        node = AriesNode(data_dir=ctx.obj["data_dir"])
        await node.start()
        try:
            tok_per_s: list[float] = []
            ttfts: list[float] = []
            prompt = " ".join(["benchmark"] * prompt_tokens)
            for i in range(runs):
                start = time.perf_counter()
                resp = await node.invoke(
                    messages=[Message(role="user", content=prompt)],
                    stream=False,
                )
                elapsed = time.perf_counter() - start
                total_tokens = max(int(resp.usage.get("completion_tokens", gen_tokens)), 1)
                tok_per_s.append(total_tokens / elapsed)
                ttfts.append(resp.latency_ms / 1000.0)
                console.print(f"  run {i+1}/{runs}: {tok_per_s[-1]:.2f} tok/s")

            table = Table(title=f"Benchmark — {model}")
            table.add_column("Metric")
            table.add_column("Median", justify="right")
            table.add_column("p95", justify="right")
            tok_per_s.sort()
            ttfts.sort()

            def p95(xs: list[float]) -> float:
                return xs[min(int(len(xs) * 0.95), len(xs) - 1)]

            table.add_row("tok/s", f"{statistics.median(tok_per_s):.2f}", f"{p95(tok_per_s):.2f}")
            table.add_row("TTFT (s)", f"{statistics.median(ttfts):.2f}", f"{p95(ttfts):.2f}")
            console.print(table)
        finally:
            await node.stop()

    run_async(_go())


if __name__ == "__main__":  # pragma: no cover
    cli()

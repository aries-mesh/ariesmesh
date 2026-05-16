"""Unit tests for Scheduler routing, filters, scores, and mandates."""
from __future__ import annotations

import tempfile
from pathlib import Path

from aries.identity.household import AgentRecord
from aries.scheduler.router import (
    DeviceHealth,
    Locality,
    Mandate,
    ScoringWeights,
    Scheduler,
    TaskConstraints,
    load_mandates_from_yaml,
)


def make_agent(
    name: str,
    vendor: str,
    capabilities: list[str],
    locality: str = "local",
    cost_class: str = "free",
    context_window: int = 32000,
) -> AgentRecord:
    return AgentRecord(
        agent_did=f"did:key:z{name}",
        name=name,
        vendor=vendor,
        capabilities=capabilities,
        context_window=context_window,
        locality=locality,
        cost_class=cost_class,
    )


def test_filter_capability_match() -> None:
    sch = Scheduler()
    a = make_agent("ollama-qwen", "ollama", ["text.qa"])
    b = make_agent("ollama-codestral", "ollama", ["code.generate"])
    selected = sch.select_agent([a, b], TaskConstraints(capability="text.qa"))
    assert selected is not None
    assert selected[0] is a


def test_filter_local_only_excludes_cloud() -> None:
    sch = Scheduler()
    local = make_agent("a", "ollama", ["text.qa"], locality="local")
    cloud = make_agent("b", "anthropic", ["text.qa"], locality="cloud-routed")
    selected = sch.select_agent(
        [cloud, local],
        TaskConstraints(capability="text.qa", locality=Locality.LOCAL_ONLY),
    )
    assert selected is not None
    assert selected[0] is local


def test_filter_cost_class_excludes_paid() -> None:
    sch = Scheduler()
    free = make_agent("a", "ollama", ["text.qa"], cost_class="free")
    paid = make_agent("b", "anthropic", ["text.qa"], cost_class="paid")
    selected = sch.select_agent(
        [paid, free], TaskConstraints(capability="text.qa", max_cost_class="metered")
    )
    assert selected is not None
    assert selected[0] is free


def test_score_local_beats_cloud_on_privacy() -> None:
    sch = Scheduler()
    local = make_agent("a", "ollama", ["text.qa"], locality="local")
    cloud = make_agent("b", "anthropic", ["text.qa"], locality="cloud-routed", cost_class="paid")
    selected = sch.select_agent([local, cloud], TaskConstraints(capability="text.qa"))
    assert selected is not None
    assert selected[0] is local


def test_score_free_beats_paid_when_local_equal() -> None:
    sch = Scheduler(weights=ScoringWeights(privacy=0.0, capability=0.0, latency=0.0, cost=1.0, health=0.0))
    free = make_agent("a", "ollama", ["text.qa"], cost_class="free")
    paid = make_agent("b", "ollama", ["text.qa"], cost_class="paid")
    selected = sch.select_agent([paid, free], TaskConstraints(capability="text.qa"))
    assert selected is not None
    assert selected[0] is free


def test_mandate_tag_override() -> None:
    mandate = Mandate(name="sensitive", when_tags=["confidential"], enforce_locality="local")
    sch = Scheduler(mandates=[mandate])
    local = make_agent("a", "ollama", ["text.qa"], locality="local")
    cloud = make_agent("b", "anthropic", ["text.qa"], locality="cloud-routed")
    selected = sch.select_agent(
        [cloud, local],
        TaskConstraints(capability="text.qa", locality=Locality.ANY, tags=["confidential"]),
    )
    assert selected is not None
    assert selected[0] is local


def test_devicehealth_low_battery_reduces_score() -> None:
    full = DeviceHealth(device_did="d1", battery_pct=80.0, charging=False)
    low = DeviceHealth(device_did="d2", battery_pct=5.0, charging=False)
    assert low.health_score < full.health_score
    assert low.health_score <= 0.2


def test_yaml_mandates_roundtrip() -> None:
    yaml_text = """
- name: night-local-only
  when_time: "20:00-06:00"
  enforce_locality: local
- name: default
  is_default: true
  enforce_cost_class: metered
"""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "mandates.yaml"
        path.write_text(yaml_text, encoding="utf-8")
        mandates = load_mandates_from_yaml(path)
    assert len(mandates) == 2
    assert mandates[0].name == "night-local-only"
    assert mandates[1].is_default
    assert mandates[1].enforce_cost_class == "metered"

"""Four-stage task scheduler: Filter → Mandate → Score → Select.

Spec reference: §10. YAML mandate loader added per plan.
"""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml

from ..identity.household import AgentRecord


# ---------------------------------------------------------------------------
# Enums and dataclasses
# ---------------------------------------------------------------------------

class Locality(str, Enum):
    LOCAL_ONLY = "local-only"
    HOUSEHOLD = "household"
    ANY = "any"


@dataclass
class TaskConstraints:
    capability: str
    locality: Locality = Locality.HOUSEHOLD
    vendor_preference: list[str] = field(default_factory=list)
    vendor_exclude: list[str] = field(default_factory=list)
    min_context_window: int = 0
    max_cost_class: str = "paid"
    max_latency_ms: Optional[int] = None
    tags: list[str] = field(default_factory=list)


@dataclass
class DeviceHealth:
    device_did: str
    cpu_percent: float = 0.0
    ram_available_gb: float = 0.0
    ram_total_gb: float = 0.0
    gpu_utilization: float = 0.0
    vram_available_gb: float = 0.0
    battery_pct: Optional[float] = None
    charging: bool = True
    thermal: str = "nominal"  # nominal | warm | throttled
    network_type: str = "wifi"
    bandwidth_mbps: float = 0.0
    last_updated: float = field(default_factory=time.time)

    @property
    def health_score(self) -> float:
        score = 1.0
        if self.cpu_percent > 80:
            score *= 0.5
        elif self.cpu_percent > 50:
            score *= 0.8
        if self.battery_pct is not None:
            if self.battery_pct < 10 and not self.charging:
                score *= 0.1
            elif self.battery_pct < 30:
                score *= 0.5
            elif self.battery_pct < 50:
                score *= 0.8
        if self.thermal == "throttled":
            score *= 0.3
        elif self.thermal == "warm":
            score *= 0.7
        return score


@dataclass
class ScoringWeights:
    privacy: float = 3.0
    capability: float = 2.0
    latency: float = 1.5
    cost: float = 1.0
    health: float = 1.0


COST_RANK = {"free": 1.0, "metered": 0.5, "paid": 0.2}
LOCALITY_RANK = {"local": 1.0, "cloud-routed": 0.2}
COST_ORDER = ["free", "metered", "paid"]


@dataclass
class Mandate:
    name: str
    when_tags: list[str] = field(default_factory=list)
    when_time: Optional[str] = None  # "HH:MM-HH:MM"
    is_default: bool = False
    enforce_locality: Optional[str] = None
    enforce_cost_class: Optional[str] = None
    enforce_max_tokens: Optional[int] = None
    scoring_overrides: Optional[ScoringWeights] = None


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class Scheduler:
    def __init__(
        self,
        weights: Optional[ScoringWeights] = None,
        mandates: Optional[list[Mandate]] = None,
    ) -> None:
        self.weights = weights or ScoringWeights()
        self.mandates: list[Mandate] = list(mandates or [])
        self._device_health: dict[str, DeviceHealth] = {}

    def update_device_health(self, health: DeviceHealth) -> None:
        self._device_health[health.device_did] = health

    def get_health(self, device_did: str) -> Optional[DeviceHealth]:
        return self._device_health.get(device_did)

    def select_agent(
        self,
        agents: list[AgentRecord],
        constraints: TaskConstraints,
        device_did_map: Optional[dict[str, str]] = None,
    ) -> Optional[tuple[AgentRecord, float]]:
        """Return the best-scoring agent with its score, or None."""
        effective = self._apply_mandates(constraints)
        candidates = self._filter(agents, effective)
        if not candidates:
            return None

        scored: list[tuple[AgentRecord, float]] = []
        for agent in candidates:
            device_did = (device_did_map or {}).get(agent.agent_did)
            health = self._device_health.get(device_did) if device_did else None
            score = self._score(agent, effective, health)
            scored.append((agent, score))
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[0]

    def _filter(
        self,
        agents: list[AgentRecord],
        constraints: TaskConstraints,
    ) -> list[AgentRecord]:
        out: list[AgentRecord] = []
        max_cost_idx = COST_ORDER.index(constraints.max_cost_class)
        for agent in agents:
            if constraints.capability not in agent.capabilities:
                continue
            if constraints.locality is Locality.LOCAL_ONLY and agent.locality != "local":
                continue
            if constraints.vendor_preference and agent.vendor not in constraints.vendor_preference:
                continue
            if agent.vendor in constraints.vendor_exclude:
                continue
            if agent.context_window < constraints.min_context_window:
                continue
            if agent.cost_class not in COST_ORDER:
                continue
            if COST_ORDER.index(agent.cost_class) > max_cost_idx:
                continue
            out.append(agent)
        return out

    def _score(
        self,
        agent: AgentRecord,
        constraints: TaskConstraints,
        health: Optional[DeviceHealth],
    ) -> float:
        w = self.weights
        privacy = LOCALITY_RANK.get(agent.locality, 0.5)
        capability = min(agent.context_window / 200_000.0, 1.0) if agent.context_window else 0.3
        cost = COST_RANK.get(agent.cost_class, 0.2)
        health_score = health.health_score if health else 0.7
        latency = 1.0 if agent.locality == "local" else 0.5

        if constraints.vendor_preference and agent.vendor in constraints.vendor_preference:
            capability = min(capability + 0.1, 1.0)

        total = (
            w.privacy * privacy
            + w.capability * capability
            + w.latency * latency
            + w.cost * cost
            + w.health * health_score
        )
        denom = w.privacy + w.capability + w.latency + w.cost + w.health
        return total / denom if denom else 0.0

    def _apply_mandates(self, constraints: TaskConstraints) -> TaskConstraints:
        effective = copy.deepcopy(constraints)
        for mandate in self.mandates:
            if not self._mandate_applies(mandate, constraints):
                continue
            if mandate.enforce_locality:
                if mandate.enforce_locality == "local":
                    effective.locality = Locality.LOCAL_ONLY
                elif mandate.enforce_locality == "household":
                    effective.locality = Locality.HOUSEHOLD
                elif mandate.enforce_locality == "any":
                    effective.locality = Locality.ANY
            if mandate.enforce_cost_class:
                effective.max_cost_class = mandate.enforce_cost_class
            if mandate.scoring_overrides is not None:
                self.weights = mandate.scoring_overrides
        return effective

    def _mandate_applies(self, mandate: Mandate, constraints: TaskConstraints) -> bool:
        if mandate.is_default:
            return True
        if mandate.when_tags and any(t in constraints.tags for t in mandate.when_tags):
            return True
        if mandate.when_time:
            return self._is_within_time_window(mandate.when_time, datetime.now())
        return False

    @staticmethod
    def _is_within_time_window(window: str, now: datetime) -> bool:
        try:
            start_s, end_s = window.split("-")
            start_h, start_m = (int(x) for x in start_s.split(":"))
            end_h, end_m = (int(x) for x in end_s.split(":"))
        except (ValueError, AttributeError):
            return False
        cur = now.hour * 60 + now.minute
        start = start_h * 60 + start_m
        end = end_h * 60 + end_m
        if start <= end:
            return start <= cur <= end
        # overnight window
        return cur >= start or cur <= end


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------

def load_mandates_from_yaml(path: str | Path) -> list[Mandate]:
    p = Path(path).expanduser()
    if not p.exists():
        return []
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or []
    if not isinstance(raw, list):
        raise ValueError(f"Mandate file {p} must be a YAML list")
    mandates: list[Mandate] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        scoring = entry.get("scoring_overrides")
        if scoring is not None:
            scoring = ScoringWeights(**scoring)
        mandates.append(
            Mandate(
                name=entry.get("name", "unnamed"),
                when_tags=list(entry.get("when_tags", [])),
                when_time=entry.get("when_time"),
                is_default=bool(entry.get("is_default", False)),
                enforce_locality=entry.get("enforce_locality"),
                enforce_cost_class=entry.get("enforce_cost_class"),
                enforce_max_tokens=entry.get("enforce_max_tokens"),
                scoring_overrides=scoring,
            )
        )
    return mandates

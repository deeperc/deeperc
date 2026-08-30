"""
Tier 1.6 — Passive-Aware Net Topology Traversal

Deterministic single-hop traversal that classifies unclassified power-bearing
nets as derived_from(rail) when they reach a confirmed rail through a single
series passive (inductor, resistor, diode, or 0Ω jumper).

No LLM calls, no datasheet lookups. Pure netlist topology.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from steps.net_name_utils import normalize_net_name


# ── Power-pin function names ───────────────────────────────────────────────────
# Extend this set as new vendor naming conventions are encountered.

POWER_PIN_FUNCTIONS = {
    "VCC", "VDD", "VCCA", "VCCB", "VCCO", "VCCIO", "VCCINT",
    "AVCC", "AVDD", "DVCC", "DVDD", "PVCC", "PVDD",
    "IOVCC", "IOVDD", "COREVDD",
    "VBAT", "VIN", "V+", "VCC1", "VCC2",
    "VDDA", "VDDD", "VDDIO", "VDDH", "VDDL", "VBUS",
}

_TRAILING_DIGITS_RE = re.compile(r"\d+$")


def _is_power_pin_name(pin_name: str) -> bool:
    """True if a pin name is a recognized power-pin function, tolerating numbered
    variants (AVCC1, VDD2, VCCIO3 …). Many MCUs split one rail across numbered
    pins (e.g. ATMEGA AVCC + AVCC1); without this, the second pin reads as a
    non-power signal and aborts the whole net's traversal.
    """
    if not pin_name:
        return False
    name = pin_name.upper().strip()
    if name in POWER_PIN_FUNCTIONS:
        return True
    return _TRAILING_DIGITS_RE.sub("", name) in POWER_PIN_FUNCTIONS


# ── Data classes ──────────────────────────────────────────────────────────────

class Confidence(Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class BridgeType(Enum):
    RESISTOR = "resistor"   # non-zero series resistor
    INDUCTOR = "inductor"   # inductor or ferrite bead (refdes L or FB)
    DIODE = "diode"
    JUMPER = "jumper"       # 0Ω resistor


class SideBranchRole(Enum):
    BYPASS_CAP = "bypass_cap"
    RAIL_CONSUMER = "rail_consumer"
    OTHER = "other"


@dataclass
class ComponentRef:
    refdes: str
    bridge_type: BridgeType
    value: str


@dataclass
class SideBranch:
    refdes: str
    pin_id: str
    role: SideBranchRole


@dataclass
class DerivedSource:
    rail: str
    voltage: float | None
    confidence: Confidence
    reason: str
    bridge: ComponentRef


@dataclass
class DerivationSourceRecord:
    rail: str
    bridge: ComponentRef
    confidence: Confidence
    reason: str


@dataclass
class DerivationRecord:
    net: str
    sources: list[DerivationSourceRecord]
    side_branches: list[SideBranch]
    was_downgraded: bool = False


# ── Internal sentinel — means "caller should reject this bridge" ───────────────

_REJECT = object()


# ── Bridge classification ──────────────────────────────────────────────────────

_K_STYLE_RE = re.compile(r'^(\d+)[kK](\d+)$')
_EIA_R_RE   = re.compile(r'^(\d+)[Rr](\d+)$')
_FLOAT_RE   = re.compile(r'^[\d.]+$')

_CONFIDENCE_ORDER = [Confidence.HIGH, Confidence.MEDIUM, Confidence.LOW]


def parse_resistor_value(value_str: str) -> Optional[float]:
    """Parse a resistor value string to ohms. Returns None if unparseable."""
    if not value_str:
        return None
    s = value_str.strip()
    s = s.rstrip('Ω')
    if not s:
        return None

    # "4k7" style: integer + k/K + digits  → 4.7 kΩ = 4700 Ω
    m = _K_STYLE_RE.match(s)
    if m:
        integer_part = int(m.group(1))
        frac_str = m.group(2)
        frac = int(frac_str) / (10 ** len(frac_str))
        return (integer_part + frac) * 1000.0

    # EIA "R as decimal point": "0R0" = 0 Ω, "1R5" = 1.5 Ω
    m = _EIA_R_RE.match(s)
    if m:
        try:
            return float(f"{m.group(1)}.{m.group(2)}")
        except ValueError:
            return None

    # Suffix-based multiplier
    multiplier = 1.0
    s_lower = s.lower()
    if s_lower.endswith('k'):
        multiplier = 1_000.0
        s = s[:-1]
    elif s_lower.endswith('m'):
        multiplier = 1_000_000.0
        s = s[:-1]
    elif s_lower.endswith('r'):
        multiplier = 1.0
        s = s[:-1]

    if not s:
        return None
    try:
        return float(s) * multiplier
    except ValueError:
        return None


def classify_component(component) -> Optional[ComponentRef]:
    """Return ComponentRef if component can act as a single-hop passive bridge.

    Permitted: R (resistor/jumper), L (inductor), FB (ferrite bead), D (diode).
    Never: C, U, IC, J, Q, F, K, SW, TP, and any other non-passive.
    """
    refdes: str = component.refdes
    if not refdes or refdes.startswith("#"):
        return None

    upper = refdes.upper()

    if upper.startswith("FB"):
        return ComponentRef(refdes=refdes, bridge_type=BridgeType.INDUCTOR, value=component.value)

    if upper.startswith("L"):
        return ComponentRef(refdes=refdes, bridge_type=BridgeType.INDUCTOR, value=component.value)

    if upper.startswith("D"):
        return ComponentRef(refdes=refdes, bridge_type=BridgeType.DIODE, value=component.value)

    if upper.startswith("R"):
        val = parse_resistor_value(component.value)
        bridge_type = BridgeType.JUMPER if (val is not None and val == 0.0) else BridgeType.RESISTOR
        return ComponentRef(refdes=refdes, bridge_type=bridge_type, value=component.value)

    return None


def confidence_for_bridge(component_ref: ComponentRef):
    """Return (Confidence, reason) or (_REJECT, reason) when the bridge is too lossy."""
    bt = component_ref.bridge_type

    if bt == BridgeType.JUMPER:
        return Confidence.HIGH, "zero-ohm jumper — lossless"

    if bt == BridgeType.INDUCTOR:
        return Confidence.HIGH, "inductor — lossless"

    if bt == BridgeType.DIODE:
        return Confidence.MEDIUM, "diode — ~0.3-0.7V drop"

    if bt == BridgeType.RESISTOR:
        val = parse_resistor_value(component_ref.value)
        if val is None:
            return Confidence.MEDIUM, "resistor with unknown value"
        if val < 100.0:
            return Confidence.HIGH, "small series resistor — voltage-equivalent"
        if val <= 1000.0:
            return Confidence.LOW, "series resistor — voltage drop possible at higher loads"
        return _REJECT, f"series resistor too large ({val:.0f}Ω) — not a power bridge"

    return _REJECT, "unknown bridge type"


# ── Traversal internals ────────────────────────────────────────────────────────

def _has_ic_power_pin(net, components_by_refdes: dict) -> bool:
    """True if any pin on the net has a recognized power-pin function name."""
    for (refdes, pin_id) in net.pins:
        if refdes.startswith("#"):
            continue
        comp = components_by_refdes.get(refdes)
        if comp is None:
            continue
        for pin in comp.pins:
            if pin.pin_id == pin_id:
                if _is_power_pin_name(pin.pin_name):
                    return True
                break
    return False


def _opposite_pin_net(component, this_pin_id: str) -> Optional[str]:
    """For a 2-pin component, return the net on the other pin. None otherwise."""
    if len(component.pins) != 2:
        return None
    for pin in component.pins:
        if pin.pin_id != this_pin_id:
            return pin.net
    return None


def _classify_side_branches(
    net,
    bridge_candidate_pins: set,
    components_by_refdes: dict,
    normalized_ground: set,
) -> tuple[list[SideBranch], bool, bool]:
    """Classify all non-bridge pins on the net.

    Returns (side_branches, should_downgrade, should_reject).
    should_reject  — a non-passive signal pin is on this net; traversal is unsafe
    should_downgrade — an unrelated passive is on this net; demote confidence one level
    """
    side_branches: list[SideBranch] = []
    should_downgrade = False
    should_reject = False

    for (refdes, pin_id) in net.pins:
        if (refdes, pin_id) in bridge_candidate_pins:
            continue
        if refdes.startswith("#"):
            continue
        comp = components_by_refdes.get(refdes)
        if comp is None:
            continue

        pin_name = ""
        for pin in comp.pins:
            if pin.pin_id == pin_id:
                pin_name = pin.pin_name
                break

        opp_net = _opposite_pin_net(comp, pin_id)
        if opp_net is not None and normalize_net_name(opp_net) in normalized_ground:
            side_branches.append(SideBranch(refdes=refdes, pin_id=pin_id, role=SideBranchRole.BYPASS_CAP))
            continue

        if _is_power_pin_name(pin_name):
            side_branches.append(SideBranch(refdes=refdes, pin_id=pin_id, role=SideBranchRole.RAIL_CONSUMER))
            continue

        side_branches.append(SideBranch(refdes=refdes, pin_id=pin_id, role=SideBranchRole.OTHER))
        if classify_component(comp) is not None:
            should_downgrade = True
        else:
            should_reject = True

    return side_branches, should_downgrade, should_reject


def _build_derived_sources(
    candidates: list[tuple],
) -> list[DerivedSource]:
    """Collapse candidates by rail; pick highest confidence per rail."""
    by_rail: dict[str, list] = {}
    for (rail, voltage, bridge_ref, confidence, reason) in candidates:
        by_rail.setdefault(rail, []).append((voltage, bridge_ref, confidence, reason))

    result = []
    for rail, entries in by_rail.items():
        best = min(entries, key=lambda e: _CONFIDENCE_ORDER.index(e[2]))
        voltage, bridge_ref, confidence, reason = best
        result.append(DerivedSource(
            rail=rail,
            voltage=voltage,
            confidence=confidence,
            reason=reason,
            bridge=bridge_ref,
        ))
    return result


def _build_record(
    net_name: str,
    candidates: list[tuple],
    side_branches: list[SideBranch],
    was_downgraded: bool = False,
) -> DerivationRecord:
    sources = [
        DerivationSourceRecord(rail=rail, bridge=bridge_ref, confidence=confidence, reason=reason)
        for (rail, _voltage, bridge_ref, confidence, reason) in candidates
    ]
    return DerivationRecord(
        net=net_name,
        sources=sources,
        side_branches=side_branches,
        was_downgraded=was_downgraded,
    )


# ── Main entry point ───────────────────────────────────────────────────────────

def traverse_passive_bridges(
    netlist,
    confirmed_voltages: dict,
    ground_nets: list,
) -> tuple[dict[str, list[DerivedSource]], dict[str, DerivationRecord]]:
    """Classify unclassified power-bearing nets that reach a confirmed rail through
    a single passive bridge component.

    Parameters
    ----------
    netlist : duck-typed object with .components (list) and .nets (list of objects
              with .name and .pins as list[tuple[str,str]] = [(refdes, pin_id)...])
    confirmed_voltages : dict[str, float | None] — net name → voltage
    ground_nets : list[str] — net names classified as ground

    Returns
    -------
    derived_by_net : dict[str, list[DerivedSource]]
        Non-empty list means the net is derived from one or more confirmed rails.
    records_by_net : dict[str, DerivationRecord]
        Full audit trail for each derived net.
    """
    components_by_refdes = {c.refdes: c for c in netlist.components}
    normalized_cv = {normalize_net_name(n) for n in confirmed_voltages}
    normalized_ground = {normalize_net_name(n) for n in ground_nets}

    derived_by_net: dict[str, list[DerivedSource]] = {}
    records_by_net: dict[str, DerivationRecord] = {}

    for net in netlist.nets:
        net_norm = normalize_net_name(net.name)

        # Skip already-classified nets
        if net_norm in normalized_cv or net_norm in normalized_ground:
            continue

        # Only traverse nets that contain an IC power pin
        if not _has_ic_power_pin(net, components_by_refdes):
            continue

        candidates: list[tuple] = []
        bridge_candidate_pins: set[tuple[str, str]] = set()

        for (refdes, pin_id) in net.pins:
            if refdes.startswith("#"):
                continue
            comp = components_by_refdes.get(refdes)
            if comp is None:
                continue

            bridge_ref = classify_component(comp)
            if bridge_ref is None:
                continue

            opp_net = _opposite_pin_net(comp, pin_id)
            if opp_net is None:
                continue
            opp_norm = normalize_net_name(opp_net)

            if opp_norm == net_norm:
                continue  # self-loop
            if opp_norm in normalized_ground:
                continue  # bypass cap to GND — not a bridge

            if opp_norm not in normalized_cv:
                continue  # other side is not a confirmed rail

            confidence, reason = confidence_for_bridge(bridge_ref)
            if confidence is _REJECT:
                continue

            # Resolve rail name and voltage
            if opp_net in confirmed_voltages:
                rail_name = opp_net
                rail_voltage = confirmed_voltages[opp_net]
            else:
                rail_name = opp_net
                rail_voltage = None
                for n, v in confirmed_voltages.items():
                    if normalize_net_name(n) == opp_norm:
                        rail_name = n
                        rail_voltage = v
                        break

            candidates.append((rail_name, rail_voltage, bridge_ref, confidence, reason))
            bridge_candidate_pins.add((refdes, pin_id))

        if not candidates:
            continue

        side_branches, should_downgrade, should_reject = _classify_side_branches(
            net, bridge_candidate_pins, components_by_refdes, normalized_ground
        )

        if should_reject:
            continue

        if should_downgrade:
            downgraded = []
            for (rail, voltage, bridge_ref, conf, reason) in candidates:
                idx = _CONFIDENCE_ORDER.index(conf)
                new_conf = _CONFIDENCE_ORDER[min(idx + 1, len(_CONFIDENCE_ORDER) - 1)]
                downgraded.append((rail, voltage, bridge_ref, new_conf, reason))
            candidates = downgraded

        derived_by_net[net.name] = _build_derived_sources(candidates)
        records_by_net[net.name] = _build_record(
            net.name, candidates, side_branches, was_downgraded=should_downgrade
        )

    return derived_by_net, records_by_net

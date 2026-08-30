"""Tests for Tier 1.6 — Passive-Aware Net Topology Traversal.

Fixture types (PinRef, Component, Net, Netlist) are defined inline and are
duck-type-compatible with the production ComponentIR/PinIR/NetIR/NetlistIR.
"""
import sys
import os
import pytest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from steps.passive_traversal import (
    traverse_passive_bridges,
    Confidence,
    BridgeType,
    SideBranchRole,
    ComponentRef,
    SideBranch,
    DerivedSource,
    DerivationSourceRecord,
    DerivationRecord,
)


# ── Minimal fixture types (duck-type-compatible with NetlistIR) ───────────────

@dataclass
class PinRef:
    pin_id: str
    net: str
    pin_name: str = ""


@dataclass
class Component:
    refdes: str
    value: str
    pins: list


@dataclass
class Net:
    name: str
    pins: list   # list[tuple[str, str]] = [(refdes, pin_id), ...]


@dataclass
class Netlist:
    components: list
    nets: list


# ── Test 1: ESP32-PoE2 SD card — real corpus fixture ─────────────────────────

def test_esp32_poe2_sd_card():
    """Net-(MICRO_SD1-VDD) is derived from +3.3V via ferrite bead L2."""
    fixture = Path(__file__).parent / "fixtures" / "esp32_poe2_sd_fragment.net"
    assert fixture.exists(), f"Fixture not found: {fixture}"

    from steps.step_02_parser import parse_netlist
    ir = parse_netlist(str(fixture))

    confirmed_voltages = {"+3.3V": 3.3}
    ground_nets = ["GND"]

    derived, records = traverse_passive_bridges(ir, confirmed_voltages, ground_nets)

    assert "Net-(MICRO_SD1-VDD)" in derived, (
        f"Expected Net-(MICRO_SD1-VDD) in derived_by_net; got keys: {list(derived.keys())}"
    )
    sources = derived["Net-(MICRO_SD1-VDD)"]
    assert len(sources) == 1
    src = sources[0]
    assert src.rail == "+3.3V"
    assert src.confidence == Confidence.HIGH
    assert src.bridge.bridge_type == BridgeType.INDUCTOR
    assert src.bridge.refdes == "L2"

    rec = records["Net-(MICRO_SD1-VDD)"]
    assert not rec.was_downgraded
    bypass_roles = {sb.role for sb in rec.side_branches}
    assert SideBranchRole.BYPASS_CAP in bypass_roles  # C7 bypass cap


# ── Test 2: Empty netlist — vacuous pass ──────────────────────────────────────

def test_empty_netlist():
    nl = Netlist(components=[], nets=[])
    derived, records = traverse_passive_bridges(nl, {}, [])
    assert derived == {}
    assert records == {}


# ── Test 3: Inductor bridge → HIGH confidence ─────────────────────────────────

def test_inductor_bridge():
    u1 = Component("U1", "some_ic", [PinRef("1", "Net-X", "VDD")])
    l1 = Component("L1", "100uH", [PinRef("1", "RAIL", "1"), PinRef("2", "Net-X", "2")])

    net_x = Net("Net-X", [("U1", "1"), ("L1", "2")])
    rail_net = Net("RAIL", [("L1", "1")])
    nl = Netlist([u1, l1], [rail_net, net_x])

    derived, records = traverse_passive_bridges(nl, {"RAIL": 3.3}, [])

    assert "Net-X" in derived
    src = derived["Net-X"]
    assert len(src) == 1
    assert src[0].rail == "RAIL"
    assert src[0].voltage == 3.3
    assert src[0].confidence == Confidence.HIGH
    assert src[0].bridge.bridge_type == BridgeType.INDUCTOR
    assert src[0].bridge.refdes == "L1"
    assert not records["Net-X"].was_downgraded


# ── Test 4: Diode bridge → MEDIUM confidence ─────────────────────────────────

def test_diode_bridge():
    u1 = Component("U1", "some_ic", [PinRef("1", "Net-X", "VDD")])
    d1 = Component("D1", "1N4148", [PinRef("A", "RAIL", "A"), PinRef("K", "Net-X", "K")])

    net_x = Net("Net-X", [("U1", "1"), ("D1", "K")])
    rail_net = Net("RAIL", [("D1", "A")])
    nl = Netlist([u1, d1], [rail_net, net_x])

    derived, records = traverse_passive_bridges(nl, {"RAIL": 5.0}, [])

    assert "Net-X" in derived
    src = derived["Net-X"]
    assert len(src) == 1
    assert src[0].confidence == Confidence.MEDIUM
    assert src[0].bridge.bridge_type == BridgeType.DIODE


# ── Test 5: Zero-ohm jumper → HIGH confidence ────────────────────────────────

def test_zero_ohm_jumper():
    u1 = Component("U1", "some_ic", [PinRef("1", "Net-X", "VDD")])
    r1 = Component("R1", "0R", [PinRef("1", "RAIL", "1"), PinRef("2", "Net-X", "2")])

    net_x = Net("Net-X", [("U1", "1"), ("R1", "2")])
    rail_net = Net("RAIL", [("R1", "1")])
    nl = Netlist([u1, r1], [rail_net, net_x])

    derived, records = traverse_passive_bridges(nl, {"RAIL": 3.3}, [])

    assert "Net-X" in derived
    src = derived["Net-X"]
    assert len(src) == 1
    assert src[0].confidence == Confidence.HIGH
    assert src[0].bridge.bridge_type == BridgeType.JUMPER


# ── Test 6: Small resistor (<100Ω) → HIGH confidence ─────────────────────────

def test_small_resistor_bridge():
    u1 = Component("U1", "some_ic", [PinRef("1", "Net-X", "VDD")])
    r1 = Component("R1", "10R", [PinRef("1", "RAIL", "1"), PinRef("2", "Net-X", "2")])

    net_x = Net("Net-X", [("U1", "1"), ("R1", "2")])
    rail_net = Net("RAIL", [("R1", "1")])
    nl = Netlist([u1, r1], [rail_net, net_x])

    derived, records = traverse_passive_bridges(nl, {"RAIL": 3.3}, [])

    assert "Net-X" in derived
    src = derived["Net-X"]
    assert len(src) == 1
    assert src[0].confidence == Confidence.HIGH
    assert src[0].bridge.bridge_type == BridgeType.RESISTOR


# ── Test 7: Side-branch passive → confidence downgraded one level ─────────────

def test_side_branch_downgrade():
    # L1 bridges RAIL to Net-X (HIGH normally).
    # R2 is an unrelated passive on Net-X (not to GND, not to a rail) → downgrade.
    u1 = Component("U1", "some_ic", [PinRef("1", "Net-X", "VDD")])
    l1 = Component("L1", "100uH", [PinRef("1", "RAIL", "1"), PinRef("2", "Net-X", "2")])
    r2 = Component("R2", "100k", [PinRef("1", "Net-X", "1"), PinRef("2", "Net-Z", "2")])

    net_x = Net("Net-X", [("U1", "1"), ("L1", "2"), ("R2", "1")])
    rail_net = Net("RAIL", [("L1", "1")])
    net_z = Net("Net-Z", [("R2", "2")])
    nl = Netlist([u1, l1, r2], [rail_net, net_x, net_z])

    derived, records = traverse_passive_bridges(nl, {"RAIL": 3.3}, [])

    assert "Net-X" in derived
    rec = records["Net-X"]
    assert rec.was_downgraded
    src = derived["Net-X"]
    assert len(src) == 1
    # L1 would be HIGH; downgraded one level → MEDIUM
    assert src[0].confidence == Confidence.MEDIUM

    other_branches = [sb for sb in rec.side_branches if sb.role == SideBranchRole.OTHER]
    assert len(other_branches) == 1
    assert other_branches[0].refdes == "R2"


# ── Test 8: Large resistor (>1kΩ) rejected — net not derived ─────────────────

def test_large_resistor_rejected():
    u1 = Component("U1", "some_ic", [PinRef("1", "Net-X", "VDD")])
    r1 = Component("R1", "10k", [PinRef("1", "RAIL", "1"), PinRef("2", "Net-X", "2")])

    net_x = Net("Net-X", [("U1", "1"), ("R1", "2")])
    rail_net = Net("RAIL", [("R1", "1")])
    nl = Netlist([u1, r1], [rail_net, net_x])

    derived, records = traverse_passive_bridges(nl, {"RAIL": 3.3}, [])

    assert "Net-X" not in derived


# ── Test 9: Diode-OR — two rails → two DerivedSources ────────────────────────

def test_diode_or():
    u1 = Component("U1", "some_ic", [PinRef("1", "Net-X", "VDD")])
    d1 = Component("D1", "1N4148", [PinRef("A", "VBAT", "A"), PinRef("K", "Net-X", "K")])
    d2 = Component("D2", "1N4148", [PinRef("A", "VBUS", "A"), PinRef("K", "Net-X", "K")])

    net_x = Net("Net-X", [("U1", "1"), ("D1", "K"), ("D2", "K")])
    vbat_net = Net("VBAT", [("D1", "A")])
    vbus_net = Net("VBUS", [("D2", "A")])
    nl = Netlist([u1, d1, d2], [vbat_net, vbus_net, net_x])

    derived, records = traverse_passive_bridges(nl, {"VBAT": 3.7, "VBUS": 5.0}, [])

    assert "Net-X" in derived
    src = derived["Net-X"]
    assert len(src) == 2
    rails = {s.rail for s in src}
    assert "VBAT" in rails
    assert "VBUS" in rails
    for s in src:
        assert s.confidence == Confidence.MEDIUM


# ── Test 10: Net without IC power pin — not traversed ────────────────────────

def test_no_ic_power_pin_skipped():
    # U1 has a signal pin (PA0) on Net-Y, not a power pin.
    u1 = Component("U1", "some_ic", [PinRef("1", "Net-Y", "PA0")])
    l1 = Component("L1", "100uH", [PinRef("1", "RAIL", "1"), PinRef("2", "Net-Y", "2")])

    net_y = Net("Net-Y", [("U1", "1"), ("L1", "2")])
    rail_net = Net("RAIL", [("L1", "1")])
    nl = Netlist([u1, l1], [rail_net, net_y])

    derived, records = traverse_passive_bridges(nl, {"RAIL": 3.3}, [])

    assert "Net-Y" not in derived

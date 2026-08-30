"""Tests for the Phase-1 cross-net bus pairing layer (peripheral_bus_pairing.py).

VERDICT-INERT: this module pairs an SDA net with its SCL net into one bus
object. It emits no findings; these tests only exercise pairing, not any
checker/verdict path. See recon `investigation/experiments/pin_capability_recon/
REPORT.md` (Q1/Q3) for the gap this fills.
"""
import os
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from steps.peripheral_kb import Signal, KBSource, Peripheral, PinRole, PinFunctionEntry
from steps.peripheral_coherence import I2C_PAIR, _KB_SIGNAL_TO_ROLE
from steps.peripheral_bus_pairing import (
    pair_buses, PairedBus, UnpairedNet, AMBIGUOUS_PAIRING, _stem,
)
from steps.peripheral_roles import Role


# ── minimal fixture types (mirrors test_peripheral_checker_i2c.py) ───────────

@dataclass
class PinRef:
    pin_id:   str
    net:      str
    pin_name: str = ""


@dataclass
class Component:
    refdes: str
    mpn:    str
    pins:   list
    @property
    def effective_mpn(self): return self.mpn


@dataclass
class Net:
    name: str
    pins: list


@dataclass
class Netlist:
    components: list
    nets:       list


def _make_kb(*entries: PinFunctionEntry) -> dict:
    return {(e.mpn, e.pin_id): e for e in entries}


def _stm32_i2c1_entries(mpn="STM32F103C8T6"):
    return (
        PinFunctionEntry(mpn, "PB6", [PinRole(Peripheral.I2C, "I2C1", Signal.I2C_SCL, KBSource.VENDOR_XML)]),
        PinFunctionEntry(mpn, "PB7", [PinRole(Peripheral.I2C, "I2C1", Signal.I2C_SDA, KBSource.VENDOR_XML)]),
    )


# ── signal 1: KB instance ─────────────────────────────────────────────────────

def test_kb_instance_pairs_sda_scl_nets():
    kb = _make_kb(*_stm32_i2c1_entries())
    nl = Netlist(
        components=[Component("U1", "STM32F103C8T6", [
            PinRef("PB6", "SCL_NET", "PB6"), PinRef("PB7", "SDA_NET", "PB7"),
        ])],
        nets=[Net("SCL_NET", [("U1", "PB6")]), Net("SDA_NET", [("U1", "PB7")])],
    )
    paired, unpaired = pair_buses(nl, "I2C", I2C_PAIR, kb=kb, peripheral_routing={},
                                   kb_signal_to_role=_KB_SIGNAL_TO_ROLE)
    assert len(paired) == 1
    bus = paired[0]
    assert bus.bus_id == "I2C1"
    assert bus.pairing_source == "kb_instance"
    assert {bus.net_a, bus.net_b} == {"SCL_NET", "SDA_NET"}
    assert unpaired == []


def test_kb_instance_priority_over_name_prefix():
    """A net pair that BOTH KB-instance and name-prefix could pair must not be
    double-emitted — KB instance wins and consumes the nets first."""
    kb = _make_kb(*_stm32_i2c1_entries())
    nl = Netlist(
        components=[Component("U1", "STM32F103C8T6", [
            PinRef("PB6", "/SCL", "PB6"), PinRef("PB7", "/SDA", "PB7"),
        ])],
        nets=[Net("/SCL", [("U1", "PB6")]), Net("/SDA", [("U1", "PB7")])],
    )
    paired, unpaired = pair_buses(nl, "I2C", I2C_PAIR, kb=kb, peripheral_routing={},
                                   kb_signal_to_role=_KB_SIGNAL_TO_ROLE)
    assert len(paired) == 1
    assert paired[0].pairing_source == "kb_instance"


def test_unconnected_net_excluded_from_kb_instance_pairing():
    """Regression: a repeating fixed-mux family (RP2040-style) exposes the same
    (instance, role) on multiple physical pins; an UNUSED one shows up as a
    synthetic 'unconnected-(...)' net and must never become a pairing candidate
    (found empirically on stamp_and_module.net during Phase-1 validation — 81
    spurious pairs from one 2-component board before this filter existed)."""
    kb = _make_kb(
        PinFunctionEntry("MCU", "GPIO0", [PinRole(Peripheral.I2C, "I2C0", Signal.I2C_SDA, KBSource.MANUAL)]),
        PinFunctionEntry("MCU", "GPIO1", [PinRole(Peripheral.I2C, "I2C0", Signal.I2C_SCL, KBSource.MANUAL)]),
        PinFunctionEntry("MCU", "GPIO4", [PinRole(Peripheral.I2C, "I2C0", Signal.I2C_SDA, KBSource.MANUAL)]),
    )
    nl = Netlist(
        components=[Component("U1", "MCU", [
            PinRef("GPIO0", "/SDA", "GPIO0"),
            PinRef("GPIO1", "/SCL", "GPIO1"),
            PinRef("GPIO4", "unconnected-(U1-GPIO4-Pad5)", "GPIO4"),
        ])],
        nets=[
            Net("/SDA", [("U1", "GPIO0")]),
            Net("/SCL", [("U1", "GPIO1")]),
            Net("unconnected-(U1-GPIO4-Pad5)", [("U1", "GPIO4")]),
        ],
    )
    paired, unpaired = pair_buses(nl, "I2C", I2C_PAIR, kb=kb, peripheral_routing={},
                                   kb_signal_to_role=_KB_SIGNAL_TO_ROLE)
    assert len(paired) == 1, f"expected only the real /SDA+/SCL pair; got {paired}"
    assert {paired[0].net_a, paired[0].net_b} == {"/SDA", "/SCL"}


# ── signal 2: net-name prefix ─────────────────────────────────────────────────

# TODO-303 2a: signal-2 guard extension — the module's existing
# _UNCONNECTED_NET_RE (already applied to signal 1 above) is now also applied
# inside the signal-2 name-prefix loop. 'unconnected_SDA'/'unconnected_SCL'
# classify as I2C_DATA/I2C_CLOCK TODAY (no D2 unwrap needed — the underscore
# form already matches role_from_net_name's anchored regex directly), so this
# is a real currently-classifiable shape, not one inert either way.

def test_unconnected_net_excluded_from_name_prefix_pairing():
    # Without the guard this would pair (both bare-stem "(bare)", both
    # role-bearing) — the guard must keep the unconnected-marked net out of
    # nets_by_role_stem/paired entirely, even though its payload is SDA-shaped.
    nl = Netlist(
        components=[
            Component("U1", "SOME_MCU", [PinRef("1", "unconnected_SDA", "SDA")]),
            Component("U2", "SENSOR",   [PinRef("1", "/SCL", "SCL")]),
        ],
        nets=[Net("unconnected_SDA", [("U1", "1")]), Net("/SCL", [("U2", "1")])],
    )
    paired, unpaired = pair_buses(nl, "I2C", I2C_PAIR)
    assert paired == [], f"unconnected-marked net must never enter signal-2 pairing; got {paired}"


def test_connected_role_bearing_net_still_pairs_via_name_prefix():
    # Control: an otherwise-identical CONNECTED net (no unconnected marker)
    # pairs normally — the guard keys strictly on the unconnected marker.
    nl = Netlist(
        components=[
            Component("U1", "SOME_MCU", [PinRef("1", "/SDA", "SDA")]),
            Component("U2", "SENSOR",   [PinRef("1", "/SCL", "SCL")]),
        ],
        nets=[Net("/SDA", [("U1", "1")]), Net("/SCL", [("U2", "1")])],
    )
    paired, unpaired = pair_buses(nl, "I2C", I2C_PAIR)
    assert len(paired) == 1
    assert paired[0].pairing_source == "name_prefix"


def test_name_prefix_pairs_bare_sda_scl():
    nl = Netlist(
        components=[
            Component("U1", "SOME_MCU", [PinRef("1", "/SDA", "SDA")]),
            Component("U2", "SENSOR",   [PinRef("1", "/SCL", "SCL")]),
        ],
        nets=[Net("/SDA", [("U1", "1")]), Net("/SCL", [("U2", "1")])],
    )
    paired, unpaired = pair_buses(nl, "I2C", I2C_PAIR)
    assert len(paired) == 1
    assert paired[0].pairing_source == "name_prefix"
    assert paired[0].bus_id == "(bare)"
    assert unpaired == []


def test_name_prefix_pairs_hierarchical_stem():
    """/I2C_{SYS}.SDA + /I2C_{SYS}.SCL — the real jetson-agx-thor-baseboard shape."""
    nl = Netlist(
        components=[
            Component("U14", "SLB9673", [PinRef("29", "/I2C_{SYS}.SDA", "SDA")]),
            Component("U54", "AT24CS01", [PinRef("6", "/I2C_{SYS}.SCL", "SCL")]),
        ],
        nets=[Net("/I2C_{SYS}.SDA", [("U14", "29")]), Net("/I2C_{SYS}.SCL", [("U54", "6")])],
    )
    paired, unpaired = pair_buses(nl, "I2C", I2C_PAIR)
    assert len(paired) == 1
    assert paired[0].bus_id == "/I2C_{SYS}"


def test_trailing_qualifier_stem_pairs_sfp():
    """Phase-2b 2b-2 fix — role-anchored stem. `/SFP/SCL_SFP` + `/SFP/SDA_SFP`
    carry a trailing qualifier AFTER the role token; the legacy last-token strip
    yielded distinct stems (`/SFP/SCL` vs `/SFP/SDA`) and never paired (the missed
    jetson SFP bus, J12+U73). Anchoring the cut to the role keyword pairs them."""
    nl = Netlist(
        components=[
            Component("J12", "SFP_CONN", [PinRef("1", "/SFP/SCL_SFP", "SCL")]),
            Component("U73", "SFP_MOD",  [PinRef("1", "/SFP/SDA_SFP", "SDA")]),
        ],
        nets=[Net("/SFP/SCL_SFP", [("J12", "1")]), Net("/SFP/SDA_SFP", [("U73", "1")])],
    )
    paired, unpaired = pair_buses(nl, "I2C", I2C_PAIR)
    assert len(paired) == 1, f"expected the SFP pair; got {paired}"
    assert paired[0].pairing_source == "name_prefix"
    assert {paired[0].net_a, paired[0].net_b} == {"/SFP/SCL_SFP", "/SFP/SDA_SFP"}
    assert unpaired == []


def test_stem_keeps_distinct_instances_apart_after_fix():
    """The role-anchored stem must NOT collapse different instances: `I2C1_SDA` and
    `I2C2_SCL` still get distinct stems (regression guard on the 2b-2 fix)."""
    assert _stem("I2C1_SDA", Role.I2C_DATA) != _stem("I2C2_SCL", Role.I2C_CLOCK)
    # bare and hierarchical bus_id shapes preserved
    assert _stem("/SCL", Role.I2C_CLOCK) == ""
    assert _stem("/I2C_{SYS}.SCL", Role.I2C_CLOCK) == "/I2C_{SYS}"


def test_kb_instance_ambiguous_mux_bucketed_not_crossproduct():
    """Phase-2b 2b-2 fix — RP2040 mux over-generation. One KB instance is capable on
    TWO wired nets per role (SCL on {/SCL,/GP1}, SDA on {/SDA,/GP0}); the old code
    cross-producted 4 phantom buses. Now the instance is bucketed AMBIGUOUS_PAIRING
    (report-only, nets NOT consumed) and the cleanly NET-NAME'd real bus /SCL+/SDA
    is recovered by name-prefix pairing."""
    kb = _make_kb(
        PinFunctionEntry("MUXMCU", "GPIO_SCLa", [PinRole(Peripheral.I2C, "I2C0", Signal.I2C_SCL, KBSource.MANUAL)]),
        PinFunctionEntry("MUXMCU", "GPIO_SCLb", [PinRole(Peripheral.I2C, "I2C0", Signal.I2C_SCL, KBSource.MANUAL)]),
        PinFunctionEntry("MUXMCU", "GPIO_SDAa", [PinRole(Peripheral.I2C, "I2C0", Signal.I2C_SDA, KBSource.MANUAL)]),
        PinFunctionEntry("MUXMCU", "GPIO_SDAb", [PinRole(Peripheral.I2C, "I2C0", Signal.I2C_SDA, KBSource.MANUAL)]),
    )
    nl = Netlist(
        components=[Component("U1", "MUXMCU", [
            PinRef("GPIO_SCLa", "/SCL", "GPIO_SCLa"), PinRef("GPIO_SCLb", "/GP1", "GPIO_SCLb"),
            PinRef("GPIO_SDAa", "/SDA", "GPIO_SDAa"), PinRef("GPIO_SDAb", "/GP0", "GPIO_SDAb"),
        ])],
        nets=[Net("/SCL", [("U1", "GPIO_SCLa")]), Net("/GP1", [("U1", "GPIO_SCLb")]),
              Net("/SDA", [("U1", "GPIO_SDAa")]), Net("/GP0", [("U1", "GPIO_SDAb")])],
    )
    paired, unpaired = pair_buses(nl, "I2C", I2C_PAIR, kb=kb, peripheral_routing={},
                                   kb_signal_to_role=_KB_SIGNAL_TO_ROLE)
    ambiguous = [b for b in paired if b.pairing_source == AMBIGUOUS_PAIRING]
    real = [b for b in paired if b.pairing_source == "name_prefix"]
    kb_pairs = [b for b in paired if b.pairing_source == "kb_instance"]
    assert len(ambiguous) == 1, f"expected 1 ambiguous bucket; got {paired}"
    assert kb_pairs == [], "ambiguous instance must not also emit kb_instance cross-product"
    assert len(real) == 1 and {real[0].net_a, real[0].net_b} == {"/SCL", "/SDA"}, \
        "the cleanly-named /SCL+/SDA bus must survive via name-prefix"


def test_name_prefix_does_not_cross_pair_different_instances():
    """I2C1_SDA must not pair with I2C2_SCL — distinct stems, distinct buses."""
    nl = Netlist(
        components=[
            Component("U1", "MCU", [
                PinRef("1", "I2C1_SDA", "SDA1"), PinRef("2", "I2C2_SCL", "SCL2"),
            ]),
        ],
        nets=[Net("I2C1_SDA", [("U1", "1")]), Net("I2C2_SCL", [("U1", "2")])],
    )
    paired, unpaired = pair_buses(nl, "I2C", I2C_PAIR)
    assert paired == []
    assert {u.net for u in unpaired} == {"I2C1_SDA", "I2C2_SCL"}


# ── unpaired visibility ────────────────────────────────────────────────────────

def test_lone_sda_net_reported_unpaired_not_dropped():
    nl = Netlist(
        components=[Component("U1", "MCU", [PinRef("1", "/SDA", "SDA")])],
        nets=[Net("/SDA", [("U1", "1")])],
    )
    paired, unpaired = pair_buses(nl, "I2C", I2C_PAIR)
    assert paired == []
    assert len(unpaired) == 1
    assert unpaired[0].net == "/SDA"
    assert unpaired[0].members == ["U1"]


def test_pairing_disabled_without_kb_falls_back_to_name_prefix_only():
    """Omitting kb/kb_signal_to_role must not raise — signal 1 simply contributes
    nothing (correct behavior for SPI/UART today, which have no KB signal source
    per recon Q2)."""
    nl = Netlist(
        components=[
            Component("U1", "MCU", [PinRef("1", "/SDA", "SDA")]),
            Component("U2", "SENSOR", [PinRef("1", "/SCL", "SCL")]),
        ],
        nets=[Net("/SDA", [("U1", "1")]), Net("/SCL", [("U2", "1")])],
    )
    paired, unpaired = pair_buses(nl, "I2C", I2C_PAIR)  # no kb kwargs at all
    assert len(paired) == 1
    assert paired[0].pairing_source == "name_prefix"

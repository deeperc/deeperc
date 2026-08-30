"""Tests for the Phase-1 unified I2C consensus evaluation (peripheral_consensus.py).

VERDICT-INERT: classifies bus members (AGREES/MINORITY_CONTRADICTS/INCAPABLE/
SILENT/UNCONSTRAINED/ASSERTS_NEITHER) and a bus-level verdict-that-WOULD-fire.
Emits no finding into any checker/report path. See recon
`investigation/experiments/pin_capability_recon/REPORT.md` Q5 for the boundary
rules this test suite locks in.
"""
import os
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from steps.peripheral_kb import Signal, KBSource, Peripheral, PinRole, PinFunctionEntry, PeripheralRouting
from steps.peripheral_coherence import I2C_PAIR, _KB_SIGNAL_TO_ROLE
from steps.peripheral_roles import Role
from steps.peripheral_bus_pairing import PairedBus
from steps.peripheral_consensus import (
    evaluate_bus_consensus, m6_flagged_pins, MemberClass, BusVerdict,
)


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


def _bus(net_a="SCL_NET", net_b="SDA_NET", bus_id="I2C1", source="kb_instance"):
    return PairedBus(protocol="I2C", bus_id=bus_id, pairing_source=source,
                      net_a=net_a, net_b=net_b)


def _sensor_entry(mpn, pin_id, signal, instance=None):
    return PinFunctionEntry(mpn, pin_id, [PinRole(Peripheral.I2C, instance, signal, KBSource.VENDOR_XML)])


# ── clean multi-member bus ────────────────────────────────────────────────────

def test_clean_bus_all_agree():
    kb = _make_kb(
        _sensor_entry("MCU", "P_SCL", Signal.I2C_SCL, "I2C1"),
        _sensor_entry("MCU", "P_SDA", Signal.I2C_SDA, "I2C1"),
        _sensor_entry("EEPROM", "SCL", Signal.I2C_SCL),
        _sensor_entry("EEPROM", "SDA", Signal.I2C_SDA),
    )
    nl = Netlist(
        components=[
            Component("U1", "MCU", [PinRef("P_SCL", "SCL_NET"), PinRef("P_SDA", "SDA_NET")]),
            Component("U2", "EEPROM", [PinRef("SCL", "SCL_NET"), PinRef("SDA", "SDA_NET")]),
        ],
        nets=[Net("SCL_NET", [("U1", "P_SCL"), ("U2", "SCL")]),
              Net("SDA_NET", [("U1", "P_SDA"), ("U2", "SDA")])],
    )
    cons = evaluate_bus_consensus(nl, _bus(), I2C_PAIR, kb=kb, peripheral_routing={},
                                   kb_signal_to_role=_KB_SIGNAL_TO_ROLE)
    assert cons.verdict == BusVerdict.CLEAN.value
    assert {m.classification for m in cons.members} == {MemberClass.AGREES.value}


# ── boundary rule: <2 identified voters -> CONSENSUS_INSUFFICIENT ────────────

def test_single_voter_is_consensus_insufficient():
    kb = _make_kb(_sensor_entry("MCU", "P_SCL", Signal.I2C_SCL, "I2C1"))
    nl = Netlist(
        components=[
            Component("U1", "MCU", [PinRef("P_SCL", "SCL_NET")]),
            Component("U3", "UNKNOWN", [PinRef("1", "SDA_NET")]),  # no MPN identity, no KB
        ],
        nets=[Net("SCL_NET", [("U1", "P_SCL")]), Net("SDA_NET", [("U3", "1")])],
    )
    cons = evaluate_bus_consensus(nl, _bus(), I2C_PAIR, kb=kb, peripheral_routing={},
                                   kb_signal_to_role=_KB_SIGNAL_TO_ROLE)
    assert cons.verdict == BusVerdict.CONSENSUS_INSUFFICIENT.value
    assert MemberClass.MINORITY_CONTRADICTS.value not in {m.classification for m in cons.members}
    assert MemberClass.INCAPABLE.value not in {m.classification for m in cons.members}


# ── boundary rule: matrix/free-mux -> UNCONSTRAINED, never INCAPABLE ─────────

def test_matrix_routed_member_is_unconstrained_not_incapable():
    kb = _make_kb(
        _sensor_entry("MCU", "P_SCL", Signal.I2C_SCL, "I2C1"),
        _sensor_entry("EEPROM", "SDA", Signal.I2C_SDA),
    )
    routing = {"MATRIX_MCU": {Peripheral.I2C: PeripheralRouting.MATRIX}}
    nl = Netlist(
        components=[
            Component("U1", "MATRIX_MCU", [PinRef("GPIO4", "SCL_NET")]),
            Component("U2", "EEPROM", [PinRef("SDA", "SDA_NET")]),
        ],
        nets=[Net("SCL_NET", [("U1", "GPIO4")]), Net("SDA_NET", [("U2", "SDA")])],
    )
    cons = evaluate_bus_consensus(nl, _bus(), I2C_PAIR, kb=kb, peripheral_routing=routing,
                                   kb_signal_to_role=_KB_SIGNAL_TO_ROLE)
    matrix_member = next(m for m in cons.members if m.refdes == "U1")
    assert matrix_member.classification == MemberClass.UNCONSTRAINED.value
    # only 1 real voter (U2) once U1 is unconstrained -> insufficient, not a flag
    assert cons.verdict == BusVerdict.CONSENSUS_INSUFFICIENT.value


# ── boundary rule: ambiguous KB roles -> ASSERTS_NEITHER, not a voter ────────

def test_ambiguous_kb_pin_asserts_neither():
    kb = _make_kb(
        PinFunctionEntry("MCU", "P_REMAP", [
            PinRole(Peripheral.I2C, "I2C1", Signal.I2C_SDA, KBSource.VENDOR_XML),
            PinRole(Peripheral.I2C, "I2C1", Signal.I2C_SCL, KBSource.VENDOR_XML),
        ]),
        _sensor_entry("EEPROM", "SCL", Signal.I2C_SCL),
        _sensor_entry("EEPROM", "SDA", Signal.I2C_SDA),
    )
    nl = Netlist(
        components=[
            Component("U1", "MCU", [PinRef("P_REMAP", "SCL_NET")]),
            Component("U2", "EEPROM", [PinRef("SCL", "SCL_NET"), PinRef("SDA", "SDA_NET")]),
        ],
        nets=[Net("SCL_NET", [("U1", "P_REMAP"), ("U2", "SCL")]), Net("SDA_NET", [("U2", "SDA")])],
    )
    cons = evaluate_bus_consensus(nl, _bus(), I2C_PAIR, kb=kb, peripheral_routing={},
                                   kb_signal_to_role=_KB_SIGNAL_TO_ROLE)
    remap_member = next(m for m in cons.members if m.refdes == "U1")
    assert remap_member.classification == MemberClass.ASSERTS_NEITHER.value
    assert remap_member.asserted_role is None


# ── boundary rule: 1-1 instance tie -> TIE_UNRESOLVABLE, no minority picked ──

def test_instance_tie_is_unresolvable():
    kb = _make_kb(
        _sensor_entry("MCU_A", "P_SDA", Signal.I2C_SDA, "I2C1"),
        _sensor_entry("MCU_B", "P_SDA", Signal.I2C_SDA, "I2C2"),
    )
    nl = Netlist(
        components=[
            Component("U1", "MCU_A", [PinRef("P_SDA", "SDA_NET")]),
            Component("U2", "MCU_B", [PinRef("P_SDA", "SDA_NET")]),
        ],
        nets=[Net("SDA_NET", [("U1", "P_SDA"), ("U2", "P_SDA")]), Net("SCL_NET", [])],
    )
    cons = evaluate_bus_consensus(nl, _bus(), I2C_PAIR, kb=kb, peripheral_routing={},
                                   kb_signal_to_role=_KB_SIGNAL_TO_ROLE)
    assert cons.verdict == BusVerdict.TIE_UNRESOLVABLE.value
    assert MemberClass.MINORITY_CONTRADICTS.value not in {m.classification for m in cons.members}


# ── synthetic INCAPABLE case ───────────────────────────────────────────────────

def test_kb_incapable_pin_flagged_when_voters_sufficient():
    """A KB-known pin whose possible_roles exclude I2C entirely (TIM/GPIO-only,
    the 'plain GPIO wired to SCL' target defect) sits on the SCL net alongside
    2 agreeing real voters -> INCAPABLE, bus WOULD_FAIL."""
    kb = _make_kb(
        _sensor_entry("MCU", "P_SDA", Signal.I2C_SDA, "I2C1"),
        _sensor_entry("EEPROM", "SDA", Signal.I2C_SDA),
        _sensor_entry("EEPROM", "SCL", Signal.I2C_SCL),
        PinFunctionEntry("MCU", "P_WRONG", [
            PinRole(Peripheral.TIM, "TIM4", Signal.TIM_CH, KBSource.VENDOR_XML),
            PinRole(Peripheral.GPIO, None, Signal.GPIO, KBSource.VENDOR_XML),
        ]),
    )
    nl = Netlist(
        components=[
            Component("U1", "MCU", [PinRef("P_SDA", "SDA_NET"), PinRef("P_WRONG", "SCL_NET")]),
            Component("U2", "EEPROM", [PinRef("SDA", "SDA_NET"), PinRef("SCL", "SCL_NET")]),
        ],
        nets=[Net("SDA_NET", [("U1", "P_SDA"), ("U2", "SDA")]),
              Net("SCL_NET", [("U1", "P_WRONG"), ("U2", "SCL")])],
    )
    cons = evaluate_bus_consensus(nl, _bus(), I2C_PAIR, kb=kb, peripheral_routing={},
                                   kb_signal_to_role=_KB_SIGNAL_TO_ROLE)
    assert cons.verdict == BusVerdict.WOULD_FAIL.value
    wrong = next(m for m in cons.members if m.refdes == "U1" and m.pin_id == "P_WRONG")
    assert wrong.classification == MemberClass.INCAPABLE.value


def test_capability_fires_single_mcu_corroborated():
    """Phase-2b spec correction — CAPABILITY path. One MCU: its SDA pin is a correct
    voter, its SCL landed on a KB-incapable pin (GPIO-only). ONE corroborating voter
    (the same device's SDA) is enough: capability is a hard KB fact, fires on a
    single KB'd device, NOT gated by the >=2-device rule. This is the primary
    single-MCU-board target (SCL routed to a pin the MCU can't do I2C on)."""
    kb = _make_kb(
        _sensor_entry("MCU", "P_SDA", Signal.I2C_SDA, "I2C1"),
        PinFunctionEntry("MCU", "P_WRONG", [
            PinRole(Peripheral.GPIO, None, Signal.GPIO, KBSource.VENDOR_XML),
        ]),
    )
    nl = Netlist(
        components=[
            # lone MCU + a non-KB header on both nets (silent, not a voter)
            Component("U1", "MCU", [PinRef("P_SDA", "SDA_NET"), PinRef("P_WRONG", "SCL_NET")]),
            Component("J1", "HEADER", [PinRef("1", "SDA_NET"), PinRef("2", "SCL_NET")]),
        ],
        nets=[Net("SDA_NET", [("U1", "P_SDA"), ("J1", "1")]),
              Net("SCL_NET", [("U1", "P_WRONG"), ("J1", "2")])],
    )
    cons = evaluate_bus_consensus(nl, _bus(), I2C_PAIR, kb=kb, peripheral_routing={},
                                   kb_signal_to_role=_KB_SIGNAL_TO_ROLE)
    assert cons.verdict == BusVerdict.WOULD_FAIL.value
    assert cons.fail_mode == "capability"
    assert cons.distinct_voter_devices == 1        # single-device-sufficient
    wrong = next(m for m in cons.members if m.pin_id == "P_WRONG")
    assert wrong.classification == MemberClass.INCAPABLE.value


def test_kb_incapable_no_corroborating_voter_stands_down():
    """Boundary: a KB-incapable pin but NOTHING on the bus positively asserts this
    protocol (zero voters) -> nothing corroborates it IS an I2C bus, so no capability
    claim. Stand down CONSENSUS_INSUFFICIENT; INCAPABLE must NOT be promoted."""
    kb = _make_kb(
        PinFunctionEntry("MCU", "P_WRONG", [
            PinRole(Peripheral.GPIO, None, Signal.GPIO, KBSource.VENDOR_XML),
        ]),
    )
    nl = Netlist(
        components=[
            Component("U1", "MCU", [PinRef("P_WRONG", "SCL_NET")]),
            Component("J1", "HEADER", [PinRef("1", "SDA_NET")]),  # non-KB, silent
        ],
        nets=[Net("SDA_NET", [("J1", "1")]), Net("SCL_NET", [("U1", "P_WRONG")])],
    )
    cons = evaluate_bus_consensus(nl, _bus(), I2C_PAIR, kb=kb, peripheral_routing={},
                                   kb_signal_to_role=_KB_SIGNAL_TO_ROLE)
    assert cons.verdict == BusVerdict.CONSENSUS_INSUFFICIENT.value
    assert cons.fail_mode is None
    wrong = next(m for m in cons.members if m.refdes == "U1")
    assert wrong.classification != MemberClass.INCAPABLE.value


def test_voter_by_refdes_single_device_two_pins_is_insufficient():
    """Phase-2b spec correction — voter-by-refdes. A lone MCU's OWN complementary
    SDA+SCL pins are TWO pins but ONE device -> not a consensus (self-corroboration).
    A clean single-device bus stands down CONSENSUS_INSUFFICIENT, it does NOT read
    as a 2-voter CLEAN consensus (the old pin-counting bug — 857/863 diagnostic
    buses were exactly this shape)."""
    kb = _make_kb(
        _sensor_entry("MCU", "P_SCL", Signal.I2C_SCL, "I2C1"),
        _sensor_entry("MCU", "P_SDA", Signal.I2C_SDA, "I2C1"),
    )
    nl = Netlist(
        components=[Component("U1", "MCU", [PinRef("P_SCL", "SCL_NET"), PinRef("P_SDA", "SDA_NET")])],
        nets=[Net("SCL_NET", [("U1", "P_SCL")]), Net("SDA_NET", [("U1", "P_SDA")])],
    )
    cons = evaluate_bus_consensus(nl, _bus(), I2C_PAIR, kb=kb, peripheral_routing={},
                                   kb_signal_to_role=_KB_SIGNAL_TO_ROLE)
    assert cons.verdict == BusVerdict.CONSENSUS_INSUFFICIENT.value
    assert cons.distinct_voter_devices == 1
    assert MemberClass.MINORITY_CONTRADICTS.value not in {m.classification for m in cons.members}


# ── pin-function-source majority (no KB at all — the jetson-board shape) ─────

def test_pinfunc_majority_flags_minority_no_kb():
    """5 devices' own literal pin-function SDA/SCL names agree; 1 disagrees
    (swapped) — the real jetson-agx-thor-baseboard U54 shape, no KB involved."""
    def dev(refdes, sda_net, scl_net):
        return Component(refdes, f"MPN_{refdes}", [
            PinRef("5", sda_net, "SDA"), PinRef("6", scl_net, "SCL"),
        ])
    comps = [dev("U22", "/SYS.SDA", "/SYS.SCL"), dev("U50", "/SYS.SDA", "/SYS.SCL"),
             dev("U60", "/SYS.SDA", "/SYS.SCL"), dev("U66", "/SYS.SDA", "/SYS.SCL")]
    # U54: swapped — its SDA pin sits on the SCL net and vice versa.
    comps.append(Component("U54", "MPN_U54", [PinRef("5", "/SYS.SCL", "SDA"), PinRef("6", "/SYS.SDA", "SCL")]))
    nl = Netlist(
        components=comps,
        nets=[
            Net("/SYS.SDA", [(c.refdes, "5" if c.refdes != "U54" else "6") for c in comps]),
            Net("/SYS.SCL", [(c.refdes, "6" if c.refdes != "U54" else "5") for c in comps]),
        ],
    )
    m6 = m6_flagged_pins(nl, kb={}, peripheral_routing={})
    cons = evaluate_bus_consensus(nl, _bus(net_a="/SYS.SCL", net_b="/SYS.SDA", bus_id="/SYS", source="name_prefix"),
                                   I2C_PAIR, kb={}, peripheral_routing={},
                                   kb_signal_to_role=_KB_SIGNAL_TO_ROLE, m6_flagged=m6)
    assert cons.verdict == BusVerdict.WOULD_FAIL.value
    u54 = [m for m in cons.members if m.refdes == "U54"]
    assert len(u54) == 2
    assert all(m.classification == MemberClass.MINORITY_CONTRADICTS.value for m in u54)
    assert all(m.role_source == "pinfunc" for m in u54)
    # the shipped M6 primitive already independently catches a real SDA<->SCL
    # swap from the same pin-function evidence — expected true (informs Phase 3
    # de-dupe, not a Phase-1 gap).
    assert all(m.caught_by_m6 is True for m in u54)

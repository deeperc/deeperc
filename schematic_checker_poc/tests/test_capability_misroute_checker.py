"""Phase 3 — LIVE capability-misroute FAIL wiring in step_08d (M14).

These assert the VERDICT-MOVING emit path added to
``step_08d_peripheral_checker.check_peripheral_buses`` (not the report-only evaluator
— that is ``test_peripheral_consensus.py``). THE INVARIANT under test: a capability
FAIL fires ONLY on a KB-CONFIRMED incapable pin; a non-KB'd pin and a matrix/free-mux
pin NEVER produce a capability FAIL (they stand down). Same lightweight synthetic
netlist shape as test_peripheral_consensus.py.
"""
import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from steps.peripheral_kb import (
    Signal, KBSource, Peripheral, PinRole, PinFunctionEntry, PeripheralRouting,
)
from steps.step_08d_peripheral_checker import (
    check_peripheral_buses, PeripheralViolation, Severity, FindingReason,
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
    ground_nets: list = field(default_factory=list)
    power_nets:  list = field(default_factory=list)


def _make_kb(*entries):
    return {(e.mpn, e.pin_id): e for e in entries}


def _sensor(mpn, pin_id, signal, instance=None):
    return PinFunctionEntry(mpn, pin_id,
                            [PinRole(Peripheral.I2C, instance, signal, KBSource.VENDOR_XML)])


def _cap_fails(findings):
    return [f for f in findings
            if f.violation == PeripheralViolation.CAPABILITY_MISMATCH
            and f.severity == Severity.FAIL]


def test_capability_incapable_pin_emits_live_fail():
    """SCL routed onto a KB-incapable (GPIO-only) MCU pin, corroborated by the same
    MCU's correct SDA voter -> a CAPABILITY_MISMATCH FAIL on that pin/net."""
    kb = _make_kb(
        _sensor("MCU", "P_SDA", Signal.I2C_SDA, "I2C1"),
        PinFunctionEntry("MCU", "P_WRONG",
                         [PinRole(Peripheral.GPIO, None, Signal.GPIO, KBSource.VENDOR_XML)]),
    )
    nl = Netlist(
        components=[
            Component("U1", "MCU", [PinRef("P_SDA", "SDA_NET", "P_SDA"),
                                    PinRef("P_WRONG", "SCL_NET", "P_WRONG")]),
            Component("J1", "HEADER", [PinRef("1", "SDA_NET", "1"), PinRef("2", "SCL_NET", "2")]),
        ],
        nets=[Net("SDA_NET", [("U1", "P_SDA"), ("J1", "1")]),
              Net("SCL_NET", [("U1", "P_WRONG"), ("J1", "2")])],
    )
    fails = _cap_fails(check_peripheral_buses(nl, kb, {}))
    assert len(fails) == 1
    f = fails[0]
    assert f.net == "SCL_NET"
    assert f.pins == ["U1.P_WRONG"]
    assert "KB alt-fn map excludes" in f.evidence


def test_non_kb_dest_never_fails_invariant():
    """INVARIANT: SCL routed onto a NON-KB'd connector pin -> the checker cannot
    confirm incapability -> NO capability FAIL (stand down)."""
    kb = _make_kb(
        _sensor("MCU", "P_SDA", Signal.I2C_SDA, "I2C1"),
        _sensor("MCU", "P_SCL", Signal.I2C_SCL, "I2C1"),
    )
    # SCL displaced off the MCU onto a non-KB header pin; MCU SCL pin parked elsewhere.
    nl = Netlist(
        components=[
            Component("U1", "MCU", [PinRef("P_SDA", "SDA_NET", "P_SDA"),
                                    PinRef("P_SCL", "PARK_NET", "P_SCL")]),
            Component("J3", "Conn_01x04_Pin", [PinRef("1", "SDA_NET", "1"),
                                               PinRef("2", "SCL_NET", "2")]),
        ],
        nets=[Net("SDA_NET", [("U1", "P_SDA"), ("J3", "1")]),
              Net("SCL_NET", [("J3", "2")]),
              Net("PARK_NET", [("U1", "P_SCL")])],
    )
    assert _cap_fails(check_peripheral_buses(nl, kb, {})) == []


def test_matrix_routed_never_fails_invariant():
    """INVARIANT: a matrix/free-mux MCU (ESP32-style) I2C pin -> UNCONSTRAINED,
    never INCAPABLE -> NO capability FAIL."""
    kb = _make_kb(_sensor("EEPROM", "SDA", Signal.I2C_SDA),
                  _sensor("EEPROM", "SCL", Signal.I2C_SCL))
    routing = {"MATRIX_MCU": {Peripheral.I2C: PeripheralRouting.MATRIX}}
    nl = Netlist(
        components=[
            Component("U1", "MATRIX_MCU", [PinRef("GPIO4", "SCL_NET", "GPIO4"),
                                           PinRef("GPIO5", "SDA_NET", "GPIO5")]),
            Component("U2", "EEPROM", [PinRef("SCL", "SCL_NET", "SCL"),
                                       PinRef("SDA", "SDA_NET", "SDA")]),
        ],
        nets=[Net("SCL_NET", [("U1", "GPIO4"), ("U2", "SCL")]),
              Net("SDA_NET", [("U1", "GPIO5"), ("U2", "SDA")])],
    )
    assert _cap_fails(check_peripheral_buses(nl, kb, routing)) == []


def test_clean_bus_no_capability_fail():
    """A correctly-wired multi-member bus emits no capability FAIL (no over-fire)."""
    kb = _make_kb(
        _sensor("MCU", "P_SCL", Signal.I2C_SCL, "I2C1"),
        _sensor("MCU", "P_SDA", Signal.I2C_SDA, "I2C1"),
        _sensor("EEPROM", "SCL", Signal.I2C_SCL),
        _sensor("EEPROM", "SDA", Signal.I2C_SDA),
    )
    nl = Netlist(
        components=[
            Component("U1", "MCU", [PinRef("P_SCL", "SCL_NET", "P_SCL"),
                                    PinRef("P_SDA", "SDA_NET", "P_SDA")]),
            Component("U2", "EEPROM", [PinRef("SCL", "SCL_NET", "SCL"),
                                       PinRef("SDA", "SDA_NET", "SDA")]),
        ],
        nets=[Net("SCL_NET", [("U1", "P_SCL"), ("U2", "SCL")]),
              Net("SDA_NET", [("U1", "P_SDA"), ("U2", "SDA")])],
    )
    assert _cap_fails(check_peripheral_buses(nl, kb, {})) == []


# ── TODO-417 H2 scope item 6 (TODO-433 ruling): consensus-path surfacing ──────
# Previously silently discarded (D1 fact 6: `_cons.verdict` was never read at
# all). Both are UNRESOLVABLE, never FAIL — the consensus path stays
# report-only for verdict purposes, same INVARIANT as the capability path.

def _unres(findings):
    return [f for f in findings
            if f.violation == PeripheralViolation.CAPABILITY_MISMATCH
            and f.severity == Severity.UNRESOLVABLE]


def test_instance_tie_emits_unresolvable_not_fail():
    """Two devices assert DIFFERENT instances on the SDA side of a name-prefix-
    paired bus, 1-1 (I2C1 vs I2C2) — a genuine instance tie, no minority picked.
    Only the SDA role carries an instance (the SCL role's instance is left None)
    so neither instance forms a clean kb_instance-sourced pair on its own (each
    would need BOTH nets_a/nets_b populated) — the bus is paired via name-prefix
    instead, which passes has_independent_identity. Must surface as ONE
    UNRESOLVABLE finding (M14_TIE_UNRESOLVABLE), never a CAPABILITY_MISMATCH FAIL
    (a pre-existing, unrelated per-net INSTANCE_MISMATCH FAIL MAY also fire on
    SDA_NET — that's a different violation type, not asserted against here)."""
    kb = _make_kb(
        _sensor("U1MCU", "SDA1", Signal.I2C_SDA, "I2C1"),
        PinFunctionEntry("U1MCU", "SCL1",
                         [PinRole(Peripheral.I2C, None, Signal.I2C_SCL, KBSource.VENDOR_XML)]),
        _sensor("U2MCU", "SDA2", Signal.I2C_SDA, "I2C2"),
    )
    nl = Netlist(
        components=[
            Component("U1", "U1MCU", [PinRef("SDA1", "SDA_NET", "SDA1"),
                                      PinRef("SCL1", "SCL_NET", "SCL1")]),
            Component("U2", "U2MCU", [PinRef("SDA2", "SDA_NET", "SDA2")]),
        ],
        nets=[Net("SDA_NET", [("U1", "SDA1"), ("U2", "SDA2")]),
              Net("SCL_NET", [("U1", "SCL1")])],
    )
    findings = check_peripheral_buses(nl, kb, {})
    assert _cap_fails(findings) == [], "an instance tie must never produce a capability FAIL"
    unres = _unres(findings)
    tie = [f for f in unres if f.reason == FindingReason.M14_TIE_UNRESOLVABLE]
    assert len(tie) == 1, f"expected exactly 1 TIE UNRESOLVABLE; got {findings}"
    assert set(tie[0].pins) >= {"U1.SDA1", "U2.SDA2"}


def test_consensus_minority_emits_unresolvable_not_fail():
    """3 distinct devices on SDA_NET: two agree (SDA), one asserts the minority
    role (SCL) — cross-device disagreement, but NOT a KB-confirmed incapability
    (fail_mode == "consensus"). Must surface as UNRESOLVABLE (M14_CONSENSUS_
    MINORITY) on the minority member only, never FAIL."""
    kb = _make_kb(
        _sensor("U1MCU", "SDA1", Signal.I2C_SDA, "I2C1"),
        _sensor("U1MCU", "SCL1", Signal.I2C_SCL, "I2C1"),
        _sensor("U2MCU", "SDA2", Signal.I2C_SDA, "I2C1"),
        _sensor("U3MCU", "WRONG", Signal.I2C_SCL, "I2C1"),   # minority: SCL on SDA_NET
    )
    nl = Netlist(
        components=[
            Component("U1", "U1MCU", [PinRef("SDA1", "SDA_NET", "SDA1"),
                                      PinRef("SCL1", "SCL_NET", "SCL1")]),
            Component("U2", "U2MCU", [PinRef("SDA2", "SDA_NET", "SDA2")]),
            Component("U3", "U3MCU", [PinRef("WRONG", "SDA_NET", "WRONG")]),
        ],
        nets=[Net("SDA_NET", [("U1", "SDA1"), ("U2", "SDA2"), ("U3", "WRONG")]),
              Net("SCL_NET", [("U1", "SCL1")])],
    )
    findings = check_peripheral_buses(nl, kb, {})
    assert _cap_fails(findings) == [], "a consensus minority must never produce a FAIL"
    unres = _unres(findings)
    minority = [f for f in unres if f.reason == FindingReason.M14_CONSENSUS_MINORITY]
    assert len(minority) == 1, f"expected exactly 1 consensus-minority UNRESOLVABLE; got {findings}"
    assert minority[0].pins == ["U3.WRONG"]
    assert minority[0].net == "SDA_NET"

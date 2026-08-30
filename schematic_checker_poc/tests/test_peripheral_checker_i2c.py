"""Tests for Phase 1.1.b — KB-anchored I2C peripheral mismatch detection.

All 17 tests exercise check_i2c_peripheral() against the Phase 1.1.b design spec.
KB data model (Signal, KBSource, Peripheral, PinRole, PinFunctionEntry) is imported
from steps.peripheral_kb. Finding types (PeripheralViolation, Severity,
PeripheralFinding) are imported from steps.step_08d_peripheral_checker.

Vacuously-passing tests (pass because no FAIL expected):
  1  test_sda_scl_swap_no_fail_emitted_per_net
  6  test_pullup_present_no_warn
  8  test_net_name_corroborates_kb
  9  test_gpio_only_net_skipped
  11 test_inter_mcu_ambiguous_no_name_silent
  12 test_three_pin_i2c_bus_passes
  13 test_ambiguous_i2c_signal_resolved_by_peer
  14 test_sensor_gpio_alt_not_fixed_function
  16 test_stm32_specific_mpn_canonicalizes_to_range_key

Tests that emit specific findings:
  2  test_protocol_mismatch_non_i2c_pin
  3  test_instance_mismatch_i2c1_vs_i2c2
  4  test_missing_scl_counterpart
  5  test_no_pullup_warns
  7  test_unknown_mcu_unresolvable
  10 test_inter_mcu_role_mismatch
  15 test_esp32_i2c_matrix_routing_unresolvable
  17 test_esp32_module_mpn_canonicalizes_to_soc

Run:
  pytest schematic_checker_poc/tests/test_peripheral_checker_i2c.py -v
"""
import os
import sys
import pytest
from dataclasses import dataclass, field
from enum import Enum

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from steps.peripheral_kb import Signal, KBSource, Peripheral, PinRole, PinFunctionEntry, PeripheralRouting
from steps.step_08d_peripheral_checker import (
    PeripheralViolation, Severity, PeripheralFinding,
    check_i2c_peripheral, _resolve_pin, _LookupStatus, is_i2c_classified_net,
)


# ── Minimal netlist fixture types ─────────────────────────────────────────────
# Same shape as test_passive_traversal.py, except Component uses 'mpn' instead
# of 'value' — the peripheral checker's KB lookup is keyed on MPN.

@dataclass
class PinRef:
    pin_id:   str
    net:      str
    pin_name: str = ""


@dataclass
class Component:
    refdes: str
    mpn:    str    # part number for KB lookup (Tier 1.6 used 'value')
    pins:   list   # list[PinRef]
    @property
    def effective_mpn(self): return self.mpn


@dataclass
class Net:
    name: str
    pins: list     # list[tuple[str, str]] = [(refdes, pin_id), ...]


@dataclass
class Netlist:
    components: list   # list[Component]
    nets:       list   # list[Net]


# ── KB helpers ────────────────────────────────────────────────────────────────

def _make_kb(*entries: PinFunctionEntry) -> dict:
    """Build a KB dict keyed by (mpn, pin_id)."""
    return {(e.mpn, e.pin_id): e for e in entries}


def _pb6_entry(mpn: str = "STM32F103C8T6") -> PinFunctionEntry:
    """STM32F103 PB6: I2C1_SCL / USART1_TX / TIM4_CH1 / GPIO."""
    return PinFunctionEntry(mpn, "PB6", [
        PinRole(Peripheral.I2C,  "I2C1",   Signal.I2C_SCL, KBSource.VENDOR_XML),
        PinRole(Peripheral.UART, "USART1", Signal.UART_TX,  KBSource.VENDOR_XML),
        PinRole(Peripheral.TIM,  "TIM4",   Signal.TIM_CH,   KBSource.VENDOR_XML),
        PinRole(Peripheral.GPIO, None,     Signal.GPIO,     KBSource.VENDOR_XML),
    ])


def _pb7_entry(mpn: str = "STM32F103C8T6") -> PinFunctionEntry:
    """STM32F103 PB7: I2C1_SDA / USART1_RX / TIM4_CH2 / GPIO."""
    return PinFunctionEntry(mpn, "PB7", [
        PinRole(Peripheral.I2C,  "I2C1",   Signal.I2C_SDA, KBSource.VENDOR_XML),
        PinRole(Peripheral.UART, "USART1", Signal.UART_RX,  KBSource.VENDOR_XML),
        PinRole(Peripheral.TIM,  "TIM4",   Signal.TIM_CH,   KBSource.VENDOR_XML),
        PinRole(Peripheral.GPIO, None,     Signal.GPIO,     KBSource.VENDOR_XML),
    ])


def _pa0_entry(mpn: str = "STM32F103C8T6") -> PinFunctionEntry:
    """STM32F103 PA0: TIM2_CH1 / USART2_CTS / GPIO — NO I2C capability."""
    return PinFunctionEntry(mpn, "PA0", [
        PinRole(Peripheral.TIM,  "TIM2",   Signal.TIM_CH,   KBSource.VENDOR_XML),
        PinRole(Peripheral.UART, "USART2", Signal.UART_CTS, KBSource.VENDOR_XML),
        PinRole(Peripheral.GPIO, None,     Signal.GPIO,     KBSource.VENDOR_XML),
    ])


def _sensor_sda_entry(mpn: str, pin_id: str = "SDA") -> PinFunctionEntry:
    """Fixed-function I2C SDA pin (e.g. 24LC256 EEPROM pin 5)."""
    return PinFunctionEntry(mpn, pin_id, [
        PinRole(Peripheral.I2C, None, Signal.I2C_SDA, KBSource.VENDOR_XML),
    ])


def _sensor_scl_entry(mpn: str, pin_id: str = "SCL") -> PinFunctionEntry:
    """Fixed-function I2C SCL pin."""
    return PinFunctionEntry(mpn, pin_id, [
        PinRole(Peripheral.I2C, None, Signal.I2C_SCL, KBSource.VENDOR_XML),
    ])


# ── Test 1: SDA↔SCL swap ─────────────────────────────────────────────────────

def test_sda_scl_swap_detected_by_coherence():
    """
    STM32F103 PB6 (I2C1_SCL) on the I2C_SDA net and PB7 (I2C1_SDA) on the
    I2C_SCL net — a clean SDA↔SCL swap. The per-net checks cannot see it in
    isolation, but the M6 coherence primitive catches it cross-net from the KB
    possible_roles: PB6's I2C role is SCL yet it sits on a net named SDA, and PB7
    (SDA) sits on SCL → two coherence FAILs.

    (Was the v1.1 stub `test_sda_scl_swap_no_fail_emitted_per_net`, which asserted
    NON-detection against the unbuilt checker — the detector now exists.)
    """
    kb = _make_kb(
        _pb6_entry(),                       # PB6 = I2C1_SCL
        _pb7_entry(),                       # PB7 = I2C1_SDA
        _sensor_sda_entry("24LC256", "SDA"),
        _sensor_scl_entry("24LC256", "SCL"),
    )
    # Swapped: PB6 (SCL-capable) wired to I2C_SDA; PB7 (SDA-capable) to I2C_SCL.
    # Pull-ups present so NO_PULLUP doesn't obscure the swap-detection gap.
    nl = Netlist(
        components=[
            Component("U1", "STM32F103C8T6", [
                PinRef("PB6", "I2C_SDA"),
                PinRef("PB7", "I2C_SCL"),
            ]),
            Component("U2", "24LC256", [
                PinRef("SDA", "I2C_SDA"),
                PinRef("SCL", "I2C_SCL"),
            ]),
            Component("R1", "4.7k", [PinRef("1", "I2C_SDA"), PinRef("2", "+3V3")]),
            Component("R2", "4.7k", [PinRef("1", "I2C_SCL"), PinRef("2", "+3V3")]),
        ],
        nets=[
            Net("I2C_SDA", [("U1", "PB6"), ("U2", "SDA"), ("R1", "1")]),
            Net("I2C_SCL", [("U1", "PB7"), ("U2", "SCL"), ("R2", "1")]),
            Net("+3V3",    [("R1", "2"), ("R2", "2")]),
        ],
    )
    findings = check_i2c_peripheral(nl, kb)
    swap_fails = [f for f in findings
                  if f.severity is Severity.FAIL and "SDA/SCL swap" in f.evidence]
    assert len(swap_fails) == 2, f"Expected 2 coherence FAILs; got {findings}"
    nets = {f.net for f in swap_fails}
    assert nets == {"I2C_SDA", "I2C_SCL"}
    assert all("U1." in p for f in swap_fails for p in f.pins)


def test_coherence_fail_surfaces_past_connector_unresolvable():
    """Regression: a definitive SDA/SCL coherence FAIL must NOT be masked by a
    per-net UNRESOLVABLE on the same net.

    Real acquired boards (shadaab1904/devnithw, STM32F103C8) route the I2C bus
    through a pin-header connector (Conn_01x04_Pin) whose MPN is not in the KB.
    The per-net two-phase check emits ROLE_MISMATCH/UNRESOLVABLE ('cannot verify
    — MPN not in KB') on /I2C2_SCL,/I2C2_SDA. That UNRESOLVABLE is a DIFFERENT
    root cause and must not suppress the SDA/SCL swap FAIL (a FAIL is stronger
    evidence than an UNRESOLVABLE). Before the severity-aware suppression fix the
    swap was masked → the recall HIT silently disappeared.
    """
    kb = _make_kb(_pb6_entry(), _pb7_entry())   # connector MPN intentionally absent
    nl = Netlist(
        components=[
            Component("U1", "STM32F103C8T6", [
                PinRef("PB6", "I2C_SDA"),       # SCL-capable pin on the SDA net (swapped)
                PinRef("PB7", "I2C_SCL"),       # SDA-capable pin on the SCL net (swapped)
            ]),
            Component("J1", "Conn_01x04_Pin", [  # header on the bus, not in KB
                PinRef("1", "I2C_SDA"),
                PinRef("2", "I2C_SCL"),
            ]),
            Component("R1", "4.7k", [PinRef("1", "I2C_SDA"), PinRef("2", "+3V3")]),
            Component("R2", "4.7k", [PinRef("1", "I2C_SCL"), PinRef("2", "+3V3")]),
        ],
        nets=[
            Net("I2C_SDA", [("U1", "PB6"), ("J1", "1"), ("R1", "1")]),
            Net("I2C_SCL", [("U1", "PB7"), ("J1", "2"), ("R2", "1")]),
            Net("+3V3",    [("R1", "2"), ("R2", "2")]),
        ],
    )
    findings = check_i2c_peripheral(nl, kb)
    swap_fails = [f for f in findings
                  if f.severity is Severity.FAIL and "SDA/SCL swap" in f.evidence]
    # The connector should still leave its UNRESOLVABLE, but the swap FAIL surfaces.
    assert len(swap_fails) == 2, f"swap FAIL masked by connector UNRESOLVABLE: {findings}"
    assert {f.net for f in swap_fails} == {"I2C_SDA", "I2C_SCL"}
    assert any(f.severity is Severity.UNRESOLVABLE for f in findings), \
        "expected the connector to still produce a per-net UNRESOLVABLE"


# ── Test 2: Protocol mismatch ─────────────────────────────────────────────────

def test_protocol_mismatch_non_i2c_pin():
    """
    STM32F103 PA0 has no I2C capability (TIM/UART/GPIO only). Wired to a
    fixed-function sensor SDA pin on net I2C_SDA. The checker must emit exactly
    one FAIL PROTOCOL_MISMATCH, with PA0 mentioned in the finding's pins.

    FAILS against the stub (returns []).
    """
    kb = _make_kb(
        _pa0_entry(),
        _sensor_sda_entry("24LC256", "SDA"),
    )
    nl = Netlist(
        components=[
            Component("U1", "STM32F103C8T6", [PinRef("PA0", "I2C_SDA")]),
            Component("U2", "24LC256",       [PinRef("SDA", "I2C_SDA")]),
        ],
        nets=[Net("I2C_SDA", [("U1", "PA0"), ("U2", "SDA")])],
    )
    findings = check_i2c_peripheral(nl, kb)
    assert len(findings) == 1, f"Expected 1 finding; got {len(findings)}: {findings}"
    assert findings[0].severity  == Severity.FAIL
    assert findings[0].violation == PeripheralViolation.PROTOCOL_MISMATCH
    pins_str = " ".join(findings[0].pins)
    assert "PA0" in pins_str, (
        f"Expected PA0 in finding pins; got {findings[0].pins}"
    )


# ── Test 3: Instance mismatch ─────────────────────────────────────────────────

def test_instance_mismatch_i2c1_vs_i2c2():
    """
    Two fixed-function I2C SDA pins from different instances (I2C1 vs I2C2)
    wired to the same net. Both agree on signal (SDA) but disagree on instance.
    The checker must emit exactly one FAIL INSTANCE_MISMATCH.

    FAILS against the stub (returns []).
    """
    kb = _make_kb(
        PinFunctionEntry("MCU_ALPHA", "P_SDA", [
            PinRole(Peripheral.I2C, "I2C1", Signal.I2C_SDA, KBSource.VENDOR_XML),
        ]),
        PinFunctionEntry("MCU_BETA", "P_SDA", [
            PinRole(Peripheral.I2C, "I2C2", Signal.I2C_SDA, KBSource.VENDOR_XML),
        ]),
    )
    nl = Netlist(
        components=[
            Component("U1", "MCU_ALPHA", [PinRef("P_SDA", "I2C_SDA")]),
            Component("U2", "MCU_BETA",  [PinRef("P_SDA", "I2C_SDA")]),
        ],
        nets=[Net("I2C_SDA", [("U1", "P_SDA"), ("U2", "P_SDA")])],
    )
    findings = check_i2c_peripheral(nl, kb)
    assert len(findings) == 1, f"Expected 1 finding; got {len(findings)}: {findings}"
    assert findings[0].severity  == Severity.FAIL
    assert findings[0].violation == PeripheralViolation.INSTANCE_MISMATCH


# ── Test 4: Missing peripheral counterpart ────────────────────────────────────

def test_missing_scl_counterpart():
    """
    A valid I2C SDA net (STM32F103 PB7 + 24LC256 SDA, both I2C1_SDA-compatible)
    with no corresponding SCL net anywhere in the netlist. The checker must emit
    exactly one FAIL MISSING_PERIPHERAL and mention the missing SCL counterpart.

    FAILS against the stub (returns []).
    """
    kb = _make_kb(
        _pb7_entry(),
        _sensor_sda_entry("24LC256", "SDA"),
    )
    nl = Netlist(
        components=[
            Component("U1", "STM32F103C8T6", [PinRef("PB7", "I2C_SDA")]),
            Component("U2", "24LC256",       [PinRef("SDA", "I2C_SDA")]),
        ],
        nets=[Net("I2C_SDA", [("U1", "PB7"), ("U2", "SDA")])],
        # No SCL net present in the netlist.
    )
    findings = check_i2c_peripheral(nl, kb)
    assert len(findings) == 1, f"Expected 1 finding; got {len(findings)}: {findings}"
    assert findings[0].severity  == Severity.FAIL
    assert findings[0].violation == PeripheralViolation.MISSING_PERIPHERAL
    ev = findings[0].evidence.upper()
    assert "SCL" in ev or "MISSING" in ev, (
        f"Expected evidence to mention missing SCL; got: {findings[0].evidence!r}"
    )


# ── Test 5: No pull-up ────────────────────────────────────────────────────────

def test_no_pullup_warns():
    """
    A valid I2C SDA net (PB7 + 24LC256 SDA, compatible) with no pull-up resistor
    to any rail. The checker must emit at least one WARN NO_PULLUP_DETECTED.
    No FAIL expected — missing pull-up is advisory only.

    FAILS against the stub (returns []).
    """
    kb = _make_kb(
        _pb7_entry(),
        _pb6_entry(),
        _sensor_sda_entry("24LC256", "SDA"),
        _sensor_scl_entry("24LC256", "SCL"),
    )
    nl = Netlist(
        components=[
            Component("U1", "STM32F103C8T6", [
                PinRef("PB7", "I2C_SDA"),
                PinRef("PB6", "I2C_SCL"),
            ]),
            Component("U2", "24LC256", [
                PinRef("SDA", "I2C_SDA"),
                PinRef("SCL", "I2C_SCL"),
            ]),
        ],
        nets=[
            Net("I2C_SDA", [("U1", "PB7"), ("U2", "SDA")]),
            Net("I2C_SCL", [("U1", "PB6"), ("U2", "SCL")]),
        ],
    )
    findings = check_i2c_peripheral(nl, kb)
    pullup_warns = [
        f for f in findings
        if f.violation == PeripheralViolation.NO_PULLUP_DETECTED
    ]
    assert len(pullup_warns) >= 1, (
        "Expected at least 1 NO_PULLUP_DETECTED finding when no pull-up present"
    )
    for f in pullup_warns:
        assert f.severity == Severity.WARN, (
            f"NO_PULLUP_DETECTED must be WARN severity; got {f.severity}"
        )
    assert all(f.severity != Severity.FAIL for f in findings), (
        "Missing pull-up should produce WARN only, not FAIL"
    )


# ── Test 6: Pull-up present ───────────────────────────────────────────────────

def test_pullup_present_no_warn():
    """
    Valid I2C SDA net with a 4.7kΩ pull-up resistor (R1) to +3V3. The checker
    must not emit NO_PULLUP_DETECTED and must not emit any FAIL.

    VACUOUSLY PASSES against the stub (returns []).
    TODO: the real implementation needs confirmed_voltages / passive traversal
    data to detect R1.2 as a confirmed power rail. If check_i2c_peripheral grows
    additional parameters for traversal results, update this fixture accordingly.
    """
    kb = _make_kb(
        _pb7_entry(),
        _pb6_entry(),
        _sensor_sda_entry("24LC256", "SDA"),
        _sensor_scl_entry("24LC256", "SCL"),
    )
    nl = Netlist(
        components=[
            Component("U1", "STM32F103C8T6", [
                PinRef("PB7", "I2C_SDA"),
                PinRef("PB6", "I2C_SCL"),
            ]),
            Component("U2", "24LC256", [
                PinRef("SDA", "I2C_SDA"),
                PinRef("SCL", "I2C_SCL"),
            ]),
            Component("R1", "4.7k", [PinRef("1", "I2C_SDA"), PinRef("2", "+3V3")]),
            Component("R2", "4.7k", [PinRef("1", "I2C_SCL"), PinRef("2", "+3V3")]),
        ],
        nets=[
            Net("I2C_SDA", [("U1", "PB7"), ("U2", "SDA"), ("R1", "1")]),
            Net("I2C_SCL", [("U1", "PB6"), ("U2", "SCL"), ("R2", "1")]),
            Net("+3V3",    [("R1", "2"), ("R2", "2")]),
        ],
    )
    findings = check_i2c_peripheral(nl, kb)
    assert all(
        f.violation != PeripheralViolation.NO_PULLUP_DETECTED for f in findings
    ), "Expected no NO_PULLUP_DETECTED when 4.7kΩ pull-up to +3V3 is present"
    assert all(f.severity != Severity.FAIL for f in findings), (
        "Expected no FAIL for a correctly wired net with pull-up"
    )


# ── NO_PULLUP suppression relax (WARN surfaces past a connector UNRESOLVABLE) ──
# Locks the step_08d Step-8 fix: a KB-coverage UNRESOLVABLE (a connector/sensor on
# the bus whose MPN isn't in the KB — the shadaab1904/devnithw Conn_01x04_Pin
# scenario) must NOT mask a genuinely missing pull-up. WARN-level analog of the
# M6/M12 severity-aware coherence suppression (9e33313 / test 83bdbb4): surface
# past an UNRESOLVABLE, de-dupe behind a FAIL. `_has_pullup` is unchanged.

def test_no_pullup_surfaces_past_connector_unresolvable():
    """THE FIX: a missing pull-up on an I2C bus that also carries a not-in-KB
    pin-header connector must still WARN.

    The connector (Conn_01x04_Pin) leaves a per-net ROLE_MISMATCH/UNRESOLVABLE
    ('MPN not in KB') on both I2C nets — a *coverage* gap, not an electrical
    fault. The genuinely missing pull-up must surface as NO_PULLUP_DETECTED past
    it. Before the relax, the connector UNRESOLVABLE masked the WARN.
    """
    kb = _make_kb(_pb7_entry(), _pb6_entry())   # connector MPN intentionally absent
    nl = Netlist(
        components=[
            Component("U1", "STM32F103C8T6", [   # correctly wired (no swap)
                PinRef("PB7", "I2C_SDA"),        # SDA-capable pin on the SDA net
                PinRef("PB6", "I2C_SCL"),        # SCL-capable pin on the SCL net
            ]),
            Component("J1", "Conn_01x04_Pin", [  # I2C header on the bus, not in KB
                PinRef("1", "I2C_SDA"),
                PinRef("2", "I2C_SCL"),
            ]),
            # NO pull-up resistors anywhere → NO_PULLUP must surface.
        ],
        nets=[
            Net("I2C_SDA", [("U1", "PB7"), ("J1", "1")]),
            Net("I2C_SCL", [("U1", "PB6"), ("J1", "2")]),
        ],
    )
    findings = check_i2c_peripheral(nl, kb)
    pullup_warns = [f for f in findings
                    if f.violation == PeripheralViolation.NO_PULLUP_DETECTED]
    assert len(pullup_warns) >= 1, (
        f"NO_PULLUP masked by the connector UNRESOLVABLE: {findings}"
    )
    for f in pullup_warns:
        assert f.severity == Severity.WARN, (
            f"NO_PULLUP_DETECTED must stay WARN, never FAIL; got {f.severity}"
        )
    # The connector should STILL leave its per-net UNRESOLVABLE — we surface past
    # it, we don't erase it.
    assert any(f.severity == Severity.UNRESOLVABLE for f in findings), (
        "expected the not-in-KB connector to still produce a per-net UNRESOLVABLE"
    )
    # And no FAIL on a correctly-wired bus.
    assert all(f.severity != Severity.FAIL for f in findings), (
        "correctly-wired bus must not FAIL"
    )


def test_no_pullup_not_emitted_when_pullup_present_despite_connector():
    """NO FALSE POSITIVE: same connector-on-the-bus topology, but the pull-ups
    ARE present. The relax must NOT surface a spurious WARN — `_has_pullup`
    returning True still suppresses NO_PULLUP regardless of the connector
    UNRESOLVABLE. This is the good-board case the precision gate guards.
    """
    kb = _make_kb(_pb7_entry(), _pb6_entry())
    nl = Netlist(
        components=[
            Component("U1", "STM32F103C8T6", [
                PinRef("PB7", "I2C_SDA"),
                PinRef("PB6", "I2C_SCL"),
            ]),
            Component("J1", "Conn_01x04_Pin", [  # not in KB → per-net UNRESOLVABLE
                PinRef("1", "I2C_SDA"),
                PinRef("2", "I2C_SCL"),
            ]),
            Component("R1", "4.7k", [PinRef("1", "I2C_SDA"), PinRef("2", "+3V3")]),
            Component("R2", "4.7k", [PinRef("1", "I2C_SCL"), PinRef("2", "+3V3")]),
        ],
        nets=[
            Net("I2C_SDA", [("U1", "PB7"), ("J1", "1"), ("R1", "1")]),
            Net("I2C_SCL", [("U1", "PB6"), ("J1", "2"), ("R2", "1")]),
            Net("+3V3",    [("R1", "2"), ("R2", "2")]),
        ],
    )
    findings = check_i2c_peripheral(nl, kb)
    assert all(
        f.violation != PeripheralViolation.NO_PULLUP_DETECTED for f in findings
    ), "pull-up IS present (R1/R2 to +3V3) — NO_PULLUP must stay suppressed"


def test_no_pullup_stays_suppressed_behind_real_fail():
    """DON'T OVER-LOOSEN: a real ROLE_MISMATCH FAIL (two MCU I2C pins with
    mutually-exclusive signals on one net) must STILL suppress NO_PULLUP, even
    with no pull-up present. Bus identity is in doubt → a pull-up warning would
    be noise on top of a more serious finding. We surface past an UNRESOLVABLE,
    never past a FAIL.
    """
    kb = _make_kb(_pb6_entry(), _pb7_entry())
    nl = Netlist(
        components=[
            Component("U1", "STM32F103C8T6", [
                PinRef("PB6", "I2C_SDA"),   # SCL-only pin ...
                PinRef("PB7", "I2C_SDA"),   # ... and SDA-only pin on the SAME net
            ]),
            # No pull-up resistor on the net.
        ],
        nets=[
            Net("I2C_SDA", [("U1", "PB6"), ("U1", "PB7")]),
        ],
    )
    findings = check_i2c_peripheral(nl, kb)
    role_fails = [f for f in findings
                  if f.severity == Severity.FAIL
                  and f.violation == PeripheralViolation.ROLE_MISMATCH]
    assert len(role_fails) >= 1, f"expected a real ROLE_MISMATCH FAIL: {findings}"
    assert all(
        f.violation != PeripheralViolation.NO_PULLUP_DETECTED for f in findings
    ), "NO_PULLUP must stay suppressed behind a real FAIL (bus identity in doubt)"


# ── Test 7: MCU not in KB ─────────────────────────────────────────────────────

def test_unknown_mcu_unresolvable():
    """
    Net I2C_SDA has one pin from "UNKNOWN_MCU_XYZ" (absent from KB) and one pin
    from a 24LC256 sensor (present in KB). The checker must emit exactly one
    UNRESOLVABLE finding, mention the missing MPN in its evidence, and include
    the sensor's KB source (VENDOR_XML) in kb_provenance.

    A pull-up (R1) is present so the now-severity-aware NO_PULLUP relax (Step 8 —
    a missing pull-up surfaces past a KB-coverage UNRESOLVABLE) does not add a
    second finding here; this keeps the test focused on the UNRESOLVABLE itself.

    FAILS against the stub (returns []).
    """
    kb = _make_kb(
        _sensor_sda_entry("24LC256", "SDA"),
        # "UNKNOWN_MCU_XYZ" deliberately absent
    )
    nl = Netlist(
        components=[
            Component("U1", "UNKNOWN_MCU_XYZ", [PinRef("pin1", "I2C_SDA")]),
            Component("U2", "24LC256",         [PinRef("SDA",  "I2C_SDA")]),
            Component("R1", "4.7k", [PinRef("1", "I2C_SDA"), PinRef("2", "+3V3")]),
        ],
        nets=[
            Net("I2C_SDA", [("U1", "pin1"), ("U2", "SDA"), ("R1", "1")]),
            Net("+3V3",    [("R1", "2")]),
        ],
    )
    findings = check_i2c_peripheral(nl, kb)
    assert len(findings) == 1, f"Expected 1 finding; got {len(findings)}: {findings}"
    assert findings[0].severity == Severity.UNRESOLVABLE
    assert KBSource.VENDOR_XML in findings[0].kb_provenance, (
        "Expected sensor's VENDOR_XML source in kb_provenance for partial coverage"
    )
    assert "UNKNOWN_MCU_XYZ" in findings[0].evidence, (
        f"Expected missing MCU MPN in evidence; got {findings[0].evidence!r}"
    )


# ── Test 8: Net name corroborates KB ─────────────────────────────────────────

def test_net_name_corroborates_kb():
    """
    Net I2C_SDA connects STM32F103 PB7 (KB: I2C1_SDA) and a sensor SDA pin.
    The net name corroborates what the KB derives — no extra PROTOCOL_MISMATCH
    finding should arise from name corroboration.

    VACUOUSLY PASSES against the stub (returns []).
    TODO: if the evidence-augmentation path produces a separately inspectable
    output, assert that a corroboration string appears in the evidence.
    """
    kb = _make_kb(
        _pb7_entry(),
        _sensor_sda_entry("24LC256", "SDA"),
    )
    nl = Netlist(
        components=[
            Component("U1", "STM32F103C8T6", [PinRef("PB7", "I2C_SDA")]),
            Component("U2", "24LC256",       [PinRef("SDA", "I2C_SDA")]),
        ],
        nets=[Net("I2C_SDA", [("U1", "PB7"), ("U2", "SDA")])],
    )
    findings = check_i2c_peripheral(nl, kb)
    assert all(
        f.violation != PeripheralViolation.PROTOCOL_MISMATCH for f in findings
    ), "Net name corroboration must not produce spurious PROTOCOL_MISMATCH"


# ── Test 9: Both pins GPIO only ───────────────────────────────────────────────

def test_gpio_only_net_skipped():
    """
    Both pins on the net have only GPIO in their role list and the net name has
    no peripheral hint. The classifier has nothing to check → zero findings.

    VACUOUSLY PASSES against the stub (returns []).
    """
    kb = _make_kb(
        PinFunctionEntry("MCU_A", "PA1", [
            PinRole(Peripheral.GPIO, None, Signal.GPIO, KBSource.VENDOR_XML),
        ]),
        PinFunctionEntry("MCU_B", "PA2", [
            PinRole(Peripheral.GPIO, None, Signal.GPIO, KBSource.VENDOR_XML),
        ]),
    )
    nl = Netlist(
        components=[
            Component("U1", "MCU_A", [PinRef("PA1", "Net-(U1-PA1)")]),
            Component("U2", "MCU_B", [PinRef("PA2", "Net-(U1-PA1)")]),
        ],
        nets=[Net("Net-(U1-PA1)", [("U1", "PA1"), ("U2", "PA2")])],
    )
    findings = check_i2c_peripheral(nl, kb)
    assert len(findings) == 0, (
        f"Expected no findings for a GPIO-only net; got {findings}"
    )


# ── Test 10: Inter-MCU role mismatch ─────────────────────────────────────────

def test_inter_mcu_role_mismatch():
    """
    Two STM32F103 MCUs: U1.PB6 (I2C1_SCL within I2C) and U2.PB7 (I2C1_SDA
    within I2C) wired to the same net named MCU_I2C_SDA. The name indicates SDA.
    PB6's only I2C role is SCL → signals conflict → FAIL ROLE_MISMATCH.

    FAILS against the stub (returns []).
    """
    kb = _make_kb(
        _pb6_entry("STM32F103C8T6"),   # PB6 → I2C1_SCL
        _pb7_entry("STM32F103RBT6"),   # PB7 → I2C1_SDA (second MCU instance)
    )
    nl = Netlist(
        components=[
            Component("U1", "STM32F103C8T6", [PinRef("PB6", "MCU_I2C_SDA")]),
            Component("U2", "STM32F103RBT6", [PinRef("PB7", "MCU_I2C_SDA")]),
        ],
        nets=[Net("MCU_I2C_SDA", [("U1", "PB6"), ("U2", "PB7")])],
    )
    findings = check_i2c_peripheral(nl, kb)
    assert len(findings) == 1, f"Expected 1 finding; got {len(findings)}: {findings}"
    assert findings[0].severity  == Severity.FAIL
    assert findings[0].violation == PeripheralViolation.ROLE_MISMATCH
    ev = findings[0].evidence
    assert "PB6" in ev or "SCL" in ev, (
        f"Expected evidence to mention PB6 or SCL constraint; got {ev!r}"
    )


# ── Test 11: Inter-MCU ambiguous, no name hint ────────────────────────────────

def test_inter_mcu_ambiguous_no_name_silent():
    """
    Two multi-function STM32F103 pins (PB6 and PB7) on a net with an auto-
    generated name — no peripheral hint, no fixed-function pin. The classifier
    has no anchor → Case 4: stays silent, zero findings.

    VACUOUSLY PASSES against the stub (returns []).
    """
    kb = _make_kb(
        _pb6_entry("STM32F103C8T6"),
        _pb7_entry("STM32F103RBT6"),
    )
    nl = Netlist(
        components=[
            Component("U1", "STM32F103C8T6", [PinRef("PB6", "Net-(U1-PB6)")]),
            Component("U2", "STM32F103RBT6", [PinRef("PB7", "Net-(U1-PB6)")]),
        ],
        nets=[Net("Net-(U1-PB6)", [("U1", "PB6"), ("U2", "PB7")])],
    )
    findings = check_i2c_peripheral(nl, kb)
    assert len(findings) == 0, (
        f"Expected no findings for an ambiguous net with no peripheral hint; got {findings}"
    )


# ── Test 12: 3-pin I2C bus ────────────────────────────────────────────────────

def test_three_pin_i2c_bus_passes():
    """
    One MCU master (STM32F103 PB7, I2C1_SDA) plus two I2C slave sensors (24LC256
    and MPU6050, each fixed-function SDA), all on I2C_SDA, with a 4.7kΩ pull-up
    to +3V3. All three SDA pins are mutually consistent → no FAIL. Pull-up present
    → no NO_PULLUP_DETECTED WARN.

    VACUOUSLY PASSES against the stub (returns []).
    """
    kb = _make_kb(
        _pb7_entry(),
        _pb6_entry(),
        _sensor_sda_entry("24LC256", "SDA"),
        _sensor_scl_entry("24LC256", "SCL"),
        _sensor_sda_entry("MPU6050", "SDA"),
        _sensor_scl_entry("MPU6050", "SCL"),
    )
    nl = Netlist(
        components=[
            Component("U1", "STM32F103C8T6", [
                PinRef("PB7", "I2C_SDA"),
                PinRef("PB6", "I2C_SCL"),
            ]),
            Component("U2", "24LC256", [PinRef("SDA", "I2C_SDA"), PinRef("SCL", "I2C_SCL")]),
            Component("U3", "MPU6050", [PinRef("SDA", "I2C_SDA"), PinRef("SCL", "I2C_SCL")]),
            Component("R1", "4.7k", [PinRef("1", "I2C_SDA"), PinRef("2", "+3V3")]),
            Component("R2", "4.7k", [PinRef("1", "I2C_SCL"), PinRef("2", "+3V3")]),
        ],
        nets=[
            Net("I2C_SDA", [("U1", "PB7"), ("U2", "SDA"), ("U3", "SDA"), ("R1", "1")]),
            Net("I2C_SCL", [("U1", "PB6"), ("U2", "SCL"), ("U3", "SCL"), ("R2", "1")]),
            Net("+3V3",    [("R1", "2"), ("R2", "2")]),
        ],
    )
    findings = check_i2c_peripheral(nl, kb)
    assert all(f.severity != Severity.FAIL for f in findings), (
        f"Expected no FAIL for a valid 3-pin I2C bus; got {findings}"
    )
    assert all(
        f.violation != PeripheralViolation.NO_PULLUP_DETECTED for f in findings
    ), "Expected no NO_PULLUP_DETECTED when pull-up resistor is present"


# ── Test 13: Ambiguous signal resolved by peer ────────────────────────────────

def test_ambiguous_i2c_signal_resolved_by_peer():
    """
    A pin with roles [I2C1_SDA, I2C1_SCL] (fixed-function for I2C, but signal-
    ambiguous) is wired to a fixed-function sensor SDA pin. The checker resolves
    the ambiguity to SDA (the only role consistent with the sensor peer) and emits
    no FAIL.

    VACUOUSLY PASSES against the stub (returns []).
    TODO: once the implementation carries role resolution in its output, assert
    that the resolved signal for the MCU pin is Signal.I2C_SDA, not I2C_SCL.
    """
    kb = _make_kb(
        PinFunctionEntry("AMB_MCU", "P_AMB", [
            PinRole(Peripheral.I2C, "I2C1", Signal.I2C_SDA, KBSource.VENDOR_XML),
            PinRole(Peripheral.I2C, "I2C1", Signal.I2C_SCL, KBSource.VENDOR_XML),
        ]),
        PinFunctionEntry("AMB_MCU", "P_SCL", [
            PinRole(Peripheral.I2C, "I2C1", Signal.I2C_SCL, KBSource.VENDOR_XML),
        ]),
        _sensor_sda_entry("24LC256", "SDA"),
        _sensor_scl_entry("24LC256", "SCL"),
    )
    nl = Netlist(
        components=[
            Component("U1", "AMB_MCU", [PinRef("P_AMB", "I2C_SDA"), PinRef("P_SCL", "I2C_SCL")]),
            Component("U2", "24LC256", [PinRef("SDA",   "I2C_SDA"), PinRef("SCL",   "I2C_SCL")]),
        ],
        nets=[
            Net("I2C_SDA", [("U1", "P_AMB"), ("U2", "SDA")]),
            Net("I2C_SCL", [("U1", "P_SCL"), ("U2", "SCL")]),
        ],
    )
    findings = check_i2c_peripheral(nl, kb)
    fail_findings = [f for f in findings if f.severity == Severity.FAIL]
    assert len(fail_findings) == 0, (
        "Ambiguity resolved to SDA by sensor peer must not produce FAIL; "
        f"got {fail_findings}"
    )


# ── Test 14: Sensor GPIO alt mode — not fixed-function ────────────────────────

def test_sensor_gpio_alt_not_fixed_function():
    """
    Sensor pin has roles [I2C_SDA, GPIO] — two different peripherals — so it is
    NOT fixed-function for I2C (v1 rule: fixed-function iff ALL roles belong to
    the same peripheral). MCU pin is multi-function. Net name SENSOR_DATA has no
    I2C hint. Without a fixed-function anchor and without a name hint, the
    classifier falls through → zero findings.

    VACUOUSLY PASSES against the stub (returns []).
    """
    kb = _make_kb(
        _pb6_entry(),
        PinFunctionEntry("SOME_SENSOR", "DATA", [
            PinRole(Peripheral.I2C,  None, Signal.I2C_SDA, KBSource.VENDOR_XML),
            PinRole(Peripheral.GPIO, None, Signal.GPIO,     KBSource.VENDOR_XML),
        ]),
    )
    nl = Netlist(
        components=[
            Component("U1", "STM32F103C8T6", [PinRef("PB6",  "SENSOR_DATA")]),
            Component("U2", "SOME_SENSOR",   [PinRef("DATA", "SENSOR_DATA")]),
        ],
        nets=[Net("SENSOR_DATA", [("U1", "PB6"), ("U2", "DATA")])],
    )
    findings = check_i2c_peripheral(nl, kb)
    assert len(findings) == 0, (
        "Non-fixed-function sensor pin + no name hint → no findings expected; "
        f"got {findings}"
    )


# ── Test 15: ESP32 I2C matrix routing → UNRESOLVABLE ─────────────────────────

def test_esp32_i2c_matrix_routing_unresolvable():
    """
    ESP32 GPIO21 on I2C_SDA net with peripheral_routing saying I2C is MATRIX.
    Even though the pin is in the KB (as GPIO-only), the checker must emit
    exactly one UNRESOLVABLE finding with evidence mentioning matrix routing,
    NOT "not in kb".

    FAILS against the stub (returns []).
    """
    kb = _make_kb(
        PinFunctionEntry("esp32", "GPIO21", [
            PinRole(Peripheral.GPIO, None, Signal.GPIO, KBSource.VENDOR_HEADER),
        ]),
        _sensor_sda_entry("BME280", "SDA"),
    )
    routing = {"esp32": {Peripheral.I2C: PeripheralRouting.MATRIX}}
    nl = Netlist(
        components=[
            Component("U1", "esp32",  [PinRef("GPIO21", "I2C_SDA")]),
            Component("U2", "BME280", [PinRef("SDA",    "I2C_SDA")]),
        ],
        nets=[Net("I2C_SDA", [("U1", "GPIO21"), ("U2", "SDA")])],
    )
    findings = check_i2c_peripheral(nl, kb, peripheral_routing=routing)
    assert len(findings) == 1, f"Expected 1 finding; got {len(findings)}: {findings}"
    assert findings[0].severity == Severity.UNRESOLVABLE
    assert "matrix" in findings[0].evidence.lower(), (
        f"Evidence must mention matrix routing; got {findings[0].evidence!r}"
    )
    assert "not in kb" not in findings[0].evidence.lower(), (
        "PERIPHERAL_UNCONSTRAINED evidence must not say 'not in kb'"
    )


# ── Test 16: STM32 specific MPN canonicalizes to range-form KB key ─────────────

def test_stm32_specific_mpn_canonicalizes_to_range_key():
    """
    KB has range-form key STM32F103C(8-B)Tx; netlist MPN is STM32F103C8T6.
    Checker must canonicalize the MPN and find the entry — no UNRESOLVABLE.

    VACUOUSLY PASSES against stub (returns []); meaningful with real implementation.
    """
    kb = {
        ("STM32F103C(8-B)Tx", "PB7"): _pb7_entry("STM32F103C(8-B)Tx"),
        ("24LC256", "SDA"):            _sensor_sda_entry("24LC256", "SDA"),
    }
    nl = Netlist(
        components=[
            Component("U1", "STM32F103C8T6", [PinRef("PB7", "I2C_SDA")]),
            Component("U2", "24LC256",       [PinRef("SDA", "I2C_SDA")]),
        ],
        nets=[Net("I2C_SDA", [("U1", "PB7"), ("U2", "SDA")])],
    )
    findings = check_i2c_peripheral(nl, kb)
    unresolvable = [f for f in findings if f.severity == Severity.UNRESOLVABLE]
    assert len(unresolvable) == 0, (
        f"PB7 should resolve via MPN canonicalization; got {unresolvable}"
    )


# ── Test 17: ESP32 module MPN canonicalizes to base SoC ──────────────────────

def test_esp32_module_mpn_canonicalizes_to_soc():
    """
    KB has key 'esp32'; netlist MPN is 'ESP32-WROOM-32'.
    Checker canonicalizes ESP32-WROOM-32 → esp32, finds routing MATRIX → UNRESOLVABLE.
    Evidence must mention matrix routing (not "not in kb").

    FAILS against the stub (returns []).
    """
    kb = {
        ("esp32", "GPIO21"): PinFunctionEntry("esp32", "GPIO21", [
            PinRole(Peripheral.GPIO, None, Signal.GPIO, KBSource.VENDOR_HEADER),
        ]),
        ("BME280", "SDA"): _sensor_sda_entry("BME280", "SDA"),
    }
    routing = {"esp32": {Peripheral.I2C: PeripheralRouting.MATRIX}}
    nl = Netlist(
        components=[
            Component("U1", "ESP32-WROOM-32", [PinRef("GPIO21", "I2C_SDA")]),
            Component("U2", "BME280",         [PinRef("SDA",    "I2C_SDA")]),
        ],
        nets=[Net("I2C_SDA", [("U1", "GPIO21"), ("U2", "SDA")])],
    )
    findings = check_i2c_peripheral(nl, kb, peripheral_routing=routing)
    assert len(findings) == 1, f"Expected 1 finding; got {len(findings)}: {findings}"
    assert findings[0].severity == Severity.UNRESOLVABLE
    assert "matrix" in findings[0].evidence.lower(), (
        f"Evidence must mention matrix routing; got {findings[0].evidence!r}"
    )


# ── Test 18: I2C-named net, no I2C-capable pins ──────────────────────────────

def test_i2c_named_net_no_capable_pins_not_classified():
    """
    Net named I2C_PULLUP with only GPIO-only pins (no I2C capability in KB).
    The name matches the I2C pattern but the name-anchored branch requires at
    least one I2C-capable pin — so the net is NOT classified as I2C.
    Zero findings expected.
    """
    kb = _make_kb(
        _pa0_entry("STM32F103C8T6"),  # PA0: TIM2/USART2/GPIO — no I2C
        _pa0_entry("SENSOR_X"),       # GPIO-only, different MPN
    )
    nl = Netlist(
        components=[
            Component("U1", "STM32F103C8T6", [PinRef("PA0", "I2C_PULLUP")]),
            Component("U2", "SENSOR_X",      [PinRef("PA0", "I2C_PULLUP")]),
        ],
        nets=[Net("I2C_PULLUP", [("U1", "PA0"), ("U2", "PA0")])],
    )
    findings = check_i2c_peripheral(nl, kb)
    assert len(findings) == 0, (
        f"Name match alone must not classify as I2C; got {findings}"
    )


# ── Test 19: I2C-named net, one I2C-capable multi-function pin ────────────────

def test_i2c_named_net_with_capable_pin_is_classified():
    """
    Net named I2C_SDA: PB7 is multi-function with I2C capability (confirms
    name-anchored classification). PA0 (GPIO-only) is also on the net.
    Net IS classified → PA0 produces PROTOCOL_MISMATCH.
    """
    kb = _make_kb(
        _pb7_entry(),  # I2C1_SDA / USART1_RX / TIM4 / GPIO — I2C capable
        _pb6_entry(),  # I2C1_SCL / USART1_TX / TIM4 / GPIO — for SCL net
        _pa0_entry(),  # TIM2 / USART2 / GPIO — no I2C capability
    )
    nl = Netlist(
        components=[
            Component("U1", "STM32F103C8T6", [
                PinRef("PB7", "I2C_SDA"),
                PinRef("PA0", "I2C_SDA"),
                PinRef("PB6", "I2C_SCL"),
            ]),
            Component("R1", "4.7k", [PinRef("1", "I2C_SDA"), PinRef("2", "+3V3")]),
            Component("R2", "4.7k", [PinRef("1", "I2C_SCL"), PinRef("2", "+3V3")]),
        ],
        nets=[
            Net("I2C_SDA", [("U1", "PB7"), ("U1", "PA0"), ("R1", "1")]),
            Net("I2C_SCL", [("U1", "PB6"), ("R2", "1")]),
            Net("+3V3",    [("R1", "2"), ("R2", "2")]),
        ],
    )
    findings = check_i2c_peripheral(nl, kb)
    protocol_mismatches = [f for f in findings
                           if f.violation == PeripheralViolation.PROTOCOL_MISMATCH]
    assert len(protocol_mismatches) == 1, (
        f"Expected 1 PROTOCOL_MISMATCH for PA0; got {protocol_mismatches}"
    )
    assert "U1.PA0" in protocol_mismatches[0].pins, (
        f"PROTOCOL_MISMATCH should name U1.PA0; got pins={protocol_mismatches[0].pins}"
    )


# ── Test 20: STM32 tape-and-reel MPN canonicalization ────────────────────────

def test_stm32_tr_mpn_canonicalizes_to_range_key():
    """
    STM32F103C8T6TR (tape-and-reel packaging suffix) must canonicalize to the
    range-form KB key STM32F103C(8-B)Tx, same as STM32F103C8T6.
    Zero UNRESOLVABLE findings expected.
    """
    kb = {
        ("STM32F103C(8-B)Tx", "PB7"): _pb7_entry("STM32F103C(8-B)Tx"),
        ("24LC256", "SDA"):            _sensor_sda_entry("24LC256", "SDA"),
    }
    nl = Netlist(
        components=[
            Component("U1", "STM32F103C8T6TR", [PinRef("PB7", "I2C_SDA")]),
            Component("U2", "24LC256",          [PinRef("SDA", "I2C_SDA")]),
        ],
        nets=[Net("I2C_SDA", [("U1", "PB7"), ("U2", "SDA")])],
    )
    findings = check_i2c_peripheral(nl, kb)
    unresolvable = [f for f in findings if f.severity == Severity.UNRESOLVABLE]
    assert len(unresolvable) == 0, (
        f"PB7 should resolve via TR-suffix canonicalization; got {unresolvable}"
    )


# ── Test 21-23: _resolve_pin matrix short-circuit generalization (2026-07-11) ─
# Direct unit tests of _resolve_pin (not check_i2c_peripheral, which is I2C-only
# and cannot exercise a UART/SPI peripheral arg). Companion to the ESP32
# routing-flag correction (build_kb.py, kb/vendor/esp32/esp32.json) — see
# investigation/experiments/esp32_kb_recon/REPORT.md.

def test_resolve_pin_matrix_short_circuit_generalizes_to_uart():
    """A matrix-flagged UART peripheral pin resolves to PERIPHERAL_UNCONSTRAINED
    when peripheral=Peripheral.UART is passed — the generalized short-circuit,
    not just the I2C-hardcoded one."""
    kb = {("esp32", "GPIO1"): PinFunctionEntry("esp32", "GPIO1", [
        PinRole(Peripheral.UART, "UART0", Signal.UART_TX, KBSource.VENDOR_HEADER),
    ])}
    routing = {"esp32": {
        Peripheral.I2C:  PeripheralRouting.MATRIX,
        Peripheral.UART: PeripheralRouting.MATRIX,
        Peripheral.SPI:  PeripheralRouting.MATRIX,
    }}
    status, entry = _resolve_pin("esp32", "GPIO1", kb, routing, "GPIO1",
                                  peripheral=Peripheral.UART)
    assert status == _LookupStatus.PERIPHERAL_UNCONSTRAINED
    assert entry is None


def test_resolve_pin_i2c_default_unchanged():
    """No peripheral= passed -> defaults to Peripheral.I2C, matching every
    pre-existing call site (check_i2c_peripheral, M14) byte-for-byte."""
    kb = {("esp32", "GPIO21"): PinFunctionEntry("esp32", "GPIO21", [
        PinRole(Peripheral.GPIO, None, Signal.GPIO, KBSource.VENDOR_HEADER),
    ])}
    routing = {"esp32": {Peripheral.I2C: PeripheralRouting.MATRIX}}
    status, entry = _resolve_pin("esp32", "GPIO21", kb, routing, "GPIO21")
    assert status == _LookupStatus.PERIPHERAL_UNCONSTRAINED
    assert entry is None

    # And explicitly passing peripheral=Peripheral.I2C is identical to the default.
    status2, entry2 = _resolve_pin("esp32", "GPIO21", kb, routing, "GPIO21",
                                    peripheral=Peripheral.I2C)
    assert (status2, entry2) == (status, entry)


def test_resolve_pin_fixed_routing_family_unaffected():
    """STM32 (fixed per-pin routing, no MATRIX flags) must never short-circuit,
    for any peripheral argument — the generalization only changes WHICH flag is
    consulted, never introduces a new matrix behavior for a fixed-routing MCU."""
    kb = {("STM32F103C(8-B)Tx", "PB6"): _pb6_entry("STM32F103C(8-B)Tx")}
    routing = {"STM32F103C(8-B)Tx": {
        Peripheral.I2C:  PeripheralRouting.FIXED,
        Peripheral.UART: PeripheralRouting.FIXED,
        Peripheral.TIM:  PeripheralRouting.FIXED,
    }}
    for peripheral in (Peripheral.I2C, Peripheral.UART, Peripheral.TIM):
        status, entry = _resolve_pin("STM32F103C8T6", "PB6", kb, routing, "PB6",
                                      peripheral=peripheral)
        assert status == _LookupStatus.OK, (
            f"STM32 (fixed routing) must resolve normally for peripheral={peripheral}; "
            f"got {status}"
        )
        assert entry is not None


# ── Test 24: multi-net early-return regression (TODO-20 Phase 1) ─────────────

def test_multinet_early_return_does_not_skip_downstream_nets():
    """
    Regression for the netlist-wide early return on the FIRST
    PERIPHERAL_UNCONSTRAINED (matrix-routed) net: before the fix, hitting a
    matrix-routed pin on net 1 made check_i2c_peripheral() `return findings`
    immediately — dropping every later net in netlist.nets AND every
    post-loop cross-net block (MISSING_PERIPHERAL / M6 / M12 / M14 / M15) for
    the WHOLE netlist, not just the matrix-routed net.

    net 1 (I2C_SDA_1): ESP32 GPIO21 <-> BME280 SDA, I2C matrix-routed —
        PERIPHERAL_UNCONSTRAINED (UNRESOLVABLE), same shape as Test 15.
    net 2/3 (I2C_SDA_2 / I2C_SCL_2): an UNRELATED, independently-wired STM32
        <-> 24LC256 I2C bus with a clean SDA<->SCL swap (PB6=SCL-capable
        wired to the SDA net, PB7=SDA-capable wired to the SCL net) — same
        shape as Test 1. This swap is only detectable by the POST-LOOP M6
        coherence block; the per-net checks cannot see it in isolation.

    Pre-fix: the early return after net 1 means the M6 block never runs at
    all, so the swap is silently dropped (only 1 finding, the UNRESOLVABLE).
    Post-fix: 1 UNRESOLVABLE (net 1) + 2 swap FAILs (net 2/3).
    """
    kb = _make_kb(
        PinFunctionEntry("esp32", "GPIO21", [
            PinRole(Peripheral.GPIO, None, Signal.GPIO, KBSource.VENDOR_HEADER),
        ]),
        _sensor_sda_entry("BME280", "SDA"),
        _pb6_entry(),                      # PB6 = I2C1_SCL
        _pb7_entry(),                      # PB7 = I2C1_SDA
        _sensor_sda_entry("24LC256", "SDA"),
        _sensor_scl_entry("24LC256", "SCL"),
    )
    routing = {"esp32": {Peripheral.I2C: PeripheralRouting.MATRIX}}
    nl = Netlist(
        components=[
            # net 1: matrix-routed ESP32 <-> BME280 SDA (I2C_SDA_1)
            Component("U1", "esp32",  [PinRef("GPIO21", "I2C_SDA_1")]),
            Component("U2", "BME280", [PinRef("SDA",    "I2C_SDA_1")]),
            # net 2/3: independent STM32 <-> 24LC256 bus, SDA/SCL swapped
            Component("U3", "STM32F103C8T6", [
                PinRef("PB6", "I2C_SDA_2"),
                PinRef("PB7", "I2C_SCL_2"),
            ]),
            Component("U4", "24LC256", [
                PinRef("SDA", "I2C_SDA_2"),
                PinRef("SCL", "I2C_SCL_2"),
            ]),
            Component("R1", "4.7k", [PinRef("1", "I2C_SDA_2"), PinRef("2", "+3V3")]),
            Component("R2", "4.7k", [PinRef("1", "I2C_SCL_2"), PinRef("2", "+3V3")]),
        ],
        nets=[
            Net("I2C_SDA_1", [("U1", "GPIO21"), ("U2", "SDA")]),
            Net("I2C_SDA_2", [("U3", "PB6"), ("U4", "SDA"), ("R1", "1")]),
            Net("I2C_SCL_2", [("U3", "PB7"), ("U4", "SCL"), ("R2", "1")]),
            Net("+3V3",      [("R1", "2"), ("R2", "2")]),
        ],
    )
    findings = check_i2c_peripheral(nl, kb, peripheral_routing=routing)

    # (i) exactly one PERIPHERAL_UNCONSTRAINED (UNRESOLVABLE) for net 1
    unconstrained = [f for f in findings
                      if f.severity is Severity.UNRESOLVABLE and f.net == "I2C_SDA_1"]
    assert len(unconstrained) == 1, (
        f"Expected exactly 1 UNRESOLVABLE finding for net 1 (I2C_SDA_1); got {findings}"
    )
    assert "matrix" in unconstrained[0].evidence.lower()

    # (ii) net 2/3's swap violation must still be detected — this is the
    # post-loop block the pre-fix early `return findings` silently skipped
    # for the WHOLE netlist the moment net 1 was seen.
    swap_fails = [f for f in findings
                  if f.severity is Severity.FAIL and "SDA/SCL swap" in f.evidence]
    assert len(swap_fails) == 2, (
        f"Expected 2 SDA/SCL swap coherence FAILs on net 2/3; got {findings}"
    )
    assert {f.net for f in swap_fails} == {"I2C_SDA_2", "I2C_SCL_2"}

    # (iii) the downstream cross-net block executed for the whole netlist:
    # net 1's UNRESOLVABLE and net 2/3's post-loop swap FAILs are ALL present
    # together (not net 1 alone).
    assert len(findings) == 3, (
        f"Expected 3 total findings (1 UNRESOLVABLE + 2 swap FAILs); got {findings}"
    )


# ── Test 25-28: matrix-only I2C-named net honest UNRESOLVABLE (TODO-329 C1) ───
# TODO-20 Phase 1 left this gap explicitly deferred: `is_i2c_classified_net`'s
# name-hint gate can never admit a matrix-routed-only I2C net because its
# `any_i2c_capable` check requires a resolved KB entry, but `_resolve_pin`
# short-circuits matrix pins to `entry=None` before lookup (see recon
# investigation/recon_reports — TODO-329). A net with ONLY a matrix-routed
# ESP32 pin (no fixed-function I2C anchor) is silently skipped at the
# Step-1 `continue` — 0 findings, not even the honest UNRESOLVABLE that a
# MIXED net (matrix pin + fixed-function anchor, Test 15/17) already gets
# via Step 3. C1 restores the UNRESOLVABLE for the matrix-ONLY case without
# touching classification, KB, or any other consumer.

def test_matrix_only_i2c_named_net_emits_unresolvable():
    """
    F1: ESP32 GPIO21 (matrix-routed) + an unresolvable header pin on an
    I2C-named net, with NO fixed-function I2C anchor (so `_classify_i2c_net`
    returns False — unlike Test 15/17's MIXED case). Real-corpus shape: the
    ESP32-EVB/ESP32-PoE2 UEXT header nets (recon: 10 such nets, 0 findings
    pre-fix on all of them).

    Pre-fix: 0 findings (net never enters the per-net loop at all).
    Post-fix: exactly 1 UNRESOLVABLE, matrix evidence, same shape as Step 3's
    PERIPHERAL_UNCONSTRAINED finding.
    """
    kb = {}   # J1 is an unresolvable header pin; esp32 routing short-circuits
              # before any KB lookup, so no esp32 KB entry is needed either.
    routing = {"esp32": {Peripheral.I2C: PeripheralRouting.MATRIX}}
    nl = Netlist(
        components=[
            Component("U1", "esp32",        [PinRef("GPIO21", "I2C_SDA")]),
            Component("J1", "CONN_HEADER",  [PinRef("1",      "I2C_SDA")]),
        ],
        nets=[Net("I2C_SDA", [("U1", "GPIO21"), ("J1", "1")])],
    )
    findings = check_i2c_peripheral(nl, kb, peripheral_routing=routing)
    assert len(findings) == 1, (
        f"Expected exactly 1 UNRESOLVABLE for the matrix-only net; got {findings}"
    )
    assert findings[0].severity == Severity.UNRESOLVABLE
    assert findings[0].net == "I2C_SDA"
    assert "matrix" in findings[0].evidence.lower()
    assert "U1.GPIO21" in findings[0].pins


def test_matrix_only_non_i2c_named_net_stays_silent():
    """
    F2: same matrix-only shape as F1, but the net name has no I2C/SDA/SCL
    token — must stay silent both pre- and post-fix (the new check is gated
    on `_I2C_NET_RE`, same pattern Step 1/Step 3 already use).
    """
    kb = {}
    routing = {"esp32": {Peripheral.I2C: PeripheralRouting.MATRIX}}
    nl = Netlist(
        components=[
            Component("U1", "esp32",       [PinRef("GPIO21", "GPIO21_MISC")]),
            Component("J1", "CONN_HEADER", [PinRef("1",       "GPIO21_MISC")]),
        ],
        nets=[Net("GPIO21_MISC", [("U1", "GPIO21"), ("J1", "1")])],
    )
    findings = check_i2c_peripheral(nl, kb, peripheral_routing=routing)
    assert findings == [], (
        f"Non-I2C-named matrix-only net must stay silent; got {findings}"
    )


def test_matrix_only_net_still_not_classified():
    """
    F3: `is_i2c_classified_net` (the shared step_08g suppression gate) must
    still return False for F1's net — the C1 fix touches only
    `check_i2c_peripheral`'s own not-classified branch, never the shared
    classification gate, so step_08g's suppression semantics are unchanged
    (a matrix-only net is not ceded to step_08d/M3; it just was never
    reachable as I2C-classified by either checker).
    """
    kb = {}
    routing = {"esp32": {Peripheral.I2C: PeripheralRouting.MATRIX}}
    nl = Netlist(
        components=[
            Component("U1", "esp32",        [PinRef("GPIO21", "I2C_SDA")]),
            Component("J1", "CONN_HEADER",  [PinRef("1",      "I2C_SDA")]),
        ],
        nets=[Net("I2C_SDA", [("U1", "GPIO21"), ("J1", "1")])],
    )
    net = nl.nets[0]
    assert is_i2c_classified_net(net, nl, kb, routing) is False


def test_mixed_matrix_net_single_emission_unchanged():
    """
    F4: a MIXED net (matrix pin + a fixed-function I2C anchor, same shape as
    Test 15/17) IS classified by `_classify_i2c_net` — so it never reaches
    the new not-classified branch C1 adds. Must still emit exactly 1
    UNRESOLVABLE via the pre-existing Step 3 path, with no double-emission
    from the new code (the two paths are mutually exclusive by construction:
    the new check only runs inside the `if not _classify_i2c_net(...)`
    branch).
    """
    kb = _make_kb(
        PinFunctionEntry("esp32", "GPIO21", [
            PinRole(Peripheral.GPIO, None, Signal.GPIO, KBSource.VENDOR_HEADER),
        ]),
        _sensor_sda_entry("BME280", "SDA"),
    )
    routing = {"esp32": {Peripheral.I2C: PeripheralRouting.MATRIX}}
    nl = Netlist(
        components=[
            Component("U1", "esp32",  [PinRef("GPIO21", "I2C_SDA")]),
            Component("U2", "BME280", [PinRef("SDA",    "I2C_SDA")]),
        ],
        nets=[Net("I2C_SDA", [("U1", "GPIO21"), ("U2", "SDA")])],
    )
    findings = check_i2c_peripheral(nl, kb, peripheral_routing=routing)
    assert len(findings) == 1, (
        f"MIXED net must still emit exactly 1 UNRESOLVABLE (no double-emission "
        f"from the new matrix-only branch); got {findings}"
    )
    assert findings[0].severity == Severity.UNRESOLVABLE
    assert "matrix" in findings[0].evidence.lower()


# ── Future test ideas (do not add yet — document as TODO) ─────────────────────
# TODO: SDA net with pull-up to an unconfirmed voltage rail (e.g. a generic
#       net that wasn't resolved by power analysis) → UNRESOLVABLE or WARN?
# TODO: I2C net with multiple MCUs having conflicting instances across a shared bus
# TODO: UART TX/RX on an I2C-named net (wrong-peripheral protocol mismatch)

"""Tests for the TODO-93 UART wrong-destination capability check (steps/uart_capability.py).

Two layers, deliberately separate:
  * the CLASSIFIER (classify_netlist_uart) — the decision, tested in isolation;
  * the LIVE EMIT (step_08d check_i2c_peripheral) — that a decision becomes a
    UART_CAPABILITY_MISMATCH FAIL, and that the INVARIANT holds at the emit site:
    a FAIL fires ONLY on KB-confirmed incapability (matrix / KB-absent / CAN-suppressed /
    unconfirmed can never produce one). Wired live in Phase 2 (2026-07-11).

See recon `investigation/experiments/uart_wrong_destination_recon/REPORT.md` Q1/Q2 for
the endpoint-confirmed (never net-name) mechanism this suite locks in.
"""
import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from steps.peripheral_kb import Signal, KBSource, Peripheral, PeripheralRouting, PinRole, PinFunctionEntry
from steps.uart_capability import classify_netlist_uart
from steps.step_08d_peripheral_checker import (
    check_i2c_peripheral, PeripheralViolation, Severity,
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
    value:  str = ""
    @property
    def effective_mpn(self): return self.mpn


@dataclass
class Net:
    name: str
    pins: list


@dataclass
class Netlist:
    components:  list
    nets:        list
    ground_nets: list = field(default_factory=list)   # step_08d's live entry point reads these
    power_nets:  list = field(default_factory=list)


def _uart_entry(mpn, pin_id, signal, instance=None):
    return PinFunctionEntry(mpn, pin_id, [PinRole(Peripheral.UART, instance, signal, KBSource.VENDOR_XML)])


def _non_uart_entry(mpn, pin_id):
    """A KB-resolved pin with real capability data but ZERO UART roles (e.g. an
    ADC-only pin) — the INCAPABLE shape."""
    return PinFunctionEntry(mpn, pin_id, [PinRole(Peripheral.UNKNOWN, None, Signal.GPIO, KBSource.VENDOR_XML)])


def _i2c_and_uart_entry(mpn, pin_id, i2c_signal, uart_signal):
    """The real STM32F103 PB6/PB7 shape: an I2C1 pin whose alt-fn map ALSO lists a
    USART role. `possible_roles` is a mux MAP, not a wiring decision — see the
    option-(a) regression test below."""
    return PinFunctionEntry(mpn, pin_id, [
        PinRole(Peripheral.I2C, "I2C1", i2c_signal, KBSource.VENDOR_XML),
        PinRole(Peripheral.UART, "USART1", uart_signal, KBSource.VENDOR_XML),
        PinRole(Peripheral.UNKNOWN, None, Signal.GPIO, KBSource.VENDOR_XML),
    ])


def _make_kb(*entries):
    return {(e.mpn, e.pin_id): e for e in entries}


# ── Test 1: incapable endpoint on a confirmed-USART net -> WOULD_FIRE ─────────
# The net is confirmed by the bridge's own pin-function (TXD_2 -> an ACTUAL
# function), the only confirming source there is. The victim's KB entry carries
# zero UART roles -> KB-confirmed incapable.

def test_incapable_endpoint_on_confirmed_usart_net_fires():
    kb = _make_kb(
        _non_uart_entry("ADCCHIP", "P1"),
    )
    nl = Netlist(
        components=[
            Component("U6", "CH340C", [PinRef("2", "N1", "TXD_2")], value="CH340C"),
            Component("U9", "ADCCHIP", [PinRef("P1", "N1", "P1_1")]),
        ],
        # deliberately non-UART-suggestive net name — proves the net-name text
        # itself is not load-bearing for this finding.
        nets=[Net("N1", [("U6", "2"), ("U9", "P1")])],
    )
    result = classify_netlist_uart(nl, kb, {}, board="synthetic")
    assert len(result.findings) == 1
    f = result.findings[0]
    assert f.status == "WOULD_FIRE"
    assert f.net == "N1"
    assert f.incapable_endpoint.refdes == "U9"
    assert f.incapable_endpoint.pin_id == "P1"
    assert "UART" not in f.incapable_endpoint.kb_possible_peripherals
    assert len(f.confirming_endpoints) == 1
    assert f.confirming_endpoints[0].refdes == "U6"
    assert f.confirming_endpoints[0].source == "pinfunc_endpoint"
    assert result.confirmed_usart_nets == 1


# ── Test 2: a correct USART link (both endpoints capable) -> no-fire ─────────

def test_correct_usart_link_both_capable_does_not_fire():
    kb = _make_kb(
        _uart_entry("MCU", "PA9", Signal.UART_TX, instance="USART1"),
    )
    nl = Netlist(
        components=[
            Component("U1", "MCU", [PinRef("PA9", "N2", "PA9_42")]),
            # bridge chip, not KB'd at all; its own pin-function literally
            # reads RXD -> confirms the net, independent of net name.
            Component("U2", "FT232RL", [PinRef("2", "N2", "RXD")]),
        ],
        nets=[Net("N2", [("U1", "PA9"), ("U2", "2")])],
    )
    result = classify_netlist_uart(nl, kb, {}, board="synthetic")
    assert result.findings == []
    assert result.confirmed_usart_nets == 1
    # U1 is a candidate victim (only U2's pin-function confirms), and its KB entry
    # DOES carry a UART role -> capable, cleared, never a finding.
    assert [c["reason"] for c in result.cleared] == ["HAS_UART_ROLE"]


# ── Test 2b (option (a), 2026-07-11): a KB UART possible_role NEVER confirms ──
# THE REGRESSION TEST FOR THE PHASE-2 STOP. Real KB: STM32F103 PB6/PB7 are the
# standard I2C1 pinout AND carry USART1 tx/rx as alternate functions. When a KB
# UART possible_role was allowed to confirm a net (the removed source (b)), every
# correctly-wired STM32 I2C bus was read as a "confirmed USART net" and the KB'd
# device at the far end (zero UART roles — an EEPROM's SDA genuinely cannot do
# UART) became a false wrong-destination FAIL. Six correct-I2C fixtures caught it.
#
# Expressible only with TWO KB'd parts: on the real corpus the peripheral KB holds
# 5 MCU-only parts, so every far-end victim was cleared as KB-absent and the bug
# measured a clean 0 through Phases 1/1.5. KB sparsity was masking it, not logic.

def test_kb_uart_possible_role_alone_never_confirms_a_correct_i2c_bus():
    kb = _make_kb(
        # the F103 shape: I2C1 SCL/SDA pins that ALSO list USART1 tx/rx alt-fns
        _i2c_and_uart_entry("MCU", "PB6", Signal.I2C_SCL, Signal.UART_TX),
        _i2c_and_uart_entry("MCU", "PB7", Signal.I2C_SDA, Signal.UART_RX),
        # the far-end EEPROM: KB-resolved, zero UART roles -> the would-be victim
        _non_uart_entry("EEPROM", "5"),
        _non_uart_entry("EEPROM", "6"),
    )
    nl = Netlist(
        components=[
            Component("U1", "MCU", [PinRef("PB6", "/SCL", "PB6_92"), PinRef("PB7", "/SDA", "PB7_93")]),
            Component("U2", "EEPROM", [PinRef("6", "/SCL", "SCL"), PinRef("5", "/SDA", "SDA")]),
        ],
        nets=[Net("/SCL", [("U1", "PB6"), ("U2", "6")]),
              Net("/SDA", [("U1", "PB7"), ("U2", "5")])],
    )
    result = classify_netlist_uart(nl, kb, {}, board="synthetic")
    assert result.confirmed_usart_nets == 0   # an alt-fn map is not a wiring decision
    assert result.findings == []
    # and live: a correct I2C bus emits no UART finding at all
    assert _uart_findings(check_i2c_peripheral(nl, kb, {})) == []


# ── Test 3: net-name-only "USART"-suggestive net, no endpoint confirmation ────
# THE MOST IMPORTANT TEST — proves net-name is NEVER consulted. If the
# classifier fell back to net-name matching (the reverted TODO-93 mechanism),
# this net's name alone would look like a UART TX net and something would be
# reported. Neither member here independently confirms UART via KB or
# pin-function, so classify_netlist_uart must produce ZERO findings and must
# not even count this net as "confirmed" (confirmed_usart_nets stays 0).

def test_uart_suggestive_net_name_alone_never_fires():
    kb = {}   # no KB coverage at all — irrelevant to this test either way
    nl = Netlist(
        components=[
            Component("J1", "GENERIC_CONN", [PinRef("1", "/USART3_TX", "1_1")]),
            Component("J2", "GENERIC_CONN", [PinRef("2", "/USART3_TX", "2_2")]),
        ],
        # net NAME strongly implies UART TX, but neither member's pin-function
        # or KB entry asserts a UART role independently.
        nets=[Net("/USART3_TX", [("J1", "1"), ("J2", "2")])],
    )
    result = classify_netlist_uart(nl, kb, {}, board="synthetic")
    assert result.findings == []
    assert result.confirmed_usart_nets == 0
    # neither pin ever became a candidate victim -> nothing to clear either,
    # because "not confirmed" short-circuits before the incapable-endpoint scan.
    assert result.cleared == []


# ── Test 4: KB-absent endpoint on an otherwise-confirmed net -> UNRESOLVABLE,
# never counted as incapable, never fires ────────────────────────────────────
# (This test covers the KB-absence half of the "KB-absent or matrix-routed"
# boundary rule. See test_esp32_style_matrix_uart_routing_clears_never_incapable
# below for the matrix-routed half, now reachable since Phase 1.5 wired
# peripheral=Peripheral.UART into the `_resolve_pin` call.)

def test_kb_absent_endpoint_on_confirmed_net_is_unresolvable_never_fires():
    kb = {}   # nothing registered for "MYSTERY_IC" at all -> KB_MISSING
    nl = Netlist(
        components=[
            Component("U6", "CH340C", [PinRef("2", "N3", "TXD_2")], value="CH340C"),
            Component("U7", "MYSTERY_IC", [PinRef("3", "N3", "3_3")]),
        ],
        nets=[Net("N3", [("U6", "2"), ("U7", "3")])],
    )
    result = classify_netlist_uart(nl, kb, {}, board="synthetic")
    assert result.findings == []
    assert result.confirmed_usart_nets == 1
    assert len(result.cleared) == 1
    cleared = result.cleared[0]
    assert cleared["refdes"] == "U7"
    assert cleared["reason"] == "KB_ABSENT_OR_MATRIX_UNRESOLVABLE"
    assert cleared["lookup_status"] == "kb_missing"


# ── Phase 1.5 — Test 5: B1 CAN/LIN guard ───────────────────────────────────────
# A CAN transceiver's own TXD/RXD pin-function string matches the UART
# pin-function pattern (Role.UART_TX/UART_RX) just as literally as a real
# USB-UART bridge's — the SIT1050T on the real OLIMEXINO-STM32F3 board is the
# live validation case (see phase1_5_seed/). Without the guard this net would
# false-confirm as USART and flag its MCU pin (which legitimately has zero
# UART roles, being a CAN pin) as a wrong-destination incapable victim.

def test_can_transceiver_txd_pinfunction_suppressed_not_confirmed():
    kb = _make_kb(
        _non_uart_entry("MCU", "PB9"),   # KB-resolved, zero UART roles (real CAN pin)
    )
    nl = Netlist(
        components=[
            Component("U3", "SIT1050T", [PinRef("1", "N5", "TXD_1")]),
            Component("U5", "MCU", [PinRef("PB9", "N5", "PB9_62")]),
        ],
        nets=[Net("/D24_CANTX", [("U3", "1"), ("U5", "PB9")])],
    )
    result = classify_netlist_uart(nl, kb, {}, board="synthetic")
    assert result.findings == []
    # the net is never even confirmed -- U3's pinfunc source is suppressed, and
    # pin-function is the ONLY confirming source there is.
    assert result.confirmed_usart_nets == 0
    assert result.cleared == []


def test_ch340_txd_pinfunction_still_confirms_not_a_can_part():
    """The B1 guard must not over-suppress: a genuine USB-UART bridge's TXD
    (CH340C, not a CAN/LIN transceiver value) confirms exactly as before."""
    kb = _make_kb(
        _non_uart_entry("MCU", "PB2"),
    )
    nl = Netlist(
        components=[
            Component("U6", "CH340C", [PinRef("2", "N6", "TXD_2")]),
            Component("U5", "MCU", [PinRef("PB2", "N6", "PB2_28")]),
        ],
        nets=[Net("N6", [("U6", "2"), ("U5", "PB2")])],
    )
    result = classify_netlist_uart(nl, kb, {}, board="synthetic")
    assert result.confirmed_usart_nets == 1
    assert len(result.findings) == 1
    assert result.findings[0].confirming_endpoints[0].refdes == "U6"
    assert result.findings[0].confirming_endpoints[0].source == "pinfunc_endpoint"


# ── Phase 1.5 — Test 6: UART matrix short-circuit, via peripheral=Peripheral.UART ──
# 8eceea5 corrected the ESP32 KB's peripheral_routing to flag uart "matrix"
# (was wrongly "fixed"). This diagnostic now passes peripheral=Peripheral.UART
# into `_resolve_pin`, so a matrix-routed part's pin is treated as genuinely
# unconstrained (never a candidate incapable victim) via ITS OWN uart flag —
# not by accidentally falling through I2C's (also-matrix, for ESP32) flag.
# Routing here deliberately sets ONLY uart to MATRIX (i2c absent) so a pass
# under the old implicit Peripheral.I2C default would MISS the short-circuit
# entirely and fall through to a KB miss instead of PERIPHERAL_UNCONSTRAINED.

def test_esp32_style_matrix_uart_routing_clears_never_incapable():
    kb = {}   # MCU entirely absent from the KB -- irrelevant, routing gates first
    routing = {"matrixmcu": {Peripheral.UART: PeripheralRouting.MATRIX}}
    nl = Netlist(
        components=[
            Component("U6", "CH340C", [PinRef("2", "N7", "TXD_2")]),
            Component("U9", "matrixmcu", [PinRef("7", "N7", "IO7_7")]),
        ],
        nets=[Net("N7", [("U6", "2"), ("U9", "7")])],
    )
    result = classify_netlist_uart(nl, kb, routing, board="synthetic")
    assert result.findings == []
    assert result.confirmed_usart_nets == 1
    assert len(result.cleared) == 1
    assert result.cleared[0]["refdes"] == "U9"
    assert result.cleared[0]["reason"] == "KB_ABSENT_OR_MATRIX_UNRESOLVABLE"
    assert result.cleared[0]["lookup_status"] == "peripheral_unconstrained"


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2 — LIVE EMIT (step_08d check_i2c_peripheral -> UART_CAPABILITY_MISMATCH)
#
# The classifier tests above prove the DECISION. These prove the WIRING, and that
# THE INVARIANT survives it: a FAIL fires ONLY on KB-confirmed incapability. The
# emit is FAIL-only — a cleared or unconfirmed net emits nothing at all.
# ══════════════════════════════════════════════════════════════════════════════

def _uart_findings(findings):
    return [f for f in findings
            if f.violation == PeripheralViolation.UART_CAPABILITY_MISMATCH]


def _uart_fails(findings):
    return [f for f in _uart_findings(findings) if f.severity == Severity.FAIL]


def test_live_incapable_pin_emits_uart_capability_fail_with_evidence():
    """The positive control, in the shape of the constructed seed's mutant: a CH340C
    bridge's TXD confirms the net (source c), the MCU pin that landed on it has ZERO
    UART roles in the KB -> one UART_CAPABILITY_MISMATCH FAIL, carrying the incapable
    pin, its actual KB roles, and the confirming endpoint."""
    kb = _make_kb(_non_uart_entry("STM32F303RCT6", "PB2"))
    nl = Netlist(
        components=[
            Component("U6", "CH340C", [PinRef("2", "/D30_USART3RX", "TXD_2")], value="CH340C"),
            Component("U5", "STM32F303RCT6", [PinRef("28", "/D30_USART3RX", "PB2")],
                      value="STM32F303RCT6"),
        ],
        nets=[Net("/D30_USART3RX", [("U6", "2"), ("U5", "28")])],
    )
    fails = _uart_fails(check_i2c_peripheral(nl, kb, {}))
    assert len(fails) == 1
    f = fails[0]
    assert f.net == "/D30_USART3RX"
    assert f.pins == ["U5.28"]
    # the evidence names the incapable pin, its KB roles, and WHO confirmed the net
    assert "KB alt-fn roles exclude UART on U5.PB2" in f.evidence
    assert "U6.TXD_2" in f.evidence
    assert "pinfunc_endpoint" in f.evidence
    assert "net name never consulted" in f.evidence


def test_live_kb_absent_pin_never_emits_a_uart_fail():
    """INVARIANT (KB-absence half): the destination has no KB entry at all, so the
    checker CANNOT assert incapability -> it stands down. A FAIL here would be a false
    positive (this is the shape of M14's Variant-B stand-down control)."""
    kb = {}   # MYSTERY_IC not in the KB -- nor is anything else
    nl = Netlist(
        components=[
            Component("U6", "CH340C", [PinRef("2", "/D30_USART3RX", "TXD_2")], value="CH340C"),
            Component("U7", "MYSTERY_IC", [PinRef("3", "/D30_USART3RX", "3_3")], value="MYSTERY_IC"),
        ],
        nets=[Net("/D30_USART3RX", [("U6", "2"), ("U7", "3")])],
    )
    assert _uart_findings(check_i2c_peripheral(nl, kb, {})) == []


def test_live_matrix_routed_pin_never_emits_a_uart_fail():
    """INVARIANT (matrix half): an ESP32-class part routes UART through a GPIO matrix,
    so ANY pin can serve UART -- 'incapable' is not a statement the KB can make. The
    routing flag short-circuits to PERIPHERAL_UNCONSTRAINED (8eceea5) and the pin is
    cleared, never flagged."""
    kb = {}
    routing = {"matrixmcu": {Peripheral.UART: PeripheralRouting.MATRIX}}
    nl = Netlist(
        components=[
            Component("U6", "CH340C", [PinRef("2", "N7", "TXD_2")], value="CH340C"),
            Component("U9", "matrixmcu", [PinRef("7", "N7", "IO7_7")], value="matrixmcu"),
        ],
        nets=[Net("N7", [("U6", "2"), ("U9", "7")])],
    )
    assert _uart_findings(check_i2c_peripheral(nl, kb, routing)) == []


def test_live_can_transceiver_net_is_silent_not_a_uart_fail():
    """INVARIANT (CAN/LIN guard): a CAN transceiver's TXD pin-function matches the UART
    pattern just as literally as a bridge's. Without the B1 guard the SIT1050T on the real
    OLIMEXINO board would false-confirm /D24_CANTX as USART and flag the MCU's CAN pin
    (which legitimately has zero UART roles) as a wrong destination. Guard -> silence."""
    kb = _make_kb(_non_uart_entry("STM32F303RCT6", "PB9"))   # real CAN pin: no UART role
    nl = Netlist(
        components=[
            Component("U3", "SIT1050T", [PinRef("1", "/D24_CANTX", "TXD_1")], value="SIT1050T"),
            Component("U5", "STM32F303RCT6", [PinRef("62", "/D24_CANTX", "PB9")],
                      value="STM32F303RCT6"),
        ],
        nets=[Net("/D24_CANTX", [("U3", "1"), ("U5", "62")])],
    )
    assert _uart_findings(check_i2c_peripheral(nl, kb, {})) == []


def test_live_unconfirmed_net_is_silent_even_with_a_uart_shaped_name():
    """INVARIANT (confirmation half) + the anti-regression guard for the REVERTED
    net-name mechanism (7924d53): a net NAMED /USART3_TX whose members assert no UART
    role via KB or pin-function is not evaluated at all. Net names are never consulted;
    a FAIL here would mean net-name matching had crept back in."""
    kb = _make_kb(_non_uart_entry("ADCCHIP", "P1"))
    nl = Netlist(
        components=[
            Component("J1", "GENERIC_CONN", [PinRef("1", "/USART3_TX", "1_1")], value="Conn_01x04"),
            Component("U9", "ADCCHIP", [PinRef("P1", "/USART3_TX", "P1_1")], value="ADCCHIP"),
        ],
        nets=[Net("/USART3_TX", [("J1", "1"), ("U9", "P1")])],
    )
    assert _uart_findings(check_i2c_peripheral(nl, kb, {})) == []

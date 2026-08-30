"""M14-F1 KB'd-doubles regression (promoted from the Evidence-Source Audit's
PB6/PB7 probe: investigation/experiments/evidence_source_audit/m14_pairing/
probe_pb6_pb7_uart.py, via investigation/experiments/m14_f1_recon/REPORT.md).

PB6/PB7 is the standard STM32F103 I2C1 pinout (SDA/SCL) but those SAME pins
are ALSO USART1_TX/RX-possible in real silicon (and therefore in any KB built
from the vendor alt-fn table). A genuine UART1-on-PB6/PB7 design gets PAIRED
by peripheral_bus_pairing.pair_buses as bus "I2C1" (pairing signal 1 reads KB
INSTANCE POSSIBILITY, not actual function) -- net names here (USART1_TX/RX)
give zero independent corroboration. Before M14-F1 this was a latent
false-positive surface: harmless only while the KB has no second KB'd part on
those nets (Scenario A) -- the moment a UART-only bridge chip is KB'd
(Scenario B, "KB widened to non-MCU parts"), 2 false CAPABILITY_MISMATCH FAILs
fired on a correct design. THE FIX (has_independent_identity gate,
peripheral_bus_pairing.py): a kb_instance-only pairing may never drive a
verdict-moving FAIL. Post-fix, both scenarios must stand down -- Scenario A's
stand-down becomes DELIBERATE (asserted here via pairing_source) rather than
an accident of KB sparsity.

Uses the REAL code path end-to-end: pair_buses + evaluate_bus_consensus + the
live M14 emission block in check_i2c_peripheral (not a reimplementation) --
same invariant-testing style as test_capability_misroute_checker.py.
"""
import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from steps.peripheral_kb import (
    Signal, KBSource, Peripheral, PinRole, PinFunctionEntry,
)
from steps.step_08d_peripheral_checker import (
    check_i2c_peripheral, PeripheralViolation, Severity,
)
from steps.peripheral_bus_pairing import pair_buses, AMBIGUOUS_PAIRING
from steps.peripheral_coherence import I2C_PAIR, _KB_SIGNAL_TO_ROLE


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


# PB6/PB7 genuinely carry BOTH I2C1 (SCL/SDA) and USART1 (TX/RX) alt-functions
# in real silicon -- exactly like a vendor-XML KB build would emit for any
# KB'd STM32F1 part. Realistic shape, not an artificial two-role stub: a
# fixed-mux MCU pin's KB entry lists EVERY alt-fn the silicon supports, not
# just the one the design actually uses.
_KB_MCU_ONLY = _make_kb(
    PinFunctionEntry("STM32F103", "PB6", [
        PinRole(Peripheral.I2C, "I2C1", Signal.I2C_SCL, KBSource.VENDOR_XML),
        PinRole(Peripheral.UART, "USART1", Signal.UART_TX, KBSource.VENDOR_XML),
    ]),
    PinFunctionEntry("STM32F103", "PB7", [
        PinRole(Peripheral.I2C, "I2C1", Signal.I2C_SDA, KBSource.VENDOR_XML),
        PinRole(Peripheral.UART, "USART1", Signal.UART_RX, KBSource.VENDOR_XML),
    ]),
)


def _netlist():
    """MCU PB6/PB7 wired to a USB-UART bridge's TXD/RXD pins, with UART-shaped
    net names (NOT I2C-shaped) -- a genuinely-correct UART1 design."""
    return Netlist(
        components=[
            Component("U1", "STM32F103", [
                PinRef("PB6", "USART1_TX", "PB6"),
                PinRef("PB7", "USART1_RX", "PB7"),
            ]),
            Component("U2", "CH340C", [
                PinRef("RXD", "USART1_TX", "RXD"),   # bridge RXD listens to MCU TXD
                PinRef("TXD", "USART1_RX", "TXD"),   # bridge TXD drives MCU RXD
            ]),
        ],
        nets=[
            Net("USART1_TX", [("U1", "PB6"), ("U2", "RXD")]),
            Net("USART1_RX", [("U1", "PB7"), ("U2", "TXD")]),
        ],
    )


def _cap_fails(findings):
    return [f for f in findings
            if f.violation == PeripheralViolation.CAPABILITY_MISMATCH
            and f.severity == Severity.FAIL]


def _pairing(nl, kb):
    paired, unpaired = pair_buses(
        nl, "I2C", I2C_PAIR, kb=kb, peripheral_routing={},
        kb_signal_to_role=_KB_SIGNAL_TO_ROLE,
    )
    return paired, unpaired


def test_scenario_a_bridge_not_kbd_stands_down_and_still_pairs_kb_instance():
    """CURRENT REALITY -- the bridge chip is NOT KB'd (peripheral_kb.py today =
    MCU-only parts). Pairing still identifies (mispairs) the UART-named nets
    as bus "I2C1" via kb_instance -- diagnostic pairing behavior is UNCHANGED
    by the M14-F1 fix. No CAPABILITY_MISMATCH fires: pre-fix this was an
    accident of KB sparsity (no second KB'd part to serve as the incapable
    candidate); post-fix it is DELIBERATE (the bus was never admissible for a
    capability verdict, regardless of what the KB contains)."""
    nl = _netlist()
    paired, _unpaired = _pairing(nl, _KB_MCU_ONLY)
    real_buses = [b for b in paired if b.pairing_source != AMBIGUOUS_PAIRING]
    assert len(real_buses) == 1
    assert real_buses[0].pairing_source == "kb_instance"
    assert real_buses[0].bus_id == "I2C1"

    findings = check_i2c_peripheral(nl, _KB_MCU_ONLY, {})
    assert _cap_fails(findings) == []


def test_scenario_b_bridge_kbd_uart_only_no_false_fail():
    """KB WIDENED -- the bridge chip (e.g. CH340-class USB-UART, a non-MCU
    part) IS KB'd with UART-only roles (no I2C role at all -- an
    ACTUAL-FUNCTION-sound KB entry on its own pins). Pairing STILL mispairs
    the bus as "I2C1" (kb_instance, unaffected by the KB-widening) -- but
    THE FIX means the capability path may not act on that identity: 0
    CAPABILITY_MISMATCH findings, where pre-fix this fired 2 false FAILs on a
    correct UART1 design."""
    kb = dict(_KB_MCU_ONLY)
    kb[("CH340C", "RXD")] = PinFunctionEntry(
        "CH340C", "RXD", [PinRole(Peripheral.UART, None, Signal.UART_RX, KBSource.VENDOR_HEADER)])
    kb[("CH340C", "TXD")] = PinFunctionEntry(
        "CH340C", "TXD", [PinRole(Peripheral.UART, None, Signal.UART_TX, KBSource.VENDOR_HEADER)])
    nl = _netlist()

    paired, _unpaired = _pairing(nl, kb)
    real_buses = [b for b in paired if b.pairing_source != AMBIGUOUS_PAIRING]
    assert len(real_buses) == 1
    assert real_buses[0].pairing_source == "kb_instance", (
        "pairing behavior itself must be unchanged by the M14-F1 gate -- "
        "only verdict admissibility changes")
    assert real_buses[0].bus_id == "I2C1"

    findings = check_i2c_peripheral(nl, kb, {})
    fails = _cap_fails(findings)
    assert fails == [], (
        f"M14-F1 regression: kb_instance-only bus identity produced a "
        f"verdict-moving CAPABILITY_MISMATCH FAIL on a correct UART1 design: {fails}")

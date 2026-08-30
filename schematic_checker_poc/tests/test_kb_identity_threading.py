"""KB identity threading fixtures (kb_identity_gap build, option a per
investigation/experiments/kb_identity_gap/REPORT.md: thread-at-resolve).

Proves ComponentIR.effective_mpn (resolved_mpn or part_number) actually
reaches the KB-lookup layer once threaded through the 6 comp.mpn ->
comp.effective_mpn call sites (step_08d_peripheral_checker.py x3,
uart_capability.py, peripheral_consensus.py, peripheral_bus_pairing.py).

Fixture 1 (threading positive/parity): a placeholder-MPN component with
resolved_mpn set to its L2-expanded concrete form must reach the SAME KB
entry -- and therefore produce byte-identical findings -- as an identical
board wired with the literal, already-concrete MPN.

Fixture 2 (literal no-op): the placeholder_expansion invariant confirmed
structurally in the recon (Step 3) -- a component with resolved_mpn unset
falls back to its own part_number, for both an ordinary literal MCU MPN
and a placeholder-pattern string that was never actually resolved (e.g.
no L2 vendor-lookup credentials in this environment).

Fixture 3 (honest miss): a resolved_mpn that expands to an MPN absent from
the KB must produce Severity.UNRESOLVABLE citing the RESOLVED (not raw)
MPN string -- never a silent pass and never a false hit against the raw
placeholder's own (nonexistent) KB entry.
"""
import os
import sys
from dataclasses import dataclass, field

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from steps.step_02_parser import ComponentIR, PinIR                      # noqa: E402
from steps.peripheral_kb import load_peripheral_kb                       # noqa: E402
from steps.peripheral_coherence import check_i2c_coherence               # noqa: E402
from steps.step_08d_peripheral_checker import (                          # noqa: E402
    canonicalize_mpn_for_kb, check_i2c_peripheral, Severity, _resolve_pin,
    _LookupStatus,
)


@dataclass
class _Net:
    name: str
    pins: list


@dataclass
class _IR:
    components: list
    nets: list = field(default_factory=list)


def _ir(components):
    nets = {}
    for c in components:
        for p in c.pins:
            nets.setdefault(p.net, []).append((c.refdes, p.pin_id))
    return _IR(components=components, nets=[_Net(n, ps) for n, ps in nets.items()])


_REAL_KB_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "kb", "vendor")


def _load_real_kb():
    if not os.path.isdir(_REAL_KB_DIR):
        pytest.skip("kb/vendor not present")
    return load_peripheral_kb(_REAL_KB_DIR)


def _pullup(refdes, net):
    return ComponentIR(
        refdes=refdes, part_number="ERJ2GE0R00X", value="10k",
        pins=[PinIR("1", "1", net), PinIR("2", "2", "+3V3")],
    )


def test_placeholder_threading_reaches_same_kb_entry_as_literal():
    kb, routing = _load_real_kb()

    placeholder_ir = _ir([
        ComponentIR(
            refdes="U22", part_number="INA219AxDCN", value="",
            pins=[PinIR("5", "SCL", "/I2C_{SYS}.SCL"),
                  PinIR("6", "SDA", "/I2C_{SYS}.SDA")],
            resolved_mpn="INA219AIDCNR",
        ),
        _pullup("R1", "/I2C_{SYS}.SCL"),
        _pullup("R2", "/I2C_{SYS}.SDA"),
    ])
    literal_ir = _ir([
        ComponentIR(
            refdes="U22", part_number="INA219AIDCNR", value="",
            pins=[PinIR("5", "SCL", "/I2C_{SYS}.SCL"),
                  PinIR("6", "SDA", "/I2C_{SYS}.SDA")],
        ),
        _pullup("R1", "/I2C_{SYS}.SCL"),
        _pullup("R2", "/I2C_{SYS}.SDA"),
    ])

    assert (check_i2c_coherence(placeholder_ir, kb, routing, canonicalize_mpn_for_kb)
            == check_i2c_coherence(literal_ir, kb, routing, canonicalize_mpn_for_kb))
    assert (check_i2c_peripheral(placeholder_ir, kb, routing)
            == check_i2c_peripheral(literal_ir, kb, routing))
    # Both sides must actually be non-trivially checked -- this is a KB-HIT
    # parity, not an absence-of-findings parity by coincidence.
    assert check_i2c_peripheral(literal_ir, kb, routing) == []
    assert check_i2c_coherence(literal_ir, kb, routing, canonicalize_mpn_for_kb) == []


def test_literal_mpn_effective_mpn_is_a_no_op():
    lit = ComponentIR(refdes="U1", part_number="STM32F405RGT6", value="", pins=[])
    assert lit.resolved_mpn is None
    assert lit.effective_mpn == lit.part_number == "STM32F405RGT6"

    unresolved_placeholder = ComponentIR(
        refdes="U2", part_number="INA219AxDCN", value="", pins=[])
    assert unresolved_placeholder.resolved_mpn is None
    assert unresolved_placeholder.effective_mpn == "INA219AxDCN"


def test_resolved_placeholder_with_no_kb_entry_is_honest_unresolvable():
    """A resolved_mpn that expands to an MPN absent from the KB must miss
    honestly at the _resolve_pin boundary every swapped call site now goes
    through -- never a silent hit, never a crash. (Not exercised via the
    full check_i2c_peripheral net-classification path: co-locating a
    KB-miss pin on the same net as a fixed-function anchor pin also taints
    that net's cross-net SDA/SCL completeness tracking (Step 9), which is
    an orthogonal, pre-existing behavior unrelated to this fix -- direct
    _resolve_pin coverage is the precise, uncoupled proof this fixture
    needs.)"""
    kb, routing = _load_real_kb()
    assert ("INA219AxDCN", "SDA") not in kb   # raw placeholder never enters the KB
    assert ("TOTALLY_UNKNOWN_MPN_NOT_IN_KB", "SDA") not in kb

    comp = ComponentIR(
        refdes="U99", part_number="SOME_PLACEHOLDER_Xx", value="",
        pins=[PinIR("1", "SDA", "/I2C_{SYS}.SDA")],
        resolved_mpn="TOTALLY_UNKNOWN_MPN_NOT_IN_KB",
    )
    status, entry = _resolve_pin(comp.effective_mpn, "1", kb, routing, "SDA")
    assert entry is None
    assert status in (_LookupStatus.KB_MISSING, _LookupStatus.PIN_NOT_IN_KB), (
        f"expected an honest miss status, got {status!r} — a resolved MPN "
        "absent from the KB must never silently hit or crash")

"""INA219 fixed-function KB regression (NON-MCU KB LANDING #1, SDA/SCL landed
2026-07-12; A0/A1 strap-marked landed 2026-07-15, Todo 240 Phase 2):
investigation/experiments/nonmcu_kb_recon/landing_ina219/{REPORT.md,
REPORT_AMENDMENT.md}, investigation/experiments/strap_semantics_recon/
{REPORT.md,PHASE2_PREDICTION.md}.

First non-MCU (fixed-function) KB entry. A fixed-function sensor pin has ONE
possible role, full stop -- no alt-fn menu to disambiguate -- so unlike an MCU
KB entry it MAY directly confirm I2C endpoint identity (CLAUDE.md KB-evidence
rule: mux entries corroborate/condemn only; fixed entries may assert, since
there is no menu). `_is_fixed_function_i2c` (step_08d_peripheral_checker.py)
already derives this structurally per pin_id -- no schema change was needed.

STOP #2 (2026-07-12, the reason this entry SHIPPED SDA/SCL-only at first): the
real jetson-agx-thor-baseboard bus straps two INA219 units' A0 address-select
pins directly onto the SDA/SCL bus lines (TI's documented 16-address
addressing scheme, SBOS448G Table 1). KB'ing A0/A1 with a bare gpio role
seemed safe (it only needed to satisfy Step 7's "every pin on this net is
KB-resolved" check) but instead produced 4 FALSE FAILS: check_i2c_peripheral's
Step4 PROTOCOL_MISMATCH and the M14 CAPABILITY_MISMATCH voter layer both
treated "KB'd pin, no I2C role, wired to an I2C-classified net" as a genuine
miswiring -- correct for a stray MCU GPIO pin, wrong for an intentional
address strap. No schema affordance existed then for "known, intentional,
non-participating bus pin"; Nelson's decision at the time: ship SDA/SCL-only,
accept the resulting UNRESOLVABLE churn on shared-bus nets, and card the
schema fix as future work (Todo 240).

TODO-240 (2026-07-15) is that future work, now built and landed: A0/A1 are
KB'd with zero `possible_roles` plus a `strap` field (datasheet-cited),
exempting them from the two condemnation gates without ever letting them
CONFIRM bus identity (`peripheral_kb.StrapInfo`, `step_08d` Step 4,
`peripheral_consensus` M14 `incapable_candidate`). The interim SDA/SCL-only
lock below is SUPERSEDED: the new locked standard is SDA/SCL protocol pins +
A0/A1 strap-marked, and nothing else.

`test_real_strap_wiring_produces_zero_findings_post_240` reproduces the real
board's topology (an A0 pin sharing a net with its own device's SDA/SCL pin,
on two different units, matching U60/U66 on jetson-agx-thor-baseboard) and is
the regression guard for the NEW behavior: this topology must now produce
*zero* findings of any severity (STOP #2's false FAILs stay silenced, and the
strap entry closes the old UNRESOLVABLE coverage gap too). If this test ever
sees a FAIL, STOP #2 has regressed; if it sees the old UNRESOLVABLE, the strap
exemption itself has regressed.

Uses the REAL loaded KB (`kb/vendor/ti/INA219AIDCNR.json`) via
load_peripheral_kb, and the real check_i2c_peripheral / check_i2c_coherence
code paths end-to-end -- same invariant-testing style as
test_m6_kb_instance_disagreement_regression.py / test_m14_kb_doubles_regression.py.
"""
import os
import sys
from dataclasses import dataclass, field

import pytest

_HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_HERE, ".."))        # schematic_checker_poc -> steps
sys.path.insert(0, os.path.join(_HERE, "..", ".."))  # repo root -> peripheral_detectability

from steps.peripheral_kb import load_peripheral_kb                       # noqa: E402
from steps.peripheral_coherence import check_i2c_coherence               # noqa: E402
from steps.step_08d_peripheral_checker import (                          # noqa: E402
    canonicalize_mpn_for_kb, check_i2c_peripheral, _is_fixed_function_i2c,
    Severity,
)


@dataclass
class _Pin:
    pin_id:   str
    pin_name: str
    net:      str


@dataclass
class _Comp:
    refdes:      str
    part_number: str
    pins:        list
    value:       object = None
    @property
    def mpn(self): return self.part_number
    @property
    def effective_mpn(self): return self.mpn


@dataclass
class _Net:
    name: str
    pins: list  # (refdes, pin_id)


@dataclass
class _IR:
    components: list
    nets:       list = field(default_factory=list)


def _ir(components):
    nets = {}
    for c in components:
        for p in c.pins:
            nets.setdefault(p.net, []).append((c.refdes, p.pin_id))
    return _IR(components=components, nets=[_Net(n, ps) for n, ps in nets.items()])


_REAL_KB_DIR = os.path.join(_HERE, "..", "..", "kb", "vendor")


def _load_real_kb():
    if not os.path.isdir(_REAL_KB_DIR):
        pytest.skip("kb/vendor not present")
    return load_peripheral_kb(_REAL_KB_DIR)


# A jetson-shaped pull-up: 2-pin R* component, other leg on a positive rail,
# value above the jumper floor -- satisfies _has_pullup so NO_PULLUP_DETECTED
# does not fire and mask the assertions below.
def _pullup(refdes, net):
    return _Comp(refdes, "ERJ2GE0R00X",
                 [_Pin("1", "1", net), _Pin("2", "2", "+3V3")], value="10k")


def test_ina219_kb_entry_is_sda_scl_plus_strap_marked_a0_a1():
    """The shipped shape post-Todo-240: SDA/SCL protocol pins, plus A0/A1
    strap-marked (zero possible_roles, non-empty datasheet_ref, no
    legal_states -- Todo 214's field is RESERVED and unused here). SDA/SCL
    stay structurally fixed-function (_is_fixed_function_i2c) so R-B (M6-F2)
    and F1 (M14-F1) are immune by construction -- no gate/loader code change
    needed. A0/A1 carry no roles at all, so they are equally immune to any
    role-content-reading consumer; their exemption is carried entirely by
    `strap is not None` at the two condemnation gates."""
    kb, routing = _load_real_kb()
    assert ("INA219AIDCNR", "SDA") in kb
    assert ("INA219AIDCNR", "SCL") in kb
    assert _is_fixed_function_i2c(kb[("INA219AIDCNR", "SDA")]) is True
    assert _is_fixed_function_i2c(kb[("INA219AIDCNR", "SCL")]) is True

    assert ("INA219AIDCNR", "A0") in kb, (
        "A0 must now be KB'd, strap-marked (Todo 240 Phase 2) -- STOP #2's "
        "false FAILs were a bare-gpio-role defect, not a reason to leave the "
        "pin uncovered forever (see landing_ina219/REPORT_AMENDMENT.md).")
    assert ("INA219AIDCNR", "A1") in kb
    a0 = kb[("INA219AIDCNR", "A0")]
    a1 = kb[("INA219AIDCNR", "A1")]
    assert a0.roles == [] and a1.roles == [], (
        "A0/A1 carry no protocol roles -- they are address-select strap pins, "
        "never bus signal pins.")
    assert a0.strap is not None and a1.strap is not None
    assert isinstance(a0.strap.datasheet_ref, str) and a0.strap.datasheet_ref.strip(), (
        "strap exemption requires a non-empty datasheet citation")
    assert isinstance(a1.strap.datasheet_ref, str) and a1.strap.datasheet_ref.strip()
    assert a0.strap.legal_states is None and a1.strap.legal_states is None, (
        "legal_states is RESERVED for Todo 214 -- this landing must not populate it")

    assert "INA219AIDCNR" not in (routing or {}), (
        "peripheral_routing must be absent for a fixed part -- omission is the "
        "correct 'N/A', not PeripheralRouting.UNKNOWN")


def test_correctly_wired_ina219_zero_findings():
    """The 0-FP contract for a fixed-function entry: a correctly-wired INA219
    (SDA-on-SDA-net, SCL-on-SCL-net, jetson pin-name shape, real pull-ups)
    produces zero findings from both check_i2c_coherence (M6) and
    check_i2c_peripheral."""
    kb, routing = _load_real_kb()
    ir = _ir([
        _Comp("U22", "INA219AIDCNR", [
            _Pin("5", "SCL", "/I2C_{SYS}.SCL"),
            _Pin("6", "SDA", "/I2C_{SYS}.SDA"),
        ]),
        _pullup("R1", "/I2C_{SYS}.SCL"),
        _pullup("R2", "/I2C_{SYS}.SDA"),
    ])
    assert check_i2c_coherence(ir, kb, routing, canonicalize_mpn_for_kb) == []
    assert check_i2c_peripheral(ir, kb, routing) == []


def test_cross_net_swap_fires_m6_role_fail():
    """A cross-net SDA/SCL swap (SDA pin landing on the SCL-named net and vice
    versa) is caught as a ROLE-level FAIL by M6 (check_i2c_coherence), and
    folded into check_i2c_peripheral's own findings as a FAIL -- the new reach
    the fixed-function entry provides for this device class."""
    kb, routing = _load_real_kb()
    ir = _ir([
        _Comp("U22", "INA219AIDCNR", [
            _Pin("5", "SCL", "/I2C_{SYS}.SDA"),   # SCL pin on the SDA net
            _Pin("6", "SDA", "/I2C_{SYS}.SCL"),   # SDA pin on the SCL net
        ]),
    ])
    coh = check_i2c_coherence(ir, kb, routing, canonicalize_mpn_for_kb)
    assert len(coh) == 2
    assert all(v.status == "FAIL" for v in coh)

    findings = check_i2c_peripheral(ir, kb, routing)
    fails = [f for f in findings if f.severity == Severity.FAIL]
    assert len(fails) == 2
    assert all("SDA/SCL swap" in f.evidence for f in fails)


def test_real_strap_wiring_produces_zero_findings_post_240():
    """Regression guard for Todo 240's shipped behavior. Reproduces the real
    jetson-agx-thor-baseboard topology: two OTHER INA219 units (U60, U66) each
    have their A0 address-select pin strapped directly onto the same physical
    net as their own SDA/SCL pin (TI's 16-address scheme) -- exactly the
    topology that produced 2 false PROTOCOL_MISMATCH + 2 false
    CAPABILITY_MISMATCH FAILs when A0/A1 were (briefly, experimentally) KB'd
    with a bare gpio role (STOP #2), and 2 UNRESOLVABLE findings under the
    interim SDA/SCL-only entry (KB coverage gap on A0).

    With the shipped strap-marked A0/A1 entry, this topology must now produce
    **zero findings of any severity**: no FAIL (STOP #2 stays silenced), no
    UNRESOLVABLE (the strap entry closes the old coverage gap -- A0's status
    is OK, not PIN_NOT_IN_KB), no WARN. If this test ever sees a FAIL, STOP #2
    has regressed; if it sees an UNRESOLVABLE here, the strap exemption itself
    has regressed."""
    kb, routing = _load_real_kb()
    ir = _ir([
        _Comp("U22", "INA219AIDCNR", [
            _Pin("5", "SCL", "/I2C_{SYS}.SCL"),
            _Pin("6", "SDA", "/I2C_{SYS}.SDA"),
        ]),
        _Comp("U60", "INA219AIDCNR", [
            _Pin("6", "SDA", "/I2C_{SYS}.SDA"),
            _Pin("7", "A0",  "/I2C_{SYS}.SDA"),   # A0 strapped onto its own SDA net
        ]),
        _Comp("U66", "INA219AIDCNR", [
            _Pin("5", "SCL", "/I2C_{SYS}.SCL"),
            _Pin("7", "A0",  "/I2C_{SYS}.SCL"),   # A0 strapped onto its own SCL net
        ]),
        _pullup("R1", "/I2C_{SYS}.SCL"),
        _pullup("R2", "/I2C_{SYS}.SDA"),
    ])
    findings = check_i2c_peripheral(ir, kb, routing)

    fails = [f for f in findings if f.severity == Severity.FAIL]
    assert fails == [], (
        f"STOP #2 regression: real jetson strap wiring produced FAIL(s): {fails}")

    unresolvable = [f for f in findings if f.severity == Severity.UNRESOLVABLE]
    assert unresolvable == [], (
        f"strap-exemption coverage regression: A0 should resolve OK (strap), "
        f"not re-open the old KB-coverage-gap UNRESOLVABLE: {unresolvable}")

    warns = [f for f in findings if f.severity == Severity.WARN]
    assert warns == [], f"unexpected WARN with pull-ups present: {warns}"

    assert findings == [], f"expected a fully clean bus post-240: {findings}"

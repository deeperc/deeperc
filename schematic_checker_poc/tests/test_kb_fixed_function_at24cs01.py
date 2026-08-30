"""AT24CS01 fixed-function KB regression (NON-MCU KB LANDING #2, second
per-part cycle after INA219: investigation/experiments/nonmcu_kb_recon/
landing_at24cs01/REPORT.md).

Second non-MCU (fixed-function) KB entry, protocol-pins-only per the interim
standard adopted after INA219's STOP #2 (kb/vendor/ti/INA219AIDCNR.json
provenance.a0_a1_decision): address/config pins are never KB'd regardless of
board wiring, since no schema affordance exists for "known, intentional,
non-participating bus pin". On jetson-agx-thor-baseboard (U54), AT24CS01's
A0/A1/A2/WP pins all tie to GND, not to any bus net -- so unlike INA219 this
landing carries no STOP-2-shape churn risk from its own excluded pins.

U54 on the real board has a genuine, pre-existing SDA/SCL swap (pin 5,
logical name "SDA", sits on the net literally named '/I2C_{SYS}.SCL'; pin 6,
"SCL", sits on '/I2C_{SYS}.SDA') -- already caught by check_i2c_coherence via
the `pin_function` source (both pins carry literal SDA/SCL names on this
symbol), independent of this KB entry. This landing must not move that
finding at all -- see the "does not touch U54" fixture below.

`test_kb_path_convicts_swap_via_bare_pin_names` is this part's new-reach
proof: unlike INA219's fixture (which used literal SDA/SCL pin names and so
was caught by `pin_function`), this constructs the synthetic device with a
BARE/generic `pin_name` (not "SDA"/"SCL") so `role_from_pin_function` returns
None and the coherence check falls through to `kb_role_lookup` -- the KB
entry, not the pin's literal name, is what convicts the swap. `pin_id` is set
to the logical token ("SDA"/"SCL") per `_resolve_pin`'s documented
hand-crafted-fixture fallback (step_08d_peripheral_checker.py: "pin_id is a
fallback only for hand-crafted fixtures whose pin_id IS the logical token").

Uses the REAL loaded KB (`kb/vendor/microchip/AT24CS01-SSHM-B.json`) via
load_peripheral_kb, and the real check_i2c_peripheral / check_i2c_coherence
code paths end-to-end -- same invariant-testing style as
test_kb_fixed_function_ina219.py.
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


def _pullup(refdes, net):
    return _Comp(refdes, "ERJ2GE0R00X",
                 [_Pin("1", "1", net), _Pin("2", "2", "+3V3")], value="10k")


def test_at24cs01_kb_entry_is_sda_scl_only_and_fixed_function():
    """Protocol-pins-only shape: exactly SDA/SCL, no A0/A1/A2/WP, both
    structurally fixed-function -- R-B/F1 immune by construction, no gate/
    loader code change needed."""
    kb, routing = _load_real_kb()
    assert ("AT24CS01-SSHM-B", "SDA") in kb
    assert ("AT24CS01-SSHM-B", "SCL") in kb
    for excluded in ("A0", "A1", "A2", "WP"):
        assert ("AT24CS01-SSHM-B", excluded) not in kb, (
            f"{excluded} must not be KB'd -- protocol-pins-only interim standard "
            "(INA219 STOP #2)")
    assert _is_fixed_function_i2c(kb[("AT24CS01-SSHM-B", "SDA")]) is True
    assert _is_fixed_function_i2c(kb[("AT24CS01-SSHM-B", "SCL")]) is True
    assert "AT24CS01-SSHM-B" not in (routing or {})


def test_correctly_wired_at24cs01_zero_findings():
    """The 0-FP contract: a correctly-wired AT24CS01 (SDA-on-SDA-net,
    SCL-on-SCL-net, literal jetson pin-name shape, real pull-ups) produces
    zero findings from both check_i2c_coherence (M6) and check_i2c_peripheral."""
    kb, routing = _load_real_kb()
    ir = _ir([
        _Comp("U54", "AT24CS01-SSHM-B", [
            _Pin("5", "SDA", "/I2C_{SYS}.SDA"),
            _Pin("6", "SCL", "/I2C_{SYS}.SCL"),
        ]),
        _pullup("R1", "/I2C_{SYS}.SCL"),
        _pullup("R2", "/I2C_{SYS}.SDA"),
    ])
    assert check_i2c_coherence(ir, kb, routing, canonicalize_mpn_for_kb) == []
    assert check_i2c_peripheral(ir, kb, routing) == []


def test_kb_path_convicts_swap_via_bare_pin_names():
    """New-reach proof for this part: a cross-net SDA/SCL swap where the pin's
    OWN name is bare/generic (not literally 'SDA'/'SCL') is still caught --
    via kb_possible_roles, not pin_function. pin_id carries the logical token
    as the hand-crafted-fixture KB fallback key."""
    kb, routing = _load_real_kb()
    ir = _ir([
        _Comp("U1", "AT24CS01-SSHM-B", [
            _Pin("SDA", "5", "/I2C_{SYS}.SCL"),   # SDA pin (bare name "5") on the SCL net
            _Pin("SCL", "6", "/I2C_{SYS}.SDA"),   # SCL pin (bare name "6") on the SDA net
        ]),
    ])
    coh = check_i2c_coherence(ir, kb, routing, canonicalize_mpn_for_kb)
    assert len(coh) == 2
    assert all(v.status == "FAIL" for v in coh)
    assert all(v.source == "kb_possible_roles" for v in coh), (
        f"expected the KB path (not pin_function) to convict this swap: {coh}")

    findings = check_i2c_peripheral(ir, kb, routing)
    fails = [f for f in findings if f.severity == Severity.FAIL]
    assert len(fails) == 2
    assert all("SDA/SCL swap" in f.evidence for f in fails)


def test_u54_real_topology_does_not_touch_this_kb_entry():
    """U54's A0/A1/A2/WP all tie to GND on jetson-agx-thor-baseboard -- none
    land on the I2C_SYS bus, so (unlike INA219's U60/U66) this landing has no
    STOP-2-shape strap-on-bus topology to guard against. This test documents
    that absence and pins it down: the real U54 pin set (with its genuine
    swap) produces exactly the pre-existing 2 FAILs, no UNRESOLVABLE from
    excluded pins, since none of them share a net with SDA/SCL."""
    kb, routing = _load_real_kb()
    ir = _ir([
        _Comp("U54", "AT24CS01-SSHM-B", [
            _Pin("1", "A0", "GND"),
            _Pin("2", "A1", "GND"),
            _Pin("3", "A2", "GND"),
            _Pin("4", "GND", "GND"),
            _Pin("5", "SDA", "/I2C_{SYS}.SCL"),   # real board: genuine swap
            _Pin("6", "SCL", "/I2C_{SYS}.SDA"),
            _Pin("7", "WP", "GND"),
            _Pin("8", "VCC", "+3V3_AON"),
        ]),
    ])
    findings = check_i2c_peripheral(ir, kb, routing)
    fails = [f for f in findings if f.severity == Severity.FAIL]
    assert len(fails) == 2
    unresolvable = [f for f in findings if f.severity == Severity.UNRESOLVABLE]
    assert unresolvable == [], (
        f"A0/A1/A2/WP tie to GND, not the bus -- no UNRESOLVABLE expected here: {unresolvable}")

"""PCAL6408A fixed-function KB regression (NON-MCU KB LANDING #3, third
per-part cycle after INA219/AT24CS01: investigation/experiments/
nonmcu_kb_recon/landing_pcal6408a/REPORT.md).

Third non-MCU (fixed-function) KB entry, protocol-pins-only per the interim
standard adopted after INA219's STOP #2 (kb/vendor/ti/INA219AIDCNR.json
provenance.a0_a1_decision): ADDR, RESET_N, INT_N, the P0-P7 port pins, and
VDD(P)/VDD(I2C)/VSS are never KB'd regardless of board wiring. On
jetson-agx-thor-baseboard (U71), NONE of these excluded pins land on the
I2C_SYS bus (ADDR/RESET_N are isolated stub nets, INT_N/P0-P7 have their own
dedicated nets, VDD ties to +3V3) -- unlike INA219's A0 pins, this landing
carries no STOP-2-shape strap-on-bus churn risk, so no strap-on-bus regression
guard is needed (see the AT24CS01 landing's fourth test for what that guard
looks like when it IS needed).

Unlike U54 (AT24CS01), U71's SDA/SCL wiring on the real board is already
correct -- no pre-existing swap defect for this part.

`test_kb_path_convicts_swap_via_bare_pin_names` is this part's new-reach
proof, mirroring AT24CS01's: a synthetic swap built with a BARE/generic
`pin_name` (not "SDA"/"SCL") so `role_from_pin_function` returns None and the
coherence check falls through to `kb_role_lookup` -- the KB entry, not the
pin's literal name, convicts the swap.

Uses the REAL loaded KB (`kb/vendor/nxp/PCAL6408ABSHP.json`) via
load_peripheral_kb, and the real check_i2c_peripheral / check_i2c_coherence
code paths end-to-end -- same invariant-testing style as
test_kb_fixed_function_at24cs01.py.
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


def test_pcal6408a_kb_entry_is_sda_scl_only_and_fixed_function():
    """Protocol-pins-only shape: exactly SDA/SCL, no ADDR/RESET_N/INT_N/P0-P7/
    VDD/VSS, both structurally fixed-function -- R-B/F1 immune by
    construction, no gate/loader code change needed."""
    kb, routing = _load_real_kb()
    assert ("PCAL6408ABSHP", "SDA") in kb
    assert ("PCAL6408ABSHP", "SCL") in kb
    for excluded in ("ADDR", "RESET", "INT", "P0", "P1", "P2", "P3", "P4",
                     "P5", "P6", "P7", "VSS"):
        assert ("PCAL6408ABSHP", excluded) not in kb, (
            f"{excluded} must not be KB'd -- protocol-pins-only interim "
            "standard (INA219 STOP #2)")
    assert _is_fixed_function_i2c(kb[("PCAL6408ABSHP", "SDA")]) is True
    assert _is_fixed_function_i2c(kb[("PCAL6408ABSHP", "SCL")]) is True
    assert "PCAL6408ABSHP" not in (routing or {})


def test_correctly_wired_pcal6408a_zero_findings():
    """The 0-FP contract: a correctly-wired PCAL6408A (SDA-on-SDA-net,
    SCL-on-SCL-net, literal jetson pin-name shape, real pull-ups) produces
    zero findings from both check_i2c_coherence (M6) and check_i2c_peripheral."""
    kb, routing = _load_real_kb()
    ir = _ir([
        _Comp("U71", "PCAL6408ABSHP", [
            _Pin("12", "SCL", "/I2C_{SYS}.SCL"),
            _Pin("13", "SDA", "/I2C_{SYS}.SDA"),
        ]),
        _pullup("R1", "/I2C_{SYS}.SCL"),
        _pullup("R2", "/I2C_{SYS}.SDA"),
    ])
    assert check_i2c_coherence(ir, kb, routing, canonicalize_mpn_for_kb) == []
    assert check_i2c_peripheral(ir, kb, routing) == []


def test_kb_path_convicts_swap_via_bare_pin_names():
    """New-reach proof for this part: a cross-net SDA/SCL swap where the
    pin's OWN name is bare/generic (not literally 'SDA'/'SCL') is still
    caught -- via kb_possible_roles, not pin_function. pin_id carries the
    logical token as the hand-crafted-fixture KB fallback key."""
    kb, routing = _load_real_kb()
    ir = _ir([
        _Comp("U1", "PCAL6408ABSHP", [
            _Pin("SDA", "12", "/I2C_{SYS}.SCL"),   # SDA pin (bare name "12") on the SCL net
            _Pin("SCL", "13", "/I2C_{SYS}.SDA"),   # SCL pin (bare name "13") on the SDA net
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


def test_u71_real_topology_has_no_strap_on_bus_and_no_swap():
    """U71's ADDR/RESET_N are isolated stub nets, INT_N/P0-P7 have their own
    dedicated nets, and VDD(P)/VDD(I2C) tie to +3V3 -- none land on the
    I2C_SYS bus (unlike INA219's U60/U66 A0 pins), so this landing has no
    strap-on-bus topology to guard against. This test documents that absence:
    the real U71 pin set produces zero findings (also, unlike U54/AT24CS01,
    U71's SDA/SCL wiring on the real board has no pre-existing swap)."""
    kb, routing = _load_real_kb()
    ir = _ir([
        _Comp("U71", "PCAL6408ABSHP", [
            _Pin("1", "RESET", "Net-(U71-RESET)"),
            _Pin("2", "P0", "/GPIOEX_P0_0"),
            _Pin("6", "VSS", "GND"),
            _Pin("10", "P7", "/GPIOEX_P0_7"),
            _Pin("11", "INT", "/GPIO_EXP_IRQ"),
            _Pin("12", "SCL", "/I2C_{SYS}.SCL"),
            _Pin("13", "SDA", "/I2C_{SYS}.SDA"),
            _Pin("14", "VDD_P", "+3V3"),
            _Pin("15", "VDD_I2C", "+3V3"),
            _Pin("16", "ADDR", "Net-(U71-ADDR)"),
        ]),
        _pullup("R1", "/I2C_{SYS}.SCL"),
        _pullup("R2", "/I2C_{SYS}.SDA"),
    ])
    findings = check_i2c_peripheral(ir, kb, routing)
    assert findings == [], (
        f"U71's real topology should produce zero findings: {findings}")

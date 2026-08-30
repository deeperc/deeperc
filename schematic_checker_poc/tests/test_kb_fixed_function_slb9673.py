"""SLB9673 fixed-function KB regression (NON-MCU KB LANDING #5, capstone of
the initial batch: investigation/experiments/nonmcu_kb_recon/
landing_slb9673/REPORT.md).

Fifth non-MCU (fixed-function) KB entry, protocol-pins-only per the interim
standard adopted after INA219's STOP #2 (kb/vendor/ti/INA219AIDCNR.json
provenance.a0_a1_decision): TEST#, RST#, I2C_PIRQ#, VDD, GND, GPIO_00/01/02,
and the NC/NCI pads are never KB'd regardless of board wiring. On
jetson-agx-thor-baseboard (U14), none of these excluded pins land on the
I2C_SYS bus -- RST# ties to a transistor gate net, TEST# straps high to
+3V3, I2C_PIRQ# has its own dedicated net, GPIO_00/01/02 are unconnected --
no STOP-2-shape strap-on-bus churn risk, so no strap-on-bus regression guard
is needed here (mirrors PCAL6408A's and BQ25672's landings, not INA219's).

Corrects the original nonmcu_kb_recon's Q2 finding that this part had "no
SDA/SCL group extracted" -- re-verified during the BQ25672 landing's
batch-assessment section: the cache DOES have a usable I2C group (pin 29=SDA,
pin 30=SCL), confirmed against the real jetson symbol too.

CAPSTONE: with SLB9673 landed, every currently-KB-able resident of the
jetson I2C_SYS bus (5x INA219, AT24CS01, PCAL6408A, SLB9673) is covered. The
bus's residual Step-7 "MPN(s) not in KB" UNRESOLVABLE churn is now
permanently floored at exactly 2 residents -- a connector (ASP-218650-01)
and a test point (TP_0.75mm_SMD) -- neither a real IC, neither ever
KB-able. `test_i2c_sys_bus_residue_is_connector_and_test_point_only` proves
this floor directly against the real jetson boards.

`test_kb_path_convicts_swap_via_bare_pin_names` is this part's new-reach
proof, mirroring the prior three landings': a synthetic swap built with a
BARE/generic `pin_name` (not "SDA"/"SCL") so `role_from_pin_function` returns
None and the coherence check falls through to `kb_role_lookup`.

Uses the REAL loaded KB (`kb/vendor/infineon/SLB9673AU20FW2610XTMA1.json`)
via load_peripheral_kb, and the real check_i2c_peripheral /
check_i2c_coherence code paths end-to-end -- same invariant-testing style as
test_kb_fixed_function_bq25672.py.
"""
import ast
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


def test_slb9673_kb_entry_is_sda_scl_only_and_fixed_function():
    """Protocol-pins-only shape: exactly SDA/SCL, no TEST#/RST#/I2C_PIRQ#/
    VDD/GND/GPIO_00-02/NC/NCI, both structurally fixed-function -- R-B/F1
    immune by construction, no gate/loader code change needed."""
    kb, routing = _load_real_kb()
    assert ("SLB9673AU20FW2610XTMA1", "SDA") in kb
    assert ("SLB9673AU20FW2610XTMA1", "SCL") in kb
    for excluded in ("TEST", "RST", "I2C_PIRQ", "VDD", "GND",
                     "GPIO_00", "GPIO_01", "GPIO_02"):
        assert ("SLB9673AU20FW2610XTMA1", excluded) not in kb, (
            f"{excluded} must not be KB'd -- protocol-pins-only interim "
            "standard (INA219 STOP #2)")
    assert _is_fixed_function_i2c(kb[("SLB9673AU20FW2610XTMA1", "SDA")]) is True
    assert _is_fixed_function_i2c(kb[("SLB9673AU20FW2610XTMA1", "SCL")]) is True
    assert "SLB9673AU20FW2610XTMA1" not in (routing or {})


def test_correctly_wired_slb9673_zero_findings():
    """The 0-FP contract: a correctly-wired SLB9673 (SDA-on-SDA-net,
    SCL-on-SCL-net, literal jetson pin-name shape, real pull-ups) produces
    zero findings from both check_i2c_coherence (M6) and check_i2c_peripheral."""
    kb, routing = _load_real_kb()
    ir = _ir([
        _Comp("U14", "SLB9673AU20FW2610XTMA1", [
            _Pin("30", "SCL", "/I2C_{SYS}.SCL"),
            _Pin("29", "SDA", "/I2C_{SYS}.SDA"),
        ]),
        _pullup("R1", "/I2C_{SYS}.SCL"),
        _pullup("R2", "/I2C_{SYS}.SDA"),
    ])
    assert check_i2c_coherence(ir, kb, routing, canonicalize_mpn_for_kb) == []
    assert check_i2c_peripheral(ir, kb, routing) == []


def test_kb_path_convicts_swap_via_bare_pin_names():
    """New-reach proof for this part: a cross-net SDA/SCL swap where the
    pin's OWN name is bare/generic (not literally 'SDA'/'SCL') is still
    caught -- via kb_possible_roles, not pin_function."""
    kb, routing = _load_real_kb()
    ir = _ir([
        _Comp("U1", "SLB9673AU20FW2610XTMA1", [
            _Pin("SDA", "29", "/I2C_{SYS}.SCL"),   # SDA pin (bare name "29") on the SCL net
            _Pin("SCL", "30", "/I2C_{SYS}.SDA"),   # SCL pin (bare name "30") on the SDA net
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


def test_u14_real_topology_has_no_strap_on_bus():
    """U14's RST# ties to a transistor gate net, TEST# straps high to +3V3,
    I2C_PIRQ# has its own dedicated net, GPIO_00/01/02 are unconnected --
    none land on the I2C_SYS bus (unlike INA219's U60/U66 A0 pins), so this
    landing has no strap-on-bus topology to guard against. This test
    documents that absence: the real U14 pin set produces zero findings when
    correctly wired."""
    kb, routing = _load_real_kb()
    ir = _ir([
        _Comp("U14", "SLB9673AU20FW2610XTMA1", [
            _Pin("1", "VDD", "+3V3"),
            _Pin("2", "GND", "GND"),
            _Pin("17", "RST", "Net-(Q1-D)"),
            _Pin("18", "I2C_PIRQ", "/Peripherals/TPM_IRQ"),
            _Pin("20", "TEST", "+3V3"),
            _Pin("29", "SDA", "/I2C_{SYS}.SDA"),
            _Pin("30", "SCL", "/I2C_{SYS}.SCL"),
            _Pin("33", "EP", "GND"),
        ]),
        _pullup("R1", "/I2C_{SYS}.SCL"),
        _pullup("R2", "/I2C_{SYS}.SDA"),
    ])
    findings = check_i2c_peripheral(ir, kb, routing)
    assert findings == [], (
        f"U14's real topology should produce zero findings: {findings}")


def test_i2c_sys_bus_residue_is_fully_accounted_for():
    """CAPSTONE assertion: against the REAL jetson-agx-thor-baseboard netlist,
    the I2C_SYS bus's residual UNRESOLVABLE sibling list -- after landing
    every currently KB-able resident (INA219 x5, AT24CS01, PCAL6408A,
    SLB9673) -- is EXACTLY the expected floor per hierarchical view, no more
    and no less:

    - `peripherals.net` (U54/U71/U14's local sheet, no INA219 units present):
      exactly {TP_0.75mm_SMD} -- the test point, never KB-able.
    - top-level (merged) board: exactly {ASP-218650-01, TP_0.75mm_SMD,
      NTS0104BQ} -- the connector and test point (never KB-able) PLUS
      `NTS0104BQ` (a real watch-list part, never landed). `INA219AIDCNR` is
      GONE from this residue as of Todo 240 Phase 2 (2026-07-15): U60/U66's A0
      address-strap pins are now KB'd strap-marked (kb/vendor/ti/
      INA219AIDCNR.json) and resolve OK, so they no longer enter
      `missing_mpns` -- this was the pre-declared expected diff, see
      investigation/experiments/strap_semantics_recon/PHASE2_PREDICTION.md.
      AT24CS01-SSHM-B, PCAL6408ABSHP, and SLB9673AU20FW2610XTMA1 must NOT
      appear anywhere -- all three left cleanly, no residual gap of their own.

    This exact-set check (not a loose substring check) is what caught, while
    writing this test, that the top-level floor was NOT "connector + TP only"
    before Todo 240 landed -- it also included the (now closed) INA219
    A0-strap gap. Fully accounted for, not silently wrong."""
    kb, routing = _load_real_kb()
    from steps.step_02_parser import parse_netlist  # noqa: PLC0415

    _repo_root = os.path.join(_HERE, "..", "..")
    expected = {
        "netlist_corpus/exported/kicad_official/demos/demos/"
        "jetson-agx-thor-baseboard/peripherals.net": {"TP_0.75mm_SMD"},
        "netlist_corpus/exported/kicad_official/demos/demos/"
        "jetson-agx-thor-baseboard/jetson-agx-thor-baseboard.net": {
            "ASP-218650-01", "TP_0.75mm_SMD", "NTS0104BQ",
        },
    }
    for relpath, expected_residue in expected.items():
        path = os.path.join(_repo_root, relpath)
        if not os.path.exists(path):
            pytest.skip(f"{relpath} not present")
        nl = parse_netlist(path)
        findings = check_i2c_peripheral(nl, kb, routing)
        i2c_sys = [f for f in findings if f.net.endswith("I2C_{SYS}.SCL")
                   or f.net.endswith("I2C_{SYS}.SDA")]
        unresolvable = [f for f in i2c_sys if f.severity == Severity.UNRESOLVABLE]
        assert len(unresolvable) == 2, f"{relpath}: {unresolvable}"
        for f in unresolvable:
            residue = set(ast.literal_eval(f.evidence.split("MPN(s) not in KB: ", 1)[1].rstrip(".")))
            assert residue == expected_residue, (
                f"{relpath} {f.net}: residue {residue} != expected {expected_residue}")

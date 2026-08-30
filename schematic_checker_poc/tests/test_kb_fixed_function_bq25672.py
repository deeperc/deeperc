"""BQ25672 fixed-function KB regression (NON-MCU KB LANDING #4, fourth and
final per-part cycle of the initial batch: investigation/experiments/
nonmcu_kb_recon/landing_bq25672/REPORT.md).

Fourth non-MCU (fixed-function) KB entry, protocol-pins-only per the interim
standard adopted after INA219's STOP #2 (kb/vendor/ti/INA219AIDCNR.json
provenance.a0_a1_decision): STAT, INT, CE, QON, ILIM_HIZ, TS, PROG/REGN, and
every power-path pin are never KB'd regardless of board wiring. On the
openair-max board (U5), none of these excluded pins land on the SDA/SCL bus
(STAT ties to an LED, several are isolated stubs or unconnected, the rest are
power-path with their own dedicated nets) -- no STOP-2-shape strap-on-bus
churn risk, so no strap-on-bus regression guard is needed here (mirrors
PCAL6408A's landing, not INA219's).

First landing off jetson-agx-thor-baseboard: BQ25672 lives on `openair-max`
(One-Air-Max.net + Solar-Module.net). Its SDA/SCL net names are bare (`/SDA`,
`/SCL`, no `I2C` prefix) -- still matched by the SDA/SCL substring regex, same
mechanism as every prior landing.

`test_kb_path_convicts_swap_via_bare_pin_names` is this part's new-reach
proof, mirroring the prior three landings': a synthetic swap built with a
BARE/generic `pin_name` (not "SDA"/"SCL") so `role_from_pin_function` returns
None and the coherence check falls through to `kb_role_lookup`.

Uses the REAL loaded KB (`kb/vendor/ti/BQ25672RQMR.json`) via
load_peripheral_kb, and the real check_i2c_peripheral / check_i2c_coherence
code paths end-to-end -- same invariant-testing style as
test_kb_fixed_function_pcal6408a.py.
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
    return _Comp(refdes, "10K",
                 [_Pin("1", "1", net), _Pin("2", "2", "+3V3")], value="10k")


def test_bq25672_kb_entry_is_sda_scl_only_and_fixed_function():
    """Protocol-pins-only shape: exactly SDA/SCL, no STAT/INT/CE/QON/
    ILIM_HIZ/TS/PROG/REGN/power-path pins, both structurally fixed-function
    -- R-B/F1 immune by construction, no gate/loader code change needed."""
    kb, routing = _load_real_kb()
    assert ("BQ25672RQMR", "SDA") in kb
    assert ("BQ25672RQMR", "SCL") in kb
    for excluded in ("STAT", "INT", "CE", "QON", "ILIM_HIZ", "TS", "PROG",
                     "REGN", "VBUS", "SYS", "BAT", "GND"):
        assert ("BQ25672RQMR", excluded) not in kb, (
            f"{excluded} must not be KB'd -- protocol-pins-only interim "
            "standard (INA219 STOP #2)")
    assert _is_fixed_function_i2c(kb[("BQ25672RQMR", "SDA")]) is True
    assert _is_fixed_function_i2c(kb[("BQ25672RQMR", "SCL")]) is True
    assert "BQ25672RQMR" not in (routing or {})


def test_correctly_wired_bq25672_zero_findings():
    """The 0-FP contract: a correctly-wired BQ25672 (SDA-on-SDA-net,
    SCL-on-SCL-net, bare openair-max net-name shape, real pull-ups) produces
    zero findings from both check_i2c_coherence (M6) and check_i2c_peripheral."""
    kb, routing = _load_real_kb()
    ir = _ir([
        _Comp("U5", "BQ25672RQMR", [
            _Pin("14", "SCL", "/SCL"),
            _Pin("15", "SDA", "/SDA"),
        ]),
        _pullup("R1", "/SCL"),
        _pullup("R2", "/SDA"),
    ])
    assert check_i2c_coherence(ir, kb, routing, canonicalize_mpn_for_kb) == []
    assert check_i2c_peripheral(ir, kb, routing) == []


def test_kb_path_convicts_swap_via_bare_pin_names():
    """New-reach proof for this part: a cross-net SDA/SCL swap where the
    pin's OWN name is bare/generic (not literally 'SDA'/'SCL') is still
    caught -- via kb_possible_roles, not pin_function."""
    kb, routing = _load_real_kb()
    ir = _ir([
        _Comp("U1", "BQ25672RQMR", [
            _Pin("SDA", "14", "/SCL"),   # SDA pin (bare name "14") on the SCL net
            _Pin("SCL", "15", "/SDA"),   # SCL pin (bare name "15") on the SDA net
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


def test_u5_real_topology_has_no_strap_on_bus():
    """U5's STAT ties to an LED indicator net, several excluded pins are
    isolated stubs or unconnected, and the rest are power-path with their
    own dedicated nets -- none land on the SDA/SCL bus (unlike INA219's
    U60/U66 A0 pins), so this landing has no strap-on-bus topology to guard
    against. This test documents that absence: the real U5 pin set produces
    zero findings when correctly wired."""
    kb, routing = _load_real_kb()
    ir = _ir([
        _Comp("U5", "BQ25672RQMR", [
            _Pin("1", "STAT", "Net-(LED3-K)"),
            _Pin("12", "QON", "unconnected-(U5-QON-Pad12)"),
            _Pin("13", "CE", "Net-(U5-CE)"),
            _Pin("14", "SCL", "/SCL"),
            _Pin("15", "SDA", "/SDA"),
            _Pin("16", "TS", "Net-(U5-TS)"),
            _Pin("17", "ILIM_HIZ", "/ILIM_HIZ"),
            _Pin("21", "INT", "unconnected-(U5-INT-Pad21)"),
            _Pin("27", "GND", "PGND"),
        ]),
        _pullup("R1", "/SCL"),
        _pullup("R2", "/SDA"),
    ])
    findings = check_i2c_peripheral(ir, kb, routing)
    assert findings == [], (
        f"U5's real topology should produce zero findings: {findings}")

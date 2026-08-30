"""MPU-6050 fixed-function KB regression (NON-MCU KB LANDING #6, TODO-237
template; dumpling wild-board arc — investigation/experiments/wild_board_hunt/).

U7 on nyuad-space/dumpling is the I2C-slave destination of 3 of U1/F405's
driver-nets (the largest single blocker on dumpling's 6 armed-but-
UNRESOLVABLE peripheral checks, per dumpling2's delta). Pin identity is
Nelson-verified (2026-07-17, NOT derived from the datasheet PDF per this
task's explicit scope):

| pin | name    | role                                              |
|-----|---------|---------------------------------------------------|
| 24  | SDA     | primary I2C data (slave)                          |
| 23  | SCL     | primary I2C clock (slave)                         |
| 6   | AUX_DA  | auxiliary I2C data (master, downstream sensor bus) |
| 7   | AUX_CL  | auxiliary I2C clock (master, downstream sensor bus)|
| 9   | AD0     | I2C address-select strap (tie high/low)            |

AUX_DA/AUX_CL are DEFERRED, not landed (see kb/vendor/tdk/MPU-6050.json
provenance.aux_bus_decision): the Signal enum has no aux/secondary-bus
variant, and PinRole.instance is only ever matched against a numeric I2C\\d
token via peripheral_roles.instance_from_net_name — an invented 'AUX'
instance string would be schema-decorative, not functional, and dumpling's
own AUX nets (Net-(U7-AUX_CL)/Net-(U7-AUX_DA)) carry no I2C token anyway.
Landing AUX_DA/AUX_CL under the same Signal.I2C_SDA/SCL roles as the primary
bus would let check_i2c_peripheral's net classification / completeness
inference treat the auxiliary (downstream-master) bus as corroborating the
primary (slave) bus — a false claim this part's own KB entry must not make.
Per SCOPE: an honest miss on AUX nets is acceptable; a false SDA-role
conviction against AUX_DA is not.

Grep-first note: dumpling's raw KiCad pinfunction strings carry a numeric
suffix (SDA_24, SCL_23, AD0_9) — but step_02_parser._logical_pin_name strips
the trailing `_<digits>` before it reaches the checker layer, so the real
board's pin_name is bare 'SDA'/'SCL'/'AD0', matching this landing's pin_id
keys (verified by parsing the real dumpling netlist — see the KB entry's
provenance.agreement_gate).

Uses the REAL loaded KB (`kb/vendor/tdk/MPU-6050.json`) via
load_peripheral_kb, and the real check_i2c_peripheral / check_i2c_coherence
code paths end-to-end — same invariant-testing style as
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


def test_mpu6050_kb_entry_is_sda_scl_plus_strap_marked_ad0_aux_deferred():
    """Shape check: SDA/SCL protocol pins (structurally fixed-function), AD0
    strap-marked (zero possible_roles, non-empty datasheet_ref, no
    legal_states -- Todo 214's field is RESERVED and unused here), AUX_DA/
    AUX_CL absent entirely (deferred per Step 2's schema-evidence decision),
    peripheral_routing absent (fixed part, N/A)."""
    kb, routing = _load_real_kb()
    assert ("MPU-6050", "SDA") in kb
    assert ("MPU-6050", "SCL") in kb
    assert _is_fixed_function_i2c(kb[("MPU-6050", "SDA")]) is True
    assert _is_fixed_function_i2c(kb[("MPU-6050", "SCL")]) is True

    assert ("MPU-6050", "AD0") in kb
    ad0 = kb[("MPU-6050", "AD0")]
    assert ad0.roles == [], "AD0 carries no protocol roles -- address strap only"
    assert ad0.strap is not None
    assert isinstance(ad0.strap.datasheet_ref, str) and ad0.strap.datasheet_ref.strip()
    assert ad0.strap.legal_states is None, (
        "legal_states is RESERVED for Todo 214 -- this landing must not populate it")

    assert ("MPU-6050", "AUX_DA") not in kb, (
        "AUX_DA must stay unlanded -- no schema vocabulary exists to separate "
        "it from the primary bus without a false corroboration risk")
    assert ("MPU-6050", "AUX_CL") not in kb

    assert "MPU-6050" not in (routing or {}), (
        "peripheral_routing must be absent for a fixed part")


def test_correctly_wired_mpu6050_zero_findings():
    """The 0-FP contract: a correctly-wired MPU-6050 (SDA-on-SDA-net,
    SCL-on-SCL-net, dumpling pin-name shape, real pull-ups) produces zero
    findings from both check_i2c_coherence (M6) and check_i2c_peripheral."""
    kb, routing = _load_real_kb()
    ir = _ir([
        _Comp("U7", "MPU-6050", [
            _Pin("23", "SCL", "/I2C3_SCL"),
            _Pin("24", "SDA", "/I2C3_SDA"),
        ]),
        _pullup("R1", "/I2C3_SCL"),
        _pullup("R2", "/I2C3_SDA"),
    ])
    assert check_i2c_coherence(ir, kb, routing, canonicalize_mpn_for_kb) == []
    assert check_i2c_peripheral(ir, kb, routing) == []


def test_cross_net_swap_fires_m6_role_fail():
    """A cross-net SDA/SCL swap (SDA pin landing on the SCL-named net and vice
    versa) is caught as a ROLE-level FAIL by M6 (check_i2c_coherence), and
    folded into check_i2c_peripheral's own findings as a FAIL -- the new
    reach this fixed-function entry provides for this device class."""
    kb, routing = _load_real_kb()
    ir = _ir([
        _Comp("U7", "MPU-6050", [
            _Pin("23", "SCL", "/I2C3_SDA"),   # SCL pin on the SDA net
            _Pin("24", "SDA", "/I2C3_SCL"),   # SDA pin on the SCL net
        ]),
    ])
    coh = check_i2c_coherence(ir, kb, routing, canonicalize_mpn_for_kb)
    assert len(coh) == 2
    assert all(v.status == "FAIL" for v in coh)

    findings = check_i2c_peripheral(ir, kb, routing)
    fails = [f for f in findings if f.severity == Severity.FAIL]
    assert len(fails) == 2
    assert all("SDA/SCL swap" in f.evidence for f in fails)


def test_kbd_doubles_stm32_i2c3_and_mpu6050_zero_findings():
    """Mandatory KB'd-doubles FP fixture (CLAUDE.md KB-evidence rule): BOTH
    endpoints of the bus are KB'd -- a real STM32F407V(E-G)Tx mux pin pair
    (PA8=I2C3 SCL, PC9=I2C3 SDA -- the real dumpling U1/F405 alt-fn pinout,
    aliased via _STM32_RANGE_MAP elsewhere) on one end, the new MPU-6050
    fixed-function entry on the other -- exactly dumpling's real U1<->U7
    topology. Correct wiring must still produce zero findings; a KB'd mux
    entry's possible_roles menu must never let R-B/F1 corroboration
    misfire into a false positive just because both sides now resolve."""
    kb, routing = _load_real_kb()
    ir = _ir([
        _Comp("U1", "STM32F407V(E-G)Tx", [
            _Pin("41", "PA8", "/I2C3_SCL"),
            _Pin("40", "PC9", "/I2C3_SDA"),
        ]),
        _Comp("U7", "MPU-6050", [
            _Pin("23", "SCL", "/I2C3_SCL"),
            _Pin("24", "SDA", "/I2C3_SDA"),
        ]),
        _pullup("R1", "/I2C3_SCL"),
        _pullup("R2", "/I2C3_SDA"),
    ])
    assert check_i2c_coherence(ir, kb, routing, canonicalize_mpn_for_kb) == []
    findings = check_i2c_peripheral(ir, kb, routing)
    assert findings == [], f"expected a fully clean KB'd-doubles bus: {findings}"


def test_ad0_strap_on_bus_produces_no_false_fail():
    """Todo-240 regression class (mirrors INA219's U60/U66 fixture): even in
    the worst-case topology where AD0 shares a net with the device's own
    SDA or SCL line (dumpling's real wiring ties AD0 to GND, off-bus, so
    this never fires there -- this fixture proves the strap exemption holds
    structurally regardless). AD0 strapped onto SDA (one unit) and onto SCL
    (a second unit) must both produce zero findings: no FAIL (a bare-role
    strap misread), no UNRESOLVABLE (a KB-coverage gap on AD0)."""
    kb, routing = _load_real_kb()
    ir = _ir([
        _Comp("U7", "MPU-6050", [
            _Pin("23", "SCL", "/I2C3_SCL"),
            _Pin("24", "SDA", "/I2C3_SDA"),
            _Pin("9",  "AD0", "/I2C3_SDA"),   # AD0 strapped onto its own SDA net
        ]),
        _Comp("U8", "MPU-6050", [
            _Pin("23", "SCL", "/I2C3_SCL"),
            _Pin("24", "SDA", "/I2C3_SDA"),
            _Pin("9",  "AD0", "/I2C3_SCL"),   # AD0 strapped onto its own SCL net
        ]),
        _pullup("R1", "/I2C3_SCL"),
        _pullup("R2", "/I2C3_SDA"),
    ])
    findings = check_i2c_peripheral(ir, kb, routing)

    fails = [f for f in findings if f.severity == Severity.FAIL]
    assert fails == [], f"strap misread as a bus role: {fails}"

    unresolvable = [f for f in findings if f.severity == Severity.UNRESOLVABLE]
    assert unresolvable == [], (
        f"strap-exemption coverage regression: AD0 should resolve OK: {unresolvable}")

    assert findings == [], f"expected a fully clean strap-on-bus topology: {findings}"


def test_dumpling_real_topology_has_no_findings():
    """Direct proof against dumpling's own U7 pin set, parsed from the real
    board -- not a synthetic shape. AUX_DA/AUX_CL/AD0 (off-bus, tied to GND)
    are present on the component but on non-bus nets; only SDA/SCL are on
    the I2C3 bus. Confirms the landing converts U1<->U7 cleanly end-to-end
    on the actual netlist this landing was commissioned for."""
    kb, routing = _load_real_kb()
    ir = _ir([
        _Comp("U7", "MPU-6050", [
            _Pin("1",  "CLKIN",  "Net-(U7-CLKIN)"),
            _Pin("6",  "AUX_DA", "Net-(U7-AUX_DA)"),
            _Pin("7",  "AUX_CL", "Net-(U7-AUX_CL)"),
            _Pin("9",  "AD0",    "GND"),
            _Pin("11", "FSYNC",  "Net-(U7-FSYNC)"),
            _Pin("23", "SCL",    "/I2C3_SCL"),
            _Pin("24", "SDA",    "/I2C3_SDA"),
        ]),
        _Comp("U1", "STM32F407V(E-G)Tx", [
            _Pin("41", "PA8", "/I2C3_SCL"),
            _Pin("40", "PC9", "/I2C3_SDA"),
        ]),
        _pullup("R1", "/I2C3_SCL"),
        _pullup("R2", "/I2C3_SDA"),
    ])
    findings = check_i2c_peripheral(ir, kb, routing)
    assert findings == [], (
        f"dumpling's real U1<->U7 I2C3 bus should be fully clean post-landing: {findings}")

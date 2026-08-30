"""M6-F2 R-B (instance-agreement) regression (promoted from the M6-F2 recon's FP
probe: investigation/experiments/m6_f2_recon/fp_probe.py variant (d), via
investigation/experiments/m6_f2_recon/build/REPORT.md).

A fixed-mux MCU pin's KB possible_roles is unambiguous but still just a MENU of
what the silicon CAN do -- it never independently confirms what a specific net
IS. Before M6-F2, an unambiguous KB role landing on any net that names the
OTHER I2C signal fired a coherence FAIL regardless of which BUS INSTANCE the
net claims -- a coincidental-token debug net misuse (KB says pin is I2C1 SCL;
net happens to be named '/I2C2_SDA' for something unrelated) is indistinguishable
from a genuine cross-instance swap using pin-role/net-role alone. THE FIX (R-B,
peripheral_coherence.find_coherence_violations): a KB-sourced conviction is
admissible only when the net's EXPLICIT instance token agrees with the KB
role's instance, or when either side asserts no instance at all (bare-token
nets, KB entries with instance=None). Disagreement -> UNRESOLVABLE, never FAIL.

Uses the REAL code path end-to-end: check_i2c_coherence (kb_role_lookup +
matrix_lookup + kb_instance_lookup all wired) -- same invariant-testing style
as test_m14_kb_doubles_regression.py / test_peripheral_coherence.py.
"""
import os
import sys
from dataclasses import dataclass, field

_HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_HERE, ".."))        # schematic_checker_poc → steps
sys.path.insert(0, os.path.join(_HERE, "..", ".."))  # repo root → peripheral_detectability

from steps import peripheral_coherence as pc            # noqa: E402
from steps.peripheral_kb import Signal, Peripheral, PeripheralRouting, PinRole, PinFunctionEntry, KBSource  # noqa: E402
from steps import step_08d_peripheral_checker as s08d   # noqa: E402


@dataclass
class _Pin:
    pin_id: str
    pin_name: str
    net: str


@dataclass
class _Comp:
    refdes: str
    part_number: str
    pins: list
    @property
    def mpn(self): return self.part_number


@dataclass
class _Net:
    name: str
    pins: list  # (refdes, pin_id)


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


_ROUTING = {}  # none of the 3 fixtures are matrix-routed; instance gate must fire
                # independently of the (already-covered) matrix downgrade

_CANON = s08d.canonicalize_mpn_for_kb


# ── (i) instance DISAGREEMENT: KB I2C1 SCL on an /I2C2_SDA net -> stand down ──
# Real shape from the audit's FP probe: PB6 (F303 KB, fixed-mux) is unambiguously
# I2C1 SCL. The net asserts a DIFFERENT bus's SDA token -- the classic
# coincidental-token debug-net misuse the recon's Step 2 identified as firing
# and shouldn't.
_KB_DISAGREE = {(_CANON("STM32F303RCT6"), "PB6"): PinFunctionEntry(
    "STM32F303RCT6", "PB6",
    [PinRole(Peripheral.I2C, "I2C1", Signal.I2C_SCL, KBSource.VENDOR_XML)])}


def test_instance_disagreement_stands_down_to_unresolvable():
    ir = _ir([_Comp("U1", "STM32F303RCT6", [_Pin("58", "PB6", "/I2C2_SDA")])])
    vios = pc.check_i2c_coherence(ir, _KB_DISAGREE, _ROUTING, _CANON)
    assert len(vios) == 1
    v = vios[0]
    assert v.source == "kb_possible_roles"
    assert v.status == "UNRESOLVABLE"
    assert not any(x.status == "FAIL" for x in vios)


# ── (ii) instance AGREEMENT (real rider shape): still FAILs ───────────────────
# My_STM32_Board / stm32_board shape (2 of the 5 shipped M6 kb_possible_roles
# HITs): PB10 (F103 KB) is unambiguously I2C2 SCL; post-swap net is /I2C2_SDA --
# SAME instance (I2C2), genuine SDA/SCL contradiction. Must keep firing.
_KB_AGREE = {(_CANON("STM32F103C8T6"), "PB10"): PinFunctionEntry(
    "STM32F103C8T6", "PB10",
    [PinRole(Peripheral.I2C, "I2C2", Signal.I2C_SCL, KBSource.VENDOR_XML)])}


def test_instance_agreement_still_fails():
    ir = _ir([_Comp("U2", "STM32F103C8T6", [_Pin("21", "PB10", "/I2C2_SDA")])])
    vios = pc.check_i2c_coherence(ir, _KB_AGREE, _ROUTING, _CANON)
    assert len(vios) == 1
    v = vios[0]
    assert v.source == "kb_possible_roles"
    assert v.status == "FAIL"


# ── (iii) bare-token net (no instance assertion at all): still FAILs ──────────
# stamp_and_module / RP2040 shape (the 5th shipped kb_possible_roles HIT, the one
# with NO instance token in its net name at all): GPIO0 (RP2040 KB) is
# unambiguously I2C0 SDA; post-swap net is the bare '/SCL' -- no instance to
# disagree with, so R-B must leave it admissible.
_KB_BARE = {(_CANON("RP2040"), "GPIO0"): PinFunctionEntry(
    "RP2040", "GPIO0",
    [PinRole(Peripheral.I2C, "I2C0", Signal.I2C_SDA, KBSource.VENDOR_HEADER)])}


def test_bare_token_net_still_fails():
    ir = _ir([_Comp("U1", "RP2040", [_Pin("1", "GPIO0", "/SCL")])])
    vios = pc.check_i2c_coherence(ir, _KB_BARE, _ROUTING, _CANON)
    assert len(vios) == 1
    v = vios[0]
    assert v.source == "kb_possible_roles"
    assert v.status == "FAIL"

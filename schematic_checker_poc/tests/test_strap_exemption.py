"""Todo 240 Phase 1 — strap-pin exemption schema + the two condemnation gates.

The capability family CONDEMNS any KB'd pin that carries zero protocol roles on a
bus it is wired to ("bus dead — pin cannot serve SCL"). The INA219 addressing
idiom (A0/A1 deliberately strapped to SDA/SCL — high-impedance inputs, 16
addresses from 2 pins) is a documented LEGITIMATE design counterexample: the
STOPped KB-amendment attempt fired 4 false FAILs on the correctly-wired jetson
board (investigation/experiments/nonmcu_kb_recon/landing_ina219/REPORT_AMENDMENT.md;
2 PROTOCOL_MISMATCH + 2 CAPABILITY_MISMATCH, U60/U66).

This file proves the additive strap schema (`PinFunctionEntry.strap: StrapInfo`)
and its consumption at exactly two gates:
  (a) step_08d Step 4  — a strap-marked pin skips the PROTOCOL_MISMATCH
      zero-roles condemnation, and is counted RESOLVED for Step-7 accounting;
  (b) peripheral_consensus M14 — a strap-marked pin is never an
      incapable_candidate, so it can never drive a CAPABILITY_MISMATCH FAIL.

Plus the card's blast-radius invariants:
  (i)  never-CONFIRM — a strap pin contributes NOTHING to any CONFIRM path
       (structurally never a voter, asserted here even when a strap pin is
       contradictorily authored WITH a bus role);
  (ii) no-hole — a real miswire on a NON-strap pin of the same part still fires.

Phase 1 is CODE ONLY: NO production KB entry carries `strap`, so every gate below
is exercised against SYNTHETIC test-KB entries (built in-code or under tmp_path),
never the corpus and never kb/vendor/. The production INA219 A0/A1 entry + the
corpus re-measure are Phase 2.
"""
import os
import sys
from dataclasses import dataclass, field

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))          # -> steps
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # -> repo root

from steps.peripheral_kb import (                                        # noqa: E402
    Signal, KBSource, Peripheral, PinRole, PinFunctionEntry, StrapInfo,
    load_kb, _entry_from_json, _strap_from_dict,
)
from steps.peripheral_coherence import I2C_PAIR, _KB_SIGNAL_TO_ROLE      # noqa: E402
from steps.peripheral_bus_pairing import PairedBus                       # noqa: E402
from steps.peripheral_consensus import (                                 # noqa: E402
    evaluate_bus_consensus, MemberClass, BusVerdict,
)
from steps.step_08d_peripheral_checker import (                          # noqa: E402
    check_i2c_peripheral, PeripheralViolation, Severity,
)

_DS = "INA219 datasheet SBOS448G §7.3.3 (A0/A1 address-select pins)"


# ── Lightweight netlist shape (mirrors test_capability_misroute_checker.py) ──

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
    value:  object = None
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


def _sensor(mpn, pin_id, signal, instance=None):
    """A fixed-function I2C sensor pin (SDA or SCL) — a real voter."""
    return PinFunctionEntry(mpn, pin_id,
                            [PinRole(Peripheral.I2C, instance, signal, KBSource.VENDOR_XML)])


def _strap_pin(mpn, pin_id, *, roles=None, datasheet_ref=_DS):
    """A strap-marked KB pin. Default role content is [GPIO] (the INA219 A0/A1
    shape); `roles` overrides it (used by the structural invariant to author a
    contradictory strap-pin-WITH-a-bus-role and prove it still never votes)."""
    if roles is None:
        roles = [PinRole(Peripheral.GPIO, None, Signal.GPIO, KBSource.MANUAL)]
    return PinFunctionEntry(mpn, pin_id, roles, strap=StrapInfo(datasheet_ref=datasheet_ref))


def _bare_gpio_pin(mpn, pin_id):
    """The STOP-2 shape: A0 KB'd as a bare GPIO role, NO strap → condemned."""
    return PinFunctionEntry(mpn, pin_id,
                            [PinRole(Peripheral.GPIO, None, Signal.GPIO, KBSource.MANUAL)])


def _pullup(refdes, net):
    """2-pin R* to a positive rail, above the jumper floor — satisfies
    _has_pullup so NO_PULLUP_DETECTED does not fire and mask assertions."""
    return Component(refdes, "ERJ2GE0R00X",
                     [PinRef("1", net, "1"), PinRef("2", "+3V3", "2")], value="10k")


def _bus(net_a="SCL_NET", net_b="SDA_NET", bus_id="I2C1", source="kb_instance"):
    return PairedBus(protocol="I2C", bus_id=bus_id, pairing_source=source,
                     net_a=net_a, net_b=net_b)


def _fails(findings):
    return [f for f in findings if f.severity == Severity.FAIL]


def _cap_fails(findings):
    return [f for f in findings
            if f.violation == PeripheralViolation.CAPABILITY_MISMATCH
            and f.severity == Severity.FAIL]


def _unresolvable(findings):
    return [f for f in findings if f.severity == Severity.UNRESOLVABLE]


# ══════════════════════════════════════════════════════════════════════════════
# 1. Schema + loader
# ══════════════════════════════════════════════════════════════════════════════

def _write_kb(dir_path, fname, mcu_part, pins):
    import json
    path = os.path.join(dir_path, fname)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"mcu_part": mcu_part, "kb_source": "manual", "pins": pins}, fh)
    return path


def test_legacy_entry_without_strap_roundtrips_unchanged(tmp_path):
    """A KB entry with NO `strap` key loads exactly as before: strap is None,
    roles unchanged. The additive field is invisible to every existing entry."""
    _write_kb(str(tmp_path), "legacy.json", "LEGACY_MCU", [
        {"pin_id": "PB6", "possible_roles": [
            {"peripheral": "i2c", "signal": "scl", "instance": "I2C1"}]},
        {"pin_id": "PB7", "possible_roles": [
            {"peripheral": "i2c", "signal": "sda", "instance": "I2C1"}]},
    ])
    kb = load_kb(str(tmp_path))
    e = kb[("LEGACY_MCU", "PB6")]
    assert e.strap is None
    assert len(e.roles) == 1
    assert e.roles[0].peripheral == Peripheral.I2C
    assert e.roles[0].signal == Signal.I2C_SCL
    # Direct _entry_from_json path (no filesystem) — same round-trip.
    e2 = _entry_from_json("LEGACY_MCU",
                          {"pin_id": "X", "possible_roles": []}, KBSource.MANUAL)
    assert e2.strap is None


def test_strap_present_parses(tmp_path):
    _write_kb(str(tmp_path), "strapped.json", "INA219", [
        {"pin_id": "SDA", "possible_roles": [{"peripheral": "i2c", "signal": "sda"}]},
        {"pin_id": "A0", "possible_roles": [{"peripheral": "gpio", "signal": "gpio"}],
         "strap": {"datasheet_ref": _DS}},
    ])
    kb = load_kb(str(tmp_path))
    a0 = kb[("INA219", "A0")]
    assert isinstance(a0.strap, StrapInfo)
    assert a0.strap.datasheet_ref == _DS
    assert a0.strap.legal_states is None          # RESERVED, default None
    assert kb[("INA219", "SDA")].strap is None     # sibling untouched


def test_strap_missing_datasheet_ref_raises():
    with pytest.raises(ValueError, match="datasheet_ref"):
        _strap_from_dict({})


def test_strap_empty_datasheet_ref_raises():
    with pytest.raises(ValueError, match="datasheet_ref"):
        _strap_from_dict({"datasheet_ref": "   "})


def test_strap_non_string_datasheet_ref_raises():
    with pytest.raises(ValueError, match="datasheet_ref"):
        _strap_from_dict({"datasheet_ref": 12345})


def test_strap_legal_states_carried_verbatim_not_validated():
    """RESERVED (Todo 214): legal_states is carried through VERBATIM. No code path
    reads, validates, or branches on its contents — an arbitrary payload loads
    without error, proving the loader does not interpret it."""
    arbitrary = ["anything", 123, {"undefined": "vocabulary"}]
    si = _strap_from_dict({"datasheet_ref": _DS, "legal_states": arbitrary})
    assert si.legal_states == arbitrary
    assert si.datasheet_ref == _DS
    # absent legal_states → None
    assert _strap_from_dict({"datasheet_ref": _DS}).legal_states is None


def test_absent_strap_key_is_none():
    assert _strap_from_dict(None) is None


# ══════════════════════════════════════════════════════════════════════════════
# 2. Gate (a) — step_08d Step 4: strap pin skips PROTOCOL_MISMATCH, counts RESOLVED
# ══════════════════════════════════════════════════════════════════════════════

def _jetson_strap_netlist(a0_entry_provider):
    """U1 INA219: SDA + SCL fixed-function pins on their own nets, A0 strapped
    directly onto the SDA net (the real U60 shape). `a0_entry_provider` supplies
    the A0 KB entry so the SAME netlist can be run strap-marked vs bare-GPIO."""
    kb = _make_kb(
        _sensor("INA219", "SDA", Signal.I2C_SDA),
        _sensor("INA219", "SCL", Signal.I2C_SCL),
        a0_entry_provider("INA219", "A0"),
    )
    nl = Netlist(
        components=[
            Component("U1", "INA219", [
                PinRef("SDA", "/I2C.SDA", "SDA"),
                PinRef("SCL", "/I2C.SCL", "SCL"),
                PinRef("A0",  "/I2C.SDA", "A0"),   # A0 strapped onto its own SDA net
            ]),
            _pullup("R1", "/I2C.SDA"),
            _pullup("R2", "/I2C.SCL"),
        ],
        nets=[Net("/I2C.SDA", [("U1", "SDA"), ("U1", "A0"), ("R1", "1")]),
              Net("/I2C.SCL", [("U1", "SCL"), ("R2", "1")]),
              Net("+3V3", [("R1", "2"), ("R2", "2")])],
    )
    return kb, nl


def test_gate_a_strap_pin_no_condemnation_and_resolved():
    """A strap-marked A0 on the SDA net: NO PROTOCOL_MISMATCH, and because A0
    resolves OK it is counted RESOLVED — it never enters Step-7's missing_mpns,
    so there is no UNRESOLVABLE churn either. Zero findings."""
    kb, nl = _jetson_strap_netlist(lambda m, p: _strap_pin(m, p))
    findings = check_i2c_peripheral(nl, kb, {})
    assert _fails(findings) == []
    assert _unresolvable(findings) == [], (
        "a KB-resolved strap pin must count RESOLVED, not produce UNRESOLVABLE churn")
    assert findings == []   # pull-ups present → no WARN either


def test_gate_a_identical_pin_without_strap_is_condemned():
    """The SAME netlist with A0 KB'd as a bare GPIO role (NO strap) reproduces
    STOP-2: the zero-I2C-roles pin on an I2C net is condemned PROTOCOL_MISMATCH,
    exactly as today. This proves the exemption — not some other change — is
    what silences the strap case."""
    kb, nl = _jetson_strap_netlist(lambda m, p: _bare_gpio_pin(m, p))
    findings = check_i2c_peripheral(nl, kb, {})
    protocol = [f for f in findings
                if f.violation == PeripheralViolation.PROTOCOL_MISMATCH
                and f.severity == Severity.FAIL]
    assert len(protocol) == 1
    assert protocol[0].pins == ["U1.A0"]


# ══════════════════════════════════════════════════════════════════════════════
# 3. Gate (b) — peripheral_consensus M14: strap pin never an incapable_candidate
# ══════════════════════════════════════════════════════════════════════════════

def _consensus_over_strap(a0_entry_provider):
    """One INA219 (SDA + SCL fixed-function voters) with an A0 pin on the SDA net,
    evaluated directly through evaluate_bus_consensus over the SCL/SDA bus."""
    kb = _make_kb(
        _sensor("INA219", "SDA", Signal.I2C_SDA),
        _sensor("INA219", "SCL", Signal.I2C_SCL),
        a0_entry_provider("INA219", "A0"),
    )
    nl = Netlist(
        components=[Component("U1", "INA219", [
            PinRef("SDA", "SDA_NET", "SDA"),
            PinRef("SCL", "SCL_NET", "SCL"),
            PinRef("A0",  "SDA_NET", "A0"),
        ])],
        nets=[Net("SDA_NET", [("U1", "SDA"), ("U1", "A0")]),
              Net("SCL_NET", [("U1", "SCL")])],
    )
    return evaluate_bus_consensus(nl, _bus(), I2C_PAIR, kb=kb, peripheral_routing={},
                                  kb_signal_to_role=_KB_SIGNAL_TO_ROLE)


def test_gate_b_strap_pin_is_silent_not_incapable():
    cons = _consensus_over_strap(lambda m, p: _strap_pin(m, p))
    a0 = next(m for m in cons.members if m.pin_id == "A0")
    assert a0.classification == MemberClass.SILENT.value
    assert a0.incapable_candidate is False
    assert cons.fail_mode != "capability"
    assert cons.verdict != BusVerdict.WOULD_FAIL.value


def test_gate_b_identical_pin_without_strap_is_incapable():
    """Mirror: the same A0 pin WITHOUT strap is INCAPABLE and drives the
    capability WOULD_FAIL, exactly as today (STOP-2's CAPABILITY_MISMATCH)."""
    cons = _consensus_over_strap(lambda m, p: _bare_gpio_pin(m, p))
    a0 = next(m for m in cons.members if m.pin_id == "A0")
    assert a0.classification == MemberClass.INCAPABLE.value
    assert cons.fail_mode == "capability"
    assert cons.verdict == BusVerdict.WOULD_FAIL.value


# ══════════════════════════════════════════════════════════════════════════════
# 4. Invariants (blast-radius guards)
# ══════════════════════════════════════════════════════════════════════════════

def test_invariant_never_confirm_structural():
    """(i) A strap pin contributes NOTHING to any CONFIRM path — STRUCTURALLY.
    Even when a strap pin is CONTRADICTORILY authored WITH a real I2C SDA role
    (a KB-authoring mistake), the hoisted strap gate classifies it SILENT before
    its role content is inspected, so it carries no asserted_role and NEVER enters
    the voter set that consensus/CONFIRM draws from."""
    contradictory = _strap_pin("INA219", "A0",
                               roles=[PinRole(Peripheral.I2C, "I2C1", Signal.I2C_SDA,
                                              KBSource.MANUAL)])
    kb = _make_kb(
        _sensor("INA219", "SDA", Signal.I2C_SDA),
        _sensor("INA219", "SCL", Signal.I2C_SCL),
        contradictory,
    )
    nl = Netlist(
        components=[Component("U1", "INA219", [
            PinRef("SDA", "SDA_NET", "SDA"),
            PinRef("SCL", "SCL_NET", "SCL"),
            PinRef("A0",  "SDA_NET", "A0"),
        ])],
        nets=[Net("SDA_NET", [("U1", "SDA"), ("U1", "A0")]),
              Net("SCL_NET", [("U1", "SCL")])],
    )
    cons = evaluate_bus_consensus(nl, _bus(), I2C_PAIR, kb=kb, peripheral_routing={},
                                  kb_signal_to_role=_KB_SIGNAL_TO_ROLE)
    a0 = next(m for m in cons.members if m.pin_id == "A0")
    assert a0.classification == MemberClass.SILENT.value
    assert a0.asserted_role is None                    # never asserts a role
    voters = [m for m in cons.members if m.asserted_role is not None]
    assert a0 not in voters                             # never enters the CONFIRM set
    assert "A0" not in {m.pin_id for m in voters}


def test_invariant_no_hole_nonstrap_miswire_still_fires():
    """(ii) The exemption must not open a hole: a genuine miswire on a NON-strap
    pin of the SAME part (the INA219's own SDA pin cross-net swapped onto the SCL
    net) still FAILs via the M6 SDA/SCL-swap machinery, with the strap A0 entry
    present in the KB."""
    kb = _make_kb(
        _sensor("INA219", "SDA", Signal.I2C_SDA),
        _sensor("INA219", "SCL", Signal.I2C_SCL),
        _strap_pin("INA219", "A0"),
    )
    nl = Netlist(
        components=[Component("U1", "INA219", [
            PinRef("SDA", "/I2C.SCL", "SDA"),   # SDA pin on the SCL net (swap)
            PinRef("SCL", "/I2C.SDA", "SCL"),   # SCL pin on the SDA net (swap)
            PinRef("A0",  "/I2C.SDA", "A0"),
        ])],
        nets=[Net("/I2C.SDA", [("U1", "SCL"), ("U1", "A0")]),
              Net("/I2C.SCL", [("U1", "SDA")])],
    )
    fails = _fails(check_i2c_peripheral(nl, kb, {}))
    assert len(fails) == 2
    assert all("SDA/SCL swap" in f.evidence for f in fails)


# ══════════════════════════════════════════════════════════════════════════════
# 5. KB'd-doubles fixture pair (INA219-shaped, synthetic test-KB, end-to-end)
# ══════════════════════════════════════════════════════════════════════════════

def _ina219_doubles_kb():
    """Synthetic INA219 KB with SDA/SCL protocol pins + A0/A1 strap-marked —
    the shape Phase 2's production entry will carry. NOT the production KB."""
    return _make_kb(
        _sensor("INA219", "SDA", Signal.I2C_SDA),
        _sensor("INA219", "SCL", Signal.I2C_SCL),
        _strap_pin("INA219", "A0"),
        _strap_pin("INA219", "A1"),
    )


def test_kbd_doubles_variant_a_a0_tied_to_scl_stays_silent():
    """Variant A — A0 tied directly to SCL (the U66 shape): the checker stays
    SILENT on A0 — no FAIL, no condemnation, and A0 counts RESOLVED in coverage
    (no UNRESOLVABLE)."""
    kb = _ina219_doubles_kb()
    nl = Netlist(
        components=[
            Component("U66", "INA219", [
                PinRef("SCL", "/I2C_{SYS}.SCL", "SCL"),
                PinRef("SDA", "/I2C_{SYS}.SDA", "SDA"),
                PinRef("A0",  "/I2C_{SYS}.SCL", "A0"),   # A0 strapped onto SCL
            ]),
            _pullup("R1", "/I2C_{SYS}.SCL"),
            _pullup("R2", "/I2C_{SYS}.SDA"),
        ],
        nets=[Net("/I2C_{SYS}.SCL", [("U66", "SCL"), ("U66", "A0"), ("R1", "1")]),
              Net("/I2C_{SYS}.SDA", [("U66", "SDA"), ("R2", "1")]),
              Net("+3V3", [("R1", "2"), ("R2", "2")])],
    )
    findings = check_i2c_peripheral(nl, kb, {})
    assert _fails(findings) == []
    assert _cap_fails(findings) == []
    assert _unresolvable(findings) == []
    assert findings == []


def test_kbd_doubles_variant_b_sda_swap_still_fails():
    """Variant B — a genuine SDA cross-net swap on the same INA219 part still
    FAILs exactly as the M6/M14 machinery does today, with A0/A1 strap-marked in
    the KB. The exemption silences ONLY the strap pins, never a real defect."""
    kb = _ina219_doubles_kb()
    nl = Netlist(
        components=[Component("U60", "INA219", [
            PinRef("SCL", "/I2C_{SYS}.SDA", "SCL"),   # SCL pin on the SDA net
            PinRef("SDA", "/I2C_{SYS}.SCL", "SDA"),   # SDA pin on the SCL net
        ])],
        nets=[Net("/I2C_{SYS}.SDA", [("U60", "SCL")]),
              Net("/I2C_{SYS}.SCL", [("U60", "SDA")])],
    )
    fails = _fails(check_i2c_peripheral(nl, kb, {}))
    assert len(fails) == 2
    assert all("SDA/SCL swap" in f.evidence for f in fails)

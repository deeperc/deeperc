"""Tests for the pin-role/net-name coherence primitive + M6 (I2C SDA/SCL swap).

Covers: the primitive's contradiction signature, the dot-widen of classify_net_name
(with a nil-blast-radius assertion), the M6 wiring producing a FAIL with correct
evidence on the stamp-style swap, the ESP32-matrix coverage gate, and a step_08d
I2C regression guard.
"""
import os
import sys
from dataclasses import dataclass, field

import pytest

_HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_HERE, ".."))        # schematic_checker_poc → steps
sys.path.insert(0, os.path.join(_HERE, "..", ".."))  # repo root → peripheral_detectability

from steps import peripheral_coherence as pc            # noqa: E402
from steps.peripheral_roles import classify_net_name, Role  # noqa: E402
from steps.peripheral_kb import Signal, Peripheral, PeripheralRouting, PinRole, PinFunctionEntry, KBSource  # noqa: E402
from steps import step_08d_peripheral_checker as s08d   # noqa: E402


# ── minimal IR doubles ────────────────────────────────────────────────────────
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
    @property
    def effective_mpn(self): return self.mpn


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


# ── Test 1: primitive — contradiction fires, coherent does not ────────────────
def test_primitive_sda_pin_on_scl_net_violates():
    ir = _ir([_Comp("U3", "RP2040_Stamp", [_Pin("2", "SDA", "/SCL"),
                                            _Pin("3", "SCL", "/SDA")])])
    vios = pc.find_coherence_violations(ir, pc.I2C_PAIR)
    nets = {(v.refdes, v.pin_id, v.pin_role, v.net_role, v.status) for v in vios}
    assert ("U3", "2", "I2C_DATA", "I2C_CLOCK", "FAIL") in nets
    assert ("U3", "3", "I2C_CLOCK", "I2C_DATA", "FAIL") in nets


def test_primitive_coherent_wiring_no_violation():
    ir = _ir([_Comp("U3", "RP2040_Stamp", [_Pin("2", "SDA", "/SDA"),
                                            _Pin("3", "SCL", "/SCL")])])
    assert pc.find_coherence_violations(ir, pc.I2C_PAIR) == []


def test_primitive_generic_pins_no_violation():
    # generic pin functions assert no role → no fire even on SDA/SCL nets
    ir = _ir([_Comp("U8", "MUX", [_Pin("7", "MP0", "/SCL"), _Pin("5", "MP1", "/SDA")])])
    assert pc.find_coherence_violations(ir, pc.I2C_PAIR) == []


# ── TODO-303 2a: unconnected-net guard (module-local, mirrors step_08g/
# peripheral_bus_pairing's own _UNCONNECTED_NET_RE) ───────────────────────────
# 'unconnected_SDA'/'unconnected_SCL' are role-bearing TODAY (classify_net_name
# has no D2 unwrap yet, but the underscore-delimited marker form already
# matches NET_NAME_PATTERNS's anchored SDA/SCL regex directly) — a real,
# currently-classifiable shape to prove the guard actually short-circuits
# role_from_net_name, not a case that would be inert either way.

def test_unconnected_net_guard_skips_role_bearing_payload():
    # Without the guard this would FAIL (pin_role SCL != net_role SDA); the
    # guard must skip role_from_net_name entirely once the net name matches
    # the unconnected marker, regardless of what role its payload implies.
    ir = _ir([_Comp("U3", "RP2040_Stamp", [_Pin("2", "SCL", "unconnected_SDA")])])
    assert pc.find_coherence_violations(ir, pc.I2C_PAIR) == []


def test_unconnected_net_guard_does_not_filter_connected_role_bearing_net():
    # Control: an otherwise-identical CONNECTED net (no unconnected marker)
    # with the same mismatched pin/net roles must still fire — the guard keys
    # strictly on the unconnected marker, not on the presence of a role token.
    ir = _ir([_Comp("U3", "RP2040_Stamp", [_Pin("2", "SCL", "/SDA")])])
    vios = pc.find_coherence_violations(ir, pc.I2C_PAIR)
    nets = {(v.refdes, v.pin_id, v.pin_role, v.net_role, v.status) for v in vios}
    assert ("U3", "2", "I2C_CLOCK", "I2C_DATA", "FAIL") in nets


# ── Test 2: dot-widen + nil blast radius ──────────────────────────────────────
def test_dot_widen_classifies_dotted_peripheral_nets():
    assert classify_net_name("SPI.MOSI").role == Role.SPI_DATA_OUT
    assert classify_net_name("UART1.TX").role == Role.UART_TX
    assert classify_net_name("I2C_{CAM}.SDA").role == Role.I2C_DATA
    assert classify_net_name("UART1.RX").role == Role.UART_RX


def test_dot_widen_nil_blast_radius_on_underscore_names():
    # widening must not change classification of the already-handled forms
    assert classify_net_name("/SDA").role == Role.I2C_DATA
    assert classify_net_name("PDC_I2C3_SCL").role == Role.I2C_CLOCK
    assert classify_net_name("D34_MOSI2").role == Role.SPI_DATA_OUT
    # and must not invent roles for non-peripheral dotted names
    assert classify_net_name("3.3V") is None
    assert classify_net_name("VBUS") is None


def test_space_delim_classifies_space_named_peripheral_nets():
    # KiCad labels can literally contain a space ('/I2C0 SCL'); the space is not a
    # role-token boundary, so without the ' ' → '_' normalise the token has no anchor
    # (recon rp2040_kb_recon.md P1). This is the close that makes the original
    # space-named i2c-swap-rp2040-kb board catch end-to-end.
    assert classify_net_name("/I2C0 SCL").role == Role.I2C_CLOCK
    assert classify_net_name("I2C0 SDA").role == Role.I2C_DATA
    assert classify_net_name("SPI1 MOSI").role == Role.SPI_DATA_OUT
    assert classify_net_name("UART1 TX").role == Role.UART_TX
    # runs of spaces collapse to runs of '_', still a valid boundary
    assert classify_net_name("I2C0  SCL").role == Role.I2C_CLOCK


def test_space_delim_no_spurious_classification_on_benign_space_names():
    # widening to space must NOT invent a (peripheral) role for benign space names.
    # classify_net_name has no rail/power-classification path at all, so the
    # masking-via-rail direction is structurally N/A here — but a benign name that
    # newly classified as a peripheral role would still be a false positive.
    for benign in ("STATUS LED", "POWER GOOD", "SCAN LINE", "MY NET", "SIGNAL QA"):
        assert classify_net_name(benign) is None, benign
    # existing underscore/dot forms remain unchanged by the space widen
    assert classify_net_name("/I2C0_SCL").role == Role.I2C_CLOCK
    assert classify_net_name("SPI.MOSI").role == Role.SPI_DATA_OUT


def test_classify_net_name_has_no_shipping_checker_consumers():
    # nil-blast-radius is structural: step_06/step_08/step_08d must not import
    # peripheral_roles (the only safe place classify_net_name can be consumed).
    import subprocess
    steps_dir = os.path.join(_HERE, "..", "steps")
    for mod in ("step_06_power_validator.py", "step_08_checker.py",
                "step_08d_peripheral_checker.py"):
        path = os.path.join(steps_dir, mod)
        if not os.path.exists(path):
            continue
        text = open(path).read()
        assert "classify_net_name" not in text, f"{mod} consumes classify_net_name"


# ── Test 3: M6 wiring — stamp swap → FAIL with evidence; clean board → none ────
_KB = {("STM32F103C(8-B)Tx", "42"): PinFunctionEntry(
    "STM32F103C(8-B)Tx", "42",
    [PinRole(Peripheral.I2C, "I2C1", Signal.I2C_SDA, KBSource.VENDOR_XML)])}
_ROUTING = {"STM32F103C(8-B)Tx": {Peripheral.I2C: PeripheralRouting.FIXED},
            "esp32": {Peripheral.I2C: PeripheralRouting.MATRIX}}


def test_m6_stamp_swap_fails_with_evidence():
    ir = _ir([_Comp("U3", "RP2040_Stamp", [_Pin("2", "SDA", "/SCL"),
                                            _Pin("3", "SCL", "/SDA")])])
    findings = s08d.check_i2c_peripheral(ir, _KB, _ROUTING)
    fails = [f for f in findings if f.severity is s08d.Severity.FAIL
             and "SDA/SCL swap" in f.evidence]
    assert len(fails) == 2
    f = next(f for f in fails if "U3.2" in f.pins)
    assert f.net == "/SCL" and "U3.2" in f.pins
    assert "is an I2C SDA pin" in f.evidence and "implies I2C SCL" in f.evidence


def test_m6_clean_i2c_board_no_coherence_finding():
    ir = _ir([_Comp("U3", "RP2040_Stamp", [_Pin("2", "SDA", "/SDA"),
                                            _Pin("3", "SCL", "/SCL")]),
              _Comp("R1", "10k", [_Pin("1", "1", "/SDA"), _Pin("2", "2", "VCC")])])
    findings = s08d.check_i2c_peripheral(ir, _KB, _ROUTING)
    assert not [f for f in findings if "SDA/SCL swap" in f.evidence]


# ── Test 4: coverage gate — ESP32 matrix KB-role pin → UNRESOLVABLE, not FAIL ──
def test_coverage_gate_matrix_kb_role_is_unresolvable():
    # KB asserts SDA for this pin, but ESP32 routes I2C via the matrix → the role
    # is not pin-determined, so a contradiction must be UNRESOLVABLE not FAIL.
    kb = {("esp32", "10"): PinFunctionEntry(
        "esp32", "10", [PinRole(Peripheral.I2C, None, Signal.I2C_SDA, KBSource.VENDOR_HEADER)])}
    ir = _ir([_Comp("U1", "ESP32-WROOM-32", [_Pin("10", "IO10", "/SCL")])])
    vios = pc.find_coherence_violations(
        ir, pc.I2C_PAIR,
        kb_role_lookup=pc.kb_role_lookup_from(kb, s08d.canonicalize_mpn_for_kb, ir),
        matrix_lookup=pc.matrix_lookup_from(_ROUTING, s08d.canonicalize_mpn_for_kb, ir, Peripheral.I2C))
    assert len(vios) == 1 and vios[0].status == "UNRESOLVABLE"
    assert vios[0].source == "kb_possible_roles"


# ── Test 5: step_08d I2C topology regression ──────────────────────────────────
def test_step_08d_existing_role_mismatch_unchanged():
    # Two STM32 I2C pins, one SDA-only one SCL-only, on the SAME net → the existing
    # per-net ROLE_MISMATCH must still fire (coherence addition is additive).
    # Multi-function pins (I2C + GPIO) so they are NOT fixed-function and the
    # per-net ROLE_MISMATCH (which excludes fixed-function anchors) can fire.
    kb = {
        ("STM32F103C(8-B)Tx", "42"): PinFunctionEntry("STM32F103C(8-B)Tx", "42",
            [PinRole(Peripheral.I2C, "I2C1", Signal.I2C_SDA, KBSource.VENDOR_XML),
             PinRole(Peripheral.GPIO, None, Signal.GPIO, KBSource.VENDOR_XML)]),
        ("STM32F103C(8-B)Tx", "43"): PinFunctionEntry("STM32F103C(8-B)Tx", "43",
            [PinRole(Peripheral.I2C, "I2C1", Signal.I2C_SCL, KBSource.VENDOR_XML),
             PinRole(Peripheral.GPIO, None, Signal.GPIO, KBSource.VENDOR_XML)]),
    }
    # net 'I2C_BUS' carries no SDA/SCL token, so coherence stays silent and only
    # the per-net ROLE_MISMATCH should fire.
    ir = _ir([_Comp("U1", "STM32F103C8T6",
                    [_Pin("42", "PB7", "I2C_BUS"), _Pin("43", "PB6", "I2C_BUS")])])
    findings = s08d.check_i2c_peripheral(ir, kb, _ROUTING)
    assert any(f.violation is s08d.PeripheralViolation.ROLE_MISMATCH
               and f.severity is s08d.Severity.FAIL
               and "conflicting I2C signals" in f.evidence for f in findings)


# ── KB pin-key fix: lookup by LOGICAL NAME, not pin number ────────────────────
# Real KB convention: keyed by logical pin name ('PB7'), as kb/vendor/stm32/*.json
# is. A real export references pins by NUMBER ('43') with the name on pinfunction.
_KB_NAME = {
    ("STM32F103C(8-B)Tx", "PB7"): PinFunctionEntry("STM32F103C(8-B)Tx", "PB7",
        [PinRole(Peripheral.I2C, "I2C1", Signal.I2C_SDA, KBSource.VENDOR_XML)]),
    ("STM32F103C(8-B)Tx", "PB6"): PinFunctionEntry("STM32F103C(8-B)Tx", "PB6",
        [PinRole(Peripheral.I2C, "I2C1", Signal.I2C_SCL, KBSource.VENDOR_XML)]),
    # ESP32 pin carries a KB I2C role too, but the part is matrix-routed.
    ("esp32", "GPIO21"): PinFunctionEntry("esp32", "GPIO21",
        [PinRole(Peripheral.I2C, None, Signal.I2C_SDA, KBSource.VENDOR_HEADER)]),
}


def test_kb_lookup_keys_by_logical_name_not_number():
    # A numeric-pin pin (pin_id '43') resolves via its logical name 'PB7'; the
    # raw number alone (no logical name) does NOT hit the name-keyed KB.
    st, entry = s08d._resolve_pin("STM32F103C8T6", "43", _KB_NAME, _ROUTING, "PB7")
    assert st is s08d._LookupStatus.OK and entry is not None
    assert any(r.signal is Signal.I2C_SDA for r in entry.roles)

    st_num, entry_num = s08d._resolve_pin("STM32F103C8T6", "43", _KB_NAME, _ROUTING, None)
    assert entry_num is None                     # number-only misses the name-keyed KB
    assert st_num is not s08d._LookupStatus.OK


def test_real_numeric_pin_stm32_swap_resolves_via_kb_name_path():
    # The anti-trap case: numeric pins (43/42) + pinfunction labels (PB7/PB6), as
    # a real STM32 export emits — NOT a logical-name-as-number symbol. The swap
    # must produce KB-sourced coherence violations through the real resolution path.
    ir = _ir([_Comp("U1", "STM32F103C8T6",
                    [_Pin("43", "PB7", "/I2C1_SCL"),    # sda-role pin on SCL net
                     _Pin("42", "PB6", "/I2C1_SDA")])]) # scl-role pin on SDA net
    vios = pc.check_i2c_coherence(ir, _KB_NAME, _ROUTING, s08d.canonicalize_mpn_for_kb)
    by_pin = {(v.pin_id, v.pin_role, v.net_role, v.status, v.source) for v in vios}
    assert ("43", "I2C_DATA", "I2C_CLOCK", "FAIL", "kb_possible_roles") in by_pin
    assert ("42", "I2C_CLOCK", "I2C_DATA", "FAIL", "kb_possible_roles") in by_pin


def test_clean_miss_when_no_logical_name():
    # A pin with no resolvable logical name and a numeric pin_id must MISS cleanly
    # (never a wrong-key hit on the name-keyed KB).
    ir = _ir([_Comp("U1", "STM32F103C8T6",
                    [_Pin("43", "", "/I2C1_SCL"), _Pin("42", "", "/I2C1_SDA")])])
    assert pc.check_i2c_coherence(ir, _KB_NAME, _ROUTING, s08d.canonicalize_mpn_for_kb) == []


def test_pin_function_path_unbroken_by_keying_change():
    # The stamp U3 swap (pin-function path, no KB) must STILL fire — the keying
    # change must not break the path that already worked (the U54-class catch).
    ir = _ir([_Comp("U3", "RP2040_Stamp", [_Pin("2", "SDA", "/SCL"),
                                            _Pin("3", "SCL", "/SDA")])])
    findings = s08d.check_i2c_peripheral(ir, _KB_NAME, _ROUTING)
    fails = [f for f in findings if f.severity is s08d.Severity.FAIL
             and "SDA/SCL swap" in f.evidence]
    assert len(fails) == 2


def test_esp32_matrix_pin_still_unresolvable_after_keying_change():
    # Even with a name-keyed KB I2C role for the ESP32 pin, matrix routing must
    # short-circuit to UNRESOLVABLE — the keying change must not resolve a wrong
    # role on a matrix part.
    st, entry = s08d._resolve_pin("ESP32-WROOM-32", "37", _KB_NAME, _ROUTING, "GPIO21")
    assert st is s08d._LookupStatus.PERIPHERAL_UNCONSTRAINED
    assert entry is None


# ── M12: SPI MOSI/MISO coherence (reuse the primitive over SPI_PAIR) ──────────
# Pin-function-first: the MOSI/MISO regex + the DI/DO/SDI/SDO/DIN/DOUT flash
# alias. The alias is net-frame directional — a peripheral's DI pin belongs on
# the MOSI net, its DO pin on the MISO net — so correctly-wired SPI is coherent
# and only a swap contradicts (the crossover guard that makes SPI coherence-
# detectable where UART/M5 is not). TODO-410 adds a KB possible_roles fallback
# (peripheral_kb.Signal.SPI_MOSI/MISO/SCK/NSS), consulted only when the pin
# function asserts no role — see the block below and check_spi_coherence's
# docstring.

# Test 1: contradiction fires; correctly-wired SPI does NOT (the crossover guard)
def test_spi_di_pin_on_miso_net_violates():
    # A flash DI pin (belongs on MOSI) landed on a MISO-named net → contradiction.
    ir = _ir([_Comp("U7", "W25Q128", [_Pin("5", "DI", "/MISO"),
                                       _Pin("2", "DO", "/MOSI")])])
    vios = pc.find_coherence_violations(ir, pc.SPI_PAIR)
    seen = {(v.refdes, v.pin_id, v.pin_role, v.net_role, v.status) for v in vios}
    assert ("U7", "5", "SPI_DATA_OUT", "SPI_DATA_IN", "FAIL") in seen   # DI on MISO
    assert ("U7", "2", "SPI_DATA_IN", "SPI_DATA_OUT", "FAIL") in seen   # DO on MOSI


def test_spi_correctly_wired_no_violation_crossover_guard():
    # The crossover backstop: DI on the MOSI net + DO on the MISO net is CORRECT
    # wiring — pin role == net role — and must NOT flag (unlike UART/M5, where a
    # TXD pin correctly sits on an RX-named net). Also covers literal MOSI/MISO.
    ir = _ir([_Comp("U7", "W25Q128", [_Pin("5", "DI", "/MOSI"),
                                      _Pin("2", "DO", "/MISO")]),
              _Comp("U1", "MCU", [_Pin("10", "MOSI", "/MOSI"),
                                  _Pin("11", "MISO", "/MISO")])])
    assert pc.find_coherence_violations(ir, pc.SPI_PAIR) == []


# Test 2: alias parity — DI/SDI/DIN and DO/SDO/DOUT, correct direction
def test_spi_alias_parity_all_data_forms():
    import peripheral_detectability as pdet
    R = pdet.Role
    for fn in ("DI", "SDI", "DIN"):
        assert pdet.role_from_pin_function(fn) is R.SPI_DATA_OUT, fn   # → MOSI net
    for fn in ("DO", "SDO", "DOUT"):
        assert pdet.role_from_pin_function(fn) is R.SPI_DATA_IN, fn    # → MISO net
    # and the alias must not over-match lookalikes
    for fn in ("DIODE", "DONE", "DISABLE", "RADIO"):
        assert pdet.role_from_pin_function(fn) is None, fn


def test_spi_alias_each_form_contradicts_via_checker():
    # Each data form, when swapped onto the opposite net, produces a checker FAIL.
    for di in ("DI", "SDI", "DIN"):
        ir = _ir([_Comp("U7", "FLASH", [_Pin("1", di, "/MISO")])])
        fails = [f for f in s08d.check_i2c_peripheral(ir, {}, _ROUTING)
                 if f.severity is s08d.Severity.FAIL and "MOSI/MISO swap" in f.evidence]
        assert len(fails) == 1, di
    for do in ("DO", "SDO", "DOUT"):
        ir = _ir([_Comp("U7", "FLASH", [_Pin("1", do, "/MOSI")])])
        fails = [f for f in s08d.check_i2c_peripheral(ir, {}, _ROUTING)
                 if f.severity is s08d.Severity.FAIL and "MOSI/MISO swap" in f.evidence]
        assert len(fails) == 1, do


# Test 3: the 2 OLIMEXINO SD swap mutants → coherence FAIL with correct evidence.
# Mirrors the real exported netlist (verified): MICRO_SD1.3 'CMD/DI' and .7
# 'DAT0/DO' with their post-swap nets D33_MISO2 / D34_MOSI2.
def test_m12_olimexino_sd_swap_fails_with_evidence():
    ir = _ir([_Comp("MICRO_SD1", "SD_Card", [
                  _Pin("3", "CMD/DI", "/D33_MISO2"),    # DI (MOSI-role) on MISO net
                  _Pin("7", "DAT0/DO", "/D34_MOSI2")])])  # DO (MISO-role) on MOSI net
    findings = s08d.check_i2c_peripheral(ir, {}, _ROUTING)
    fails = [f for f in findings if f.severity is s08d.Severity.FAIL
             and "MOSI/MISO swap" in f.evidence]
    assert len(fails) == 2
    f3 = next(f for f in fails if "MICRO_SD1.3" in f.pins)
    assert f3.net == "/D33_MISO2"
    assert "is an SPI MOSI pin" in f3.evidence and "implies SPI MISO" in f3.evidence
    assert "role source: pin_function" in f3.evidence
    f7 = next(f for f in fails if "MICRO_SD1.7" in f.pins)
    assert "is an SPI MISO pin" in f7.evidence and "implies SPI MOSI" in f7.evidence


# Test 4: coverage gate — matrix-routed / no-role SPI pin → no FAIL.
# SPI has no KB role source, so a generic-named pin asserts no role and yields no
# finding at all (the routing-independent pin-function path simply does not fire).
# The invariant under test is the safety one: such a pin is NEVER a coherence FAIL.
def test_spi_coverage_gate_no_role_pin_not_a_fail():
    # ESP32 matrix-routed SPI pin carries a generic GPIO function on a MOSI net →
    # no role → no finding (and certainly no FAIL).
    ir = _ir([_Comp("U1", "ESP32-WROOM-32", [_Pin("23", "IO23", "/MOSI")])])
    assert pc.find_coherence_violations(ir, pc.SPI_PAIR) == []
    assert not [f for f in s08d.check_i2c_peripheral(ir, {}, _ROUTING)
                if "MOSI/MISO swap" in f.evidence]


# ── Todo 99 (M99): SCK — checker-side group widen to {MOSI, MISO, SCK} ────────
# find_coherence_violations makes no 2-member assumption (frozenset membership
# only), so this is a signal-set widen, not a primitive change (recon: Step 1a).
# The mutation-operator pair (SPI_PAIR) and its MOSI/MISO evidence text are
# UNCHANGED — verified below, and by test_m12_spi_unchanged_by_m99_addition.

def test_sck_pin_on_mosi_net_violates():
    # A pin literally named SCK landed on a MOSI-named net -> contradiction.
    ir = _ir([_Comp("U1", "MCU", [_Pin("18", "SCK", "/MOSI"),
                                   _Pin("19", "MOSI", "/SCK")])])
    vios = pc.find_coherence_violations(ir, pc.SPI_COHERENCE_GROUP)
    seen = {(v.refdes, v.pin_id, v.pin_role, v.net_role, v.status) for v in vios}
    assert ("U1", "18", "SPI_CLOCK", "SPI_DATA_OUT", "FAIL") in seen
    assert ("U1", "19", "SPI_DATA_OUT", "SPI_CLOCK", "FAIL") in seen
    # not visible under the old 2-member pair (regression guard on the widen itself)
    assert pc.find_coherence_violations(ir, pc.SPI_PAIR) == []


def test_sck_correctly_wired_no_violation():
    # SCK on the SCK net, MOSI on the MOSI net — pin role == net role, no flag.
    ir = _ir([_Comp("U1", "MCU", [_Pin("18", "SCK", "/SCK"),
                                   _Pin("19", "MOSI", "/MOSI"),
                                   _Pin("20", "MISO", "/MISO")])])
    assert pc.find_coherence_violations(ir, pc.SPI_COHERENCE_GROUP) == []


def test_m99_sck_swap_fails_via_checker_with_new_evidence_tail():
    ir = _ir([_Comp("U1", "MCU", [_Pin("18", "SCK", "/MOSI"),
                                   _Pin("19", "MOSI", "/SCK")])])
    findings = s08d.check_i2c_peripheral(ir, {}, _ROUTING)
    fails = [f for f in findings if f.severity is s08d.Severity.FAIL
             and "SPI role swap" in f.evidence]
    assert len(fails) == 2
    f18 = next(f for f in fails if "U1.18" in f.pins)
    assert "is an SPI SCK pin" in f18.evidence and "implies SPI MOSI" in f18.evidence
    assert "SCK/MOSI SPI role swap" in f18.evidence
    # the old fixed literal must NOT appear on an SCK-involved violation
    assert "MOSI/MISO swap" not in f18.evidence


# Test 5: M6 I2C coherence + stamp catch unchanged by the SPI addition.
def test_m6_i2c_unchanged_by_m12_addition():
    # SDA/SCL stamp swap still fires exactly 2 I2C FAILs; SPI block adds nothing
    # for a board with no SPI nets.
    ir = _ir([_Comp("U3", "RP2040_Stamp", [_Pin("2", "SDA", "/SCL"),
                                            _Pin("3", "SCL", "/SDA")])])
    findings = s08d.check_i2c_peripheral(ir, _KB, _ROUTING)
    i2c_fails = [f for f in findings if f.severity is s08d.Severity.FAIL
                 and "SDA/SCL swap" in f.evidence]
    spi_fails = [f for f in findings if "MOSI/MISO swap" in f.evidence]
    assert len(i2c_fails) == 2 and spi_fails == []


def test_m12_spi_unchanged_by_m99_addition():
    # The M12 MOSI/MISO swap catch — evidence text and count — is BYTE-IDENTICAL
    # after the M99/SCK group widen (mirrors test_m12_olimexino_sd_swap_fails_with_evidence).
    ir = _ir([_Comp("MICRO_SD1", "SD_Card", [
                  _Pin("3", "CMD/DI", "/D33_MISO2"),
                  _Pin("7", "DAT0/DO", "/D34_MOSI2")])])
    findings = s08d.check_i2c_peripheral(ir, {}, _ROUTING)
    fails = [f for f in findings if f.severity is s08d.Severity.FAIL
             and "MOSI/MISO swap" in f.evidence]
    assert len(fails) == 2
    f3 = next(f for f in fails if "MICRO_SD1.3" in f.pins)
    assert f3.net == "/D33_MISO2"
    assert "is an SPI MOSI pin" in f3.evidence and "implies SPI MISO" in f3.evidence
    assert "role source: pin_function" in f3.evidence
    assert "SPI role swap" not in f3.evidence   # new tail must not leak into MOSI/MISO case


# ── TODO-410: SPI KB possible_roles source (check_spi_coherence) ──────────────
# Mirrors the I2C KB tests above (test_coverage_gate_matrix_kb_role_is_unresolvable
# / test_real_numeric_pin_stm32_swap_resolves_via_kb_name_path), same shape: a
# generic-named MCU pin (no MOSI/MISO/SCK token) resolves its role from KB
# possible_roles only, since the pin-function source asserts nothing.

def test_spi_kb_role_pin_on_wrong_net_violates():
    # PA6 (KB: SPI1 MISO), generic pin-function (no MOSI/MISO/SCK token), sits
    # on a MOSI-named net -> KB-sourced violation. Mirrors the real
    # spi_swap_stm32 fixture (STM32F103C8Tx PA6_16 on /MOSI, TODO-410 Phase 1 recon).
    kb = {("STM32F103C(8-B)Tx", "PA6"): PinFunctionEntry(
        "STM32F103C(8-B)Tx", "PA6",
        [PinRole(Peripheral.SPI, "SPI1", Signal.SPI_MISO, KBSource.VENDOR_XML)])}
    ir = _ir([_Comp("U1", "STM32F103C8T6", [_Pin("16", "PA6", "/MOSI")])])
    vios = pc.check_spi_coherence(ir, kb, s08d.canonicalize_mpn_for_kb)
    assert len(vios) == 1
    v = vios[0]
    assert v.pin_role == "SPI_DATA_IN" and v.net_role == "SPI_DATA_OUT"
    assert v.status == "FAIL" and v.source == "kb_possible_roles"


def test_spi_kb_role_ambiguous_pin_no_violation():
    # A remappable pin whose KB possible_roles include BOTH MOSI (SPI1) and MISO
    # (SPI2) asserts neither -> no violation (same ambiguity rule as I2C).
    kb = {("STM32F103C(8-B)Tx", "PB5"): PinFunctionEntry(
        "STM32F103C(8-B)Tx", "PB5",
        [PinRole(Peripheral.SPI, "SPI1", Signal.SPI_MOSI, KBSource.VENDOR_XML),
         PinRole(Peripheral.SPI, "SPI2", Signal.SPI_MISO, KBSource.VENDOR_XML)])}
    ir = _ir([_Comp("U1", "STM32F103C8T6", [_Pin("41", "PB5", "/MOSI")])])
    assert pc.check_spi_coherence(ir, kb, s08d.canonicalize_mpn_for_kb) == []


def test_spi_pin_function_wins_over_conflicting_kb():
    # Pin-function names the pin MOSI directly and it correctly sits on the MOSI
    # net; the KB (if consulted) would say MISO. Source 1 (pin-function) fires
    # FIRST — find_coherence_violations only falls back to KB when the pin-
    # function source asserts NO role of the pair — so the KB is never
    # consulted and no violation fires despite the conflicting KB entry (D2:
    # no conflict-arm between the two sources by design).
    kb = {("STM32F103C(8-B)Tx", "PA6"): PinFunctionEntry(
        "STM32F103C(8-B)Tx", "PA6",
        [PinRole(Peripheral.SPI, "SPI1", Signal.SPI_MISO, KBSource.VENDOR_XML)])}
    ir = _ir([_Comp("U1", "STM32F103C8T6", [_Pin("16", "MOSI", "/MOSI")])])
    assert pc.check_spi_coherence(ir, kb, s08d.canonicalize_mpn_for_kb) == []


def test_spi_kb_nss_role_out_of_coherence_group():
    # KB says this pin is SPI1 NSS; NSS is NOT in SPI_COHERENCE_GROUP (D3 ruling)
    # -> even sitting on a MOSI-named net, no coherence violation fires.
    kb = {("STM32F103C(8-B)Tx", "PA4"): PinFunctionEntry(
        "STM32F103C(8-B)Tx", "PA4",
        [PinRole(Peripheral.SPI, "SPI1", Signal.SPI_NSS, KBSource.VENDOR_XML)])}
    ir = _ir([_Comp("U1", "STM32F103C8T6", [_Pin("14", "PA4", "/MOSI")])])
    assert pc.check_spi_coherence(ir, kb, s08d.canonicalize_mpn_for_kb) == []


# ── Test 6: RP2040 KB — fixed-mux I2C role catches the GPIO-named swap ─────────
# RP2040 pins are named GPIOn (no SDA/SCL in the name), so the pin-function path is
# blind; the catch must come from the KB possible_roles (kb/vendor/raspberrypi).
# This is the real loaded KB + canonicalization, so it doubles as the "RP2040 entry
# present and wired" guard. GPIO0 = I2C0 SDA (fixed), GPIO1 = I2C0 SCL.
_REAL_KB_DIR = os.path.join(_HERE, "..", "..", "kb", "vendor")


def _load_real_kb():
    from steps.peripheral_kb import load_peripheral_kb  # noqa: PLC0415
    if not os.path.isdir(_REAL_KB_DIR):
        pytest.skip("kb/vendor not present")
    return load_peripheral_kb(_REAL_KB_DIR)


def test_rp2040_kb_canonicalization():
    # All silicon/stepping forms collapse to the one KB key; module MPNs do not.
    assert s08d.canonicalize_mpn_for_kb("RP2040") == "RP2040"
    assert s08d.canonicalize_mpn_for_kb("RP2040-B2") == "RP2040"
    assert s08d.canonicalize_mpn_for_kb("SC0914(13)") == "SC0914(13)"


def test_rp2040_i2c_swap_caught_via_kb():
    kb, routing = _load_real_kb()
    assert ("RP2040", "GPIO0") in kb, "RP2040 KB must be loaded"
    # GPIO0 (I2C0 SDA) on the SCL net, GPIO1 (I2C0 SCL) on the SDA net → swap.
    ir = _ir([_Comp("U1", "RP2040-B2", [_Pin("2", "GPIO0", "/I2C0_SCL"),
                                        _Pin("3", "GPIO1", "/I2C0_SDA")])])
    fails = [f for f in s08d.check_i2c_peripheral(ir, kb, routing)
             if f.severity is s08d.Severity.FAIL and "SDA/SCL swap" in f.evidence]
    assert len(fails) == 2
    assert all("role source: kb_possible_roles" in f.evidence for f in fails)


def test_rp2040_correct_i2c_no_false_fail():
    kb, routing = _load_real_kb()
    # GPIO0 (SDA) on the SDA net, GPIO1 (SCL) on the SCL net → coherent, no FAIL.
    ir = _ir([_Comp("U1", "RP2040-B2", [_Pin("2", "GPIO0", "/I2C0_SDA"),
                                        _Pin("3", "GPIO1", "/I2C0_SCL")])])
    fails = [f for f in s08d.check_i2c_peripheral(ir, kb, routing)
             if f.severity is s08d.Severity.FAIL]
    assert fails == []


# ── Test 7: STM32F303 KB — Todo 93 UART seed unblock (OLIMEXINO-STM32F3) ───────
# F303 target added to kb/vendor/stm32/ so OLIMEXINO-STM32F3 (STM32F303RCT6) becomes
# a KB'd-MCU-with-exposed-UART seed for the (separately-scoped) Todo 93 build. This
# is a target-coverage add only — no _STM32_SIGNAL_MAP / Signal-enum change.

def test_stm32f303_kb_canonicalization():
    assert s08d.canonicalize_mpn_for_kb("STM32F303RCT6") == "STM32F303R(B-C)Tx"
    assert s08d.canonicalize_mpn_for_kb("STM32F303RBT6") == "STM32F303R(B-C)Tx"


def test_stm32f303_uart_role_resolved_via_kb():
    # OLIMEXINO-STM32F3 (Rev C1/D): U5 PB10/PB11 on /D29_USART3TX, /D30_USART3RX.
    # Pin-function is bare "PB10"/"PB11" (no alt-fn text) — the role can ONLY come
    # from the KB. This is the Todo 93 seed-unblock this KB add exists for.
    kb, routing = _load_real_kb()
    assert ("STM32F303R(B-C)Tx", "PB10") in kb, "STM32F303 KB must be loaded"
    status, entry = s08d._resolve_pin("STM32F303RCT6", "29", kb, routing, pin_name="PB10")
    assert status is s08d._LookupStatus.OK
    assert any(r.signal is Signal.UART_TX and r.instance == "USART3" for r in entry.roles)

    status, entry = s08d._resolve_pin("STM32F303RCT6", "30", kb, routing, pin_name="PB11")
    assert status is s08d._LookupStatus.OK
    assert any(r.signal is Signal.UART_RX and r.instance == "USART3" for r in entry.roles)


def test_stm32f303_i2c2_role_resolved_via_kb():
    # Same board's I2C2 bus (PA9=SCL, PA10=SDA) — confirms the KB add also lights
    # up M6 targeting on this seed's I2C2 bus (bad-corpus/recall consequence,
    # measured separately; not a new check wired here).
    kb, routing = _load_real_kb()
    status, entry = s08d._resolve_pin("STM32F303RCT6", "42", kb, routing, pin_name="PA9")
    assert status is s08d._LookupStatus.OK
    assert any(r.signal is Signal.I2C_SCL and r.instance == "I2C2" for r in entry.roles)

    status, entry = s08d._resolve_pin("STM32F303RCT6", "43", kb, routing, pin_name="PA10")
    assert status is s08d._LookupStatus.OK
    assert any(r.signal is Signal.I2C_SDA and r.instance == "I2C2" for r in entry.roles)


# ── Test 8: STM32F405 KB — F405-range alias onto F407V (dumpling coverage) ────
# F405 (RM0090) shares its reference manual and AF table with F407 — Nelson
# verified via STM32CubeMX per-part XML (2026-07-17) that the AF-to-peripheral
# mapping for shared GPIO names is identical between STM32F405RG and the
# existing STM32F407V(E-G)Tx KB entry. That verification licenses exactly one
# thing: aliasing the F405-range stem onto the F407V KB data — no new KB file,
# no new pins/roles. Todo-79 precedent (F407V seed itself was a similarly
# small, targeted _STM32_RANGE_MAP add). F411 (RM0383/RM0390, a different
# sub-family) is explicitly OUT of scope — locked by a permanent regression
# fixture below.

def test_stm32f405_kb_canonicalization():
    # The alias IS the shared value: F405RGTx canonicalizes to the exact same
    # range-form string the two existing F407V entries already use, so a
    # lookup dereferences the SAME loaded KB dict entries — no new file.
    assert s08d.canonicalize_mpn_for_kb("STM32F405RGTx") == "STM32F407V(E-G)Tx"


def test_stm32f405_i2c_role_resolved_via_alias():
    # U1 on dumpling: STM32F405RGTx PB6 must resolve I2C1/SCL identically to
    # a direct F407V query on the same GPIO name (real KB, real canonicalization).
    kb, routing = _load_real_kb()
    status, entry = s08d._resolve_pin("STM32F405RGTx", "92", kb, routing, pin_name="PB6")
    assert status is s08d._LookupStatus.OK
    assert any(r.signal is Signal.I2C_SCL and r.instance == "I2C1" for r in entry.roles)

    status_f407, entry_f407 = s08d._resolve_pin("STM32F407VGT6", "92", kb, routing, pin_name="PB6")
    assert status_f407 is s08d._LookupStatus.OK
    assert {(r.peripheral, r.signal, r.instance) for r in entry.roles} == \
           {(r.peripheral, r.signal, r.instance) for r in entry_f407.roles}


def test_stm32f405_pin_absent_from_kb_is_honest_miss():
    # Subset safety: a GPIO name genuinely absent from the F407V KB (PF0 is not
    # bonded out in this KB's pin list at all) must return PIN_NOT_IN_KB via the
    # F405 key — never a false CONFIRM. The 64-pin R package being a strict subset
    # of the 100-pin V package's GPIO names is the same mechanism, already safe.
    kb, routing = _load_real_kb()
    assert ("STM32F407V(E-G)Tx", "PF0") not in kb, "test assumes PF0 is absent from the real KB"
    status, entry = s08d._resolve_pin("STM32F405RGTx", "1", kb, routing, pin_name="PF0")
    assert status is s08d._LookupStatus.PIN_NOT_IN_KB
    assert entry is None


def test_stm32f411_stays_kb_missing_permanent_lock():
    # F411 is a different sub-family (own reference manual) and explicitly out
    # of scope for this alias — must stay KB_MISSING until its own future card.
    kb, routing = _load_real_kb()
    assert s08d.canonicalize_mpn_for_kb("STM32F411CEUx") == "STM32F411CEUx"
    status, entry = s08d._resolve_pin("STM32F411CEUx", "1", kb, routing, pin_name="PB6")
    assert status is s08d._LookupStatus.KB_MISSING
    assert entry is None


def test_stm32f401_other_f4_family_not_aliased():
    # Guard against the entry bleeding across F4 families: a different F4
    # family's canonicalized stem must not collide with the F405 range key.
    # No corpus board exercises this; wild boards do, so it must be locked here.
    kb, routing = _load_real_kb()
    assert s08d.canonicalize_mpn_for_kb("STM32F401CCU6") == "STM32F401CCU6"
    status, entry = s08d._resolve_pin("STM32F401CCU6", "1", kb, routing, pin_name="PB6")
    assert status is s08d._LookupStatus.KB_MISSING
    assert entry is None


# ── TODO-303 2c P-2c-4: Phase 2a's guards hold under D2's newly-armed input ──
# Direction 2 (2c) makes classify_net_name truthful on unconnected-(...) names
# it previously always returned None for. Prove Phase 2a's consumer-side
# guards -- not this phase's own code -- still intercept a floating pin's
# synthetic net name before it can produce a false coherence finding or a
# phantom pairing entry.

def test_d2_newly_classifiable_unconnected_net_produces_no_coherence_finding():
    """Without Phase 2a's guard, a floating SDA-shaped pin's synthetic net
    name would now contradict a real SCL pin function on the same net (D2
    makes the net classify as I2C_DATA) -- the guard in
    find_coherence_violations must intercept it before role_from_net_name is
    ever consulted."""
    # Sanity: the net DOES classify under D2 now -- this test is about
    # consumer-side containment, not classifier truthfulness (2c explicitly
    # keeps classify_net_name truthful on unconnected-(...) names).
    assert classify_net_name("unconnected-(U1-SDA-Pad5)").role == Role.I2C_DATA
    ir = _ir([_Comp("U1", "GENERIC_IC", [
        _Pin("1", "SCL", "unconnected-(U1-SDA-Pad5)"),
    ])])
    vios = pc.find_coherence_violations(ir, pc.I2C_PAIR)
    assert vios == []


def test_d2_newly_classifiable_unconnected_net_produces_no_pairing_entry():
    """Same shape through the signal-2 (name-prefix) pairing loop's PAIRED
    output -- an unconnected-(...) net that now classifies under Direction 2
    must never enter nets_by_role_stem / produce a PairedBus. (The separate
    `unpaired` diagnostic bucket recomputes role_from_net_name without this
    guard -- a known, documented, verdict-inert gap, Phase-1 T1 row 2 /
    Phase-2a's discovered-not-actioned note: its return value is discarded at
    the only production call site -- out of scope here, not asserted on.)"""
    from steps.peripheral_bus_pairing import pair_buses
    ir = _ir([
        _Comp("U1", "GENERIC_IC", [_Pin("1", "SCL", "/SCL")]),
        _Comp("U2", "GENERIC_IC", [_Pin("1", "SDA_GHOST", "unconnected-(U2-SDA-Pad3)")]),
    ])
    paired, _unpaired = pair_buses(ir, "I2C", pc.I2C_PAIR)
    assert paired == []

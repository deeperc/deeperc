"""Tests for step_08g — merged pull-up-PRESENCE check (Todo 243 + 212).

Covers the ONE polarity-aware pull-path walk primitive and the three WARN-tier
presence families (generic OD/OC, flash-CS, SD), plus the D6/D10 reversed-diode
lock and the LED-sink acceptance shape from the Phase-1 recon
(investigation/experiments/open_drain_pullup_recon/REPORT.md §5-§6).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from steps.step_02_parser import ComponentIR, PinIR, NetIR, NetlistIR
from steps.peripheral_kb import Signal, KBSource, Peripheral, PinRole, PinFunctionEntry
from steps.step_08g_pullup_presence import (
    has_pull_path, Direction, check_pullup_presence,
    _family_generic_od, _family_flash_cs, _family_sd,
    FAMILY_GENERIC_OD, FAMILY_FLASH_CS, FAMILY_SD,
    VIOLATION_OD_NO_PULLUP, VIOLATION_OD_NO_PULLDOWN,
    VIOLATION_FLASH_CS_NO_PULLUP, VIOLATION_SD_NO_PULLUP,
    VIOLATION_I2C_NAME_NO_PULLUP,
    ACTIVATION_OD_PINTYPE, ACTIVATION_I2C_NET_NAME,
    _build_comps_on_net, _net_i2c_pinfunction_tokens, _I2C_TOKEN_RE,
)


# ── builders ──────────────────────────────────────────────────────────────────

def C(refdes, pins, mpn="", value="", footprint=""):
    """pins: list of (pin_id, pin_name, net)."""
    return ComponentIR(
        refdes=refdes, part_number=mpn, value=value, footprint=footprint,
        pins=[PinIR(pid, pname, net) for pid, pname, net in pins],
    )


def build(components, pintypes=None, power_nets=None, ground_nets=None):
    """Auto-build nets from component pin membership (keeps net.pins consistent)."""
    netmap = {}
    for c in components:
        for p in c.pins:
            netmap.setdefault(p.net, []).append((c.refdes, p.pin_id))
    nets = [NetIR(name=n, pins=pins) for n, pins in netmap.items()]
    return NetlistIR(
        source_file="t", components=components, nets=nets,
        power_nets=power_nets or [], ground_nets=ground_nets or [],
        pintypes=pintypes or {},
    )


def net_of(ir, name):
    return next(n for n in ir.nets if n.name == name)


def _walk(ir, net_name, direction=Direction.UP):
    return has_pull_path(net_of(ir, net_name), ir, direction)


class _Spec:
    """Minimal pin_specs entry with an .open_drain attribute."""
    def __init__(self, open_drain=False, pin_type="input"):
        self.open_drain = open_drain
        self.pin_type = pin_type


# ═══ Step 2: the walk primitive ═══════════════════════════════════════════════

def test_walk_direct_r_to_rail_accept():
    ir = build([
        C("U1", [("1", "OUT", "SIG")]),
        C("U2", [("1", "IN", "SIG")]),
        C("R1", [("1", "1", "SIG"), ("2", "2", "+3V3")]),
    ])
    assert _walk(ir, "SIG", Direction.UP) is True


def test_walk_sub_floor_resistor_rejected():
    """A 0Ω / sub-10Ω link to the rail is a jumper, not a pull-up."""
    ir = build([
        C("U1", [("1", "OUT", "SIG")]),
        C("R1", [("1", "1", "SIG"), ("2", "2", "+3V3")], value="0"),
    ])
    assert _walk(ir, "SIG", Direction.UP) is False


def test_walk_diode_plus_r_correct_orientation_accept():
    """Cathode-on-net diode → anode → series R → rail (the D6 valid path)."""
    ir = build([
        C("U1", [("13", "O4", "RELAY_1")]),
        C("K1", [("2", "2", "RELAY_1")]),                    # coil (load, non-passive)
        C("D6", [("1", "K", "RELAY_1"), ("2", "A", "Net-D6-A")]),  # cathode on net
        C("R5", [("1", "1", "Net-D6-A"), ("2", "2", "RELAY_5V")], value="2K2"),
    ])
    assert _walk(ir, "RELAY_1", Direction.UP) is True


def test_walk_reversed_flyback_diode_rejected_D10_lock():
    """Anode-on-net diode reaching the rail is a flyback/protection diode, NOT a
    pull path (the D6/D10 lock — reversed orientation must be rejected)."""
    ir = build([
        C("U1", [("13", "O4", "RELAY_1")]),
        C("K1", [("2", "2", "RELAY_1")]),
        C("D10", [("2", "A", "RELAY_1"), ("1", "K", "RELAY_5V")]),  # anode on net (reversed)
    ])
    assert _walk(ir, "RELAY_1", Direction.UP) is False


def test_walk_diode_direct_to_rail_accept():
    """Cathode-on-net diode whose anode is directly a rail (no series R)."""
    ir = build([
        C("U1", [("1", "OUT", "SIG")]),
        C("U2", [("1", "IN", "SIG")]),
        C("D1", [("1", "K", "SIG"), ("2", "A", "+5V")]),
    ])
    assert _walk(ir, "SIG", Direction.UP) is True


def test_walk_up_query_not_satisfied_by_r_to_gnd():
    """Direction guard: an R to GROUND must not satisfy an UP (pull-up) query."""
    ir = build([
        C("U1", [("1", "OUT", "SIG")]),
        C("R1", [("1", "1", "SIG"), ("2", "2", "GND")]),
    ], ground_nets=["GND"])
    assert _walk(ir, "SIG", Direction.UP) is False
    assert _walk(ir, "SIG", Direction.DOWN) is True     # same R DOES satisfy a down query


def test_walk_series_depth_gt1_rejected():
    """Two resistors in series (R→R) exceed the one-element tolerance → reject."""
    ir = build([
        C("U1", [("1", "OUT", "SIG")]),
        C("R1", [("1", "1", "SIG"), ("2", "2", "MID")]),
        C("R2", [("1", "1", "MID"), ("2", "2", "+3V3")]),
    ])
    assert _walk(ir, "SIG", Direction.UP) is False


def test_walk_underscore_voltage_rail_recognized():
    """RELAY_5V-class rail: the base _POWER_RAIL_RE splits only on '/'+ws so a
    trailing '_5V' voltage token is missed; the walk applies the SAME recognizer
    per '_'-segment (corrects recon §6's parenthetical). A direct R to RELAY_5V
    must be accepted."""
    ir = build([
        C("U1", [("1", "OUT", "SIG")]),
        C("R1", [("1", "1", "SIG"), ("2", "2", "RELAY_5V")], value="4K7"),
    ])
    assert _walk(ir, "SIG", Direction.UP) is True


def test_walk_indeterminate_diode_polarity_rejected():
    """A diode whose net-side pin isn't a resolvable A/K terminal → not a pull path."""
    ir = build([
        C("U1", [("1", "OUT", "SIG")]),
        C("D1", [("1", "1", "SIG"), ("2", "2", "+3V3")]),   # numeric pin names
    ])
    assert _walk(ir, "SIG", Direction.UP) is False


# ═══ Step 3: Family 1 — generic OD/OC ═════════════════════════════════════════

def _od_net(pintype="open_collector", pull=False, members=2):
    comps = [C("U1", [("13", "O4", "SIG")])]
    pintypes = {("U1", "13"): pintype}
    if members >= 2:
        comps.append(C("U2", [("6", "IN", "SIG")]))
    if pull:
        comps.append(C("R1", [("1", "1", "SIG"), ("2", "2", "+3V3")], value="4K7"))
    return build(comps, pintypes=pintypes)


def _run_fam1(ir, kb=None, routing=None, pin_specs=None):
    cbr = {c.refdes: c for c in ir.components}
    con = _build_comps_on_net(ir)
    return _family_generic_od(ir, pin_specs or {}, kb or {}, routing, cbr, con)


def test_fam1_od_no_pullup_warns():
    f = _run_fam1(_od_net(pull=False))
    assert len(f) == 1
    assert f[0].family == FAMILY_GENERIC_OD
    assert f[0].violation == VIOLATION_OD_NO_PULLUP
    assert f[0].severity == "WARN"
    assert f[0].net == "SIG"


def test_fam1_od_with_pullup_silent():
    assert _run_fam1(_od_net(pull=True)) == []


def test_fam1_single_member_net_skipped():
    """<2 non-passive members = not actively used → no WARN."""
    assert _run_fam1(_od_net(pull=False, members=1)) == []


def test_fam1_pin_specs_open_drain_alone_not_a_trigger():
    """A pin flagged open_drain by the datasheet cache but NOT symbol-OD is not a
    trigger (symbol pintype is the sole trigger; pin_specs is corroborating-only)."""
    ir = build([
        C("U1", [("13", "O4", "SIG")]),
        C("U2", [("6", "IN", "SIG")]),
    ], pintypes={("U1", "13"): "output"})          # NOT open_collector
    specs = {("U1", "O4"): _Spec(open_drain=True)}
    assert _run_fam1(ir, pin_specs=specs) == []


def test_fam1_pin_specs_corroboration_recorded():
    ir = _od_net(pull=False)
    specs = {("U1", "O4"): _Spec(open_drain=True)}
    f = _run_fam1(ir, pin_specs=specs)
    assert len(f) == 1 and f[0].corroboration == ["U1.O4"]


def test_fam1_i2c_classified_net_suppressed():
    """An OD pin on an I2C-classified net is suppressed — M3/NO_PULLUP owns it.
    Classification uses the real KB gate, not a net-name heuristic."""
    ir = build([
        C("U1", [("5", "SDA", "SDA_BUS")], mpn="SENSOR_A"),
        C("U2", [("6", "SDA", "SDA_BUS")], mpn="SENSOR_B"),
    ], pintypes={("U1", "5"): "open_collector"})
    kb = {
        ("SENSOR_A", "SDA"): PinFunctionEntry("SENSOR_A", "SDA", [
            PinRole(Peripheral.I2C, None, Signal.I2C_SDA, KBSource.VENDOR_XML)]),
        ("SENSOR_B", "SDA"): PinFunctionEntry("SENSOR_B", "SDA", [
            PinRole(Peripheral.I2C, None, Signal.I2C_SDA, KBSource.VENDOR_XML)]),
    }
    # Without the KB → NOT I2C-classified → WARN (control).
    assert len(_run_fam1(ir)) == 1
    # With the KB → fixed-function I2C → suppressed.
    assert _run_fam1(ir, kb=kb) == []


def test_fam1_i2c_name_hint_alone_does_not_suppress():
    """A net merely NAMED SDA (no KB-confirmed I2C pin) is NOT I2C-classified, so
    the generic-OD WARN still fires — a name heuristic must not gate suppression."""
    ir = build([
        C("U1", [("13", "SDA", "MY_SDA")]),
        C("U2", [("6", "IN", "MY_SDA")]),
    ], pintypes={("U1", "13"): "open_collector"})
    assert len(_run_fam1(ir, kb={})) == 1     # not suppressed


def test_fam1_open_emitter_no_pulldown_warns():
    """Symmetric pull-DOWN case: an open-emitter output with no path to ground."""
    ir = build([
        C("U1", [("13", "E4", "SIG")]),
        C("U2", [("6", "IN", "SIG")]),
    ], pintypes={("U1", "13"): "open_emitter"}, ground_nets=["GND"])
    f = _run_fam1(ir)
    assert len(f) == 1 and f[0].violation == VIOLATION_OD_NO_PULLDOWN


def test_fam1_open_emitter_with_pulldown_silent():
    ir = build([
        C("U1", [("13", "E4", "SIG")]),
        C("U2", [("6", "IN", "SIG")]),
        C("R1", [("1", "1", "SIG"), ("2", "2", "GND")], value="10k"),
    ], pintypes={("U1", "13"): "open_emitter"}, ground_nets=["GND"])
    assert _run_fam1(ir) == []


def test_fam1_od_no_connect_compound_pintype_still_od():
    """'open_collector+no_connect' base type is still OD (but a 1-member net is
    skipped by the actively-used gate — this fixture forces ≥2 to isolate the tag)."""
    ir = build([
        C("U1", [("6", "PG", "SIG")]),
        C("U2", [("1", "IN", "SIG")]),
    ], pintypes={("U1", "6"): "open_collector+no_connect"})
    f = _run_fam1(ir)
    assert len(f) == 1 and f[0].violation == VIOLATION_OD_NO_PULLUP


# ═══ TODO-272 Phase 2a: Family 1 — name-based alternate entry ═════════════════
# Option A: a net with NO open_collector/open_emitter pin can still enter Family
# 1 if classify_net_name resolves an I2C_DATA/I2C_CLOCK role from the net name.
# Recon: investigation/recon_reports/todo272_step08g_od_gate_recon.md (TRNXSDR
# shape: /SMU/I2C1_SDA_A etc., 3-node nets, bidirectional/input/unspecified
# pintypes, zero open_collector/open_emitter anywhere).

def _i2c_name_net(pull=False, members=3, mcu=False, net_name="/X/I2C1_SDA_B",
                  pinfunction_token=True):
    """TRNXSDR-shaped I2C-named net: no OD pin declared anywhere, pintypes cycle
    through bidirectional/input/unspecified -- must enter Family 1 ONLY via the
    name path. Pin-shape MIRRORS the real acruxcz TRNXSDR-carrier export (recon
    investigation/recon_reports/todo272_step08g_od_gate_recon.md, lines
    140-159): net '/SMU/I2C1_SDA_B' really has pins pinfunction="SEL_DFC/SCL_DFC1"
    (token), "PB9" (MCU GPIO, NO token), "SCL" (token) -- i.e. NOT every member
    carries the token, which is exactly what the Phase 2b entry gate (TODO-272)
    now requires >=1 of. When ``pinfunction_token=False`` all members get the
    MCU-GPIO shape (no token anywhere) to build the "gate blocks entry" case."""
    mpns = ["SENSOR_A", ("STM32G070RBTx" if mcu else "SENSOR_B"), "SENSOR_C"]
    pintype_cycle = ["unspecified", "bidirectional", "input"]
    if pinfunction_token:
        # Real TRNXSDR I2C1_SDA_B shape: only 2 of 3 members carry a token; the
        # middle member (PB9-shaped) deliberately does NOT.
        pin_names = ["SEL_DFC/SCL_DFC1", "PB9", "SCL"]
    else:
        pin_names = ["PB6", "PB9", "PB7"]     # no SDA/SCL/I2C token anywhere
    comps, pintypes = [], {}
    for i in range(members):
        rd = f"U{i + 1}"
        pname = pin_names[i % len(pin_names)]
        comps.append(C(rd, [(str(i + 1), pname, net_name)], mpn=mpns[i % len(mpns)]))
        pintypes[(rd, str(i + 1))] = pintype_cycle[i % len(pintype_cycle)]
    if pull:
        comps.append(C("R1", [("1", "1", net_name), ("2", "2", "+3V3")], value="4K7"))
    return build(comps, pintypes=pintypes)


def test_fam1_name_path_no_pullup_warns():
    """(i) TRNXSDR-shaped: 3-node /X/I2C1_SDA_B, bidirectional/input/unspecified
    pintypes, no OD pin anywhere, no pull-up -> exactly one WARN via the name
    path."""
    f = _run_fam1(_i2c_name_net(pull=False))
    assert len(f) == 1
    assert f[0].family == FAMILY_GENERIC_OD
    assert f[0].violation == VIOLATION_I2C_NAME_NO_PULLUP
    assert f[0].activation == ACTIVATION_I2C_NET_NAME
    assert f[0].net == "/X/I2C1_SDA_B"


def test_fam1_name_path_with_pullup_silent():
    """(ii) Same net WITH a pull-up resistor to a rail -> no finding."""
    assert _run_fam1(_i2c_name_net(pull=True)) == []


def test_fam1_name_path_unconnected_veto():
    """(iii) unconnected-(...)-shaped I2C-tokened name -> vetoed by the module's
    own _UNCONNECTED_NET_RE (order-1 gate, checked before the >=2-refdes gate),
    no finding -- even though classify_net_name's D2 unwrap would otherwise
    resolve I2C_DATA from the embedded 'SDA' token (see
    test_unconnected_net_regex_matches_step08g_pullup_presence in
    test_recall_detectability.py, which asserts the regex agreement on this
    exact shape)."""
    ir = build([
        C("U1", [("2", "SDA", "unconnected-(U1-SDA_2-Pad5)")]),
        C("U2", [("6", "IN", "unconnected-(U1-SDA_2-Pad5)")]),
    ])
    assert _run_fam1(ir) == []


def test_fam1_name_path_kb_doubles_suppressed():
    """(iv) KB-evidence-rule fixture: same net shape but WITH KB coverage that
    makes is_i2c_classified_net True -> M3/NO_PULLUP owns it, Family 1 stands
    aside even on the name-based entry path (KB CONDEMNS/CORROBORATES, never
    unilaterally asserts -- here it corroborates M3's ownership claim)."""
    ir = build([
        C("U1", [("5", "SDA", "/X/I2C1_SDA_B")], mpn="SENSOR_A"),
        C("U2", [("6", "SDA", "/X/I2C1_SDA_B")], mpn="SENSOR_B"),
    ])
    kb = {
        ("SENSOR_A", "SDA"): PinFunctionEntry("SENSOR_A", "SDA", [
            PinRole(Peripheral.I2C, None, Signal.I2C_SDA, KBSource.VENDOR_XML)]),
        ("SENSOR_B", "SDA"): PinFunctionEntry("SENSOR_B", "SDA", [
            PinRole(Peripheral.I2C, None, Signal.I2C_SDA, KBSource.VENDOR_XML)]),
    }
    # Without the KB -> name path fires (control).
    assert len(_run_fam1(ir)) == 1
    # With the KB -> fixed-function I2C -> suppressed, M3 owns.
    assert _run_fam1(ir, kb=kb) == []


def test_fam1_name_path_i3c_named_net_not_classified():
    """(v) I3C-named net -> classify_net_name declines (I3C != I2C, card 252 I3C
    discrimination), so the name path never activates -> no finding even with
    no pull-up."""
    ir = build([
        C("U1", [("2", "SDA", "I3C0_SDA")]),
        C("U2", [("6", "IN", "I3C0_SDA")]),
    ])
    assert _run_fam1(ir) == []


def test_fam1_od_path_unchanged_by_widening():
    """(vi) OD-pintyped net (existing trigger) -> finding unchanged vs current
    behavior, activation=od_pintype."""
    f = _run_fam1(_od_net(pull=False))
    assert len(f) == 1
    assert f[0].violation == VIOLATION_OD_NO_PULLUP
    assert f[0].activation == ACTIVATION_OD_PINTYPE


def test_fam1_both_ways_net_handled_once_via_od_path():
    """(vi) A net qualifying BOTH ways (an open_collector pin present AND an
    I2C-shaped net name) is handled once, via the OD path -- exactly one
    finding, activation=od_pintype, not double-counted."""
    ir = build([
        C("U1", [("13", "SDA", "/I2C1_SDA")]),
        C("U2", [("6", "IN", "/I2C1_SDA")]),
    ], pintypes={("U1", "13"): "open_collector"})
    f = _run_fam1(ir)
    assert len(f) == 1
    assert f[0].violation == VIOLATION_OD_NO_PULLUP
    assert f[0].activation == ACTIVATION_OD_PINTYPE


def test_fam1_name_path_mcu_caveat_present():
    """(vii) MCU-family part (STM32) on the net -> caveat clause present in the
    finding's evidence text."""
    f = _run_fam1(_i2c_name_net(pull=False, mcu=True))
    assert len(f) == 1
    assert "internal I2C pull-ups" in f[0].evidence
    assert "verify against the firmware configuration and datasheet" in f[0].evidence


def test_fam1_name_path_no_mcu_caveat_absent():
    """(vii) No MCU-family part on the net -> caveat clause absent."""
    f = _run_fam1(_i2c_name_net(pull=False, mcu=False))
    assert len(f) == 1
    assert "internal I2C pull-ups" not in f[0].evidence


# ═══ TODO-272 Phase 2b: pinfunction-corroboration ENTRY gate ══════════════════
# Nelson's adjudication on the Phase 2a Step-6 measurement (75/139 = 54% of raw
# name-path findings would disappear under a required >=1 non-passive SDA/SCL/
# I2C pinfunction token) promotes that filter into the actual gate: the name
# path now requires classify_net_name AND pinfunction corroboration to ACTIVATE
# at all -- no finding is built when the token requirement is unmet.

def test_fam1_name_path_no_pinfunction_token_no_entry():
    """classify_net_name resolves I2C_DATA from the net name, but NO non-passive
    pin on the net carries an SDA/SCL/I2C pinfunction token (all MCU-GPIO-shaped
    pin names, e.g. PB6/PB9/PB7) -> the name path never activates -> no finding,
    even with no pull-up present."""
    ir = _i2c_name_net(pull=False, pinfunction_token=False)
    assert _run_fam1(ir) == []


def test_fam1_name_path_token_gate_matches_step6_measurement_shape():
    """Direct proof of the Phase 2a Step-6 shape: a net where only SOME
    non-passive members carry the token (the real TRNXSDR PB9-in-the-middle
    case) still enters -- corroboration requires >=1, not ALL."""
    f = _run_fam1(_i2c_name_net(pull=False, pinfunction_token=True))
    assert len(f) == 1
    assert f[0].violation == VIOLATION_I2C_NAME_NO_PULLUP
    # PB9 (no token) must NOT appear in corroboration; SCL/SEL_DFC/SCL_DFC1 must.
    assert not any(c.split(".", 1)[1] == "PB9" for c in f[0].corroboration)
    assert len(f[0].corroboration) == 2


def test_i2c_token_re_matcher_representative_set():
    """Unit test over the promoted `_I2C_TOKEN_RE` matcher (verbatim from the
    Phase 2a projection script's measurement) across a representative token
    set: an SDA-token pin, an SCL-token pin, an I2C-prefixed pinfunction, and a
    non-matching (MCU GPIO) pinfunction that must NOT match."""
    assert _I2C_TOKEN_RE.search("SDA_6")
    assert _I2C_TOKEN_RE.search("SEL_DFC/SCL_DFC1_3")
    assert _I2C_TOKEN_RE.search("I2C3_SCL")
    assert not _I2C_TOKEN_RE.search("PB9_63")


def test_net_i2c_pinfunction_tokens_skips_passive_refdes():
    """Passive filter proof: a resistor whose pin_name happens to contain an
    I2C-shaped token (contrived, but the gate must not read it) is excluded --
    only non-passive refdes contribute tokens."""
    ir = build([
        C("U1", [("1", "PB6", "/X/I2C1_SDA_B")]),
        C("U2", [("2", "PB9", "/X/I2C1_SDA_B")]),
        C("R1", [("1", "SDA_LOOKALIKE", "/X/I2C1_SDA_B"), ("2", "2", "+3V3")], value="4K7"),
    ])
    net = net_of(ir, "/X/I2C1_SDA_B")
    cbr = {c.refdes: c for c in ir.components}
    tokens = _net_i2c_pinfunction_tokens(net, cbr)
    assert tokens == []


# ═══ Step 4: Family 2 — flash-CS ══════════════════════════════════════════════

def _run_fam2(ir):
    cbr = {c.refdes: c for c in ir.components}
    return _family_flash_cs(ir, cbr, _build_comps_on_net(ir))


def test_fam2_flash_cs_no_pullup_warns():
    ir = build([
        C("U1", [("6", "~{CS}", "/QSPI_SS"), ("5", "CLK", "/QSPI_CLK"),
                 ("2", "DI", "/QSPI_DI")], mpn="W25Q128JVS"),
        C("U2", [("50", "QSPI_SS", "/QSPI_SS")], mpn="RP2040"),   # MCU QSPI master
    ])
    f = _run_fam2(ir)
    assert len(f) == 1
    assert f[0].family == FAMILY_FLASH_CS
    assert f[0].violation == VIOLATION_FLASH_CS_NO_PULLUP
    assert f[0].net == "/QSPI_SS"
    assert f[0].corroboration      # net-name /QSPI_SS corroborates


def test_fam2_flash_cs_with_pullup_silent():
    ir = build([
        C("U1", [("6", "~{CS}", "/QSPI_SS")], mpn="W25Q128JVS"),
        C("U2", [("50", "QSPI_SS", "/QSPI_SS")], mpn="RP2040"),
        C("R1", [("1", "1", "/QSPI_SS"), ("2", "2", "+3V3")], value="10k"),
    ])
    assert _run_fam2(ir) == []


def test_fam2_flash_ce_hash_literal_warns():
    """SST26 flash uses 'CE#' as its chip-select literal."""
    ir = build([
        C("U1", [("7", "CE#", "/SPI_CS0")], mpn="SST26VF064B-104I/SM"),
        C("U2", [("3", "CS", "/SPI_CS0")], mpn="SOC"),
    ])
    f = _run_fam2(ir)
    assert len(f) == 1 and f[0].net == "/SPI_CS0"


def test_fam2_non_flash_spi_part_not_identified():
    """A non-flash SPI device with a CS pin (IMU / clock-gen / level-shifter) is
    NOT flash-class (no flash MPN) → Family 2 does not fire."""
    ir = build([
        C("U1", [("6", "AP_CS", "/CS_LINE"), ("5", "SCLK", "/SCK"),
                 ("2", "SDI", "/MOSI")], mpn="ICM-42605"),   # IMU, not flash
        C("U2", [("3", "CS", "/CS_LINE")], mpn="SOC"),
    ])
    assert _run_fam2(ir) == []


# ═══ Step 4: Family 3 — SD CMD/DAT ════════════════════════════════════════════

def _sd_socket(pull=False):
    """microSD socket with CMD + DAT0-3(+CD/CS ambiguous). Optionally pull all."""
    pins = [
        ("3", "CMD/DI", "SD_CMD"),
        ("7", "DAT0/DO", "SD_DAT0"),
        ("8", "DAT1/RES", "SD_DAT1"),
        ("1", "DAT2/RES", "SD_DAT2"),
        ("2", "CD/DAT3/CS", "SD_DAT3"),          # ambiguous → SKIP
        ("5", "CLK", "SD_CLK"),
    ]
    comps = [C("MICRO_SD1", pins, mpn="TFC-WXCP11-08-LF")]
    if pull:
        for i, net in enumerate(["SD_CMD", "SD_DAT0", "SD_DAT1", "SD_DAT2"]):
            comps.append(C(f"R{i}", [("1", "1", net), ("2", "2", "+3V3")], value="47k"))
    return build(comps)


def _run_fam3(ir):
    cbr = {c.refdes: c for c in ir.components}
    return _family_sd(ir, cbr, _build_comps_on_net(ir))


def test_fam3_sd_lines_no_pullup_warn():
    f = _run_fam3(_sd_socket(pull=False))
    nets = {x.net for x in f}
    # CMD + DAT0/1/2 fire; DAT3 (CD/DAT3/CS ambiguous) is skipped.
    assert nets == {"SD_CMD", "SD_DAT0", "SD_DAT1", "SD_DAT2"}
    assert all(x.violation == VIOLATION_SD_NO_PULLUP for x in f)
    assert all(x.family == FAMILY_SD for x in f)


def test_fam3_sd_dat3_ambiguous_skipped():
    f = _run_fam3(_sd_socket(pull=False))
    assert "SD_DAT3" not in {x.net for x in f}


def test_fam3_sd_lines_with_pullup_silent():
    assert _run_fam3(_sd_socket(pull=True)) == []


# ═══ Step 6: unconnected-(...) net guard, families 2/3 (step08g_unconnected_recon) ═
# A KiCad-synthetic `unconnected-(...)` net means the pin isn't wired at all — a
# categorically different (and pre-existing) claim from "wired with no pull-up".
# Family 1 is already immune via its ≥2-distinct-refdes actively-used gate
# (test_fam1_single_member_net_skipped, above); families 2/3 had no equivalent
# gate until this fix.

def test_fam2_flash_cs_dangling_pad_not_warned():
    """A flash CS pin whose pad is genuinely unrouted (its own unconnected-(...)
    singleton, nothing else on the net) must NOT produce a WARN -- the pin isn't
    wired, so there's no "missing pull-up" to report."""
    ir = build([
        C("U9", [("1", "~{CS}", "unconnected-(U9-~{CS}-Pad1)"),
                 ("2", "DO", "/QSPI_DO"), ("7", "CLK", "/QSPI_CLK")],
          mpn="W25Q32JVSSIQ"),
    ])
    assert _run_fam2(ir) == []


def test_fam3_sd_dangling_dat_pins_not_warned_but_wired_cmd_still_fires():
    """Per-pin proof: DAT0/DAT2 sit on their own unconnected-(...) singletons (not
    wired) while CMD is genuinely wired to a real net with no pull-up. The DAT
    WARNs must be suppressed; the CMD WARN must still fire -- proving the guard
    is per-pin, not per-component."""
    ir = build([
        C("MICRO_SD1", [
            ("3", "CMD/DI", "SD_CMD"),
            ("7", "DAT0/DO", "unconnected-(MICRO_SD1-DAT0{slash}DO-Pad7)"),
            ("1", "DAT2/RES", "unconnected-(MICRO_SD1-DAT2{slash}RES-Pad1)"),
        ], mpn="TFC-WXCP11-08-LF"),
    ])
    f = _run_fam3(ir)
    nets = {x.net for x in f}
    assert nets == {"SD_CMD"}
    assert all(x.violation == VIOLATION_SD_NO_PULLUP for x in f)


def test_fam2_flash_cs_multipin_same_refdes_unconnected_net_not_warned():
    """Guard is name-keyed, not pin-count-sensitive: KiCad buckets several
    no-connect pads of the SAME component under one synthetic net name (recon
    §4 -- e.g. unconnected-(U30B-DRAIN1-Pad15) spans 4 real pins of U30). A CS
    pin sharing such a multi-pin (but still single-refdes, still-unrouted) net
    must still be suppressed."""
    ir = build([
        C("U9", [("1", "~{CS}", "unconnected-(U9-~{CS}-Pad1)"),
                 ("9", "HOLD#", "unconnected-(U9-~{CS}-Pad1)"),
                 ("2", "DO", "/QSPI_DO")],
          mpn="W25Q32JVSSIQ"),
    ])
    assert _run_fam2(ir) == []


def test_fam2_flash_cs_single_refdes_named_net_still_warns():
    """Anti-cardinality regression: a flash CS pin alone (no other component) on
    a real NAMED net (not unconnected-(...)) with no pull-up must still WARN --
    single-refdes is a legitimate presence-check candidate; only the synthetic
    unconnected-(...) name marker suppresses, never a bare participant count."""
    ir = build([
        C("U9", [("1", "~{CS}", "/FLASH_CS")], mpn="W25Q32JVSSIQ"),
    ])
    f = _run_fam2(ir)
    assert len(f) == 1 and f[0].net == "/FLASH_CS"


# ═══ Step 5: LED-sink acceptance (real relay-driver shape) ════════════════════

def _relay_board():
    """Mirrors Amit_Amp_V0.1 RELAY_1: U1 OD sink, K1 coil, D6 (cathode-on-net +
    R5 → RELAY_5V, valid), D10 (flyback, anode-on-net, cathode-on-RELAY_5V)."""
    comps = [
        C("U1", [("13", "O4", "RELAY_1"), ("9", "COM", "RELAY_5V")], mpn="ULN2003A"),
        C("K1", [("1", "1", "RELAY_5V"), ("2", "2", "RELAY_1")]),        # coil
        C("D6", [("1", "K", "RELAY_1"), ("2", "A", "Net-D6-A")]),         # valid pull leg
        C("D10", [("2", "A", "RELAY_1"), ("1", "K", "RELAY_5V")]),        # flyback (reversed)
        C("R5", [("1", "1", "Net-D6-A"), ("2", "2", "RELAY_5V")], value="2K2"),
    ]
    return build(comps, pintypes={("U1", "13"): "open_collector"})


def test_led_sink_topology_accepted_no_warn():
    """The correctly-oriented LED-sink pull path (D6+R5) is ACCEPTED; the reversed
    flyback D10 on the SAME net is correctly NOT counted — no false WARN."""
    ir = _relay_board()
    assert _walk(ir, "RELAY_1", Direction.UP) is True
    assert _run_fam1(ir) == []               # Family 1 does not warn on this board


def test_led_sink_without_valid_leg_warns():
    """Remove the valid D6+R5 leg, leaving only the reversed flyback D10 → the
    generic-OD WARN correctly fires (the flyback is not a pull path)."""
    comps = [
        C("U1", [("13", "O4", "RELAY_1"), ("9", "COM", "RELAY_5V")], mpn="ULN2003A"),
        C("K1", [("1", "1", "RELAY_5V"), ("2", "2", "RELAY_1")]),
        C("D10", [("2", "A", "RELAY_1"), ("1", "K", "RELAY_5V")]),
    ]
    ir = build(comps, pintypes={("U1", "13"): "open_collector"})
    f = _run_fam1(ir)
    assert len(f) == 1 and f[0].net == "RELAY_1"


# ═══ Integration: full check_pullup_presence entry ════════════════════════════

def test_check_entry_combines_families_and_is_warn_only():
    ir = build([
        C("U1", [("13", "O4", "OD_SIG")]),
        C("U2", [("6", "IN", "OD_SIG")]),
        C("U3", [("6", "~{CS}", "/QSPI_SS")], mpn="W25Q128JVS"),
        C("U4", [("50", "QSPI_SS", "/QSPI_SS")], mpn="RP2040"),
    ], pintypes={("U1", "13"): "open_collector"})
    findings = check_pullup_presence(ir, {}, {}, None)
    fams = sorted({f.family for f in findings})
    assert fams == [FAMILY_FLASH_CS, FAMILY_GENERIC_OD]
    assert all(f.severity == "WARN" for f in findings)


def test_check_entry_clean_board_no_findings():
    ir = build([
        C("U1", [("1", "OUT", "SIG")], mpn="LOGIC"),
        C("U2", [("1", "IN", "SIG")], mpn="LOGIC"),
    ], pintypes={("U1", "1"): "output"})
    assert check_pullup_presence(ir, {}, {}, None) == []

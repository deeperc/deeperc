"""TODO-415 — curated per-pin supply map (steps/pin_supply_map.py) and its three
uses in step_08_checker: the driver's voltage, the no-voltage reason and the
highest-rail driver ranking. Synthetic components only; no corpus, no caches."""
import json
import os
import sys
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import provenance  # noqa: E402
from steps import pin_supply_map as psm  # noqa: E402
from steps.step_02_parser import ComponentIR, NetIR, NetlistIR, PinIR  # noqa: E402
from steps.step_05_validator import ValidatedPinSpec  # noqa: E402
from steps.step_08_checker import (  # noqa: E402
    PowerDomainResolution,
    RailProvenance,
    SignalUnresolvableReason as R,
    _RESOLUTION_CONF,
    _rank_voltage,
    _resolve_power_domain,
    _unresolved_driver_voltage_reason,
    run,
)
from steps.step_10_report import build_report  # noqa: E402

CV = {"P3V3": 3.3, "P1V8": 1.8, "P5V": 5.0, "P2V5": 2.5, "X": None}

RULED_IDS = {
    "sn74lxch8t245.A", "sn74lxch8t245.B", "sn74lxch8t245.DIR", "sn74lxch8t245.OE",
    "sn74axc8t245.A", "sn74axc8t245.B", "sn74axc8t245.DIR", "sn74axc8t245.OE",
    "sn74lvc1t45.A", "sn74lvc1t45.B", "sn74lvc1t45.DIR",
    "txb0108.A", "txb0108.B", "txb0108.OE",
    "nts0104.A", "nts0104.B", "nts0104.OE",
    "rp2040.GPIO", "rp2040.GPIO_ADC", "rp2040.QSPI", "rp2040.SWD", "rp2040.RUN",
    "sn74lvch16t245.A", "sn74lvch16t245.B", "sn74lvch16t245.DIR", "sn74lvch16t245.OE",
    "xczu.BANK",
}


def comp(pn, pins):
    """pins: (pin_id, pin_name, net)."""
    return NS(part_number=pn, refdes="U1",
              pins=[NS(pin_id=i, pin_name=n, net=net) for i, n, net in pins])


def volt(c, name, cv=CV):
    m = psm.pin_supply(c, name, cv)
    return m.voltage if m else "SILENT"


# ── the table ─────────────────────────────────────────────────────────────────

def test_rule_ids_are_exactly_the_ruled_set():
    assert psm.all_rule_ids() == RULED_IDS
    assert sum(len(f.rules) for f in psm.FAMILIES) == len(RULED_IDS)   # no duplicate id


def test_every_rule_carries_document_page_and_quote():
    for fam in psm.FAMILIES:
        assert fam.document.strip() and fam.kind in ("named", "bank")
        for rule in fam.rules:
            assert rule.page.strip() and rule.quote.strip(), (fam.prefix, rule.rule_id)
            assert (rule.pattern is None) == (fam.kind == "bank")
            # the citation sits inside a bracketed note in the evidence label
            assert not set("()") & set(fam.document + rule.page), (fam.prefix, rule.rule_id)


def test_no_family_prefix_shadows_another():
    ps = [f.prefix for f in psm.FAMILIES]
    assert ps == [p.casefold() for p in ps]
    assert not [(a, b) for a in ps for b in ps if a != b and b.startswith(a)]


@pytest.mark.parametrize("pn, prefix", [
    ("SN74LXCH8T245PWR", "sn74lxch8t245"), ("SN74AXC8T245RJWR", "sn74axc8t245"),
    ("SN74LVC1T45DCKT", "sn74lvc1t45"), ("TXB0108DQSR", "txb0108"),
    ("NTS0104BQ", "nts0104"), ("RP2040", "rp2040"), (" rp2040 ", "rp2040"),
    ("SN74LVCH16T245DGG", "sn74lvch16t245"), ("XCZU4CG-1SFVC784E", "xczu"),
])
def test_family_for_ruled_parts(pn, prefix):
    assert psm.family_for(pn).prefix == prefix


@pytest.mark.parametrize("pn", [
    "USB7252CT-I/KDX", "TPS65988DHRSHR", "HD3SS3220IRNHR", "SI5340B-D-GM",
    "ESP32-S3-MINI-1-N8", "LMV358IDGKR", "74LVC1T45", "", None,
])
def test_family_for_parts_with_no_rule(pn):
    assert psm.family_for(pn) is None


def test_norm_and_loose():
    assert psm.norm("~{OE}") == "oe" and psm.norm("GPIO26/ADC0") == "gpio26_adc0"
    assert psm.norm("QSPI-SS-N") == "qspi_ss_n" and psm.norm("GPIO1_0") == "gpio1_0"
    assert psm.norm(None) == "" and psm.loose(None) == ""
    assert psm.loose("VCC(A)") == psm.loose("VCC_A") == psm.loose("vcca") == "vcca"


# ── pin matching per family ───────────────────────────────────────────────────

def test_rp2040_pin_names():
    names = ("GPIO1_0", "GPIO26/ADC0", "GPIO26_ADC0", "GPIO29", "GPIO29_ADC3", "GPIO30",
             "GPIO5_ADC0", "GPIO26_ADC1", "QSPI-SS-N", "QSPI_SD0", "QSPI_FOO", "USB_DP",
             "GPIO7", "GPIO25", "SWD", "SWDIO", "SWCLK", "RUN", "XIN", "TESTEN")
    rp = comp("RP2040", [("49", "IOVDD", "P3V3"), ("50", "DVDD", "P1V8")]
              + [(str(i + 1), n, "n") for i, n in enumerate(names)])
    want = {"GPIO1_0": "SILENT", "GPIO26/ADC0": 3.3, "GPIO26_ADC0": 3.3, "GPIO29": 3.3,
            "GPIO29_ADC3": 3.3, "GPIO30": "SILENT", "GPIO5_ADC0": "SILENT",
            "GPIO26_ADC1": "SILENT", "QSPI-SS-N": 3.3, "QSPI_SD0": 3.3,
            "QSPI_FOO": "SILENT", "USB_DP": "SILENT", "GPIO7": 3.3, "GPIO25": 3.3,
            "SWD": 3.3, "SWDIO": 3.3, "SWCLK": 3.3, "RUN": 3.3, "XIN": "SILENT",
            "TESTEN": "SILENT"}
    assert {n: volt(rp, n) for n in names} == want
    assert psm.pin_supply(rp, "GPIO29", CV).rule == "rp2040.GPIO_ADC"
    assert psm.pin_supply(rp, "GPIO7", CV).rule == "rp2040.GPIO"


def test_rule_applies_only_when_the_supply_pin_is_on_the_component():
    # A module sold under the chip's name has no IOVDD pin: the map is silent.
    stamp = comp("RP2040_Stamp", [("2", "GPIO0", "n"), ("3", "3V3", "P3V3")])
    assert psm.family_for("RP2040_Stamp").prefix == "rp2040"
    assert psm.rule_for(stamp, "GPIO0") is not None
    assert psm.pin_supply(stamp, "GPIO0", CV) is None


def test_axc8t245_enable_and_direction_spellings():
    ax = comp("SN74AXC8T245RJWR", [("1", "VCCA", "P1V8"), ("2", "VCCB", "P3V3")] + [
        (str(i + 3), n, "n") for i, n in enumerate(
            ("OE_N", "~{OE}", "OEN", "NOE", "OE_B", "DIR1", "DIR2", "DIR", "A1", "B8",
             "B9", "OE2", "A0"))])
    want = {"OE_N": 1.8, "~{OE}": 1.8, "OEN": 1.8, "NOE": 1.8, "OE_B": 1.8, "DIR1": 1.8,
            "DIR2": 1.8, "DIR": 1.8, "A1": 1.8, "B8": 3.3, "B9": "SILENT",
            "OE2": "SILENT", "A0": "SILENT"}
    assert {n: volt(ax, n) for n in want} == want


def test_lxch8t245_and_lvc1t45_and_lvch16t245():
    lx = comp("SN74LXCH8T245PWR", [("1", "VCCA", "P1V8"), ("24", "VCCB", "P3V3"),
                                   ("23", "VCCB", "P3V3"), ("2", "DIR", "n"),
                                   ("3", "A1", "n"), ("21", "B1", "n"), ("22", "~{OE}", "n"),
                                   ("9", "DIR1", "n")])
    assert [volt(lx, n) for n in ("A1", "B1", "DIR", "~{OE}", "DIR1")] == [
        1.8, 3.3, 1.8, 1.8, "SILENT"]
    assert psm.pin_supply(lx, "B1", CV).n_supply_pins == 2
    lv = comp("SN74LVC1T45DCKT", [("1", "VCCA", "P3V3"), ("6", "VCCB", "P1V8"),
                                  ("3", "A", "n"), ("4", "B", "n"), ("5", "DIR", "n")])
    assert [volt(lv, n) for n in ("A", "B", "DIR", "A1")] == [3.3, 1.8, 3.3, "SILENT"]
    l16 = comp("SN74LVCH16T245DGG", [("7", "VCCA", "P1V8"), ("18", "VCCB", "P3V3"),
                                     ("3", "2A1", "n"), ("4", "~{1OE}", "n"),
                                     ("5", "1DIR", "n"), ("6", "2B8", "n"), ("8", "A1", "n"),
                                     ("9", "3A1", "n")])
    assert [volt(l16, n) for n in ("2A1", "~{1OE}", "1DIR", "2B8", "A1", "3A1")] == [
        1.8, 1.8, 1.8, 3.3, "SILENT", "SILENT"]


def test_nts0104_supply_pin_spellings():
    nt = comp("NTS0104BQ", [("1", "VCC_A", "P1V8"), ("14", "VCC(B)", "P3V3"),
                            ("2", "A1", "n"), ("13", "B1", "n"), ("8", "OE", "n"),
                            ("5", "A5", "n")])
    assert [volt(nt, n) for n in ("A1", "B1", "OE", "A5")] == [1.8, 3.3, 1.8, "SILENT"]
    assert psm.pin_supply(nt, "B1", CV).supply == "VCCB"


def test_mapped_pin_never_takes_another_supplys_voltage():
    tx = comp("TXB0108DQSR", [("1", "VCCA", "X"), ("2", "A1", "n"), ("3", "VCCB", "P3V3"),
                              ("4", "B1", "n")])
    m = psm.pin_supply(tx, "A1", CV)
    assert (m.state, m.voltage, m.net, m.n_supply_pins, m.n_confirmed) == (
        "SUPPLY_UNCONFIRMED", None, None, 1, 0)
    assert volt(tx, "B1") == 3.3
    off = comp("TXB0108DQSR", [("1", "VCCA", "FLOATING_NET"), ("2", "A1", "n")])
    assert psm.pin_supply(off, "A1", CV).state == "SUPPLY_UNCONFIRMED"
    mixed = comp("TXB0108DQSR", [("1", "VCCA", "P3V3"), ("9", "VCC_A", "P1V8"),
                                 ("2", "A1", "n")])
    m = psm.pin_supply(mixed, "A1", CV)
    assert (m.state, m.voltage, m.voltages) == ("SUPPLY_MIXED", None, (1.8, 3.3))


def test_pin_supply_fields_for_a_mapped_pin():
    tx = comp("TXB0108DQSR", [("1", "VCCA", "P1V8"), ("2", "A1", "n")])
    m = psm.pin_supply(tx, "A1", CV)
    assert m == psm.PinSupply(
        family="txb0108", rule="txb0108.A", supply="VCCA", net="P1V8", voltage=1.8,
        state="MAPPED", voltages=(1.8,), citation="TI TXB0108 datasheet SCES643L p. 4",
        n_supply_pins=1, n_confirmed=1)
    assert psm.pin_supply(None, "A1", CV) is None
    assert psm.pin_supply(comp("LM358", [("1", "A1", "n")]), "A1", CV) is None


# ── the XCZU bank rule ────────────────────────────────────────────────────────

def _fpga():
    return comp("XCZU4CG-1SFVC784E", [
        ("Y13", "IO_L1P_AD15P_44_Y13", "n"), ("AA14", "VCCO_44_AA14", "P3V3"),
        ("AB1", "VCCO_44_AB1", "P3V3"), ("AG5", "IO_L10N_T1U_N7_QBC_AD4N_64_AG5", "n"),
        ("H16", "PS_MIO31_H16", "n"), ("P16", "PS_POR_B_P16", "n"),
        ("Z1", "IO_L2P_66_Q9", "n"), ("K1", "IO_L3P_T0_65", "n"), ("K2", "VCCO_65", "P1V8"),
        ("K4", "VCCO_PSIO0_500_K4", "P1V8"), ("K5", "IO_500_K5", "n"),
        ("K6", "VCCO_644_K6", "P1V8"),
        # bank 43: three VCCO pins, one on a net with no confirmed voltage
        ("A1", "IO_L5P_HDGC_43_A1", "n"), ("B1", "VCCO_43_B1", "P3V3"),
        ("B2", "VCCO_43_B2", "P3V3"), ("B3", "VCCO_43_B3", "X"),
        # bank 45: a VCCO-like name whose last segment is NOT that pin's own ball
        ("C1", "IO_L6P_HDGC_45_C1", "n"), ("C2", "VCCO_45_ZZ9", "P3V3"),
        # bank 46: its two VCCO pins disagree
        ("D1", "IO_L7P_HDGC_46_D1", "n"), ("D2", "VCCO_46_D2", "P3V3"),
        ("D3", "VCCO_46_D3", "P1V8"),
    ])


def test_xczu_bank_of():
    fp = _fpga()
    want = {"IO_L1P_AD15P_44_Y13": "44", "IO_L10N_T1U_N7_QBC_AD4N_64_AG5": "64",
            "PS_MIO31_H16": None, "PS_POR_B_P16": None,
            "IO_L2P_66_Q9": None,        # last segment is not this pin's ball
            "IO_L3P_T0_65": "65",        # vendor form, no ball appended
            "IO_500_K5": None,           # fewer than three segments once the ball is dropped
            "VCCO_44_AA14": None, None: None, "": None}
    assert {n: psm.bank_of(fp, n) for n in want} == want


def test_xczu_bank_rule_voltages():
    fp = _fpga()
    want = {"IO_L1P_AD15P_44_Y13": 3.3,                  # two VCCO_44 pins
            "IO_L10N_T1U_N7_QBC_AD4N_64_AG5": "SILENT",  # no VCCO_64 pin (644 is not 64)
            "PS_MIO31_H16": "SILENT", "PS_POR_B_P16": "SILENT",   # PS pins: no rule
            "IO_L2P_66_Q9": "SILENT", "IO_L3P_T0_65": 1.8, "IO_500_K5": "SILENT",
            "IO_L5P_HDGC_43_A1": 3.3,
            "IO_L6P_HDGC_45_C1": "SILENT",               # VCCO_45_ZZ9 is not a VCCO_45 pin
            "IO_L7P_HDGC_46_D1": None}                   # mixed: no voltage
    assert {n: volt(fp, n) for n in want} == want
    m = psm.pin_supply(fp, "IO_L1P_AD15P_44_Y13", CV)
    assert (m.rule, m.supply, m.n_supply_pins, m.n_confirmed) == ("xczu.BANK", "VCCO_44", 2, 2)
    assert m.citation == "AMD UG1075 v1.13 Zynq UltraScale+ Packaging and Pinouts Table 1-4"
    partial = psm.pin_supply(fp, "IO_L5P_HDGC_43_A1", CV)
    assert (partial.state, partial.n_supply_pins, partial.n_confirmed) == ("MAPPED", 3, 2)
    assert psm.pin_supply(fp, "IO_L7P_HDGC_46_D1", CV).state == "SUPPLY_MIXED"
    assert [p.pin_id for p in psm.supply_pins(fp, "VCCO_44", "44")] == ["AA14", "AB1"]
    assert psm.supply_pins(fp, "VCCO_45", "45") == []
    assert psm.supply_pins(fp, "VCCO_64", "64") == []


# ── step_08_checker ───────────────────────────────────────────────────────────

def _ir(*comps):
    """comps: (refdes, part_number, [(pin_id, pin_name, net)])."""
    components, by_net = [], {}
    for refdes, pn, pins in comps:
        components.append(ComponentIR(
            refdes=refdes, part_number=pn, value=pn,
            pins=[PinIR(pin_id=i, pin_name=n, net=net) for i, n, net in pins]))
        for i, _n, net in pins:
            by_net.setdefault(net, []).append((refdes, i))
    nets = [NetIR(name=n, pins=p) for n, p in by_net.items()]
    return NetlistIR(source_file="test", components=components, nets=nets,
                     power_nets=[n for n in by_net if n in CV], ground_nets=[])


def _shifter(refdes="U1", vcca="P1V8", vccb="P3V3", a_net="SIG_A", b_net="SIG_B"):
    return (refdes, "SN74LXCH8T245PWR",
            [("1", "VCCA", vcca), ("24", "VCCB", vccb), ("3", "A1", a_net),
             ("21", "B1", b_net)])


def _ic(refdes, net, rail="P3V3", pin="IO"):
    return (refdes, f"PN_{refdes}", [("1", pin, net), ("2", "VCC", rail)])


def _spec(pin_name="IO", pin_type="bidirectional", exact=True, abs_max=None, VIH_min=None):
    return ValidatedPinSpec(
        part_number="X", pin_name=pin_name, pin_id="1", VIH_max=None, VIH_min=VIH_min,
        absolute_max_voltage=abs_max, pin_type=pin_type, source_snippet="snippet",
        signal_score=75, confidence="high", is_verified=True, pin_type_exact=exact,
        open_drain=False, expr_notes=[])


CONFIRMED = {"P3V3": 3.3, "P1V8": 1.8, "P5V": 5.0, "P2V5": 2.5}
PROV = {n: RailProvenance("deterministic", "high") for n in CONFIRMED}
LXCH_A = "TI SN74LXCH8T245 datasheet SCES917B p. 1"
LXCH_B = "TI SN74LXCH8T245 datasheet SCES917B p. 3, Table 5-1"


def test_resolve_power_domain_uses_the_pins_own_supply():
    ir = _ir(_shifter())
    assert _resolve_power_domain("U1", ir, CONFIRMED, {}) is None        # part level: 2 voltages
    a = _resolve_power_domain("U1", ir, CONFIRMED, {}, pin_name="A1")
    assert a == PowerDomainResolution(
        voltage=1.8, net="P1V8", resolution="pin_supply_map",
        detail=f"driver pin A1 runs from VCCA per the supply map: {LXCH_A}")
    b = _resolve_power_domain("U1", ir, CONFIRMED, {}, pin_name="B1")
    assert (b.voltage, b.net, b.resolution) == (3.3, "P3V3", "pin_supply_map")
    # a pin with no rule keeps the part-level answer
    assert _resolve_power_domain("U1", ir, CONFIRMED, {}, pin_name="NC") is None
    assert _RESOLUTION_CONF["pin_supply_map"] == "high"
    assert "per_pin_resolved" not in _RESOLUTION_CONF


def test_resolve_power_domain_unmapped_part_is_unchanged_by_pin_name():
    ir = _ir(_ic("U2", "SIG"))
    want = PowerDomainResolution(voltage=3.3, net="P3V3", resolution="single_vcc")
    assert _resolve_power_domain("U2", ir, CONFIRMED, {}) == want
    assert _resolve_power_domain("U2", ir, CONFIRMED, {}, pin_name="IO") == want
    assert want.detail is None


def test_mapped_supply_unconfirmed_gives_no_voltage_and_names_the_supply():
    ir = _ir(_shifter(vcca="NOT_A_RAIL"))
    # VCCB is confirmed at 3.3 V; pin A1 must not take it.
    assert _resolve_power_domain("U1", ir, CONFIRMED, {}, pin_name="A1") is None
    assert _unresolved_driver_voltage_reason("U1", ir, CONFIRMED, pin_name="A1") == (
        R.DRIVER_NO_CONFIRMED_RAIL,
        "Driver voltage unknown — U1 pin A1 runs from VCCA (supply map), which is on "
        "no confirmed rail")
    # without a pin name: the part-level reason, as before
    _code, text = _unresolved_driver_voltage_reason("U1", ir, CONFIRMED)
    assert "supply map" not in text and "VCCA" not in text


def test_mapped_supply_on_two_voltages_is_ambiguous_with_the_supply_named():
    ir = _ir(("U1", "TXB0108DQSR", [("1", "VCCA", "P3V3"), ("9", "VCC_A", "P1V8"),
                                    ("2", "A1", "SIG")]))
    assert _resolve_power_domain("U1", ir, CONFIRMED, {}, pin_name="A1") is None
    assert _unresolved_driver_voltage_reason("U1", ir, CONFIRMED, pin_name="A1") == (
        R.DRIVER_DOMAIN_AMBIGUOUS,
        "Driver voltage unknown — U1 pin A1 runs from VCCA (supply map), whose pins "
        "are on rails of more than one voltage: [1.8, 3.3]")


def test_unmapped_ambiguous_part_keeps_its_reason_text():
    ir = _ir(("U14", "PN_X", [("1", "VCCA", "P5V"), ("2", "VCCB", "P3V3"),
                              ("3", "OUT", "SIG")]))
    assert _unresolved_driver_voltage_reason("U14", ir, CONFIRMED, pin_name="OUT") == (
        R.DRIVER_DOMAIN_AMBIGUOUS,
        "Driver voltage unknown — U14 power domain ambiguous "
        "(2 rail pin(s), 2 distinct voltage(s): [3.3, 5.0])")


def test_rank_voltage():
    ir = _ir(_shifter(), _ic("U2", "SIG_A"), _shifter("U3", vcca="NOT_A_RAIL"))
    assert _rank_voltage("U1", "A1", ir, CONFIRMED) == 1.8      # its own side
    assert _rank_voltage("U1", "B1", ir, CONFIRMED) == 3.3
    assert _rank_voltage("U1", "NC", ir, CONFIRMED) == 3.3      # no rule: highest rail
    assert _rank_voltage("U2", "IO", ir, CONFIRMED) == 3.3
    assert _rank_voltage("U3", "A1", ir, CONFIRMED) is None     # mapped, supply unconfirmed
    assert _rank_voltage("U_GHOST", "A1", ir, CONFIRMED) is None


def test_run_mapped_driver_passes_at_its_own_side_voltage():
    # The 1.8 V side of a shifter into a 1.8 V part. Before TODO-415 the shifter
    # had two rails and the row was DRIVER_DOMAIN_AMBIGUOUS.
    ir = _ir(_shifter(), _ic("U2", "SIG_A", rail="P1V8"))
    specs = {("U2", "IO"): _spec(pin_type="digital_input", abs_max=2.5, VIH_min=1.2)}
    rows = [r for r in run(ir, specs, CONFIRMED, PROV) if r.net_name == "SIG_A"]
    assert len(rows) == 1
    r = rows[0]
    assert (r.status, r.driver_refdes, r.driver_pin_name, r.driver_voltage) == (
        "PASS", "U1", "A1", 1.8)
    assert r.driver_selection == "highest_rail" and r.unresolvable_reason is None
    assert r.driver_confidence == "high" and r.driver_voltage_source == "deterministic"
    assert r.evidence_label == (
        "driver 1.8V within receiver limits "
        f"(driver pin A1 runs from VCCA per the supply map: {LXCH_A})")


def test_run_ranking_uses_the_mapped_voltage():
    # Shifter pin A1 runs from 1.8 V; the other endpoint is a 2.5 V part. The
    # part-level ranking put the shifter first (its highest rail is 3.3 V); the
    # per-pin ranking makes the 2.5 V part the driver.
    ir = _ir(_shifter(), _ic("U2", "SIG_A", rail="P2V5"))
    specs = {("U1", "A1"): _spec("A1", abs_max=4.6), ("U2", "IO"): _spec(abs_max=3.6)}
    rows = [r for r in run(ir, specs, CONFIRMED, PROV) if r.net_name == "SIG_A"]
    assert [(r.driver_refdes, r.receiver_refdes, r.status, r.driver_voltage)
            for r in rows] == [("U2", "U1", "PASS", 2.5)]
    assert rows[0].evidence_label == "driver 2.5V within receiver limits"
    # and on the 3.3 V side the shifter still outranks a 2.5 V part
    ir = _ir(_shifter(), _ic("U2", "SIG_B", rail="P2V5"))
    rows = [r for r in run(ir, {("U2", "IO"): _spec(abs_max=3.6)}, CONFIRMED, PROV)
            if r.net_name == "SIG_B"]
    assert [(r.driver_refdes, r.driver_pin_name, r.driver_voltage, r.status)
            for r in rows] == [("U1", "B1", 3.3, "PASS")]


def test_run_mapped_overvoltage_is_downgraded_not_failed():
    # A highest-rail driver's FAIL or WARN is downgraded (driver-type guard), so on
    # a highest-rail pick a map rule adds a PASS, not a FAIL. The label still names
    # the supply. (A mapped pin typed as a push-pull output by its own extracted
    # group is a priority-1 driver and can FAIL at its mapped voltage.)
    ir = _ir(_shifter(vccb="P5V"), _ic("U2", "SIG_B", rail="P3V3"))
    rows = [r for r in run(ir, {("U2", "IO"): _spec(abs_max=3.6)}, CONFIRMED, PROV)
            if r.net_name == "SIG_B"]
    assert len(rows) == 1
    r = rows[0]
    assert (r.status, r.unresolvable_reason, r.driver_refdes, r.driver_voltage) == (
        "UNRESOLVABLE", R.DRIVER_NOT_PUSH_PULL, "U1", 5.0)
    assert f"runs from VCCB per the supply map: {LXCH_B}" in r.evidence_label


def test_run_mapped_supply_unconfirmed_row():
    # U2's pin is typed as an input by its own group, so the shifter is the only
    # driver candidate; its VCCA is on no confirmed rail.
    ir = _ir(_shifter(vcca="NOT_A_RAIL"), _ic("U2", "SIG_A", rail="P1V8"))
    specs = {("U2", "IO"): _spec(pin_type="digital_input", abs_max=2.5)}
    rows = [r for r in run(ir, specs, CONFIRMED, PROV) if r.net_name == "SIG_A"]
    assert len(rows) == 1
    r = rows[0]
    assert (r.status, r.unresolvable_reason, r.driver_refdes, r.driver_voltage) == (
        "UNRESOLVABLE", R.DRIVER_NO_CONFIRMED_RAIL, "U1", None)
    assert r.unresolvable_detail == (
        "Driver voltage unknown — U1 pin A1 runs from VCCA (supply map), which is on "
        "no confirmed rail")
    assert r.driver_selection == "first_pin"


def test_run_part_without_a_rule_is_untouched():
    ir = _ir(("U1", "PN_SHIFT", [("1", "VCCA", "P1V8"), ("24", "VCCB", "P3V3"),
                                 ("3", "A1", "SIG_A")]),
             _ic("U2", "SIG_A", rail="P1V8"))
    specs = {("U2", "IO"): _spec(pin_type="digital_input", abs_max=2.5)}
    rows = [r for r in run(ir, specs, CONFIRMED, PROV) if r.net_name == "SIG_A"]
    assert [(r.status, r.unresolvable_reason) for r in rows] == [
        ("UNRESOLVABLE", R.DRIVER_DOMAIN_AMBIGUOUS)]
    assert "power domain ambiguous" in rows[0].unresolvable_detail


def test_report_row_has_no_new_key(tmp_path):
    ir = _ir(_shifter(), _ic("U2", "SIG_A", rail="P1V8"), _ic("U3", "SIG_C"),
             _ic("U4", "SIG_C"))
    specs = {("U2", "IO"): _spec(pin_type="digital_input", abs_max=2.5),
             ("U4", "IO"): _spec(pin_type="digital_input", abs_max=3.6)}
    results = run(ir, specs, CONFIRMED, PROV)
    rep = build_report(source_netlist="", results=results, confirmed_voltages=CONFIRMED,
                       extraction_metadata={}, output_path=str(tmp_path / "r.json"),
                       components=ir.components)
    rows = json.loads((tmp_path / "r.json").read_text())["results"]
    assert rows == rep["results"]
    mapped = next(e for e in rows if e["net"] == "SIG_A")
    plain = next(e for e in rows if e["net"] == "SIG_C")
    assert mapped["status"] == plain["status"] == "PASS"
    assert list(mapped) == list(plain) and list(mapped["driver"]) == list(plain["driver"])
    assert mapped["driver"]["voltage_v"] == 1.8 and mapped["driver"]["confidence"] == "high"
    assert mapped["evidence_label"].startswith(
        "driver 1.8V within receiver limits (driver pin A1 runs from VCCA per the "
        "supply map: ")
    assert "supply map" not in plain["evidence_label"]


# ── era membership and import direction ───────────────────────────────────────

def test_module_is_an_era_member_and_imports_no_checker():
    assert "steps/pin_supply_map.py" in provenance.CORE_PIPELINE_FILES
    assert "steps/pin_supply_map.py" in provenance.CHECKER_SOURCE_FILES
    src = open(psm.__file__, encoding="utf-8").read()
    imports = [ln for ln in src.splitlines() if ln.startswith(("import ", "from "))]
    assert imports == ["from __future__ import annotations", "import re",
                       "from typing import NamedTuple"]

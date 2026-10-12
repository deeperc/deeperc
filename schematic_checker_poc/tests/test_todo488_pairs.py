"""TODO-488 — NUMBER|NAME example_pins: the G2'' name-first matcher (steps/pin_match.py),
the libparts symbol-name reader and its step_02 plumbing, and the pair-aware
extraction validator. Synthetic fixtures only.

The legacy parity table's expected outcomes were produced by the pre-TODO-488
pin_match (ccf173d) on the same inputs and are frozen here as literals.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from steps import extraction_validator as V  # noqa: E402
from steps.pin_match import (  # noqa: E402
    alternates, listing_supply_groups, match_groups, pin_match_meta, pin_names,
    read_libpart_pins, resolve_pin_spec_from_groups, split_entry,
)
from steps.step_02_parser import parse_netlist, parse_sexp, tokenize  # noqa: E402


def _g(name, ex, pt="digital_input", **kw):
    d = {"group_name": name, "pin_type": pt, "example_pins": ex}
    d.update(kw)
    return d


def _names(hits):
    return [h["group_name"] for h in hits]


# ── entry split / names(pin) / alternates ────────────────────────────────────

@pytest.mark.parametrize("entry,expected", [
    ("12|PH0", ("pair", "12", "PH0")),
    (" 12 | PH0 ", ("pair", "12", "PH0")),
    ("A3|VDD|X", ("pair", "A3", "VDD|X")),          # first "|" only
    ("?|NC", ("name", "", "NC")),
    ("PA0", ("bare", "", "PA0")),
    ("5|", ("bare", "", "5|")),                      # an empty half is not a pair
    ("|VCC", ("bare", "", "|VCC")),
])
def test_split_entry(entry, expected):
    assert split_entry(entry) == expected


def test_pin_names_excludes_empty_and_pin_id():
    assert pin_names("4", "4", "") == []
    assert pin_names("4", "", "~") == ["~"]           # the parser maps "~" to "" before this
    assert pin_names("10", "10", "GPIO25/DAC_1") == ["GPIO25/DAC_1"]
    assert pin_names("1", "K", "K") == ["K"]


def test_alternates_strip_one_trailing_paren_group():
    assert alternates("PH0(RXD2)") == {"PH0(RXD2)", "PH0"}
    assert alternates("G/D") == {"G", "D"}
    assert alternates("(ADC7)") == {"(ADC7)"}


# ── the rescore2 unit cases (pair_match3_unit.txt), under the K3 staging ─────

def test_u1_paren_suffixed_board_name_binds_by_name():
    # atmega2560 board pin 12 "PH0(RXD2)" + "12|PH0": S3 (one trailing "(...)" stripped)
    gs = [_g("PORTH", ["12|PH0"])]
    assert match_groups("12", "PH0(RXD2)", gs) == ("slash", gs)


def test_u2_name_wins_over_a_shifted_number():
    # atmega2560 pin 35 "PL0(ICP4)": "35|PL2" (a) + "33|PL0" (b) -> b, by name
    a, b = _g("a", ["35|PL2"]), _g("b", ["33|PL0"])
    assert match_groups("35", "PL0(ICP4)", [a, b]) == ("slash", [b])


def test_u3_named_pin_with_no_name_match_is_num_conflict():
    # 1n4148 symbol/board pin 1 "K" + "1|Anode"
    gs = [_g("diode", ["1|Anode"])]
    assert match_groups("1", "K", gs, symbol_name="K") == ("num_conflict", [])


def test_u4_symbol_name_makes_a_numbered_board_pin_named():
    # esp32 board pin "10" (no pinfunction), libparts "GPIO25/DAC_1/..." + "10|IO25"
    gs = [_g("gpio", ["10|IO25"])]
    sym = "GPIO25/DAC_1/ADC2_CH8/RTC_GPIO6/EMAC_RXD0"
    assert match_groups("10", "10", gs, symbol_name=sym) == ("num_conflict", [])
    assert match_groups("10", "10", gs) == ("number", gs)      # without the symbol name: nameless


def test_u5_nameless_pin_binds_by_number():
    # tpd4e05u06 nameless pin 4 + "4|D2+"
    gs = [_g("io", ["4|D2+"])]
    assert match_groups("4", "4", gs, symbol_name="") == ("number", gs)


def test_u6_pair_name_binds_despite_number_mismatch():
    # fdn340p "2|G" -> pin 1 named G
    gs = [_g("gate", ["2|G"])]
    assert match_groups("1", "G", gs, symbol_name="G") == ("exact", gs)


def test_u7_nameless_pin_number_in_two_groups_is_ambiguous():
    gs = [_g("in", ["1|IN"]), _g("out", ["1|OUT"])]
    assert match_groups("1", "1", gs) == ("num_ambiguous", [])


def test_u8_slash_alternate_of_board_name():
    gs = [_g("drain", ["5|D"])]
    assert match_groups("1", "G/D", gs) == ("slash", gs)


# ── further matcher semantics ────────────────────────────────────────────────

def test_symbol_name_binds_when_board_name_is_the_pin_number():
    gs = [_g("other", ["3|PB0"]), _g("vdd", ["7|VDD"], "power", supply_max=3.6)]
    assert match_groups("7", "7", gs, symbol_name="VDD") == ("exact", [gs[1]])


def test_pair_name_half_never_matches_pin_id():
    # "7|7"-style: the name half is not compared to pin_id (only bare tokens are)
    gs = [_g("a", ["9|7"])]
    assert match_groups("7", "X", gs) == ("none", [])
    assert match_groups("7", "X", [_g("b", ["7"])])[0] == "exact"


def test_question_mark_entry_is_name_only():
    gs = [_g("nc", ["?|NC"])]
    assert match_groups("4", "NC", gs) == ("exact", gs)
    assert match_groups("4", "4", gs) == ("none", [])


def test_num_conflict_and_ambiguous_fall_through_like_none():
    res = {"pin_groups": [_g("diode", ["1|Anode"]), _g("dflt", ["X"])], "default_group": "dflt"}
    assert resolve_pin_spec_from_groups("1", "K", res, symbol_name="K")["group_name"] == "dflt"
    assert pin_match_meta("1", "K", res, symbol_name="K") == (False, False)
    res2 = {"pin_groups": [_g("in", ["1|IN"]), _g("out", ["1|OUT"], "digital_output")], "default_group": "out"}
    assert resolve_pin_spec_from_groups("1", "1", res2)["group_name"] == "out"
    assert pin_match_meta("1", "1", res2) == (False, False)


def test_number_bind_is_pin_type_exact():
    res = {"pin_groups": [_g("io", ["4|D2+"])], "default_group": None}
    assert resolve_pin_spec_from_groups("4", "4", res)["group_name"] == "io"
    assert pin_match_meta("4", "4", res) == (True, False)


# ── legacy parity: bare tokens, symbol_name "" == pre-TODO-488 (frozen) ──────

LEGACY_GROUPS = [
    {'group_name': 'SIG', 'pin_type': 'digital_input', 'example_pins': ['PA0', '5', '~{RESET}', 'PB1']},
    {'group_name': 'SIG2', 'pin_type': 'digital_output', 'example_pins': ['PB1', 'OE', 'TX']},
    {'group_name': 'VDD', 'pin_type': 'power', 'example_pins': ['VDD', 'IOVDD', '12'], 'supply_max': 3.6},
    {'group_name': 'VBAT', 'pin_type': 'power', 'example_pins': ['VBAT', '~{OE}']},
    {'group_name': 'ODFLG', 'pin_type': 'digital_output', 'example_pins': ['FLG', '/CS', '|VCC', '5|'],
     'applies_to': 'open-drain fault flag'},
    {'group_name': 'NULLEX', 'pin_type': 'digital_input', 'example_pins': None},
    {'group_name': 'RX', 'pin_type': 'power', 'example_pins': ['RX', '/CS2'], 'supply_max': 3.6},
    {'group_name': 'RX2', 'pin_type': 'power', 'example_pins': ['~{CS2}'], 'supply_abs_max': 4.0},
]
# (pin_id, pin_name, match kind, match hits, resolved group, pin_match_meta, listing_supply_groups)
LEGACY_CASES = [
    ('5', 'PA0', 'exact', ['SIG'], 'SIG', [True, False], []),
    ('7', 'PA0', 'exact', ['SIG'], 'SIG', [True, False], []),
    ('9', 'PB1', 'exact', ['SIG', 'SIG2'], 'SIG', [True, False], []),
    ('3', 'RESET', 'norm', ['SIG'], 'SIG', [True, False], []),
    ('4', '~{OE}', 'exact', ['VBAT'], 'VBAT', [True, False], []),
    ('30', 'PA9/TX', 'slash', ['SIG2'], 'SIG2', [True, False], []),
    ('31', 'OE/~{OE}', 'none', [], 'SIG2', [False, False], []),
    ('40', 'UNKNOWN', 'none', [], 'SIG2', [False, False], []),
    ('41', '', 'none', [], 'SIG2', [False, False], []),
    ('12', '12', 'exact', ['VDD'], 'VDD', [True, False], ['VDD']),
    ('13', 'IOVDD', 'exact', ['VDD'], 'VDD', [True, False], ['VDD']),
    ('14', 'FLG', 'exact', ['ODFLG'], 'ODFLG', [True, True], []),
    ('15', 'CS', 'norm', ['ODFLG'], 'ODFLG', [True, True], []),
    ('16', 'VCC', 'none', [], 'SIG2', [False, False], []),
    ('17', '5|', 'exact', ['ODFLG'], 'ODFLG', [True, True], []),
    ('18', 'CS2', 'none', [], 'SIG2', [False, False], ['RX', 'RX2']),
    ('19', 'RX/CS2', 'none', [], 'SIG2', [False, False], ['RX', 'RX2']),
    ('20', 'VBAT', 'exact', ['VBAT'], 'VBAT', [True, False], []),
]


@pytest.mark.parametrize("pid,pname,kind,hits,resolved,meta,listing", LEGACY_CASES)
def test_bare_token_parity_with_pre_todo488(pid, pname, kind, hits, resolved, meta, listing):
    res = {"pin_groups": LEGACY_GROUPS, "default_group": "SIG2"}
    k, h = match_groups(pid, pname, LEGACY_GROUPS, symbol_name="")
    assert (k, _names(h)) == (kind, hits)
    assert resolve_pin_spec_from_groups(pid, pname, res, symbol_name="")["group_name"] == resolved
    assert list(pin_match_meta(pid, pname, res, symbol_name="")) == meta
    assert _names(listing_supply_groups(pid, pname, res, symbol_name="")) == listing


def test_paren_strip_is_the_one_bare_token_widening():
    # S3's trailing "(...)" strip is new for bare tokens too (pre-TODO-488: no match -> fallback)
    gs = [_g("PORTH", ["PH0"])]
    assert match_groups("12", "PH0(RXD2)", gs) == ("slash", gs)


# ── listing_supply_groups with pair entries ─────────────────────────────────

def test_listing_supply_groups_pair_entries():
    vdd = _g("VDDIO", ["7|IOVDD", "9|VDD"], "power", supply_max=3.6)
    sig = _g("SIG", ["3|PB0"])
    res = {"pin_groups": [sig, vdd]}
    assert listing_supply_groups("7", "IOVDD", res) == [vdd]                     # S1 by pair name
    assert listing_supply_groups("9", "9", res) == [vdd]                         # nameless: number stage
    assert listing_supply_groups("9", "9", res, symbol_name="VDD") == [vdd]      # symbol name, S1
    assert listing_supply_groups("3", "3", res) == []                            # number -> non-power group
    assert listing_supply_groups("9", "PB5", res) == []                          # named: num_conflict


# ── libparts reader ──────────────────────────────────────────────────────────

MODERN = """(export (version "E")
  (libparts
    (libpart (lib "MCU_Microchip") (part "ATmega2560-16A")
      (fields (field (name "Reference") "U"))
      (pins
        (pin (num "12") (name "PH0(RXD2)") (type "bidirectional"))
        (pin (num "13") (name "~") (type "passive"))
        (pin (num "14") (name "") (type "passive"))
        (pin (num "12") (name "DUP") (type "passive"))))))"""

OLD_STYLE = """(export (version D)
  (libparts
    (libpart (lib OLIMEX_Buttons) (part "SIDE_WTCM-TR(3x4)")
      (fields
        (field (name Reference) SW))
      (pins
        (pin (num 1) (name ~) (type passive))
        (pin (num 2) (name ~) (type passive))))
    (libpart (lib OLIMEX_RF) (part ESP32-WROOM)
      (pins
        (pin (num 10) (name GPIO25/DAC_1) (type BiDi))
        (pin (num 25) (name ~{RESET}) (type input))))))"""


def _tree(text):
    return parse_sexp(tokenize(text), 0)[0]


def test_read_libpart_pins_modern():
    assert read_libpart_pins(_tree(MODERN)) == {
        ("MCU_Microchip", "ATmega2560-16A"): {"12": "PH0(RXD2)", "13": "", "14": ""}}


def test_read_libpart_pins_old_style_unquoted():
    assert read_libpart_pins(_tree(OLD_STYLE)) == {
        ("OLIMEX_Buttons", "SIDE_WTCM-TR(3x4)"): {"1": "", "2": ""},
        ("OLIMEX_RF", "ESP32-WROOM"): {"10": "GPIO25/DAC_1", "25": "~{RESET}"}}


def test_read_libpart_pins_without_libparts_section():
    assert read_libpart_pins(_tree('(export (version "E") (components))')) == {}


# ── step_02 plumbing ─────────────────────────────────────────────────────────

NETLIST = """(export (version "E")
  (components
    (comp (ref "U1") (value "ATmega2560-16AU")
      (libsource (lib "MCU_Microchip") (part "ATmega2560-16A") (description "")))
    (comp (ref "J1") (value "CONN")
      (libsource (lib "Connector") (part "Conn_01x02") (description ""))))
  (libparts
    (libpart (lib "MCU_Microchip") (part "ATmega2560-16A")
      (pins
        (pin (num "12") (name "PH0(RXD2)") (type "bidirectional"))
        (pin (num "13") (name "~") (type "passive")))))
  (nets
    (net (code "1") (name "RXD2")
      (node (ref "U1") (pin "12") (pinfunction "PH0(RXD2)") (pintype "bidirectional"))
      (node (ref "J1") (pin "1") (pintype "passive")))
    (net (code "2") (name "N2")
      (node (ref "U1") (pin "13") (pintype "passive"))
      (node (ref "U1") (pin "99") (pintype "passive"))
      (node (ref "J1") (pin "2") (pintype "passive")))))"""


def test_parser_fills_symbol_name(tmp_path):
    p = tmp_path / "board.net"
    p.write_text(NETLIST)
    ir = parse_netlist(p)
    u1 = next(c for c in ir.components if c.refdes == "U1")
    j1 = next(c for c in ir.components if c.refdes == "J1")
    assert (u1.libsource_lib, u1.libsource_part) == ("MCU_Microchip", "ATmega2560-16A")
    pins = {p.pin_id: (p.pin_name, p.symbol_name) for p in u1.pins}
    assert pins == {"12": ("PH0(RXD2)", "PH0(RXD2)"), "13": ("13", ""), "99": ("99", "")}
    assert all(p.symbol_name == "" for p in j1.pins)
    assert ir.libparts_unmatched == 1


# ── validator: pair-aware name reads ─────────────────────────────────────────

def _validate(groups):
    return V.validate({"pin_groups": groups, "default_group": None}, model_input="", source_text=None, stem="x")


def test_entry_name():
    assert V.entry_name("1|IOVDD") == "IOVDD"
    assert V.entry_name("?|ALL PINS") == "ALL PINS"
    assert V.entry_name("IOVDD") == "IOVDD"
    assert V.entry_name("5|") == "5|"
    assert V.entry_name(None) is None


def test_validator_retype_on_pair_entry():
    out, rep = _validate([_g("io supply", ["1|IOVDD"], "digital_input", supply_max=3.6)])
    assert out["pin_groups"][0]["pin_type"] == "power"
    assert rep["counts"]["retyped"] == 1


def test_validator_blanket_drop_on_question_mark_pair():
    out, rep = _validate([_g("all", ["?|ALL PINS", "3|PB0"])])
    assert out["pin_groups"][0]["example_pins"] == ["3|PB0"]
    assert rep["counts"]["example_pins_blanket_dropped"] == 1


def test_validator_counts_malformed_entries_without_dropping():
    out, rep = _validate([_g("sig", ["5|", "|VCC", "1|A"]), _g("sig2", ["2|"])])
    assert rep["counts"]["example_pins_malformed"] == 3
    assert out["pin_groups"][0]["example_pins"] == ["5|", "|VCC", "1|A"]


def test_validator_ground_test_reads_name_half():
    assert V.is_ground(_g("ret", ["5|GND", "12|GND"], "power"))
    assert not V.is_ground(_g("ret", ["5|GND", "1|VDD"], "power"))

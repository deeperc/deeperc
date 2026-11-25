"""TODO-415: curated per-pin supply map for multi-rail parts.

step_08_checker asks this module which SUPPLY PIN a signal pin of a part runs
from. When a rule applies, the pin's voltage is that supply pin's confirmed
rail voltage and never another supply's; step_08 uses it for the driver's
voltage and for the highest-rail driver ranking.

A rule applies only when all three hold:
  1. the component's part number starts with a family prefix below (casefolded);
  2. the pin name matches one of the family's rules;
  3. the rule's supply pin is on that component.
Otherwise the map is SILENT (``pin_supply`` returns None) and step_08 runs its
part-level logic unchanged. A part of a listed family that lacks the supply
pin (a module sold under the chip's name, say) is therefore untouched.

Every rule carries the manufacturer document, the page and a short quote that
names the supply (TODO-415 Rulings decision 10). A rule without one does not
belong here. Document and page strings hold no parentheses (step_08 puts them
inside a bracketed note in the evidence label). Page numbers are PDF page indexes counted from 1. Quotes are the
document's words with subscripts written inline (V CCA -> VCCA); a table row is
quoted as its text and the row is named in brackets.

Not in the map, by ruling: USB7252, TPS65988, HD3SS3220 (USB / CC / high-speed
pins, not logic outputs at a supply level), SI5340B, ESP32-S3 modules, and the
PS pins of XCZU parts (the pin name does not carry the bank).

Pure and I/O-free: standard library only. It must not import step_08_checker
(which imports this module). It is a CORE_PIPELINE_FILES member (provenance.py):
any edit moves ``checker_code_hash`` and stales old reports, by design.
"""
from __future__ import annotations

import re
from typing import NamedTuple


class Rule(NamedTuple):
    rule_id: str             # unique inside its family, e.g. "A"
    pattern: "str | None"    # regex, fullmatched on norm(pin name); None for a "bank" family
    supply: str              # supply pin name ("VCCO_<bank>" for a "bank" family)
    page: str                # where the quote is
    quote: str               # the words that name the supply


class Family(NamedTuple):
    prefix: str              # casefolded part-number prefix
    kind: str                # "named" | "bank"
    document: str
    rules: "tuple[Rule, ...]"


# Enable-pin spellings after norm(): OE, ~{OE}, OE_N, OEN, NOE, N_OE, OE_B, OEB.
_EN = r"n?_?oe(?:_?[nb])?"

FAMILIES: "tuple[Family, ...]" = (
    Family("sn74lxch8t245", "named", "TI SN74LXCH8T245 datasheet SCES917B", (
        Rule("A", r"a[1-8]", "VCCA", "p. 1",
             "Ax pins and control pins (DIR and OE) are referenced to VCCA logic levels"),
        Rule("B", r"b[1-8]", "VCCB", "p. 3, Table 5-1",
             "Referenced to VCCB (rows B1 to B8)"),
        Rule("DIR", r"dir", "VCCA", "p. 19",
             "The control pins of the SN74LXCH8T245 (DIR and OE) are referenced to VCCA"),
        Rule("OE", _EN, "VCCA", "p. 3, Table 5-1",
             "Referenced to VCCA (row OE)"),
    )),
    Family("sn74axc8t245", "named", "TI SN74AXC8T245 datasheet SCES875C", (
        Rule("A", r"a[1-8]", "VCCA", "p. 19",
             "The I/O pins labeled with A and the control pins (DIR1, DIR2, and OE) are "
             "supported by VCCA"),
        Rule("B", r"b[1-8]", "VCCB", "p. 19",
             "the I/O pins labeled with B are supported by VCCB"),
        Rule("DIR", r"dir[12]?", "VCCA", "p. 19",
             "control pins (DIR1, DIR2, and OE) are supported by VCCA"),
        Rule("OE", _EN, "VCCA", "p. 19",
             "control pins (DIR1, DIR2, and OE) are supported by VCCA"),
    )),
    Family("sn74lvc1t45", "named", "TI SN74LVC1T45 datasheet SCES515N", (
        Rule("A", r"a", "VCCA", "p. 1", "The A port is designed to track VCCA"),
        Rule("B", r"b", "VCCB", "p. 1", "The B port is designed to track VCCB"),
        Rule("DIR", r"dir", "VCCA", "p. 1", "DIR input circuit referenced to VCCA"),
    )),
    Family("txb0108", "named", "TI TXB0108 datasheet SCES643L", (
        Rule("A", r"a[1-8]", "VCCA", "p. 4", "Input/output 1. Referenced to VCCA (row A1)"),
        Rule("B", r"b[1-8]", "VCCB", "p. 4", "Input/output 8. Referenced to VCCB (row B8)"),
        Rule("OE", _EN, "VCCA", "p. 4",
             "Pull OE low to place all outputs in 3-state mode. Referenced to VCCA"),
    )),
    Family("nts0104", "named", "NXP NTS0104 data sheet Rev. 3", (
        Rule("A", r"a[1-4]", "VCCA", "p. 5, Table 3",
             "data input or output (referenced to VCC(A)) (row A1, A2, A3, A4)"),
        Rule("B", r"b[1-4]", "VCCB", "p. 5, Table 3",
             "data input or output (referenced to VCC(B)) (row B4, B3, B2, B1)"),
        Rule("OE", _EN, "VCCA", "p. 5, Table 3",
             "output enable input (active HIGH; referenced to VCC(A))"),
    )),
    Family("rp2040", "named", "Raspberry Pi RP2040 Datasheet, pin table", (
        Rule("GPIO", r"gpio(?:[0-9]|1[0-9]|2[0-5])", "IOVDD", "pp. 613-614",
             "GPIO0 2 Digital IO (FT) IOVDD Pull-Down User IO"),
        Rule("GPIO_ADC",
             r"gpio26(?:_adc0)?|gpio27(?:_adc1)?|gpio28(?:_adc2)?|gpio29(?:_adc3)?",
             "IOVDD", "p. 613", "GPIO26 / ADC0 38 Digital IO / IOVDD / Pull-Down User IO or ADC"),
        Rule("QSPI", r"qspi_(?:sd[0-3]|sclk|sck|clk|ss|ss_n|ssn|cs|csn|cs_n|ncs)",
             "IOVDD", "p. 613", "QSPI_SD3 51 Digital IO IOVDD QSPI data"),
        Rule("SWD", r"swclk|swdio|swd", "IOVDD", "pp. 613-614",
             "SWCLK 24 Digital In (FT) IOVDD Pull-Up Debug clock"),
        Rule("RUN", r"run", "IOVDD", "p. 614", "RUN 26 Digital In (FT) IOVDD Pull-Up Chip enable"),
    )),
    Family("sn74lvch16t245", "named", "TI SN74LVCH16T245 datasheet SCES635B", (
        Rule("A", r"[12]a[1-8]", "VCCA", "pp. 4-5",
             "Input/Output. Referenced to VCCA (row 1A1)"),
        Rule("B", r"[12]b[1-8]", "VCCB", "pp. 4-5",
             "Input/Output. Referenced to VCCB (row 1B1)"),
        Rule("DIR", r"[12]dir", "VCCA", "p. 1",
             "The SN74LVCH16T245 device control pins (1DIR, 2DIR, 1OE, and 2OE) are "
             "supplied by VCCA"),
        Rule("OE", r"n?_?[12]oe(?:_?[nb])?", "VCCA", "p. 5",
             "Tri-State output-mode enables. Referenced to VCCA (row 1OE)"),
    )),
    # Zynq UltraScale+ PL user I/O. The supply is the bank's VCCO pins; the bank is
    # read from the pin name. Checked pin by pin for the XCZU4CG in SFVC784 against
    # AMD's package file xczu4cgsfvc784pkg.txt rev 1.1 (252 of 252 user I/O pins;
    # VCCO pins of banks 43-46 and 64-66). PS pins have no rule. A package whose
    # bank has no VCCO pin of its own number (UG1075: bank 66 runs from VCCO_65 on
    # UBVA494, SBVA484 and UBVA530) gets no voltage from this rule: it is silent.
    Family("xczu", "bank", "AMD UG1075 v1.13 Zynq UltraScale+ Packaging and Pinouts", (
        Rule("BANK", None, "VCCO_<bank>", "Table 1-4",
             "VCCO_[bank number]: Power-supply pins for the output drivers (per bank)"),
    )),
)


class PinSupply(NamedTuple):
    family: str              # family prefix
    rule: str                # "<family>.<rule_id>"
    supply: str              # supply pin name; "VCCO_44" for a bank rule
    net: "str | None"        # net of the first confirmed supply pin (None unless MAPPED)
    voltage: "float | None"  # None unless state is MAPPED
    state: str               # "MAPPED" | "SUPPLY_UNCONFIRMED" | "SUPPLY_MIXED"
    voltages: "tuple[float, ...]"   # sorted distinct confirmed voltages of the supply pins
    citation: str            # "<document> <page>"
    n_supply_pins: int
    n_confirmed: int


def norm(name: "str | None") -> str:
    """casefold; "~", "{" and "}" removed; every other run of non-alphanumerics
    becomes one "_"; leading and trailing "_" stripped.
    "~{OE}" -> "oe", "GPIO26/ADC0" -> "gpio26_adc0", "QSPI-SS-N" -> "qspi_ss_n",
    "GPIO1_0" -> "gpio1_0" (separators are kept, so it is not read as GPIO10)."""
    s = re.sub(r"[~{}]", "", (name or "").casefold())
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def loose(name: "str | None") -> str:
    """casefold, every non-alphanumeric character removed: "VCC(A)" == "VCC_A"
    == "VCCA". Used for the SUPPLY pin names of "named" families only."""
    return re.sub(r"[^a-z0-9]", "", (name or "").casefold())


def family_for(part_number: "str | None") -> "Family | None":
    pn = (part_number or "").casefold().strip()
    if not pn:
        return None
    for fam in FAMILIES:
        if pn.startswith(fam.prefix):
            return fam
    return None


def bank_of(comp, pin_name: "str | None") -> "str | None":
    """The bank of a PL user I/O pin of a "bank" family part, or None.
    Vendor form: IO_..._<bank>. A symbol may append the ball
    (IO_..._<bank>_<ball>); that last segment is dropped ONLY when it equals the
    pin_id of a pin carrying this name. Anything else (PS_MIO.., MGT.., a name
    not starting IO_, a non-numeric bank segment) has no bank."""
    segs = (pin_name or "").strip().split("_")
    if len(segs) < 3 or segs[0].casefold() != "io":
        return None
    ids = {(p.pin_id or "").casefold() for p in comp.pins if p.pin_name == pin_name}
    if segs[-1].casefold() in ids:
        segs = segs[:-1]
    b = segs[-1]
    return b if (len(segs) >= 3 and b.isdigit() and 2 <= len(b) <= 3) else None


def rule_for(comp, pin_name: "str | None"):
    """None when no family matches the part or no rule matches the pin; else
    (family, rule, supply pin name, bank). bank is None for a "named" family."""
    fam = family_for(getattr(comp, "part_number", None))
    if fam is None:
        return None
    if fam.kind == "bank":
        b = bank_of(comp, pin_name)
        if b is None:
            return None
        return fam, fam.rules[0], f"VCCO_{b}", b
    n = norm(pin_name)
    for rule in fam.rules:
        if re.fullmatch(rule.pattern, n):
            return fam, rule, rule.supply, None
    return None


def supply_pins(comp, supply: str, bank: "str | None") -> list:
    """The component's pins that ARE the named supply.
    named family: loose-equal names. bank family: the pin is named VCCO_<bank>,
    or VCCO_<bank>_<ball> where <ball> is that pin's own pin_id."""
    if bank is None:
        t = loose(supply)
        return [p for p in comp.pins if loose(p.pin_name) == t]
    base = f"vcco_{bank}"
    out = []
    for p in comp.pins:
        n = (p.pin_name or "").strip().casefold()
        if n == base or (p.pin_id and n == f"{base}_{p.pin_id.casefold()}"):
            out.append(p)
    return out


def pin_supply(comp, pin_name: "str | None", confirmed_voltages: dict) -> "PinSupply | None":
    """None = the map is silent (see the module docstring). Otherwise a PinSupply:
      MAPPED              the supply pins on confirmed rails show ONE voltage;
      SUPPLY_UNCONFIRMED  no supply pin is on a confirmed rail (voltage None);
      SUPPLY_MIXED        they show more than one voltage (voltage None).
    A mapped pin never takes another supply's voltage. n_confirmed below
    n_supply_pins with state MAPPED means some pins of that supply sit on nets
    with no confirmed voltage; the confirmed ones decide."""
    if comp is None:
        return None
    r = rule_for(comp, pin_name)
    if r is None:
        return None
    fam, rule, supply, bank = r
    pins = supply_pins(comp, supply, bank)
    if not pins:
        return None
    vs = [(p.net, confirmed_voltages[p.net]) for p in pins
          if p.net in confirmed_voltages and confirmed_voltages[p.net] is not None]
    distinct = tuple(sorted({v for _, v in vs}))
    rid = f"{fam.prefix}.{rule.rule_id}"
    citation = f"{fam.document} {rule.page}"
    if len(distinct) == 1:
        return PinSupply(fam.prefix, rid, supply, vs[0][0], distinct[0], "MAPPED",
                         distinct, citation, len(pins), len(vs))
    state = "SUPPLY_MIXED" if distinct else "SUPPLY_UNCONFIRMED"
    return PinSupply(fam.prefix, rid, supply, None, None, state,
                     distinct, citation, len(pins), len(vs))


def all_rule_ids() -> "set[str]":
    return {f"{fam.prefix}.{rule.rule_id}" for fam in FAMILIES for rule in fam.rules}

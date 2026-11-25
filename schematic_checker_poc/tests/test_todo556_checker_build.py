"""TODO-556 checker era build: decisions 16-20 (and the shape decision 27 adds, in its
own test file when that patch is applied).

  16  step_08: a receiver that is the driver's own part instance gets no row; one
      not_checked_parts SAME_INSTANCE entry per part (TODO-567).
  17  step_08: a GAP part classed protection_diode is never a signal endpoint, with or
      without limits; it is disclosed as NON_IC_PART.
  18  passive_traversal (07b): a rail-named net with no voltage takes a single HIGH
      inductor / ferrite bridge, past the IC-power-pin gate and the side-branch check.
  19  step_08b: a supply-admitted own regulator / bootstrap pin on a net with no voltage
      gets no row; disclosed in not_checked_pins with reason OWN_SUPPLY_PIN.
  20  part_bindings: ESP32-WROOM-32D-4MB is bound to esp32-wroom-32d.
Plus the report and schema surface: build_report always writes not_checked_pins, and
docs/report_schema.json knows SAME_INSTANCE and OWN_SUPPLY_PIN.
"""
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

POC = Path(__file__).resolve().parent.parent
REPO = POC.parent
sys.path.insert(0, str(POC))

from llm import ollama_client  # noqa: E402
from steps import checker_registry, part_bindings, step_08b_supply_checker as sc  # noqa: E402
from steps.passive_traversal import BridgeType, Confidence, traverse_passive_bridges  # noqa: E402
from steps.step_02_parser import ComponentIR, NetIR, NetlistIR, PinIR  # noqa: E402
from steps.step_05_validator import ValidatedPinSpec  # noqa: E402
from steps.step_08_checker import run  # noqa: E402
from steps.step_10_report import build_report  # noqa: E402

SCHEMA = json.loads((REPO / "docs" / "report_schema.json").read_text())
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)
CONFIRMED = {"VCC_3V3": 3.3, "VCC_5V": 5.0}


# ── fixtures ───────────────────────────────────────────────────────────────────

def _spec(part, pin, *, abs_max=3.6, VIH_min=2.0, pin_type="input"):
    return ValidatedPinSpec(
        part_number=part, pin_name=pin, pin_id="1", VIH_max=None, VIH_min=VIH_min,
        absolute_max_voltage=abs_max, pin_type=pin_type, source_snippet="snippet",
        signal_score=75, confidence="high", is_verified=True, pin_type_exact=True)


def _ic(refdes, part, sig_pins, vcc_net="VCC_3V3"):
    pins = [PinIR(pin_id=str(i + 1), pin_name=n, net=net) for i, (n, net) in enumerate(sig_pins)]
    pins.append(PinIR(pin_id=str(len(pins) + 1), pin_name="VCC", net=vcc_net))
    return ComponentIR(refdes=refdes, part_number=part, value="", pins=pins)


def _tvs(refdes, part, sig_pins):
    c = ComponentIR(refdes=refdes, part_number=part, value="",
                    pins=[PinIR(pin_id=str(i + 1), pin_name=n, net=net)
                          for i, (n, net) in enumerate(sig_pins)])
    c.libsource_lib = "Power_Protection_TVS"   # rule J: "tvs" in the library -> protection_diode
    return c


def _ir(components):
    nets: dict[str, list] = {}
    for c in components:
        for p in c.pins:
            nets.setdefault(p.net, []).append((c.refdes, p.pin_id))
    rails = [n for n in nets if n.startswith("VCC_")]
    return NetlistIR(source_file="t", components=components,
                     nets=[NetIR(name=n, pins=ps) for n, ps in nets.items()],
                     power_nets=rails, ground_nets=[])


def _no_llm(monkeypatch):
    def _down(*a, **k):
        raise RuntimeError("no local model in tests")
    monkeypatch.setattr(ollama_client, "generate", _down)


# ── 16: same-instance rows ─────────────────────────────────────────────────────

def _self_driven():
    # U1 drives SW from PA0 and reads it on PA1; U2 also reads SW. U1 drives SIG to U2 too.
    u1 = _ic("U1", "BUCK-1", [("PA0", "SW"), ("PA1", "SW"), ("PA2", "SIG")])
    u2 = _ic("U2", "RCV-1", [("IN", "SW"), ("IN2", "SIG")])
    specs = {("U1", "PA0"): _spec("BUCK-1", "PA0", pin_type="output"),
             ("U1", "PA1"): _spec("BUCK-1", "PA1"),
             ("U2", "IN"): _spec("RCV-1", "IN"), ("U2", "IN2"): _spec("RCV-1", "IN2")}
    return _ir([u1, u2]), specs


def test_same_instance_receiver_gets_no_row_and_other_receivers_keep_theirs():
    ir, specs = _self_driven()
    out = []
    rows = run(ir, specs, CONFIRMED, not_checked_out=out)
    keys = sorted((r.net_name, r.driver_refdes, r.receiver_refdes, r.receiver_pin_name) for r in rows)
    assert keys == [("SIG", "U1", "U2", "IN2"), ("SW", "U1", "U2", "IN")]
    assert all(r.status == "PASS" for r in rows)


def test_same_instance_disclosure_one_entry_per_part():
    ir, specs = _self_driven()
    out = []
    run(ir, specs, CONFIRMED, not_checked_out=out)
    si = [e for e in out if e["reason"] == "SAME_INSTANCE"]
    assert si == [{"refdes": "U1", "part_number": "BUCK-1", "checker": "signal",
                   "reason": "SAME_INSTANCE", "nets": ["SW"], "pins": ["PA1"],
                   "pins_with_rows": 0}]


def test_same_instance_pins_with_rows_counts_the_parts_remaining_receiver_rows():
    # U1 is the receiver of a row on RX (driven by U3's explicit output) and drives
    # itself on SW.
    u1 = _ic("U1", "MCU-1", [("PA0", "SW"), ("PA1", "SW"), ("RX", "RX")])
    u3 = _ic("U3", "DRV-3", [("TX", "RX")])
    specs = {("U1", "PA0"): _spec("MCU-1", "PA0", pin_type="output"),
             ("U1", "RX"): _spec("MCU-1", "RX"),
             ("U3", "TX"): _spec("DRV-3", "TX", pin_type="output")}
    out = []
    rows = run(_ir([u1, u3]), specs, CONFIRMED, not_checked_out=out)
    assert [(r.net_name, r.receiver_refdes) for r in rows] == [("RX", "U1")]
    (si,) = [e for e in out if e["reason"] == "SAME_INSTANCE"]
    assert si["pins_with_rows"] == 1 and si["nets"] == ["SW"]


def test_same_instance_rows_drop_without_a_disclosure_list():
    ir, specs = _self_driven()
    rows = run(ir, specs, CONFIRMED)
    assert not any(r.receiver_refdes == r.driver_refdes for r in rows)


def test_a_net_with_only_the_drivers_own_pins_is_silent():
    u1 = _ic("U1", "SELF-PART", [("PA0", "SELF"), ("PA1", "SELF")])
    out = []
    assert run(_ir([u1]), {}, CONFIRMED, not_checked_out=out) == []
    assert [e["reason"] for e in out] == ["SAME_INSTANCE"]


# ── 17: protection diodes are never signal endpoints ──────────────────────────

def _tvs_board():
    u1 = _ic("U1", "MCU-1", [("PA0", "DP")])
    u2 = _ic("U2", "PHY-1", [("D+", "DP")])
    tvs = _tvs("U9", "TPD4E05U06QDQARQ1", [("IO1", "DP")])
    specs = {("U1", "PA0"): _spec("MCU-1", "PA0", pin_type="output"),
             ("U2", "D+"): _spec("PHY-1", "D+"),
             ("U9", "IO1"): _spec("TPD4E05U06QDQARQ1", "IO1", abs_max=6.0)}
    return _ir([u1, u2, tvs]), specs


def test_protection_diode_with_limits_is_not_a_receiver():
    ir, specs = _tvs_board()
    out = []
    rows = run(ir, specs, CONFIRMED, not_checked_out=out)
    assert [(r.receiver_refdes, r.status) for r in rows] == [("U2", "PASS")]
    (e,) = [e for e in out if e["refdes"] == "U9"]
    assert e["reason"] == "NON_IC_PART" and e["part_class"] == "protection_diode"
    assert e["nets"] == ["DP"] and e["pins_with_rows"] == 0


def test_protection_diode_alone_with_one_ic_gives_no_row():
    u1 = _ic("U1", "MCU-1", [("PA0", "DP")])
    tvs = _tvs("U9", "TPD4E05U06QDQARQ1", [("IO1", "DP")])
    specs = {("U9", "IO1"): _spec("TPD4E05U06QDQARQ1", "IO1", abs_max=6.0)}
    assert run(_ir([u1, tvs]), specs, CONFIRMED) == []


def test_other_gap_classes_with_limits_still_receive():
    # TODO-554's rule is unchanged for every class but protection_diode.
    u1 = _ic("U1", "MCU-1", [("PA0", "SIG")])
    q = ComponentIR(refdes="U5", part_number="FET-1", value="",
                    pins=[PinIR(pin_id="1", pin_name="G", net="SIG")])
    q.libsource_lib = "Transistor_FET"
    specs = {("U1", "PA0"): _spec("MCU-1", "PA0", pin_type="output"),
             ("U5", "G"): _spec("FET-1", "G", abs_max=20.0)}
    rows = run(_ir([u1, q]), specs, CONFIRMED)
    assert [(r.receiver_refdes, r.status) for r in rows] == [("U5", "PASS")]


# ── 18: 07b bridge into rail-named nets ────────────────────────────────────────

@dataclass
class _P:
    pin_id: str
    net: str
    pin_name: str = ""


@dataclass
class _C:
    refdes: str
    value: str
    pins: list


@dataclass
class _N:
    name: str
    pins: list


@dataclass
class _NL:
    components: list
    nets: list


def _vdd1a_board(bridge="L3", value="600R"):
    """/VDD1A-2A: LAN8710A pins 1 and 27 (numeric names), a bridge to +3V3, and a
    49.9R resistor to the magnetics (a side branch that rejects the net today)."""
    comps = [
        _C("U4", "LAN8710A", [_P("1", "/VDD1A-2A", "1"), _P("27", "/VDD1A-2A", "27")]),
        _C(bridge, value, [_P("1", "+3V3"), _P("2", "/VDD1A-2A")]),
        _C("R12", "49.9R", [_P("1", "/VDD1A-2A"), _P("2", "/TXP")]),
        _C("C20", "100nF", [_P("1", "/VDD1A-2A"), _P("2", "GND")]),
    ]
    nets: dict[str, list] = {}
    for c in comps:
        for p in c.pins:
            nets.setdefault(p.net, []).append((c.refdes, p.pin_id))
    return _NL(comps, [_N(n, ps) for n, ps in nets.items()])


def test_rail_named_net_takes_a_single_inductor_bridge():
    derived, records = traverse_passive_bridges(
        _vdd1a_board(), {"+3V3": 3.3}, ["GND"], rail_named_nets={"/VDD1A-2A"})
    (src,) = derived["/VDD1A-2A"]
    assert (src.rail, src.voltage, src.confidence) == ("+3V3", 3.3, Confidence.HIGH)
    assert src.bridge.refdes == "L3" and src.bridge.bridge_type == BridgeType.INDUCTOR
    assert records["/VDD1A-2A"].was_downgraded is False


def test_same_net_not_rail_named_is_walked_as_before():
    derived, _ = traverse_passive_bridges(_vdd1a_board(), {"+3V3": 3.3}, ["GND"])
    assert "/VDD1A-2A" not in derived
    derived, _ = traverse_passive_bridges(_vdd1a_board(), {"+3V3": 3.3}, ["GND"],
                                          rail_named_nets=set())
    assert "/VDD1A-2A" not in derived


def test_ferrite_bead_counts_as_an_inductor():
    derived, _ = traverse_passive_bridges(
        _vdd1a_board(bridge="FB2"), {"+3V3": 3.3}, ["GND"], rail_named_nets={"/VDD1A-2A"})
    assert derived["/VDD1A-2A"][0].bridge.refdes == "FB2"


@pytest.mark.parametrize("bridge, value", [("R7", "0R"), ("R7", "22R"), ("D3", "BAT54")])
def test_other_bridges_are_not_relaxed(bridge, value):
    # A 0R jumper or small resistor is HIGH in the ordinary walk, but the relaxed path
    # is narrowed to inductor / ferrite bridges (decision 18); a diode is MEDIUM.
    derived, _ = traverse_passive_bridges(
        _vdd1a_board(bridge=bridge, value=value), {"+3V3": 3.3}, ["GND"],
        rail_named_nets={"/VDD1A-2A"})
    assert "/VDD1A-2A" not in derived


def test_two_rails_stay_ambiguous():
    nl = _vdd1a_board()
    nl.components.append(_C("L9", "600R", [_P("1", "+3V3B"), _P("2", "/VDD1A-2A")]))
    for n in nl.nets:
        if n.name == "/VDD1A-2A":
            n.pins.append(("L9", "2"))
    nl.nets.append(_N("+3V3B", [("L9", "1")]))
    derived, _ = traverse_passive_bridges(nl, {"+3V3": 3.3, "+3V3B": 3.3}, ["GND"],
                                          rail_named_nets={"/VDD1A-2A"})
    assert len(derived.get("/VDD1A-2A", [])) != 1


def test_rail_named_net_with_an_ordinary_high_result_keeps_it():
    # A rail-named net with an IC power pin and only a bypass cap: the ordinary walk
    # already gives a single HIGH source; nothing changes.
    comps = [_C("U1", "X", [_P("1", "AVCC", "AVCC")]),
             _C("R0", "0R", [_P("1", "+5V"), _P("2", "AVCC")]),
             _C("C1", "100nF", [_P("1", "AVCC"), _P("2", "GND")])]
    nets = [_N("AVCC", [("U1", "1"), ("R0", "2"), ("C1", "1")]),
            _N("+5V", [("R0", "1")]), _N("GND", [("C1", "2")])]
    for rn in (set(), {"AVCC"}):
        derived, _ = traverse_passive_bridges(_NL(comps, nets), {"+5V": 5.0}, ["GND"],
                                              rail_named_nets=rn)
        (src,) = derived["AVCC"]
        assert src.bridge.bridge_type == BridgeType.JUMPER and src.confidence == Confidence.HIGH


# ── 19: own regulator / bootstrap pins ─────────────────────────────────────────

@pytest.mark.parametrize("name, tok", [
    ("VDDCR", "VDDCR"), ("VCAP_1", "VCAP_1"), ("VCAP_2", "VCAP_2"), ("BOOT", "BOOT"),
    ("VBST", "VBST"), ("BSTA", "BSTA"), ("BSTC", "BSTC"), ("V_{DDCR}", None),
    ("VREG", None), ("VREG_VIN", None), ("VBAT", None), ("VDD", None), ("VDD1A", None),
    ("VIN", None), ("V3", None), ("BST", "BST"), ("BOOT0", None), ("", None), (None, None),
])
def test_own_supply_pin_token(name, tok):
    assert sc.own_supply_pin_token(name) == tok


def test_own_token_set_has_no_bare_vreg():
    assert "VREG" not in sc.OWN_SUPPLY_PIN_TOKENS
    assert "VREGOUT" in sc.OWN_SUPPLY_PIN_TOKENS


def _lan(pin_name, net, symbol_name=""):
    comp = ComponentIR(refdes="U4", part_number="LAN8710A-EZC(QFN32) ", value="",
                       pins=[PinIR(pin_id="6", pin_name=pin_name, net=net, symbol_name=symbol_name),
                             PinIR(pin_id="12", pin_name="VDDIO", net="+3V3")])
    groups = {"pin_groups": [
        {"group_name": "VDDCR", "pin_type": "power", "supply_rail_name": "VDDCR",
         "supply_min": 1.14, "supply_max": 1.26, "supply_abs_max": 1.5, "example_pins": ["6|VDDCR"]},
        {"group_name": "VDDIO", "pin_type": "power", "supply_rail_name": "VDDIO",
         "supply_min": 1.6, "supply_max": 3.6, "supply_abs_max": 3.6, "example_pins": ["12|VDDIO"]}]}
    return comp, {comp.part_number: groups}


def test_own_pin_on_a_no_voltage_net_has_no_row_and_is_disclosed():
    comp, pg = _lan("VDDCR", "/VDDCR")
    out = []
    rows = sc.check_component_supplies([comp], pg, {"+3V3": 3.3}, {}, not_checked_pins_out=out)
    assert [(r.supply_pin_name, r.status) for r in rows] == [("VDDIO", "PASS")]
    assert out == [{"refdes": "U4", "part_number": "LAN8710A-EZC(QFN32) ", "pin_name": "VDDCR",
                    "net": "/VDDCR", "checker": "supply", "reason": "OWN_SUPPLY_PIN",
                    "token": "VDDCR"}]


def test_numeric_own_pin_is_named_by_its_symbol_name():
    comp, pg = _lan("6", "/VDDCR", symbol_name="VDDCR")
    out = []
    rows = sc.check_component_supplies([comp], pg, {"+3V3": 3.3}, {}, not_checked_pins_out=out)
    assert [r.supply_pin_name for r in rows] == ["VDDIO"]
    assert out and out[0]["pin_name"] == "6" and out[0]["symbol_name"] == "VDDCR"


def test_own_pin_with_a_voltage_keeps_its_row():
    comp, pg = _lan("VDDCR", "+1V2")
    out = []
    rows = sc.check_component_supplies([comp], pg, {"+3V3": 3.3, "+1V2": 1.2}, {},
                                       not_checked_pins_out=out)
    assert sorted((r.supply_pin_name, r.status) for r in rows) == [("VDDCR", "PASS"), ("VDDIO", "PASS")]
    assert out == []


def test_own_pin_rows_drop_without_a_disclosure_list():
    comp, pg = _lan("VDDCR", "/VDDCR")
    rows = sc.check_component_supplies([comp], pg, {"+3V3": 3.3})
    assert [r.supply_pin_name for r in rows] == ["VDDIO"]


def test_supply_adapter_fills_the_contexts_pin_list():
    comp, pg = _lan("VDDCR", "/VDDCR")
    ctx = SimpleNamespace(ir=_ir([comp]), seen_parts=pg, confirmed_voltages={"+3V3": 3.3},
                          derived_provenance={}, not_checked_pins=[])
    spec = next(s for s in checker_registry.REGISTRY if s.name == "supply")
    rows = spec.run(ctx)
    assert [r.supply_pin_name for r in rows] == ["VDDIO"]
    assert [e["reason"] for e in ctx.not_checked_pins] == ["OWN_SUPPLY_PIN"]


# ── 20: binding ────────────────────────────────────────────────────────────────

def test_esp32_wroom_32d_4mb_is_bound():
    assert part_bindings.bound_stem("ESP32-WROOM-32D-4MB") == "esp32-wroom-32d"
    assert part_bindings.bound_stem(" ESP32-WROOM-32D-4MB ") == "esp32-wroom-32d"
    assert part_bindings.bound_stem("ESP32-WROOM-32D") is None


# ── report and schema ──────────────────────────────────────────────────────────

def _errors(report):
    return sorted((list(e.absolute_path), e.message) for e in VALIDATOR.iter_errors(report))


def _report(tmp_path, monkeypatch, **kw):
    _no_llm(monkeypatch)
    ir, specs = _self_driven()
    ncp = []
    rows = run(ir, specs, CONFIRMED, not_checked_out=ncp)
    comp, pg = _lan("VDDCR", "/VDDCR")
    pins = []
    supply = sc.check_component_supplies([comp], pg, {"+3V3": 3.3}, {}, not_checked_pins_out=pins)
    args = dict(source_netlist="t.net", results=rows, confirmed_voltages=dict(CONFIRMED),
                extraction_metadata={}, output_path=str(tmp_path / "r.json"),
                components=ir.components, supply_results=supply,
                not_checked_parts=ncp, not_checked_pins=pins)
    args.update(kw)
    return build_report(**args)


def test_report_with_both_disclosures_validates(tmp_path, monkeypatch):
    rep = _report(tmp_path, monkeypatch)
    assert _errors(rep) == []
    assert [e["reason"] for e in rep["not_checked_parts"]] == ["SAME_INSTANCE"]
    (p,) = rep["not_checked_pins"]
    assert p["part_number"] == "LAN8710A-EZC(QFN32)"   # written stripped
    assert p["reason"] == "OWN_SUPPLY_PIN" and p["token"] == "VDDCR"


def test_not_checked_pins_is_always_written(tmp_path, monkeypatch):
    _no_llm(monkeypatch)
    rep = build_report(source_netlist="", results=[], confirmed_voltages={},
                       extraction_metadata={}, output_path=str(tmp_path / "r.json"))
    assert rep["not_checked_pins"] == [] and _errors(rep) == []


def test_schema_requires_each_new_reasons_keys(tmp_path, monkeypatch):
    rep = _report(tmp_path, monkeypatch)
    for key in ("nets", "pins", "pins_with_rows"):
        bad = json.loads(json.dumps(rep))
        bad["not_checked_parts"][0].pop(key)
        assert _errors(bad), key
    bad = json.loads(json.dumps(rep))
    bad["not_checked_pins"][0].pop("token")
    assert _errors(bad)
    ok = json.loads(json.dumps(rep))
    ok.pop("not_checked_pins")
    assert _errors(ok) == []   # optional key


def test_not_checked_pins_reasons_match_the_producer(tmp_path, monkeypatch):
    node = SCHEMA["properties"]["not_checked_pins"]["items"]["properties"]["reason"]
    for c in node["x-known-codes"]:
        assert c in node["description"]
    produced = {e["reason"] for e in _report(tmp_path, monkeypatch)["not_checked_pins"]}
    assert produced == set(node["x-known-codes"]) == {"OWN_SUPPLY_PIN"}

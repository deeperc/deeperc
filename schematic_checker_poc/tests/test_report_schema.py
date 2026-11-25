"""TODO-558 — report schema 1.0: docs/report_schema.json and the report it describes.

Covers:
  * the schema file itself (a valid draft 2020-12 schema; REPORT_SCHEMA_VERSION
    matches its schema_version pattern);
  * reports built through build_report from in-test fixtures, with rows from the
    real producers where a test can drive them (step_08 run() for signal rows and
    NON_IC_PART disclosures, merged_refdes.disclosures for MERGED_REFDES), validate;
  * what the schema rejects (a status outside the closed list, a poc- version, a
    padded part number, a known not_checked_parts reason missing its own keys) and
    what it lets through (a later 1.x version, an unknown reason code, an unknown
    not_checked_parts reason on the common keys only, a key added later);
  * the reason-code coverage guard: every code each checker can emit is in the
    schema's list for its array, and the list holds nothing else;
  * the commit's report changes: receiver.part_number, receiver.VIH_min_v, part
    numbers written stripped after every lookup.

jsonschema is a dev dependency (requirements-dev.txt). This file imports it at
module level on purpose: without it the run fails loudly instead of skipping.
"""
import copy
import json
import os
import sys
from pathlib import Path

import jsonschema  # noqa: F401  (no importorskip: TODO-558 ruling, item 6)
import pytest

POC = Path(__file__).resolve().parent.parent
REPO = POC.parent
sys.path.insert(0, str(POC))

from llm import ollama_client  # noqa: E402
from steps import merged_refdes, step_08g_pullup_presence, step_10_report  # noqa: E402
from steps.m2_output_conflict import OutputConflictFinding  # noqa: E402
from steps.step_02_parser import (  # noqa: E402
    ComponentIR, MergedRefdes, NetIR, NetlistIR, PinIR,
)
from steps.step_05_validator import ValidatedPinSpec  # noqa: E402
from steps.step_08_checker import CheckResult, SignalUnresolvableReason, run  # noqa: E402
from steps.step_08b_supply_checker import (  # noqa: E402
    SupplyCheckResult, SupplyUnresolvableReason,
)
from steps.step_08c_structural_checker import StructuralCheckResult  # noqa: E402
from steps.step_08d_peripheral_checker import (  # noqa: E402
    FindingReason, PeripheralFinding, PeripheralViolation, Severity,
)
from steps.step_08e_pullup_value_checker import PullupBand, PullupValueFinding  # noqa: E402
from steps.step_08g_pullup_presence import PullupPresenceFinding  # noqa: E402
from steps.peripheral_kb import KBSource  # noqa: E402
from steps.step_10_report import build_report  # noqa: E402

SCHEMA_PATH = REPO / "docs" / "report_schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text())
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)


def _errors(report):
    return sorted((list(e.absolute_path), e.message) for e in VALIDATOR.iter_errors(report))


def _no_llm(monkeypatch):
    def _down(*a, **k):
        raise RuntimeError("no local model in tests")
    monkeypatch.setattr(ollama_client, "generate", _down)


# ── fixtures ───────────────────────────────────────────────────────────────────

def _spec(part, pin, *, abs_max=None, VIH_min=None, VIH_max=None, pin_type="input"):
    return ValidatedPinSpec(
        part_number=part, pin_name=pin, pin_id="1", VIH_max=VIH_max, VIH_min=VIH_min,
        absolute_max_voltage=abs_max, pin_type=pin_type, source_snippet="snippet",
        signal_score=75, confidence="high", is_verified=True, pin_type_exact=True)


def _ic(refdes, part, sig_pins, vcc_net):
    pins = [PinIR(pin_id=str(i + 1), pin_name=name, net=net)
            for i, (name, net) in enumerate(sig_pins)]
    pins.append(PinIR(pin_id=str(len(pins) + 1), pin_name="VCC", net=vcc_net))
    return ComponentIR(refdes=refdes, part_number=part, value="", pins=pins)


def _connector(refdes, part, sig_pins):
    c = ComponentIR(refdes=refdes, part_number=part, value="",
                    pins=[PinIR(pin_id=str(i + 1), pin_name=n, net=net)
                          for i, (n, net) in enumerate(sig_pins)])
    c.libsource_lib = "Connector_Generic"
    return c


def _ir(components, merged=None):
    nets: dict[str, list] = {}
    for c in components:
        for p in c.pins:
            nets.setdefault(p.net, []).append((c.refdes, p.pin_id))
    rails = [n for n in nets if n.startswith("VCC_")]
    ir = NetlistIR(source_file="t", components=components,
                   nets=[NetIR(name=n, pins=ps) for n, ps in nets.items()],
                   power_nets=rails, ground_nets=[])
    if merged is not None:
        ir.merged_refdes = merged
    return ir


CONFIRMED = {"VCC_3V3": 3.3, "VCC_5V": 5.0}


def _signal_rows_and_disclosures():
    """Real step_08 output: one PASS row (U2 has limits), one UNRESOLVABLE row
    (U3 has none), and one NON_IC_PART disclosure (J1, a connector on SIG2).
    The part numbers carry the netlist padding the jetson boards have."""
    drv = _ic("U1", " DRV-PART ", [("PA0", "SIG"), ("PA1", "SIG2")], "VCC_3V3")
    rcv = _ic("U2", "RCV-PART ", [("IN", "SIG")], "VCC_3V3")
    rcv2 = _ic("U3", "RCV2-PART", [("IN", "SIG2")], "VCC_3V3")
    j1 = _connector("J1", " CONN-X ", [("1", "SIG2")])
    ir = _ir([drv, rcv, rcv2, j1])
    specs = {("U2", "IN"): _spec("RCV-PART ", "IN", abs_max=3.6, VIH_min=2.0)}
    out = []
    rows = run(ir, specs, CONFIRMED, not_checked_out=out)
    return ir, rows, out


def _merged_disclosure():
    ir = _ir([_ic("U7", " MEM-A", [("D0", "BUS")], "VCC_3V3")], merged={
        "U7": MergedRefdes(records=2, sheet_paths=["/a/", "/b/"],
                           part_numbers=[" MEM-A", "MEM-B "],
                           collided_pins={"1": ["BUS", "BUS2"]}, test="both"),
        "X9": MergedRefdes(records=0, sheet_paths=[], part_numbers=[],
                           collided_pins={"2": ["N1", "N2"]}, test="pins"),
    })
    return merged_refdes.disclosures(ir)


def _full_report(tmp_path, monkeypatch, **overrides):
    _no_llm(monkeypatch)
    ir, rows, ncp = _signal_rows_and_disclosures()
    ncp = ncp + _merged_disclosure()
    supply = [
        SupplyCheckResult(refdes="U1", part_number=" DRV-PART ", supply_pin_name="VCC",
                          supply_pin_id="3", connected_net="VCC_3V3", actual_voltage=3.3,
                          rated_min=1.8, rated_max=3.6, rated_abs_max=4.0, status="PASS",
                          confidence="high", evidence_label="Confirmed: 3.3V within 1.8V-3.6V",
                          explanation=None),
        SupplyCheckResult(refdes="U3", part_number="RCV2-PART", supply_pin_name="VCC",
                          supply_pin_id="2", connected_net="VCC_X", actual_voltage=None,
                          rated_min=None, rated_max=None, rated_abs_max=None,
                          status="UNRESOLVABLE", confidence="low",
                          evidence_label="Net voltage not confirmed", explanation=None,
                          unresolvable_reason=SupplyUnresolvableReason.NET_VOLTAGE_NOT_CONFIRMED),
    ]
    structural = [StructuralCheckResult(
        refdes="U2", part_number="RCV-PART ", pin_name="VCC", pin_id="2", pin_kind="power",
        connected_net="VCC_3V3", expected_kind="power", status="PASS", confidence="high",
        evidence_label="Confirmed: connected", explanation=None, finding_code=None)]
    peripheral = [PeripheralFinding(
        violation=PeripheralViolation.ROLE_MISMATCH, severity=Severity.UNRESOLVABLE,
        net="/I2C_SDA", pins=["U1.PA0"], evidence="Cannot fully verify net",
        kb_provenance=[KBSource.VENDOR_XML], reason=FindingReason.KB_MISSING)]
    pullup_value = [PullupValueFinding(
        net="/I2C_SCL", refdes="R1", value_str="47k", ohms=47000.0, severity=PullupBand.WARN,
        band="outside 1kΩ–10kΩ", evidence="47k pull-up", pins=["R1", "U1"])]
    output_conflict = [OutputConflictFinding(
        net="/OUT", drivers=[{"refdes": "U1", "pin_id": "1", "pin_name": "PA0",
                              "pintype": "output"},
                             {"refdes": "U2", "pin_id": "1", "pin_name": "IN",
                              "pintype": "output"}],
        evidence_label="Confirmed: two push-pull outputs")]
    presence = [PullupPresenceFinding(
        net="/SD_CMD", family="sd", violation="SD_LINE_NO_PULLUP", pins=["U1.PA1"],
        evidence="no pull-up", severity="UNRESOLVABLE",
        reason=step_08g_pullup_presence.REASON_PULL_PATH_INDETERMINATE)]
    # Keyed as the netlist wrote the part number (padded); one entry is
    # cache_drift and one staged, so drift_warning and staged_cache_notice appear.
    metadata = {
        " DRV-PART ": {"resolved": True, "cache_currency": "stale", "cache_tier": "staged",
                       "cache_stem": "drv-part"},
        "RCV-PART ": {"resolved": True, "cache_currency": "current"},
    }
    kwargs = dict(
        source_netlist="t.net", results=rows, confirmed_voltages=dict(CONFIRMED),
        extraction_metadata=metadata, output_path=str(tmp_path / "report.json"),
        components=ir.components, supply_results=supply, structural_results=structural,
        peripheral_results=peripheral, pullup_results=pullup_value,
        output_conflict_results=output_conflict, pullup_presence_results=presence,
        rail_candidates=[], not_checked_parts=ncp)
    kwargs.update(overrides)
    return build_report(**kwargs)


# ── the schema file ────────────────────────────────────────────────────────────

def test_schema_file_is_a_valid_draft_2020_12_schema():
    assert SCHEMA["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    jsonschema.Draft202012Validator.check_schema(SCHEMA)


def test_report_schema_version_matches_the_schema_pattern():
    assert step_10_report.REPORT_SCHEMA_VERSION == "1.0"
    pattern = SCHEMA["properties"]["schema_version"]["pattern"]
    assert jsonschema.Draft202012Validator({"pattern": pattern}).is_valid("1.0")


def test_status_enum_is_closed_and_named_per_array():
    assert SCHEMA["$defs"]["status"] == {"enum": ["PASS", "WARN", "FAIL", "UNRESOLVABLE"]}
    props = SCHEMA["properties"]
    by_status = ["results", "power_supply_results", "structural_integrity_results",
                 "output_conflict_results"]
    by_severity = ["peripheral_integrity_results", "pullup_value_results",
                   "pullup_presence_results"]
    for name in by_status:
        assert props[name]["items"]["properties"]["status"]["$ref"] == "#/$defs/status"
        assert "status" in props[name]["items"]["required"]
    for name in by_severity:
        assert props[name]["items"]["properties"]["severity"]["$ref"] == "#/$defs/status"
        assert "status" not in props[name]["items"]["properties"]
    oc_sev = props["output_conflict_results"]["items"]["properties"]["severity"]
    assert oc_sev == {"type": "string", "description": oc_sev["description"]}


# ── reports from build_report validate ─────────────────────────────────────────

def test_empty_report_validates(tmp_path, monkeypatch):
    _no_llm(monkeypatch)
    rep = build_report(source_netlist="", results=[], confirmed_voltages={},
                       extraction_metadata={}, output_path=str(tmp_path / "r.json"))
    assert _errors(rep) == []
    on_disk = json.loads((tmp_path / "r.json").read_text())
    assert _errors(on_disk) == []


def test_full_report_validates_and_covers_every_array(tmp_path, monkeypatch):
    rep = _full_report(tmp_path, monkeypatch)
    assert _errors(rep) == []
    assert _errors(json.loads((tmp_path / "report.json").read_text())) == []
    for key in ("results", "power_supply_results", "structural_integrity_results",
                "peripheral_integrity_results", "pullup_value_results",
                "output_conflict_results", "pullup_presence_results"):
        assert rep[key], key
    assert {e["reason"] for e in rep["not_checked_parts"]} == {"NON_IC_PART", "MERGED_REFDES"}
    assert {r["status"] for r in rep["results"]} == {"PASS", "UNRESOLVABLE"}
    assert "drift_warning" in rep and "staged_cache_notice" in rep
    assert any(r.get("cache_tier") == "staged" for r in rep["results"]
               + rep["power_supply_results"])


# ── what the schema rejects and lets through ───────────────────────────────────

@pytest.fixture
def report(tmp_path, monkeypatch):
    return _full_report(tmp_path, monkeypatch)


def _mut(report, fn):
    r = copy.deepcopy(report)
    fn(r)
    return _errors(r)


def test_rejects_a_status_outside_the_closed_list(report):
    assert _mut(report, lambda r: r["results"][0].__setitem__("status", "MAYBE"))
    assert _mut(report, lambda r: r["peripheral_integrity_results"][0].__setitem__("severity", "INFO"))
    assert _mut(report, lambda r: r["output_conflict_results"][0].__setitem__("status", "critical"))


def test_output_conflict_severity_is_an_open_string(report):
    assert _mut(report, lambda r: r["output_conflict_results"][0].__setitem__("severity", "major")) == []


@pytest.mark.parametrize("version, ok", [
    ("1.0", True), ("1.1", True), ("1.12", True),
    ("poc-1.6", False), ("2.0", False), ("1", False), ("1.0.1", False), ("1.0\n", False)])
def test_schema_version_pattern(report, version, ok):
    assert (_mut(report, lambda r: r.__setitem__("schema_version", version)) == []) is ok


def test_rejects_a_padded_part_number(report):
    assert _mut(report, lambda r: r["results"][0]["driver"].__setitem__("part_number", " X"))
    assert _mut(report, lambda r: r["results"][0]["receiver"].__setitem__("part_number", "X "))
    assert _mut(report, lambda r: r["power_supply_results"][0].__setitem__("part_number", "X\t"))
    assert _mut(report, lambda r: r["structural_integrity_results"][0].__setitem__("part_number", "X\n"))
    assert _mut(report, lambda r: r["results"][0]["driver"].__setitem__("part_number", "A B")) == []


def test_unknown_reason_codes_validate_and_badly_shaped_codes_do_not(report):
    assert _mut(report, lambda r: r["results"][1].__setitem__("unresolvable_reason", "A_CODE_FROM_1_7")) == []
    assert _mut(report, lambda r: r["results"][1].__setitem__("unresolvable_reason", "free text"))


def test_not_checked_parts_entry_shapes(report):
    ncp = {e["reason"]: i for i, e in enumerate(report["not_checked_parts"])}

    def drop(reason, key):
        return lambda r: r["not_checked_parts"][ncp[reason]].pop(key)
    assert _mut(report, drop("NON_IC_PART", "nets"))
    assert _mut(report, drop("NON_IC_PART", "pins_with_rows"))
    assert _mut(report, drop("MERGED_REFDES", "collided_pins"))
    assert _mut(report, drop("MERGED_REFDES", "refdes"))
    unknown = {"refdes": "Y1", "part_number": None, "checker": "supply", "reason": "STRAPPED_PIN"}
    assert _mut(report, lambda r: r["not_checked_parts"].append(unknown)) == []
    assert _mut(report, lambda r: r["not_checked_parts"].append(
        {"refdes": "Y1", "part_number": None, "checker": "signal"}))


def test_additive_keys_validate(report):
    def add(r):
        r["a_key_from_1_3"] = {"x": 1}
        r["summary"]["new_count"] = 4
        r["results"][0]["driver"]["new_field"] = True
        r["not_checked_pins"] = [{"refdes": "U1", "part_number": "P", "pin_name": "BOOT0",
                                  "net": "GND", "checker": "signal", "reason": "STRAPPED"}]
    assert _mut(report, add) == []


def test_extraction_metadata_and_provenance_contents_are_not_covered(report):
    def scramble(r):
        r["extraction_metadata"] = {" padded key ": {"anything": [1, "x"]}}
        r["provenance"] = {"whatever": None}
    assert _mut(report, scramble) == []
    assert _mut(report, lambda r: r.pop("extraction_metadata")) == []
    assert _mut(report, lambda r: r.pop("not_checked_parts"))


# ── reason-code coverage (every reason set, TODO-558 ruling item 5) ────────────

def _known(array, key):
    node = SCHEMA["properties"][array]["items"]["properties"][key]
    codes = node["x-known-codes"]
    for c in codes:
        assert c in node["description"], (array, key, c)
    return set(codes)


def _ncp_known():
    node = SCHEMA["properties"]["not_checked_parts"]["items"]["properties"]["reason"]
    for c in node["x-known-codes"]:
        assert c in node["description"]
    return set(node["x-known-codes"])


def test_signal_codes_match_the_enum():
    assert _known("results", "unresolvable_reason") == {m.value for m in SignalUnresolvableReason}


def test_supply_codes_match_the_enum():
    assert (_known("power_supply_results", "unresolvable_reason")
            == {m.value for m in SupplyUnresolvableReason})


def test_peripheral_codes_match_the_enum():
    assert _known("peripheral_integrity_results", "reason") == {m.value for m in FindingReason}


def test_pullup_presence_codes_match_the_constants():
    assert _known("pullup_presence_results", "reason") == {
        step_08g_pullup_presence.REASON_PULL_PATH_INDETERMINATE,
        step_08g_pullup_presence.REASON_MERGED_REFDES}


def test_pullup_value_codes_match_the_constant():
    # step_08e sets its reason only through merged_refdes (TODO-569).
    assert _known("pullup_value_results", "reason") == {merged_refdes.MERGED_REFDES}


def _same_instance_disclosure():
    """TODO-556 decision 16: U1 is both endpoints of SELF, so step_08 emits no row
    there and one SAME_INSTANCE entry."""
    u1 = _ic("U1", "SELF-PART", [("PA0", "SELF"), ("PA1", "SELF")], "VCC_3V3")
    out = []
    assert run(_ir([u1]), {}, CONFIRMED, not_checked_out=out) == []
    return out


def test_not_checked_parts_reasons_match_the_producers():
    _, _, ncp = _signal_rows_and_disclosures()
    produced = {e["reason"] for e in ncp + _merged_disclosure() + _same_instance_disclosure()}
    assert produced == _ncp_known() == {"NON_IC_PART", merged_refdes.MERGED_REFDES,
                                        "SAME_INSTANCE"}


def test_every_reason_code_matches_the_code_pattern():
    import re
    pattern = re.compile(SCHEMA["$defs"]["reason_code"]["pattern"])
    every = (_known("results", "unresolvable_reason")
             | _known("power_supply_results", "unresolvable_reason")
             | _known("peripheral_integrity_results", "reason")
             | _known("pullup_presence_results", "reason")
             | _known("pullup_value_results", "reason") | _ncp_known())
    assert every and all(pattern.match(c) for c in every)


# ── the commit's report changes ────────────────────────────────────────────────

def test_receiver_part_number_and_vih_min(report):
    pass_row = next(r for r in report["results"] if r["status"] == "PASS")
    assert pass_row["receiver"]["refdes"] == "U2"
    assert pass_row["receiver"]["part_number"] == "RCV-PART"
    assert pass_row["receiver"]["VIH_min_v"] == 2.0
    assert "VIH_max_v" in pass_row["receiver"]
    assert list(pass_row["receiver"]) == ["refdes", "part_number", "pin_name", "VIH_min_v",
                                          "VIH_max_v", "absolute_max_v"]


def test_part_numbers_written_stripped_after_lookup(report):
    pass_row = next(r for r in report["results"] if r["status"] == "PASS")
    assert pass_row["driver"]["part_number"] == "DRV-PART"
    # the receiver's tier was looked up under its padded key ("current")
    assert pass_row["evidence_tier"] == "confirmed_local"
    sup = report["power_supply_results"][0]
    assert sup["part_number"] == "DRV-PART"
    # ...and the supply row's cache (padded key, "stale", staged) was still found
    assert sup["evidence_tier"] == "cache_drift" and sup["cache_tier"] == "staged"
    assert report["structural_integrity_results"][0]["part_number"] == "RCV-PART"
    j1 = next(e for e in report["not_checked_parts"] if e["refdes"] == "J1")
    assert j1["part_number"] == "CONN-X"
    u7 = next(e for e in report["not_checked_parts"] if e["refdes"] == "U7")
    assert u7["part_number"] == "MEM-A" and u7["part_numbers"] == ["MEM-A", "MEM-B"]
    x9 = next(e for e in report["not_checked_parts"] if e["refdes"] == "X9")
    assert x9["part_number"] is None
    # extraction_metadata keys keep the netlist's string (not covered, lookup keys)
    assert set(report["extraction_metadata"]) == {" DRV-PART ", "RCV-PART "}


def test_stripping_does_not_mutate_the_producer_entries(tmp_path, monkeypatch):
    _no_llm(monkeypatch)
    entries = _merged_disclosure()
    before = copy.deepcopy(entries)
    build_report(source_netlist="", results=[], confirmed_voltages={},
                 extraction_metadata={}, output_path=str(tmp_path / "r.json"),
                 not_checked_parts=entries)
    assert entries == before


def test_unpadded_not_checked_entry_is_written_unchanged(tmp_path, monkeypatch):
    _no_llm(monkeypatch)
    entry = {"refdes": "J1", "part_number": "X", "part_class": "connector",
             "class_evidence": "provenance", "checker": "signal", "reason": "NON_IC_PART",
             "nets": ["SIG"], "pins_with_rows": 0}
    rep = build_report(source_netlist="", results=[], confirmed_voltages={},
                       extraction_metadata={}, output_path=str(tmp_path / "r.json"),
                       not_checked_parts=[entry])
    assert rep["not_checked_parts"] == [entry]

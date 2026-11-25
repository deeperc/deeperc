"""TODO-556 decision 27: step_08b gives a part classed connector no supply rows and
discloses it once in not_checked_parts as NON_IC_PART with checker "supply"
(TODO-554's non-IC rule, applied to the supply checker)."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import jsonschema

POC = Path(__file__).resolve().parent.parent
REPO = POC.parent
sys.path.insert(0, str(POC))

from llm import ollama_client  # noqa: E402
from steps import checker_registry, step_08b_supply_checker as sc  # noqa: E402
from steps.step_02_parser import ComponentIR, NetIR, NetlistIR, PinIR  # noqa: E402
from steps.step_10_report import build_report  # noqa: E402

VALIDATOR = jsonschema.Draft202012Validator(
    json.loads((REPO / "docs" / "report_schema.json").read_text()))


def _conn(refdes="M7", lib="Connector_Generic"):
    c = ComponentIR(refdes=refdes, part_number="MICROMOD-2222-C", value="",
                    pins=[PinIR(pin_id="1", pin_name="VIN", net="VIN"),
                          PinIR(pin_id="2", pin_name="3.3V", net="+3V3"),
                          PinIR(pin_id="3", pin_name="SDA", net="SDA")])
    c.libsource_lib = lib
    return c


def _ic():
    return ComponentIR(refdes="U1", part_number="LDO-1", value="",
                       pins=[PinIR(pin_id="1", pin_name="VIN", net="VIN")])


def _ir(components):
    nets: dict[str, list] = {}
    for c in components:
        for p in c.pins:
            nets.setdefault(p.net, []).append((c.refdes, p.pin_id))
    return NetlistIR(source_file="t", components=components,
                     nets=[NetIR(name=n, pins=ps) for n, ps in nets.items()],
                     power_nets=[], ground_nets=[])


def test_connector_gets_no_supply_rows_and_one_disclosure():
    out = []
    rows = sc.check_component_supplies([_conn(), _ic()], {}, {"VIN": 5.0}, {},
                                       not_checked_out=out)
    assert [(r.refdes, r.supply_pin_name) for r in rows] == [("U1", "VIN")]
    assert out == [{"refdes": "M7", "part_number": "MICROMOD-2222-C",
                    "part_class": "connector", "class_evidence": "library",
                    "checker": "supply", "reason": "NON_IC_PART", "nets": ["VIN"],
                    "pins_with_rows": 0}]


def test_connector_by_refdes_provenance_is_skipped_too():
    c = _conn(refdes="J3", lib="")
    out = []
    assert sc.check_component_supplies([c], {}, {"VIN": 5.0}, {}, not_checked_out=out) == []
    assert out[0]["class_evidence"] == "provenance"


def test_connector_without_a_supply_pin_is_not_disclosed():
    c = ComponentIR(refdes="J2", part_number="HDR", value="",
                    pins=[PinIR(pin_id="1", pin_name="SDA", net="SDA")])
    c.libsource_lib = "Connector_Generic"
    out = []
    assert sc.check_component_supplies([c], {}, {}, {}, not_checked_out=out) == []
    assert out == []


def test_rows_drop_without_a_disclosure_list():
    assert sc.check_component_supplies([_conn()], {}, {"VIN": 5.0}) == []


def test_supply_adapter_appends_after_the_signal_entries():
    ctx = SimpleNamespace(ir=_ir([_conn(), _ic()]), seen_parts={}, confirmed_voltages={"VIN": 5.0},
                          derived_provenance={}, not_checked_pins=[],
                          not_checked_parts=[{"reason": "NON_IC_PART", "checker": "signal"}])
    spec = next(s for s in checker_registry.REGISTRY if s.name == "supply")
    rows = spec.run(ctx)
    assert [r.refdes for r in rows] == ["U1"]
    assert [e["checker"] for e in ctx.not_checked_parts] == ["signal", "supply"]


def test_report_with_a_supply_disclosure_validates(tmp_path, monkeypatch):
    monkeypatch.setattr(ollama_client, "generate",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no model")))
    out = []
    rows = sc.check_component_supplies([_conn(), _ic()], {}, {"VIN": 5.0}, {}, not_checked_out=out)
    rep = build_report(source_netlist="", results=[], confirmed_voltages={"VIN": 5.0},
                       extraction_metadata={}, output_path=str(tmp_path / "r.json"),
                       supply_results=rows, not_checked_parts=out)
    assert list(VALIDATOR.iter_errors(rep)) == []
    assert rep["not_checked_parts"][0]["checker"] == "supply"

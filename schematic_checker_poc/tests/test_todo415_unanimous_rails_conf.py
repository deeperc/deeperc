"""TODO-415 Rulings decision 19 — a driver whose confirmed supply pins all show
one voltage ("unanimous_rails") has driver confidence high, not low. Confidence
only: no status or reason depends on it."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from steps.step_02_parser import ComponentIR, NetIR, NetlistIR, PinIR  # noqa: E402
from steps.step_05_validator import ValidatedPinSpec  # noqa: E402
from steps.step_08_checker import (  # noqa: E402
    RailProvenance,
    _RESOLUTION_CONF,
    _driver_confidence,
    run,
)

CONFIRMED = {"+3V3": 3.3, "VDDA": 3.3}


def test_table_entry():
    assert _RESOLUTION_CONF == {"single_vcc": "high", "unanimous_rails": "high",
                                "pin_supply_map": "high"}


def test_driver_confidence_unanimous_rails():
    assert _driver_confidence(RailProvenance("deterministic", "high"), "unanimous_rails") == "high"
    # weakest link still applies: the rail's own confidence caps it
    assert _driver_confidence(RailProvenance("gemma", "medium"), "unanimous_rails") == "medium"
    assert _driver_confidence(RailProvenance("deterministic", "low"), "unanimous_rails") == "low"
    assert _driver_confidence(None, "unanimous_rails") == "low"
    assert _driver_confidence(RailProvenance("deterministic", "high"), "not_a_resolution") == "low"


def _board():
    def c(refdes, pins):
        return ComponentIR(refdes=refdes, part_number=f"PN_{refdes}", value=f"PN_{refdes}",
                           pins=[PinIR(pin_id=i, pin_name=n, net=net) for i, n, net in pins])
    comps = [c("U1", [("1", "IO", "SIG"), ("2", "VDD", "+3V3"), ("3", "VDDA", "VDDA")]),
             c("U2", [("1", "IN", "SIG")])]
    nets = [NetIR(name="SIG", pins=[("U1", "1"), ("U2", "1")]),
            NetIR(name="+3V3", pins=[("U1", "2")]), NetIR(name="VDDA", pins=[("U1", "3")])]
    return NetlistIR(source_file="test", components=comps, nets=nets,
                     power_nets=["+3V3", "VDDA"], ground_nets=[])


def _row(prov):
    spec = ValidatedPinSpec(
        part_number="X", pin_name="IN", pin_id="1", VIH_max=None, VIH_min=None,
        absolute_max_voltage=3.6, pin_type="digital_input", source_snippet="snippet",
        signal_score=75, confidence="high", is_verified=True, pin_type_exact=True,
        open_drain=False, expr_notes=[])
    rows = [r for r in run(_board(), {("U2", "IN"): spec}, CONFIRMED, prov)
            if r.net_name == "SIG"]
    assert len(rows) == 1
    return rows[0]


def test_run_unanimous_rails_driver_reads_high():
    r = _row({n: RailProvenance("deterministic", "high") for n in CONFIRMED})
    assert (r.status, r.driver_refdes, r.driver_voltage) == ("PASS", "U1", 3.3)
    assert r.driver_confidence == "high" and r.combined_confidence == "high"


def test_run_low_confidence_rail_keeps_the_row_low():
    r = _row({n: RailProvenance("deterministic", "low") for n in CONFIRMED})
    assert r.status == "PASS" and r.driver_confidence == "low"

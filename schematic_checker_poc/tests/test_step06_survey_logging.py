"""STEP_06_DEBUG survey-logging instrumentation tests.

Verifies the flag-gated per-net trace logger in step_06_power:
  - with STEP_06_DEBUG=1, infer_power_nets writes one JSONL line per net with
    the expected classification path;
  - with the flag unset, nothing is written (zero-cost-when-off).

Post-Fix-γ: Step 06 is fully deterministic (no Gemma). The former gemma_called
path is replaced by unclassified_highfanout for high-fanout, non-signal nets the
name rules can't classify.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from steps import step_06_power
from steps.step_02_parser import ComponentIR, NetIR, NetlistIR, PinIR

LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "logs")


def _build_ir():
    """Minimal netlist exercising one net per terminal decision path:
      GND                  → tier1_ground
      +3V3 (fanout 5)      → tier1_power_known
      /Boost Converter Vin → unclassified_highfanout (high-fanout, non-signal,
                             no Tier-1 match, no Gemma fallback)
      /GPIO12 (fanout 4)   → signal_filtered
      /LED (fanout 2)      → low_fanout_skip
    """
    def net(name, n):
        return NetIR(name=name, pins=[(f"U1", str(i)) for i in range(n)])

    nets = [
        net("GND", 6),
        net("+3V3", 5),
        net("/Boost Converter Vin", 4),
        net("/GPIO12", 4),
        net("/LED", 2),
    ]
    comp = ComponentIR(refdes="U1", part_number="GENERIC", value="", pins=[
        PinIR(pin_id=str(i), pin_name="P", net="") for i in range(6)
    ])
    return NetlistIR(source_file="/tmp/board_under_test.net",
                     components=[comp], nets=nets)


def _trace_files():
    if not os.path.isdir(LOG_DIR):
        return set()
    return {f for f in os.listdir(LOG_DIR)
            if f.startswith("step_06_trace_") and f.endswith(".jsonl")}


def test_survey_logging_on(monkeypatch):
    monkeypatch.setenv("STEP_06_DEBUG", "1")
    step_06_power._SURVEY_FH = None  # force a fresh run-scoped file

    before = _trace_files()
    step_06_power.infer_power_nets(_build_ir())
    if step_06_power._SURVEY_FH is not None:
        step_06_power._SURVEY_FH.flush()
    new = _trace_files() - before
    assert len(new) == 1, f"expected exactly one new trace file, got {new}"

    path = os.path.join(LOG_DIR, new.pop())
    with open(path, encoding="utf-8") as fh:
        recs = [json.loads(line) for line in fh if line.strip()]

    by_net = {r["net"]: r for r in recs}
    assert len(recs) == 5, f"expected 5 per-net records, got {len(recs)}"
    assert by_net["GND"]["path"] == "tier1_ground"
    assert by_net["+3V3"]["path"] == "tier1_power_known"
    assert by_net["/GPIO12"]["path"] == "signal_filtered"
    assert by_net["/LED"]["path"] == "low_fanout_skip"

    u = by_net["/Boost Converter Vin"]
    assert u["path"] == "unclassified_highfanout"
    assert u["board"] == "board_under_test.net"
    assert "U1" in u["refdes"]
    # The removed gemma sub-object is gone; the fallthrough record carries the
    # exemption flags instead.
    assert "gemma" not in u
    assert u["voltage_prefix_exempt"] is False
    assert u["synthetic_rail_rescue"] is False

    os.remove(path)  # don't leave test artifacts behind


def test_survey_logging_off(monkeypatch):
    monkeypatch.delenv("STEP_06_DEBUG", raising=False)
    step_06_power._SURVEY_FH = None

    before = _trace_files()
    step_06_power.infer_power_nets(_build_ir())
    assert _trace_files() == before, "no trace file should be written when flag is off"

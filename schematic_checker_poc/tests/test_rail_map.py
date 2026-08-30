"""TODO-134 — confirmed rail-map engine tier tests (loader + step_06 injection)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from steps import rail_map as rm
from steps.step_06_power import infer_power_nets
from steps.step_02_parser import ComponentIR, NetIR, NetlistIR, PinIR


def _ir(*net_names, n_pins=4):
    comp = ComponentIR(refdes="U1", part_number="GENERIC", value="",
                       pins=[PinIR(pin_id=str(i), pin_name="P", net="")
                             for i in range(n_pins * len(net_names))])
    nets = []
    k = 0
    for name in net_names:
        nets.append(NetIR(name=name, pins=[("U1", str(k + i)) for i in range(n_pins)]))
        k += n_pins
    return NetlistIR(source_file="/tmp/t.net", components=[comp], nets=nets)


# ── Loader / validation ──────────────────────────────────────────────────────

def test_load_valid_sidecar(tmp_path):
    net = tmp_path / "b.net"; net.write_text("x")
    (tmp_path / "b.net.rails.json").write_text(json.dumps({
        "Net-(D2-K)": {"voltage": 12.0},
        "Spare1": {"is_rail": False},
        "GND_SENSE": {"is_ground": True},
        "VBAT_RAW": {"voltage": None},
    }))
    m = rm.load_rail_map(str(net))
    assert m["Net-(D2-K)"] == {"voltage": 12.0, "is_rail": True, "is_ground": False}
    assert m["Spare1"]["is_rail"] is False
    assert m["GND_SENSE"]["is_ground"] is True
    assert m["VBAT_RAW"]["voltage"] is None


def test_no_map_returns_none(tmp_path):
    net = tmp_path / "b.net"; net.write_text("x")
    assert rm.load_rail_map(str(net)) is None


def test_explicit_overrides_sidecar(tmp_path):
    net = tmp_path / "b.net"; net.write_text("x")
    (tmp_path / "b.net.rails.json").write_text(json.dumps({"A": {"voltage": 1.0}}))
    other = tmp_path / "other.json"; other.write_text(json.dumps({"B": {"voltage": 2.0}}))
    m = rm.load_rail_map(str(net), explicit_path=str(other))
    assert "B" in m and "A" not in m


def test_malformed_map_is_hard_error(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(["not", "an", "object"]))
    with pytest.raises(rm.RailMapError):
        rm.load_rail_map(None, explicit_path=str(bad))


def test_bad_voltage_type_errors(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"A": {"voltage": "twelve"}}))
    with pytest.raises(rm.RailMapError):
        rm.load_rail_map(None, explicit_path=str(bad))


def test_bad_is_rail_type_errors(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"A": {"is_rail": "no"}}))
    with pytest.raises(rm.RailMapError):
        rm.load_rail_map(None, explicit_path=str(bad))


def test_missing_explicit_errors():
    with pytest.raises(rm.RailMapError):
        rm.load_rail_map(None, explicit_path="/no/such/map.json")


# ── Normalization / key matching ─────────────────────────────────────────────

def test_basename_key_matches_hierarchical_net():
    idx = rm.build_index({"CAN12V_prot": {"voltage": 12.0, "is_rail": True, "is_ground": False}})
    decl, key = rm.lookup(idx, "/CAN bus/CAN12V_prot")
    assert decl is not None and key == "CAN12V_prot"


def test_full_hierarchical_key_matches():
    idx = rm.build_index({"/Expansion connector/+3V3_AUX":
                          {"voltage": 3.3, "is_rail": True, "is_ground": False}})
    decl, _ = rm.lookup(idx, "/Expansion connector/+3V3_AUX")
    assert decl and decl["voltage"] == 3.3


# ── step_06 injection ────────────────────────────────────────────────────────

def _map(d):
    return {k: {"voltage": v.get("voltage"), "is_rail": v.get("is_rail", True),
                "is_ground": v.get("is_ground", False)} for k, v in d.items()}


def test_user_confirmed_introduces_unrecognized_net():
    # Net-(D2-K) is a topology net Tier-1 never classifies — only the map can add it.
    ir = _ir("Net-(D2-K)")
    rails, grounds = infer_power_nets(ir, rail_map=_map({"Net-(D2-K)": {"voltage": 12.0}}))
    hit = [r for r in rails if r.net_name == "Net-(D2-K)"]
    assert hit and hit[0].voltage_v == 12.0 and hit[0].source == "user_confirmed"


def test_user_confirmed_precedence_and_conflict():
    # +12V Tier-1-infers 12V; user declares 3.3 → user wins, conflict surfaced.
    ir = _ir("+12V")
    rails, _ = infer_power_nets(ir, rail_map=_map({"+12V": {"voltage": 3.3}}))
    hit = [r for r in rails if r.net_name == "+12V"]
    assert hit and hit[0].voltage_v == 3.3 and hit[0].source == "user_confirmed"
    conflicts = ir.rail_map_conflicts
    assert any(c["net"] == "+12V" and c["kind"] == "conflict" for c in conflicts)


def test_is_rail_false_unskips_and_unmasks():
    # +5V would be a Tier-1 rail; declaring not-a-rail leaves it UNCLASSIFIED
    # (so step_08 will not skip it) and surfaces an unmask note.
    ir = _ir("+5V")
    rails, grounds = infer_power_nets(ir, rail_map=_map({"+5V": {"is_rail": False}}))
    assert all(r.net_name != "+5V" for r in rails)
    assert "+5V" not in grounds
    assert "+5V" in ir.rail_map_nonrail
    assert any(c["net"] == "+5V" and c["kind"] == "unmask" for c in ir.rail_map_conflicts)


def test_is_ground_routes_to_ground():
    ir = _ir("MY_RETURN")
    rails, grounds = infer_power_nets(ir, rail_map=_map({"MY_RETURN": {"is_ground": True}}))
    assert "MY_RETURN" in grounds
    assert all(r.net_name != "MY_RETURN" for r in rails)


def test_unmatched_key_warns(capsys):
    ir = _ir("VCC")
    infer_power_nets(ir, rail_map=_map({"NOSUCHNET": {"voltage": 1.0}}))
    out = capsys.readouterr().out
    assert "matched no net" in out and "NOSUCHNET" in out


def test_integration_warn_clears_nonrail_retains_unskip():
    """With-map fixture (STEP 5.2): step_06 → step_08c. A declared rail clears its
    structural WARN; an is_rail:false net KEEPS its WARN and stays OUT of
    confirmed_voltages (so step_08 will signal-check it = the un-skip)."""
    from steps import step_08c_structural_checker as sc
    comp = ComponentIR(refdes="U1", part_number="X", value="", pins=[
        PinIR(pin_id="1", pin_name="VCC", net="Net-(D2-K)"),
        PinIR(pin_id="2", pin_name="VDD", net="Spare1"),
        PinIR(pin_id="3", pin_name="IO", net="Net-(D2-K)"),
        PinIR(pin_id="4", pin_name="IO2", net="Spare1"),
        PinIR(pin_id="5", pin_name="GND", net="MY_RTN"),
        PinIR(pin_id="6", pin_name="IO3", net="MY_RTN"),
    ])
    nets = [NetIR(name="Net-(D2-K)", pins=[("U1", "1"), ("U1", "3")]),
            NetIR(name="Spare1", pins=[("U1", "2"), ("U1", "4")]),
            NetIR(name="MY_RTN", pins=[("U1", "5"), ("U1", "6")])]
    ir = NetlistIR(source_file="/tmp/t.net", components=[comp], nets=nets)
    rails, grounds = infer_power_nets(ir, rail_map=_map({
        "Net-(D2-K)": {"voltage": 12.0},
        "Spare1": {"is_rail": False},
        "MY_RTN": {"is_ground": True}}))
    confirmed = {r.net_name: r.voltage_v for r in rails}
    res = sc.check_structural_integrity([comp], confirmed, grounds, nets)
    status = {r.connected_net: r.status for r in res}
    assert status["Net-(D2-K)"] == "PASS"   # declared rail → WARN cleared
    assert status["Spare1"] == "WARN"        # not-a-rail → WARN retained
    assert status["MY_RTN"] == "PASS"        # declared ground → routed/PASS
    # un-skip: declared rail enters confirmed_voltages (step_08 skips it); the
    # not-a-rail net does NOT (step_08 will signal-check it).
    assert "Net-(D2-K)" in confirmed and "Spare1" not in confirmed
    assert "MY_RTN" in grounds


def test_inert_when_no_map():
    # Without a map the classifier is byte-identical to baseline behavior.
    ir = _ir("+3V3", "Net-(D2-K)")
    rails, grounds = infer_power_nets(ir)  # no rail_map
    names = {r.net_name for r in rails}
    assert "+3V3" in names               # Tier-1 still classifies
    assert "Net-(D2-K)" not in names      # topology net still unclassified
    assert ir.rail_map_conflicts == [] and ir.rail_map_nonrail == []

from steps.step_02_parser import ComponentIR, PinIR, NetIR, NetlistIR
from steps.step_05_validator import ValidatedPinSpec
from steps.m2_output_conflict import (
    classify_netlist,
    check_output_conflicts,
    VIOLATION_OUTPUT_CONFLICT,
    CLASSIFICATION_WOULD_FAIL,
    CLASSIFICATION_CLEAN,
    CLASSIFICATION_UNRESOLVABLE,
    CLASSIFICATION_SKIPPED_POWER_GROUND,
    CLASSIFICATION_SKIPPED_BIDIR_TRISTATE,
    REASON_GENUINE_MULTIDRIVER,
    REASON_SAME_REFDES_DEDUP,
    REASON_OPEN_DRAIN_VETO,
    REASON_BIDIR_TRISTATE_PRESENT,
    REASON_POWER_GROUND,
)


def _spec(part_number, pin_name, pin_id, pin_type="output", open_drain=False):
    return ValidatedPinSpec(
        part_number=part_number, pin_name=pin_name, pin_id=pin_id,
        VIH_max=None, VIH_min=None, absolute_max_voltage=None,
        pin_type=pin_type, source_snippet=None, signal_score=0,
        confidence="high", is_verified=True,
        pin_type_exact=True, open_drain=open_drain,
    )


def _comp(refdes, part_number, pins):
    """pins: list of (pin_id, pin_name, net)."""
    return ComponentIR(
        refdes=refdes, part_number=part_number, value=part_number,
        pins=[PinIR(pin_id=pid, pin_name=pname, net=net) for pid, pname, net in pins],
    )


def test_two_pushpull_drivers_would_fail():
    """Two distinct-refdes pins both pintype=output on one net -> WOULD_FAIL."""
    net = NetIR(name="SIGNAL_CONFLICT", pins=[("U1", "1"), ("U2", "1")])
    comp1 = _comp("U1", "74HC595", [("1", "QA", "SIGNAL_CONFLICT")])
    comp2 = _comp("U2", "74HC595", [("1", "QB", "SIGNAL_CONFLICT")])
    ir = NetlistIR(
        source_file="t", components=[comp1, comp2], nets=[net],
        power_nets=[], ground_nets=[],
        pintypes={("U1", "1"): "output", ("U2", "1"): "output"},
    )
    results = classify_netlist(ir, pin_specs={})
    r = next(x for x in results if x.net_name == "SIGNAL_CONFLICT")
    assert r.classification == CLASSIFICATION_WOULD_FAIL
    assert r.reason == REASON_GENUINE_MULTIDRIVER
    assert {d.refdes for d in r.drivers} == {"U1", "U2"}


def test_open_drain_veto_clears_to_clean():
    """AP22615A/SIC431AED shape: 2 pins pintype=output but both datasheet-flagged
    open_drain -> CLEAN, not WOULD_FAIL."""
    net = NetIR(name="CSIA_FLG", pins=[("U44", "3"), ("U45", "3")])
    comp1 = _comp("U44", "AP22615A", [("3", "FLG", "CSIA_FLG")])
    comp2 = _comp("U45", "AP22615A", [("3", "FLG", "CSIA_FLG")])
    ir = NetlistIR(
        source_file="t", components=[comp1, comp2], nets=[net],
        power_nets=[], ground_nets=[],
        pintypes={("U44", "3"): "output", ("U45", "3"): "output"},
    )
    pin_specs = {
        ("U44", "FLG"): _spec("AP22615A", "FLG", "3", open_drain=True),
        ("U45", "FLG"): _spec("AP22615A", "FLG", "3", open_drain=True),
    }
    results = classify_netlist(ir, pin_specs=pin_specs)
    r = next(x for x in results if x.net_name == "CSIA_FLG")
    assert r.classification == CLASSIFICATION_CLEAN
    assert r.reason == REASON_OPEN_DRAIN_VETO
    assert len(r.vetoed_pins) == 2


def test_bidirectional_pin_skips_net():
    """Any bidirectional/tri_state/open_collector/open_emitter pin on the net
    -> whole net skipped, no WOULD_FAIL finding regardless of driver count."""
    net = NetIR(name="SHARED_BUS", pins=[("U1", "1"), ("U2", "1"), ("U3", "1")])
    comp1 = _comp("U1", "IC1", [("1", "D0", "SHARED_BUS")])
    comp2 = _comp("U2", "IC2", [("1", "D0", "SHARED_BUS")])
    comp3 = _comp("U3", "IC3", [("1", "D0", "SHARED_BUS")])
    ir = NetlistIR(
        source_file="t", components=[comp1, comp2, comp3], nets=[net],
        power_nets=[], ground_nets=[],
        pintypes={("U1", "1"): "output", ("U2", "1"): "output", ("U3", "1"): "tri_state"},
    )
    results = classify_netlist(ir, pin_specs={})
    r = next(x for x in results if x.net_name == "SHARED_BUS")
    assert r.classification == CLASSIFICATION_SKIPPED_BIDIR_TRISTATE
    assert r.reason == REASON_BIDIR_TRISTATE_PRESENT


def test_same_refdes_multipin_dedupes_to_one_driver():
    """N pins of the SAME refdes bonded on one net (e.g. multi-phase SW nodes)
    is one driver, not N -> CLEAN even though raw pin count is 2."""
    net = NetIR(name="SW_BONDED", pins=[("U1", "1"), ("U1", "2")])
    comp1 = _comp("U1", "DCDC1", [("1", "SW1", "SW_BONDED"), ("2", "SW2", "SW_BONDED")])
    ir = NetlistIR(
        source_file="t", components=[comp1], nets=[net],
        power_nets=[], ground_nets=[],
        pintypes={("U1", "1"): "output", ("U1", "2"): "output"},
    )
    results = classify_netlist(ir, pin_specs={})
    r = next(x for x in results if x.net_name == "SW_BONDED")
    assert r.classification == CLASSIFICATION_CLEAN
    assert r.reason == REASON_SAME_REFDES_DEDUP
    assert len(r.drivers) == 1


def test_power_net_skipped():
    net = NetIR(name="VCC_3V3", pins=[("U1", "1"), ("U2", "1")])
    comp1 = _comp("U1", "IC1", [("1", "VOUT", "VCC_3V3")])
    comp2 = _comp("U2", "IC2", [("1", "VOUT", "VCC_3V3")])
    ir = NetlistIR(
        source_file="t", components=[comp1, comp2], nets=[net],
        power_nets=[], ground_nets=[],
        pintypes={("U1", "1"): "output", ("U2", "1"): "output"},
    )
    results = classify_netlist(ir, pin_specs={})
    r = next(x for x in results if x.net_name == "VCC_3V3")
    assert r.classification == CLASSIFICATION_SKIPPED_POWER_GROUND
    assert r.reason == REASON_POWER_GROUND


def test_ambiguous_pintype_crosses_threshold_is_unresolvable():
    """One definite output driver + one pin with unresolvable (no pintype, no
    pin_specs) pintype that COULD be a second driver -> UNRESOLVABLE, not a
    silent CLEAN or WOULD_FAIL."""
    net = NetIR(name="MAYBE_CONFLICT", pins=[("U1", "1"), ("U2", "1")])
    comp1 = _comp("U1", "IC1", [("1", "OUT", "MAYBE_CONFLICT")])
    comp2 = _comp("U2", "IC2", [("1", "UNK", "MAYBE_CONFLICT")])
    ir = NetlistIR(
        source_file="t", components=[comp1, comp2], nets=[net],
        power_nets=[], ground_nets=[],
        pintypes={("U1", "1"): "output"},  # U2 pin 1 has no pintype entry at all
    )
    results = classify_netlist(ir, pin_specs={})
    r = next(x for x in results if x.net_name == "MAYBE_CONFLICT")
    assert r.classification == CLASSIFICATION_UNRESOLVABLE


def test_single_driver_net_is_clean():
    net = NetIR(name="SIGNAL_A", pins=[("U1", "1"), ("U2", "1")])
    comp1 = _comp("U1", "IC1", [("1", "OUT", "SIGNAL_A")])
    comp2 = _comp("U2", "IC2", [("1", "IN", "SIGNAL_A")])
    ir = NetlistIR(
        source_file="t", components=[comp1, comp2], nets=[net],
        power_nets=[], ground_nets=[],
        pintypes={("U1", "1"): "output", ("U2", "1"): "input"},
    )
    results = classify_netlist(ir, pin_specs={})
    r = next(x for x in results if x.net_name == "SIGNAL_A")
    assert r.classification == CLASSIFICATION_CLEAN


# ── step_08f VERDICT-MOVING emit path (M2 v1, cross-component, FAIL-only) ──────

def test_emit_cross_component_short_fails_with_evidence():
    """A 2-distinct-refdes push-pull short emits exactly one OUTPUT_CONFLICT FAIL
    whose evidence names both driver pins."""
    net = NetIR(name="SIGNAL_CONFLICT", pins=[("U1", "1"), ("U2", "1")])
    comp1 = _comp("U1", "74HC595", [("1", "QA", "SIGNAL_CONFLICT")])
    comp2 = _comp("U2", "74HC595", [("1", "QB", "SIGNAL_CONFLICT")])
    ir = NetlistIR(
        source_file="t", components=[comp1, comp2], nets=[net],
        power_nets=[], ground_nets=[],
        pintypes={("U1", "1"): "output", ("U2", "1"): "output"},
    )
    findings = check_output_conflicts(ir, pin_specs={})
    assert len(findings) == 1
    f = findings[0]
    assert f.net == "SIGNAL_CONFLICT"
    assert f.status == "FAIL"
    assert f.violation == VIOLATION_OUTPUT_CONFLICT
    assert f.severity == "critical"
    assert {d["refdes"] for d in f.drivers} == {"U1", "U2"}
    assert "U1.1" in f.evidence_label and "U2.1" in f.evidence_label


def test_emit_is_fail_only_excluded_nets_emit_nothing():
    """Every non-WOULD_FAIL net (open-drain veto, bidir skip, same-refdes dedup,
    power/ground, single driver) emits NO finding."""
    # open-drain veto
    net_od = NetIR(name="CSIA_FLG", pins=[("U44", "3"), ("U45", "3")])
    od1 = _comp("U44", "AP22615A", [("3", "FLG", "CSIA_FLG")])
    od2 = _comp("U45", "AP22615A", [("3", "FLG", "CSIA_FLG")])
    ir_od = NetlistIR(
        source_file="t", components=[od1, od2], nets=[net_od],
        power_nets=[], ground_nets=[],
        pintypes={("U44", "3"): "output", ("U45", "3"): "output"},
    )
    pin_specs = {
        ("U44", "FLG"): _spec("AP22615A", "FLG", "3", open_drain=True),
        ("U45", "FLG"): _spec("AP22615A", "FLG", "3", open_drain=True),
    }
    assert check_output_conflicts(ir_od, pin_specs) == []

    # bidir present -> whole net skipped
    net_b = NetIR(name="SHARED_BUS", pins=[("U1", "1"), ("U2", "1"), ("U3", "1")])
    ir_b = NetlistIR(
        source_file="t",
        components=[_comp("U1", "IC1", [("1", "D0", "SHARED_BUS")]),
                    _comp("U2", "IC2", [("1", "D0", "SHARED_BUS")]),
                    _comp("U3", "IC3", [("1", "D0", "SHARED_BUS")])],
        nets=[net_b], power_nets=[], ground_nets=[],
        pintypes={("U1", "1"): "output", ("U2", "1"): "output", ("U3", "1"): "tri_state"},
    )
    assert check_output_conflicts(ir_b, pin_specs={}) == []

    # same-refdes (same-component v2 scope) -> no finding
    net_s = NetIR(name="SW_BONDED", pins=[("U1", "1"), ("U1", "2")])
    ir_s = NetlistIR(
        source_file="t",
        components=[_comp("U1", "DCDC1", [("1", "SW1", "SW_BONDED"), ("2", "SW2", "SW_BONDED")])],
        nets=[net_s], power_nets=[], ground_nets=[],
        pintypes={("U1", "1"): "output", ("U1", "2"): "output"},
    )
    assert check_output_conflicts(ir_s, pin_specs={}) == []

    # power net -> skipped
    net_p = NetIR(name="VCC_3V3", pins=[("U1", "1"), ("U2", "1")])
    ir_p = NetlistIR(
        source_file="t",
        components=[_comp("U1", "IC1", [("1", "VOUT", "VCC_3V3")]),
                    _comp("U2", "IC2", [("1", "VOUT", "VCC_3V3")])],
        nets=[net_p], power_nets=[], ground_nets=[],
        pintypes={("U1", "1"): "output", ("U2", "1"): "output"},
    )
    assert check_output_conflicts(ir_p, pin_specs={}) == []

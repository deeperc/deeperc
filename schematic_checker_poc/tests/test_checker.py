import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from steps.step_02_parser import NetlistIR, ComponentIR, PinIR, NetIR
from steps.step_05_validator import ValidatedPinSpec
from steps.step_08_checker import run, CheckResult


def _make_ir(driver_vcc_net: str, driver_vcc_v: float) -> tuple[NetlistIR, dict[str, float]]:
    ir = NetlistIR(
        source_file="test",
        components=[
            ComponentIR(
                refdes="U_DRV",
                part_number="Driver",
                value="Driver",
                pins=[
                    PinIR(pin_id="1", pin_name="OUT", net="SIG"),
                    PinIR(pin_id="2", pin_name="VCC", net=driver_vcc_net),
                ],
            ),
            ComponentIR(
                refdes="U_RCV",
                part_number="Receiver",
                value="Receiver",
                pins=[
                    PinIR(pin_id="1", pin_name="IN", net="SIG"),
                    PinIR(pin_id="2", pin_name="VCC", net="VCC_3V3"),
                ],
            ),
        ],
        nets=[
            NetIR(name="SIG", pins=[("U_DRV", "1"), ("U_RCV", "1")]),
            NetIR(name=driver_vcc_net, pins=[("U_DRV", "2")]),
            NetIR(name="VCC_3V3", pins=[("U_RCV", "2")]),
        ],
        power_nets=[driver_vcc_net, "VCC_3V3"],
        ground_nets=[],
    )
    confirmed = {driver_vcc_net: driver_vcc_v, "VCC_3V3": 3.3}
    return ir, confirmed


def _spec(VIH_max, confidence="high", snippet="VIH 3.6V typical") -> ValidatedPinSpec:
    return ValidatedPinSpec(
        part_number="Receiver",
        pin_name="IN",
        pin_id="1",
        VIH_max=VIH_max,
        VIH_min=None,
        absolute_max_voltage=None,
        pin_type="input",
        source_snippet=snippet,
        signal_score=75,
        confidence=confidence,
        is_verified=(confidence != "low"),
    )


def _run_single(driver_v: float, VIH_max: float | None, confidence="high",
                driver_exact: bool = True, driver_open_drain: bool = False) -> CheckResult:
    ir, confirmed = _make_ir("VCC_5V", driver_v)
    pin_specs = {("U_RCV", "IN"): _spec(VIH_max, confidence)}
    # Mark OUT as output type. driver_exact/driver_open_drain model whether the
    # output type came from a positive example_pins match (genuine push-pull) vs a
    # default-fallback / open-drain — the push-pull guard requires exact + not
    # open-drain before an output pin may assert an overvoltage.
    pin_specs[("U_DRV", "OUT")] = ValidatedPinSpec(
        part_number="Driver", pin_name="OUT", pin_id="1",
        VIH_max=None, VIH_min=None, absolute_max_voltage=None, pin_type="output",
        source_snippet=None, signal_score=5, confidence="low", is_verified=False,
        pin_type_exact=driver_exact, open_drain=driver_open_drain,
    )
    results = run(ir, pin_specs, confirmed)
    sig = next(r for r in results if r.net_name == "SIG")
    return sig


def test_fail_5v_into_3v3():
    # Genuine push-pull output (exact-matched, not open-drain): 5V into 3.3V → FAIL.
    # This is the SIGNAL_QA / 74HC595-QA → STM32 real-overdrive case; it must survive
    # the push-pull guard.
    r = _run_single(5.0, 3.6)
    assert r.status == "FAIL"


def test_fallback_typed_output_downgraded():
    # Driver 'output' type came from a default-group FALLBACK, not a positive match
    # (the ULN2003 base-input-mistyped-as-collector-output false positive). It must
    # NOT assert an overvoltage — downgrade the FAIL to UNRESOLVABLE.
    r = _run_single(5.0, 3.6, driver_exact=False)
    assert r.status == "UNRESOLVABLE"


def test_open_drain_output_downgraded():
    # Driver is a documented open-drain output (the AP22615 FLG false positive) —
    # it sinks only, never sources its 5V VCC — so the FAIL is downgraded.
    r = _run_single(5.0, 3.6, driver_open_drain=True)
    assert r.status == "UNRESOLVABLE"


def test_exact_pushpull_pass_kept():
    # A safe margin from a genuine push-pull output is still a real PASS (guard only
    # touches FAIL/WARN, never PASS).
    r = _run_single(3.3, 3.6)
    assert r.status == "PASS"


def test_pass_3v3_into_3v6():
    r = _run_single(3.3, 3.6)
    assert r.status == "PASS"


def test_warn_equal_vih():
    # driver == VIH_max exactly: 3.3 > 3.3 * 0.9 = 2.97, and 3.3 is not > 3.3 → WARN
    r = _run_single(3.3, 3.3)
    assert r.status == "WARN"


def test_warn_within_10_percent():
    # 3.3 > 3.45 * 0.9 = 3.105 → WARN; 3.3 not > 3.45 → not FAIL
    r = _run_single(3.3, 3.45)
    assert r.status == "WARN"


def test_null_specs_unresolvable():
    # A receiver with NO extracted thresholds (VIH_max/VIH_min/abs_max all None)
    # has no basis for a compatibility verdict, so the modern checker routes it to
    # UNRESOLVABLE rather than emitting a low-confidence WARN guess (the deliberate
    # UNRESOLVABLE-bucket design; has_receiver_specs gate, step_08_checker.py:433).
    r = _run_single(3.3, None, confidence="low")
    assert r.status == "UNRESOLVABLE"
    assert r.combined_confidence == "low"
    assert "specs" in (r.unresolvable_reason or "").lower()


# ──────────────────────────────────────────────────────────────────────────────
# Fix α — unit tests for _component_power_domain guard
#
# Tests the function directly (no run() / ValidatedPinSpec construction) to
# avoid coupling with the 5 pre-existing orphan failures in this file. Uses
# SimpleNamespace stubs for pin_specs so the proxy's getattr-based shape check
# (`getattr(spec, "power_domain", None)`) can be exercised both ways without
# touching ValidatedPinSpec.
#
# Background: cross_board_confident_wrong_analysis.md / commit 6cb1878.
# ──────────────────────────────────────────────────────────────────────────────

from types import SimpleNamespace

from steps.step_08_checker import _component_power_domain


def _make_pcd_ir(refdes: str, pin_nets: list[tuple[str, str]]) -> NetlistIR:
    """Build a minimal NetlistIR around one component with the given (pin_name, net) pairs."""
    pins = [PinIR(pin_id=str(i + 1), pin_name=pn, net=net)
            for i, (pn, net) in enumerate(pin_nets)]
    return NetlistIR(
        source_file="test",
        components=[ComponentIR(refdes=refdes, part_number="X", value="X", pins=pins)],
        nets=[NetIR(name=net, pins=[(refdes, str(i + 1))])
              for i, (_, net) in enumerate(pin_nets)],
        power_nets=[],
        ground_nets=[],
    )


def test_component_power_domain_no_vcc_returns_none():
    # Comp has only signal pins; no confirmed rails → None.
    ir = _make_pcd_ir("U1", [("OUT", "SIG_A"), ("IN", "SIG_B")])
    assert _component_power_domain("U1", ir, confirmed_voltages={}, pin_specs={}) is None


def test_component_power_domain_single_vcc_returns_voltage():
    # Single VCC pin is unambiguous; return its voltage regardless of pin_specs state.
    ir = _make_pcd_ir("U1", [("OUT", "SIG"), ("VCC", "VCC_3V3")])
    assert _component_power_domain(
        "U1", ir, confirmed_voltages={"VCC_3V3": 3.3}, pin_specs={}
    ) == 3.3


def test_component_power_domain_multi_vcc_no_extraction_returns_none():
    # Two VCC pins, no per-pin power_domain in pin_specs → guard fires → None.
    # This is the bug case (formerly: returned whichever iterated first).
    ir = _make_pcd_ir("U1", [("VCCA", "VCC_5V"), ("VCCB", "VCC_3V3"), ("OUT", "SIG")])
    assert _component_power_domain(
        "U1", ir,
        confirmed_voltages={"VCC_5V": 5.0, "VCC_3V3": 3.3},
        pin_specs={},
    ) is None


def test_component_power_domain_multi_vcc_with_extraction_returns_first():
    # Two VCC pins; proxy detects per-pin power_domain on a spec → trust first-iter.
    # Forward-compat: when ValidatedPinSpec gains a power_domain field on resolved
    # parts, this path activates. SimpleNamespace stub matches the getattr proxy.
    ir = _make_pcd_ir("U1", [("VCCA", "VCC_5V"), ("VCCB", "VCC_3V3"), ("OUT", "SIG")])
    pin_specs = {
        ("U1", "OUT"): SimpleNamespace(power_domain="VCCA"),
    }
    assert _component_power_domain(
        "U1", ir,
        confirmed_voltages={"VCC_5V": 5.0, "VCC_3V3": 3.3},
        pin_specs=pin_specs,
    ) == 5.0


def test_component_power_domain_unknown_refdes_returns_none():
    # Refdes not present in IR → None.
    ir = _make_pcd_ir("U1", [("VCC", "VCC_3V3")])
    assert _component_power_domain(
        "U_MISSING", ir,
        confirmed_voltages={"VCC_3V3": 3.3},
        pin_specs={},
    ) is None


def test_component_power_domain_reproduces_xczu4cg_case():
    # Simulates vme-wren/fpga.net case 3: XCZU4CG has resolved: false,
    # multiple supply rails on the chip (P0V85, P1V2, P1V8, P3V3, MGT_1V8),
    # one signal output pin (RCLK). Pre-fix: returned 0.85 (first VCC iter)
    # → "1.8V driver below 2.3V VIH_min" was actually the first-VCC false
    # positive after Step 06 unblocked the rails. Post-fix: None → UNRESOLVABLE.
    ir = _make_pcd_ir("IC14", [
        ("VCC_PSADC",   "P0V85"),
        ("VCC_PSPLL",   "P1V2"),
        ("VCCO_PSIO0",  "P1V8"),
        ("VCCO_PSDDR",  "P3V3"),
        ("MGTAVCC",     "MGT_1V8"),
        ("F_IOEXP_RCLK", "RCLK_NET"),
    ])
    confirmed = {"P0V85": 0.85, "P1V2": 1.2, "P1V8": 1.8, "P3V3": 3.3, "MGT_1V8": 1.8}
    # No pin_specs → resolved: false proxy → guard fires.
    assert _component_power_domain("IC14", ir, confirmed, pin_specs={}) is None


# ──────────────────────────────────────────────────────────────────────────────
# v2_10.4 — provenance-calibrated drv_conf
#
# Tests the two pure helpers directly (no run() / ValidatedPinSpec), matching
# the Fix α pattern, so they sidestep the 5 pre-existing orphan failures.
# drv_conf = weakest-link(rail-classification confidence, resolution confidence).
# ──────────────────────────────────────────────────────────────────────────────

from steps.step_08_checker import (
    _resolve_power_domain,
    _driver_confidence,
    RailProvenance,
    PowerDomainResolution,
    combined_confidence,
)


def test_driver_confidence_deterministic_single_vcc_is_high():
    # Tier-1 deterministic rail, unambiguous single-VCC part → both high → high.
    prov = RailProvenance(source="deterministic", confidence="high")
    assert _driver_confidence(prov, "single_vcc") == "high"


def test_driver_confidence_gemma_single_vcc_is_medium():
    # The core behavioral change: a Gemma-classified (medium) rail no longer
    # masquerades as high just because a voltage was resolved.
    prov = RailProvenance(source="gemma", confidence="medium")
    assert _driver_confidence(prov, "single_vcc") == "medium"


def test_driver_confidence_deterministic_per_pin_resolved_is_high():
    # Multi-VCC grounded by per-pin extraction is trustworthy → high (forward-compat).
    prov = RailProvenance(source="deterministic", confidence="high")
    assert _driver_confidence(prov, "per_pin_resolved") == "high"


def test_driver_confidence_low_rail_caps_combined():
    # A deterministically-named rail whose voltage couldn't be extracted is "low";
    # weakest-link drags the result to low even on an unambiguous domain.
    prov = RailProvenance(source="deterministic", confidence="low")
    assert _driver_confidence(prov, "single_vcc") == "low"


def test_driver_confidence_missing_provenance_is_low():
    # Conservative default: provided dict but no entry for this net → low.
    assert _driver_confidence(None, "single_vcc") == "low"


def test_resolve_power_domain_single_vcc_carries_net_and_tag():
    ir = _make_pcd_ir("U1", [("OUT", "SIG"), ("VCC", "VCC_3V3")])
    res = _resolve_power_domain("U1", ir, confirmed_voltages={"VCC_3V3": 3.3}, pin_specs={})
    assert res == PowerDomainResolution(voltage=3.3, net="VCC_3V3", resolution="single_vcc")


def test_resolve_power_domain_multi_vcc_no_extraction_is_none():
    # Parity with the Fix α guard: ambiguous multi-VCC without per-pin mapping → None.
    ir = _make_pcd_ir("U1", [("VCCA", "VCC_5V"), ("VCCB", "VCC_3V3"), ("OUT", "SIG")])
    assert _resolve_power_domain(
        "U1", ir, confirmed_voltages={"VCC_5V": 5.0, "VCC_3V3": 3.3}, pin_specs={}
    ) is None


def test_resolve_power_domain_per_pin_resolved_tag():
    # Proxy detects per-pin power_domain → tagged per_pin_resolved (forward-compat).
    ir = _make_pcd_ir("U1", [("VCCA", "VCC_5V"), ("VCCB", "VCC_3V3"), ("OUT", "SIG")])
    pin_specs = {("U1", "OUT"): SimpleNamespace(power_domain="VCCA")}
    res = _resolve_power_domain(
        "U1", ir, confirmed_voltages={"VCC_5V": 5.0, "VCC_3V3": 3.3}, pin_specs=pin_specs
    )
    assert res.resolution == "per_pin_resolved" and res.voltage == 5.0


def test_combined_confidence_gemma_driver_into_high_receiver_is_medium():
    # End-to-end: medium drv_conf (gemma rail) + high recv_conf → medium combined.
    drv_conf = _driver_confidence(RailProvenance("gemma", "medium"), "single_vcc")
    assert combined_confidence(drv_conf, "high") == "medium"


# ──────────────────────────────────────────────────────────────────────────────
# Driver-type guard — only a genuine push-pull output asserts its VCC as the net
# drive voltage. Non-push-pull drivers (priority-2 highest-VCC / priority-3 first
# -pin fallbacks: bidirectional, passive analog-switch throw, open-drain flag,
# split-rail translator) → UNRESOLVABLE, never FAIL. Clears the analog-switch /
# open-drain / split-rail false positives triaged in 81c3e84 while preserving the
# real 5V-output-into-3.3V-input FAIL (test_fail_5v_into_3v3 above, and
# test_guard_genuine_pushpull_output_still_fails below).
# ──────────────────────────────────────────────────────────────────────────────

def _guard_ir(driver_vcc_pins: list[tuple[str, str]], driver_sig_pin="IO"):
    """IR with a driver (VCC pin(s) + one signal pin on SIG) and a 3.3V receiver
    on SIG. driver_vcc_pins = list of (pin_name, vcc_net). confirmed_voltages and
    a 3.6V-abs_max receiver spec are returned alongside."""
    drv_pins = [PinIR(pin_id=str(i + 1), pin_name=pn, net=net)
                for i, (pn, net) in enumerate(driver_vcc_pins)]
    sig_pid = str(len(drv_pins) + 1)
    drv_pins.append(PinIR(pin_id=sig_pid, pin_name=driver_sig_pin, net="SIG"))
    nets = [NetIR(name="SIG", pins=[("U_DRV", sig_pid), ("U_RCV", "1")])]
    confirmed = {}
    for i, (pn, net) in enumerate(driver_vcc_pins):
        nets.append(NetIR(name=net, pins=[("U_DRV", str(i + 1))]))
    nets.append(NetIR(name="VCC_3V3", pins=[("U_RCV", "2")]))
    confirmed = {net: v for (_, net), v in
                 zip(driver_vcc_pins, [5.0, 3.3, 1.8][:len(driver_vcc_pins)])}
    confirmed["VCC_3V3"] = 3.3
    ir = NetlistIR(
        source_file="test",
        components=[
            ComponentIR(refdes="U_DRV", part_number="Drv", value="Drv", pins=drv_pins),
            ComponentIR(refdes="U_RCV", part_number="Rcv", value="Rcv", pins=[
                PinIR(pin_id="1", pin_name="IN", net="SIG"),
                PinIR(pin_id="2", pin_name="VCC", net="VCC_3V3"),
            ]),
        ],
        nets=nets,
        power_nets=[net for _, net in driver_vcc_pins] + ["VCC_3V3"],
        ground_nets=[],
    )
    return ir, confirmed


def _recv_absmax_spec(abs_max=3.6):
    return ValidatedPinSpec(
        part_number="Rcv", pin_name="IN", pin_id="1",
        VIH_max=None, VIH_min=None, absolute_max_voltage=abs_max, pin_type="input",
        source_snippet="abs max 3.6V", signal_score=75, confidence="high",
        is_verified=True,
    )


def _drv_spec(pin_name, pin_type, *, pin_type_exact=True, open_drain=False):
    # pin_type_exact defaults True: these guard tests model a driver whose type
    # came from a positive match. The push-pull path additionally requires the
    # output type be exact + not open-drain (see step_08 explicit_out filter).
    return ValidatedPinSpec(
        part_number="Drv", pin_name=pin_name, pin_id="X",
        VIH_max=None, VIH_min=None, absolute_max_voltage=None, pin_type=pin_type,
        source_snippet=None, signal_score=5, confidence="low", is_verified=False,
        pin_type_exact=pin_type_exact, open_drain=open_drain,
    )


def _sig_result(ir, pin_specs, confirmed):
    return next(r for r in run(ir, pin_specs, confirmed) if r.net_name == "SIG")


def test_guard_open_drain_like_driver_unresolvable():
    # Open-drain flag (AP22615 FLG class): no datasheet output classification →
    # pin_type None → priority-2 highest-VCC. 5V VCC > 3.6 abs_max but the pin does
    # not push 5V (high level = pull-up rail) → UNRESOLVABLE, not FAIL.
    ir, confirmed = _guard_ir([("VIN", "VCC_5V")])
    pin_specs = {("U_RCV", "IN"): _recv_absmax_spec()}
    r = _sig_result(ir, pin_specs, confirmed)
    assert r.status == "UNRESOLVABLE"
    assert "push-pull" in (r.unresolvable_reason or "")


def test_guard_passive_switch_terminal_unresolvable():
    # TS5A3159 class: analog-switch throw terminal typed passive → priority-2.
    ir, confirmed = _guard_ir([("V+", "VCC_5V")])
    pin_specs = {("U_RCV", "IN"): _recv_absmax_spec(),
                 ("U_DRV", "IO"): _drv_spec("IO", "passive")}
    r = _sig_result(ir, pin_specs, confirmed)
    assert r.status == "UNRESOLVABLE"


def test_guard_bidirectional_driver_unresolvable():
    # Bidirectional pin → not a confirmed push-pull output → priority-2 → UNRESOLVABLE
    # (consistent with the Fix-α split-rail outcome).
    ir, confirmed = _guard_ir([("VCC", "VCC_5V")])
    pin_specs = {("U_RCV", "IN"): _recv_absmax_spec(),
                 ("U_DRV", "IO"): _drv_spec("IO", "bidirectional")}
    r = _sig_result(ir, pin_specs, confirmed)
    assert r.status == "UNRESOLVABLE"


def test_guard_genuine_pushpull_output_still_fails():
    # LOAD-BEARING anti-over-reach: a true push-pull output (priority-1) at 5V into
    # a 3.6V-abs_max input must STILL FAIL. The 74HC595→STM32 real-defect shape.
    ir, confirmed = _guard_ir([("VCC", "VCC_5V")])
    pin_specs = {("U_RCV", "IN"): _recv_absmax_spec(),
                 ("U_DRV", "IO"): _drv_spec("IO", "output")}
    r = _sig_result(ir, pin_specs, confirmed)
    assert r.status == "FAIL"
    assert r.driver_voltage == 5.0


def test_guard_txb0108_split_rail_unresolvable():
    # TXB0108 A-side class: two VCC pins (VCCA 3.3 / VCCB 5.0) → _resolve_power_domain
    # returns None via the Fix-α multi-VCC guard AND the driving pin is bidirectional.
    # Both reasons agree → UNRESOLVABLE (guard/Fix-α consistency).
    ir, confirmed = _guard_ir([("VCCB", "VCC_5V"), ("VCCA", "VCC_3V3B")], driver_sig_pin="A1")
    confirmed["VCC_3V3B"] = 3.3
    pin_specs = {("U_RCV", "IN"): _recv_absmax_spec(),
                 ("U_DRV", "A1"): _drv_spec("A1", "bidirectional")}
    r = _sig_result(ir, pin_specs, confirmed)
    assert r.status == "UNRESOLVABLE"


def test_guard_nonpushpull_pass_is_preserved():
    # Surgical-guard invariant: a non-push-pull driver at a SAFE margin (VCC <=
    # receiver abs_max) must KEEP its PASS — the net cannot exceed the driver's own
    # VCC, so the passing margin is sound regardless of pin type. (Distinguishes the
    # surgical guard from a blanket non-push-pull → UNRESOLVABLE, which would be a
    # false coverage loss and would score as a regression on bidirectional buses.)
    ir, confirmed = _guard_ir([("VCC", "VCC_3V3B")])
    confirmed["VCC_3V3B"] = 3.3
    pin_specs = {("U_RCV", "IN"): _recv_absmax_spec(3.6),
                 ("U_DRV", "IO"): _drv_spec("IO", "bidirectional")}
    r = _sig_result(ir, pin_specs, confirmed)
    assert r.status == "PASS"


def test_guard_open_drain_output_with_pullup_unresolvable():
    # AP22615 FLG class: the driver pin is datasheet-typed "output" (priority-1
    # push-pull path) BUT it is OPEN-DRAIN — the net has a pull-up resistor to a
    # rail (3.3V) below the driver's VCC (5.0V). The logic-high is the pull-up rail,
    # not the 5V VIN, so the 5V-over-3.6V FAIL is a false positive → UNRESOLVABLE.
    ir = NetlistIR(
        source_file="test",
        components=[
            ComponentIR(refdes="U_DRV", part_number="Drv", value="Drv", pins=[
                PinIR(pin_id="1", pin_name="FLG", net="SIG"),
                PinIR(pin_id="2", pin_name="VIN", net="VCC_5V"),
            ]),
            ComponentIR(refdes="U_RCV", part_number="Rcv", value="Rcv", pins=[
                PinIR(pin_id="1", pin_name="IN", net="SIG"),
                PinIR(pin_id="2", pin_name="VCC", net="VCC_3V3"),
            ]),
            # pull-up resistor: one foot on SIG, the other on +3V3
            ComponentIR(refdes="R1", part_number="R", value="10k", pins=[
                PinIR(pin_id="1", pin_name="1", net="SIG"),
                PinIR(pin_id="2", pin_name="2", net="VCC_3V3"),
            ]),
        ],
        nets=[
            NetIR(name="SIG", pins=[("U_DRV", "1"), ("U_RCV", "1"), ("R1", "1")]),
            NetIR(name="VCC_5V", pins=[("U_DRV", "2")]),
            NetIR(name="VCC_3V3", pins=[("U_RCV", "2"), ("R1", "2")]),
        ],
        power_nets=["VCC_5V", "VCC_3V3"], ground_nets=[],
    )
    confirmed = {"VCC_5V": 5.0, "VCC_3V3": 3.3}
    pin_specs = {("U_RCV", "IN"): _recv_absmax_spec(3.6),
                 ("U_DRV", "FLG"): _drv_spec("FLG", "output")}
    r = next(x for x in run(ir, pin_specs, confirmed) if x.net_name == "SIG")
    assert r.status == "UNRESOLVABLE"


# ──────────────────────────────────────────────────────────────────────────────
# U1 power-domain resolution fix (Options 1+2+3) — recon:
# investigation/experiments/u1_power_domain_recon/REPORT.md; projection:
# investigation/experiments/u1_power_domain_projection/REPORT.md.
#
# Option 1: ambiguity = >1 DISTINCT voltage among confirmed rails, not >1 pin.
# Option 2: membership = pins on non-None-valued confirmed rails only.
# Option 3: the conflated UNRESOLVABLE message splits into three reasons.
# ──────────────────────────────────────────────────────────────────────────────

from steps.step_08_checker import _unresolved_driver_voltage_reason


def test_resolve_power_domain_unanimous_different_rails_resolves():
    # Dumpling-shaped: U1's real case — 2 differently-named rails (+3V3, VDDA),
    # both 3.3V. Pre-fix: 2 pins -> ambiguous -> None (RED). Post-fix: the pins
    # agree -> resolves, tagged "unanimous_rails".
    ir = _make_pcd_ir("U1", [("VDD", "+3V3"), ("VDDA", "VDDA_NET"), ("OUT", "SIG")])
    res = _resolve_power_domain(
        "U1", ir, confirmed_voltages={"+3V3": 3.3, "VDDA_NET": 3.3}, pin_specs={}
    )
    assert res == PowerDomainResolution(voltage=3.3, net="+3V3", resolution="unanimous_rails")


def test_resolve_power_domain_unanimous_same_net_resolves():
    # U7-shaped: 2 pins on the SAME net (+3V3) -- the sharpest degenerate case:
    # zero ambiguity (one net, one voltage) yet pre-fix pin-count logic still
    # returned None. Post-fix: resolves.
    ir = _make_pcd_ir("U7", [("VDD1", "+3V3"), ("VDD2", "+3V3"), ("SDA", "SIG")])
    res = _resolve_power_domain(
        "U7", ir, confirmed_voltages={"+3V3": 3.3}, pin_specs={}
    )
    assert res == PowerDomainResolution(voltage=3.3, net="+3V3", resolution="unanimous_rails")


def test_resolve_power_domain_two_distinct_voltages_stays_none():
    # Dominant real "genuine ambiguity" shape per the projection (654/1191 rows):
    # 2 rails, 2 DIFFERENT voltages -> must stay None. Guards against an
    # over-loosened Option 1 (e.g. "resolve to max/min voltage") -- a buggy
    # implementation like that would return a voltage here instead of None.
    ir = _make_pcd_ir("U14", [("VCCA", "VCC_5V"), ("VCCB", "VCC_3V3"), ("OUT", "SIG")])
    assert _resolve_power_domain(
        "U14", ir, confirmed_voltages={"VCC_5V": 5.0, "VCC_3V3": 3.3}, pin_specs={}
    ) is None


def test_resolve_power_domain_none_valued_rail_returns_none_not_object():
    # Option 2: the sole confirmed-rail pin sits on a net whose confirmed voltage
    # is None (e.g. a signal net like PWR_GOOD that rail inference tracked but
    # never assigned a voltage). Pre-fix: membership test didn't exclude None,
    # so len(vcc_pins)==1 short-circuited to a NON-None PowerDomainResolution
    # object carrying voltage=None (the ":454" confidence-path leak). Post-fix:
    # the pin is excluded from membership entirely -> no resolution object at all.
    ir = _make_pcd_ir("U6", [("PWR_GOOD", "PWR_GOOD_NET"), ("SDA", "SIG")])
    res = _resolve_power_domain(
        "U6", ir, confirmed_voltages={"PWR_GOOD_NET": None}, pin_specs={}
    )
    assert res is None


def test_unresolved_driver_voltage_reason_message_split():
    # Option 3: the three-way message split, numerals included for the
    # ambiguous case.
    ir_gap = _make_pcd_ir("U9", [("OUT", "SIG")])  # no VCC pin at all
    reason_gap = _unresolved_driver_voltage_reason("U9", ir_gap, confirmed_voltages={})
    assert "no confirmed rail on any pin" in reason_gap
    assert "U9" in reason_gap

    ir_ambig = _make_pcd_ir("U14", [("VCCA", "VCC_5V"), ("VCCB", "VCC_3V3"), ("OUT", "SIG")])
    reason_ambig = _unresolved_driver_voltage_reason(
        "U14", ir_ambig, confirmed_voltages={"VCC_5V": 5.0, "VCC_3V3": 3.3}
    )
    assert "ambiguous" in reason_ambig
    assert "2 rail" in reason_ambig or "2 pin" in reason_ambig
    assert "2 distinct voltage" in reason_ambig

    reason_no_datasheet = _unresolved_driver_voltage_reason(
        "U_GHOST", ir_gap, confirmed_voltages={}
    )
    assert "no resolved datasheet" in reason_no_datasheet
    assert "U_GHOST" in reason_no_datasheet


def test_hadesnt_shaped_multi_row_board_flip_at_unit_scale():
    # hadesnt.json's real board-level effect: several signal rows off ONE driver
    # whose supply pins sit on 2 differently-named same-voltage rails (like U1's
    # +3V3/VDDA), all converting UNRESOLVABLE->PASS -- the mechanism behind the
    # single curated has_unresolvable_only -> all_pass flip predicted by the
    # projection (Phase 1, Table 2).
    ir = NetlistIR(
        source_file="test",
        components=[
            ComponentIR(refdes="U_DRV", part_number="Drv", value="Drv", pins=[
                PinIR(pin_id="1", pin_name="VDD", net="+3V3"),
                PinIR(pin_id="2", pin_name="VDDA", net="VDDA_NET"),
                PinIR(pin_id="3", pin_name="SDA", net="SIG_A"),
                PinIR(pin_id="4", pin_name="SCL", net="SIG_B"),
            ]),
            ComponentIR(refdes="U_RCV1", part_number="Rcv1", value="Rcv1", pins=[
                PinIR(pin_id="1", pin_name="SDA", net="SIG_A"),
            ]),
            ComponentIR(refdes="U_RCV2", part_number="Rcv2", value="Rcv2", pins=[
                PinIR(pin_id="1", pin_name="SCL", net="SIG_B"),
            ]),
        ],
        nets=[
            NetIR(name="SIG_A", pins=[("U_DRV", "3"), ("U_RCV1", "1")]),
            NetIR(name="SIG_B", pins=[("U_DRV", "4"), ("U_RCV2", "1")]),
            NetIR(name="+3V3", pins=[("U_DRV", "1")]),
            NetIR(name="VDDA_NET", pins=[("U_DRV", "2")]),
        ],
        power_nets=["+3V3", "VDDA_NET"], ground_nets=[],
    )
    confirmed = {"+3V3": 3.3, "VDDA_NET": 3.3}
    pin_specs = {
        ("U_RCV1", "SDA"): _recv_absmax_spec(5.5),
        ("U_RCV2", "SCL"): _recv_absmax_spec(5.5),
    }
    results = run(ir, pin_specs, confirmed)
    rows = [r for r in results if r.net_name in ("SIG_A", "SIG_B")]
    assert len(rows) == 2
    assert all(r.status == "PASS" for r in rows)

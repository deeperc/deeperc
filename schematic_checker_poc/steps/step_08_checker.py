import re
from dataclasses import dataclass
from typing import NamedTuple
from steps.step_02_parser import NetlistIR
from steps.step_05_validator import ValidatedPinSpec
from steps.step_06_power import POWER_NET_RE, GROUND_NET_RE
from trace.writer import write_trace

PASSIVE_REFDES_RE = re.compile(
    r'^(?:'
    r'R|RN|C|CN|L|FB|D|LED|F|T|TR|Y|XTAL|XT|'   # passives + resistor/cap networks
    r'Q|QN|'                                       # discrete transistors / MOSFETs
    r'J|P|CON|X|'                                  # connectors (CN stays in passives above)
    r'PPTC|FUSE|PTC|NTC|'                          # polyfuses, fuses, thermistors
    r'SW|BTN|KEY|PB|'                              # switches
    r'TP|FID|MH|H'                                 # test points, fiducials, mounting holes
    r')\d+',
    re.IGNORECASE
)


def _normalize_net_name(name: str) -> str:
    from steps.net_name_utils import normalize_net_name
    return normalize_net_name(name)


def is_passive_component(refdes: str) -> bool:
    """Returns True if refdes belongs to a component that cannot be a voltage driver."""
    return bool(PASSIVE_REFDES_RE.match(refdes))


# Fix 3: confidence helpers
CONFIDENCE_RANK = {"high": 2, "medium": 1, "low": 0}


def combined_confidence(
    driver_conf: str | None,
    receiver_conf: str | None,
) -> str:
    """Returns weaker of driver and receiver confidence. None → 'low'."""
    if driver_conf is None or receiver_conf is None:
        return "low"
    return min(
        [driver_conf, receiver_conf],
        key=lambda c: CONFIDENCE_RANK.get(c, 0),
    )


# v2_10.4: provenance-calibrated driver confidence.
# Two provenance dimensions feed drv_conf:
#   - rail-classification provenance (step_06 PowerRail source/confidence),
#     carried past step_07's flatten via a parallel net→RailProvenance dict
#     built at the call sites;
#   - power-domain resolution provenance (which branch of _resolve_power_domain
#     produced the voltage).
# They are combined weakest-link via combined_confidence / CONFIDENCE_RANK.
class RailProvenance(NamedTuple):
    source: str        # "deterministic" | "gemma" | ...
    confidence: str    # "high" | "medium" | "low"


class PowerDomainResolution(NamedTuple):
    voltage: float
    net: str
    resolution: str    # "single_vcc" | "per_pin_resolved"


# Both voltage-yielding resolution branches mean "we know this is correct";
# the weakest-link combination already catches a flaky (low-confidence) rail,
# so per_pin_resolved is high too (forward-compat; currently unreachable while
# _has_per_pin_power_domain is universally False).
_RESOLUTION_CONF = {"single_vcc": "high", "per_pin_resolved": "high"}


def _driver_confidence(rail_prov: "RailProvenance | None", resolution: str) -> str:
    """Weakest-link of rail-classification confidence and power-domain
    resolution confidence. Missing rail provenance → 'low' (conservative)."""
    rail_conf = rail_prov.confidence if rail_prov else "low"
    res_conf = _RESOLUTION_CONF.get(resolution, "low")
    return combined_confidence(rail_conf, res_conf)


# Fix 4: correct VIH_min/VIH_max/abs_max semantics
def evaluate_compatibility(
    driver_voltage: float,
    receiver_VIH_min: float | None,
    receiver_VIH_max: float | None,
    receiver_abs_max: float | None,
) -> tuple[str, str]:
    """
    Returns (status, evidence_label).

    FAIL: driver_voltage > abs_max, or (no abs_max and > VIH_max)
    WARN: driver_voltage > 0.95 * absolute_max_voltage  (transient overshoot risk)
    WARN: driver_voltage < VIH_min for HIGH signals  (signal won't register)
    PASS: otherwise

    VIH_min is a LOWER bound — never used as an upper limit.
    """
    if receiver_abs_max is not None:
        if driver_voltage > receiver_abs_max:
            return "FAIL", (
                f"driver {driver_voltage}V exceeds receiver "
                f"absolute max {receiver_abs_max}V"
            )
        if driver_voltage > 0.95 * receiver_abs_max:
            return "WARN", (
                f"driver {driver_voltage}V is within 5% of receiver "
                f"absolute max {receiver_abs_max}V — transient overshoot risk"
            )

    if receiver_abs_max is None and receiver_VIH_max is not None:
        if driver_voltage > receiver_VIH_max:
            return "FAIL", (
                f"driver {driver_voltage}V exceeds receiver "
                f"VIH_max {receiver_VIH_max}V"
            )
        if driver_voltage > 0.95 * receiver_VIH_max:
            return "WARN", (
                f"driver {driver_voltage}V is within 5% of receiver "
                f"VIH_max {receiver_VIH_max}V"
            )

    if receiver_VIH_min is not None:
        if driver_voltage < receiver_VIH_min:
            return "WARN", (
                f"driver {driver_voltage}V is below receiver VIH_min {receiver_VIH_min}V "
                f"— signal may not register as logic HIGH"
            )

    return "PASS", f"driver {driver_voltage}V within receiver limits"


@dataclass
class CheckResult:
    net_name: str
    status: str           # "PASS" | "FAIL" | "WARN" | "UNRESOLVABLE"
    driver_refdes: str | None
    driver_voltage: float | None
    driver_voltage_source: str
    receiver_refdes: str
    receiver_pin_name: str
    receiver_VIH_max: float | None
    receiver_abs_max: float | None
    receiver_confidence: str
    driver_confidence: str
    combined_confidence: str
    evidence_label: str
    unresolvable_reason: str | None
    receiver_VIH_min: float | None = None   # Fix 4: stored separately from VIH_max


# Pin names that indicate a VCC/power supply pin
_VCC_NAMES = {"vcc", "vdd", "avdd", "dvdd", "vbat", "v+", "pwr"}
_GND_NAMES = {"gnd", "vss", "agnd", "dgnd", "v-", "gnd_ref"}
_OUTPUT_TYPES = {"output", "bidirectional", "digital_output"}
_INPUT_TYPES = {"input", "bidirectional", "digital_input", "analog"}


def _is_power_pin(pin_name: str) -> bool:
    return pin_name.lower() in _VCC_NAMES or pin_name.lower().startswith("vcc") or pin_name.lower().startswith("vdd")


def _is_gnd_pin(pin_name: str) -> bool:
    return pin_name.lower() in _GND_NAMES


def _has_per_pin_power_domain(
    refdes: str,
    pin_specs: dict[tuple[str, str], "ValidatedPinSpec"],
) -> bool:
    """Forward-compatible proxy for 'extraction provides per-pin power-domain mapping.'

    Today: ValidatedPinSpec has no `power_domain` field, so this is universally
    False — multi-VCC parts fall into the guard's None branch. When extraction is
    later improved to populate a per-pin `power_domain` attribute on resolved
    parts, this proxy auto-loosens and those parts resume returning a voltage.
    """
    for (rd, _), spec in pin_specs.items():
        if rd == refdes and getattr(spec, "power_domain", None):
            return True
    return False


def _resolve_power_domain(
    refdes: str,
    ir: NetlistIR,
    confirmed_voltages: dict[str, float],
    pin_specs: dict[tuple[str, str], "ValidatedPinSpec"],
) -> "PowerDomainResolution | None":
    """Resolve a component's VCC voltage with resolution provenance, else None.

    Behavior (U1 power-domain fix, Options 1+2 — see
    investigation/experiments/u1_power_domain_recon/REPORT.md and
    .../u1_power_domain_projection/REPORT.md):
    - 0 confirmed-rail pins → None (no domain to determine). Membership
      excludes rails whose confirmed voltage is None (Option 2) — a net that
      rail inference tracked but never assigned a voltage is not a domain
      member, and must never yield a resolution object with voltage=None.
    - 1 confirmed-rail pin → (voltage, net, "single_vcc") (unambiguous).
    - >1 confirmed-rail pin, all agreeing on ONE voltage → resolves, tagged
      "unanimous_rails" (Option 1: ambiguity is DISTINCT-VOLTAGE disagreement,
      not pin/rail COUNT — a part with several same-voltage supply pins, e.g.
      +3V3 and VDDA both at 3.3V, or two pins on the same net, is not
      ambiguous just because it has more than one pin).
    - >1 confirmed-rail pin, genuinely disagreeing on voltage → trust the
      first-iter heuristic only when extraction provides per-pin power-domain
      mapping for this part (see `_has_per_pin_power_domain`), tagged
      "per_pin_resolved"; otherwise None.

    Pre-Fix-α logic returned the first-iterated VCC pin's voltage in all
    cases, which produced wrong attributions on multi-VCC parts without per-pin
    extraction (FPGAs, connectors, modules). See
    `corpus_results/cross_board_confident_wrong_analysis.md` for the cases that
    motivated the guard. The guard itself (pre-Options-1+2) then over-rejected:
    it counted PINS, not DISTINCT VOLTAGES, so a same-voltage multi-rail MCU
    (the common case) was misclassified as ambiguous.

    Returns the matched net name so callers can look up rail-classification
    provenance (v2_10.4 drv_conf calibration).
    """
    comp = next((c for c in ir.components if c.refdes == refdes), None)
    if comp is None:
        return None
    vcc_pins = [(pin.net, confirmed_voltages[pin.net])
                for pin in comp.pins
                if pin.net in confirmed_voltages and confirmed_voltages[pin.net] is not None]
    if not vcc_pins:
        return None
    if len(vcc_pins) == 1:
        net, v = vcc_pins[0]
        return PowerDomainResolution(voltage=v, net=net, resolution="single_vcc")
    # >1 confirmed-rail pin: ambiguous only if the rails DISAGREE on voltage.
    distinct_voltages = {v for _, v in vcc_pins}
    if len(distinct_voltages) == 1:
        net, v = vcc_pins[0]
        return PowerDomainResolution(voltage=v, net=net, resolution="unanimous_rails")
    # Genuine multi-voltage ambiguity: unless extraction disambiguates.
    # _has_per_pin_power_domain is the escape hatch for that case; it is
    # permanently-False until ValidatedPinSpec.power_domain exists (see its
    # docstring) — kept wired here unchanged, not removed or extended.
    if _has_per_pin_power_domain(refdes, pin_specs):
        net, v = vcc_pins[0]  # preserve existing first-iter behavior for extraction-grounded parts
        return PowerDomainResolution(voltage=v, net=net, resolution="per_pin_resolved")
    return None


def _unresolved_driver_voltage_reason(
    refdes: str | None,
    ir: NetlistIR,
    confirmed_voltages: dict[str, float],
) -> str:
    """Classify why _resolve_power_domain returned no voltage, for the
    UNRESOLVABLE message (Option 3: the message used to conflate two different
    conditions — "no resolved datasheet" and "power domain ambiguous" — into
    one string, even when the driver's part_number was known; see the recon's
    §2(d) finding). Read-only re-derivation of _resolve_power_domain's own
    membership test; never changes PowerDomainResolution's contract.

    The "no resolved datasheet" branch (comp not found in ir.components) is
    currently unreachable from run()'s call site — driver_ref always comes
    from a component already found via comp_by_ref — but is kept as the
    honest classification for a refdes with no component of record, and to
    keep this function correct if ever called on an unvalidated refdes.
    """
    comp = next((c for c in ir.components if c.refdes == refdes), None) if refdes else None
    if comp is None:
        return f"Driver voltage unknown — {refdes or 'unknown'} has no resolved datasheet"
    vcc_pins = [(pin.net, confirmed_voltages[pin.net])
                for pin in comp.pins
                if pin.net in confirmed_voltages and confirmed_voltages[pin.net] is not None]
    if not vcc_pins:
        return f"Driver voltage unknown — {refdes} has no confirmed rail on any pin"
    distinct_voltages = sorted({v for _, v in vcc_pins})
    return (
        f"Driver voltage unknown — {refdes} power domain ambiguous "
        f"({len(vcc_pins)} rail pin(s), {len(distinct_voltages)} distinct voltage(s): "
        f"{distinct_voltages})"
    )


def _component_power_domain(
    refdes: str,
    ir: NetlistIR,
    confirmed_voltages: dict[str, float],
    pin_specs: dict[tuple[str, str], "ValidatedPinSpec"],
) -> float | None:
    """Backward-compatible wrapper over `_resolve_power_domain` returning just
    the voltage. Preserved for existing callers/tests that assert on `float|None`."""
    r = _resolve_power_domain(refdes, ir, confirmed_voltages, pin_specs)
    return r.voltage if r else None


def _get_component_vcc_voltage(
    refdes: str,
    components: list,
    confirmed_voltages: dict[str, float],
) -> float | None:
    """Return the highest confirmed VCC rail voltage for a component.
    Using max() handles split-rail transceivers (VCCA/VCCB) correctly.
    Accepts both named pins and synthesized numeric pins.
    """
    comp = next((c for c in components if c.refdes == refdes), None)
    if not comp:
        return None
    voltages = [
        confirmed_voltages[pin.net]
        for pin in comp.pins
        if pin.net in confirmed_voltages and confirmed_voltages[pin.net] is not None
    ]
    return max(voltages) if voltages else None


_RESISTOR_REFDES_RE = re.compile(r'^(?:R|RN)\d', re.IGNORECASE)


def _net_has_pullup_below(
    net,
    comp_by_ref: dict,
    confirmed_voltages: dict[str, float],
    driver_voltage: float,
) -> bool:
    """True if `net` carries a pull-up resistor to a confirmed rail whose voltage
    is below `driver_voltage` — the open-drain circuit signature.

    For an open-drain output the logic-HIGH level is set by this external pull-up,
    not by the device's VCC; so a resolved driver voltage above the pull-up rail
    over-states the actual net voltage. We look for a resistor (R/RN) on the net
    whose *other* pin sits on a confirmed power rail lower than driver_voltage.
    """
    for refdes, _pin_id in net.pins:
        if not _RESISTOR_REFDES_RE.match(refdes):
            continue
        comp = comp_by_ref.get(refdes)
        if comp is None:
            continue
        for pin in comp.pins:
            if pin.net == net.name:
                continue  # the resistor's foot on this same net
            v = confirmed_voltages.get(pin.net)
            if v is not None and v < driver_voltage:
                return True
    return False


def run(
    ir: NetlistIR,
    pin_specs: dict[tuple[str, str], ValidatedPinSpec],  # (refdes, pin_name) → spec
    confirmed_voltages: dict[str, float],
    rail_provenance: "dict[str, RailProvenance] | None" = None,
) -> list[CheckResult]:
    """rail_provenance: optional net name → RailProvenance (source+confidence)
    from step_06. When provided, drv_conf is provenance-calibrated; when omitted,
    falls back to the legacy presence-based labeling (backward compatible)."""
    results: list[CheckResult] = []

    # Index components by refdes
    comp_by_ref = {c.refdes: c for c in ir.components}
    # Build (refdes, pin_id) → pin_name map
    pin_name_map: dict[tuple[str, str], str] = {}
    for comp in ir.components:
        for pin in comp.pins:
            pin_name_map[(comp.refdes, pin.pin_id)] = pin.pin_name

    # Build normalized power/ground sets once for normalized net-name comparison.
    # This lets +3.3V match a confirmed_voltages entry keyed as 3.3V (or VCC_3V3).
    norm_power  = {_normalize_net_name(n) for n in confirmed_voltages}
    norm_ground = {_normalize_net_name(n) for n in ir.ground_nets}

    for net in ir.nets:
        # Skip power rail and ground nets — voltage already known, no driver/receiver to check.
        net_norm = _normalize_net_name(net.name)
        if net_norm in norm_power or net_norm in norm_ground:
            continue
        if net.name in ir.power_nets:
            continue
        if POWER_NET_RE.match(net.name) or GROUND_NET_RE.match(net.name):
            continue

        # Identify drivers and receivers on this net via priority-ordered inference
        signal_pins: list[dict] = []
        for refdes, pin_id in net.pins:
            pin_name = pin_name_map.get((refdes, pin_id), "")
            if _is_power_pin(pin_name) or _is_gnd_pin(pin_name):
                continue
            if comp_by_ref.get(refdes) is None:
                continue
            if is_passive_component(refdes):
                continue
            spec     = pin_specs.get((refdes, pin_name))
            pin_type = spec.pin_type if spec else None
            vcc      = _get_component_vcc_voltage(refdes, ir.components, confirmed_voltages)
            signal_pins.append({"refdes": refdes, "pin_name": pin_name,
                                 "pin_type": pin_type, "vcc": vcc,
                                 "pin_type_exact": spec.pin_type_exact if spec else False,
                                 "open_drain": spec.open_drain if spec else False})

        # If all pins on this net are passive, skip without generating a result
        all_pins_on_net = [
            refdes for refdes, _ in net.pins
            if comp_by_ref.get(refdes) is not None
        ]
        if not signal_pins and all_pins_on_net and all(
            is_passive_component(r) for r in all_pins_on_net
        ):
            continue

        # Priority 1: explicit output pin drives; everything else receives.
        # A pin only counts as a genuine push-pull driver (trusted to assert its
        # VCC as an overvoltage) when its output type came from a POSITIVE
        # example_pins match AND the group is not documented open-drain. This
        # excludes two false-positive classes surfaced by the Haiku re-extraction:
        #   • a control/logic INPUT mis-typed as output via the default_group
        #     fallback (ULN2003 base input → collector-output default), and
        #   • a documented open-drain output that sinks-only (AP22615 FLG).
        # Both then fall to the priority-2/3 path and are downgraded (non-push-pull
        # FAIL/WARN → UNRESOLVABLE) by the driver-type guard below. A genuine,
        # positively-matched push-pull output (74HC595 QA → STM32) is untouched.
        explicit_out = [p for p in signal_pins
                        if p["pin_type"] in {"output", "digital_output", "power_output"}
                        and p.get("pin_type_exact")
                        and not p.get("open_drain")]
        if explicit_out:
            drv_pins      = explicit_out
            recv_pins     = [p for p in signal_pins if p not in explicit_out]
            driver_priority = "driver_explicit_output"
        else:
            # Priority 2: no explicit output — highest-VCC component drives
            # (conservative: higher supply → more likely to overdrive the lower-voltage side)
            with_vcc = sorted(
                [p for p in signal_pins if p["vcc"] is not None],
                key=lambda p: p["vcc"], reverse=True,
            )
            if len(with_vcc) >= 2:
                drv_pins        = [with_vcc[0]]
                recv_pins       = [p for p in signal_pins if p is not with_vcc[0]]
                driver_priority = "driver_highest_vcc_bidirectional"
            elif len(with_vcc) == 1:
                drv_pins        = with_vcc
                recv_pins       = [p for p in signal_pins if p is not with_vcc[0]]
                driver_priority = "driver_highest_vcc_bidirectional"
            else:
                # Priority 3: no VCC info — first pin is driver, rest receive
                drv_pins        = signal_pins[:1]
                recv_pins       = signal_pins[1:]
                driver_priority = "driver_first_pin"

        drivers   = [(p["refdes"], p["pin_name"]) for p in drv_pins]
        receivers = [(p["refdes"], p["pin_name"]) for p in recv_pins]

        if not receivers:
            continue

        for recv_ref, recv_pin_name in receivers:
            spec = pin_specs.get((recv_ref, recv_pin_name))
            VIH_max        = spec.VIH_max if spec else None
            VIH_min        = spec.VIH_min if spec else None
            abs_max        = spec.absolute_max_voltage if spec else None
            recv_conf      = spec.confidence if spec else "low"
            source_snippet = spec.source_snippet if spec else None

            source_evidence = (
                f"Confirmed — {source_snippet[:80]}"
                if source_snippet and recv_conf != "low"
                else "Assumed/low-confidence"
            )

            driver_ref = drivers[0][0] if drivers else None
            resolution: PowerDomainResolution | None = None
            if driver_ref:
                resolution = _resolve_power_domain(driver_ref, ir, confirmed_voltages, pin_specs)

            # Driver-type guard (applied at the verdict, below): only a genuine
            # push-pull output (the priority-1 explicit-output path) is confirmed to
            # actively drive its VCC rail onto the net. A driver chosen by the
            # highest-VCC or first-pin fallback (priority 2/3) is bidirectional /
            # passive (analog-switch throw) / a power or connector pin — none of
            # which is confirmed to push its VCC onto the net. We therefore do NOT
            # trust such a driver to assert an OVERVOLTAGE: any FAIL/WARN it would
            # produce is downgraded to UNRESOLVABLE. A PASS (driver VCC <= receiver
            # limit) is KEPT — the net cannot exceed the driver's own VCC, so a
            # passing margin is safe regardless of pin type, and dropping it would be
            # a false coverage loss.
            driver_is_pushpull = (driver_priority == "driver_explicit_output")
            driver_voltage = resolution.voltage if resolution else None

            # Open-drain refinement: a pin the datasheet/symbol types as "output" can
            # still be OPEN-DRAIN (e.g. an AP22615 FLG fault flag) — its logic-HIGH
            # level is set by an external pull-up, not by the device's VCC. The
            # circuit signature is a pull-up resistor on the net to a rail BELOW the
            # driver's resolved VCC; when present, the driver's VCC over-states the
            # net voltage, so an over-voltage verdict from it is not trustworthy.
            # Minimum-viable handling (trap-proof — we do not assert the pull-up
            # voltage as fact): treat the push-pull driver as unconfirmed for this
            # net so any FAIL/WARN is downgraded to UNRESOLVABLE (clears the AP22615
            # open-drain false positive triaged in 81c3e84).
            driver_open_drain_like = (
                driver_is_pushpull and driver_voltage is not None
                and _net_has_pullup_below(net, comp_by_ref, confirmed_voltages, driver_voltage)
            )

            has_driver_voltage = driver_voltage is not None
            has_receiver_specs = (VIH_min is not None or VIH_max is not None or abs_max is not None)

            # v2_10.4: provenance-calibrated drv_conf. Legacy presence-based path
            # when no rail_provenance supplied (backward compat for callers/tests).
            if rail_provenance is None:
                drv_conf = "high" if has_driver_voltage else "low"
                driver_voltage_source = "power_domain" if has_driver_voltage else "unknown"
            elif resolution is not None:
                prov = rail_provenance.get(resolution.net)
                drv_conf = _driver_confidence(prov, resolution.resolution)
                driver_voltage_source = prov.source if prov else "power_domain"
            else:
                drv_conf = "low"
                driver_voltage_source = "unknown"
            combined_conf = combined_confidence(drv_conf, recv_conf)  # Fix 3

            if not has_driver_voltage and not has_receiver_specs:
                results.append(CheckResult(
                    net_name=net.name,
                    status="UNRESOLVABLE",
                    driver_refdes=driver_ref,
                    driver_voltage=None,
                    driver_voltage_source="unknown",
                    receiver_refdes=recv_ref,
                    receiver_pin_name=recv_pin_name,
                    receiver_VIH_max=None,
                    receiver_abs_max=None,
                    receiver_confidence="low",
                    driver_confidence="low",
                    combined_confidence="low",
                    evidence_label="Unresolvable — no driver voltage or receiver specs available",
                    unresolvable_reason="Neither driver voltage nor receiver pin specs could be determined",
                    receiver_VIH_min=None,
                ))
                write_trace(
                    net_name=net.name, step_id="step_08_checker",
                    step_display_name="Net compatibility checker", step_type="deterministic",
                    decision_path="unresolvable_missing_data",
                    inputs={"driver_refdes": driver_ref, "receiver_refdes": recv_ref,
                            "receiver_pin": recv_pin_name, "driver_priority": driver_priority},
                    outputs={"status": "UNRESOLVABLE", "reason": "no driver voltage or receiver specs"},
                )
                continue

            if not has_driver_voltage:
                results.append(CheckResult(
                    net_name=net.name,
                    status="UNRESOLVABLE",
                    driver_refdes=driver_ref,
                    driver_voltage=None,
                    driver_voltage_source="unknown",
                    receiver_refdes=recv_ref,
                    receiver_pin_name=recv_pin_name,
                    receiver_VIH_max=VIH_max,
                    receiver_abs_max=abs_max,
                    receiver_confidence=recv_conf,
                    driver_confidence="low",
                    combined_confidence="low",
                    evidence_label=source_evidence,
                    unresolvable_reason=_unresolved_driver_voltage_reason(driver_ref, ir, confirmed_voltages),
                    receiver_VIH_min=VIH_min,
                ))
                write_trace(
                    net_name=net.name, step_id="step_08_checker",
                    step_display_name="Net compatibility checker", step_type="deterministic",
                    decision_path="unresolvable_no_driver_voltage",
                    inputs={"driver_refdes": driver_ref, "receiver_refdes": recv_ref,
                            "receiver_pin": recv_pin_name, "driver_priority": driver_priority},
                    outputs={"status": "UNRESOLVABLE", "abs_max": abs_max, "VIH_max": VIH_max},
                )
                continue

            if not has_receiver_specs:
                results.append(CheckResult(
                    net_name=net.name,
                    status="UNRESOLVABLE",
                    driver_refdes=driver_ref,
                    driver_voltage=driver_voltage,
                    driver_voltage_source=driver_voltage_source,
                    receiver_refdes=recv_ref,
                    receiver_pin_name=recv_pin_name,
                    receiver_VIH_max=None,
                    receiver_abs_max=None,
                    receiver_confidence="low",
                    driver_confidence=drv_conf,
                    combined_confidence="low",
                    evidence_label="Assumed/low-confidence",
                    unresolvable_reason=f"Receiver specs unknown — {recv_ref} pin specs not extracted (signal_score too low or datasheet section not found)",
                    receiver_VIH_min=None,
                ))
                write_trace(
                    net_name=net.name, step_id="step_08_checker",
                    step_display_name="Net compatibility checker", step_type="deterministic",
                    decision_path="unresolvable_no_receiver_specs",
                    inputs={"driver_refdes": driver_ref, "driver_voltage": driver_voltage,
                            "receiver_refdes": recv_ref, "receiver_pin": recv_pin_name,
                            "driver_priority": driver_priority},
                    outputs={"status": "UNRESOLVABLE", "driver_voltage": driver_voltage},
                )
                continue

            # Fix 4: correct FAIL/WARN/PASS semantics — VIH_min is a lower bound, never upper
            status, compat_evidence = evaluate_compatibility(
                driver_voltage=driver_voltage,
                receiver_VIH_min=VIH_min,
                receiver_VIH_max=VIH_max,
                receiver_abs_max=abs_max,
            )

            # Driver-type guard (see note above): a non-push-pull driver (priority
            # 2/3 fallback — bidirectional / passive analog-switch throw / open-drain
            # flag / power/connector pin) is not confirmed to actively drive its VCC
            # onto the net, so it cannot justify an OVERVOLTAGE verdict. Downgrade
            # any FAIL/WARN it would produce to UNRESOLVABLE. PASS is kept (the net
            # cannot exceed the driver's own VCC, so a passing margin is safe and
            # dropping it would be a false coverage loss). This clears the analog
            # -switch / open-drain / split-rail FALSE POSITIVES triaged in 81c3e84;
            # the real push-pull overdrive (5V '595 output → 3.3V input) is priority
            # -1 and untouched.
            if (not driver_is_pushpull or driver_open_drain_like) and status in ("FAIL", "WARN"):
                results.append(CheckResult(
                    net_name=net.name,
                    status="UNRESOLVABLE",
                    driver_refdes=driver_ref,
                    driver_voltage=driver_voltage,
                    driver_voltage_source=driver_voltage_source,
                    receiver_refdes=recv_ref,
                    receiver_pin_name=recv_pin_name,
                    receiver_VIH_max=VIH_max,
                    receiver_abs_max=abs_max,
                    receiver_confidence=recv_conf,
                    driver_confidence=drv_conf,
                    combined_confidence="low",
                    evidence_label=(
                        f"Driver {driver_ref} pin is not a confirmed push-pull output "
                        f"(bidirectional/passive/open-drain/fallback) — its VCC is not "
                        f"asserted onto the net, so the would-be {status} "
                        f"({compat_evidence}) is not established"
                    ),
                    unresolvable_reason=(
                        f"Driver {driver_ref} is not a push-pull output; over-voltage "
                        f"verdict from a non-driving pin's VCC is not trustworthy"
                    ),
                    receiver_VIH_min=VIH_min,
                ))
                write_trace(
                    net_name=net.name, step_id="step_08_checker",
                    step_display_name="Net compatibility checker", step_type="deterministic",
                    decision_path="unresolvable_driver_not_pushpull",
                    inputs={"driver_refdes": driver_ref, "driver_voltage": driver_voltage,
                            "receiver_refdes": recv_ref, "receiver_pin": recv_pin_name,
                            "driver_priority": driver_priority, "would_be_status": status},
                    outputs={"status": "UNRESOLVABLE", "downgraded_from": status},
                )
                continue

            results.append(CheckResult(
                net_name=net.name,
                status=status,
                driver_refdes=driver_ref,
                driver_voltage=driver_voltage,
                driver_voltage_source=driver_voltage_source,
                receiver_refdes=recv_ref,
                receiver_pin_name=recv_pin_name,
                receiver_VIH_max=VIH_max,
                receiver_abs_max=abs_max,
                receiver_confidence=recv_conf,
                driver_confidence=drv_conf,
                combined_confidence=combined_conf,
                evidence_label=compat_evidence,
                unresolvable_reason=None,
                receiver_VIH_min=VIH_min,
            ))
            write_trace(
                net_name=net.name, step_id="step_08_checker",
                step_display_name="Net compatibility checker", step_type="deterministic",
                decision_path=driver_priority,
                inputs={"driver_refdes": driver_ref, "driver_voltage": driver_voltage,
                        "receiver_refdes": recv_ref, "receiver_pin": recv_pin_name,
                        "abs_max": abs_max, "VIH_min": VIH_min, "VIH_max": VIH_max},
                outputs={"status": status, "evidence": compat_evidence,
                         "combined_confidence": combined_conf},
            )

    return results

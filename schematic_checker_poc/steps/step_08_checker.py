import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import NamedTuple
from steps.step_02_parser import NetlistIR
from steps.step_05_validator import ValidatedPinSpec
from steps.step_06_power import POWER_NET_RE, GROUND_NET_RE
from steps.part_class import classify_part
from steps import pin_supply_map
from trace.writer import write_trace

logger = logging.getLogger(__name__)

PASSIVE_REFDES_RE = re.compile(
    r'^(?:'
    r'R|RN|C|CN|L|FB|D|LED|F|T|TR|Y|XTAL|XT|'   # passives + resistor/cap networks
    r'Q|QN|'                                       # discrete transistors / MOSFETs
    r'J|P|CON|X|'                                  # connectors (CN stays in passives above)
    r'PPTC|FUSE|PTC|NTC|'                          # polyfuses, fuses, thermistors
    r'SW|BTN|KEY|PB|'                              # switches
    r'TP|FID|MH|H'                                 # test points, fiducials, mounting holes
    r')(?:\d+|\?)',                                # "?" = unannotated (TODO-554)
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
    resolution: str    # "single_vcc" | "unanimous_rails" | "pin_supply_map"
    # TODO-415: for "pin_supply_map", the supply pin and the datasheet citation
    # the voltage rests on; appended to the row's evidence label.
    detail: "str | None" = None


# A resolution listed here means "we know this voltage is the pin's"; the
# weakest-link combination already catches a flaky (low-confidence) rail.
# pin_supply_map (TODO-415): the pin's supply is named by a datasheet-cited rule.
# unanimous_rails (TODO-415 Rulings decision 19): several confirmed supply pins
# that all show one voltage are as certain as one; the entry was missing, so
# such drivers read low.
# A resolution not listed reads as low.
_RESOLUTION_CONF = {"single_vcc": "high", "unanimous_rails": "high",
                    "pin_supply_map": "high"}


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


class SignalUnresolvableReason(Enum):
    """Why a signal row is UNRESOLVABLE (TODO-557 / TODO-558 section 5). The
    human-readable text for the row is CheckResult.unresolvable_detail."""
    NO_DRIVER_VOLTAGE_NO_RECEIVER_SPECS = "NO_DRIVER_VOLTAGE_NO_RECEIVER_SPECS"
    DRIVER_NO_DATASHEET = "DRIVER_NO_DATASHEET"
    DRIVER_NO_CONFIRMED_RAIL = "DRIVER_NO_CONFIRMED_RAIL"
    DRIVER_DOMAIN_AMBIGUOUS = "DRIVER_DOMAIN_AMBIGUOUS"
    RECEIVER_SPECS_NOT_EXTRACTED = "RECEIVER_SPECS_NOT_EXTRACTED"
    DRIVER_NOT_PUSH_PULL = "DRIVER_NOT_PUSH_PULL"
    RECEIVER_DEFAULT_GROUP = "RECEIVER_DEFAULT_GROUP"
    RECEIVER_SPECS_UNRESOLVED = "RECEIVER_SPECS_UNRESOLVED"
    NO_DRIVER_ON_NET = "NO_DRIVER_ON_NET"
    MERGED_REFDES = "MERGED_REFDES"  # TODO-569: driver or receiver refdes is merged


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
    unresolvable_reason: SignalUnresolvableReason | None
    receiver_VIH_min: float | None = None   # Fix 4: stored separately from VIH_max
    unresolvable_detail: str | None = None  # TODO-557: the prose for unresolvable_reason
    driver_pin_name: str | None = None      # TODO-557: the driver's pin on this net
    driver_selection: str | None = None     # TODO-557: how the driver was chosen


# Pin names that indicate a VCC/power supply pin
_VCC_NAMES = {"vcc", "vdd", "avdd", "dvdd", "vbat", "v+", "pwr"}
_GND_NAMES = {"gnd", "vss", "agnd", "dgnd", "v-", "gnd_ref"}
_OUTPUT_TYPES = {"output", "bidirectional", "digital_output"}
_INPUT_TYPES = {"input", "bidirectional", "digital_input", "analog"}


def _is_power_pin(pin_name: str) -> bool:
    return pin_name.lower() in _VCC_NAMES or pin_name.lower().startswith("vcc") or pin_name.lower().startswith("vdd")


def _is_gnd_pin(pin_name: str) -> bool:
    return pin_name.lower() in _GND_NAMES


def _first_component(ir: NetlistIR, refdes: "str | None"):
    """The first component carrying refdes, or None."""
    return next((c for c in ir.components if c.refdes == refdes), None) if refdes else None


def _resolve_power_domain(
    refdes: str,
    ir: NetlistIR,
    confirmed_voltages: dict[str, float],
    pin_specs: dict[tuple[str, str], "ValidatedPinSpec"],
    pin_name: "str | None" = None,
) -> "PowerDomainResolution | None":
    """Resolve the voltage a pin of a component runs from, with resolution
    provenance, else None.

    Per-pin supply map first (TODO-415), when pin_name is given and
    steps/pin_supply_map.py has a rule for (part, pin) whose supply pin is on the
    component: the voltage is that supply pin's confirmed rail voltage, tagged
    "pin_supply_map". If that supply is on no confirmed rail, or on rails of more
    than one voltage, the result is None: a mapped pin never falls back to another
    supply of the part. Where the map is silent, the part-level logic below runs.

    Part-level logic (U1 power-domain fix, Options 1+2 — see the U1 power-domain
    recon and its projection):
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
    - >1 confirmed-rail pin, genuinely disagreeing on voltage → None. Which rail
      a given pin runs from is not known at part level; only a supply-map rule
      resolves such a pin.

    Pre-Fix-α logic returned the first-iterated VCC pin's voltage in all
    cases, which produced wrong attributions on multi-VCC parts (FPGAs,
    connectors, modules). See
    `corpus_results/cross_board_confident_wrong_analysis.md` for the cases that
    motivated the guard. The guard itself (pre-Options-1+2) then over-rejected:
    it counted PINS, not DISTINCT VOLTAGES, so a same-voltage multi-rail MCU
    (the common case) was misclassified as ambiguous.

    pin_specs is unused (kept for the callers' signature).

    Returns the matched net name so callers can look up rail-classification
    provenance (v2_10.4 drv_conf calibration).
    """
    comp = _first_component(ir, refdes)
    if comp is None:
        return None
    if pin_name is not None:
        mapped = pin_supply_map.pin_supply(comp, pin_name, confirmed_voltages)
        if mapped is not None:
            if mapped.voltage is None:
                return None
            return PowerDomainResolution(
                voltage=mapped.voltage, net=mapped.net, resolution="pin_supply_map",
                detail=(f"driver pin {pin_name} runs from {mapped.supply} per the "
                        f"supply map: {mapped.citation}"))
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
    # Genuine multi-voltage ambiguity and no supply-map rule for this pin.
    return None


def _unresolved_driver_voltage_reason(
    refdes: str | None,
    ir: NetlistIR,
    confirmed_voltages: dict[str, float],
    pin_name: "str | None" = None,
) -> tuple[SignalUnresolvableReason, str]:
    """(code, prose) — classify why _resolve_power_domain returned no voltage, for the
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

    TODO-415: when pin_name is given and the supply map has a rule for the pin,
    the reason is about that rule's supply pin: on no confirmed rail
    (DRIVER_NO_CONFIRMED_RAIL), or on rails of more than one voltage
    (DRIVER_DOMAIN_AMBIGUOUS). No new reason code.
    """
    comp = _first_component(ir, refdes)
    if comp is None:
        return (SignalUnresolvableReason.DRIVER_NO_DATASHEET,
                f"Driver voltage unknown — {refdes or 'unknown'} has no resolved datasheet")
    if pin_name is not None:
        mapped = pin_supply_map.pin_supply(comp, pin_name, confirmed_voltages)
        if mapped is not None and mapped.state == "SUPPLY_MIXED":
            return (SignalUnresolvableReason.DRIVER_DOMAIN_AMBIGUOUS,
                    f"Driver voltage unknown — {refdes} pin {pin_name} runs from "
                    f"{mapped.supply} (supply map), whose pins are on rails of more "
                    f"than one voltage: {list(mapped.voltages)}")
        if mapped is not None and mapped.state == "SUPPLY_UNCONFIRMED":
            return (SignalUnresolvableReason.DRIVER_NO_CONFIRMED_RAIL,
                    f"Driver voltage unknown — {refdes} pin {pin_name} runs from "
                    f"{mapped.supply} (supply map), which is on no confirmed rail")
    vcc_pins = [(pin.net, confirmed_voltages[pin.net])
                for pin in comp.pins
                if pin.net in confirmed_voltages and confirmed_voltages[pin.net] is not None]
    if not vcc_pins:
        return (SignalUnresolvableReason.DRIVER_NO_CONFIRMED_RAIL,
                f"Driver voltage unknown — {refdes} has no confirmed rail on any pin")
    distinct_voltages = sorted({v for _, v in vcc_pins})
    return SignalUnresolvableReason.DRIVER_DOMAIN_AMBIGUOUS, (
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


def _rank_voltage(
    refdes: str,
    pin_name: str,
    ir: NetlistIR,
    confirmed_voltages: dict[str, float],
) -> float | None:
    """The voltage an endpoint pin is ranked at for the highest-rail driver choice
    (TODO-415): the pin's supply-map voltage when a rule applies to it (None when
    that supply has no single confirmed voltage), else the part's highest
    confirmed rail as before."""
    mapped = pin_supply_map.pin_supply(
        _first_component(ir, refdes), pin_name, confirmed_voltages)
    if mapped is not None:
        return mapped.voltage
    return _get_component_vcc_voltage(refdes, ir.components, confirmed_voltages)


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


def _driver_ineligible(p: dict) -> bool:
    """TODO-557: a signal endpoint that may never be picked as a net's driver —
    an open-drain pin (any match, default group included), or a pin whose
    extracted group positively types it as an input. It stays a receiver."""
    return bool(p.get("open_drain")) or (
        p.get("pin_type") in ("digital_input", "input") and bool(p.get("pin_type_exact")))


# TODO-557: report value of CheckResult.driver_selection per driver_priority path.
_DRIVER_SELECTION = {
    "driver_explicit_output": "explicit_output",
    "driver_highest_vcc_bidirectional": "highest_rail",
    "driver_first_pin": "first_pin",
}

_NO_DRIVER_DETAIL = ("No driver on this net in the netlist: every endpoint is an input "
                     "pin, an open-drain pin or a non-IC part")


_REGEX_ONLY_SKIP_LOGGED = False


def _note_regex_only_skip(net_name: str) -> None:
    """TODO-196 tripwire: log once per process when a net is skipped by the
    POWER_NET_RE / GROUND_NET_RE arm alone."""
    global _REGEX_ONLY_SKIP_LOGGED
    if not _REGEX_ONLY_SKIP_LOGGED:
        _REGEX_ONLY_SKIP_LOGGED = True
        logger.warning(
            "[STEP 08] TODO-196 tripwire: net %r was skipped by the POWER_NET_RE / "
            "GROUND_NET_RE name pattern alone (not in confirmed_voltages, ground_nets "
            "or power_nets). This arm was believed unreachable; further occurrences "
            "in this run are not logged.", net_name)


def run(
    ir: NetlistIR,
    pin_specs: dict[tuple[str, str], ValidatedPinSpec],  # (refdes, pin_name) → spec
    confirmed_voltages: dict[str, float],
    rail_provenance: "dict[str, RailProvenance] | None" = None,
    *,
    not_checked_out: list | None = None,
) -> list[CheckResult]:
    """rail_provenance: optional net name → RailProvenance (source+confidence)
    from step_06. When provided, drv_conf is provenance-calibrated; when omitted,
    falls back to the legacy presence-based labeling (backward compatible).

    TODO-554: a part recognised as non-IC by part_class.classify_part whose refdes
    the passive regex misses (a GAP part) is never a driver, and is a receiver only
    on a pin with extracted limits. not_checked_out, when given, is extended with one
    NON_IC_PART dict per recognised part that has a signal pin on a net with an IC
    endpoint and is not the receiver of a row there.

    TODO-556 decision 17: a GAP part classed protection_diode (a TVS / ESD array) is
    never a signal endpoint, with or without limits; it is disclosed as NON_IC_PART.
    TODO-556 decision 16 (TODO-567): a receiver that is the same part instance as the
    net's driver gets no row; each such part is disclosed once, after the NON_IC_PART
    entries, as a SAME_INSTANCE dict (nets, pins, pins_with_rows)."""
    results: list[CheckResult] = []

    # Index components by refdes
    comp_by_ref = {c.refdes: c for c in ir.components}

    # TODO-554: classify each component once; a refdes is RECOGNISED only if every
    # component carrying it is, with the first such component's class/evidence.
    positions_by_ref: dict[str, list[int]] = {}
    for i, c in enumerate(ir.components):
        positions_by_ref.setdefault(c.refdes, []).append(i)
    classes = [classify_part(c) for c in ir.components]
    recognised: dict[str, tuple[str, str]] = {}
    for ref, idxs in positions_by_ref.items():
        if all(classes[i] is not None for i in idxs):
            recognised[ref] = classes[idxs[0]]
    gap_refs = {ref for ref in recognised if not is_passive_component(ref)}
    # (refdes) -> set of (net, pin_name) qualifying disclosure pins (step 4g)
    disclosure_pins: dict[str, set[tuple[str, str]]] = {}
    # TODO-556 decision 16: (refdes) -> set of (net, pin_name) whose row was dropped
    # because the receiver is the driver's own part instance.
    same_instance: dict[str, set[tuple[str, str]]] = {}
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
        # The two skips above are expected to cover every net this pattern matches;
        # the tripwire reports the first one they do not (TODO-196).
        if POWER_NET_RE.match(net.name) or GROUND_NET_RE.match(net.name):
            _note_regex_only_skip(net.name)
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
            is_gap = refdes in gap_refs
            if is_gap and recognised[refdes][0] == "protection_diode":
                continue  # TODO-556 decision 17: a protection diode is never an endpoint
            if is_gap and not (spec is not None and (
                    spec.VIH_min is not None or spec.VIH_max is not None
                    or spec.absolute_max_voltage is not None)):
                continue  # TODO-554: GAP pin without extracted limits is not an endpoint
            pin_type = spec.pin_type if spec else None
            vcc      = _rank_voltage(refdes, pin_name, ir, confirmed_voltages)
            signal_pins.append({"refdes": refdes, "pin_name": pin_name,
                                 "pin_type": pin_type, "vcc": vcc,
                                 "pin_type_exact": spec.pin_type_exact if spec else False,
                                 "open_drain": spec.open_drain if spec else False,
                                 "gap": is_gap})

        # TODO-554 step 4g: candidate disclosure pins — recognised parts' signal
        # pins on a net that has at least one IC (non-GAP) endpoint.
        if not_checked_out is not None and any(not p["gap"] for p in signal_pins):
            for refdes, pin_id in net.pins:
                if refdes not in recognised or comp_by_ref.get(refdes) is None:
                    continue
                pin_name = pin_name_map.get((refdes, pin_id), "")
                if _is_power_pin(pin_name) or _is_gnd_pin(pin_name):
                    continue
                disclosure_pins.setdefault(refdes, set()).add((net.name, pin_name))

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
        # TODO-554 / TODO-557: a non-IC (GAP) part, an open-drain pin and a pin
        # typed as an input by its own extracted group are never drivers under any
        # priority. Driver choice runs over the remaining endpoints only; every
        # other endpoint, ineligible ones included, is a receiver.
        drv_candidates = [p for p in signal_pins
                          if not p["gap"] and not _driver_ineligible(p)]
        explicit_out = [p for p in drv_candidates
                        if p["pin_type"] in {"output", "digital_output", "power_output"}
                        and p.get("pin_type_exact")
                        and not p.get("open_drain")]
        if explicit_out:
            drv_pins      = explicit_out
            recv_pins     = [p for p in signal_pins if p not in explicit_out]
            driver_priority = "driver_explicit_output"
        else:
            # Priority 2: no explicit output — the endpoint with the highest rail
            # drives (conservative: higher supply → more likely to overdrive the
            # lower-voltage side). TODO-415: p["vcc"] is the pin's own supply-map
            # voltage where a rule applies, else the part's highest confirmed rail.
            with_vcc = sorted(
                [p for p in drv_candidates if p["vcc"] is not None],
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
                drv_pins        = drv_candidates[:1]
                recv_pins       = [p for p in signal_pins
                                   if not any(p is d for d in drv_pins)]
                driver_priority = "driver_first_pin"

        drivers   = [(p["refdes"], p["pin_name"]) for p in drv_pins]
        receivers = [(p["refdes"], p["pin_name"]) for p in recv_pins]

        if not receivers:
            continue
        # TODO-557: no eligible driver. When the net has an IC endpoint and at
        # least two signal endpoints, every receiver gets an UNRESOLVABLE
        # NO_DRIVER_ON_NET row; otherwise the net stays silent.
        if not drivers:
            if any(not p["gap"] for p in signal_pins) and len(signal_pins) >= 2:
                for p in recv_pins:
                    _s = pin_specs.get((p["refdes"], p["pin_name"]))
                    results.append(CheckResult(
                        net_name=net.name,
                        status="UNRESOLVABLE",
                        driver_refdes=None,
                        driver_voltage=None,
                        driver_voltage_source="unknown",
                        receiver_refdes=p["refdes"],
                        receiver_pin_name=p["pin_name"],
                        receiver_VIH_max=_s.VIH_max if _s else None,
                        receiver_abs_max=_s.absolute_max_voltage if _s else None,
                        receiver_confidence=_s.confidence if _s else "low",
                        driver_confidence="low",
                        combined_confidence="low",
                        evidence_label="Unresolvable — no driver on this net in the netlist",
                        unresolvable_reason=SignalUnresolvableReason.NO_DRIVER_ON_NET,
                        receiver_VIH_min=_s.VIH_min if _s else None,
                        unresolvable_detail=_NO_DRIVER_DETAIL,
                        driver_pin_name=None,
                        driver_selection="none",
                    ))
                    write_trace(
                        net_name=net.name, step_id="step_08_checker",
                        step_display_name="Net compatibility checker", step_type="deterministic",
                        decision_path="unresolvable_no_driver_on_net",
                        inputs={"driver_refdes": None, "receiver_refdes": p["refdes"],
                                "receiver_pin": p["pin_name"]},
                        outputs={"status": "UNRESOLVABLE", "reason": "NO_DRIVER_ON_NET"},
                    )
            continue

        start = len(results)
        for recv_ref, recv_pin_name in receivers:
            if recv_ref == drivers[0][0]:
                # TODO-556 decision 16 (TODO-567): the receiver is the driver's own
                # part instance; such a row asserts nothing. No row; disclosed below.
                same_instance.setdefault(recv_ref, set()).add((net.name, recv_pin_name))
                continue
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
                resolution = _resolve_power_domain(
                    driver_ref, ir, confirmed_voltages, pin_specs, pin_name=drivers[0][1])

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
                    unresolvable_reason=SignalUnresolvableReason.NO_DRIVER_VOLTAGE_NO_RECEIVER_SPECS,
                    receiver_VIH_min=None,
                    unresolvable_detail="Neither driver voltage nor receiver pin specs could be determined",
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
                _code, _detail = _unresolved_driver_voltage_reason(
                    driver_ref, ir, confirmed_voltages, pin_name=drivers[0][1])
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
                    unresolvable_reason=_code,
                    receiver_VIH_min=VIH_min,
                    unresolvable_detail=_detail,
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
                    unresolvable_reason=SignalUnresolvableReason.RECEIVER_SPECS_NOT_EXTRACTED,
                    receiver_VIH_min=None,
                    unresolvable_detail=f"Receiver specs unknown — {recv_ref} pin specs not extracted (signal_score too low or datasheet section not found)",
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
            # TODO-415: a driver voltage taken from the supply map names its supply
            # pin and datasheet citation in the label (also inside the two
            # downgrade labels below, which quote compat_evidence).
            if resolution is not None and resolution.detail:
                compat_evidence += f" ({resolution.detail})"

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
                    unresolvable_reason=SignalUnresolvableReason.DRIVER_NOT_PUSH_PULL,
                    receiver_VIH_min=VIH_min,
                    unresolvable_detail=(
                        f"Driver {driver_ref} is not a push-pull output; over-voltage "
                        f"verdict from a non-driving pin's VCC is not trustworthy"
                    ),
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

            # Receiver-fallback guard (TODO-504): a receiver pin that no extracted
            # group lists takes the part's default group (pin_type_exact False).
            # Those limits are not evidence for this pin, so a FAIL/WARN from them is
            # downgraded to UNRESOLVABLE; a PASS is kept and labelled. Placed after
            # the driver-type guard so driver-guarded rows keep their text.
            if spec is not None and not spec.pin_type_exact and status in ("FAIL", "WARN"):
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
                        f"Receiver {recv_ref} pin {recv_pin_name} is not listed in any extracted "
                        f"pin group; its limits come from the part's default group, so the "
                        f"would-be {status} ({compat_evidence}) is not established"
                    ),
                    unresolvable_reason=SignalUnresolvableReason.RECEIVER_DEFAULT_GROUP,
                    receiver_VIH_min=VIH_min,
                    unresolvable_detail=(
                        f"Receiver {recv_ref} pin {recv_pin_name} matched no "
                        f"pin group; default-group limits are not evidence for this pin"
                    ),
                ))
                write_trace(
                    net_name=net.name, step_id="step_08_checker",
                    step_display_name="Net compatibility checker", step_type="deterministic",
                    decision_path="unresolvable_receiver_fallback_spec",
                    inputs={"driver_refdes": driver_ref, "driver_voltage": driver_voltage,
                            "receiver_refdes": recv_ref, "receiver_pin": recv_pin_name,
                            "driver_priority": driver_priority, "would_be_status": status},
                    outputs={"status": "UNRESOLVABLE", "downgraded_from": status},
                )
                continue
            if spec is not None and not spec.pin_type_exact and status == "PASS":
                compat_evidence += (" (receiver limits from the part's default pin group;"
                                    " pin not listed)")

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

        # TODO-557: every row off this net's driver names the driver pin and how
        # the driver was chosen.
        for r in results[start:]:
            r.driver_pin_name = drivers[0][1]
            r.driver_selection = _DRIVER_SELECTION[driver_priority]

    for r in results:
        _attach_expr_notes(r, pin_specs.get((r.receiver_refdes, r.receiver_pin_name)))

    if not_checked_out is not None:
        receiver_of_row = {(r.net_name, r.receiver_refdes, r.receiver_pin_name)
                           for r in results}
        entries: list[tuple[tuple[str, int], dict]] = []
        for ref, pins in disclosure_pins.items():
            qualifying = {(n, pn) for n, pn in pins if (n, ref, pn) not in receiver_of_row}
            if not qualifying:
                continue
            part_class, class_evidence = recognised[ref]
            nets = sorted({n for n, _ in qualifying})
            pins_with_rows = len({(r.net_name, r.receiver_pin_name)
                                  for r in results if r.receiver_refdes == ref})
            for i in positions_by_ref[ref]:
                entries.append(((ref, i), {
                    "refdes": ref,
                    "part_number": ir.components[i].part_number,
                    "part_class": part_class,
                    "class_evidence": class_evidence,
                    "checker": "signal",
                    "reason": "NON_IC_PART",
                    "nets": nets,
                    "pins_with_rows": pins_with_rows,
                }))
        entries.sort(key=lambda e: e[0])
        not_checked_out.extend(e for _, e in entries)
        # TODO-556 decision 16: one SAME_INSTANCE entry per part, sorted by refdes.
        for ref in sorted(same_instance):
            dropped = same_instance[ref]
            not_checked_out.append({
                "refdes": ref,
                "part_number": ir.components[positions_by_ref[ref][0]].part_number,
                "checker": "signal",
                "reason": "SAME_INSTANCE",
                "nets": sorted({n for n, _ in dropped}),
                "pins": sorted({pn for _, pn in dropped}),
                "pins_with_rows": len({(r.net_name, r.receiver_pin_name)
                                       for r in results if r.receiver_refdes == ref}),
            })
    return results


def _attach_expr_notes(result: CheckResult, spec: ValidatedPinSpec | None) -> None:
    """TODO-336: surface step_05's rail-expression notes on every result for a
    receiver whose spec carries them. The evidence label gains a " [note; note]"
    suffix. A "no receiver specs" UNRESOLVABLE (the only rows emitted with all
    three receiver limits None) names the notes as its reason, because an
    unresolved expression field is exactly why the spec is missing. Status is
    never touched here."""
    notes = getattr(spec, "expr_notes", None) if spec is not None else None
    if not notes:
        return
    joined = "; ".join(notes)
    result.evidence_label = f"{result.evidence_label} [{joined}]"
    if (result.status == "UNRESOLVABLE" and result.receiver_VIH_min is None
            and result.receiver_VIH_max is None and result.receiver_abs_max is None):
        # TODO-557 R7: a no-driver row keeps its own reason and detail.
        if result.unresolvable_reason is SignalUnresolvableReason.NO_DRIVER_ON_NET:
            return
        if result.driver_voltage is None:
            result.unresolvable_reason = SignalUnresolvableReason.NO_DRIVER_VOLTAGE_NO_RECEIVER_SPECS
            result.unresolvable_detail = (
                f"Neither driver voltage nor receiver pin specs could be determined — "
                f"receiver specs unresolved: {joined}")
        else:
            result.unresolvable_reason = SignalUnresolvableReason.RECEIVER_SPECS_UNRESOLVED
            result.unresolvable_detail = (
                f"Receiver specs unresolved — {result.receiver_refdes}: {joined}")

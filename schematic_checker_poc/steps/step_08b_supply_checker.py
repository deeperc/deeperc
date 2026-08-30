"""
Step 08b — Power Supply Voltage Compatibility Check

For each component, finds the net connected to its VCC/VDD/VCCA/VCCB pin,
looks up that net's confirmed voltage, and compares against the supply
voltage range extracted from the datasheet.

  Step 8  — checks SIGNAL nets between IC pins
  Step 8b — checks SUPPLY voltage on each IC's power pins
"""
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

from steps.step_05_validator import validate_supply_group
from steps.step_06_power import (
    _converter_family,
    _VIN_PIN_RE,
    _VOUT_PIN_RE,
    infer_voltage_from_name,
)

SUPPLY_PIN_NAMES = {
    'VCC', 'VDD', 'VCCA', 'VCCB', 'VCCO', 'VCCIO', 'VCCINT',
    'AVDD', 'DVDD', 'PVDD', 'IOVDD', 'COREVDD',
    'VBAT', 'VIN', 'V+', 'AVCC', 'DVCC', 'VCC1', 'VCC2',
    'VDDA', 'VDDD', 'VDDIO',
}


@dataclass
class SupplyCheckResult:
    refdes: str
    part_number: str
    supply_pin_name: str
    supply_pin_id: str
    connected_net: str
    actual_voltage: float | None
    rated_min: float | None
    rated_max: float | None
    rated_abs_max: float | None
    status: str                    # "PASS" | "FAIL" | "WARN" | "UNRESOLVABLE"
    confidence: str
    evidence_label: str
    explanation: str | None


# Trailing voltage written with a 'V' (optionally separator-led): _3V3, 1V8, 5V0.
_VOLT_V_SUFFIX_RE = re.compile(r'[_-]?\d+V\d*$')
# Trailing digit run = voltage magnitude or bank/index (optionally separator-led):
# 33 (VCC33), _44 (VCCO_44), _1 (VDDIO_1).
_DIGIT_SUFFIX_RE = re.compile(r'[_-]?\d+$')


def _supply_pin_candidates(name: str) -> set[str]:
    """Reduce-to-canonical candidate generation.

    Given an already-uppercased/stripped pin name, return the set of candidate
    stems obtained by stripping ONLY recognized voltage/bank/side decorations and
    by normalizing separators. The caller tests each candidate against the
    canonical SUPPLY_PIN_NAMES set. This can only ever recognize *decorated forms
    of an already-curated canonical name* — it can never invent a new supply
    prefix (the residual stem must itself be canonical). Arbitrary trailing
    letters are never stripped, so control/sense pins (VCC_EN, VCCO_SENSE,
    VDD_OK, ...) cannot reduce to a canonical stem.
    """
    cands: set[str] = set()
    # Side/rail separator normalization: VCC_A -> VCCA, VCC_B -> VCCB.
    no_sep = name.replace('_', '').replace('-', '')
    variants = {name, no_sep}
    for v in list(variants):
        # Strip trailing voltage-with-V pattern: VIN_3V3 -> VIN, VCC5V0 -> VCC.
        stripped_v = _VOLT_V_SUFFIX_RE.sub('', v)
        if stripped_v != v and stripped_v:
            cands.add(stripped_v)
            cands.add(stripped_v.replace('_', '').replace('-', ''))
        # Strip trailing digit run: VCC33 -> VCC, VCCO_44 -> VCCO.
        stripped_d = _DIGIT_SUFFIX_RE.sub('', v)
        if stripped_d != v and stripped_d:
            cands.add(stripped_d)
            cands.add(stripped_d.replace('_', '').replace('-', ''))
    cands.update(variants)
    return cands


def _is_supply_pin(pin_name: str) -> bool:
    if not pin_name:
        return False
    name = pin_name.upper().strip()
    if not name:
        return False
    # Fast path: exact canonical match (V+, VCC1, VCC2, VDD, ...).
    if name in SUPPLY_PIN_NAMES:
        return True
    # Reduce-to-canonical: a decorated form matches only if, after stripping a
    # recognized voltage/bank/side decoration, the residual stem is canonical.
    return any(c in SUPPLY_PIN_NAMES for c in _supply_pin_candidates(name))


def _pin_name_nominal_voltage(pin_name: str) -> float | None:
    """Best-effort nominal rail voltage encoded in a supply pin NAME.

    Used only by the rail-name/group-mismatch guard below. Returns e.g.
    VDD5 -> 5.0, VCC33 -> 3.3, VIN_3V3 -> 3.3, VCC50 -> 5.0; None when the name
    carries no voltage decoration (VCC, VDD, VCC_A, AVCC, ...).

    The single-/double-digit reading is intentionally permissive (VCC1 -> 1.0)
    because the guard only ever consults this on an already-FAIL/WARN pin and
    only fires when the value EXCEEDS the matched group's max — a benign index
    digit (1, 2) never exceeds a real supply max, so it cannot suppress a real
    over-voltage.
    """
    if not pin_name:
        return None
    name = pin_name.upper().strip()
    # 1. explicit V-form anywhere (3V3, 5V0, 1V8, _3V3, bare 5V): reuse step_06.
    v = infer_voltage_from_name(name)
    if v is not None:
        return v
    # 2. trailing two-digit decimal-coded magnitude: VCC33->3.3, VDD18->1.8.
    m = re.search(r'(\d{2})$', name)
    if m:
        return int(m.group(1)) / 10.0
    # 3. trailing single digit: VDD5->5.0, VCC3->3.0 (also VCC1->1.0, inert).
    m = re.search(r'(\d)$', name)
    if m:
        return float(m.group(1))
    return None


def _rail_name_group_mismatch(pin_name: str, rated_max: float | None,
                              rated_abs_max: float | None) -> float | None:
    """Rail-name vs matched-group mismatch detector (multi-rail parts).

    When a part exposes several distinctly-named supply rails (e.g. VCC33 *and*
    VDD5) but datasheet extraction captured only ONE power group, every supply
    pin gets matched to that single group. A pin whose NAME implies a higher
    rail than the matched group's operating max is therefore being checked
    against the WRONG rail's spec — asserting over-voltage on it is a false
    positive. Returns the pin's name-implied voltage when it exceeds the matched
    group's max (caller downgrades FAIL/WARN -> UNRESOLVABLE), else None.

    Inert for the M1 witness: VCC33 (name 3.3V) matched to its own 3.3V group
    (max 3.6V) -> 3.3 !> 3.6 -> None -> the real over-voltage FAIL is preserved.
    """
    nominal = _pin_name_nominal_voltage(pin_name)
    if nominal is None:
        return None
    ceiling = rated_max if rated_max is not None else rated_abs_max
    if ceiling is None:
        return None
    return nominal if nominal > ceiling else None


_NEG_RAIL_NAMES = {"V-", "VCC-", "VEE"}


def _norm_rail(s) -> str:
    return re.sub(r"[^A-Z0-9+\-]", "", str(s or "").upper())


def is_negative_rail(group: dict) -> bool:
    """A negative supply rail — rail_name in {V-, VCC-, VEE} (format-tolerant) OR supply_max <= 0.
    Such a group sits at a negative potential; it must never be applied as a positive over-voltage
    ceiling (a `max=0.0` would false-FAIL any positive net). The checker has no bipolar model —
    the honest posture is UNRESOLVABLE (see the caller)."""
    if _norm_rail(group.get("supply_rail_name")) in _NEG_RAIL_NAMES:
        return True
    smax = group.get("supply_max")
    try:
        return smax is not None and float(smax) <= 0
    except (TypeError, ValueError):
        return False


def _range_contains(group: dict, v: float) -> bool:
    try:
        lo, hi = group.get("supply_min"), group.get("supply_max")
        return lo is not None and hi is not None and float(lo) <= v <= float(hi)
    except (TypeError, ValueError):
        return False


def _range_distance(group: dict, v: float) -> float:
    try:
        lo, hi = float(group.get("supply_min")), float(group.get("supply_max"))
    except (TypeError, ValueError):
        return float("inf")
    return (lo - v) if v < lo else (v - hi) if v > hi else 0.0


def _pick_by_voltage(cands: list, v: float | None) -> dict:
    """Among >1 candidate power groups, pick PRINCIPLED (order-independent): the group whose
    [supply_min, supply_max] contains v; if several contain it, the first (they agree v is
    in-range); if NONE contains v, the nearest range (a true over/under-voltage still evaluates
    as such downstream — do NOT demote it); if v is unknown or a single candidate, the first
    (voltage-aware pick impossible → preserve old first-match behaviour)."""
    if v is None or len(cands) == 1:
        return cands[0]
    containing = [g for g in cands if _range_contains(g, v)]
    if containing:
        return containing[0]
    return min(cands, key=lambda g: _range_distance(g, v))


def _log_latent_flip(cands: list, picked: dict, pin_name: str, v) -> None:
    """Telemetry: the voltage-aware pick differing from old first-match is a LATENT flip the
    corpus never triggers today — a nonzero count is the tripwire, logged at debug."""
    if len(cands) > 1 and picked is not cands[0]:
        logger.debug("[step_08b] voltage-aware pick for pin %s @ %sV chose rail %r (old first-match "
                     "would pick %r)", pin_name, v, picked.get("supply_rail_name"),
                     cands[0].get("supply_rail_name"))


def _find_matching_supply_group(pin_name: str, pin_groups_result: dict | None,
                                actual_voltage: float | None = None) -> dict | None:
    if not pin_groups_result:
        return None

    pin_upper = pin_name.upper().strip()
    power = [g for g in pin_groups_result.get("pin_groups", []) if g.get("pin_type") == "power"]

    # Pass 1: exact rail_name == pin_name (the pin's OWN rail). Negatives are KEPT here — a pin
    # naming its own negative rail is routed to UNRESOLVABLE by the caller, not given a 0V ceiling.
    # LLM extractor may return non-string values; coerce defensively.
    exact = [g for g in power if str(g.get("supply_rail_name") or "").upper().strip() == pin_upper]
    if exact:
        picked = _pick_by_voltage(exact, actual_voltage)
        _log_latent_flip(exact, picked, pin_name, actual_voltage)
        return picked

    # Pass 2 fallback: a power group with a usable range, EXCLUDING negative rails so a positive
    # pin never falls back onto a negative group (TL072 V+ must not land on VCC-).
    fallback = [g for g in power
                if g.get("supply_min") is not None and g.get("supply_max") is not None
                and not is_negative_rail(g)]
    if fallback:
        picked = _pick_by_voltage(fallback, actual_voltage)
        _log_latent_flip(fallback, picked, pin_name, actual_voltage)
        return picked

    return None


def evaluate_supply(
    actual_voltage: float | None,
    rated_min: float | None,
    rated_max: float | None,
    rated_abs_max: float | None,
    abs_max_source: str | None = None,
) -> tuple[str, str]:
    """
    Returns (status, evidence_label).

    FAIL: actual > supply_abs_max (damage) or actual > supply_max (out of spec)
    WARN: actual > 0.95 * supply_abs_max (transient overshoot) or actual < supply_min (undervoltage)
    PASS: supply_min <= actual <= supply_max

    D2 label shape (TODO-398): every non-UNRESOLVABLE label carries a
    "Confirmed — " prefix (this checker asserts each of these off a
    datasheet-derived pin-group spec, never a heuristic) so step_10_report's
    existing startswith("Confirmed") tier-rewrite fires on cache-drift/
    unverified currency the same way it already does for every other
    checker's "Confirmed —" labels — see _rewrite_for_tier. UNRESOLVABLE
    keeps no prefix, matching this checker's own existing UNRESOLVABLE
    labels elsewhere in this module (though NOT step_08_checker.py's,
    which prefixes "Unresolvable — "; reported as a cross-checker
    convention difference, not changed here). `abs_max_source` is the
    pin-group's own free-text datasheet citation for the abs-max threshold
    (e.g. "Table 6: Voltage characteristics") — appended parenthetically
    only on the two labels that assert against rated_abs_max, and only
    when the caller actually has one; omitted entirely (no "(None)")
    otherwise.
    """
    if actual_voltage is None:
        return "UNRESOLVABLE", "Net voltage not confirmed"

    abs_max_cite = f" ({abs_max_source})" if abs_max_source else ""

    if rated_abs_max is not None:
        if actual_voltage > rated_abs_max:
            return "FAIL", (
                f"Confirmed — actual {actual_voltage}V exceeds supply absolute max "
                f"{rated_abs_max}V — device damage{abs_max_cite}"
            )
        if actual_voltage > 0.95 * rated_abs_max:
            return "WARN", (
                f"Confirmed — actual {actual_voltage}V is within 5% of supply abs "
                f"max {rated_abs_max}V — transient overshoot risk{abs_max_cite}"
            )

    if rated_max is not None and actual_voltage > rated_max:
        return "FAIL", (
            f"Confirmed — actual {actual_voltage}V exceeds rated supply max "
            f"{rated_max}V — out of spec, likely damage"
        )

    if rated_min is not None and actual_voltage < rated_min:
        return "WARN", (
            f"Confirmed — actual {actual_voltage}V is below rated supply min "
            f"{rated_min}V — device may not operate correctly"
        )

    return "PASS", (
        f"Confirmed — supply {actual_voltage}V within rated range "
        f"{rated_min}V - {rated_max}V"
    )


# ── Fix β / Option A: reject topologically-implausible converter VIN voltages ──
# Gemma misclassifies a converter's voltage-free, role-named VIN net (e.g.
# /Boost Converter Vin) by grabbing the board's dominant rail — on a boost that
# is the OUTPUT, producing a confident-but-wrong overvoltage FAIL. Prompt
# enrichment (Option D) does not move Gemma3:4b (see corpus_results/
# fix_beta_validation.md), so we reject the value deterministically *after*
# inference: a converter-VIN voltage that violates the part's rated input window
# (or, for a boost, is ≥ a known VOUT rail) is downgraded FAIL → UNRESOLVABLE
# rather than trusted. We deliberately do NOT fabricate a clamped number.
#
# Scope guard: only fires when the net name carries no deterministic voltage
# token (infer_voltage_from_name is None) — a named rail like P12V/P3V3 is
# trustworthy and is never second-guessed. Across the v2_10.2 corpus this
# affects only flight-computer U8.

def _ic_vout_voltage(comp, confirmed_voltages: dict) -> float | None:
    """Voltage of the converter's VOUT rail, if exactly one is known."""
    volts = {
        confirmed_voltages[p.net]
        for p in comp.pins
        if _VOUT_PIN_RE.match((p.pin_name or "").strip())
        and p.net in confirmed_voltages
    }
    return next(iter(volts)) if len(volts) == 1 else None


def _converter_vin_voltage_implausible(
    comp,
    pin,
    actual_voltage: float | None,
    rated_max: float | None,
    rated_abs_max: float | None,
    confirmed_voltages: dict,
) -> str | None:
    """Return a reason string if `actual_voltage` on this converter's VIN pin is
    topologically implausible (Gemma likely read the output rail), else None."""
    if actual_voltage is None:
        return None
    family = _converter_family(comp.part_number)
    if family is None:
        return None
    if not _VIN_PIN_RE.match((pin.pin_name or "").strip()):
        return None
    # Trust deterministically voltage-named rails; only adjudicate voltage-free
    # role-named nets (the diagnosed Gemma failure mode).
    if infer_voltage_from_name(pin.net) is not None:
        return None
    if rated_abs_max is not None and actual_voltage > rated_abs_max:
        return (f"converter VIN {actual_voltage}V exceeds rated input abs-max "
                f"{rated_abs_max}V — implausible for a {family} input; "
                f"Gemma likely misread the output rail")
    if rated_max is not None and actual_voltage > rated_max:
        return (f"converter VIN {actual_voltage}V exceeds rated input max "
                f"{rated_max}V — implausible for a {family} input; "
                f"Gemma likely misread the output rail")
    if family == "boost":
        vout = _ic_vout_voltage(comp, confirmed_voltages)
        if vout is not None and actual_voltage >= vout:
            return (f"boost VIN {actual_voltage}V ≥ VOUT {vout}V — "
                    f"topologically impossible for a boost input; "
                    f"Gemma likely misread the output rail")
    return None


def check_component_supplies(
    components: list,
    pin_groups: dict,           # part_number → pin_groups_result
    confirmed_voltages: dict,   # net_name → voltage_v
    derived_provenance: dict | None = None,  # net → {confidence, rail, voltage, bridge, bridge_type}
) -> list[SupplyCheckResult]:
    from steps.net_name_utils import normalize_net_name
    results = []
    derived_provenance = derived_provenance or {}
    _norm_prov = {normalize_net_name(n): p for n, p in derived_provenance.items()}

    def _voltage_for(net):
        v = confirmed_voltages.get(net)
        if v is not None:
            return v
        nn = normalize_net_name(net)
        for key, val in confirmed_voltages.items():
            if normalize_net_name(key) == nn:
                return val
        return None

    for comp in components:
        pn = comp.part_number
        groups_result = pin_groups.get(pn)

        # Sibling supply rails on this part: {net → voltage} across all its supply
        # pins. Used by the rail/group-mismatch guard to tell a genuine
        # undervoltage from a wrong-rail binding (a distinct sibling rail that the
        # matched group actually describes). Computed per-part, not per-pin.
        sibling_rails: dict[str, float] = {}
        for sp in comp.pins:
            if _is_supply_pin(sp.pin_name):
                sv = _voltage_for(sp.net)
                if sv is not None:
                    sibling_rails[sp.net] = sv

        for pin in comp.pins:
            if not _is_supply_pin(pin.pin_name):
                continue

            actual_voltage = _voltage_for(pin.net)

            group = _find_matching_supply_group(pin.pin_name, groups_result, actual_voltage)

            if group is not None:
                supply_val = validate_supply_group(group)
                if not supply_val.ok:
                    results.append(SupplyCheckResult(
                        refdes=comp.refdes,
                        part_number=pn,
                        supply_pin_name=pin.pin_name,
                        supply_pin_id=pin.pin_id,
                        connected_net=pin.net,
                        actual_voltage=actual_voltage,
                        rated_min=None,
                        rated_max=None,
                        rated_abs_max=None,
                        status="UNRESOLVABLE",
                        confidence="low",
                        evidence_label=f"[validator:{supply_val.reason}] {supply_val.evidence}",
                        explanation=None,
                    ))
                    continue

            if group is None:
                results.append(SupplyCheckResult(
                    refdes=comp.refdes,
                    part_number=pn,
                    supply_pin_name=pin.pin_name,
                    supply_pin_id=pin.pin_id,
                    connected_net=pin.net,
                    actual_voltage=actual_voltage,
                    rated_min=None,
                    rated_max=None,
                    rated_abs_max=None,
                    status="UNRESOLVABLE",
                    confidence="low",
                    evidence_label="Supply spec not extracted from datasheet",
                    explanation=None,
                ))
                continue

            # Negative supply rail (V-/VCC-/VEE or max<=0): the checker has no bipolar model, so a
            # negative group must not be applied as a positive over-voltage ceiling (max=0.0 would
            # false-FAIL any positive net). Route UNRESOLVABLE — honest, not wrong. Selection already
            # keeps positive pins off negative groups (fallback excludes them); this catches a pin
            # whose OWN rail is the negative one. Verdict-neutral today (no negative group is
            # currently selected for any evaluated net in the corpus).
            if is_negative_rail(group):
                results.append(SupplyCheckResult(
                    refdes=comp.refdes,
                    part_number=pn,
                    supply_pin_name=pin.pin_name,
                    supply_pin_id=pin.pin_id,
                    connected_net=pin.net,
                    actual_voltage=actual_voltage,
                    rated_min=None,
                    rated_max=None,
                    rated_abs_max=None,
                    status="UNRESOLVABLE",
                    confidence="low",
                    evidence_label="Negative supply rail (V-/VCC-/VEE) — no bipolar model; "
                                   "not evaluated as a positive-voltage ceiling",
                    explanation=None,
                ))
                continue

            def _to_float(v) -> float | None:
                if v is None:
                    return None
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None

            rated_min     = _to_float(group.get("supply_min"))
            rated_max     = _to_float(group.get("supply_max"))
            rated_abs_max = _to_float(group.get("supply_abs_max"))
            abs_max_source = group.get("abs_max_source")

            status, evidence = evaluate_supply(
                actual_voltage=actual_voltage,
                rated_min=rated_min,
                rated_max=rated_max,
                rated_abs_max=rated_abs_max,
                abs_max_source=abs_max_source,
            )

            confidence = "high" if (rated_min is not None and rated_max is not None) else "medium"
            if actual_voltage is None:
                confidence = "low"

            # Option A: a converter VIN whose value is topologically implausible
            # (over the rated input window, or — for a boost — ≥ a known VOUT
            # rail) is more likely an inference error than a real defect.
            # Downgrade to UNRESOLVABLE for human review rather than trust it.
            # Fires on the implausible value regardless of the prior PASS/FAIL —
            # a boost VIN ≥ VOUT is wrong even if it lands inside a loose window.
            reason = _converter_vin_voltage_implausible(
                comp, pin, actual_voltage, rated_max, rated_abs_max,
                confirmed_voltages,
            )
            if reason is not None:
                status = "UNRESOLVABLE"
                confidence = "low"
                evidence = f"[converter-VIN rejected] {reason}"

            # Rail-name / matched-group mismatch (multi-rail part, single group
            # extracted): a pin whose NAME implies a higher rail than the
            # matched group's max is being checked against the wrong rail's
            # spec — don't assert over-voltage, surface as UNRESOLVABLE. Only
            # downgrades FAIL/WARN; never touches a PASS, so it can only improve
            # precision. Inert on the M1 witness (VCC33 3.3 !> 3.6).
            if status in ("FAIL", "WARN"):
                mismatch_v = _rail_name_group_mismatch(
                    pin.pin_name, rated_max, rated_abs_max)
                if mismatch_v is not None:
                    status = "UNRESOLVABLE"
                    confidence = "low"
                    evidence = (
                        f"[rail-name mismatch] pin '{pin.pin_name}' implies a "
                        f"~{mismatch_v}V rail but the only extracted supply group "
                        f"maxes at {rated_max}V — multi-rail part, this pin's rail "
                        f"spec was not extracted; cannot assert over-voltage"
                    )

            # Rail/group mismatch, UNDER-voltage direction (core-vs-IO). A WARN
            # "actual < supply_min" can mean either (1) a genuine undervoltage or
            # (2) a wrong-rail binding: a dedicated low rail (e.g. RP2040 DVDD on
            # +1V1=1.1V) matched a group whose range actually describes a DIFFERENT
            # sibling rail on the same multi-rail part (the IO supply). To avoid
            # masking a genuine undervoltage we gate on SIBLING-RAIL CONSISTENCY,
            # not a bare ratio: fire only when another supply pin of this part sits
            # on a different net whose voltage falls inside this matched group's
            # range — proving the group belongs to the sibling rail, so this pin's
            # own (lower) rail spec was never extracted. Likely a module/wrong
            # datasheet (Mode-0). Surface as UNRESOLVABLE, not a false WARN.
            if (status == "WARN" and actual_voltage is not None
                    and rated_min is not None and actual_voltage < rated_min):
                hi = rated_max if rated_max is not None else rated_min
                sibling_in_group = any(
                    snet != pin.net and rated_min <= sv <= hi
                    for snet, sv in sibling_rails.items()
                )
                if sibling_in_group:
                    status = "UNRESOLVABLE"
                    confidence = "low"
                    evidence = (
                        f"[rail/group mismatch: core-vs-IO] pin '{pin.pin_name}' on "
                        f"{pin.net} = {actual_voltage}V, but the matched supply group "
                        f"[{rated_min}-{rated_max}]V fits a different sibling rail on "
                        f"this multi-rail part — this pin's dedicated rail spec was not "
                        f"extracted (likely a module/wrong datasheet); cannot assert "
                        f"undervoltage"
                    )

            # Passive-bridge propagation provenance (Step 7b): the voltage was
            # derived across a series passive, not read off a named rail. Grade
            # the verdict by bridge confidence so a lossy bridge can't masquerade
            # as a clean confirmed PASS.
            prov = derived_provenance.get(pin.net) or _norm_prov.get(normalize_net_name(pin.net))
            if prov is not None and status != "UNRESOLVABLE":
                bridge_desc = f"{prov['rail']} via {prov['bridge']} ({prov['bridge_type']})"
                if prov["confidence"] == "high":
                    # 0Ω/ferrite/inductor — voltage-equivalent; trust the verdict.
                    evidence = f"{evidence} — {prov['voltage']}V propagated from {bridge_desc}"
                else:
                    # MEDIUM (diode ~0.3-0.7V drop): not a confident assertion.
                    if status == "PASS":
                        status = "WARN"
                        confidence = "medium"
                        evidence = (f"[assumed: {bridge_desc}, ~0.3-0.7V drop] {evidence} "
                                    f"— treat as approximate")
                    elif status == "FAIL":
                        status = "UNRESOLVABLE"
                        confidence = "low"
                        evidence = (f"[assumed propagation across {bridge_desc} — too weak "
                                    f"to assert damage] {evidence}")

            results.append(SupplyCheckResult(
                refdes=comp.refdes,
                part_number=pn,
                supply_pin_name=pin.pin_name,
                supply_pin_id=pin.pin_id,
                connected_net=pin.net,
                actual_voltage=actual_voltage,
                rated_min=rated_min,
                rated_max=rated_max,
                rated_abs_max=rated_abs_max,
                status=status,
                confidence=confidence,
                evidence_label=evidence,
                explanation=None,
            ))

    return results

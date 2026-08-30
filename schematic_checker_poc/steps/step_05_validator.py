import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class SupplyGroupValidation:
    ok: bool
    reason: str | None    # e.g. "supply_min_equals_supply_max", or None
    evidence: str | None  # human-readable explanation for the UNRESOLVABLE label


def validate_supply_group(group: dict) -> SupplyGroupValidation:
    """
    Rule 1 (supply_min == supply_max): real supply specs are always ranges.
    A point value signals the LLM read a typical/regulator value, not the
    operating min/max pair.

    Documented case: FT232RL — extractor read 3V3OUT row (Typ=3.3V) instead
    of VCCIO row (1.8V–5.25V), producing supply_min=supply_max=3.3, which
    caused downstream false FAILs on a valid 5V connection.
    """
    supply_min = group.get("supply_min")
    supply_max = group.get("supply_max")
    if supply_min is None or supply_max is None:
        return SupplyGroupValidation(ok=True, reason=None, evidence=None)
    try:
        smin, smax = float(supply_min), float(supply_max)
    except (TypeError, ValueError):
        return SupplyGroupValidation(ok=True, reason=None, evidence=None)
    if smin == smax:
        return SupplyGroupValidation(
            ok=False,
            reason="supply_min_equals_supply_max",
            evidence=(
                f"Extracted supply range is a point value ({smin}V = {smax}V), "
                f"not a range. Real supply specs are always ranges; the extractor "
                f"likely read a typical operating voltage, an internal regulator "
                f"output, or a non-supply row instead of the actual supply min/max pair."
            ),
        )
    return SupplyGroupValidation(ok=True, reason=None, evidence=None)


def _to_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


@dataclass
class ValidatedPinSpec:
    part_number: str
    pin_name: str
    pin_id: str
    VIH_max: float | None
    VIH_min: float | None
    absolute_max_voltage: float | None
    pin_type: str | None
    source_snippet: str | None
    signal_score: int
    confidence: str   # "high" | "medium" | "low"
    is_verified: bool
    # Driver-direction provenance (set by the caller after validate()): whether
    # pin_type came from a positive example_pins match (vs a default fallback),
    # and whether the resolved group is documented open-drain. step_08 requires
    # both (exact + not open-drain) before trusting an "output" pin as a push-pull
    # driver that can assert an overvoltage. See step_04b.pin_match_meta.
    pin_type_exact: bool = False
    open_drain: bool = False


def validate(
    part_number: str,
    pin_name: str,
    pin_id: str,
    VIH_max: float | None,
    absolute_max_voltage: float | None,
    pin_type: str | None,
    source_snippet: str | None,
    VIH_min: float | None = None,
) -> ValidatedPinSpec:
    score = 0

    # Either VIH_max or VIH_min contributes — prefer VIH_max, fall back to VIH_min
    vih_val = VIH_max if VIH_max is not None else VIH_min
    if vih_val is not None:
        score += 25
        if isinstance(vih_val, (int, float)) and 0.5 <= vih_val <= 20.0:
            score += 15

    if absolute_max_voltage is not None:
        score += 20
        if isinstance(absolute_max_voltage, (int, float)) and 1.0 <= absolute_max_voltage <= 50.0:
            score += 10

    if source_snippet is not None and len(source_snippet) > 10:
        score += 15
        if any(c.isdigit() for c in source_snippet):
            score += 10

    if pin_type is not None:
        score += 5

    if vih_val is not None and absolute_max_voltage is not None:
        if abs(vih_val - absolute_max_voltage) < 0.01:
            score -= 20
            logger.warning(
                f"[STEP 05] {pin_name}: VIH and absolute_max_voltage are identical "
                f"({vih_val}) — likely extracted from same table, absolute_max may be wrong"
            )

    if score >= 60:
        confidence = "high"
    elif score >= 30:
        confidence = "medium"
    else:
        confidence = "low"

    return ValidatedPinSpec(
        part_number=part_number,
        pin_name=pin_name,
        pin_id=pin_id,
        VIH_max=VIH_max,
        VIH_min=VIH_min,
        absolute_max_voltage=absolute_max_voltage,
        pin_type=pin_type,
        source_snippet=source_snippet,
        signal_score=score,
        confidence=confidence,
        is_verified=(confidence != "low"),
    )


def validate_pin_groups(
    part_number: str,
    pin_groups_result: dict | None,
) -> dict[str, "ValidatedPinSpec"]:
    """
    Validates a whole-part extraction result.
    Returns a dict keyed by group_name → ValidatedPinSpec.
    """
    if not pin_groups_result:
        return {}
    validated: dict[str, ValidatedPinSpec] = {}
    for group in pin_groups_result.get("pin_groups", []):
        group_name = group.get("group_name", "unknown")
        spec = validate(
            part_number=part_number,
            pin_id=group_name,
            pin_name=group.get("applies_to", group_name),
            VIH_max=None,
            VIH_min=_to_float(group.get("VIH_min")),
            absolute_max_voltage=_to_float(group.get("absolute_max_voltage")),
            pin_type=group.get("pin_type"),
            source_snippet=group.get("VIH_source") or "",
        )
        validated[group_name] = spec
    return validated

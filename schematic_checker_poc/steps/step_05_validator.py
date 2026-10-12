import logging
import re
from dataclasses import dataclass, field

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

    Rule 2 (supply_abs_max < supply_max, supply_max > 0; TODO-200, TODO-484 Q3):
    an absolute maximum can never sit below the recommended operating maximum,
    so one of the two was misread. Reachable even when supply_min is None.
    Rule 1 wins when both fire. Negative rails (supply_max <= 0) are skipped so
    they keep reaching step_08b's NEGATIVE_RAIL branch.
    """
    supply_min = group.get("supply_min")
    supply_max = group.get("supply_max")
    if supply_min is not None and supply_max is not None:
        try:
            smin, smax = float(supply_min), float(supply_max)
        except (TypeError, ValueError):
            smin = smax = None
        if smin is not None and smin == smax:
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
    smax = _to_float(supply_max)
    sabs = _to_float(group.get("supply_abs_max"))
    if smax is not None and sabs is not None and smax > 0 and sabs < smax:
        return SupplyGroupValidation(
            ok=False,
            reason="supply_abs_max_below_supply_max",
            evidence=(
                f"Extracted supply absolute max ({sabs}V) is below the "
                f"operating max ({smax}V); an absolute maximum can never sit "
                f"below the recommended operating maximum, so one of the two "
                f"was misread."
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
    # TODO-336: one human-readable note per rail-relative / conditional /
    # non-numeric field that validate() evaluated or left unresolved. Empty for
    # a plain numeric group. step_08 appends these to its evidence labels.
    expr_notes: list[str] = field(default_factory=list)


# ── Rail-relative expressions (TODO-336) ─────────────────────────────────────
# Extraction stores a rail-relative threshold ("0.7 VDD") verbatim in a sibling
# <field>_expr key with the numeric field null; a condition-dependent value goes
# in <field>_alts. validate() evaluates the expression against the receiver
# part's own rail pin voltage. Anything it cannot evaluate stays None (exactly
# the null behaviour downstream) and carries one of these named reasons.
EXPR_UNPARSEABLE = "EXPR_UNPARSEABLE"
RAIL_SYMBOL_NOT_ON_PART = "RAIL_SYMBOL_NOT_ON_PART"
RAIL_VOLTAGE_UNKNOWN = "RAIL_VOLTAGE_UNKNOWN"
RAIL_AMBIGUOUS = "RAIL_AMBIGUOUS"
CONDITIONAL_ALTERNATIVES = "CONDITIONAL_ALTERNATIVES"
NON_NUMERIC_VALUE = "NON_NUMERIC_VALUE"

# A digit-led alphanumeric rail name ("3V3", "1V8": digits, letters, a digit) is ONE
# symbol, tried before num so it never lexes as num+sym; "0.7VDD" (no digit after the
# letters) and "0.7VDD1" (a "." before the digit run) stay num+sym. TODO-509 fork (d).
_EXPR_TOKEN_RE = re.compile(
    r"\s*(?:(?P<sym>(?<![\d.])\d+[A-Za-z]+\d[A-Za-z0-9_]*(?:\+(?!\s*[\d.]))?"
    r"|[A-Za-z][A-Za-z0-9_]*(?:\+(?!\s*[\d.]))?)"
    r"|(?P<num>\d+(?:\.\d*)?|\.\d+)"
    r"|(?P<op>[-+*()]))")


def _expr_tokens(text: str) -> list[tuple[str, str]] | None:
    s = text.strip()
    s = s.replace("×", "*").replace("−", "-").replace("–", "-")
    s = re.sub(r"(?<=[\d.])\s*[Vv](?=\s*(?:\)|$))", "", s)           # trailing unit "V"
    s = re.sub(r"(?<=[\d.)])\s*[xX]\s*(?=[A-Za-z(])", "*", s)          # 0.7xVCC / 0.7 x VCC
    s = re.sub(r"(?<=[A-Za-z0-9_)])\s+[xX]\s+(?=[\d.(])", "*", s)      # VCC x 0.7
    out, pos = [], 0
    while pos < len(s):
        if s[pos:].strip() == "":
            break
        m = _EXPR_TOKEN_RE.match(s, pos)
        if not m or m.end() == pos:
            return None
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        pos = m.end()
    return out


def parse_rail_expr(text, allow_bare: bool = False) -> tuple[float, float, str] | None:
    """Parse a single-rail linear expression into (k, c, symbol), meaning
    value = k * rail + c. Accepts k*SYM, SYM*k, "k SYM", SYM±c, k*(SYM−c)[±c2],
    (SYM)−c, with ×/x/* and an optional trailing "V". Exactly one distinct rail
    symbol, at least one number and a non-zero rail coefficient; anything else
    returns None. allow_bare admits a lone symbol (k=1, c=0) — only for an explicit
    <field>_expr, never a legacy in-field string (the "BVCEO" guard)."""
    if not isinstance(text, str):
        return None
    toks = _expr_tokens(text)
    if not toks:
        return None
    if not allow_bare and not any(kind == "num" for kind, _ in toks):
        return None                                    # a bare symbol ("BVCEO") is not a form
    syms: set[str] = set()
    i = 0

    # linear form: (k, c) with a single shared symbol, or None on a non-linear term
    def expr():
        nonlocal i
        acc = term()
        while acc is not None and i < len(toks) and toks[i] in (("op", "+"), ("op", "-")):
            sign = 1 if toks[i][1] == "+" else -1
            i += 1
            rhs = term()
            if rhs is None:
                return None
            acc = (acc[0] + sign * rhs[0], acc[1] + sign * rhs[1])
        return acc

    def term():
        nonlocal i
        acc = factor()
        while acc is not None and i < len(toks):
            if toks[i] == ("op", "*"):
                i += 1
            elif toks[i][0] in ("num", "sym") or toks[i] == ("op", "("):
                pass                                   # implicit product: "0.7 VDD", "0.7(VCC-1)"
            else:
                break
            rhs = factor()
            if rhs is None or (acc[0] and rhs[0]):
                return None                            # rail × rail is not linear
            acc = (acc[0] * rhs[1] + rhs[0] * acc[1], acc[1] * rhs[1])
        return acc

    def factor():
        nonlocal i
        if i >= len(toks):
            return None
        kind, val = toks[i]
        i += 1
        if kind == "num":
            return (0.0, float(val))
        if kind == "sym":
            syms.add(val)
            return (1.0, 0.0)
        if val == "-":
            f = factor()
            return None if f is None else (-f[0], -f[1])
        if val == "+":
            return factor()
        if val == "(":
            inner = expr()
            if inner is None or i >= len(toks) or toks[i] != ("op", ")"):
                return None
            i += 1
            return inner
        return None

    try:
        form = expr()
    except (IndexError, ValueError):
        return None
    if form is None or i != len(toks) or len(syms) != 1 or form[0] == 0:
        return None
    return form[0], form[1], syms.pop()


def _norm_rail_name(name: str) -> str:
    # ONE leading "+" is a polarity mark ("+VA", "+3V3"), stripped; a trailing "+" is
    # part of the name ("V+") and kept. TODO-509 fork (d).
    return re.sub(r"^\+", "", re.sub(r"[\s_\-]", "", str(name))).upper()


def resolve_rail_voltage(symbol: str, rails, notes: list | None = None
                         ) -> tuple[float | None, str | None]:
    """Voltage of the receiver part's pin(s) whose normalized name equals the
    expression's rail symbol. rails: iterable of (pin_name, voltage|None).
    Returns (voltage, None) or (None, reason). Footnote fallback (TODO-509 fork
    (d)): a symbol that is not on the part and ends in a digit ("VDD1", a footnote
    marker glued to the rail) is retried once with that digit stripped; the
    substitution is appended to `notes` when one is passed."""
    want = _norm_rail_name(symbol)
    matched = [v for name, v in (rails or ()) if _norm_rail_name(name) == want]
    if not matched and want[-1:].isdigit():
        base = want[:-1]
        matched = [v for name, v in (rails or ()) if _norm_rail_name(name) == base]
        if matched and notes is not None:
            notes.append(f"footnote fallback {symbol}→{base}")
    if not matched:
        return None, RAIL_SYMBOL_NOT_ON_PART
    known = {round(float(v), 6) for v in matched if v is not None}
    if not known:
        return None, RAIL_VOLTAGE_UNKNOWN
    if len(known) > 1:
        return None, RAIL_AMBIGUOUS
    return known.pop(), None


def _fmt_v(v: float) -> str:
    return str(round(float(v), 6))


def resolve_field(name: str, raw, expr=None, alts=None, rails=None
                  ) -> tuple[float | None, str | None]:
    """Resolve one signal field to (value, note). A legacy numeric (or numeric
    string) with no _expr/_alts returns (float, None) exactly as _to_float did.
    Precedence: _alts (never evaluated this era) > _expr (the numeric is ignored
    even when non-null) > the numeric field itself; a numeric field holding an
    expression string is evaluated as a legacy in-field expression."""
    if alts:
        n = len(alts) if isinstance(alts, list) else 1
        note = f"{name} unevaluated: {CONDITIONAL_ALTERNATIVES} ({n} row{'s' if n != 1 else ''})"
        if raw is not None:
            note += f"; numeric {raw!r} discarded"
        return None, note
    tags, legacy = [], False
    if isinstance(expr, str) and expr.strip():
        if raw is not None:
            tags.append(f"numeric {raw!r} discarded")
    elif isinstance(raw, str):
        num = _to_float(raw)
        if num is not None:
            return num, None
        if parse_rail_expr(raw) is None:
            return None, f"{name} unevaluated: {NON_NUMERIC_VALUE} ({raw!r})"
        expr, legacy = raw, True
        tags.append("legacy in-field expression")
    else:
        return _to_float(raw), None
    suffix = f" [{'; '.join(tags)}]" if tags else ""
    form = parse_rail_expr(expr, allow_bare=not legacy)
    if form is None:
        return None, f"{name} unevaluated: {EXPR_UNPARSEABLE} ({expr!r}){suffix}"
    k, c, sym = form
    fallback: list[str] = []
    rail_v, reason = resolve_rail_voltage(sym, rails, fallback)
    if reason is not None:
        return None, f"{name} unevaluated: {reason} ({sym}){suffix}"
    if fallback:
        tags.extend(fallback)
        suffix = f" [{'; '.join(tags)}]"
    value = round(k * rail_v + c, 6)
    return value, f"{name} = {expr} @ {sym}={_fmt_v(rail_v)} V → {_fmt_v(value)} V{suffix}"


def validate(
    part_number: str,
    pin_name: str,
    pin_id: str,
    VIH_max: float | None,
    absolute_max_voltage: float | None,
    pin_type: str | None,
    source_snippet: str | None,
    VIH_min: float | None = None,
    *,
    VIH_min_expr: str | None = None,
    VIH_min_alts: list | None = None,
    absolute_max_voltage_expr: str | None = None,
    absolute_max_voltage_alts: list | None = None,
    rails: list[tuple[str, float | None]] | None = None,
) -> ValidatedPinSpec:
    """VIH_min / absolute_max_voltage take the raw extracted value (a float, a
    string, or None); step_05 owns the string→number decision (TODO-336). The
    keyword-only _expr/_alts/rails inputs default to absent, so a caller passing
    plain floats gets exactly the pre-336 result with empty expr_notes."""
    expr_notes: list[str] = []
    VIH_min, note = resolve_field("VIH_min", VIH_min, VIH_min_expr, VIH_min_alts, rails)
    if note:
        expr_notes.append(note)
    absolute_max_voltage, note = resolve_field(
        "absolute_max_voltage", absolute_max_voltage,
        absolute_max_voltage_expr, absolute_max_voltage_alts, rails)
    if note:
        expr_notes.append(note)

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
        expr_notes=expr_notes,
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

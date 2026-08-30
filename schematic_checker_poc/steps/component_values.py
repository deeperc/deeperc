"""Shared component-value parsing primitives.

Home for format-robust value-string → SI-unit parsers reused across value-range
checkers (step_08e M4-pullup resistor values today; future M11 crystal-cap
values, etc.). Deliberately step-independent so it is not owned by any one
checker.

The corpus (recon investigation/m4_pullup_value_recon.md, Step 2) carries
resistor value strings in wildly heterogeneous formats — plain (`10K`, `5.1k`),
RKM/IEC-60062 (`2k2`, `4k7`, `10kR`), European comma-decimal (`4,7K`),
symbol-name-embedded (`R_2k2_0402` — the single most common form), and
value/footprint (`2.2k/R0402`). A bare ``float()`` fails on every one of them.

`resistance_ohms` returns a normalized ohm float, or ``None`` when the string is
genuinely unparseable — callers MUST route ``None`` to UNRESOLVABLE, never a
silent pass (CLAUDE.md failure-handling rule).
"""

import re

# Resistance multiplier suffixes. R = ×1 (also the RKM decimal marker), k/K =
# kilo, M = mega, G = giga. Case-insensitive.
_MULT = {"R": 1.0, "K": 1e3, "M": 1e6, "G": 1e9}

# RKM infix: digit(s), a multiplier letter as the decimal point, digit(s).
# e.g. 2K2 -> 2.2k, 4K7 -> 4.7k, 1K5 -> 1.5k, 4R7 -> 4.7, 2M2 -> 2.2M
_RKM_INFIX_RE = re.compile(r"^(\d+)([RKMG])(\d+)$")

# Standard: number (optional decimal) + optional multiplier + optional trailing
# 'R' noise. e.g. 10K, 5.1K, 2K, 100, 470R, 10KR (K then trailing R), 0.5
_STD_RE = re.compile(r"^(\d*\.?\d+)\s*([RKMG]?)R?$")

# Leading-R RKM (sub-ohm): R47 -> 0.47, R100 -> 0.1
_LEADING_R_RE = re.compile(r"^R(\d+)$")

# A footprint code alone (0402/0603/0805/1206/2512 …) is a pure 4-digit token —
# never a resistance value. Used to skip footprint tokens inside symbol names.
_FOOTPRINT_TOKEN_RE = re.compile(r"^\d{4}$")


def _parse_core(su: str):
    """Parse an already-normalized (uppercased, comma→dot, no footprint) token."""
    m = _RKM_INFIX_RE.match(su)
    if m:
        whole, mult, frac = m.groups()
        return float(f"{whole}.{frac}") * _MULT[mult]
    m = _STD_RE.match(su)
    if m:
        num, mult = m.groups()
        val = float(num)
        return val * (_MULT[mult] if mult else 1.0)
    m = _LEADING_R_RE.match(su)
    if m:
        return float(f"0.{m.group(1)}")
    return None


def resistance_ohms(value_str) -> float | None:
    """Normalize a resistor value string to ohms, or ``None`` if unparseable.

    Handles plain (`10K`, `5.1k`, `2k`), RKM (`2k2`, `4k7`, `10kR`, `4R7`),
    European comma-decimal (`4,7K`), value/footprint (`2.2k/R0402`), and
    symbol-name-embedded (`R_2k2_0402`, `R_200k_0402`) forms. Returns ``None``
    for empty / non-value strings (e.g. ``DNP``) — callers route ``None`` to
    UNRESOLVABLE.
    """
    if value_str is None:
        return None
    s = str(value_str).strip()
    if not s:
        return None

    # value/footprint: keep the part before the first '/'  (2.2k/R0402 -> 2.2k)
    s = s.split("/")[0].strip()

    # symbol-name-embedded: R_2k2_0402 / R_200k_0402 → pick the value-bearing
    # underscore token (skip a leading bare 'R' symbol prefix and footprint codes).
    if "_" in s:
        parts = [p for p in s.split("_") if p]
        if parts and parts[0].upper() == "R":
            parts = parts[1:]
        cand = None
        for p in parts:
            if _FOOTPRINT_TOKEN_RE.match(p):
                continue
            if re.search(r"\d", p) and re.fullmatch(r"(?i)[\d.,rkmg]+", p):
                cand = p
                break
        if cand is not None:
            s = cand

    # European comma decimal → dot  (4,7K -> 4.7K)
    s = s.replace(",", ".").strip()

    return _parse_core(s.upper())

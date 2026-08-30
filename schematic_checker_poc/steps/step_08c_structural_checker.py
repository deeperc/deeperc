"""
Step 08c — Structural Integrity Checker (V2 Phase 1.1.a)

Parallel check category alongside Step 8 (signal voltage) and Step 8b
(supply voltage). Detects components with power or ground pins that
are not properly connected to power rails or ground nets.

Phase 1.1.a is purely deterministic — no datasheet KB used. Pin name
patterns for VCC/VDD/GND are vendor-independent and standardized.

Two checks are performed per component:
  1. Power pin connectivity — every pin named VCC/VDD/AVDD/etc. must
     be on a net in confirmed_voltages.
  2. Ground pin connectivity — every pin named GND/VSS/AGND/etc. must
     be on a net in ground_nets.
"""
import logging
import re
from dataclasses import dataclass, field
from steps.net_name_utils import normalize_net_name

logger = logging.getLogger(__name__)


# ── Defensive tripwire (F5 / TODO-172) ────────────────────────────────────────
# The prefix-only power-pin branch (`elif is_power and power_recog == "prefix":
# continue`, below) emits NO finding by design: a decorated power pin that IS
# connected (multi-pin net) but on a net step_06 neither confirmed nor recognized
# as a rail. It was suppressed at 268c938 to avoid a +54-WARN false-positive wave.
#
# The F5 recon established this branch is NOT reached by any bad-corpus mutant
# (M7 floats a power pin onto a *single*-pin orphan net → the single-pin FAIL
# branch above, never here) and is near-zero on the good corpus (decorated power
# pins are near-universally rail-connected → PASS at branch 2 / 2.5). It is,
# however, reachable BY CONSTRUCTION (see
# test_prefix_only_pin_multipin_unconfirmed_net_suppressed) — not dead code — so
# rather than delete it we INSTRUMENT it: if a real board ever reaches it, the
# counter + a one-shot WARNING announce it. Reaching it means the is_power /
# is_ground pin-name bucketing has a gap worth investigating. Finding behavior is
# UNCHANGED — this adds observability only, never a FAIL/WARN/UNRESOLVABLE.
_UNCLAIMED_POWER_PIN_HITS = 0
_unclaimed_power_pin_logged = False


def reset_unclaimed_power_pin_tripwire() -> None:
    """Reset the tripwire counter + one-shot-log flag (tests / per-process reuse)."""
    global _UNCLAIMED_POWER_PIN_HITS, _unclaimed_power_pin_logged
    _UNCLAIMED_POWER_PIN_HITS = 0
    _unclaimed_power_pin_logged = False


def unclaimed_power_pin_hits() -> int:
    """Pins that reached the believed-unreachable prefix-only branch since the last
    reset. Expected 0 on every corpus run to date — nonzero is the tripwire."""
    return _UNCLAIMED_POWER_PIN_HITS


def _record_unclaimed_power_pin(refdes: str, pin_name: str, net_name: str,
                                pin_count: int) -> None:
    """Tripwire hook for the prefix-only defensive branch: count the hit and log
    ONCE per process (throttled so a pathological board can't spam). Emits no
    finding — observability only."""
    global _UNCLAIMED_POWER_PIN_HITS, _unclaimed_power_pin_logged
    _UNCLAIMED_POWER_PIN_HITS += 1
    if not _unclaimed_power_pin_logged:
        _unclaimed_power_pin_logged = True
        logger.warning(
            "[step_08c] prefix-only power pin %s on %s reached the believed-"
            "unreachable defensive branch (net %r, %d pins) — no finding emitted "
            "(F5/TODO-172). Reaching this path means the power-pin bucketing has a "
            "gap to investigate.",
            pin_name, refdes, net_name, pin_count,
        )


# Pin name patterns — standardized across all IC vendors

POWER_PIN_NAMES = {
    "VCC", "VDD", "VCCA", "VCCB", "VCCO", "VCCIO", "VCCINT",
    "AVCC", "AVDD", "DVCC", "DVDD", "PVCC", "PVDD",
    "IOVCC", "IOVDD", "COREVDD",
    "VBAT", "VIN", "V+", "VCC1", "VCC2",
    "VDDA", "VDDD", "VDDIO",
}

GROUND_PIN_NAMES = {
    "GND", "VSS", "AGND", "DGND", "PGND", "SGND", "CGND", "FGND",
    "AVSS", "DVSS", "VSSA", "VSSD",
}


# Net names that explicitly indicate "this pin is unconnected" by KiCad convention.
# Note: Net-(...) and N$... are NOT here — they are generic auto-names that may
# have any number of pins. Floating detection for those relies on net_pin_counts.
SYNTHETIC_UNCONNECTED_RE = re.compile(
    r"^(?:"
    r"unconnected[-_(]"      # KiCad: unconnected-(U1-Pad14)
    r"|.*#FLG\d*"            # KiCad power flag net (also checked by _is_power_flag_net)
    r"|.*#PWR\d*"            # KiCad implicit power net
    r"|.*PWR_FLAG"           # KiCad PWR_FLAG symbol net
    r")",
    re.IGNORECASE,
)


# Prefix pattern for decorated power-pin names (VDD2A, VDD_IO, VCC+, VCCPLL,
# VCCO_45, VDD33, …) that exact-set membership misses. Kept intentionally in sync
# with generate_bad_corpus.PWRPIN_RE — that is the canonical "what counts as a
# power pin" definition; generator and checker previously disagreed, and this
# aligns them. Pins matched ONLY by this prefix (not in POWER_PIN_NAMES) are
# treated conservatively at the finding site (FAIL-only; see the guard below).
_PWRPIN_PREFIX_RE = re.compile(r'(?i)^(vdd|vcc|avdd|dvdd|vddio|vdda|vbat|vin|vsys|v\+)')


def _power_pin_recognition(pin_name: str) -> str | None:
    """Return 'exact' (in POWER_PIN_NAMES), 'prefix' (decorated, prefix-only), or None."""
    if not pin_name:
        return None
    norm = pin_name.upper().strip()
    if norm in POWER_PIN_NAMES:
        return "exact"
    if _PWRPIN_PREFIX_RE.match(norm):
        return "prefix"
    return None


def _is_power_pin_name(pin_name: str) -> bool:
    return _power_pin_recognition(pin_name) is not None


# Rail-NAME recognizer (option ii — step_06 rail confirmation, recon
# recon_step06_rail_confirmation_2026-06-08.md §E/§G). Recognizes a *net name* as
# a power rail without asserting any voltage, so a recognized power pin on a
# decorated/vendor rail name (e.g. /DVDD, /nRF52_VDD) that step_06 left
# unconfirmed still PASSes branch 2.5 — recovering the 2 seed-masked M7 misses.
# Applied to normalize_net_name() output (leading / + - already stripped).
# Recognition is per path/whitespace SEGMENT: a power-token / voltage-token
# PREFIX, or a vendor-prefixed _VDD/_VCC/_PWR/_VBUS/_VSW SUFFIX. normalize_net_name
# strips only LEADING /+-, so a rail token buried after an embedded hierarchical
# segment ("/CAN bus/CAN12V_prot", "/pic_sockets/VCC_PIC", "/Buck Vin") would
# never reach the ^-anchored match; splitting on '/' and whitespace and testing
# each segment reduces those to a matchable basename/word.
#
# TODO-133 name-form widen (rail_name_recognizer_recon.md PART 4, Path A). This
# recognizer is step_08c-LOCAL — NOT consumed by step_06/08/08b/08d — and branch
# 2.5 is PASS-only-before-FAIL/WARN, so a generous match can only flip a
# structural WARN→PASS, never suppress a signal check (architecturally; step_08's
# net-skip never reads it). General FORMS, not the literal recon strings.
# CONSERVATIVE bound so EN/IN/Spare/+12C/Net-(…) stay WARN: a segment qualifies
# only if it carries an explicit power keyword (incl. VSW/PWR), a \d+V voltage
# pattern (optionally domain-prefixed: CAN12V, 5VREG, P3V3, +3V3_AUX), or a
# _VDD/_VCC/_PWR/_VBUS/_VSW suffix.
# NOTE: this recognizer (C) gates M7 floating-power PASS branch 2.5 (≥2-pin,
# PASS-only). step_08d._is_power_rail_net / _POWER_RAIL_RE (D) answers a RELATED
# but NARROWER question — "is this a pull-up TARGET?" — on a DIFFERENT surface:
# the pull-up high-side, feeding M3 NO_PULLUP presence + M4 value (step_08e).
# NOT M6/M12, which are pin-role↔net coherence and touch no rail recognizer.
# Fork-6 (TODO-165) gave D C's normalize+split+`_VCC/_VDD$` suffix so it too
# recognizes hierarchical rails (e.g. `/CSIA_I2C_VCC`), but a NARROWED token set
# — NOT C's `_PWR$`/`_VSW$` suffix, VIN/VOUT/VSW/VREF — because on the pull-up
# surface those over-match (a 0Ω link to a `*_PWR` domain read as a pull-up: the
# Cycle-A STOP). C was left byte-unchanged (M7's single-pin-float immunity rests
# on C's exact form). The two remain source-independent — do NOT merge or import.
_RAIL_TOKEN_RE = re.compile(
    r'^(?:'
    r'VCC|VDD|AVCC|AVDD|DVCC|DVDD|PVCC|PVDD|IOVCC|IOVDD|COREVDD|'
    r'VDDA|VDDD|VDDIO|VBAT|VBUS|VSYS|VREF|VIN|VOUT|VSW|PWR'
    r')(?:$|[_/A-Z0-9])'
    r'|^[A-Z]{0,6}\d+V\d*(?:$|[_/A-Z0-9])'
    r'|.*_(?:VDD|VCC|VDDA|VDDD|VDDIO|AVDD|DVDD|PVDD|PWR|VBUS|VSW)\d*$',
    re.IGNORECASE,
)
# Back-compat alias (no external caller, but kept to avoid import surprises).
_RAIL_NAME_RE = _RAIL_TOKEN_RE


def _is_rail_name(net_name: str) -> bool:
    """True when a net NAME looks like a power rail — a power keyword, a \\d+V
    voltage token (optionally domain-prefixed), or a _VDD/_VCC/_PWR/_VBUS/_VSW
    suffix — in ANY '/'-or-whitespace-delimited segment. Voltage-agnostic:
    recognition only, never a voltage assertion."""
    if not net_name:
        return False
    norm = normalize_net_name(net_name)
    # Split on path '/' + whitespace; strip a per-segment leading polarity marker
    # (normalize_net_name only strips the LEADING +/- of the whole string, but an
    # embedded segment can carry its own, e.g. ".../+3V3_AUX").
    return any(
        _RAIL_TOKEN_RE.match(seg.lstrip("+-"))
        for seg in re.split(r"[/\s]+", norm)
        if seg.lstrip("+-")
    )


def _is_ground_pin_name(pin_name: str) -> bool:
    if not pin_name:
        return False
    return pin_name.upper().strip() in GROUND_PIN_NAMES


_POWER_FLAG_NET_RE = re.compile(r"#FLG\d*|#PWR\d*|PWR_FLAG", re.IGNORECASE)


def _is_synthetic_unconnected_net(net_name: str) -> bool:
    if not net_name:
        return True
    return bool(SYNTHETIC_UNCONNECTED_RE.match(net_name))


def _is_power_flag_net(net_name: str) -> bool:
    """True if the net connects through a KiCad power flag pseudo-component."""
    return bool(_POWER_FLAG_NET_RE.search(net_name))


@dataclass
class StructuralCheckResult:
    refdes: str
    part_number: str
    pin_id: str
    pin_name: str
    pin_kind: str            # "power" | "ground"
    connected_net: str
    expected_kind: str       # "power_rail" | "ground_net"
    status: str              # "PASS" | "FAIL" | "WARN"
    confidence: str          # always "high" — deterministic check
    evidence_label: str
    explanation: str | None = None
    finding_code: str | None = None  # e.g. "NC_POWER_PIN_INTENTIONAL" (branch 4 NC gate)


def check_structural_integrity(
    components,
    confirmed_voltages: dict,
    ground_nets: list,
    nets,
    pintypes: dict | None = None,
) -> list[StructuralCheckResult]:
    # (ref, pin_id) -> raw KiCad pintype string (e.g. "power_in+no_connect"),
    # from step_02_parser._extract_pintypes / ir.pintypes. Optional/keyword so
    # every pre-existing caller (all of tests/test_structural_checker.py,
    # tests/test_rail_map.py) is unaffected — an omitted/None pintypes behaves
    # exactly as before (branch 4 gate below finds no entry -> FAIL, unchanged).
    pintypes = pintypes or {}
    # Build net → pin count map to distinguish single-pin from multi-pin nets
    net_pin_counts = {net.name: len(net.pins) for net in nets}

    normalized_power = {normalize_net_name(n) for n in confirmed_voltages.keys()}
    normalized_ground = {normalize_net_name(n) for n in ground_nets}

    results = []

    for comp in components:
        # Skip KiCad pseudo-components (power flags, hierarchical labels, etc.)
        if comp.refdes.startswith("#"):
            continue

        for pin in comp.pins:
            power_recog = _power_pin_recognition(pin.pin_name)  # 'exact' | 'prefix' | None
            is_power = power_recog is not None
            is_ground = _is_ground_pin_name(pin.pin_name)

            if not is_power and not is_ground:
                continue

            net_name = pin.net or ""
            net_norm = normalize_net_name(net_name)

            kind = "power" if is_power else "ground"
            expected = "power_rail" if is_power else "ground_net"
            pin_count_on_net = net_pin_counts.get(net_name, 0)
            finding_code = None

            # ── PASS conditions (checked first, in priority order) ──────────

            # 1. Power flag pseudo-component nets — KiCad ERC mechanism.
            if _is_power_flag_net(net_name):
                status = "PASS"
                evidence = (
                    f"{kind.capitalize()} pin {pin.pin_name} on {comp.refdes} "
                    f"connects through a KiCad power flag on net '{net_name}' "
                    f"— connection assumed valid"
                )

            # 2. Recognized power rail — PASS regardless of pin count.
            #    Per-sheet hierarchical exports may show a global rail as
            #    single-pin on a sub-sheet; confirmed_voltages membership
            #    is proof of a real connection.
            elif is_power and net_norm in normalized_power:
                voltage = confirmed_voltages.get(net_name) or next(
                    (v for n, v in confirmed_voltages.items()
                     if normalize_net_name(n) == net_norm),
                    None,
                )
                voltage_str = f"({voltage}V)" if voltage is not None else "(voltage unconfirmed)"
                status = "PASS"
                evidence = (
                    f"Power pin {pin.pin_name} on {comp.refdes} connected to "
                    f"power rail {net_name} {voltage_str}"
                )

            # 2.5 Recognized power-rail NAME (decorated/vendor) on a multi-pin net
            #     → PASS. Voltage is NOT asserted (trap-proof): step_06 /
            #     confirmed_voltages untouched. The pin_count >= 2 gate is
            #     mandatory — it preserves the single-pin floating FAIL below so a
            #     rail-named *single-pin* net still FAILs (real-float detection).
            #     PASS-only-before-FAIL/WARN: this branch can only convert WARN/
            #     FAIL → PASS, never create a finding (precision-safe by
            #     construction). See recon_step06_rail_confirmation_2026-06-08.md.
            elif is_power and pin_count_on_net >= 2 and _is_rail_name(net_name):
                status = "PASS"
                evidence = (
                    f"Power pin {pin.pin_name} on {comp.refdes} connected to "
                    f"power rail {net_name} (recognized rail name, voltage "
                    f"unconfirmed)"
                )

            # 3. Recognized ground net — PASS regardless of pin count.
            elif is_ground and net_norm in normalized_ground:
                status = "PASS"
                evidence = (
                    f"Ground pin {pin.pin_name} on {comp.refdes} connected to "
                    f"ground net {net_name}"
                )

            # ── FAIL conditions ──────────────────────────────────────────────

            # 4. Empty net or KiCad explicit unconnected marker.
            #
            #    NC gate (TODO-304/FP-08C-NOCONN, recon
            #    investigation/recon_reports/step08c_noconnect_recon.md): a pin
            #    can land here not because it's an accidental wiring defect but
            #    because KiCad's own no_connect flag says the designer meant it
            #    (e.g. a multi-pad power ball a datasheet says to leave floating
            #    when unused — kria U30 VDDA1P8; or a connector pad genuinely
            #    unused — interf_u BUS1 GND). ir.pintypes carries that flag
            #    (a compound "power_in+no_connect"/"passive+no_connect" string)
            #    but was never consulted here until now. Demote to WARN ONLY
            #    when the exact "no_connect" token is present — absent, or the
            #    (ref, pin_id) missing from pintypes entirely, falls through to
            #    the unchanged FAIL below (recon P3/P4: this never fires on any
            #    frozen M7 mutant — those pins carry a bare pintype with no NC
            #    suffix, real floats stay FAIL).
            elif not net_name or SYNTHETIC_UNCONNECTED_RE.match(net_name):
                pintype = pintypes.get((comp.refdes, pin.pin_id), "")
                is_designer_nc = "no_connect" in pintype.split("+")
                if is_designer_nc:
                    status = "WARN"
                    finding_code = "NC_POWER_PIN_INTENTIONAL"
                    evidence = (
                        f"{kind.capitalize()} pin {pin.pin_name} on {comp.refdes} "
                        f"is designer-asserted no-connect on floating {kind} pin; "
                        f"verify datasheet permits leaving this pin unconnected "
                        f"(net '{net_name}', pintype '{pintype}')"
                    )
                else:
                    status = "FAIL"
                    evidence = (
                        f"{kind.capitalize()} pin {pin.pin_name} on {comp.refdes} "
                        f"is on a KiCad-marked unconnected net '{net_name}' — "
                        f"pin is truly floating"
                    )

            # 5. Single-pin net not recognized as a rail — effectively floating.
            elif pin_count_on_net <= 1:
                status = "FAIL"
                evidence = (
                    f"{kind.capitalize()} pin {pin.pin_name} on {comp.refdes} "
                    f"is on a single-pin net '{net_name}' — effectively floating"
                )

            # ── WARN ─────────────────────────────────────────────────────────

            # 6. Multi-pin net not recognized as power or ground — needs review.
            #
            # GUARD (precision): a prefix-only (decorated) power pin — e.g. VDD2A,
            # VCCO_45 — recognized by _PWRPIN_PREFIX_RE but NOT in POWER_PIN_NAMES
            # is held to the FAIL path only. Here it is on a MULTI-pin net (so it
            # is connected, not floating); we emit NO finding. This zeroes the
            # predicted +54-WARN good-corpus exposure from broadening recognition.
            # KNOWN LIMITATION: a decorated power pin mis-wired onto a step_06-
            # unconfirmed multi-pin rail will NOT WARN — this suppression does
            # double duty as both a false-positive guard and a potential
            # true-positive suppressor. Revisit letting prefix-only pins rejoin the
            # WARN branch if/when step_06 rail confirmation tightens. Exact-set
            # pins are unaffected and WARN exactly as before (precision invariant).
            elif is_power and power_recog == "prefix":
                # Defensive tripwire (F5/TODO-172): announce if ever reached. The
                # finding behavior — emit nothing — is UNCHANGED. See module note.
                _record_unclaimed_power_pin(comp.refdes, pin.pin_name, net_name,
                                            pin_count_on_net)
                continue

            else:
                status = "WARN"
                evidence = (
                    f"{kind.capitalize()} pin {pin.pin_name} on {comp.refdes} "
                    f"is on net '{net_name}' which is not classified as a "
                    f"{expected.replace('_', ' ')}. Net has {pin_count_on_net} pins"
                    f" — may be a custom rail with a non-standard name, or a "
                    f"wiring error."
                )

            results.append(StructuralCheckResult(
                refdes=comp.refdes,
                part_number=comp.part_number,
                pin_id=pin.pin_id,
                pin_name=pin.pin_name,
                pin_kind=kind,
                connected_net=net_name,
                expected_kind=expected,
                status=status,
                confidence="high",
                evidence_label=evidence,
                explanation=None,
                finding_code=finding_code,
            ))

    return results

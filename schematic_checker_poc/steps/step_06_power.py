"""
Step 06 — Power Net Inference

Fully deterministic (Fix γ): power/ground nets are classified by name via the
Tier-1 regex layer plus the Tier-1 augmentation rules (classify_power_augmented).
The former Tier-2 Gemma fallback was removed per the LLM-decision survey
(corpus_results/step_06_llm_decision_survey.md §5) — it contributed zero
actionable findings and substantial noise. High-fanout nets the name rules can't
classify stay unclassified and resolve to UNRESOLVABLE downstream.
"""
import atexit
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from trace.writer import write_trace
from steps import rail_map as rail_map_mod

# ── Survey trace logger (STEP_06_DEBUG) ───────────────────────────────────────
# Permanent, flag-gated observability hook for surveying Gemma's actual
# contribution to Step 06 classification. This is NOT a remove-me-later TODO —
# it stays so the "does Step 06 still need Gemma?" question can be re-surveyed as
# the pipeline evolves (see corpus_results/step_06_llm_decision_survey.md).
#
# Off by default: with STEP_06_DEBUG unset, _survey_enabled() is False and no
# records are built or written — zero cost on normal runs. Set STEP_06_DEBUG=1
# to emit one compact JSON line per net to logs/step_06_trace_<UTC-ts>.jsonl.
# Distinct from trace/writer.py, which is a single-net deep-dive tool; this is a
# corpus-wide, every-net path-distribution logger. Decision-path names are kept
# aligned with that module's vocabulary where they overlap.
_SURVEY_FH = None  # cached append-mode file handle, opened lazily on first emit


def _survey_enabled() -> bool:
    """True when STEP_06_DEBUG=1. Read at call time so tests can toggle it."""
    return os.environ.get("STEP_06_DEBUG") == "1"


def _survey_emit(records: list[dict]) -> None:
    """Append one JSON line per survey record to logs/step_06_trace_<ts>.jsonl.
    Lazily opens (and atexit-closes) a single run-scoped file. No-op if the
    flag is off or there are no records."""
    global _SURVEY_FH
    if not records or not _survey_enabled():
        return
    if _SURVEY_FH is None:
        log_dir = Path(__file__).resolve().parents[2] / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        _SURVEY_FH = open(log_dir / f"step_06_trace_{ts}.jsonl", "a", encoding="utf-8")
        atexit.register(_SURVEY_FH.close)
    for rec in records:
        _SURVEY_FH.write(json.dumps(rec, ensure_ascii=False) + "\n")
    _SURVEY_FH.flush()

# ── Tier 1: deterministic patterns ────────────────────────────────────────────

POWER_NET_RE = re.compile(
    r'^(?:'
    r'VCC\w*|VDD\w*|AVDD\w*|DVDD\w*|PVDD\w*|'   # VCC, VDD variants
    r'VSS\w*|GND\w*|AGND\w*|DGND\w*|PGND\w*|'   # GND variants
    r'PWR\w*|POWER\w*|SUPPLY\w*|'                 # generic power names
    r'V\d+V\d*|P\d+V\d*|'                         # V3V3, P3V3, V1V8, P5V0
    r'\+\d+V\d*|\-\d+V\d*|'                       # +5V, +3V3, -12V
    r'\+\d+\.\d+V\d*|\-\d+\.\d+V\d*|'             # +3.3V, +5.0V, -12.5V
    r'.*_3V3$|.*_5V0?$|.*_1V8$|.*_1V2$|'         # suffix patterns: VCC_3V3
    r'.*_12V$|.*_24V$|.*_48V$|'                   # higher voltage suffixes
    r'VREF\w*|VBAT\w*|VBUS\w*|VCAP\w*|'          # special supply names
    r'VIN\w*|VOUT\w*|VREG\w*|'                    # regulator nets
    r'EP|PAD|EPAD'                                 # thermal/exposed pads
    r')$',
    re.IGNORECASE
)

GROUND_NET_RE = re.compile(
    r'^/?(?:'
    r'GND\w*|VSS\w*|AGND\w*|DGND\w*|PGND\w*|'
    r'SGND\w*|CGND\w*|FGND\w*|'                  # signal/chassis/frame GND
    r'EARTH|SHIELD|'
    r'.*_GND$|.*_VSS$'
    r')$',
    re.IGNORECASE
)

# Voltage inference from net name — returns float or None
VOLTAGE_FROM_NAME_RE = [
    (re.compile(r'3V3|3\.3V', re.IGNORECASE), 3.3),
    (re.compile(r'5V0|5\.0V|(?<!\d)5V(?!\d)', re.IGNORECASE), 5.0),
    (re.compile(r'1V8|1\.8V', re.IGNORECASE), 1.8),
    (re.compile(r'1V2|1\.2V', re.IGNORECASE), 1.2),
    (re.compile(r'2V5|2\.5V', re.IGNORECASE), 2.5),
    (re.compile(r'12V|12\.0V', re.IGNORECASE), 12.0),
    (re.compile(r'24V|24\.0V', re.IGNORECASE), 24.0),
    (re.compile(r'48V|48\.0V', re.IGNORECASE), 48.0),
    (re.compile(r'(?<!\d)9V(?!\d)', re.IGNORECASE), 9.0),
    (re.compile(r'(?<!\d)15V(?!\d)', re.IGNORECASE), 15.0),
    # USB bus voltage — labeled convention (nominal 5V). Explicit, not a silent
    # default; USB-C PD rails can exceed this (flagged in the net-voltage recon).
    (re.compile(r'VBUS', re.IGNORECASE), 5.0),
]

# Generic decimal-rail parser: "<int>V<frac>" uses 'V' as the decimal point, the
# standard EE/KiCad convention (3V3=3.3, 1V1=1.1, 2V8=2.8, 1V35=1.35). Anchored
# with digit boundaries so it never fires mid-number; it is consulted ONLY after
# the explicit table misses, and infer_voltage_from_name is only ever called on
# nets already classified as power rails, so signal-name collisions don't reach
# it. This is what resolves the +1V1 RP2040 core rail (the dominant Tier-1 gap).
_DECIMAL_RAIL_RE = re.compile(r'(?<!\d)(\d{1,2})V(\d{1,2})(?!\d)', re.IGNORECASE)


def infer_voltage_from_name(net_name: str) -> float | None:
    for pattern, voltage in VOLTAGE_FROM_NAME_RE:
        if pattern.search(net_name):
            return voltage
    m = _DECIMAL_RAIL_RE.search(net_name)
    if m:
        return float(f"{int(m.group(1))}.{m.group(2)}")
    return None


def is_power_net_deterministic(net_name: str) -> bool:
    return bool(POWER_NET_RE.match(net_name))


def is_ground_net_deterministic(net_name: str) -> bool:
    return bool(GROUND_NET_RE.match(net_name))


# ── Definitely-signal filter (never sent to Gemma) ────────────────────────────
# Net-name shapes that are signals / strapping pins / KiCad-synthetic by
# construction and can never be a power or ground rail. Tier 1 runs first, so
# standard rails (P3V3, +5V, PWR*, VIN* …) are already classified before this
# filter is consulted; it only ever sees nets Tier 1 left unclassified.
DEFINITE_SIGNAL_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in (
        r'^/?(?:io|gpio|pin|port)[a-z]*\d+',   # /IO0 /GPIO12 /PORTB3 (+ compound /GPIO0/XTAL1/…)
        r'^/?p[a-z]\d+',                       # /PA0 /PB3 — true GPIO ports (narrowed bare-p)
        r'^/?net-\([^)]+\)',                   # Net-(C6-Pad1) — KiCad synthetic
        r'^/?net_[a-z0-9]+_pad\d+',            # NET_C1_PAD2 — KiCad synthetic
        r'^/?unconnected-\(',                  # unconnected-(#FLG06-…)
        r'^/?[a-z0-9_]*_(?:en|enable|rst|reset|nrst|boot|wake|int|irq|cs|csn|ce)$',
        r'^/?(?:en|rst|reset|nrst|boot|wake|int|irq|cs|csn|ce)$',
        r'^/?[a-z0-9_]*_(?:sda|scl|mosi|miso|sck|sclk|tx|rx|txd|rxd|dp|dm|d\+|d\-)$',
        r'^/?(?:sda|scl|mosi|miso|sck|sclk|tx|rx|txd|rxd|dp|dm|d\+|d\-)$',
        r'^/?[a-z0-9_]*_(?:clk|clkin|clkout|xtal\d?|osc\d?|xi|xo)$',
        r'^/?(?:clk|clkin|clkout|xtal\d?|osc\d?|xi|xo)$',
    )
]


# ── Option B: rail-token rescue for KiCad-synthetic nets ──────────────────────
# A KiCad-synthetic net (Net-(…) / NET_…_PAD) can still be a real power rail
# when the rail is isolated behind a series passive (fuse / ferrite / sense
# resistor) so KiCad never gave it a friendly name — yet the auto-name embeds a
# rail-like token (VBAT, VIN, VDD, BAT, 5.0V, 3V3 …). Naming alone cannot decide
# these, so they are handed to Gemma instead of being hard-filtered. Scope is
# deliberately limited to the synthetic shapes: explicit signal-suffix names
# such as /3V3_EN keep their rail token but stay filtered (they are not
# synthetic), and unconnected-(…) flags are likewise unaffected.
_SYNTHETIC_NET_RE = re.compile(
    r'^/?(?:net-\([^)]+\)|net_[a-z0-9]+_pad\d+)', re.IGNORECASE)

_RAIL_TOKEN_RE = re.compile(
    r'(?<![a-z])(?:'
    r'v(?:bat|in|dd[a]?|cc[a]?|sys|bus|out|aa|ee|pp)'  # VBAT VIN VDD VDDA VCC …
    r'|bat(?:t|tery)?'                                  # BAT BATT BATTERY
    r'|pwr|power'                                       # PWR POWER
    r'|\d+v\d+'                                         # 3V3 5V0 1V8
    r'|\d+\.\d+\s*v'                                    # 5.0V 3.3V
    r'|\d+\s*v'                                         # 12V 5V
    r'|[+-]\d+(?:v\d*)?'                                # +5 +3V3 -12
    r')(?![a-z])',
    re.IGNORECASE,
)

# ── Voltage-prefixed rail names (never signals, regardless of suffix) ──────────
# A name that *starts* with a voltage-rail prefix (P3V3_CLK, +5V_CLK, VDD_OSC …)
# is a real rail even when its suffix carries a signal token. Without this,
# is_definitely_signal() would filter P3V3_CLK as a clock signal and never hand
# it to Gemma — the corpus_run_v2_10 regression on vme-wren clocks.net.
# Deliberately excludes the bare \d+V_ form: /3V3_EN, /5V_RST are genuine
# strapping signals and must stay filtered (locked Option-B audit decision +
# test_rail_token_rescue_is_scoped). Only the P-convention, signed, and
# named-rail prefixes are exempted.
_VOLTAGE_PREFIX_RE = re.compile(
    r'^/?(?:'
    r'P\d+V\d*'                                         # P3V3 P5V0 P12V
    r'|V\d+V\d*'                                         # V3V3 V1V8
    r'|\+\d+V\d*|\-\d+V\d*'                               # +5V +3V3 -12V
    r'|A?VDD|DVDD|PVDD|VCC|VSS|VBAT|VIN|VBUS|VSYS|VOUT|VEE|VAA|VPP'
    r')(?:[_/]|$)',
    re.IGNORECASE,
)


def is_definitely_signal(net_name: str) -> bool:
    """True for net-name shapes that are signals/synthetic by construction and
    must never reach the Tier-2 Gemma classifier."""
    if not net_name:
        return False
    # Option B: synthetic net carrying a rail-like token → let Gemma adjudicate.
    if _SYNTHETIC_NET_RE.match(net_name) and _RAIL_TOKEN_RE.search(net_name):
        return False
    # Voltage-prefixed names (P3V3_CLK, +5V_CLK, VDD_OSC …) are rails, not
    # signals, whatever suffix token they carry. Wins over the signal patterns.
    if _VOLTAGE_PREFIX_RE.match(net_name):
        return False
    return any(p.search(net_name) for p in DEFINITE_SIGNAL_PATTERNS)


# ── Fix γ: Tier-1 augmentation (survey §4 sketches #1–#4) ─────────────────────
# Recover the rail names POWER_NET_RE misses that Gemma used to catch, so the
# Tier-2 LLM call can be retired without losing legitimate deterministic rails.
# See corpus_results/step_06_llm_decision_survey.md §4. Kept separate from
# POWER_NET_RE so the original matcher and its tests stay untouched.

# #1: voltage-rail prefix, tolerant of a leading '/' and a trailing suffix that
# POWER_NET_RE's ^…$ anchoring rejects (/+5V_USB, +5V_EXT, +3.3VLAN, +3.3VA,
# P5V_VME, /+3.3V). The decimal-volt branch is NEW — _VOLTAGE_PREFIX_RE lacks it.
# Bare \d+V_ strapping (3V3_EN, 5V_RST) is intentionally NOT matched (no sign /
# P / V prefix), preserving the locked Option-B signal-exemption decision.
_GAMMA_VOLTAGE_RAIL_RE = re.compile(
    r'^/?(?:'
    r'[+-]?\d+\.\d+V\d*'          # +3.3V, 3.3V (decimal) — base for +3.3VLAN/+3.3VA
    r'|P\d+V\d*'                  # P5V, P3V3, P12V
    r'|V\d+V\d*'                  # V3V3, V1V8
    r'|[+-]\d+V\d*'              # +5V, +3V3, -12V (signed integer-volt)
    r')(?:[_/]?\w*)?$',
    re.IGNORECASE,
)

# #2: named analog/peripheral VCC (AVCC, XVCC, USBVCC, ISL_VCC). POWER_NET_RE
# only matches VCC as a prefix; these are VCC-suffixed. Voltage unknown.
_GAMMA_VCC_RE = re.compile(r'^/?\w*VCC$', re.IGNORECASE)

# #4: +BATT / VBAT-family unknown-voltage rails (+VBAT, +BATT, bare BATT). The
# tight anchor avoids matching BATCH-style signal names.
_GAMMA_BATT_RE = re.compile(r'^/?\+?V?BAT(?:TERY|T)?\d*$', re.IGNORECASE)


def _synthetic_rail_voltage(net_name: str) -> tuple[bool, float | None] | None:
    """#3: classify a KiCad-synthetic Net-(…)/NET_…_PAD net carrying a rail
    token. Returns (True, voltage) for exactly one distinct voltage token,
    (True, None) for a non-voltage rail token or multiple distinct voltage
    tokens (ambiguous — never guess), or None when this is not a
    synthetic-with-rail-token net (caller falls through)."""
    if not (_SYNTHETIC_NET_RE.match(net_name) and _RAIL_TOKEN_RE.search(net_name)):
        return None
    volts = {v for pat, v in VOLTAGE_FROM_NAME_RE if pat.search(net_name)}
    return (True, next(iter(volts))) if len(volts) == 1 else (True, None)


def classify_power_augmented(net_name: str) -> tuple[bool, float | None] | None:
    """Fix γ Tier-1 augmentation. Returns (True, voltage) when an augmented
    rule recognises a power rail POWER_NET_RE misses (voltage None = unknown);
    None when no augmented rule matches. Run only after is_power_net_deterministic
    so existing rails keep their current handling."""
    syn = _synthetic_rail_voltage(net_name)
    if syn is not None:
        return syn
    if _GAMMA_VOLTAGE_RAIL_RE.match(net_name):
        return True, infer_voltage_from_name(net_name)
    if _GAMMA_VCC_RE.match(net_name) or _GAMMA_BATT_RE.match(net_name):
        return True, infer_voltage_from_name(net_name)
    return None


# ── Fix β: converter-VIN topology context ─────────────────────────────────────
# Consumed by step_08b (supply-overvoltage check), NOT a Gemma prompt — step 06
# is fully deterministic (Gemma removed, TODO-62). A converter's INPUT (VIN) net
# is easy to misjudge when the net name carries the pin's *function* but not its
# *voltage* (e.g. /Boost Converter Vin): the board's dominant rail on a boost is
# the OUTPUT, not the input. This table + the VIN/VOUT pin matchers below let
# step_08b distinguish a converter's input from its output before asserting an
# overvoltage FAIL.
#
# Family inference is an EXPLICIT table, never a regex: TPS6xxxx spans both boost
# (TPS6118x/TPS6123x) and buck (TPS621xx), so a pattern would mislabel parts —
# worse than omitting the hint. Keys are MPN prefixes matched case-insensitively
# against the netlist part_number (e.g. "LM3670" matches "LM3670MF"). Grow this
# table one entry at a time as converters surface in future corpus runs.
CONVERTER_FAMILY = {
    "TPS61230":  "boost",
    "TPS62125":  "buck",
    "TPS51200":  "linear_sequencer",   # sink/source DDR termination — Tier-1 only
    "LM3670":    "buck",
}


# Logical pin-name shapes. VIN matcher is deliberately tight and must never match
# VOUT; VOUT matcher feeds Tier-3 rail lookup.
_VIN_PIN_RE  = re.compile(r'^(?:P?VIN|VINP|VBAT|VBUS|VSYS)$', re.IGNORECASE)
_VOUT_PIN_RE = re.compile(r'^(?:VOUT\d*|VO)$', re.IGNORECASE)


def _converter_family(part_number: str) -> str | None:
    """Return the topology bucket for an MPN via prefix match, else None."""
    if not part_number:
        return None
    pn = part_number.upper()
    for prefix, family in CONVERTER_FAMILY.items():
        if pn.startswith(prefix.upper()):
            return family
    return None


# ── Main entry point ──────────────────────────────────────────────────────────

@dataclass
class PowerRail:
    net_name: str
    voltage_v: float | None
    confidence: str    # "high" | "medium" | "low"
    source: str        # "deterministic" | "user_confirmed"


def infer_power_nets(ir, rail_map=None) -> tuple[list[PowerRail], list[str]]:
    """
    Classifies all nets into power rails and ground nets.

    Returns (power_rails: list[PowerRail], ground_nets: list[str]).

    Deterministic name-based classification on every net (Tier-1 regex +
    classify_power_augmented). High-fanout nets (>=4 pins) left unclassified are
    signal-filtered where possible; the rest stay unclassified (Fix γ removed the
    Tier-2 Gemma fallback).
    """
    net_fanout = {net.name: len(net.pins) for net in ir.nets}

    power_rails: list[PowerRail] = []
    ground_nets: list[str] = []
    classified_names: set[str] = set()

    # Survey trace (STEP_06_DEBUG): per-net classification-path records, built
    # only when the flag is on. Keyed by net name; emitted once at function end.
    _survey_on = _survey_enabled()
    survey: dict[str, dict] = {}
    _board = os.path.basename(getattr(ir, "source_file", "") or "") or "<unknown>"

    def _rec(net, path: str, **extra) -> None:
        if not _survey_on:
            return
        survey[net.name] = {
            "board": _board,
            "net": net.name,
            "fanout": len(net.pins),
            "path": path,
            "refdes": sorted({ref for ref, _ in net.pins}),
            **extra,
        }

    # ── Top tier (TODO-134): user-supplied confirmed rail map ──────────────
    # Highest precedence — a user DECLARATION overrides Tier-1/Fix-γ inference.
    # INERT when rail_map is None (the run_corpus_test path): every branch is
    # guarded, so the deterministic output is byte-identical when unused.
    _rail_index = rail_map_mod.build_index(rail_map) if rail_map else None
    _rail_matched_keys: set[str] = set()
    rail_map_conflicts: list[dict] = []
    rail_map_nonrail: list[str] = []

    def _shadow_classify(nm: str):
        """What Tier-1/Fix-γ WOULD have decided — for conflict detection only."""
        if is_ground_net_deterministic(nm):
            return ("ground", None)
        if is_power_net_deterministic(nm):
            return ("power", infer_voltage_from_name(nm))
        aug = classify_power_augmented(nm)
        if aug is not None:
            return ("power", aug[1])
        return None

    for net in ir.nets:
        name = net.name

        if _rail_index is not None:
            decl, mkey = rail_map_mod.lookup(_rail_index, name)
            if decl is not None:
                _rail_matched_keys.add(mkey)
                shadow = _shadow_classify(name)
                # (i) declared GROUND → ir.ground_nets (not carried in confirmed_voltages)
                if decl["is_ground"]:
                    ground_nets.append(name)
                    classified_names.add(name)
                    if shadow is not None and shadow[0] != "ground":
                        rail_map_conflicts.append({
                            "net": name, "declared": "ground",
                            "inferred": f"{shadow[0]}={shadow[1]}", "kind": "conflict"})
                    _rec(net, "user_ground")
                    continue
                # (ii) declared NOT-a-rail → leave UNCLASSIFIED: step_08c keeps its
                # WARN AND step_08 no longer skips it (the un-skip / unmask direction)
                if decl["is_rail"] is False:
                    rail_map_nonrail.append(name)
                    if shadow is not None and shadow[0] == "power":
                        rail_map_conflicts.append({
                            "net": name, "declared": "not_a_rail",
                            "inferred": f"power={shadow[1]}", "kind": "unmask"})
                    _rec(net, "user_nonrail")
                    continue
                # (iii) declared POWER rail
                v = decl["voltage"]
                power_rails.append(PowerRail(
                    net_name=name, voltage_v=v,
                    confidence="high", source="user_confirmed"))
                classified_names.add(name)
                if shadow is not None:
                    inf_kind, inf_v = shadow
                    if inf_kind == "ground" or (
                            inf_v is not None and v is not None
                            and float(inf_v) != float(v)):
                        rail_map_conflicts.append({
                            "net": name, "declared": f"power={v}",
                            "inferred": f"{inf_kind}={inf_v}", "kind": "conflict"})
                _rec(net, "user_confirmed")
                continue

        if is_ground_net_deterministic(name):
            ground_nets.append(name)
            classified_names.add(name)
            _rec(net, "tier1_ground")
            continue

        if is_power_net_deterministic(name):
            voltage = infer_voltage_from_name(name)
            confidence = "high" if voltage is not None else "low"
            power_rails.append(PowerRail(
                net_name=name,
                voltage_v=voltage,
                confidence=confidence,
                source="deterministic",
            ))
            classified_names.add(name)
            _rec(net, "tier1_power_known" if voltage is not None
                 else "tier1_power_unknown")
            continue

        # Fix γ Tier-1 augmentation (survey §4 sketches #1–#4): rail names that
        # are real supplies by construction but miss POWER_NET_RE (leading '/',
        # trailing suffix, named *VCC, synthetic rail tokens, +BATT). Recovers
        # the legitimate catches Gemma used to make, deterministically.
        aug = classify_power_augmented(name)
        if aug is not None:
            _, voltage = aug
            power_rails.append(PowerRail(
                net_name=name,
                voltage_v=voltage,
                confidence="high" if voltage is not None else "low",
                source="deterministic",
            ))
            classified_names.add(name)
            _rec(net, "tier1_power_known" if voltage is not None
                 else "tier1_power_unknown")

    print(f"[STEP 06] Deterministic: {len(power_rails)} power rails, "
          f"{len(ground_nets)} ground nets classified")

    for rail in power_rails:
        write_trace(
            net_name=rail.net_name,
            step_id="step_06_power",
            step_display_name="Power net inference",
            step_type="hybrid",
            decision_path="tier1_deterministic",
            inputs={"net_name": rail.net_name, "fanout": net_fanout.get(rail.net_name, 0)},
            outputs={"voltage_v": rail.voltage_v, "confidence": rail.confidence, "source": "deterministic"},
        )
    for gnd in ground_nets:
        write_trace(
            net_name=gnd,
            step_id="step_06_power",
            step_display_name="Power net inference",
            step_type="hybrid",
            decision_path="tier1_deterministic_ground",
            inputs={"net_name": gnd, "fanout": net_fanout.get(gnd, 0)},
            outputs={"is_ground": True, "source": "deterministic"},
        )

    MIN_FANOUT_FOR_AMBIGUOUS = 4
    high_fanout_unclassified = [
        net for net in ir.nets
        if net.name not in classified_names
        and net_fanout[net.name] >= MIN_FANOUT_FOR_AMBIGUOUS
    ]
    # Logical pin-name lookup, kept for the survey trace's pin_names field.
    pinname_by_ref_pin = {
        (c.refdes, p.pin_id): p.pin_name
        for c in ir.components for p in c.pins
    }

    # Survey: nets below the fanout gate never reached a Tier-2 decision and stay
    # unclassified (→ UNRESOLVABLE downstream).
    if _survey_on:
        for net in ir.nets:
            if net.name not in classified_names and net_fanout[net.name] < MIN_FANOUT_FOR_AMBIGUOUS:
                _rec(net, "low_fanout_skip")

    # Fix γ: no Tier-2 Gemma fallback. High-fanout nets Tier-1 didn't classify are
    # signal-filtered where possible; the remainder stay unclassified and resolve
    # to UNRESOLVABLE in later steps (replacing the former gemma_called path).
    n_unclassified = 0
    for net in high_fanout_unclassified:
        if is_definitely_signal(net.name):
            _rec(net, "signal_filtered")
            continue
        n_unclassified += 1
        _rec(
            net, "unclassified_highfanout",
            pin_names=sorted({
                pn for ref, pid in net.pins
                if (pn := pinname_by_ref_pin.get((ref, pid)))
            }),
            voltage_prefix_exempt=bool(_VOLTAGE_PREFIX_RE.match(net.name)),
            synthetic_rail_rescue=bool(
                _SYNTHETIC_NET_RE.match(net.name)
                and _RAIL_TOKEN_RE.search(net.name)),
        )
    n_filtered = len(high_fanout_unclassified) - n_unclassified
    if n_filtered:
        print(f"[STEP 06] Signal filter excluded {n_filtered} high-fanout net(s)")
    if n_unclassified:
        print(f"[STEP 06] {n_unclassified} high-fanout net(s) left unclassified "
              f"(no Tier-2 fallback)")

    print(f"[STEP 06] Total: {len(power_rails)} power rail(s), {len(ground_nets)} ground net(s)")

    # ── TODO-134 V3 hook: rail-candidate nets (step_06 portion) ─────────────
    # High-fanout unclassified nets carrying a power token — the stable engine
    # output a future elicitation UX consumes ("we think these are rails —
    # confirm?"). Verdict-neutral: attached to ir, read by no checker. main.py
    # unions this with the step_08c structural-WARN set for the full artifact.
    ir.rail_candidates_step06 = [
        {"net": net.name, "fanout": net_fanout[net.name],
         "refdes": sorted({ref for ref, _ in net.pins}),
         "inferred_voltage": infer_voltage_from_name(net.name)}
        for net in high_fanout_unclassified
        if not is_definitely_signal(net.name)
        and (POWER_NET_RE.match(net.name) or _RAIL_TOKEN_RE.search(net.name)
             or _VOLTAGE_PREFIX_RE.match(net.name))
    ]

    # ── TODO-134 rail-map: surface conflicts + unmatched keys; attach to ir ──
    if rail_map:
        for key in rail_map:
            if key not in _rail_matched_keys:
                print(f"[STEP 06] rail-map: key {key!r} matched no net (typo?)")
        for c in rail_map_conflicts:
            write_trace(
                net_name=c["net"], step_id="step_06_power",
                step_display_name="Power net inference",
                step_type="deterministic",
                decision_path="user_override_conflict",
                inputs={"declared": c["declared"], "inferred": c["inferred"],
                        "kind": c.get("kind", "conflict")},
                outputs={"resolved": "user_wins"})
            print(f"[STEP 06] {c.get('kind','conflict').upper()} {c['net']}: declared "
                  f"{c['declared']} vs inferred {c['inferred']} — user wins (surfaced)")
    ir.rail_map_conflicts = rail_map_conflicts
    ir.rail_map_nonrail = rail_map_nonrail

    if _survey_on:
        _survey_emit(list(survey.values()))

    return power_rails, ground_nets

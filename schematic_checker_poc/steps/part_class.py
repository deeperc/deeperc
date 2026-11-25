"""Part / pin classification helpers shared by the corpus driver and step_08b.

The pseudo-part recognizer (moved verbatim from run_checks.py, TODO-484) and the
admission-side ground/pad pin-name filter used by step_08b's A-prime supply-pin
admission.
"""
import re


# ── Provenance-based pseudo-part recognizer (eligibility gate only) ────────────
# A real test point / solder jumper / connector / crystal whose VALUE is a signal
# name (e.g. a TestPoint valued "SPI1 MOSI", a SolderJumper valued "SDA", a
# connector valued "CANBUS") reads as a signal-shaped IC under the value-string
# heuristic and wrongly counts as a BLOCKING unresolvable part, inflating the
# eligibility blocking ratio. The distinguishing signal is PROVENANCE — the
# libsource part / footprint / refdes prefix — NOT the value string (keying on the
# value misfires on genuinely oddly-named real ICs like ME6210-SOT89 or BL4054B).
# This recognizes those pseudo-parts so they do not block eligibility. It is used
# ONLY by is_eligible — it does NOT alter the IR consumed by the checkers.
_REFDES_PREFIX_RE = re.compile(r'^[A-Za-z#]+')


def pseudo_part_provenance(refdes: str, footprint: str, libsource_part: str) -> str | None:
    """Return the pseudo-part class (testpoint/jumper/connector/crystal/switch/
    buzzer/mounting) inferred from PROVENANCE, else None for a genuine IC.

    Keyed on libsource part / footprint / refdes prefix, never the value string.
    """
    ref = refdes or ""
    m = _REFDES_PREFIX_RE.match(ref)
    refpre = m.group(0) if m else ""
    lp = (libsource_part or "")
    lpl = lp.lower()
    fpl = (footprint or "").lower()

    if lp == "TestPoint" or fpl.startswith("testpoint:") or refpre == "TP":
        return "testpoint"
    if "mountinghole" in lpl or "mountinghole" in fpl or "fiducial" in lpl or "fiducial" in fpl:
        return "mounting"
    if (lpl.startswith("solderjumper") or lpl == "sj" or refpre in ("JP", "SJ")
            or "jumper" in lpl or "solder_jumper" in fpl):
        return "jumper"
    if (refpre in ("J", "P", "CON", "CAN", "BH") or "connector" in lpl
            or fpl.startswith("connector:") or re.match(r"(?i)conn?\d", lp)
            or re.match(r"(?i)bh\d", lp) or "_connectors" in fpl or "uext" in fpl):
        return "connector"
    if (refpre in ("Y", "X") or lpl.startswith("crystal") or fpl.startswith("crystal:")
            or lpl.startswith("resonator")):
        return "crystal"
    if refpre == "SW" or "switch" in lpl:
        return "switch"
    if refpre == "BZ" or "buzzer" in lpl:
        return "buzzer"
    return None


def is_pseudo_part_by_provenance(comp) -> bool:
    """True when a ComponentIR is a non-IC pseudo-part by provenance — must not
    count toward the eligibility blocking-unresolvable numerator."""
    return pseudo_part_provenance(
        getattr(comp, "refdes", "") or "",
        getattr(comp, "footprint", "") or "",
        getattr(comp, "libsource_part", "") or "",
    ) is not None


# ── Non-IC part recognizer (step_08 signal endpoints, TODO-554) ────────────────
# The measured rule set (investigation/experiments/part_class_exec_recon/classify.py,
# rules A..K) plus the two later-measured rules L and M, appended after K so no
# earlier rule's result changes. Each rule is an ordered list of
# (evidence_field, condition); the rule fires on the first true condition and that
# condition's field is the class_evidence. Rule A is pseudo_part_provenance itself.
NON_IC_CLASSES = frozenset({
    "connector", "testpoint", "crystal", "buzzer", "mounting", "switch", "jumper",
    "resistor_network", "led", "relay", "varistor_pot", "protection_diode",
    "transistor",
})

_TVS_RE = re.compile(r"\btvs\b")


def _non_ic_rules(L: str, P: str, FL: str, FN: str, D: str):
    """Rules B..M, in order: (part_class, [(evidence, thunk), ...])."""
    return (
        ("connector", [  # B
            ("library", lambda: L.startswith("connector")),
            ("footprint", lambda: FL.startswith("connector")),
            ("part", lambda: P.startswith("conn_")),
            ("description", lambda: "connector" in D),
            ("library", lambda: "batteryholder" in L),
        ]),
        ("testpoint", [  # C
            ("part", lambda: "testpad" in P),
            ("footprint", lambda: "testpad" in FN),
            ("footprint", lambda: "testpoint" in FN),
        ]),
        ("jumper", [  # D
            ("library", lambda: "jumper" in L),
            ("footprint", lambda: "jumper" in FL),
        ]),
        ("resistor_network", [  # E
            ("footprint", lambda: FN.startswith(("r_matrix", "r_array", "r_pack", "r_network"))),
            ("part", lambda: P.startswith(("r_pack", "r_network"))),
        ]),
        ("led", [  # F
            ("part", lambda: P == "led"),
            ("part", lambda: P.startswith("led_")),
            ("footprint", lambda: FL.startswith("led")),
            ("footprint", lambda: "_leds" in FL),
            ("library", lambda: L.startswith("led")),
            ("description", lambda: D.startswith("light emitting")),
        ]),
        ("switch", [  # G
            ("footprint", lambda: FN.startswith(("sw_", "sw-"))),
            ("library", lambda: "button" in L),
            ("part", lambda: "button" in P),
            ("library", lambda: "pushbutton" in L),
            ("description", lambda: "tactile switch" in D),
        ]),
        ("relay", [  # H
            ("library", lambda: "relay" in L),
            ("part", lambda: "relay" in P),
            ("footprint", lambda: "relay" in FN),
        ]),
        ("varistor_pot", [  # I
            ("part", lambda: P == "varistor"),
            ("part", lambda: P == "pot"),
            ("footprint", lambda: FL.startswith("potentiometer")),
            ("description", lambda: "voltage dependent resistor" in D),
        ]),
        ("protection_diode", [  # J
            ("library", lambda: "tvs" in L),
            ("library", lambda: L.endswith("_diodes")),
            ("description", lambda: bool(_TVS_RE.search(D))),
            ("description", lambda: "esd protection" in D),
            ("description", lambda: "schottky" in D),
        ]),
        ("transistor", [  # K
            ("library", lambda: "transistor" in L and "array" not in L),
            ("part", lambda: "mos+diod" in P),
            ("description", lambda: D.startswith("power mosfet")),
        ]),
        ("switch", [  # L
            ("footprint", lambda: "button" in FL),
            ("footprint", lambda: "switch" in FL),
        ]),
        ("protection_diode", [  # M
            ("footprint", lambda: "diode" in FL),
        ]),
    )


def classify_part(comp) -> tuple[str, str] | None:
    """(part_class, class_evidence) for a non-IC part, else None for an IC.

    comp is a ComponentIR or any object with refdes, footprint, libsource_lib,
    libsource_part, libsource_description (missing attributes read as "").
    class_evidence is "provenance" (rule A, pseudo_part_provenance) or the field
    the firing condition tests: "library", "part", "footprint", "description".
    """
    R = getattr(comp, "refdes", "") or ""
    footprint = getattr(comp, "footprint", "") or ""
    lib = getattr(comp, "libsource_lib", "") or ""
    part = getattr(comp, "libsource_part", "") or ""
    desc = getattr(comp, "libsource_description", "") or ""
    pc = pseudo_part_provenance(R, footprint, part)
    if pc is not None:
        return pc, "provenance"
    L = lib.casefold()
    P = part.casefold()
    fp = footprint.casefold()
    FL, _, FN = fp.partition(":") if ":" in fp else (fp, "", "")
    D = desc.casefold()
    for part_class, conditions in _non_ic_rules(L, P, FL, FN, D):
        for evidence, cond in conditions:
            if cond():
                return part_class, evidence
    return None


# ── Ground / pad pin-name filter (step_08b supply-pin admission) ───────────────
_GROUND_PAD_TOKENS = frozenset({"GND", "VSS", "PAD", "EP", "SUB", "TAB"})
_GROUND_TOKEN_RE = re.compile(r"[A-Z]?(GND|VSS)[A-Z0-9]*")
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]+")


def is_ground_or_pad_pin(pin_name: str) -> bool:
    """True when a pin NAME is ground- or pad-shaped.

    The admission-side ground/pad filter for step_08b (TODO-484): a pin that a
    power group lists but whose name reads as ground, an exposed pad, substrate
    or tab is never admitted as a supply pin by listing. Upper-cases, deletes
    '{' and '}' (so KiCad's P_{GND} reads P_GND — this brace removal is local to
    this match; general brace normalization is TODO-528, out of scope), splits
    on runs of non-alphanumerics, and matches each token against the fixed
    vocabulary or [A-Z]?(GND|VSS)[A-Z0-9]*. Vocabulary measured in
    the TODO-484 design recon (M6): 0 false exclusions on
    the 450 CORE pins.
    """
    if not pin_name:
        return False
    name = pin_name.upper().replace("{", "").replace("}", "")
    for token in _NON_ALNUM_RE.split(name):
        if not token:
            continue
        if token in _GROUND_PAD_TOKENS or _GROUND_TOKEN_RE.fullmatch(token):
            return True
    return False

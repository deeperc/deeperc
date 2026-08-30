"""
Phase 1.1.b: KB-anchored I2C peripheral mismatch detector.

check_i2c_peripheral(netlist, kb, peripheral_routing=None) -> list[PeripheralFinding]

Net classification
------------------
A net is treated as an I2C net if:
  1. Any pin on it is fixed-function I2C (ALL roles belong to Peripheral.I2C), OR
  2. The net name matches an I2C pattern (I2C, SDA, or SCL, case-insensitive).

Checks run per net
------------------
PROTOCOL_MISMATCH  FAIL  — pin in KB but has no I2C roles at all
INSTANCE_MISMATCH  FAIL  — two I2C-capable pins with different non-None instances
ROLE_MISMATCH      FAIL  — >=2 MCU I2C-capable pins with mutually exclusive signals
                           (some SCL-only, some SDA-only)
NO_PULLUP_DETECTED WARN  — no resistor found connecting net to a positive power rail
UNRESOLVABLE            — MCU absent from KB, pin absent from KB, or peripheral is
                           matrix-routed (PERIPHERAL_UNCONSTRAINED path)

Cross-net check
---------------
MISSING_PERIPHERAL FAIL  — I2C SDA net exists but no SCL net (or vice versa)

MPN canonicalization
--------------------
Real netlists use specific MPNs (STM32F103C8T6, ESP32-WROOM-32).  KB files use
range-form keys (STM32F103C(8-B)Tx, esp32).  canonicalize_mpn_for_kb() normalises
before lookup; a two-phase fallback keeps test fixtures using exact MPNs working.
"""
import re
from dataclasses import dataclass, field
from enum import Enum

from .peripheral_kb import Peripheral, PeripheralRouting, Signal
from .peripheral_coherence import check_i2c_coherence, check_spi_coherence
from .net_name_utils import normalize_net_name
from .component_values import resistance_ohms
from .peripheral_roles import is_i3c_net_name


# ── Output types ─────────────────────────────────────────────────────────────

class PeripheralViolation(Enum):
    ROLE_MISMATCH      = "ROLE_MISMATCH"
    PROTOCOL_MISMATCH  = "PROTOCOL_MISMATCH"
    INSTANCE_MISMATCH  = "INSTANCE_MISMATCH"
    MISSING_PERIPHERAL = "MISSING_PERIPHERAL"
    NO_PULLUP_DETECTED = "NO_PULLUP_DETECTED"
    CAPABILITY_MISMATCH = "CAPABILITY_MISMATCH"   # M14 (Phase 3): SDA/SCL on a KB-incapable pin
    # M15 (TODO-93 Phase 2): a UART link landing on a pin the KB says has no UART role.
    # A DISTINCT value rather than a reuse of CAPABILITY_MISMATCH: the recall harness
    # matches catches by violation string, so a shared value would let a UART FAIL and an
    # I2C FAIL score each other's operator. Same severity + same invariant, different net.
    UART_CAPABILITY_MISMATCH = "UART_CAPABILITY_MISMATCH"


class Severity(Enum):
    FAIL         = "FAIL"
    WARN         = "WARN"
    UNRESOLVABLE = "UNRESOLVABLE"
    PASS         = "PASS"


@dataclass
class PeripheralFinding:
    violation:     PeripheralViolation
    severity:      Severity
    net:           str
    pins:          list           # list[str], e.g. ["U1.PB6", "U2.SDA"]
    evidence:      str
    kb_provenance: list = field(default_factory=list)  # list[KBSource]


# ── Internal lookup status ────────────────────────────────────────────────────

class _LookupStatus(Enum):
    OK                       = "ok"
    KB_MISSING               = "kb_missing"
    PIN_NOT_IN_KB            = "pin_not_in_kb"
    PERIPHERAL_UNCONSTRAINED = "peripheral_unconstrained"


# ── MPN canonicalization ──────────────────────────────────────────────────────

_STM32_RANGE_MAP: dict[str, str] = {
    "STM32F103C8T":  "STM32F103C(8-B)Tx",
    "STM32F103CBT":  "STM32F103C(8-B)Tx",
    "STM32F407VET":  "STM32F407V(E-G)Tx",
    "STM32F407VGT":  "STM32F407V(E-G)Tx",
    "STM32F303RBT":  "STM32F303R(B-C)Tx",
    "STM32F303RCT":  "STM32F303R(B-C)Tx",
    # F405-range alias onto the F407V KB data (Todo-79 precedent: the F407V
    # seed itself was a similarly small, targeted range-map add). F405 shares
    # its reference manual (RM0090) and AF table with F407; Nelson verified via
    # STM32CubeMX per-part XML (2026-07-17) that the AF-to-peripheral mapping
    # for shared GPIO names is identical between STM32F405RG and the existing
    # F407V entry. That verification licenses exactly this alias — no new KB
    # file, no new pins/roles, no other F4 family (F411 is a different
    # sub-family, RM0383/RM0390, and stays KB_MISSING by design).
    "STM32F405RGT":  "STM32F407V(E-G)Tx",
}

_ESP32_VARIANT_RE = re.compile(
    r'^ESP32[-_]?(S2|S3|C2|C3|C6|H2|P4)\b', re.I
)
_ESP32_BASE_RE = re.compile(r'^ESP32\b', re.I)

# RP2040 silicon MPNs vary by reel/stepping (RP2040, RP2040-B2, RP2040B2) but all
# share the one fixed-mux GPIO function table → one KB key. Match only the bare
# silicon stem; Pico/module MPNs (e.g. SC0914) are deliberately NOT mapped this
# cycle to keep the KB blast radius to RP2040 silicon (recon SCOPE).
_RP2040_RE = re.compile(r'^RP2040', re.I)


def _canonicalize_stm32(mpn: str) -> str:
    upper = mpn.upper()
    if upper.endswith("TR"):   # strip tape-and-reel suffix before temperature digit
        upper = upper[:-2]
    # Strip the trailing temperature-grade code: a digit (…C8T6) OR the literal 'X'
    # placeholder form (…C8Tx → …C8TX). Both reduce to the range-map key (…C8T).
    stripped = re.sub(r'[\dX]$', '', upper)
    return _STM32_RANGE_MAP.get(stripped, mpn)


def _canonicalize_esp32(mpn: str) -> str:
    m = _ESP32_VARIANT_RE.match(mpn)
    if m:
        return f"esp32-{m.group(1).lower()}"
    if _ESP32_BASE_RE.match(mpn):
        return "esp32"
    return mpn


def canonicalize_mpn_for_kb(mpn: str) -> str:
    """Map a specific MPN to its KB canonical (range-form) key.

    STM32F103C8T6  -> STM32F103C(8-B)Tx
    ESP32-WROOM-32 -> esp32
    UNKNOWN_XYZ    -> UNKNOWN_XYZ  (unchanged; leads to KB_MISSING)
    """
    u = mpn.upper()
    if u.startswith("STM32"):
        return _canonicalize_stm32(mpn)
    if u.startswith("ESP32"):
        return _canonicalize_esp32(mpn)
    if _RP2040_RE.match(mpn):
        return "RP2040"
    return mpn


# ── Passive component filter ─────────────────────────────────────────────────

def _is_passive_refdes(refdes: str) -> bool:
    """True for passives (R*, C*, L*, FB*) that should not be KB-looked-up."""
    r = refdes.upper()
    return r.startswith(("R", "C", "L")) or r.startswith("FB")


# ── Net classification helpers ────────────────────────────────────────────────

_I2C_NET_RE = re.compile(r'(?:I2C|SDA|SCL)', re.I)
_SDA_RE     = re.compile(r'SDA', re.I)
_SCL_RE     = re.compile(r'SCL', re.I)


def _is_fixed_function_i2c(entry) -> bool:
    """True if ALL roles on the entry belong to Peripheral.I2C."""
    return bool(entry.roles) and all(r.peripheral == Peripheral.I2C for r in entry.roles)


def _net_signal_hint(net_name: str):
    """Return Signal.I2C_SDA, Signal.I2C_SCL, or None based on net name.

    I3C-discrimination gate FIRST (card 252, i3c_band_recon): I3C is a distinct
    protocol from I2C (push-pull SDR; no SCL pull-up; a high-value SDA pull-up
    is normal) — an I3C-named net (e.g. 'I3C0_SCL_3V3') must never hint I2C just
    because "SCL" is a substring embedded in "I3C...".
    """
    if is_i3c_net_name(net_name):
        return None
    if _SDA_RE.search(net_name):
        return Signal.I2C_SDA
    if _SCL_RE.search(net_name):
        return Signal.I2C_SCL
    return None


def _classify_i2c_net(net, comps_by_refdes, pinname_by_ref_id, kb,
                      peripheral_routing) -> bool:
    """True if ``net`` is an I2C net, per the check_i2c_peripheral Step-1 rule.

    Primary: any non-passive pin resolves a fixed-function I2C KB entry (ALL roles
    I2C). Secondary: net name matches the I2C pattern AND ≥1 pin is I2C-capable
    (≥1 I2C role) — a name hint ALONE is explicitly insufficient. Extracted
    verbatim from the per-net loop so the M3/NO_PULLUP gate has a single source of
    truth (reused by step_08g's generic-OD suppression); the caller passes the
    prebuilt `comps_by_refdes`/`pinname_by_ref_id` maps so behavior is byte-
    identical to the inlined loop.

    I3C discrimination (card 252) is checked FIRST, outright — an I3C-named net
    is never I2C even when a genuine fixed-function-I2C device shares the bus
    (a real I3C legacy-compatibility topology): the PRIMARY gate below is
    name-independent and would otherwise misclassify it regardless of the
    SECONDARY gate's own name check.
    """
    if is_i3c_net_name(net.name):
        return False
    is_i2c = False
    any_i2c_capable = False
    for refdes, pin_id in net.pins:
        comp = comps_by_refdes.get(refdes)
        if comp is None or _is_passive_refdes(comp.refdes):
            continue
        _status, entry = _resolve_pin(comp.effective_mpn, pin_id, kb, peripheral_routing,
                                      pinname_by_ref_id.get((refdes, pin_id)))
        if entry is not None:
            if any(r.peripheral == Peripheral.I2C for r in entry.roles):
                any_i2c_capable = True
            if _is_fixed_function_i2c(entry):
                is_i2c = True
                break
    if not is_i2c and any_i2c_capable and _I2C_NET_RE.search(net.name):
        is_i2c = True
    return is_i2c


def is_i2c_classified_net(net, netlist, kb, peripheral_routing=None) -> bool:
    """Public wrapper: is ``net`` I2C-classified per check_i2c_peripheral's gate?

    Builds the lookup maps from ``netlist`` and delegates to `_classify_i2c_net`.
    step_08g's generic-OD family calls THIS to suppress on I2C nets (M3/NO_PULLUP
    owns those) — reusing the real KB-resolution gate, never a net-name heuristic.
    """
    comps_by_refdes = {c.refdes: c for c in netlist.components}
    pinname_by_ref_id = {
        (c.refdes, p.pin_id): p.pin_name
        for c in netlist.components for p in c.pins
    }
    return _classify_i2c_net(net, comps_by_refdes, pinname_by_ref_id, kb,
                             peripheral_routing)


# ── Pull-up detection ─────────────────────────────────────────────────────────

# Positive-rail recognizer for pull-up high-side nets. Feeds the pull-up surface:
# M3 NO_PULLUP_DETECTED presence (`_has_pullup` below), the de-dup guard, and —
# via `from .step_08d_peripheral_checker import _is_power_rail_net` — step_08e's
# M4 pull-up VALUE check. Fork-6 (TODO-165) borrowed step_08c._is_rail_name's
# normalize + per-segment '/'/whitespace split + a NARROWED subset of its
# rail-token set, so a pull-up whose high-side is a hierarchical/decorated rail
# (e.g. `/CSIA_I2C_VCC`) is recognized here as step_08c's M7 gate recognizes it.
#
# NARROWED, not a full port of C (deliberate — the pull-up surface asks "is this
# a pull-up TARGET?", a narrower question than C's M7 "is this a power rail?"):
#   * suffix branch is `_VCC$`/`_VDD$`-family ONLY — NOT `_PWR$`/`_VSW$`. Porting
#     C's `_PWR$` made a 0Ω level-shifter link to `FLASH_PWR` (a switched USB
#     domain) read as a pull-up → false FAIL on usb_debug_pd (the Cycle-A STOP).
#   * tokens add PVCC/PVDD/IOVCC/IOVDD/COREVDD/VBAT/VSYS only — NOT VIN/VOUT/VSW
#     (converter/switching nodes are never pull-up targets → a new FP class) and
#     NOT VREF (no current need).
#   * dotted-decimal `+3.3V`/`+1.8V` IS matched (KiCad global-label convention;
#     ESP32-PoE2 / kit-dev M4 sites) — C misses the dot, D must not.
#   * Fix #1 (folded): the prior bare `\+` alternative matched ANY '+'-prefixed
#     net (+EN/+RUN) as a rail. Dropped — normalize strips the leading '+', then
#     a real rail token must still match, so `+3V3`/`+5V`/`+3.3V` keep matching
#     while `+EN`/`+RUN` no longer do.
# Deliberately NOT an import of step_08c — the two recognizers stay source-
# independent (see the step_08c drift note).
_POWER_RAIL_RE = re.compile(
    r'^(?:'
    r'VCC|VDD|VBUS|PWR|AVCC|AVDD|DVCC|DVDD|'
    r'PVCC|PVDD|IOVCC|IOVDD|COREVDD|VBAT|VSYS'
    r')(?:$|[_/A-Z0-9])'
    r'|^\d+(?:\.\d+)?V\d*(?:$|[_/A-Z0-9])'
    r'|.*_(?:VDD|VCC|VDDA|VDDD|VDDIO|AVDD|DVDD|PVDD)\d*$',
    re.I,
)

# Jumper-class floor (Fork-6 Option B): a resistor whose value parses BELOW this
# is a 0Ω link / shunt / config strap, categorically NOT an I2C pull-up. Applied
# in `_has_pullup` (presence) and step_08e `_pullup_sites` (value). An UNPARSEABLE
# value is NOT floored out — it must still reach step_08e as UNRESOLVABLE. The
# floor sits well below any plausible pull-up incl. the 100Ω M4 mutants (100 > 10),
# so it cannot collapse M4 recall.
PULLUP_MIN_OHMS = 10.0


def _is_power_rail_net(net_name: str) -> bool:
    """True when a net NAME looks like a positive power rail (recognition only,
    voltage-agnostic). Normalizes, then matches the rail pattern in ANY '/'-or-
    whitespace-delimited segment (a per-segment leading polarity marker stripped
    too), mirroring step_08c._is_rail_name."""
    if not net_name:
        return False
    norm = normalize_net_name(net_name)
    return any(
        _POWER_RAIL_RE.match(seg.lstrip("+-"))
        for seg in re.split(r"[/\s]+", norm)
        if seg.lstrip("+-")
    )


def _has_pullup(net, netlist) -> bool:
    """Return True if a resistor on ``net`` connects to a positive power rail.

    A component is a pull-up candidate if:
    - refdes starts with 'R' or 'FB', and
    - it has exactly 2 pins.
    The other pin's net must match a power-rail pattern.
    """
    comps_by_refdes = {c.refdes: c for c in netlist.components}
    net_pins_set = {(refdes, pin_id) for refdes, pin_id in net.pins}

    for refdes, _pin_id in net.pins:
        comp = comps_by_refdes.get(refdes)
        if comp is None:
            continue
        r = comp.refdes.upper()
        if not (r.startswith("R") or r.startswith("FB")):
            continue
        if len(comp.pins) != 2:
            continue
        # Find the other pin
        other_pins = [p for p in comp.pins if (comp.refdes, p.pin_id) not in net_pins_set]
        if not other_pins:
            continue
        other_net_name = other_pins[0].net
        if not _is_power_rail_net(other_net_name):
            continue
        # Jumper-class floor: a 0Ω link / shunt to the rail is not a pull-up.
        # Unparseable / value-less components pass (kept as a candidate — never
        # silently dropped; getattr guards component shapes without a value).
        ohms = resistance_ohms(getattr(comp, "value", None))
        if ohms is not None and ohms < PULLUP_MIN_OHMS:
            continue
        return True
    return False


# ── KB lookup ─────────────────────────────────────────────────────────────────

def _resolve_pin(mpn: str, pin_id: str, kb: dict, peripheral_routing: dict | None,
                 pin_name: str | None = None, peripheral: Peripheral = Peripheral.I2C):
    """Two-phase KB lookup with canonicalization.

    The KB is keyed by the LOGICAL pin name (e.g. ``('STM32F103C(8-B)Tx','PB7')``);
    a real KiCad netlist references pins by NUMBER, which the parser stores as
    ``pin_id`` ('43') with the logical name on ``pin_name`` (derived from the
    pinfunction). So the lookup must key by ``pin_name`` — keying by the raw
    number misses the name-keyed KB. ``pin_name`` is preferred; ``pin_id`` is a
    fallback only for hand-crafted fixtures whose pin_id IS the logical token
    (a numeric pin_id simply misses the name-keyed KB → a clean miss, never a
    wrong-key hit).

    ``peripheral`` selects WHICH tracked peripheral's routing flag gates the
    matrix short-circuit (default ``Peripheral.I2C``, preserving every existing
    call site's behavior unchanged). Generalized 2026-07-11 alongside the ESP32
    KB routing-flag correction (UART/SPI are now correctly flagged "matrix" —
    see investigation/experiments/esp32_kb_recon/REPORT.md) so a future
    UART/SPI-anchored capability check can pass ``peripheral=Peripheral.UART``/
    ``Peripheral.SPI`` and get the same PERIPHERAL_UNCONSTRAINED protection I2C
    already has. Mirrors the pattern already used by
    ``peripheral_coherence.matrix_lookup_from(..., peripheral)``.

    Returns (_LookupStatus, PinFunctionEntry | None).
    """
    # Check matrix routing first (before even looking up the pin)
    canonical = canonicalize_mpn_for_kb(mpn)
    routing_for_mcu = (peripheral_routing or {}).get(canonical)
    if routing_for_mcu is None and canonical != mpn:
        routing_for_mcu = (peripheral_routing or {}).get(mpn)
    if routing_for_mcu and routing_for_mcu.get(peripheral) == PeripheralRouting.MATRIX:
        return _LookupStatus.PERIPHERAL_UNCONSTRAINED, None

    # Candidate keys: logical name first (real name-keyed KB), then the raw
    # pin_id (fixtures). Order-preserving + de-duped so a numeric pin_id is tried
    # once and just misses the name-keyed KB.
    keys = [k for k in (pin_name, pin_id) if k]
    keys = list(dict.fromkeys(keys))
    entry = None
    for key in keys:
        entry = kb.get((canonical, key))                      # Phase 1: canonical
        if entry is None and canonical != mpn:
            entry = kb.get((mpn, key))                         # Phase 2: exact MPN
        if entry is not None:
            break

    if entry is None:
        # Determine if the MCU is entirely absent or just this pin
        mcu_known = any(k[0] in (canonical, mpn) for k in kb)
        if mcu_known:
            return _LookupStatus.PIN_NOT_IN_KB, None
        return _LookupStatus.KB_MISSING, None

    return _LookupStatus.OK, entry


# ── Main checker ──────────────────────────────────────────────────────────────

def check_i2c_peripheral(
    netlist,
    kb: dict,
    peripheral_routing: dict | None = None,
) -> list[PeripheralFinding]:
    """Check I2C peripheral wiring against the KB.

    Args:
        netlist:            Netlist object with .components and .nets.
        kb:                 dict[(mpn, pin_id) -> PinFunctionEntry] from load_kb().
        peripheral_routing: dict[mcu_part -> {Peripheral -> PeripheralRouting}]
                            from load_kb_routing(), or None.

    Returns a flat list of PeripheralFinding objects (FAIL, WARN, UNRESOLVABLE).
    """
    findings: list[PeripheralFinding] = []
    comps_by_refdes = {c.refdes: c for c in netlist.components}
    # (refdes, pin_id) → logical pin name (the KB key); the net loop iterates by
    # pin number, so resolve the name here to look the KB up correctly.
    pinname_by_ref_id = {
        (c.refdes, p.pin_id): p.pin_name
        for c in netlist.components for p in c.pins
    }

    sda_nets: list[str] = []
    scl_nets: list[str] = []

    for net in netlist.nets:
        # ── Step 1: classify this net (passives excluded) ─────────────────────
        # Primary: any pin is fixed-function I2C (ALL roles are I2C).
        # Secondary: net name matches I2C pattern AND at least one pin has
        #   I2C capability (≥1 I2C role) — name hint alone is not sufficient.
        # (Single source of truth — step_08g reuses `is_i2c_classified_net`.)
        if not _classify_i2c_net(net, comps_by_refdes, pinname_by_ref_id, kb,
                                 peripheral_routing):
            # TODO-329 C1: an I2C-named net whose ONLY I2C-capable-looking pin is
            # matrix-routed (e.g. bare ESP32 GPIO, no fixed-function anchor) never
            # classifies — `_classify_i2c_net`'s `any_i2c_capable` requires a
            # resolved KB entry, but `_resolve_pin` short-circuits a matrix pin to
            # `entry=None` before lookup (recon: TODO-20 Phase 1's own sizing scan
            # named this the deferred gap). Such a net was silently dropped with 0
            # findings — worse than the MIXED case (matrix pin + a fixed-function
            # anchor, Step 3 below), which at least gets the honest UNRESOLVABLE.
            # Emit that same honest UNRESOLVABLE here, gated on the net name so a
            # non-I2C net stays silent; do NOT touch classification itself — the
            # net still skips Steps 2-9 exactly as before.
            if _I2C_NET_RE.search(net.name):
                for refdes, pin_id in net.pins:
                    comp = comps_by_refdes.get(refdes)
                    if comp is None or _is_passive_refdes(comp.refdes):
                        continue
                    status, _entry = _resolve_pin(
                        comp.effective_mpn, pin_id, kb, peripheral_routing,
                        pinname_by_ref_id.get((refdes, pin_id)))
                    if status == _LookupStatus.PERIPHERAL_UNCONSTRAINED:
                        canonical = canonicalize_mpn_for_kb(comp.effective_mpn)
                        findings.append(PeripheralFinding(
                            violation=PeripheralViolation.ROLE_MISMATCH,
                            severity=Severity.UNRESOLVABLE,
                            net=net.name,
                            pins=[f"{refdes}.{pin_id}"],
                            evidence=(
                                f"Cannot verify I2C wiring on net {net.name!r}. "
                                f"{comp.effective_mpn} (canonical: {canonical}) routes I2C via "
                                "the GPIO matrix — any GPIO can serve as SDA or SCL. Pin "
                                "assignment correctness depends on firmware configuration, "
                                "not schematic pin choice."
                            ),
                            kb_provenance=[],
                        ))
                        break  # one UNRESOLVABLE per net is enough
            continue

        # ── Step 2: resolve all non-passive pins on this net ─────────────────
        Results = []  # [(refdes, pin_id, mpn, status, entry)]
        for refdes, pin_id in net.pins:
            comp = comps_by_refdes.get(refdes)
            if comp is None or _is_passive_refdes(comp.refdes):
                continue
            status, entry = _resolve_pin(comp.effective_mpn, pin_id, kb, peripheral_routing,
                                         pinname_by_ref_id.get((refdes, pin_id)))
            Results.append((refdes, pin_id, comp.effective_mpn, status, entry))

        net_findings: list[PeripheralFinding] = []

        # ── Step 3: PERIPHERAL_UNCONSTRAINED → UNRESOLVABLE (early exit) ─────
        peripheral_unconstrained = False
        for refdes, pin_id, mpn, status, entry in Results:
            if status == _LookupStatus.PERIPHERAL_UNCONSTRAINED:
                canonical = canonicalize_mpn_for_kb(mpn)
                findings.append(PeripheralFinding(
                    violation=PeripheralViolation.ROLE_MISMATCH,
                    severity=Severity.UNRESOLVABLE,
                    net=net.name,
                    pins=[f"{refdes}.{pin_id}"],
                    evidence=(
                        f"Cannot verify I2C wiring on net {net.name!r}. "
                        f"{mpn} (canonical: {canonical}) routes I2C via the GPIO matrix — "
                        "any GPIO can serve as SDA or SCL. Pin assignment correctness "
                        "depends on firmware configuration, not schematic pin choice."
                    ),
                    kb_provenance=[],
                ))
                peripheral_unconstrained = True
                break  # one UNRESOLVABLE per net is enough
        if peripheral_unconstrained:
            continue  # this net is done; downstream nets/post-loop blocks still run

        # ── Step 4: PROTOCOL_MISMATCH ─────────────────────────────────────────
        protocol_mismatch_emitted = False
        for refdes, pin_id, mpn, status, entry in Results:
            if status != _LookupStatus.OK or entry is None:
                continue
            i2c_roles = [r for r in entry.roles if r.peripheral == Peripheral.I2C]
            if len(i2c_roles) == 0:
                # Todo 240 strap exemption: a KB pin explicitly marked as a
                # deliberately non-participating strap/config pin (e.g. INA219
                # A0/A1 address-select strapped onto SDA/SCL) is NOT a protocol
                # violation — skip the condemnation. The pin still has status OK,
                # so it is counted RESOLVED by Step 7 below (it never enters
                # `missing_mpns`, which keys on KB_MISSING/PIN_NOT_IN_KB) — that
                # accounting is already correct with no change here. A pin
                # WITHOUT strap continues to condemn exactly as before.
                if entry.strap is not None:
                    continue
                net_findings.append(PeripheralFinding(
                    violation=PeripheralViolation.PROTOCOL_MISMATCH,
                    severity=Severity.FAIL,
                    net=net.name,
                    pins=[f"{refdes}.{pin_id}"],
                    evidence=(
                        f"Pin {refdes}.{pin_id} ({mpn}) has no I2C capability "
                        f"(roles: {[r.peripheral.value for r in entry.roles]}) "
                        f"but is wired to I2C net {net.name!r}."
                    ),
                    kb_provenance=[r.source for r in entry.roles],
                ))
                protocol_mismatch_emitted = True

        # ── Step 5: INSTANCE_MISMATCH ─────────────────────────────────────────
        i2c_instances: set[str] = set()
        for _refdes, _pin_id, _mpn, status, entry in Results:
            if status != _LookupStatus.OK or entry is None:
                continue
            for r in entry.roles:
                if r.peripheral == Peripheral.I2C and r.instance is not None:
                    i2c_instances.add(r.instance)
        if len(i2c_instances) > 1:
            net_findings.append(PeripheralFinding(
                violation=PeripheralViolation.INSTANCE_MISMATCH,
                severity=Severity.FAIL,
                net=net.name,
                pins=[f"{rd}.{pi}" for rd, pi, _, s, e in Results
                      if s == _LookupStatus.OK and e],
                evidence=(
                    f"Net {net.name!r} connects pins from conflicting I2C instances: "
                    f"{sorted(i2c_instances)}. All pins on an I2C bus must share the "
                    "same peripheral instance."
                ),
                kb_provenance=[],
            ))

        # ── Step 6: ROLE_MISMATCH ─────────────────────────────────────────────
        # Only fires when >=2 NON-fixed-function I2C-capable pins have mutually
        # exclusive signals. Fixed-function sensor anchors are excluded because a
        # fixed-function SDA pin on the SCL net is a swap (cross-net, not per-net).
        i2c_capable = []
        for refdes, pin_id, mpn, status, entry in Results:
            if status != _LookupStatus.OK or entry is None:
                continue
            if _is_fixed_function_i2c(entry):
                continue  # fixed-function anchors excluded
            i2c_roles = [r for r in entry.roles if r.peripheral == Peripheral.I2C]
            if i2c_roles:
                i2c_capable.append((refdes, pin_id, i2c_roles))

        if len(i2c_capable) >= 2:
            only_scl = [
                (rd, pi) for rd, pi, roles in i2c_capable
                if all(r.signal == Signal.I2C_SCL for r in roles)
            ]
            only_sda = [
                (rd, pi) for rd, pi, roles in i2c_capable
                if all(r.signal == Signal.I2C_SDA for r in roles)
            ]
            if only_scl and only_sda:
                scl_pins_str = ", ".join(f"{rd}.{pi}" for rd, pi in only_scl)
                sda_pins_str = ", ".join(f"{rd}.{pi}" for rd, pi in only_sda)
                net_findings.append(PeripheralFinding(
                    violation=PeripheralViolation.ROLE_MISMATCH,
                    severity=Severity.FAIL,
                    net=net.name,
                    pins=[f"{rd}.{pi}" for rd, pi in only_scl + only_sda],
                    evidence=(
                        f"Net {net.name!r} has conflicting I2C signals: "
                        f"SCL-only pins [{scl_pins_str}] and SDA-only pins [{sda_pins_str}] "
                        "cannot share a single net. "
                        f"SCL pin(s): {scl_pins_str}."
                    ),
                    kb_provenance=[],
                ))

        # ── Step 7: KB coverage / UNRESOLVABLE ───────────────────────────────
        # Must come before NO_PULLUP so that KB-missing UNRESOLVABLE is visible
        # when we decide whether to suppress the pull-up warning.
        if not protocol_mismatch_emitted:
            missing_mpns = [
                mpn for _rd, _pi, mpn, status, _e in Results
                if status in (_LookupStatus.KB_MISSING, _LookupStatus.PIN_NOT_IN_KB)
            ]
            if missing_mpns:
                provenance = [
                    src
                    for _rd, _pi, _mpn, status, entry in Results
                    if status == _LookupStatus.OK and entry
                    for src in {r.source for r in entry.roles}
                ]
                net_findings.append(PeripheralFinding(
                    violation=PeripheralViolation.ROLE_MISMATCH,
                    severity=Severity.UNRESOLVABLE,
                    net=net.name,
                    pins=[],
                    evidence=(
                        f"Cannot fully verify net {net.name!r}: "
                        f"MPN(s) not in KB: {missing_mpns}."
                    ),
                    kb_provenance=list(set(provenance)),
                ))

        # ── Step 8: NO_PULLUP_DETECTED ────────────────────────────────────────
        # Suppressed behind a real electrical FAIL on this net (PROTOCOL / INSTANCE
        # / ROLE_MISMATCH) — the bus identity itself is in doubt there, so a pull-up
        # warning would be noise on top of a more serious finding. NOT suppressed
        # behind a KB-coverage UNRESOLVABLE: a connector/sensor on the bus whose MPN
        # isn't in the KB (e.g. the I2C-header Conn_01x04_Pin on shadaab1904 /
        # devnithw) is a *coverage gap*, a different root cause, and must not mask a
        # genuinely missing pull-up. By provenance, that KB-miss UNRESOLVABLE
        # (Step 7, from KB_MISSING / PIN_NOT_IN_KB) is the ONLY UNRESOLVABLE that can
        # reach Step 8 — the matrix-routed PERIPHERAL_UNCONSTRAINED case already
        # early-returned at Step 3 — so surfacing past it never surfaces past a
        # firmware-routing ambiguity. This is the WARN-level analog of the M6/M12
        # severity-aware coherence suppression (9e33313): surface past an
        # UNRESOLVABLE, de-dupe behind a FAIL. `_has_pullup` is unchanged, so a net
        # that actually HAS a pull-up (the value-blind heuristic) still suppresses
        # the WARN — no new false positive on a correctly-wired board, connector or
        # not.
        net_has_fail = any(f.severity == Severity.FAIL for f in net_findings)
        if not net_has_fail and not _has_pullup(net, netlist):
            net_findings.append(PeripheralFinding(
                violation=PeripheralViolation.NO_PULLUP_DETECTED,
                severity=Severity.WARN,
                net=net.name,
                pins=[],
                evidence=(
                    f"No pull-up resistor found on I2C net {net.name!r}. "
                    "I2C is open-drain and requires a pull-up to a positive supply."
                ),
                kb_provenance=[],
            ))

        findings.extend(net_findings)

        # ── Step 9: track net direction for MISSING_PERIPHERAL ───────────────
        # Skip nets that already have a FAIL/UNRESOLVABLE — they are broken on
        # their own and should not drive the cross-net check.
        net_has_hard = any(
            f.severity in (Severity.FAIL, Severity.UNRESOLVABLE)
            for f in net_findings
        )
        if not net_has_hard:
            direction = _net_signal_hint(net.name)
            if direction is None:
                for _rd, _pi, _mpn, status, entry in Results:
                    if status == _LookupStatus.OK and entry and _is_fixed_function_i2c(entry):
                        i2c_roles = [r for r in entry.roles if r.peripheral == Peripheral.I2C]
                        signals = {r.signal for r in i2c_roles}
                        if signals == {Signal.I2C_SDA}:
                            direction = Signal.I2C_SDA
                        elif signals == {Signal.I2C_SCL}:
                            direction = Signal.I2C_SCL
                        break
            if direction == Signal.I2C_SDA:
                sda_nets.append(net.name)
            elif direction == Signal.I2C_SCL:
                scl_nets.append(net.name)

    # ── MISSING_PERIPHERAL (cross-net) ────────────────────────────────────────
    if sda_nets and not scl_nets:
        findings.append(PeripheralFinding(
            violation=PeripheralViolation.MISSING_PERIPHERAL,
            severity=Severity.FAIL,
            net=sda_nets[0],
            pins=[],
            evidence=(
                f"I2C SDA net(s) {sda_nets} found but no I2C SCL net present in "
                "the netlist. A complete I2C bus requires both SDA and SCL."
            ),
            kb_provenance=[],
        ))
        # NO_PULLUP WARNs are secondary to the missing-SCL structural failure.
        findings[:] = [
            f for f in findings
            if f.violation != PeripheralViolation.NO_PULLUP_DETECTED
        ]
    elif scl_nets and not sda_nets:
        findings.append(PeripheralFinding(
            violation=PeripheralViolation.MISSING_PERIPHERAL,
            severity=Severity.FAIL,
            net=scl_nets[0],
            pins=[],
            evidence=(
                f"I2C SCL net(s) {scl_nets} found but no I2C SDA net present in "
                "the netlist. A complete I2C bus requires both SDA and SCL."
            ),
            kb_provenance=[],
        ))
        findings[:] = [
            f for f in findings
            if f.violation != PeripheralViolation.NO_PULLUP_DETECTED
        ]

    # ── M6: SDA/SCL coherence (cross-net swap) ────────────────────────────────
    # The per-net ROLE_MISMATCH above needs ≥2 KB-I2C pins on one net; a name-based
    # SDA↔SCL swap leaves each net internally coherent and lands on peripheral pins
    # the KB doesn't cover, so it never fired (gate #0a). The shared coherence
    # primitive catches it from the surviving evidence: a pin whose function/KB
    # role is SDA sitting on an SCL-named net (or vice versa). Coverage-gated:
    # ESP32 matrix-routed I2C → UNRESOLVABLE, never a FAIL. Suppressed only behind an
    # EQUAL-OR-STRONGER existing finding on the same net (FAIL > UNRESOLVABLE): a
    # definitive coherence FAIL must surface even when the per-net path left an
    # UNRESOLVABLE (e.g. an I2C header connector on the bus whose MPN isn't in the KB)
    # — that UNRESOLVABLE is a different root cause and must not mask the swap. A
    # coherence FAIL is still de-duped behind a real per-net FAIL (same root cause).
    _flagged_fail = {f.net for f in findings if f.severity == Severity.FAIL}
    _flagged_any = {f.net for f in findings
                    if f.severity in (Severity.FAIL, Severity.UNRESOLVABLE)}
    for v in check_i2c_coherence(netlist, kb, peripheral_routing, canonicalize_mpn_for_kb):
        if v.net in (_flagged_fail if v.status == "FAIL" else _flagged_any):
            continue
        _pin_sig = "SDA" if v.pin_role == "I2C_DATA" else "SCL"
        _net_sig = "SDA" if v.net_role == "I2C_DATA" else "SCL"
        findings.append(PeripheralFinding(
            violation=PeripheralViolation.ROLE_MISMATCH,
            severity=Severity.FAIL if v.status == "FAIL" else Severity.UNRESOLVABLE,
            net=v.net,
            pins=[f"{v.refdes}.{v.pin_id}"],
            evidence=(
                f"Pin {v.refdes}.{v.pin_id} (function {v.pin_function!r}) is an I2C "
                f"{_pin_sig} pin but sits on net {v.net!r}, whose name implies I2C "
                f"{_net_sig} — SDA/SCL swap (role source: {v.source})."
            ),
            kb_provenance=[],
        ))

    # ── M12/M99: SPI MOSI/MISO/SCK coherence (cross-net swap) ─────────────────
    # Mirror of the M6 I2C block over the SPI role GROUP, reusing the same shared
    # primitive. The DI/DO/SDI/SDO/DIN/DOUT flash alias is recognized checker-side
    # via role_from_pin_function (peripheral_detectability), with the same
    # net-frame direction the detectability gate uses: a peripheral's DI pin
    # belongs on the MOSI net, DO on the MISO net — so CORRECTLY-wired SPI is
    # coherent and does NOT flag (verified 0 FAIL over all 270 eligible good
    # boards); only a swap contradicts. TODO-410: SPI now ALSO consults the KB
    # possible_roles (peripheral_kb.Signal.SPI_MOSI/MISO/SCK/NSS), pin-function-
    # first exactly as I2C already does — see check_spi_coherence's docstring.
    # Suppressed only behind an equal-or-stronger finding (recompute to include
    # the M6 catches above), same severity-aware policy as M6 — a FAIL surfaces
    # past an UNRESOLVABLE.
    #
    # Todo 99 (M99): the group widened from {MOSI, MISO} to {MOSI, MISO, SCK} —
    # find_coherence_violations made no 2-member assumption, so this is purely a
    # signal-set change here too. The MOSI/MISO evidence text is UNCHANGED byte-
    # for-byte (the "MOSI/MISO swap" tail is preserved exactly when both roles are
    # in {MOSI, MISO}) — existing tests/recall assert on that literal substring;
    # only a violation involving SCK gets the new tail.
    _SPI_SIG_LABEL = {"SPI_DATA_OUT": "MOSI", "SPI_DATA_IN": "MISO", "SPI_CLOCK": "SCK"}
    _flagged_fail = {f.net for f in findings if f.severity == Severity.FAIL}
    _flagged_any = {f.net for f in findings
                    if f.severity in (Severity.FAIL, Severity.UNRESOLVABLE)}
    for v in check_spi_coherence(netlist, kb, canonicalize_mpn_for_kb):
        if v.net in (_flagged_fail if v.status == "FAIL" else _flagged_any):
            continue
        _pin_sig = _SPI_SIG_LABEL.get(v.pin_role, v.pin_role)
        _net_sig = _SPI_SIG_LABEL.get(v.net_role, v.net_role)
        _mosi_miso = {"SPI_DATA_OUT", "SPI_DATA_IN"}
        _swap_tail = ("MOSI/MISO swap" if {v.pin_role, v.net_role} <= _mosi_miso
                      else f"{_pin_sig}/{_net_sig} SPI role swap")
        findings.append(PeripheralFinding(
            violation=PeripheralViolation.ROLE_MISMATCH,
            severity=Severity.FAIL if v.status == "FAIL" else Severity.UNRESOLVABLE,
            net=v.net,
            pins=[f"{v.refdes}.{v.pin_id}"],
            evidence=(
                f"Pin {v.refdes}.{v.pin_id} (function {v.pin_function!r}) is an SPI "
                f"{_pin_sig} pin but sits on net {v.net!r}, whose name implies SPI "
                f"{_net_sig} — {_swap_tail} (role source: {v.source})."
            ),
            kb_provenance=[],
        ))

    # ── M14: CAPABILITY MISROUTE (Phase 3, VERDICT-MOVING) ────────────────────
    # SDA/SCL routed onto a pin the KB says CANNOT serve that role (a deterministic
    # dead bus). ADDITIVE to M6/M12 (caught_by_m6=False by construction): a misroute
    # DISPLACES the MCU's capable pin off the role net, so the per-net classifier
    # above drops the net (no I2C-capable pin remains) and PROTOCOL_MISMATCH never
    # fires — the cross-net BUS PAIRING recovers the bus identity from the intact
    # (SDA) side, and the KB then confirms the misrouted pin is incapable.
    #
    # THE INVARIANT — a FAIL fires ONLY on KB-CONFIRMED incapability. Enforced
    # structurally: the evaluator sets `fail_mode == "capability"` ONLY when a member
    # is INCAPABLE, and a member becomes INCAPABLE ONLY from a resolved KB lookup
    # whose possible_roles exclude BOTH I2C roles (peripheral_consensus
    # `incapable_candidate`). A matrix/free-mux pin → UNCONSTRAINED and a non-KB'd pin
    # → SILENT/pinfunc can NEVER become INCAPABLE → they route to UNRESOLVABLE / no
    # finding, never FAIL. CONSENSUS_INSUFFICIENT / TIE_UNRESOLVABLE / the
    # cross-device MINORITY (consensus) path emit NO capability FAIL here — the
    # consensus/swap path stays REPORT-ONLY this phase (it overlaps M6, thin data).
    #
    # M14-F1 (investigation/experiments/m14_f1_recon/REPORT.md): a bus paired
    # SOLELY via KB instance possibility (no independent net-name corroboration)
    # must NOT be admitted to this verdict — see the has_independent_identity
    # gate below. possible_roles may CONDEMN a member (incapable) but a
    # kb_instance-only pairing may never by itself CONFIRM bus identity into a
    # FAIL (the same possibility-may-condemn-never-confirm principle as M15).
    #
    # Lazy imports break the module cycle (peripheral_consensus / peripheral_bus_pairing
    # import back into THIS module). Runs on every board; pair_buses returns [] for a
    # board with no I2C role nets, so the added cost is bounded.
    from .peripheral_bus_pairing import pair_buses, AMBIGUOUS_PAIRING, has_independent_identity
    from .peripheral_consensus import evaluate_bus_consensus, m6_flagged_pins, MemberClass
    from .peripheral_coherence import I2C_PAIR, _KB_SIGNAL_TO_ROLE

    _paired, _ = pair_buses(
        netlist, "I2C", I2C_PAIR, kb=kb, peripheral_routing=peripheral_routing,
        kb_signal_to_role=_KB_SIGNAL_TO_ROLE,
    )
    _m6_flagged = m6_flagged_pins(netlist, kb, peripheral_routing)
    _cap_emitted: set = set()   # (net, refdes, pin_id) — de-dup within this block only
    for _bus in _paired:
        if _bus.pairing_source == AMBIGUOUS_PAIRING:
            continue
        if not has_independent_identity(_bus):   # M14-F1: KB possibility alone may
            continue                              # never assert bus identity into a verdict
        _cons = evaluate_bus_consensus(
            netlist, _bus, I2C_PAIR, kb=kb, peripheral_routing=peripheral_routing,
            kb_signal_to_role=_KB_SIGNAL_TO_ROLE, m6_flagged=_m6_flagged,
        )
        if _cons.fail_mode != "capability":   # INVARIANT gate — capability path only
            continue
        for _m in _cons.members:
            if _m.classification != MemberClass.INCAPABLE.value:
                continue
            _key = (_m.net, _m.refdes, _m.pin_id)
            if _key in _cap_emitted:
                continue
            _cap_emitted.add(_key)
            _comp = comps_by_refdes.get(_m.refdes)
            _mpn = _comp.effective_mpn if _comp else "?"
            _pname = pinname_by_ref_id.get((_m.refdes, _m.pin_id))
            _status, _entry = _resolve_pin(_mpn, _m.pin_id, kb, peripheral_routing, _pname)
            _kb_roles = sorted({r.peripheral.value for r in _entry.roles}) if _entry else []
            _sig = _net_signal_hint(_m.net)
            _role = "SDA" if _sig == Signal.I2C_SDA else ("SCL" if _sig == Signal.I2C_SCL else "SDA/SCL")
            _pin_label = f"{_m.refdes}.{_pname or _m.pin_id}"
            findings.append(PeripheralFinding(
                violation=PeripheralViolation.CAPABILITY_MISMATCH,
                severity=Severity.FAIL,
                net=_m.net,
                pins=[f"{_m.refdes}.{_m.pin_id}"],
                evidence=(
                    f"Confirmed — KB alt-fn map excludes {_role} on {_pin_label} "
                    f"({_mpn}): possible roles {_kb_roles} contain no I2C SDA/SCL "
                    f"function, but the pin sits on paired I2C bus {_bus.bus_id!r} "
                    f"(net {_m.net!r}), corroborated by {_cons.distinct_voter_devices} "
                    f"voter device(s). This is a dead bus: the peripheral physically "
                    f"cannot serve {_role} on this pin. "
                    f"(additive to M6/M12; caught_by_m6={_m.caught_by_m6})"
                ),
                kb_provenance=[r.source for r in _entry.roles] if _entry else [],
            ))

    # ── M15: UART WRONG-DESTINATION CAPABILITY (TODO-93 Phase 2, VERDICT-MOVING) ──
    # A UART link landing on a pin the KB says cannot serve UART at all — the same
    # deterministic dead-link defect as M14, on the UART bus instead of I2C. The
    # decision lives in `uart_capability.classify_netlist_uart` (unit-tested in
    # isolation); this block only turns its findings into PeripheralFindings.
    #
    # NET-NAME IS NEVER CONSULTED. UART is device-relative — a correctly-wired
    # bridge's TXD legitimately sits on a net *named* RX (named from the MCU's
    # receiving perspective), so a net-name-derived role assertion mis-fires on
    # correct boards (the reverted 7924d53 attempt did exactly that). A net is
    # "confirmed USART" ONLY by an ENDPOINT whose own pin-function string reads
    # TXD/RXD — an ACTUAL function — gated by the CAN/LIN guard, since a CAN
    # transceiver's TXD/RXD pin-function matches just as literally (a real SIT1050T
    # on the OLIMEXINO seed proves this is not theoretical).
    #
    # A KB UART possible_role does NOT confirm (source (b), removed at the Phase-2
    # STOP): possible_roles is an alt-fn MAP, and STM32 PB6/PB7 — the standard I2C1
    # pinout — carry USART1 tx/rx as alternates, so every correct I2C bus was being
    # read as a USART net. possible_roles may CONDEMN (zero UART roles = the silicon
    # cannot do it) but never CONFIRM. See uart_capability.py + phase2/REPORT.md.
    #
    # THE INVARIANT — a FAIL fires ONLY on KB-CONFIRMED incapability. Structural in
    # the classifier (a matrix-routed or non-KB'd pin routes to `cleared`, never to a
    # finding) and RE-ASSERTED here, so the emit path cannot outlive a future
    # classifier change that weakens it: no confirming endpoint, or any UART role at
    # all on the victim pin, and this block emits nothing.
    #
    # Lazy import breaks the module cycle (uart_capability imports _resolve_pin /
    # _is_passive_refdes / _LookupStatus back out of THIS module).
    from .uart_capability import classify_netlist_uart

    for _uf in classify_netlist_uart(netlist, kb, peripheral_routing).findings:
        _inc = _uf.incapable_endpoint
        if _inc is None or not _uf.confirming_endpoints:
            continue   # INVARIANT: an unconfirmed net can never produce a FAIL
        if any((p or "").upper() == Peripheral.UART.value for p in _inc.kb_possible_peripherals):
            continue   # INVARIANT: never FAIL on a pin the KB gives ANY UART role
        _ce = _uf.confirming_endpoints[0]
        _ce_label = f"{_ce.refdes}.{_ce.pin_name or _ce.pin_id}"
        _ce_why = f"pin function {_ce.pin_name!r} → {_ce.pinfunc_role}"
        _pin_label = f"{_inc.refdes}.{_inc.pin_name or _inc.pin_id}"
        findings.append(PeripheralFinding(
            violation=PeripheralViolation.UART_CAPABILITY_MISMATCH,
            severity=Severity.FAIL,
            net=_uf.net,
            pins=[f"{_inc.refdes}.{_inc.pin_id}"],
            evidence=(
                f"Confirmed — KB alt-fn roles exclude UART on {_pin_label} "
                f"({_inc.mpn}): possible roles {_inc.kb_possible_peripherals} contain "
                f"no UART function, but the pin sits on net {_uf.net!r}, confirmed as a "
                f"USART link by endpoint {_ce_label} ({_ce.mpn}, {_ce_why}; source "
                f"{_ce.source}). This is a dead link: the part physically cannot serve "
                f"UART on this pin. (net name never consulted)"
            ),
            kb_provenance=_inc.kb_sources,
        ))

    return findings

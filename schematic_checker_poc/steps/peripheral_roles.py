"""
Peripheral role classification for V2 Phase 1.1.a.

Defines roles, peripherals, and classifiers that turn net names and
pin function strings into RoleAssignment evidence objects. Phase 1.1.a
uses only deterministic regex pattern matching — no datasheet lookups.

Forward compatibility: the data model is designed to accommodate
additional sources (datasheet KB, bus topology inference) in later
phases without schema changes.
"""
import re
from dataclasses import dataclass, field
from enum import Enum


class Peripheral(Enum):
    I2C = "I2C"
    SPI = "SPI"
    UART = "UART"
    UNKNOWN = "UNKNOWN"


class Role(Enum):
    # I2C
    I2C_DATA = "I2C_DATA"
    I2C_CLOCK = "I2C_CLOCK"
    # SPI
    SPI_DATA_OUT = "SPI_DATA_OUT"      # MOSI
    SPI_DATA_IN = "SPI_DATA_IN"        # MISO
    SPI_CLOCK = "SPI_CLOCK"            # SCK
    SPI_CHIP_SELECT = "SPI_CHIP_SELECT"
    # UART
    UART_TX = "UART_TX"
    UART_RX = "UART_RX"
    UART_RTS = "UART_RTS"
    UART_CTS = "UART_CTS"
    UNKNOWN = "UNKNOWN"


# Map role to its peripheral for quick lookup
ROLE_TO_PERIPHERAL = {
    Role.I2C_DATA: Peripheral.I2C,
    Role.I2C_CLOCK: Peripheral.I2C,
    Role.SPI_DATA_OUT: Peripheral.SPI,
    Role.SPI_DATA_IN: Peripheral.SPI,
    Role.SPI_CLOCK: Peripheral.SPI,
    Role.SPI_CHIP_SELECT: Peripheral.SPI,
    Role.UART_TX: Peripheral.UART,
    Role.UART_RX: Peripheral.UART,
    Role.UART_RTS: Peripheral.UART,
    Role.UART_CTS: Peripheral.UART,
}


@dataclass
class RoleAssignment:
    """A single piece of evidence about what role a net or pin has."""
    role: Role
    peripheral: Peripheral
    confidence: str          # "high" | "medium" | "low"
    source: str              # "net_name" | "pin_function" | "datasheet_kb" | etc.
    evidence: str            # Human-readable description


# ── Net name patterns ────────────────────────────────────────────────────────
# Patterns are checked in order, first match wins.
# Each entry: (compiled_pattern, peripheral, role, evidence_label)

NET_NAME_PATTERNS = [
    # I2C
    (re.compile(r'(?:^|[_/])(?:I2C\d*[_/])?SDA(?:[_/]?\d*|_[A-Z]+)?$', re.I),
     Peripheral.I2C, Role.I2C_DATA, "I2C SDA"),
    (re.compile(r'(?:^|[_/])(?:I2C\d*[_/])?SCL(?:[_/]?\d*|_[A-Z]+)?$', re.I),
     Peripheral.I2C, Role.I2C_CLOCK, "I2C SCL"),
    # SPI
    (re.compile(r'(?:^|[_/])(?:SPI\d*[_/])?MOSI(?:[_/]?\d*)?$', re.I),
     Peripheral.SPI, Role.SPI_DATA_OUT, "SPI MOSI"),
    (re.compile(r'(?:^|[_/])(?:SPI\d*[_/])?MISO(?:[_/]?\d*)?$', re.I),
     Peripheral.SPI, Role.SPI_DATA_IN, "SPI MISO"),
    (re.compile(r'(?:^|[_/])(?:SPI\d*[_/])?(?:SCK|SCLK)(?:[_/]?\d*)?$', re.I),
     Peripheral.SPI, Role.SPI_CLOCK, "SPI clock"),
    (re.compile(r'(?:^|[_/])(?:SPI\d*[_/])?(?:CS|SS|NSS)(?:_?N)?(?:[_/]?\d*)?$', re.I),
     Peripheral.SPI, Role.SPI_CHIP_SELECT, "SPI chip-select"),
    # UART
    (re.compile(r'(?:^|[_/])(?:UART\d*[_/]|USART\d*[_/])?TX(?:D)?(?:[_/]?\d*)?$', re.I),
     Peripheral.UART, Role.UART_TX, "UART TX"),
    (re.compile(r'(?:^|[_/])(?:UART\d*[_/]|USART\d*[_/])?RX(?:D)?(?:[_/]?\d*)?$', re.I),
     Peripheral.UART, Role.UART_RX, "UART RX"),
    (re.compile(r'(?:^|[_/])(?:UART\d*[_/]|USART\d*[_/])?RTS(?:[_/]?\d*)?$', re.I),
     Peripheral.UART, Role.UART_RTS, "UART RTS"),
    (re.compile(r'(?:^|[_/])(?:UART\d*[_/]|USART\d*[_/])?CTS(?:[_/]?\d*)?$', re.I),
     Peripheral.UART, Role.UART_CTS, "UART CTS"),
]


# ── Pin function patterns ────────────────────────────────────────────────────
# These match against the `pinfunction` field in KiCad netlists.
# Pin function strings often contain alt function names like "PB7/I2C1_SDA"
# or just "SDA" for peripheral ICs.

PIN_FUNCTION_PATTERNS = [
    (re.compile(r'(?:^|[/_])SDA(?:[/_]\d+)?$', re.I),
     Peripheral.I2C, Role.I2C_DATA, "pin function SDA"),
    (re.compile(r'(?:^|[/_])SCL(?:[/_]\d+)?$', re.I),
     Peripheral.I2C, Role.I2C_CLOCK, "pin function SCL"),
    (re.compile(r'(?:^|[/_])MOSI(?:[/_]\d+)?$', re.I),
     Peripheral.SPI, Role.SPI_DATA_OUT, "pin function MOSI"),
    (re.compile(r'(?:^|[/_])MISO(?:[/_]\d+)?$', re.I),
     Peripheral.SPI, Role.SPI_DATA_IN, "pin function MISO"),
    (re.compile(r'(?:^|[/_])(?:SCK|SCLK)(?:[/_]\d+)?$', re.I),
     Peripheral.SPI, Role.SPI_CLOCK, "pin function SCK"),
    (re.compile(r'(?:^|[/_])(?:CS|SS|NSS)(?:_?N)?(?:[/_]\d+)?$', re.I),
     Peripheral.SPI, Role.SPI_CHIP_SELECT, "pin function CS"),
    (re.compile(r'(?:^|[/_])TX(?:D)?(?:[/_]\d+)?$', re.I),
     Peripheral.UART, Role.UART_TX, "pin function TX"),
    (re.compile(r'(?:^|[/_])RX(?:D)?(?:[/_]\d+)?$', re.I),
     Peripheral.UART, Role.UART_RX, "pin function RX"),
    (re.compile(r'(?:^|[/_])RTS(?:[/_]\d+)?$', re.I),
     Peripheral.UART, Role.UART_RTS, "pin function RTS"),
    (re.compile(r'(?:^|[/_])CTS(?:[/_]\d+)?$', re.I),
     Peripheral.UART, Role.UART_CTS, "pin function CTS"),
]


# Direction 1 (TODO-303 2b, wild_netname_recognition_recon.md Shape B): a wild
# board's role token is frequently followed by a trailing decorator suffix
# (voltage rail, camera/bus instance index) the anchored NET_NAME_PATTERNS
# above don't tolerate — 'CCI0_I2C_SCL_3V3', 'SCL_CAM0'. The suffix vocabulary
# is a DELIBERATELY CLOSED SET, not an open alnum charclass: an open suffix
# already produced a real false-positive shape one step further out
# (FP-08E-I3C precedent) by swallowing DisplayPort/HDMI TMDS differential-pair
# nets like 'DP1_TXD0_HDMI_TX2_N' as a false UART TX match. Closed-set only:
# structural voltage tokens (_3V3/_5V/_1V8/_1V2/_2V5/_12V/_1V1-shaped), the
# literal _VBUS/_CONN tokens, and _CAM<n>. Iterative — strips repeatedly so a
# name with more than one trailing decorator ('..._CAM0_3V3') still resolves.
_CLOSED_SET_SUFFIX_RE = re.compile(r'(?:_\d+V\d*|_VBUS|_CONN|_CAM\d+)$', re.I)


def _strip_closed_set_suffix(name: str) -> str:
    while True:
        stripped = _CLOSED_SET_SUFFIX_RE.sub('', name)
        if stripped == name:
            return name
        name = stripped


# Direction 2 (TODO-303 2c) unwrap grammar — literal KiCad auto-gen shapes
# only. Anchored start+end so a name that merely CONTAINS 'Net-' mid-string
# ('USB_Net-Speed_Sel') is untouched, and an unclosed/malformed wrapper
# ('Net-(U1-SDA', 'Net-()') falls through to plain classification below
# rather than mis-extracting (`(.+)` requires a non-empty payload with a
# literal closing paren at the end of the string).
_WRAPPER_RE = re.compile(r'^(?:Net|unconnected)-\((.+)\)$')
_PAD_SUFFIX_RE = re.compile(r'-Pad\d+$')
_REF_PREFIX_RE = re.compile(r'^[A-Za-z0-9]+-')



# Direction 4 (TODO-303 2d): KiCad's overbar/active-low escape syntax renders a
# net-name segment as '~{TEXT}' (the visual overbar). Strip the escape wrapper
# to its bare TEXT before token matching -- '~{CS}' should classify exactly as
# 'CS' does. Runs BEFORE Direction 1's suffix strip so a stacked '~{CS_3V3}'
# shape (suffix INSIDE the braces) resolves too, not just '~{CS}_3V3' (suffix
# outside) -- D1's closed-set regex is end-anchored ($) and can't see past a
# trailing '}', so the brace must come off first either way.
_TILDE_BRACE_RE = re.compile(r'~\{([^{}]*)\}')


def _strip_tilde_brace(name: str) -> str:
    return _TILDE_BRACE_RE.sub(r'\1', name)


def _unwrap_compound_name(name: str) -> list[str] | None:
    """Unwrap a KiCad 'Net-(REF-PINFUNC{slash}PINFUNC...)' / 'unconnected-(REF-
    PINFUNC{slash}PINFUNC...-PadN)' compound name into its constituent alt-
    function element strings. Returns None if `name` isn't wrapper-shaped."""
    m = _WRAPPER_RE.match(name)
    if not m:
        return None
    payload = m.group(1)
    payload = _PAD_SUFFIX_RE.sub('', payload)          # trailing -PadN
    payload = _REF_PREFIX_RE.sub('', payload, count=1)  # leading REF-
    return payload.split('{slash}')


def classify_net_name(net_name: str) -> RoleAssignment | None:
    """
    Classify a net name as a peripheral role.
    Returns None if the name doesn't match any peripheral pattern.
    """
    if not net_name:
        return None
    name = net_name.lstrip('+-/').strip()
    # Direction 2 (TODO-303 2c, wild_netname_recognition_recon.md Shape A/F):
    # KiCad auto-generates 'Net-(REF-PINFUNC{slash}PINFUNC...)' /
    # 'unconnected-(REF-PINFUNC{slash}PINFUNC...-PadN)' compound names for any
    # pin with no user net label, embedding every alt-function name the pin's
    # symbol declares. Unwrap first (before the plain-name path below) and
    # classify each element with this SAME function (recursive — inherits
    # Direction 1's suffix tolerance and the I3C gate for free, no duplicated
    # logic). Exactly one distinct resulting role -> that role; more than one
    # (ordering-ambiguous alt-fn chain) or zero -> decline (None), per card
    # requirement R2 (recon T3: 1/96 Shape-F chains are multi-role in the
    # 8-board sample — a small, bounded forfeit, not a design defect).
    # classify_net_name stays TRUTHFUL on unconnected-(...) names here (it may
    # now return a real role for them) — Phase 2a's consumer-side guards
    # (peripheral_coherence.find_coherence_violations,
    # peripheral_bus_pairing's signal-2 loop) are the ONLY containment layer
    # for unconnected nets; this function does not re-implement that check.
    elements = _unwrap_compound_name(name)
    if elements is not None:
        assignments = [classify_net_name(el) for el in elements]
        distinct_roles = {a.role for a in assignments if a is not None}
        if len(distinct_roles) != 1:
            return None
        winner = next(a for a in assignments if a is not None)
        return RoleAssignment(
            role=winner.role,
            peripheral=winner.peripheral,
            confidence="medium",
            source="net_name",
            evidence=f"D2 unwrap of compound name '{net_name}' -> single "
                     f"resolved role {winner.role.value} ({winner.evidence})",
        )
    # Hierarchical peripheral nets use a dot delimiter (KiCad sheet-scoped labels:
    # 'SPI.MOSI', 'UART1.TX', 'I2C_{CAM}.SDA'). The role tokens are anchored on
    # '_'/'/' boundaries, so normalise '.' → '_' to recognise them. A KiCad label
    # can also literally contain a SPACE ('/I2C0 SCL', 'I2C0 SDA') — the space is
    # not a role-token boundary, so without this the token has no anchor and the
    # name fails to classify (recon rp2040_kb_recon.md P1). Normalise ' ' → '_'
    # too; runs of spaces collapse to runs of '_', still a valid boundary. Nil
    # blast radius: classify_net_name is consumed only by the coherence primitive /
    # detectability harness, never by step_06/step_08/step_08d.
    name = name.replace('.', '_').replace(' ', '_')
    # Direction 4: strip KiCad's '~{X}' active-low escape wrapper before
    # Direction 1's suffix strip (see _strip_tilde_brace above for the
    # stacking-order rationale). Inherited "for free" by Direction 2's
    # recursive per-element unwrap, exactly like Direction 1 already is.
    name = _strip_tilde_brace(name)
    # Direction 1: strip trailing closed-set decorator suffixes before the I3C
    # gate and pattern loop run, so both apply to the same normalised name a
    # bare-role-token match would see. Structurally a no-op for names that
    # already match directly — the closed-set suffixes are alnum-mixed shapes
    # (digit+'V'+digit, or literal VBUS/CONN/CAM+digit) that no existing
    # NET_NAME_PATTERNS trailing branch accepts, so stripping never removes
    # part of an already-successful match.
    name = _strip_closed_set_suffix(name)
    # I3C discrimination (card 252): an I3C token wins over an embedded SDA/SCL
    # substring before the anchored patterns below even run — see is_i3c_net_name.
    if is_i3c_net_name(name):
        return None
    for pattern, peripheral, role, label in NET_NAME_PATTERNS:
        if pattern.search(name):
            return RoleAssignment(
                role=role,
                peripheral=peripheral,
                confidence="medium",
                source="net_name",
                evidence=f"{label} pattern matched in net name '{net_name}'",
            )
    return None


# I3C net-name recognition (card 252, investigation/experiments/i3c_band_recon).
# I3C is a DISTINCT protocol from I2C — push-pull for SDR signalling, no SCL
# pull-up needed at all, and an SDA pull-up (open-drain phases/hot-join only) in
# the tens-to-hundreds-of-kOhm range is normal — so an I3C-named net must never
# be treated as I2C-family by either classify_net_name (this module, feeds
# coherence/pairing) or step_08d's own bare-substring gate (both consult this
# same function; see step_08d_peripheral_checker.py). Checked as a short-circuit
# EXCLUSION so the I3C token wins over an embedded SDA/SCL substring
# ('I3C0_SCL_3V3' contains "SCL" as a substring but is not an I2C net).
# Left-bounded only via a negative lookbehind — 'I3C\d*' has no natural RIGHT
# boundary before an instance digit/underscore/bracket, but must not fire on
# 'I3C' embedded in another token: 'MIPI_I3C0_SCL' and bracket-index
# 'I3C[0]_SCL_3V3' must match; 'SDI3C_FOO' (I3C directly preceded by 'D', not a
# token boundary) must not.
_I3C_NET_RE = re.compile(r'(?<![A-Za-z0-9])I3C\d*', re.I)


def is_i3c_net_name(net_name: str) -> bool:
    """True if net_name is I3C-shaped (a distinct protocol from I2C — see the
    comment above `_I3C_NET_RE`). Single source of truth for I3C discrimination,
    consumed by `classify_net_name` below and by step_08d_peripheral_checker's
    `_net_signal_hint`/`_classify_i2c_net` so an I3C net is excluded from I2C
    treatment at both classification layers identically."""
    if not net_name:
        return False
    name = net_name.replace('.', '_').replace(' ', '_')
    return bool(_I3C_NET_RE.search(name))


# I2C instance token a net name asserts (the 'I2C\d' stem classify_net_name's SDA/
# SCL patterns match but discard). Used by the coherence checker's R-B admissibility
# gate (M6-F2): a KB-sourced role conviction is inadmissible when the net explicitly
# asserts a DIFFERENT bus instance than the KB entry's — a coincidental-token debug
# net misuse, not a genuine cross-instance swap. Bare-token nets (no digit) return
# None, which stays ADMISSIBLE (no instance assertion to disagree with).
_INSTANCE_TOKEN_RE = re.compile(r'(?i)I2C[_-]?(\d+)')


def instance_from_net_name(net_name: str) -> str | None:
    """Extract the explicit I2C instance token a net name asserts, e.g. 'I2C2' from
    '/D8_I2C2_SDA' or '/I2C2_SCL'. Returns None for bare-token nets ('/SDA', '/SCL')
    or instance-less hierarchical labels ('/I2C_{SYS}.SDA') that assert no instance.
    """
    if not net_name:
        return None
    m = _INSTANCE_TOKEN_RE.search(net_name)
    return f"I2C{m.group(1)}" if m else None


def classify_pin_function(pin_function: str) -> RoleAssignment | None:
    """
    Classify a KiCad pinfunction string as a peripheral role.
    Returns None if the string doesn't contain a recognized peripheral pattern.

    Note: pin functions often contain multiple roles in alt-function form,
    e.g. "PB7/I2C1_SDA" — the first match wins.
    """
    if not pin_function:
        return None
    for pattern, peripheral, role, label in PIN_FUNCTION_PATTERNS:
        if pattern.search(pin_function):
            return RoleAssignment(
                role=role,
                peripheral=peripheral,
                confidence="medium",
                source="pin_function",
                evidence=f"{label} pattern matched in pinfunction '{pin_function}'",
            )
    return None

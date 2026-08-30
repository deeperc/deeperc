"""Step 08g — merged pull-up-PRESENCE check (Todo 243 + Todo 212 sub-checks 1-2).

One polarity-aware pull-path walk serving three WARN-tier presence families:

  * Family 1 — generic open-drain / open-collector (and the symmetric open-emitter
    pull-DOWN case): a symbol-OD pin on an actively-used net (≥2 non-passive
    members) with no valid pull path → WARN. Suppressed on I2C-classified nets
    (M3/NO_PULLUP owns those) via the shared `is_i2c_classified_net` gate — never a
    net-name heuristic. `pin_specs.open_drain` is corroborating-only, never the
    sole trigger (symbol pintype is the ~100%-coverage primary signal — recon §7).
    TODO-272 Phase 2a widening: a net with NO open_collector/open_emitter pin can
    still enter Family 1 via a name-based alternate ENTRY — `classify_net_name`
    resolves an I2C_DATA/I2C_CLOCK role from the net name — gated by the module's
    own `_UNCONNECTED_NET_RE` veto (name-path only; the OD path stays byte-
    identical and untouched), then the same ≥2-refdes activity gate and the same
    `is_i2c_classified_net` KB-ownership suppression as the OD path (KB-classified
    → M3 owns; name-classified-only → Family 1's job). Findings carry an
    `activation` field ("od_pintype" | "i2c_net_name") so downstream triage can
    partition the two entry paths (recon:
    investigation/recon_reports/todo272_step08g_od_gate_recon.md).
    TODO-272 Phase 2b (Nelson adjudication on the Phase 2a Step-6 measurement):
    the name-based entry additionally requires pinfunction corroboration — at
    least one NON-PASSIVE pin on the net whose pinfunction matches the
    `_I2C_TOKEN_RE` token matcher (promoted verbatim from the Phase 2a projection
    script's measurement, `investigation/experiments/todo272_widen_family1/
    projection.py`, which found 75/139 = 54% of raw name-path findings would
    disappear under this requirement). Without a token-bearing pin, the name-path
    trigger never activates (no finding is emitted at all — this is an ENTRY
    gate, not a suppression on an already-built finding). The corroboration
    tokens found are still recorded in `corroboration`/`evidence` exactly as
    before; the only behavioral change is that an EMPTY token list now blocks
    entry instead of merely omitting the corroboration clause from evidence text.
  * Family 2 — SPI/QSPI flash chip-select presence: a flash-class part's own CS
    pin-function literal identifies the CS net (net-name corroborating); no pull-up
    path → WARN.
  * Family 3 — SD CMD / DAT0-2 presence: an SD-socket's CMD/DAT pin-function
    literals identify the nets; no pull-up path → WARN. The DAT3/card-detect/CS
    triple-use pin is ambiguous → SKIP silently (precision over recall in v1).

Design decisions are LOCKED by the Phase-1 recon
(investigation/experiments/open_drain_pullup_recon/REPORT.md §6-§7) and the census
(investigation/experiments/pullup_presence_build/CENSUS.md). Key invariants:

  * ONE walk primitive (`has_pull_path`), direction-parameterized (up→power rail /
    down→ground rail — v1 uses "up" everywhere except the open-emitter case), with
    one-series-element tolerance (R, or a cathode-on-net diode + at most one series
    R). A diode leg counts ONLY with correct orientation — cathode-on-net for a
    pull-up leg — gated on the KiCad A_/K_ pin-function polarity. A reversed/flyback
    diode reaching a rail is NOT a pull path (the D6/D10 lock from the recon).
  * Rail vocabulary REUSES step_08d `_is_power_rail_net` (`_POWER_RAIL_RE`, the
    Fork-6/TODO-165 recognizer) — no new regex. The ≥10Ω jumper floor
    (`PULLUP_MIN_OHMS`) is applied to the series resistor exactly as `_has_pullup`.
  * Per-sheet scoping (consistent with existing NO_PULLUP; card-239 limitation
    accepted). No cross-sheet merging. Presence only — NO value-range checks. NO KB
    authoring — identification is symbol pinfunction / pintype only.

Wired LIVE as the registry `08g` checker (VERDICT_MOVING, WARN-only): a new WARN
moves a board all_pass→has_warn (classify_report). Serialized under
`pullup_presence_results`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from steps.step_02_parser import NetlistIR
from steps.step_06_power import POWER_NET_RE, GROUND_NET_RE
from steps.component_values import resistance_ohms
# Reuse step_08d primitives (import, do not fork): positive-rail recognition, the
# jumper floor, the passive filter, and the shared I2C-classification gate.
from steps.step_08d_peripheral_checker import (
    _is_power_rail_net,
    _is_passive_refdes,
    PULLUP_MIN_OHMS,
    is_i2c_classified_net,
)
# TODO-272 Phase 2a: Family 1's name-based alternate entry. step_08g becomes the
# FOURTH consumer of classify_net_name/role_from_net_name (after
# peripheral_coherence, peripheral_bus_pairing, peripheral_detectability — the
# TODO-313 "three copies" lineage) — the module's OWN `_UNCONNECTED_NET_RE`
# (line ~150, already defined for Families 2/3) is reused VERBATIM as this
# fourth consumer's veto; no new regex copy is defined.
from steps.peripheral_roles import classify_net_name, Role

# ── OD/OC pintype vocabulary ──────────────────────────────────────────────────
# KiCad's ETYPE has no "open_drain" — an open-drain pin is exported as
# open_collector; open_emitter is the symmetric pull-down case. Compound
# "+no_connect" tags are stripped to the base type (a no-connect OD pin sits alone
# on an unconnected-(...) net and is excluded by the ≥2-member gate anyway).
OD_PINTYPES = {"open_collector", "open_emitter"}


def _base_pintype(pt: str | None) -> str:
    return (pt or "").split("+")[0]


# ── Diode / resistor topology ─────────────────────────────────────────────────
_RESISTOR_PREFIXES = ("R", "FB")
_DIODE_PREFIXES = ("D", "CR", "LED")
_CATHODE_NAMES = {"K"}     # KiCad diode/LED cathode pin-function logical name
_ANODE_NAMES = {"A"}       # anode


def _is_resistor(refdes: str) -> bool:
    # Match _has_pullup's exact prefix logic (R* / FB*) — same series-link class.
    r = refdes.upper()
    return r.startswith("R") or r.startswith("FB")


def _is_diode(refdes: str) -> bool:
    r = refdes.upper()
    return r.startswith(_DIODE_PREFIXES)


class Direction(Enum):
    UP = "up"       # pull-up: toward a positive power rail; diode cathode-on-net
    DOWN = "down"   # pull-down: toward a ground rail; diode anode-on-net


# ── Family / violation vocabulary ─────────────────────────────────────────────

FAMILY_GENERIC_OD = "generic_od"
FAMILY_FLASH_CS = "flash_cs"
FAMILY_SD = "sd"

VIOLATION_OD_NO_PULLUP = "OPEN_DRAIN_NO_PULLUP"
VIOLATION_OD_NO_PULLDOWN = "OPEN_EMITTER_NO_PULLDOWN"
VIOLATION_FLASH_CS_NO_PULLUP = "FLASH_CS_NO_PULLUP"
VIOLATION_SD_NO_PULLUP = "SD_LINE_NO_PULLUP"
# TODO-272 Phase 2a: name-based alternate-entry violation (distinct from
# VIOLATION_OD_NO_PULLUP — no symbol-declared open_collector/open_emitter pin is
# involved on this path, so labeling it "OPEN_DRAIN..." would misstate the
# evidence; the `activation` field on the finding is the primary partition key).
VIOLATION_I2C_NAME_NO_PULLUP = "I2C_NET_NAME_NO_PULLUP"

# Family 1 activation-source tags (TODO-272 Phase 2a).
ACTIVATION_OD_PINTYPE = "od_pintype"
ACTIVATION_I2C_NET_NAME = "i2c_net_name"

# I2C role set classify_net_name may resolve (peripheral_roles.Role); used only
# to gate the name-based alternate entry, never to broaden it to other buses.
_I2C_ROLES = (Role.I2C_DATA, Role.I2C_CLOCK)

# Corroboration-only pinfunction token scan for name-path findings (never a
# trigger — mirrors pin_specs.open_drain's corroborating-only status on the OD
# path). Reuses the same bare-substring shape as step_08d's own _I2C_NET_RE
# rather than importing it, since this is text-evidence only, not a gate.
_I2C_TOKEN_RE = re.compile(r'(?:I2C|SDA|SCL)', re.I)

# MCU-family MPN/value regex (TODO-272 Phase 2a, dispatch-specified list): these
# families commonly offer configurable internal I2C pull-ups, so a name-path WARN
# on a net with one of these parts gets an additional verify-before-assuming-
# defect caveat (modeled on the connector-NRST caveat, corpus_known_warns.md:189-192).
_MCU_FAMILY_MPN_RE = re.compile(
    r'(STM32|ESP32|RP2040|ATMEGA|ATTINY|NRF5\d|SAMD|PIC\d|MSP430|CH32|GD32)',
    re.I,
)

# Caveat clause appended to name-path evidence when an MCU-family part sits on
# the net. Wording modeled on the connector-NRST TP-WARN-by-design language
# (corpus_known_warns.md:189-192): the MCU's internal pull-up may be a
# deliberate design choice; net-level analysis structurally cannot resolve it.
_MCU_INTERNAL_PULLUP_CAVEAT = (
    " Note: a component on this net matches an MCU family known to offer "
    "configurable internal I2C pull-ups; the MCU's internal pull-up may be "
    "relied on here deliberately. Net-level analysis cannot resolve this — "
    "verify against the firmware configuration and datasheet before treating "
    "this as a defect."
)


@dataclass
class PullupPresenceFinding:
    """A single presence WARN. WARN-only by construction (v1). `severity` is the
    plain string "WARN" so the registry's `_severity_reader` moves the board
    verdict (all_pass→has_warn)."""
    net: str
    family: str
    violation: str
    pins: list
    evidence: str
    severity: str = "WARN"
    corroboration: list = field(default_factory=list)
    # TODO-272 Phase 2a: which Family-1 entry path produced this finding
    # ("od_pintype" | "i2c_net_name"). Families 2/3 have no alternate entry and
    # are out of this phase's scope — left at the default (None, "not
    # applicable") rather than mislabeled as "od_pintype" (their activation was
    # never pintype-based to begin with; it's MPN/pin-function-primary).
    activation: str | None = None


# ── Census-fixed identification matchers (vocabulary from CENSUS.md) ───────────

# SPI/QSPI NOR-flash MPN families (MPN-primary; pin-shape alone over-identifies —
# it caught USB hubs / FPGAs / level-shifters / clock-gens / IMUs / LoRa radios).
FLASH_MPN_RE = re.compile(
    r'(W25[QNXM]|MX25|GD25|IS25|S25FL|S25FS|N25Q|MT25Q|SST25|SST26|EN25|'
    r'AT25[SDF]|AT45DB|25LC\d|25AA\d|XM25|ZD25|P25Q|BY25|FM25|W25P)',
    re.I,
)

# A power/ground pin-name must never read as a chip-select (V_{SS} etc.).
_PWRGND_RE = re.compile(r'(V_?\{?SS|VSS|VDD|GND|VCC)', re.I)
# Chip-select segment (CS/nCS/CS#/SS/CE/SCS, with ~{}/#/_N decorations).
_CS_SEG_RE = re.compile(r'^(?:~?\{?)(?:N|n)?(?:CS|SS|CE|SCS)[#}]?(?:_?N)?$', re.I)
# SD CMD / DAT logical segments.
_SD_CMD_SEG_RE = re.compile(r'^(?:~?\{?)?(?:SD_?CMD|SDIO_?CMD|CMD)[}]?$', re.I)
_SD_DAT_SEG_RE = re.compile(r'^(?:~?\{?)?(?:SD_?DAT[A]?[0-3]|SDIO_?DAT[0-3]|DAT[0-3])[}]?$', re.I)
# net-name CS corroboration (soft signal recorded in evidence).
_CS_NET_RE = re.compile(r'(CS|SS|CHIP_?SEL)', re.I)

# KiCad synthetic net name for a floating (unrouted) pin, e.g.
# "unconnected-(U9-~{CS}-Pad1)" (step08g_unconnected_recon/REPORT.md §1/§4/§5).
# Duplicated (not imported), matching peripheral_bus_pairing.py's
# _UNCONNECTED_NET_RE and the codebase's own documented per-checker
# small-net-name-recognizer convention. A CS/CMD/DAT pad on such a net isn't
# wired at all -- a categorically different, pre-existing claim from "wired
# with no pull-up" -- so families 2/3 must never fire on it. Name-keyed, not
# pin-count-sensitive: KiCad can bucket several no-connect pads of the SAME
# component under one such name (recon §4), and a bare single-refdes CS/CMD/
# DAT pin on a real NAMED net is still a legitimate WARN candidate, so a
# cardinality gate (family 1's ≥2-distinct-refdes shape) is deliberately NOT
# used here (recon §5).
_UNCONNECTED_NET_RE = re.compile(r'unconnected[-_(]', re.IGNORECASE)


def _segs(name: str) -> list[str]:
    return [s for s in re.split(r'[/_]', (name or "").strip()) if s]


def _pin_is_cs(pin_name: str) -> bool:
    if _PWRGND_RE.search(pin_name or ""):
        return False
    return any(_CS_SEG_RE.match(s) for s in _segs(pin_name))


def _pin_is_sd_cmd(pin_name: str) -> bool:
    return any(_SD_CMD_SEG_RE.match(s) for s in _segs(pin_name))


def _pin_is_sd_dat(pin_name: str) -> bool:
    return any(_SD_DAT_SEG_RE.match(s) for s in _segs(pin_name))


def _sd_dat_is_ambiguous(pin_name: str) -> bool:
    """DAT3/card-detect/CS triple-use pin (e.g. 'CD/DAT3/CS') — identification is
    ambiguous, SKIP silently."""
    return _pin_is_sd_dat(pin_name) and (
        _pin_is_cs(pin_name) or bool(re.search(r'\bCD\b', pin_name or "")))


# ── The walk primitive ────────────────────────────────────────────────────────

def _reaches_power_rail(net_name: str) -> bool:
    """Positive-rail recognition for the walk, REUSING step_08d `_is_power_rail_net`
    (the TODO-165 `_POWER_RAIL_RE` vocabulary — no new regex).

    Refinement over the base recognizer: `_is_power_rail_net` splits a name only on
    `/` and whitespace, so a trailing voltage token joined by `_` — RELAY_5V,
    COIL_12V — is NOT recognized (the `5V` token is never isolated). This is the
    exact case of the four LED-sink relay nets (Amit_Amp_V0.1 RELAY_1..4 → R5→
    RELAY_5V): recon REPORT.md §6's parenthetical assumed `_POWER_RAIL_RE` matches
    `RELAY_5V`, but it does NOT (verified against the board). The task's required
    LED-sink acceptance + the "4 LED-sink produce NOTHING" prediction both depend on
    recognizing it, so we apply the SAME recognizer per `_`-segment as well. This
    reuses the vocabulary verbatim (never a new regex) and is walk-local — M3/M4's
    `_is_power_rail_net` is untouched. (Flagged in the Phase-A report / PHASEB
    prediction as a correction of the recon parenthetical.)
    """
    if _is_power_rail_net(net_name):
        return True
    return any(_is_power_rail_net(seg) for seg in (net_name or "").split("_") if seg)


def _is_ground_rail(net_name: str, netlist) -> bool:
    if not net_name:
        return False
    if net_name in getattr(netlist, "ground_nets", []) or []:
        return True
    return bool(GROUND_NET_RE.match(net_name))


def _floor_ok(comp) -> bool:
    """Jumper-class floor (shared semantics with _has_pullup): a resistor whose
    value parses < PULLUP_MIN_OHMS is a 0Ω link / shunt, not a pull element. An
    unparseable value passes (never silently dropped)."""
    ohms = resistance_ohms(getattr(comp, "value", None))
    return ohms is None or ohms >= PULLUP_MIN_OHMS


def _r_to_rail(far_net: str, comps_on_net: dict, rail_pred) -> bool:
    """True if a 2-pin R/FB on ``far_net`` has its OTHER pin on a rail (≥floor).
    The single series-R step of the diode+R chain (no second R — depth-1 only)."""
    if not far_net:
        return False
    for comp in comps_on_net.get(far_net, ()):
        if not _is_resistor(comp.refdes) or len(comp.pins) != 2:
            continue
        others = [p for p in comp.pins if p.net != far_net]
        if not others:
            continue
        if rail_pred(others[0].net) and _floor_ok(comp):
            return True
    return False


def has_pull_path(net, netlist, direction: Direction = Direction.UP,
                  comps_by_refdes: dict | None = None,
                  comps_on_net: dict | None = None) -> bool:
    """Polarity-aware, one-series-element-tolerant pull-path walk from ``net``
    toward a rail in ``direction``.

    Accepts (direction=UP, toward a positive power rail):
      * a 2-pin R/FB directly on ``net`` whose other pin is a power rail (≥floor);
      * a cathode-on-``net`` diode whose anode is a power rail directly, OR reaches
        a power rail via ONE series R (≥floor).
    Rejects: a reversed (anode-on-net) diode; an R→R chain (series depth >1); a
    down-path when an up-path was queried (rail predicate is direction-specific).

    direction=DOWN mirrors it (toward ground; diode ANODE-on-net) for the
    open-emitter symmetric case.
    """
    if comps_by_refdes is None:
        comps_by_refdes = {c.refdes: c for c in netlist.components}
    if comps_on_net is None:
        comps_on_net = _build_comps_on_net(netlist)

    if direction is Direction.UP:
        rail_pred = _reaches_power_rail
        net_side_names = _CATHODE_NAMES     # diode cathode sits on the OD/query net
    else:
        rail_pred = lambda n: _is_ground_rail(n, netlist)   # noqa: E731
        net_side_names = _ANODE_NAMES       # diode anode sits on the query net

    seen_refdes: set[str] = set()
    for refdes, _pin_id in net.pins:
        if refdes in seen_refdes:
            continue
        seen_refdes.add(refdes)
        comp = comps_by_refdes.get(refdes)
        if comp is None or len(comp.pins) != 2:
            continue

        # (1) direct R/FB to rail
        if _is_resistor(comp.refdes):
            others = [p for p in comp.pins if p.net != net.name]
            if others and rail_pred(others[0].net) and _floor_ok(comp):
                return True
            continue

        # (2) cathode-on-net diode (up) / anode-on-net diode (down), + optional R
        if _is_diode(comp.refdes):
            on_net = [p for p in comp.pins if p.net == net.name]
            others = [p for p in comp.pins if p.net != net.name]
            if not on_net or not others:
                continue
            # polarity gate: the pin ON the query net must be the correct terminal
            if _logical_polarity(on_net[0].pin_name) not in net_side_names:
                continue   # reversed / indeterminate diode → not a pull path
            far_net = others[0].net
            if rail_pred(far_net):
                return True
            if _r_to_rail(far_net, comps_on_net, rail_pred):
                return True
            # else: diode reaches neither a rail nor a rail-via-one-R → reject leg
    return False


def _logical_polarity(pin_name: str) -> str | None:
    """Return 'A' (anode), 'K' (cathode), or None (indeterminate) for a diode pin.
    The IR pin_name is the derived logical name (KiCad 'A_2'→'A', 'K_1'→'K')."""
    n = (pin_name or "").strip().upper()
    if n in _ANODE_NAMES:
        return "A"
    if n in _CATHODE_NAMES:
        return "K"
    return None


def _build_comps_on_net(netlist) -> dict:
    idx: dict[str, list] = {}
    for comp in netlist.components:
        for p in comp.pins:
            idx.setdefault(p.net, []).append(comp)
    return idx


# ── Family 1: generic open-drain / open-collector (+ open-emitter) ────────────

def _is_power_or_ground_net(net, netlist) -> bool:
    if net.name in (getattr(netlist, "power_nets", []) or []):
        return True
    if net.name in (getattr(netlist, "ground_nets", []) or []):
        return True
    return bool(POWER_NET_RE.match(net.name) or GROUND_NET_RE.match(net.name))


def _net_has_mcu_family_part(net, comps_by_refdes) -> bool:
    """True if any component with a pin on ``net`` matches the MCU-family MPN/
    value regex (TODO-272 Phase 2a caveat gate; not an activation trigger)."""
    seen: set[str] = set()
    for refdes, _pin_id in net.pins:
        if refdes in seen:
            continue
        seen.add(refdes)
        comp = comps_by_refdes.get(refdes)
        if comp is None:
            continue
        if _MCU_FAMILY_MPN_RE.search(getattr(comp, "effective_mpn", "") or comp.mpn or "") \
                or _MCU_FAMILY_MPN_RE.search(getattr(comp, "value", "") or ""):
            return True
    return False


def _net_i2c_pinfunction_tokens(net, comps_by_refdes) -> list[str]:
    """Pinfunction SDA/SCL/I2C tokens present on ``net``, scanned over NON-PASSIVE
    pins only (TODO-272 Phase 2b: a passive's pin_name — "1"/"2"/"A"/"K" — is not
    a meaningful pinfunction signal and must never itself corroborate an I2C
    read). TODO-272 Phase 2b promotes this from corroboration-only evidence
    enrichment to a required name-path ENTRY gate (see the module docstring) —
    the same scan now also decides whether the name path activates at all, not
    just what its evidence text says."""
    tokens = []
    for refdes, pin_id in net.pins:
        if _is_passive_refdes(refdes):
            continue
        comp = comps_by_refdes.get(refdes)
        pname = _pin_name_of(comp, pin_id)
        if pname and _I2C_TOKEN_RE.search(pname):
            tokens.append(f"{refdes}.{pname}")
    return tokens


def _family_generic_od(ir, pin_specs, kb, peripheral_routing,
                       comps_by_refdes, comps_on_net) -> list[PullupPresenceFinding]:
    findings: list[PullupPresenceFinding] = []
    pintypes = ir.pintypes or {}
    for net in ir.nets:
        if _is_power_or_ground_net(net, ir):
            continue
        # OD pins on this net, split by direction.
        oc_pins, oe_pins = [], []
        for refdes, pin_id in net.pins:
            base = _base_pintype(pintypes.get((refdes, pin_id)))
            if base == "open_collector":
                oc_pins.append((refdes, pin_id))
            elif base == "open_emitter":
                oe_pins.append((refdes, pin_id))

        # TODO-272 Phase 2a — Option A: name-based alternate ENTRY. A net with no
        # OD/OE pin can still qualify if classify_net_name resolves an I2C role
        # from the net name. OD-path nets are UNCHANGED (this branch is mutually
        # exclusive with it — a net qualifying both ways is handled once, via the
        # OD path below, per the dispatch's regression guarantee).
        name_path = False
        name_path_tokens: list[str] = []
        if not oc_pins and not oe_pins:
            # (1) the module's OWN _UNCONNECTED_NET_RE veto — name-path only; the
            # OD path has never needed this (an unrouted OD pin's synthetic net
            # structurally fails the ≥2-refdes gate below on its own).
            if _UNCONNECTED_NET_RE.search(net.name or ""):
                continue
            role_assignment = classify_net_name(net.name)
            if role_assignment is None or role_assignment.role not in _I2C_ROLES:
                continue
            # TODO-272 Phase 2b: pinfunction corroboration is now a required
            # ENTRY condition, not just evidence enrichment — classify_net_name
            # alone is no longer sufficient. At least one non-passive pin on the
            # net must carry an SDA/SCL/I2C pinfunction token (_I2C_TOKEN_RE, the
            # matcher promoted verbatim from the Phase 2a projection script's
            # Step-6 measurement). No token → the name path never activates.
            name_path_tokens = _net_i2c_pinfunction_tokens(net, comps_by_refdes)
            if not name_path_tokens:
                continue
            name_path = True

        # (2) Actively-used gate: ≥2 DISTINCT non-passive refdes on the net.
        # Shared by both paths, unchanged.
        non_passive = {rd for rd, _ in net.pins
                       if rd in comps_by_refdes and not _is_passive_refdes(rd)}
        if len(non_passive) < 2:
            continue

        # (3) Suppress on I2C-classified nets — M3/NO_PULLUP owns those. Reuse the
        # real KB-resolution gate VERBATIM (step_08d), never a net-name heuristic.
        # Same call, same order, for both paths.
        if is_i2c_classified_net(net, ir, kb, peripheral_routing):
            continue

        if name_path:
            # (4) has_pull_path walk, unchanged — I2C is open-drain-in-intent, so
            # only the UP (pull-up) direction applies here.
            if not has_pull_path(net, ir, Direction.UP, comps_by_refdes, comps_on_net):
                evidence = (
                    f"Net {net.name!r} is classified as an I2C data/clock line by "
                    f"net name (no symbol-declared open-collector/open-drain pin on "
                    f"this net; classifier evidence: {role_assignment.evidence}), "
                    f"but has no pull-up path to a positive rail. I2C requires an "
                    f"external pull-up on both SDA and SCL."
                    f" (pinfunction corroborates I2C: {name_path_tokens})"
                )
                if _net_has_mcu_family_part(net, comps_by_refdes):
                    evidence += _MCU_INTERNAL_PULLUP_CAVEAT
                findings.append(PullupPresenceFinding(
                    net=net.name, family=FAMILY_GENERIC_OD,
                    violation=VIOLATION_I2C_NAME_NO_PULLUP,
                    pins=[f"{rd}.{pid}" for rd, pid in net.pins],
                    corroboration=name_path_tokens,
                    evidence=evidence,
                    activation=ACTIVATION_I2C_NET_NAME,
                ))
            continue

        # pin_specs.open_drain is corroborating-only (never the sole trigger).
        corr = []
        for refdes, pin_id in oc_pins + oe_pins:
            comp = comps_by_refdes.get(refdes)
            pname = _pin_name_of(comp, pin_id)
            spec = (pin_specs or {}).get((refdes, pname))
            if spec is not None and getattr(spec, "open_drain", False):
                corr.append(f"{refdes}.{pname}")

        if oc_pins:
            if not has_pull_path(net, ir, Direction.UP, comps_by_refdes, comps_on_net):
                drv = ", ".join(f"{rd}.{_pin_name_of(comps_by_refdes.get(rd), pid)}"
                                for rd, pid in oc_pins)
                findings.append(PullupPresenceFinding(
                    net=net.name, family=FAMILY_GENERIC_OD,
                    violation=VIOLATION_OD_NO_PULLUP,
                    pins=[f"{rd}.{pid}" for rd, pid in oc_pins],
                    corroboration=corr,
                    evidence=(
                        f"Open-collector/open-drain output(s) [{drv}] drive net "
                        f"{net.name!r} with no pull-up path to a positive rail "
                        f"(direct R, or a correctly-oriented diode+R). An OD output "
                        f"only sinks — without a pull-up the net has no defined high "
                        f"level."
                        + (f" (datasheet corroborates open-drain: {corr})" if corr else "")
                    ),
                    activation=ACTIVATION_OD_PINTYPE,
                ))
        if oe_pins:
            if not has_pull_path(net, ir, Direction.DOWN, comps_by_refdes, comps_on_net):
                drv = ", ".join(f"{rd}.{_pin_name_of(comps_by_refdes.get(rd), pid)}"
                                for rd, pid in oe_pins)
                findings.append(PullupPresenceFinding(
                    net=net.name, family=FAMILY_GENERIC_OD,
                    violation=VIOLATION_OD_NO_PULLDOWN,
                    pins=[f"{rd}.{pid}" for rd, pid in oe_pins],
                    corroboration=corr,
                    evidence=(
                        f"Open-emitter output(s) [{drv}] drive net {net.name!r} with "
                        f"no pull-down path to ground. An open-emitter output only "
                        f"sources — without a pull-down the net has no defined low "
                        f"level."
                    ),
                    activation=ACTIVATION_OD_PINTYPE,
                ))
    return findings


# ── Family 2: SPI/QSPI flash chip-select presence ─────────────────────────────

def _family_flash_cs(ir, comps_by_refdes, comps_on_net) -> list[PullupPresenceFinding]:
    findings: list[PullupPresenceFinding] = []
    seen_nets: set[str] = set()
    for comp in ir.components:
        if not FLASH_MPN_RE.search(comp.mpn or ""):
            continue
        for p in comp.pins:
            if not _pin_is_cs(p.pin_name):
                continue
            net = _net_by_name(ir, p.net)
            if net is None or p.net in seen_nets or _UNCONNECTED_NET_RE.search(p.net):
                continue
            seen_nets.add(p.net)
            corr = [f"net-name {p.net!r}"] if _CS_NET_RE.search(p.net) else []
            if not has_pull_path(net, ir, Direction.UP, comps_by_refdes, comps_on_net):
                findings.append(PullupPresenceFinding(
                    net=p.net, family=FAMILY_FLASH_CS,
                    violation=VIOLATION_FLASH_CS_NO_PULLUP,
                    pins=[f"{comp.refdes}.{p.pin_id}"],
                    corroboration=corr,
                    evidence=(
                        f"Flash chip-select {comp.refdes}.{p.pin_name} "
                        f"({comp.mpn}) on net {p.net!r} has no pull-up path to a "
                        f"positive rail. A SPI/QSPI flash CS is commonly pulled up "
                        f"so the device stays deselected while the master's CS driver "
                        f"is high-Z (reset / boot)."
                        + (f" (corroborated by {corr})" if corr else "")
                    ),
                ))
    return findings


# ── Family 3: SD CMD / DAT0-2 presence ────────────────────────────────────────

def _family_sd(ir, comps_by_refdes, comps_on_net) -> list[PullupPresenceFinding]:
    findings: list[PullupPresenceFinding] = []
    seen_nets: set[str] = set()
    for comp in ir.components:
        if _is_passive_refdes(comp.refdes):
            continue
        has_cmd = any(_pin_is_sd_cmd(p.pin_name) for p in comp.pins)
        has_dat = any(_pin_is_sd_dat(p.pin_name) for p in comp.pins)
        if not (has_cmd and has_dat):       # SD-socket pin-shape: CMD + DAT together
            continue
        for p in comp.pins:
            is_cmd = _pin_is_sd_cmd(p.pin_name)
            is_dat = _pin_is_sd_dat(p.pin_name)
            if not (is_cmd or is_dat):
                continue
            if _sd_dat_is_ambiguous(p.pin_name):
                continue                     # DAT3/CD/CS triple-use → SKIP silently
            net = _net_by_name(ir, p.net)
            if net is None or p.net in seen_nets or _UNCONNECTED_NET_RE.search(p.net):
                continue
            seen_nets.add(p.net)
            role = "CMD" if is_cmd else "DAT"
            if not has_pull_path(net, ir, Direction.UP, comps_by_refdes, comps_on_net):
                findings.append(PullupPresenceFinding(
                    net=p.net, family=FAMILY_SD,
                    violation=VIOLATION_SD_NO_PULLUP,
                    pins=[f"{comp.refdes}.{p.pin_id}"],
                    corroboration=[f"pin-function {p.pin_name!r}"],
                    evidence=(
                        f"SD {role} line {comp.refdes}.{p.pin_name} on net {p.net!r} "
                        f"has no pull-up path to a positive rail. SD CMD/DAT lines are "
                        f"open-drain-ish in SD-bus mode and are conventionally pulled "
                        f"up (10-100kΩ)."
                    ),
                ))
    return findings


# ── Helpers ───────────────────────────────────────────────────────────────────

def _net_by_name(ir, name):
    for net in ir.nets:
        if net.name == name:
            return net
    return None


def _pin_name_of(comp, pin_id: str) -> str:
    if comp is None:
        return pin_id
    for p in comp.pins:
        if p.pin_id == pin_id:
            return p.pin_name or pin_id
    return pin_id


# ── Public entry (registry 08g adapter target) ────────────────────────────────

def check_pullup_presence(ir: NetlistIR, pin_specs: dict, peripheral_kb: dict,
                          peripheral_routing: dict | None = None
                          ) -> list[PullupPresenceFinding]:
    """VERDICT-MOVING (registry `08g`). Emit a WARN per net where a symbol-OD /
    flash-CS / SD line lacks the appropriate pull path. WARN-only in v1."""
    comps_by_refdes = {c.refdes: c for c in ir.components}
    comps_on_net = _build_comps_on_net(ir)
    findings: list[PullupPresenceFinding] = []
    findings += _family_generic_od(ir, pin_specs, peripheral_kb, peripheral_routing,
                                   comps_by_refdes, comps_on_net)
    findings += _family_flash_cs(ir, comps_by_refdes, comps_on_net)
    findings += _family_sd(ir, comps_by_refdes, comps_on_net)
    return findings

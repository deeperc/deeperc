"""Pin-role / net-name coherence primitive (Phase 1.1.c).

A coherence violation is a pin whose TRUE role — from its pin-function name
(peripheral_roles regex + the DI/DO SPI alias) or the KB possible_roles — sitting
on a net whose name implies the OTHER signal of a peripheral pair
(I2C SDA↔SCL, SPI MOSI↔MISO). This is the exact contradiction gate #0a already
encodes for the recall harness (peripheral_detectability.detectable_in_principle);
this module is the pipeline-side DETECTOR built on the SAME role recognition —
not a re-derivation.

Shared + signal-pair-parameterised so M6 (I2C) wires it today and M12 (SPI) can
reuse `find_coherence_violations` over `SPI_PAIR` unchanged. UART/M5 is a topology
problem (≥2 TX drivers on a net), NOT coherence — deliberately not handled here.

Todo 99 (M99, SCK swap): the checker's SPI group widens to a 3-member
`SPI_COHERENCE_GROUP` ({MOSI, MISO, SCK}) — `find_coherence_violations` makes no
2-member assumption (frozenset membership only), so this needed zero primitive
changes (recon: investigation/experiments/swap_family_recon/REPORT.md Step 1a).
`SPI_PAIR` itself stays the 2-member MOSI/MISO pair (still consumed as-is by
existing tests and the M12 mutation-operator label) — purely additive.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .peripheral_kb import Peripheral, PeripheralRouting, Signal
from .peripheral_roles import instance_from_net_name

# Reuse gate #0a role recognition (peripheral_roles regex + DI/DO SPI alias). The
# pipeline runs with only schematic_checker_poc/ on sys.path, so bootstrap the repo
# root (this file is …/schematic_checker_poc/steps/peripheral_coherence.py).
try:
    import peripheral_detectability as _pdet
except ImportError:  # pragma: no cover - path bootstrap
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    import peripheral_detectability as _pdet

# Canonical signal pairs (re-exported so consumers don't hardcode roles).
I2C_PAIR = _pdet.SWAPPED_SIGNALS["M6_I2C_SDA_SCL_SWAP"]
SPI_PAIR = _pdet.SWAPPED_SIGNALS["M12_SPI_MOSI_MISO_SWAP"]
# Todo 99: the CHECKER's SPI group widens to include SCK; the mutation-operator
# pair (SPI_PAIR) stays 2-member — only this checker-side group grows.
SPI_COHERENCE_GROUP = SPI_PAIR | frozenset({_pdet.Role.SPI_CLOCK})

# peripheral_kb.Signal → peripheral_roles.Role, for the KB possible_roles source.
_KB_SIGNAL_TO_ROLE = {
    Signal.I2C_SDA:  _pdet.Role.I2C_DATA,
    Signal.I2C_SCL:  _pdet.Role.I2C_CLOCK,
    # TODO-410: SPI_NSS maps to Role.SPI_CHIP_SELECT for completeness (kb_role_lookup_from
    # is a generic (refdes, pin_id) -> set[Role] builder shared by future NSS-aware callers),
    # but SPI_COHERENCE_GROUP below does NOT include SPI_CHIP_SELECT (D3 ruling) — an
    # NSS-only KB conviction can therefore never intersect the group check_spi_coherence
    # passes, so it is inert for coherence today, not consumed.
    Signal.SPI_MOSI: _pdet.Role.SPI_DATA_OUT,
    Signal.SPI_MISO: _pdet.Role.SPI_DATA_IN,
    Signal.SPI_SCK:  _pdet.Role.SPI_CLOCK,
    Signal.SPI_NSS:  _pdet.Role.SPI_CHIP_SELECT,
}


def _is_passive(refdes: str) -> bool:
    r = (refdes or "").upper()
    return r.startswith("FB") or r[:1] in ("R", "C", "L")


# KiCad synthetic net name for a floating (unconnected) pin, e.g.
# "unconnected-(U1-GPIO12-Pad13)". Same token step_08g_pullup_presence.py's
# _UNCONNECTED_NET_RE and peripheral_bus_pairing.py's own copy already
# recognize (duplicated, not imported, per this codebase's per-checker
# name-marker convention — TODO-303 Phase-1 recon). A floating pin's synthetic
# net name is never a real bus signal; without this guard, a role-bearing
# token embedded in the synthetic wrapper (e.g. "unconnected-(U1-SDA-Pad5)")
# would be misread as a genuine I2C/SPI net once classify_net_name widens to
# recognize KiCad's Net-(...)/unconnected-(...) wrapping.
_UNCONNECTED_NET_RE = re.compile(r'unconnected[-_(]', re.IGNORECASE)


@dataclass
class CoherenceViolation:
    refdes:       str
    pin_id:       str
    pin_function: str
    net:          str
    pin_role:     str   # peripheral_roles.Role.value — the pin's true role
    net_role:     str   # the net-name-implied role (the contradiction)
    status:       str   # "FAIL" | "UNRESOLVABLE"
    source:       str   # "pin_function" | "kb_possible_roles"


def find_coherence_violations(
    netlist,
    signal_pair,
    *,
    kb_role_lookup=None,
    matrix_lookup=None,
    kb_instance_lookup=None,
) -> list[CoherenceViolation]:
    """Pin-role vs net-name contradictions within ``signal_pair``.

    Args:
        netlist:        parsed IR with ``.components[].pins[]`` (pin_id, pin_name, net).
        signal_pair:    frozenset of two peripheral_roles.Role (e.g. ``I2C_PAIR``).
        kb_role_lookup: optional ``(refdes, pin_id) -> set[Role]`` — the KB
                        possible_roles source, used only when the pin function
                        asserts no role of the pair.
        matrix_lookup:  optional ``(refdes) -> bool`` — True when the pin's
                        peripheral is matrix-routed (ESP32 I2C). Coverage gate:
                        a KB-derived role on a matrix-routed part is not pin-
                        determined → UNRESOLVABLE, never a coherence FAIL.
        kb_instance_lookup: optional ``(refdes, pin_id) -> {Role: instance|None}``
                        — the instance-preserving twin of kb_role_lookup (M6-F2,
                        R-B). Admissibility gate: a KB-sourced conviction whose
                        bus instance DISAGREES with an explicit instance token the
                        net name asserts is not a genuine cross-instance swap
                        signature — it is indistinguishable from a coincidental-
                        token debug net → UNRESOLVABLE, not FAIL. Bare-token nets
                        (no instance assertion) and matching instances stay
                        admissible exactly as before (see
                        investigation/experiments/m6_f2_recon/REPORT.md). KB
                        possible_roles may CONDEMN (ambiguity-free) or CORROBORATE
                        (instance agreement) but must never unilaterally ASSERT —
                        see the CLAUDE.md KB-evidence rule.

    Returns a flat list of CoherenceViolation (FAIL or UNRESOLVABLE).
    """
    out: list[CoherenceViolation] = []
    for comp in netlist.components:
        if _is_passive(comp.refdes):
            continue
        for pin in comp.pins:
            if _UNCONNECTED_NET_RE.search(pin.net):
                continue
            net_role = _pdet.role_from_net_name(pin.net)
            if net_role not in signal_pair:
                continue
            # Source 1: role-bearing pin function. Routing-independent — a pin
            # literally named SDA is SDA regardless of the MCU's routing.
            pin_role = _pdet.role_from_pin_function(pin.pin_name)
            source = "pin_function"
            from_kb = False
            if pin_role not in signal_pair and kb_role_lookup is not None:
                # KB source — only when UNAMBIGUOUS: a pin whose possible_roles
                # include BOTH SDA and SCL (remappable) asserts neither, so it
                # cannot contradict a net name. Intersect with the pair and require
                # exactly one (also makes selection deterministic — no set order).
                kb_roles = (kb_role_lookup(comp.refdes, pin.pin_id) or set()) & signal_pair
                if len(kb_roles) == 1:
                    pin_role = next(iter(kb_roles))
                    source = "kb_possible_roles"
                    from_kb = True
            if pin_role not in signal_pair or pin_role == net_role:
                continue
            status = "FAIL"
            if from_kb and matrix_lookup is not None and matrix_lookup(comp.refdes):
                status = "UNRESOLVABLE"
            elif from_kb and kb_instance_lookup is not None:
                # R-B: KB instance vs net-asserted instance. Disagreement only —
                # a bare-token net or a KB entry with no instance stays admissible.
                net_instance = instance_from_net_name(pin.net)
                kb_instance = (kb_instance_lookup(comp.refdes, pin.pin_id) or {}).get(pin_role)
                if (net_instance is not None and kb_instance is not None
                        and net_instance.upper() != kb_instance.upper()):
                    status = "UNRESOLVABLE"
            out.append(CoherenceViolation(
                refdes=comp.refdes, pin_id=pin.pin_id, pin_function=pin.pin_name,
                net=pin.net, pin_role=pin_role.value, net_role=net_role.value,
                status=status, source=source))
    return out


# ── KB / routing lookup builders (consumed by the M6/M12 wirings) ─────────────

def kb_role_lookup_from(kb, canonicalize, netlist):
    """Build a ``(refdes, pin_id) -> set[Role]`` over the I2C-signal KB roles.

    The KB is keyed by the LOGICAL pin name (e.g. 'PB7'), but the IR pin_id is the
    pin NUMBER ('43'); the logical name lives on pin.pin_name (from the pinfunction).
    Resolve the name from the netlist and key by it (preferred), falling back to the
    raw pin_id only for fixtures whose pin_id IS the logical token — a numeric pin_id
    simply misses the name-keyed KB (clean miss, never a wrong-key hit).
    """
    ref2mpn = {c.refdes: c.mpn for c in netlist.components}
    name_by_ref_id = {
        (c.refdes, p.pin_id): p.pin_name
        for c in netlist.components for p in c.pins
    }

    def lookup(refdes, pin_id):
        mpn = ref2mpn.get(refdes)
        if not mpn:
            return set()
        keys = [k for k in (name_by_ref_id.get((refdes, pin_id)), pin_id) if k]
        for key in dict.fromkeys(keys):
            entry = kb.get((canonicalize(mpn), key)) or kb.get((mpn, key))
            if entry:
                return {_KB_SIGNAL_TO_ROLE[r.signal] for r in entry.roles
                        if r.signal in _KB_SIGNAL_TO_ROLE}
        return set()

    return lookup


def kb_role_instance_lookup_from(kb, canonicalize, netlist):
    """Build a ``(refdes, pin_id) -> {Role: instance|None}`` over the I2C-signal KB
    roles — the instance-preserving twin of ``kb_role_lookup_from`` (M6-F2, R-B).

    ``kb_role_lookup_from`` collapses to ``set[Role]`` and is consumed by 3 other
    generator/recall sites (netlist_swap.py, run_recall_harness.py,
    generate_bad_corpus.py) that must stay byte-identical — this is a PARALLEL
    lookup, not a modification, built the same way (name-keyed with numeric
    pin_id fallback) but keeping each role's ``PinRole.instance`` string.
    """
    ref2mpn = {c.refdes: c.mpn for c in netlist.components}
    name_by_ref_id = {
        (c.refdes, p.pin_id): p.pin_name
        for c in netlist.components for p in c.pins
    }

    def lookup(refdes, pin_id):
        mpn = ref2mpn.get(refdes)
        if not mpn:
            return {}
        keys = [k for k in (name_by_ref_id.get((refdes, pin_id)), pin_id) if k]
        for key in dict.fromkeys(keys):
            entry = kb.get((canonicalize(mpn), key)) or kb.get((mpn, key))
            if entry:
                return {_KB_SIGNAL_TO_ROLE[r.signal]: r.instance for r in entry.roles
                        if r.signal in _KB_SIGNAL_TO_ROLE}
        return {}

    return lookup


def matrix_lookup_from(routing, canonicalize, netlist, peripheral):
    """Build a ``(refdes) -> bool`` True when ``peripheral`` is matrix-routed."""
    ref2mpn = {c.refdes: c.mpn for c in netlist.components}

    def lookup(refdes):
        mpn = ref2mpn.get(refdes)
        if not mpn:
            return False
        r = (routing or {}).get(canonicalize(mpn)) or (routing or {}).get(mpn)
        return bool(r) and r.get(peripheral) == PeripheralRouting.MATRIX

    return lookup


def check_i2c_coherence(netlist, kb, routing, canonicalize) -> list[CoherenceViolation]:
    """M6 wiring: coherence over the I2C SDA↔SCL pair (pin-function + KB sources,
    ESP32-matrix coverage-gated)."""
    return find_coherence_violations(
        netlist, I2C_PAIR,
        kb_role_lookup=kb_role_lookup_from(kb, canonicalize, netlist),
        matrix_lookup=matrix_lookup_from(routing, canonicalize, netlist, Peripheral.I2C),
        kb_instance_lookup=kb_role_instance_lookup_from(kb, canonicalize, netlist),
    )


def check_spi_coherence(netlist, kb, canonicalize) -> list[CoherenceViolation]:
    """M12/M99 wiring: coherence over the SPI MOSI/MISO/SCK group.

    Pin-function-first, KB-fills-silence — the same evidence-priority rule
    ``find_coherence_violations`` already applies for I2C (Source 1: the
    MOSI/MISO/SCK regex plus the DI/DO/SDI/SDO/DIN/DOUT flash alias, both in
    ``peripheral_detectability.role_from_pin_function``; Source 2: KB
    ``possible_roles``, consulted ONLY when the pin function asserts no role of
    the group, and only when unambiguous — see the KB-evidence rule in
    CLAUDE.md). The DI/DO alias is *net-frame directional*: a peripheral's DI
    pin belongs on the MOSI net, its DO pin on the MISO net — so CORRECTLY-wired
    SPI is coherent (pin role == net role) and never flags; only a swap (a
    DI/MOSI-role pin landing on a MISO net, or a SCK-role pin landing on a
    MOSI/MISO-named net, or vice versa) contradicts.

    TODO-410 (STM32 SPI KB roles): ``peripheral_kb.Signal`` now carries
    SPI_MOSI/SPI_MISO/SPI_SCK/SPI_NSS and ``_KB_SIGNAL_TO_ROLE`` maps them, so a
    generic-named MCU pin (e.g. STM32 "PA6", no MOSI/MISO/SCK token) can now be
    condemned via its KB possible_roles exactly as I2C already does — this is
    what lights up the STM32 F1/F3/F4 MCU side of M12 (previously pin-function-
    only, catching only peripheral-IC pins with role-bearing names/aliases).

    No ``matrix_lookup``: the pin-function source is routing-independent (a pin
    literally named MOSI is MOSI regardless of the MCU's GPIO matrix), and no SPI
    KB currently carries ``PeripheralRouting.MATRIX`` (STM32 SPI is FIXED; ESP32's
    KB is SPI-signal-less) — there is no live matrix-routed SPI KB entry to gate.
    No ``kb_instance_lookup``: its net-side half, ``instance_from_net_name``, is
    I2C-only (matches only ``I2C\\d`` tokens) and would never fire on an SPI net
    name — passing it would be structurally inert, not a safety gate (D2 ruling).
    """
    return find_coherence_violations(
        netlist, SPI_COHERENCE_GROUP,
        kb_role_lookup=kb_role_lookup_from(kb, canonicalize, netlist),
    )

"""Cross-net bus pairing (Phase 1 pairing layer).

Recon basis: ``investigation/experiments/pin_capability_recon/REPORT.md`` (Q1/Q3).
The per-NET multi-member consensus machinery already exists and ships today
(``step_08d_peripheral_checker.check_i2c_peripheral``: PROTOCOL_MISMATCH /
INSTANCE_MISMATCH / ROLE_MISMATCH). What's missing is a layer that answers
"which two nets are the SDA net and the SCL net of the SAME bus" — today
``INSTANCE_MISMATCH`` only compares members *within one net*
(``tests/test_peripheral_checker_i2c.py:992``'s own acknowledged TODO). This
module builds exactly that cross-net pairing, and nothing else: it emits no
findings itself and does not change any existing checker's own classification
logic.

NOTE (M14-F1, ``investigation/experiments/m14_f1_recon/REPORT.md``, post
``e725e26``): despite the above, this module's output IS consumed by a
verdict-moving checker — ``step_08d_peripheral_checker``'s M14
CAPABILITY_MISMATCH emission calls ``pair_buses()`` directly for bus identity.
That consumer gates admissibility through ``has_independent_identity()``
below: a ``kb_instance``-only pairing (bus identity resting SOLELY on KB
alt-fn possibility, no independent corroboration) may never drive a
verdict-moving FAIL — only a ``name_prefix``-paired bus may. This module's
own pairing behavior is unchanged by that gate (both signals still run, both
still populate ``paired`` for diagnostics); it is the CONSUMER's decision
what may reach a verdict, not this module's.

Deliberately narrower than the general master/slave bus-topology model
(Notion Todo 49, NEEDS DESIGN): it only pairs two nets into one bus, using two
signals that already exist in the codebase, in priority order:

  1. KB instance (``peripheral_kb.PinRole.instance``) — if a KB-resolved
     member has a pin asserting one role of ``signal_pair`` (e.g. SDA) with
     instance "I2C1" on net A, and another (or the same) KB-resolved member
     asserts the other role (SCL) with instance "I2C1" on net B, then (A, B)
     is bus "I2C1". Reuses ``step_08d_peripheral_checker._resolve_pin`` — the
     already-tested KB lookup — rather than re-deriving pin resolution.
  2. Net-name prefix — two nets classified into the two different roles of
     ``signal_pair`` (via ``peripheral_detectability.role_from_net_name``,
     unmodified) pair when they share the same stem after their ROLE KEYWORD is
     excised (Phase-2b 2b-2: role-anchored, not last-token — so a trailing
     qualifier survives and ``/SFP/SCL_SFP``+``/SFP/SDA_SFP`` pair). Handles bare
     ``/SDA``+``/SCL`` (stem ``""``) and hierarchical ``/I2C_{SYS}.SDA``+
     ``/I2C_{SYS}.SCL`` (stem ``/I2C_{SYS}``), while keeping distinct instances
     apart (``I2C1_SDA`` stem ``I2C1_`` vs ``I2C2_SCL`` stem ``I2C2_``).
     ``classify_net_name`` already tolerates an instance prefix (``I2C\\d*[_/]``)
     in its match but discards it into an unstructured evidence string
     (``peripheral_roles.py:56-63``); this module captures the stem independently
     via local regexes rather than modifying that shipped, tested function.

Protocol-agnostic by construction: ``signal_pair`` is a frozenset of two
``peripheral_roles.Role`` values (``peripheral_coherence.I2C_PAIR`` today);
SPI/UART reuse is structural (same two-role-per-net shape) but NOT exercised
this phase — the KB has no SPI/UART signal source yet (recon Q2), so
``kb_signal_to_role=None`` for those protocols simply disables signal 1 and
falls back to signal 2 only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .step_08d_peripheral_checker import (
    _resolve_pin, _is_passive_refdes, _LookupStatus,
)
from .peripheral_roles import Role

try:
    import peripheral_detectability as _pdet
except ImportError:  # pragma: no cover - path bootstrap, mirrors peripheral_coherence.py
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    import peripheral_detectability as _pdet


# ── output types ────────────────────────────────────────────────────────────

@dataclass
class PairedBus:
    protocol:       str            # e.g. "I2C" — caller-supplied label, not derived
    bus_id:         str            # KB instance string ("I2C1") or the net-name stem
    pairing_source: str            # "kb_instance" | "name_prefix"
    net_a:          str            # net carrying signal_pair's first role (by Role.value order)
    net_b:          str            # net carrying signal_pair's second role
    members_a:      list = field(default_factory=list)   # non-passive refdes on net_a
    members_b:      list = field(default_factory=list)   # non-passive refdes on net_b


def has_independent_identity(bus: "PairedBus") -> bool:
    """True when ``bus``'s identity is confirmed by something OTHER than KB
    possibility — today that means net-name pairing (``pairing_source ==
    "name_prefix"``).

    A ``kb_instance``-paired bus rests ENTIRELY on the KB's alt-fn possibility
    map (what a pin COULD be), with no independent check that the design
    actually uses it that way — the exact evidence-class misuse the M14-F1
    recon (``investigation/experiments/m14_f1_recon/REPORT.md``) found: a
    genuine STM32 UART1-on-PB6/PB7 design pairs as bus "I2C1" purely because
    those same pins ALSO carry I2C1 in the vendor alt-fn table.

    Possibility may PAIR a bus for diagnostics and may CONDEMN a member as
    incapable (a KB possible_roles list excluding a role is a hard silicon
    fact) — but it may never, by itself, assert bus IDENTITY into a
    verdict-moving finding. Any consumer that turns a paired bus into a
    verdict-moving FAIL (e.g. step_08d's M14 CAPABILITY_MISMATCH) MUST gate on
    this. Report-only/diagnostic consumers (``peripheral_consensus``'s own
    classifications) are unaffected — this governs verdict admissibility only,
    not pairing behavior itself.
    """
    return bus.pairing_source == "name_prefix"


@dataclass
class UnpairedNet:
    protocol: str
    net:      str
    role:     str          # Role.value of the net's name-implied role
    members:  list = field(default_factory=list)


# Phase-2b 2b-2: pairing_source of a report-only bucket for a KB instance whose
# candidate nets are AMBIGUOUS — a repeating fixed-mux family (RP2040) asserts the
# same (instance, role) on multiple wired nets, so >1 candidate net for a role
# means no unique bus. Such entries are appended to ``paired`` for visibility but
# carry candidate nets in a ';'-joined ``net_a``/``net_b`` and MUST be skipped by
# any consumer computing a verdict (they are not a resolved bus). Their nets are
# NOT consumed, so a cleanly NET-NAME'd real bus among them is still recovered by
# signal-2 (name-prefix) pairing.
AMBIGUOUS_PAIRING = "ambiguous_pairing"


# KiCad synthetic net name for a floating (unconnected) pin, e.g.
# "unconnected-(U1-GPIO12-Pad13)". Same token step_08c_structural_checker.py's
# SYNTHETIC_UNCONNECTED_RE and step_06_power.py already recognize (duplicated,
# not imported, per this codebase's own source-independence convention for
# small net-name recognizers — see step_08c's drift note). A floating pin is
# never a real bus member; without this filter, a repeating fixed-mux family
# (RP2040: I2C0 SDA is KB-capable on 8 different physical GPIOs, only one of
# which is ever actually wired) cross-products every unused candidate pin's
# synthetic net into a phantom paired bus — caught empirically on the RP2040
# stamp_and_module.net fixture during Phase-1 validation (81 spurious pairs
# from a 2-component board before this filter).
_UNCONNECTED_NET_RE = re.compile(r'unconnected[-_(]')


# ── name-prefix stem extraction ───────────────────────────────────────────────
# Legacy fallback: strip the LAST '_'/'/'-delimited alnum token. Used for roles
# with no keyword recognizer here (SPI/UART — not exercised this phase), unchanged.
_TRAILING_TOKEN_RE = re.compile(r'[_/]?([A-Za-z0-9]+)$')

# Phase-2b 2b-2 fix: role-ANCHORED stem. The legacy last-token strip drops the
# TRAILING QUALIFIER (`/SFP/SCL_SFP` -> `/SFP/SCL`) instead of the role token, so a
# qualified SDA/SCL pair never shares a stem and never pairs (the jetson SFP bus
# J12+U73 was missed for exactly this reason — Phase-1 report). Anchoring the cut
# to the role KEYWORD instead — excise `SDA`/`SCL` wherever classify_net_name found
# it and keep BOTH the instance prefix and the trailing qualifier — makes
# `/SFP/SCL_SFP` and `/SFP/SDA_SFP` share a stem while keeping distinct instances
# apart (`I2C1_SDA` -> `I2C1_` vs `I2C2_SCL` -> `I2C2_`, still unequal). The keyword
# REs mirror the role tokens in peripheral_roles.NET_NAME_PATTERNS (source-
# independent by the same convention as _UNCONNECTED_NET_RE); a role absent here
# falls back to the legacy strip, so SPI/UART behavior is byte-identical.
_ROLE_STEM_KEYWORD = {
    Role.I2C_DATA:  re.compile(r'SDA', re.I),
    Role.I2C_CLOCK: re.compile(r'SCL', re.I),
}
_COLLAPSE_SEP_RE = re.compile(r'[_/]{2,}')


def _stem(net_name: str, role=None) -> str:
    normalised = net_name.replace('.', '_').replace(' ', '_')
    kw = _ROLE_STEM_KEYWORD.get(role)
    if kw is not None:
        m = kw.search(normalised)
        if m:
            residue = normalised[:m.start()] + normalised[m.end():]
            # collapse the separator run left where the role token was excised, so a
            # mid-string role (`/SFP/SCL_SFP`) aligns with its complement; rstrip
            # (not full strip) preserves a leading '/' so `/I2C_{SYS}.SDA` keeps its
            # bus_id, while bare `/SCL` -> '' -> "(bare)".
            residue = _COLLAPSE_SEP_RE.sub('_', residue).rstrip('_/')
            return residue
    m = _TRAILING_TOKEN_RE.search(normalised)
    if not m:
        return normalised
    return normalised[:m.start()].rstrip('_/')


def _non_passive_members(net, comps_by_refdes) -> list:
    out = []
    for refdes, _pin_id in net.pins:
        comp = comps_by_refdes.get(refdes)
        if comp is None or _is_passive_refdes(refdes):
            continue
        if refdes not in out:
            out.append(refdes)
    return out


# ── the pairing function ──────────────────────────────────────────────────────

def pair_buses(
    netlist,
    protocol: str,
    signal_pair,
    *,
    kb: dict | None = None,
    peripheral_routing: dict | None = None,
    kb_signal_to_role: dict | None = None,
) -> tuple[list[PairedBus], list[UnpairedNet]]:
    """Pair nets of ``netlist`` into buses over ``signal_pair``.

    Args:
        netlist:            parsed IR with ``.components`` and ``.nets``.
        protocol:            caller label for the returned objects (e.g. "I2C").
        signal_pair:          frozenset of exactly two ``peripheral_roles.Role``.
        kb, peripheral_routing: KB inputs for signal-1 (KB instance) pairing;
                              omit (None) to run signal 2 (name-prefix) only.
        kb_signal_to_role:    optional ``dict[peripheral_kb.Signal, Role]``
                              bridging KB roles into ``signal_pair``'s Role
                              vocabulary (``peripheral_coherence._KB_SIGNAL_TO_ROLE``
                              for I2C). None disables signal-1 pairing — correct
                              for protocols the KB has no signal source for.

    Returns:
        ``(paired, unpaired)``. A net can appear in at most one ``PairedBus``;
        every I2C-role-implying net that isn't consumed by a pairing appears in
        ``unpaired`` instead (visibility, not an error). ``paired`` may also carry
        report-only ``AMBIGUOUS_PAIRING`` entries (Phase-2b 2b-2) — a KB instance
        whose candidate nets don't resolve to a unique bus; these are NOT resolved
        buses and a consumer computing a verdict must skip
        ``pairing_source == AMBIGUOUS_PAIRING``.
    """
    if len(signal_pair) != 2:
        raise ValueError("signal_pair must have exactly two roles")
    role_a, role_b = sorted(signal_pair, key=lambda r: r.value)

    comps_by_refdes = {c.refdes: c for c in netlist.components}
    paired: list[PairedBus] = []
    consumed_nets: set[str] = set()

    # ── signal 1: KB instance ──────────────────────────────────────────────
    if kb and kb_signal_to_role:
        # instance -> {role: set(net_name)}
        instance_nets: dict[str, dict] = {}
        pinname_by_ref_id = {
            (c.refdes, p.pin_id): p.pin_name
            for c in netlist.components for p in c.pins
        }
        for comp in netlist.components:
            if _is_passive_refdes(comp.refdes):
                continue
            for pin in comp.pins:
                if _UNCONNECTED_NET_RE.search(pin.net):
                    continue
                status, entry = _resolve_pin(
                    comp.effective_mpn, pin.pin_id, kb, peripheral_routing,
                    pinname_by_ref_id.get((comp.refdes, pin.pin_id)),
                )
                if status != _LookupStatus.OK or entry is None:
                    continue
                for r in entry.roles:
                    role = kb_signal_to_role.get(r.signal)
                    if role not in signal_pair or r.instance is None:
                        continue
                    bucket = instance_nets.setdefault(r.instance, {role_a: set(), role_b: set()})
                    bucket[role].add(pin.net)

        for instance, roles in sorted(instance_nets.items()):
            nets_a = sorted(roles[role_a])
            nets_b = sorted(roles[role_b])
            # Ambiguity gate (Phase-2b 2b-2): a uniquely-resolvable bus has exactly
            # ONE candidate net per role. When a role maps to >1 net (RP2040 mux: the
            # same I2C instance is capable on several wired GPIOs), the full cross
            # product mints phantom buses — bucket the instance AMBIGUOUS_PAIRING
            # (report-only) and do NOT consume its nets, so a cleanly-named real bus
            # among them (/SCL+/SDA) is still recovered by signal-2 below.
            if len(nets_a) > 1 or len(nets_b) > 1:
                cand = nets_a + nets_b
                members = sorted({
                    m for n in cand
                    for obj in [next((x for x in netlist.nets if x.name == n), None)]
                    if obj is not None
                    for m in _non_passive_members(obj, comps_by_refdes)
                })
                paired.append(PairedBus(
                    protocol=protocol, bus_id=instance, pairing_source=AMBIGUOUS_PAIRING,
                    net_a=";".join(nets_a), net_b=";".join(nets_b),
                    members_a=members, members_b=[],
                ))
                continue
            for net_a in nets_a:
                for net_b in nets_b:
                    net_a_obj = next((n for n in netlist.nets if n.name == net_a), None)
                    net_b_obj = next((n for n in netlist.nets if n.name == net_b), None)
                    if net_a_obj is None or net_b_obj is None:
                        continue
                    paired.append(PairedBus(
                        protocol=protocol, bus_id=instance, pairing_source="kb_instance",
                        net_a=net_a, net_b=net_b,
                        members_a=_non_passive_members(net_a_obj, comps_by_refdes),
                        members_b=_non_passive_members(net_b_obj, comps_by_refdes),
                    ))
                    consumed_nets.add(net_a)
                    consumed_nets.add(net_b)

    # ── signal 2: net-name prefix (over nets not already kb-instance-paired) ──
    nets_by_role_stem: dict = {role_a: {}, role_b: {}}   # stem -> net_name (first wins)
    role_of_net: dict[str, object] = {}
    for net in netlist.nets:
        if net.name in consumed_nets:
            continue
        if _UNCONNECTED_NET_RE.search(net.name):
            continue
        net_role = _pdet.role_from_net_name(net.name)
        if net_role not in signal_pair:
            continue
        role_of_net[net.name] = net_role
        stem = _stem(net.name, net_role)
        nets_by_role_stem[net_role].setdefault(stem, []).append(net.name)

    stems_a = nets_by_role_stem[role_a]
    stems_b = nets_by_role_stem[role_b]
    for stem in sorted(set(stems_a) & set(stems_b)):
        for net_a in stems_a[stem]:
            for net_b in stems_b[stem]:
                net_a_obj = next(n for n in netlist.nets if n.name == net_a)
                net_b_obj = next(n for n in netlist.nets if n.name == net_b)
                paired.append(PairedBus(
                    protocol=protocol, bus_id=(stem or "(bare)"), pairing_source="name_prefix",
                    net_a=net_a, net_b=net_b,
                    members_a=_non_passive_members(net_a_obj, comps_by_refdes),
                    members_b=_non_passive_members(net_b_obj, comps_by_refdes),
                ))
                consumed_nets.add(net_a)
                consumed_nets.add(net_b)

    # ── unpaired ────────────────────────────────────────────────────────────
    unpaired: list[UnpairedNet] = []
    for net in netlist.nets:
        if net.name in consumed_nets:
            continue
        net_role = role_of_net.get(net.name)
        if net_role is None:
            net_role = _pdet.role_from_net_name(net.name)
        if net_role not in signal_pair:
            continue
        unpaired.append(UnpairedNet(
            protocol=protocol, net=net.name, role=net_role.value,
            members=_non_passive_members(net, comps_by_refdes),
        ))

    # Note: the only production call site discards this (step_08d_peripheral_checker.py:793 does `_paired, _ = pair_buses(...)`).
    return paired, unpaired

"""Unified I2C bus consensus evaluation — Phase 1.

Given a bus paired by ``peripheral_bus_pairing.pair_buses``, every non-passive
member on either net asserts a role (SDA/SCL, + instance where available) from
whichever source resolves: KB ``possible_roles``/``instance`` where the MPN is
KB'd, else the member's own literal pin-function name (recon
``pin_capability_recon/REPORT.md`` Q4/Q5 — the jetson-board evidence showed
non-KB slave ICs still carry real pin_function role labels, so majority
consensus is not KB-coverage-gated). The majority role/instance among
identified voters becomes the bus's consensus; a minority or KB-incapable
member is the defect signal.

This module computes and CLASSIFIES that signal only — it emits no
``CoherenceViolation`` / ``PeripheralFinding`` itself.

CORRECTION (M14-F1, ``investigation/experiments/m14_f1_recon/REPORT.md``, post
``e725e26``): this module IS imported by ``step_08d_peripheral_checker.py``
and IS live in a verdict path — the ``fail_mode == "capability"`` output of
``evaluate_bus_consensus`` drives step_08d's M14 CAPABILITY_MISMATCH FAIL
(gated additionally, since M14-F1, through
``peripheral_bus_pairing.has_independent_identity`` on the underlying bus).
The claim below that this module is diagnostic-only was accurate for Phase 1
but has been false since the Phase-3 M14 wiring; it is preserved narrowly and
correctly for ONE thing only — the ``fail_mode == "consensus"`` classification
(cross-device minority) remains report-only: step_08d's M14 block filters to
``fail_mode == "capability"`` exclusively and never consumes a "consensus"
result for a verdict.

Boundary rules (recon Q5 + Phase-2b spec correction), applied as REPORT-ONLY
classifications. The verdict gating is SPLIT into two independent paths (Phase-2b
STEP 2b-1) because a capability defect and a consensus defect answer different
questions and need different sufficiency thresholds:

  - CAPABILITY path — a member whose KB possible_roles EXCLUDE this protocol
    entirely, sitting on a bus that at least one voter corroborates IS this
    protocol -> that member is INCAPABLE, bus WOULD_FAIL. This is a HARD KB fact
    (the pin physically cannot serve the role), so it fires on a single KB'd
    device and is NOT gated by the >=2-device consensus rule — it MUST fire on a
    single-MCU board (the primary Phase-2/3 target: SCL routed to a pin the MCU
    can't do I2C on). The lone corroborating voter may be the SAME device (an MCU
    whose SDA pin is correct but whose SCL landed on an incapable pin).
  - CONSENSUS path — a member whose asserted role is the MINORITY vs the majority
    of the OTHER distinct devices -> MINORITY_CONTRADICTS, bus WOULD_FAIL. A
    majority needs corroboration ACROSS devices, so this path requires >=2
    DISTINCT REFDES among the voters (voter-by-refdes, Phase-2b: a lone device's
    own complementary SDA+SCL pins are ONE voter, not two — self-corroboration is
    not consensus).

Stand-down / boundary classifications (both paths):
  - fewer than 2 DISTINCT voter devices AND no corroborated capability defect
                                              -> CONSENSUS_INSUFFICIENT (stand down)
  - matrix/free-mux member (no fixed pin capability, e.g. ESP32 I2C)
                                              -> UNCONSTRAINED, never INCAPABLE
  - KB member whose possible_roles include BOTH roles of the pair (remappable)
                                              -> ASSERTS_NEITHER, not a voter
                                                 (mirrors peripheral_coherence's
                                                 ``len(kb_roles) == 1`` gate)
  - a 1-1 instance-vote tie (consensus path)  -> TIE_UNRESOLVABLE, no minority picked

``BusConsensus.fail_mode`` records WHICH path fired ("capability" | "consensus" |
None) for the Phase-2 recall table and the M6 de-dupe surface.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum

from .peripheral_bus_pairing import PairedBus
from .step_08d_peripheral_checker import (
    _resolve_pin, _is_passive_refdes, _LookupStatus, canonicalize_mpn_for_kb,
)
from .peripheral_coherence import check_i2c_coherence

try:
    import peripheral_detectability as _pdet
except ImportError:  # pragma: no cover - path bootstrap, mirrors peripheral_coherence.py
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    import peripheral_detectability as _pdet


class MemberClass(Enum):
    AGREES               = "AGREES"
    MINORITY_CONTRADICTS = "MINORITY_CONTRADICTS"
    INCAPABLE            = "INCAPABLE"
    SILENT                = "SILENT"
    UNCONSTRAINED         = "UNCONSTRAINED"
    ASSERTS_NEITHER       = "ASSERTS_NEITHER"


class BusVerdict(Enum):
    WOULD_FAIL             = "WOULD_FAIL"
    CLEAN                  = "CLEAN"
    CONSENSUS_INSUFFICIENT = "CONSENSUS_INSUFFICIENT"
    TIE_UNRESOLVABLE       = "TIE_UNRESOLVABLE"


@dataclass
class MemberAssertion:
    refdes:              str
    pin_id:               str
    net:                  str
    role_source:          str            # "kb" | "pinfunc" | "none"
    asserted_role:         object         # peripheral_roles.Role | None
    asserted_instance:     str | None
    classification:        str            # MemberClass.value
    caught_by_m6:           bool | None = None   # only set for flagged members
    # Internal: True when the KB says this pin's possible_roles exclude BOTH
    # roles of the pair (candidate for INCAPABLE). Held pending, not promoted to
    # `classification` until the bus is known to have enough voters to trust a
    # verdict — an "incapable" claim needs a real consensus to be incapable
    # RELATIVE TO, same as MINORITY_CONTRADICTS does. Exposed (not name-mangled)
    # so the diagnostic can explain a CONSENSUS_INSUFFICIENT bus that still had
    # an incapable-looking member.
    incapable_candidate:   bool = False


@dataclass
class BusConsensus:
    bus:               PairedBus
    verdict:           str
    majority_role_a:    object          # Role | None — consensus role on bus.net_a
    majority_role_b:    object
    majority_instance:  str | None
    members:           list = field(default_factory=list)   # list[MemberAssertion]
    # WHICH verdict path produced a WOULD_FAIL: "capability" (a KB-incapable member,
    # single-device-sufficient) | "consensus" (a cross-device minority, >=2 distinct
    # devices) | None (not a WOULD_FAIL). Phase-2b STEP 2b-1; feeds the recall table
    # and the M6 de-dupe surface.
    fail_mode:         str | None = None
    # Count of DISTINCT voter refdes on the bus (voter-by-refdes). Exposed so the
    # diagnostic can show why a 2-pin single-device bus is CONSENSUS_INSUFFICIENT.
    distinct_voter_devices: int = 0


def _majority(votes):
    """Plurality vote. Returns (winner, is_tie). Empty/all-None input -> (None, False)
    — no votes is not a tie, it's an absence of evidence."""
    votes = [v for v in votes if v is not None]
    if not votes:
        return None, False
    counts = Counter(votes)
    top = max(counts.values())
    winners = [v for v, c in counts.items() if c == top]
    if len(winners) > 1:
        return None, True
    return winners[0], False


def m6_flagged_pins(netlist, kb, peripheral_routing) -> set:
    """(refdes, pin_id) pairs the SHIPPED per-pin M6 coherence primitive already
    flags on this netlist. Read-only call into ``check_i2c_coherence`` — used only
    to annotate Phase-1 findings with "would this already be caught today", never
    to alter or re-derive the primitive's own behavior."""
    violations = check_i2c_coherence(netlist, kb, peripheral_routing, canonicalize_mpn_for_kb)
    return {(v.refdes, v.pin_id) for v in violations}


def _member_assertions(netlist, net_name, signal_pair, kb, peripheral_routing,
                        kb_signal_to_role, comps_by_refdes, pinname_by_ref_id, nets_by_name):
    net = nets_by_name.get(net_name)
    out = []
    if net is None:
        return out
    for refdes, pin_id in net.pins:
        comp = comps_by_refdes.get(refdes)
        if comp is None or _is_passive_refdes(refdes):
            continue

        role_source = "none"
        asserted_role = None
        asserted_instance = None
        classification = None   # filled below; None means "resolve after majority"
        incapable_candidate = False

        status, entry = _LookupStatus.KB_MISSING, None
        if kb:
            status, entry = _resolve_pin(
                comp.effective_mpn, pin_id, kb, peripheral_routing,
                pinname_by_ref_id.get((refdes, pin_id)),
            )

        if status == _LookupStatus.PERIPHERAL_UNCONSTRAINED:
            role_source = "kb"
            classification = MemberClass.UNCONSTRAINED.value
        elif status == _LookupStatus.OK and entry is not None and entry.strap is not None:
            # Todo 240 strap exemption (STRUCTURAL — invariant, not behavior):
            # a KB pin explicitly marked as a deliberately non-participating
            # strap/config pin (INA219 A0/A1 address-select strapped onto
            # SDA/SCL) is classified SILENT UNCONDITIONALLY — before its role
            # content is ever inspected. This guarantees, independent of how the
            # KB entry is authored, that a strap pin (a) is NEVER an
            # incapable_candidate → never drives an M14 CAPABILITY_MISMATCH FAIL
            # (gate b), and (b) carries no asserted_role → is structurally NEVER
            # a voter, so it contributes NOTHING to any CONFIRM/corroboration
            # path. role_source stays "none" (it asserts nothing).
            classification = MemberClass.SILENT.value
        elif status == _LookupStatus.OK and entry is not None:
            role_source = "kb"
            relevant = [
                r for r in entry.roles
                if (kb_signal_to_role or {}).get(r.signal) in signal_pair
            ]
            if len(relevant) == 0:
                incapable_candidate = True   # resolved once bus voter-sufficiency is known
            elif len(relevant) == 1:
                asserted_role = kb_signal_to_role[relevant[0].signal]
                asserted_instance = relevant[0].instance
                # classification resolved once bus majority is known
            else:
                classification = MemberClass.ASSERTS_NEITHER.value
        else:
            pin = next((p for p in comp.pins if p.pin_id == pin_id), None)
            pin_role = _pdet.role_from_pin_function(pin.pin_name) if pin else None
            if pin_role in signal_pair:
                role_source = "pinfunc"
                asserted_role = pin_role
            else:
                classification = MemberClass.SILENT.value

        out.append(MemberAssertion(
            refdes=refdes, pin_id=pin_id, net=net_name, role_source=role_source,
            asserted_role=asserted_role, asserted_instance=asserted_instance,
            classification=classification, incapable_candidate=incapable_candidate,
        ))
    return out


def evaluate_bus_consensus(
    netlist,
    bus: PairedBus,
    signal_pair,
    *,
    kb: dict | None = None,
    peripheral_routing: dict | None = None,
    kb_signal_to_role: dict | None = None,
    m6_flagged: set | None = None,
) -> BusConsensus:
    """Evaluate unified consensus over one paired bus. REPORT-ONLY: returns a
    classification, emits nothing into any verdict/report path."""
    comps_by_refdes = {c.refdes: c for c in netlist.components}
    nets_by_name = {n.name: n for n in netlist.nets}
    pinname_by_ref_id = {
        (c.refdes, p.pin_id): p.pin_name
        for c in netlist.components for p in c.pins
    }

    members = (
        _member_assertions(netlist, bus.net_a, signal_pair, kb, peripheral_routing,
                            kb_signal_to_role, comps_by_refdes, pinname_by_ref_id, nets_by_name)
        + _member_assertions(netlist, bus.net_b, signal_pair, kb, peripheral_routing,
                              kb_signal_to_role, comps_by_refdes, pinname_by_ref_id, nets_by_name)
    )
    voters = [m for m in members if m.asserted_role is not None]
    distinct_devices = {m.refdes for m in voters}         # voter-by-refdes (Phase-2b)
    n_distinct = len(distinct_devices)
    has_incapable = any(m.incapable_candidate for m in members)

    def _finish(verdict, majority_role_a=None, majority_role_b=None, majority_instance=None,
                promote_incapable=False, fail_mode=None):
        for m in members:
            if m.incapable_candidate:
                m.classification = (MemberClass.INCAPABLE.value if promote_incapable
                                     else MemberClass.AGREES.value)
            elif m.classification is None:
                m.classification = MemberClass.AGREES.value
        for m in members:
            if m.classification in (MemberClass.MINORITY_CONTRADICTS.value, MemberClass.INCAPABLE.value):
                m.caught_by_m6 = (m.refdes, m.pin_id) in m6_flagged if m6_flagged is not None else None
        return BusConsensus(bus=bus, verdict=verdict, majority_role_a=majority_role_a,
                             majority_role_b=majority_role_b, majority_instance=majority_instance,
                             members=members, fail_mode=fail_mode, distinct_voter_devices=n_distinct)

    # Per-net role majority + instance vote (needed by both paths for context /
    # minority classification; a net's own voters decide that net's majority role).
    def _net_majority(net_name):
        return _majority([m.asserted_role for m in voters if m.net == net_name])

    majority_role_a, tie_a = _net_majority(bus.net_a)
    majority_role_b, tie_b = _net_majority(bus.net_b)
    instance_votes = [m.asserted_instance for m in voters if m.asserted_instance is not None]
    majority_instance, instance_tie = _majority(instance_votes)

    # ── CAPABILITY PATH (Phase-2b 2b-1) ──────────────────────────────────────
    # A KB-incapable member on a bus corroborated as this-protocol by >=1 voter is
    # a HARD KB fact — the pin cannot physically serve the role. Fires on a single
    # KB'd device (the corroborating voter may be that same device); NOT gated by
    # the >=2-distinct-device consensus rule. Precision rests on the corroboration
    # requirement (>=1 voter positively asserting this protocol on the bus) + the
    # pairing layer's net-name/KB-instance gating — verified 0 non-jetson WOULD_FAIL
    # across the 125-board precision corpus (Phase-2b 2b-5).
    if has_incapable and len(voters) >= 1:
        return _finish(BusVerdict.WOULD_FAIL.value, majority_role_a=majority_role_a,
                       majority_role_b=majority_role_b, majority_instance=majority_instance,
                       promote_incapable=True, fail_mode="capability")

    # ── CONSENSUS PATH ────────────────────────────────────────────────────────
    # Boundary rule: <2 DISTINCT voter devices -> stand down. A minority claim needs
    # a cross-device majority to contradict; one device's own two pins are not a
    # consensus. Any incapable_candidate here is UNcorroborated (no voter) -> it is
    # NOT promoted (folds to AGREES in _finish).
    if n_distinct < 2:
        return _finish(BusVerdict.CONSENSUS_INSUFFICIENT.value)

    # Boundary rule: 1-1 instance tie -> stand down, no minority picked.
    if instance_tie:
        return _finish(BusVerdict.TIE_UNRESOLVABLE.value, majority_instance=None)

    # Cross-device role consensus, per net. A role tie on one net degrades to "no
    # minority picked on that net"; the other net's clear majority still governs.
    any_flag = False
    for m in members:
        if m.classification is not None or m.incapable_candidate:
            continue
        net_majority = majority_role_a if m.net == bus.net_a else majority_role_b
        net_tie = tie_a if m.net == bus.net_a else tie_b
        if net_tie or net_majority is None or m.asserted_role == net_majority:
            m.classification = MemberClass.AGREES.value
        else:
            m.classification = MemberClass.MINORITY_CONTRADICTS.value
            any_flag = True

    verdict = BusVerdict.WOULD_FAIL.value if any_flag else BusVerdict.CLEAN.value
    return _finish(verdict, majority_role_a=majority_role_a, majority_role_b=majority_role_b,
                   majority_instance=majority_instance,
                   fail_mode="consensus" if any_flag else None)

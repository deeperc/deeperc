"""M2 output-conflict driver classifier + step_08f VERDICT-MOVING checker.

Classifies each net for a potential "≥2 push-pull drivers" output-to-output
conflict. Since Phase 2 (M2 v1, 2026-07-10) `check_output_conflicts` is wired
LIVE as the `08f` registry checker (VERDICT_MOVING, FAIL-only). The classifier
core (`classify_netlist`/`classify_net`) is unchanged from Phases 1/1.5 — the
Phase-2 wiring is a thin FAIL-only emit wrapper over it. See
investigation/experiments/m2_output_conflict_recon/REPORT.md for the recon
that measured the design's FP surface (0/8157 good-corpus nets), and
phase1_diagnostic/REPORT_v2.md for the report-only diagnostic (negative
control 0 WOULD_FAIL over 125 good boards; positive control 5/5 cross-component
push-pull HITs) that preceded this wiring.

**v1 scope (locked):** CROSS-component only (≥2 DISTINCT refdes — same-component
is v2) and FAIL-only emission (CLEAN / UNRESOLVABLE / any SKIPPED_* → NO
finding). WOULD_FAIL already guarantees ≥2 distinct-refdes drivers (the
open-drain veto + bidir/tri-state/power-ground skips run first, and drivers are
keyed by refdes), so filtering to WOULD_FAIL IS the v1 cross-component set.

Design (from the recon, not re-derived here):
  - candidate drivers = pins whose KiCad-native `pintype` is `output` or
    `power_out` (the primary, ~100%-coverage signal — NetlistIR.pintypes).
  - VETO a candidate whose (refdes, pin_name) has a `pin_specs` entry with
    `open_drain=True` (the datasheet-cache override step_08's driver-trust
    gate already uses, Todo 106 / cfce679) — an open-drain output sinks only,
    it is not a second push-pull driver.
  - DEDUPE candidates by refdes — N pins of one component bonded on one net
    (e.g. multi-phase SW nodes) is one driver, not N.
  - SKIP the whole net (no finding) if ANY pin on it is `bidirectional`,
    `tri_state`, `open_collector`, or `open_emitter` — these are legitimate
    multi-drop/shared-bus topologies that cannot be judged from static
    topology alone.
  - SKIP power/ground nets (reuse step_06's POWER_NET_RE/GROUND_NET_RE, plus
    ir.power_nets/ir.ground_nets membership).
  - WOULD_FAIL when ≥2 distinct-refdes candidate drivers remain after veto
    and dedupe; else CLEAN.
  - UNRESOLVABLE when a pin's pintype is missing/`unspecified` and no
    pin_specs entry arbitrates it, AND that ambiguity could change the
    verdict (i.e. resolving it either way would cross the ≥2 threshold).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from steps.step_02_parser import NetlistIR, NetIR
from steps.step_06_power import POWER_NET_RE, GROUND_NET_RE

PUSHPULL_PINTYPES = {"output", "power_out", "power_output"}
MULTIDRIVE_EXCLUDE_PINTYPES = {"bidirectional", "tri_state", "open_collector", "open_emitter"}
AMBIGUOUS_PINTYPES = {None, "", "unspecified"}

# step_05_validator.ValidatedPinSpec.pin_type values that arbitrate an
# ambiguous KiCad pintype as a genuine push-pull driver when the datasheet
# cache has an entry for the pin (mirrors PUSHPULL_PINTYPES for the
# pin_specs.pin_type vocabulary, which step_04b/step_05 populate separately).
PUSHPULL_SPEC_TYPES = {"output", "digital_output", "power_output"}

CLASSIFICATION_WOULD_FAIL = "WOULD_FAIL"
CLASSIFICATION_CLEAN = "CLEAN"
CLASSIFICATION_UNRESOLVABLE = "UNRESOLVABLE"
CLASSIFICATION_SKIPPED_POWER_GROUND = "SKIPPED_POWER_GROUND"
CLASSIFICATION_SKIPPED_BIDIR_TRISTATE = "SKIPPED_BIDIR_TRISTATE"

# Evidence-only exclusion-reason tags (independent of `classification`; explain
# WHY a net that had ≥2 raw output-typed pins did not end up WOULD_FAIL).
REASON_NO_MULTIDRIVER_CANDIDATE = "no_multidriver_candidate"   # <2 raw output pins to begin with
REASON_SAME_REFDES_DEDUP = "same_refdes_dedup"                 # raw >=2 pins, but 1 distinct refdes
REASON_OPEN_DRAIN_VETO = "open_drain_veto"                     # >=2 distinct refdes raw, vetoed to <2
REASON_BIDIR_TRISTATE_PRESENT = "bidir_tristate_present"
REASON_POWER_GROUND = "power_ground"
REASON_GENUINE_MULTIDRIVER = "genuine_pushpull_multidriver"    # the headline WOULD_FAIL case
REASON_UNRESOLVABLE_AMBIGUOUS = "unresolvable_ambiguous_pintype"


@dataclass
class M2PinEvidence:
    refdes: str
    pin_id: str
    pin_name: str
    pintype: str | None
    open_drain_veto: bool = False


@dataclass
class M2NetResult:
    net_name: str
    classification: str
    reason: str
    drivers: list[M2PinEvidence] = field(default_factory=list)      # final counted drivers (post veto/dedupe)
    raw_output_pins: list[M2PinEvidence] = field(default_factory=list)  # all pintype in PUSHPULL_PINTYPES, pre-veto/dedupe
    vetoed_pins: list[M2PinEvidence] = field(default_factory=list)
    ambiguous_pins: list[M2PinEvidence] = field(default_factory=list)


def _is_power_or_ground_net(net_name: str, power_nets: list[str], ground_nets: list[str]) -> bool:
    if net_name in power_nets or net_name in ground_nets:
        return True
    return bool(POWER_NET_RE.match(net_name) or GROUND_NET_RE.match(net_name))


def classify_net(
    net: NetIR,
    comp_refdes_set: set[str],
    pin_name_map: dict[tuple[str, str], str],
    pintypes: dict[tuple[str, str], str],
    pin_specs: dict,
    power_nets: list[str],
    ground_nets: list[str],
) -> M2NetResult:
    """Classify a single net for the M2 ≥2-push-pull-driver output conflict."""
    if _is_power_or_ground_net(net.name, power_nets, ground_nets):
        return M2NetResult(
            net_name=net.name,
            classification=CLASSIFICATION_SKIPPED_POWER_GROUND,
            reason=REASON_POWER_GROUND,
        )

    pins: list[M2PinEvidence] = []
    for refdes, pin_id in net.pins:
        if refdes not in comp_refdes_set:
            continue
        pin_name = pin_name_map.get((refdes, pin_id), "")
        pintype = pintypes.get((refdes, pin_id))
        pins.append(M2PinEvidence(refdes=refdes, pin_id=pin_id, pin_name=pin_name, pintype=pintype))

    if any(p.pintype in MULTIDRIVE_EXCLUDE_PINTYPES for p in pins):
        return M2NetResult(
            net_name=net.name,
            classification=CLASSIFICATION_SKIPPED_BIDIR_TRISTATE,
            reason=REASON_BIDIR_TRISTATE_PRESENT,
            raw_output_pins=[p for p in pins if p.pintype in PUSHPULL_PINTYPES],
        )

    raw_output_pins = [p for p in pins if p.pintype in PUSHPULL_PINTYPES]
    raw_distinct_refdes = {p.refdes for p in raw_output_pins}

    vetoed: list[M2PinEvidence] = []
    kept: dict[str, list[M2PinEvidence]] = {}
    for p in raw_output_pins:
        spec = pin_specs.get((p.refdes, p.pin_name))
        if spec is not None and getattr(spec, "open_drain", False):
            p.open_drain_veto = True
            vetoed.append(p)
        else:
            kept.setdefault(p.refdes, []).append(p)

    definite_drivers: dict[str, M2PinEvidence] = {refdes: ps[0] for refdes, ps in kept.items()}

    ambiguous_pins = [p for p in pins if p.pintype in AMBIGUOUS_PINTYPES]
    unresolved_ambiguous_refdes: set[str] = set()
    for p in ambiguous_pins:
        spec = pin_specs.get((p.refdes, p.pin_name))
        if spec is None:
            unresolved_ambiguous_refdes.add(p.refdes)
            continue
        # Arbitrate via the datasheet-cache pin_type when available.
        if spec.pin_type in PUSHPULL_SPEC_TYPES and not getattr(spec, "open_drain", False):
            definite_drivers.setdefault(p.refdes, p)
        # else: arbitrated as a non-driver (input/passive/etc.) — resolved, ignore.

    definite_count = len(definite_drivers)

    if unresolved_ambiguous_refdes:
        new_refdes = unresolved_ambiguous_refdes - set(definite_drivers.keys())
        potential_max = definite_count + len(new_refdes)
        if definite_count < 2 <= potential_max:
            return M2NetResult(
                net_name=net.name,
                classification=CLASSIFICATION_UNRESOLVABLE,
                reason=REASON_UNRESOLVABLE_AMBIGUOUS,
                drivers=list(definite_drivers.values()),
                raw_output_pins=raw_output_pins,
                vetoed_pins=vetoed,
                ambiguous_pins=ambiguous_pins,
            )

    if definite_count >= 2:
        classification = CLASSIFICATION_WOULD_FAIL
        reason = REASON_GENUINE_MULTIDRIVER
    else:
        classification = CLASSIFICATION_CLEAN
        if len(raw_output_pins) >= 2 and len(raw_distinct_refdes) < 2:
            reason = REASON_SAME_REFDES_DEDUP
        elif len(raw_distinct_refdes) >= 2 and definite_count < 2:
            reason = REASON_OPEN_DRAIN_VETO
        else:
            reason = REASON_NO_MULTIDRIVER_CANDIDATE

    return M2NetResult(
        net_name=net.name,
        classification=classification,
        reason=reason,
        drivers=list(definite_drivers.values()),
        raw_output_pins=raw_output_pins,
        vetoed_pins=vetoed,
        ambiguous_pins=ambiguous_pins,
    )


def classify_netlist(ir: NetlistIR, pin_specs: dict) -> list[M2NetResult]:
    """Classify every net in `ir`. REPORT-ONLY: returns classifications, emits
    nothing into any pipeline output."""
    comp_refdes_set = {c.refdes for c in ir.components}
    pin_name_map: dict[tuple[str, str], str] = {}
    for comp in ir.components:
        for pin in comp.pins:
            pin_name_map[(comp.refdes, pin.pin_id)] = pin.pin_name

    return [
        classify_net(
            net, comp_refdes_set, pin_name_map, ir.pintypes, pin_specs,
            ir.power_nets, ir.ground_nets,
        )
        for net in ir.nets
    ]


# ── step_08f VERDICT-MOVING emit (M2 v1, cross-component, FAIL-only) ───────────

VIOLATION_OUTPUT_CONFLICT = "OUTPUT_CONFLICT"


@dataclass
class OutputConflictFinding:
    """A single M2 v1 output-conflict FAIL. FAIL-only by construction — the
    checker emits an instance ONLY for a WOULD_FAIL net; there is no PASS/WARN/
    UNRESOLVABLE finding. `status` is fixed to "FAIL" so the registry's default
    status reader (checker_registry._status_reader) moves the board verdict."""
    net: str
    drivers: list[dict]          # [{refdes, pin_id, pin_name, pintype}] — distinct refdes
    evidence_label: str
    violation: str = VIOLATION_OUTPUT_CONFLICT
    status: str = "FAIL"
    severity: str = "critical"


def check_output_conflicts(ir: NetlistIR, pin_specs: dict) -> list[OutputConflictFinding]:
    """VERDICT-MOVING (registry `08f`). Emit an OUTPUT_CONFLICT FAIL for every net
    the classifier returns WOULD_FAIL — i.e. ≥2 DISTINCT-refdes push-pull output
    drivers surviving the open-drain veto and the bidir/tri-state/power-ground
    skips. Every other classification (CLEAN, UNRESOLVABLE, any SKIPPED_*) emits
    NOTHING (FAIL-only). WOULD_FAIL is inherently cross-component, so this is the
    v1 (cross-component-only) set with no extra gate."""
    findings: list[OutputConflictFinding] = []
    for res in classify_netlist(ir, pin_specs):
        if res.classification != CLASSIFICATION_WOULD_FAIL:
            continue
        drivers = sorted(
            ({"refdes": d.refdes, "pin_id": d.pin_id, "pin_name": d.pin_name,
              "pintype": d.pintype} for d in res.drivers),
            key=lambda d: (d["refdes"], d["pin_id"]),
        )
        driver_label = ", ".join(f"{d['refdes']}.{d['pin_id']}" for d in drivers)
        findings.append(OutputConflictFinding(
            net=res.net_name,
            drivers=drivers,
            evidence_label=(
                f"Confirmed — ≥2 push-pull output drivers on net "
                f"{res.net_name}: {driver_label}"),
        ))
    return findings

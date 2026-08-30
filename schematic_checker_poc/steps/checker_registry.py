"""Declarative checker registry (Review F1, design spec §3.2/§3.3).

Single source of truth for the five report-producing checkers. Each `CheckerSpec`
carries everything the pipeline used to hand-maintain as parallel conventions:

  * `run`               — thin adapter over the checker's native signature (dispatch)
  * `report_bucket` +   — where its results land in the report + how to read each
    `extract_statuses`     result's status → feeds the registry-driven `classify_report`
  * `verdict_role`      — VERDICT_MOVING vs DETAIL_ONLY, a *typed, test-assertable*
                          property (fixes "detail-only by design vs by omission";
                          TODO-164's M4 promotion becomes a one-line flip here)
  * `source_file`       — feeds `provenance.CHECKER_SOURCE_FILES` (the step_08e
                          omission, TODO-168, becomes structurally impossible)

Import direction (no cycles): this module imports the checker modules (steps.*);
`pipeline.py` and `provenance.py` import THIS; nothing below imports it. The `run`
adapters read a duck-typed context (a `pipeline.PipelineContext`) — deliberately not
import-typed — so there is no cycle with `pipeline`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from steps import (
    step_08_checker,
    step_08b_supply_checker,
    step_08c_structural_checker,
    step_08d_peripheral_checker,
    step_08e_pullup_value_checker,
    step_08g_pullup_presence,
)
from steps import m2_output_conflict


class VerdictRole(Enum):
    VERDICT_MOVING = "VERDICT_MOVING"   # participates in the board verdict (classify_report)
    DETAIL_ONLY = "DETAIL_ONLY"         # reported + recall-scored, never moves the board verdict


def _status_reader(results: list[dict]) -> list[str]:
    """Default: read each serialized result's uppercase ``status`` field."""
    return [str(r.get("status", "")).upper() for r in results]


def _severity_reader(results: list[dict]) -> list[str]:
    """Peripheral/pullup serialize their status under ``severity`` (an enum .value)."""
    return [str(r.get("severity", "")).upper() for r in results]


@dataclass(frozen=True)
class CheckerSpec:
    name: str                 # "signal" | "supply" | "structural" | "peripheral" | "pullup_value"
    step_id: str              # "08" | "08b" | "08c" | "08d" | "08e"
    source_file: str          # rel path, e.g. "steps/step_08e_pullup_value_checker.py"
    report_bucket: str        # key in the report JSON, e.g. "pullup_value_results"
    verdict_role: VerdictRole
    run: Callable[[Any], list]  # adapter: PipelineContext -> native result objects
    extract_statuses: Callable[[list[dict]], list[str]] = _status_reader
    # Key under report["summary"] holding this checker's own pass/warn/fail/unresolvable
    # sub-counts (e.g. "peripheral_checks"), or None when the checker's counts ARE the
    # top-level summary aggregate already (the "signal"/step_08 net-level totals — see
    # derive_checker_counts). Feeds compare-tooling (corpus_baseline.py) a per-checker
    # finding-count surface with zero hand-maintained keys anywhere else (Todo 248).
    summary_key: str | None = None


# Registry order mirrors today's execution order (08 → 08b → 08c → 08d → 08e).
REGISTRY: tuple[CheckerSpec, ...] = (
    CheckerSpec(
        name="signal", step_id="08",
        source_file="steps/step_08_checker.py",
        report_bucket="results",
        verdict_role=VerdictRole.VERDICT_MOVING,
        run=lambda ctx: step_08_checker.run(
            ctx.ir, ctx.pin_specs, ctx.confirmed_voltages,
            rail_provenance=ctx.rail_provenance),
    ),
    CheckerSpec(
        name="supply", step_id="08b",
        source_file="steps/step_08b_supply_checker.py",
        report_bucket="power_supply_results",
        verdict_role=VerdictRole.VERDICT_MOVING,
        run=lambda ctx: step_08b_supply_checker.check_component_supplies(
            components=ctx.ir.components, pin_groups=ctx.seen_parts,
            confirmed_voltages=ctx.confirmed_voltages,
            derived_provenance=ctx.derived_provenance),
        summary_key="supply_checks",
    ),
    CheckerSpec(
        name="structural", step_id="08c",
        source_file="steps/step_08c_structural_checker.py",
        report_bucket="structural_integrity_results",
        verdict_role=VerdictRole.VERDICT_MOVING,
        run=lambda ctx: step_08c_structural_checker.check_structural_integrity(
            components=ctx.ir.components, confirmed_voltages=ctx.confirmed_voltages,
            ground_nets=ctx.ir.ground_nets, nets=ctx.ir.nets,
            pintypes=ctx.ir.pintypes),
        # No "unresolvable" sub-key: step_08c never assigns status="UNRESOLVABLE"
        # (confirmed by grep, Todo 248 recon step 1) — only PASS/WARN/FAIL exist.
        summary_key="structural_checks",
    ),
    CheckerSpec(
        name="peripheral", step_id="08d",
        source_file="steps/step_08d_peripheral_checker.py",
        report_bucket="peripheral_integrity_results",
        verdict_role=VerdictRole.VERDICT_MOVING,
        run=lambda ctx: step_08d_peripheral_checker.check_i2c_peripheral(
            ctx.ir, ctx.peripheral_kb, ctx.peripheral_routing),
        extract_statuses=_severity_reader,
        summary_key="peripheral_checks",
    ),
    CheckerSpec(
        name="pullup_value", step_id="08e",
        source_file="steps/step_08e_pullup_value_checker.py",
        report_bucket="pullup_value_results",
        # VERDICT_MOVING since TODO-164 (measured flip, 2026-07-07): pullup findings
        # now participate in the board verdict. The flip is NOT an era break — this
        # registry file is deliberately unhashed (not in CHECKER_SOURCE_FILES), so
        # it reinterprets existing reports rather than staling them.
        verdict_role=VerdictRole.VERDICT_MOVING,
        run=lambda ctx: step_08e_pullup_value_checker.check_pullup_values(ctx.ir),
        extract_statuses=_severity_reader,
        summary_key="pullup_value_checks",
    ),
    CheckerSpec(
        name="output_conflict", step_id="08f",
        source_file="steps/m2_output_conflict.py",
        report_bucket="output_conflict_results",
        # M2 v1 (2026-07-10): output-to-output short (≥2 distinct-refdes push-pull
        # drivers on one net). VERDICT_MOVING, FAIL-only, cross-component only.
        # Its serialized results carry a "status" field ("FAIL"), so the default
        # _status_reader applies (no custom extract_statuses).
        verdict_role=VerdictRole.VERDICT_MOVING,
        run=lambda ctx: m2_output_conflict.check_output_conflicts(ctx.ir, ctx.pin_specs),
        summary_key="output_conflict_checks",
    ),
    CheckerSpec(
        name="pullup_presence", step_id="08g",
        source_file="steps/step_08g_pullup_presence.py",
        report_bucket="pullup_presence_results",
        # Merged pull-up-PRESENCE (Todo 243 + 212): generic OD/OC + flash-CS + SD
        # WARN-tier families over one polarity-aware pull-path walk. VERDICT_MOVING,
        # WARN-only in v1 — a new WARN moves a board all_pass→has_warn. Its
        # serialized results carry a "severity" field ("WARN"), so use the
        # severity reader (mirrors 08d/08e).
        verdict_role=VerdictRole.VERDICT_MOVING,
        run=lambda ctx: step_08g_pullup_presence.check_pullup_presence(
            ctx.ir, ctx.pin_specs, ctx.peripheral_kb, ctx.peripheral_routing),
        extract_statuses=_severity_reader,
        summary_key="pullup_presence_checks",
    ),
)

BY_STEP: dict[str, CheckerSpec] = {s.step_id: s for s in REGISTRY}
BY_NAME: dict[str, CheckerSpec] = {s.name: s for s in REGISTRY}
VERDICT_MOVING: tuple[CheckerSpec, ...] = tuple(
    s for s in REGISTRY if s.verdict_role is VerdictRole.VERDICT_MOVING)
# Source files of the checkers, in registry order — provenance appends these to
# the CORE_PIPELINE_FILES to form CHECKER_SOURCE_FILES.
SOURCE_FILES: tuple[str, ...] = tuple(s.source_file for s in REGISTRY)


def derive_checker_counts(
    summary: dict, specs: "tuple[CheckerSpec, ...]" = VERDICT_MOVING,
) -> dict[str, int]:
    """Flatten each VERDICT_MOVING checker's own ``report["summary"][spec.summary_key]``
    sub-dict into ``{spec.name}_{pass|warn|fail|unresolvable}`` keys — whichever fields
    that sub-dict actually reports (e.g. ``structural_checks`` has no ``unresolvable``;
    ``pullup_presence_checks`` has only ``warn``).

    Registry-derived (Todo 248, closing the compare_staleness_recon gap): a checker
    gains a per-finding compare-tooling surface the moment its CheckerSpec sets
    ``summary_key`` — no hand-maintained key list in run_checks.py or
    corpus_baseline.py. ``specs`` defaults to the real registry but accepts an
    injected tuple so tests can prove the derivation on a synthetic spec without
    mutating the module-level REGISTRY.
    """
    out: dict[str, int] = {}
    for spec in specs:
        if not spec.summary_key:
            continue
        sub = summary.get(spec.summary_key) or {}
        for field in ("pass", "warn", "fail", "unresolvable"):
            if field in sub:
                out[f"{spec.name}_{field}"] = int(sub[field])
    return out

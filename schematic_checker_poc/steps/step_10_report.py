import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.text import Text

from steps.step_08_checker import CheckResult
from steps.step_08b_supply_checker import SupplyCheckResult
from steps.step_08c_structural_checker import StructuralCheckResult
from steps.step_08d_peripheral_checker import PeripheralFinding
from steps.m2_output_conflict import OutputConflictFinding
from steps import doc_identity
from steps import checker_registry as _checker_registry
from llm import ollama_client

logger = logging.getLogger(__name__)
console = Console()

EXPLANATION_PROMPT = """
In one sentence of 25 words or fewer, explain why this is a
schematic violation. Be specific. No preamble.

The violation direction is: the DRIVER is outputting too HIGH a
voltage for the RECEIVER to safely accept.

Net: {net_name}
Driver: {driver_refdes}, logic-high output approximately {driver_voltage}V
  (source: {driver_voltage_source})
Receiver: {receiver_refdes} pin {receiver_pin_name}
  Limit: {threshold_label}
Violation: {driver_voltage}V > {receiver_threshold}V
Combined confidence: {combined_confidence}

If combined_confidence is "medium" or "low", note the uncertainty
briefly in parentheses at the end.
"""


SUPPLY_EXPLANATION_PROMPT = """
In one sentence of 25 words or fewer, explain this power supply violation.
Be specific. No preamble.

Component: {part_number} (refdes {refdes})
Supply pin: {supply_pin}
Connected net: {connected_net}
Actual voltage: {actual_voltage}V
Rated supply range: {rated_min}V to {rated_max}V
Absolute max supply: {rated_abs_max}V
Violation: {evidence_label}
"""


def _supply_explain(r: SupplyCheckResult) -> str | None:
    if r.status == "PASS":
        return None
    if r.status == "UNRESOLVABLE":
        return r.evidence_label

    prompt = SUPPLY_EXPLANATION_PROMPT.format(
        part_number=r.part_number,
        refdes=r.refdes,
        supply_pin=f"{r.supply_pin_name} (pin {r.supply_pin_id})",
        connected_net=r.connected_net,
        actual_voltage=r.actual_voltage,
        rated_min=r.rated_min,
        rated_max=r.rated_max,
        rated_abs_max=r.rated_abs_max,
        evidence_label=r.evidence_label,
    )
    try:
        return ollama_client.generate(prompt, temperature=0.0, step_hint="10_report")
    except RuntimeError as e:
        logger.warning("[STEP 10] explanation text unavailable (local LLM not running) — verdicts unaffected")
        logger.debug(f"[STEP 10] supply explanation failure detail: {e}")
        return r.evidence_label


def _get_part_number(refdes: str | None, components) -> str | None:
    if refdes is None:
        return None
    comp = next((c for c in components if c.refdes == refdes), None)
    return comp.part_number if comp else None


# ── evidence-tier vocabulary (TODO-368 Phase 2 FIX) ────────────────────────────
#
# A SEPARATE axis from the doc_identity qualifier above: doc_identity asks "does
# this cache's stem appear in the PDF text it claims to come from" (a wrong-
# document check); this layer asks "is the cache we read still current relative
# to the PDF on disk" (step_03_resolver.check_cache_provenance). Both are
# text-only, VERDICT-NEUTRAL annotations on evidence_label; per the dispatch's
# ORDER requirement this tier transform runs FIRST, doc_identity qualification
# SECOND (its suffix appends to the already-tier-rewritten label).
TIER_CONFIRMED_LOCAL = "confirmed_local"     # check_cache_provenance == "current"
TIER_CACHE_DRIFT = "cache_drift"             # check_cache_provenance == "stale"
TIER_CACHE_UNVERIFIED = "cache_unverified"   # "legacy_unverified" | "current_no_pdf_verify"
TIER_ASSUMED = "assumed"                     # label starts "Assumed" — not a cache read at all
TIER_NOT_CACHE_DERIVED = "not_cache_derived"  # KB/topology buckets — no cache backs the finding

# FROZEN — byte-exact, any deviation is a dispatch STOP condition.
_TIER2_PARENTHETICAL = " (not locally verified)"
_TIER3_PARENTHETICAL = " (local datasheet differs from cache source)"
_CONFIRMED_PREFIX = "Confirmed"
_CACHE_SOURCED_PREFIX = "Cache-sourced"

# FROZEN — byte-exact (TODO-380 Phase 2, decision W1-W4). One tier
# (not_cache_derived) now carries FOUR distinct wordings depending on which
# bucket/root-cause emits it — implemented per-site below, deliberately NOT
# folded into `_TIER_PARENTHETICAL` (that dict is keyed by tier, and a
# single tier here maps to more than one string).
_W_PERIPHERAL_KB = " (MCU KB — no datasheet required)"
_W_TOPOLOGY = " (netlist topology — no datasheet required)"
_W_NETLIST_EVIDENCE_ONLY = " (netlist evidence only — no part data in the cache)"
_W_NO_CACHE_RESULTS = " (no part data in the cache — see LIMITATIONS.md to add parts)"

# b) STATUS -> TIER MAP, enumerated from check_cache_provenance's full return
# vocabulary (steps/step_03_resolver.py check_cache_provenance docstring:
# 'current' | 'stale' | 'legacy_unverified' | 'current_no_pdf_verify' — closed
# set, verified against the implementation at TODO-368 build time). Every
# member maps cleanly under the dispatch's rule; no STOP fired.
_STATUS_TIER_MAP = {
    "current": TIER_CONFIRMED_LOCAL,
    "stale": TIER_CACHE_DRIFT,
    "legacy_unverified": TIER_CACHE_UNVERIFIED,
    "current_no_pdf_verify": TIER_CACHE_UNVERIFIED,
}

_TIER_PARENTHETICAL = {
    TIER_CACHE_UNVERIFIED: _TIER2_PARENTHETICAL,
    TIER_CACHE_DRIFT: _TIER3_PARENTHETICAL,
}

# Multi-cache precedence (the M2 output-conflict bucket cites >=2 drivers, each
# backed by its own part's cache_currency): the highest-precedence tier among
# the cited parts wins. Mirrors doc_identity.qualify_many's own precedent
# exactly (MISS outranks unverifiable: "a measured negative is more informative
# than 'nothing could be measured'") — cache_drift (an actively MEASURED
# mismatch) outranks cache_unverified (nothing could be verified) outranks
# confirmed_local (all clean).
_TIER_PRECEDENCE = (TIER_CACHE_DRIFT, TIER_CACHE_UNVERIFIED, TIER_CONFIRMED_LOCAL)


def _cache_currency_of(part_number: str | None, extraction_metadata: dict) -> str | None:
    """The check_cache_provenance status of the cache that supplied `part_number`'s
    specs — a dict lookup, no cache is re-read (mirrors `_doc_identity_of`)."""
    return ((extraction_metadata or {}).get(part_number) or {}).get("cache_currency")


def _tier_for_status(status: str | None) -> str:
    """b) map. `status=None` means no cache-load path ever computed a currency
    status for this part this run (a fresh this-run extraction, an unresolved
    part, or an extraction_metadata dict — e.g. a pre-368 test fixture — that
    never threaded cache_currency at all). That carries no NEGATIVE currency
    signal, so it is treated the same as confirmed_local: no text rewrite."""
    if status is None:
        return TIER_CONFIRMED_LOCAL
    return _STATUS_TIER_MAP.get(status, TIER_CONFIRMED_LOCAL)


def _rewrite_for_tier(label: str | None, tier: str) -> str | None:
    """The FROZEN prefix-rewrite + parenthetical-append, for a label already
    resolved to `tier`. A no-op for confirmed_local/assumed (no parenthetical
    registered) and for `label is None`."""
    parenthetical = _TIER_PARENTHETICAL.get(tier)
    if parenthetical is None or label is None:
        return label
    new_label = label
    if new_label.startswith(_CONFIRMED_PREFIX):
        new_label = _CACHE_SOURCED_PREFIX + new_label[len(_CONFIRMED_PREFIX):]
    return new_label + parenthetical


def _apply_tier(label: str | None, cache_currency: str | None) -> tuple[str | None, str]:
    """Tier transform for one finding backed by a SINGLE part. Returns
    (new_label, evidence_tier). Runs BEFORE doc_identity qualification."""
    if label is not None and label.startswith("Assumed"):
        return label, TIER_ASSUMED
    tier = _tier_for_status(cache_currency)
    return _rewrite_for_tier(label, tier), tier


def _apply_tier_many(label: str | None, cache_currencies: list) -> tuple[str | None, str]:
    """As `_apply_tier`, for a finding citing several caches (the M2
    output-conflict bucket) — see `_TIER_PRECEDENCE`."""
    if label is not None and label.startswith("Assumed"):
        return label, TIER_ASSUMED
    tiers = [_tier_for_status(c) for c in cache_currencies] or [TIER_CONFIRMED_LOCAL]
    tier = next((cand for cand in _TIER_PRECEDENCE if cand in tiers), TIER_CONFIRMED_LOCAL)
    return _rewrite_for_tier(label, tier), tier


def _never_cached(part_number: str | None, extraction_metadata: dict) -> bool:
    """TODO-380 Phase 2: True when `part_number` never completed a real
    cache-currency check — either it was never queued for resolution at all
    (root cause 2: no `extraction_metadata` entry exists for it — e.g. the
    `has_non_power_pin` pre-queue filter, `pipeline.py:388-398`), or it WAS
    queued and resolution found nothing to serve (root cause 1: an entry
    exists, `resolved` is falsy, and no `cache_currency` was ever attached).

    Deliberately NOT true for a fresh this-run extraction (`resolved` is
    truthy, no `cache_currency` yet — nothing was ever cached, but a cache
    WAS just written) — that shape legitimately stays `confirmed_local`
    through `_apply_tier`'s existing `_tier_for_status(None)` path; it is
    not this function's population."""
    e = (extraction_metadata or {}).get(part_number)
    if e is None:
        return True
    return (not e.get("resolved")) and ("cache_currency" not in e)


def _apply_tier_or_never_cached(
        label: str | None, part_number: str | None, extraction_metadata: dict,
        never_cached_wording: str) -> tuple[str | None, str]:
    """As `_apply_tier`, but first routes to `not_cache_derived` (with a
    site-specific wording suffix) when `_never_cached` — TODO-380 Phase 2,
    closing the confirmed_local fail-open for root causes 1+2. The
    pre-existing `Assumed` short-circuit still wins over everything (checked
    first here, exactly as `_apply_tier` already does internally), so this
    never reorders that precedent."""
    if label is not None and label.startswith("Assumed"):
        return label, TIER_ASSUMED
    if _never_cached(part_number, extraction_metadata):
        new_label = label + never_cached_wording if label is not None else label
        return new_label, TIER_NOT_CACHE_DERIVED
    return _apply_tier(label, _cache_currency_of(part_number, extraction_metadata))


def _cache_manifest_version():
    """The shipped-cache manifest's version, when one exists.

    D368-E left this as an unconditional None until a real manifest was
    introduced elsewhere. LT-38 Phase 3b introduces it —
    ``datasheets_parsed/CACHE_MANIFEST.json``, written by
    ``export/package_cache.py`` — so this now reads its ``cache_version``
    field, via the resolver's memoized loader (no extra file read per report).

    Returns None when no manifest is present, which is the normal state of a
    private/dev tree and reproduces the pre-3b header exactly.

    THIS IS THE ANNOUNCED CODE-ERA BREAK: ``steps/step_10_report.py`` is a
    ``CORE_PIPELINE_FILES`` member (provenance.py), so editing it moves
    ``checker_code_hash`` and stales every cached report by design.

    The import is local: step_03_resolver is heavy (pdfplumber, ollama client)
    and step_10_report is imported by lighter consumers that must not pay for
    it — the same lazy-import discipline the M14 capability wiring uses."""
    try:
        from steps import step_03_resolver
        return step_03_resolver.cache_manifest_version()
    except Exception:  # never let report rendering fail on a manifest read
        return None


# ── staged-cache marker (TODO-386 Phase 3, S3-α) ────────────────────────────
#
# A staging-tier serve is reported on a NEW, INDEPENDENT axis: a per-finding
# `cache_tier` field plus a report-header banner enumerating the staged stems.
# The frozen five-value evidence_tier vocabulary above is deliberately
# UNTOUCHED — "this evidence came from an unpromoted cache" is a provenance
# fact about the FILE, orthogonal to how current/verifiable its contents are,
# and folding it into evidence_tier would have made a sixth label whose
# meaning overlaps two existing ones.
#
# Present-only-when-staged, on both axes (field and banner), so a normal report
# is byte-identical to its pre-386 shape — the same inert-by-default idiom
# `rail_candidates` / `rail_map_conflicts` / `drift_warning` already use.
#
# THIS IS THE ANNOUNCED CODE-ERA BREAK for TODO-386 Phase 3: steps/step_10_report.py
# is a CORE_PIPELINE_FILES member (provenance.py), so editing it moves
# checker_code_hash and stales every cached report by design. The S3-α marker
# cannot be produced anywhere else: build_report both owns finding→part
# attribution and writes the report file itself, so a driver-side post-pass
# would have to duplicate the attribution AND rewrite the just-written JSON.
CACHE_TIER_STAGED = "staged"

STAGED_CACHE_BANNER_SENTENCE = (
    "One or more findings in this report cite a datasheet cache served from the "
    "UNPROMOTED staging tier (datasheets_staged/). That evidence has not passed "
    "the promotion gate (export/promote_staged.py) and must not be treated as "
    "canonical."
)


def _cache_tier_of(part_number: str | None, extraction_metadata: dict) -> str | None:
    """The cache tier that supplied `part_number`'s specs — a dict lookup, no
    cache is re-read (mirrors `_cache_currency_of` / `_doc_identity_of`)."""
    return ((extraction_metadata or {}).get(part_number) or {}).get("cache_tier")


def _staged_marker(part_number: str | None, extraction_metadata: dict) -> dict:
    """`{"cache_tier": "staged"}` when this finding's cited cache came from the
    staging tier, else `{}` — spliced into a finding dict so non-staged reports
    keep their exact pre-386 shape."""
    if _cache_tier_of(part_number, extraction_metadata) == CACHE_TIER_STAGED:
        return {"cache_tier": CACHE_TIER_STAGED}
    return {}


def _staged_marker_many(part_numbers: list, extraction_metadata: dict) -> dict:
    """As `_staged_marker`, for a finding citing several caches (the M2
    output-conflict bucket): staged if ANY cited part was served from staging.
    ANY, not ALL — the marker's job is to stop a reader trusting evidence that
    is partly unpromoted, and one unpromoted citation is enough for that."""
    if any(_cache_tier_of(p, extraction_metadata) == CACHE_TIER_STAGED
           for p in part_numbers):
        return {"cache_tier": CACHE_TIER_STAGED}
    return {}


def _staged_stems(extraction_metadata: dict) -> list[str]:
    """Sorted, de-duplicated stems served from staging this run — what the
    header banner enumerates. Falls back to the part number when a pre-386
    extraction_metadata carries no `cache_stem` key."""
    return sorted({
        (entry.get("cache_stem") or part)
        for part, entry in (extraction_metadata or {}).items()
        if isinstance(entry, dict) and entry.get("cache_tier") == CACHE_TIER_STAGED
    })


DRIFT_WARNING_SENTENCE = (
    "One or more findings in this report cite a datasheet cache whose stored "
    "hash no longer matches the PDF on disk (cache_drift) — treat those "
    "findings' evidence as possibly out of date."
)


def _doc_identity_of(part_number: str | None, extraction_metadata: dict) -> dict | None:
    """The doc-identity verdict of the cache that supplied `part_number`'s specs.

    TODO-337 Phase 2b-iii. `extraction_metadata` is keyed by part number and already
    carries the verdict (pipeline.py lifts it from the cache's provenance beside
    source_hash), so this is a dict lookup — no part is re-resolved, no cache is
    re-read. None (-> no qualifier) for an unknown part or a pre-2b-ii cache."""
    return ((extraction_metadata or {}).get(part_number) or {}).get("doc_identity")


def _qualified(label: str | None, part_number: str | None,
               extraction_metadata: dict) -> str | None:
    """`label` with the cited cache's doc-identity qualifier appended, if any."""
    return doc_identity.qualify(
        label, _doc_identity_of(part_number, extraction_metadata))


def _explain(result: CheckResult) -> str:
    if result.status == "UNRESOLVABLE":
        return result.unresolvable_reason or "Insufficient data to evaluate this net."

    receiver_threshold = result.receiver_VIH_max or result.receiver_abs_max
    driver_voltage = result.driver_voltage

    if driver_voltage is None and receiver_threshold is None:
        return "Insufficient data — neither driver voltage nor receiver threshold available."

    if driver_voltage is None:
        return (
            f"Driver voltage for {result.driver_refdes} could not be determined "
            f"from available datasheets or power domain inference."
        )

    if receiver_threshold is None:
        return (
            f"Receiver threshold for {result.receiver_refdes} pin "
            f"{result.receiver_pin_name} could not be extracted from its datasheet."
        )

    threshold_label = (
        f"VIH_max {result.receiver_VIH_max}V"
        if result.receiver_VIH_max is not None
        else f"absolute maximum {result.receiver_abs_max}V"
    )

    prompt = EXPLANATION_PROMPT.format(
        net_name=result.net_name,
        driver_refdes=result.driver_refdes,
        driver_voltage=driver_voltage,
        receiver_refdes=result.receiver_refdes,
        receiver_pin_name=result.receiver_pin_name,
        threshold_label=threshold_label,
        receiver_threshold=receiver_threshold,
        driver_voltage_source=result.driver_voltage_source,
        receiver_confidence=result.receiver_confidence,
        combined_confidence=result.combined_confidence,
    )
    try:
        return ollama_client.generate(prompt, temperature=0.0, step_hint="10_report")
    except RuntimeError as e:
        logger.warning("[STEP 10] explanation text unavailable (local LLM not running) — verdicts unaffected")
        logger.debug(f"[STEP 10] explanation failure detail: {e}")
        return (
            f"{result.driver_refdes} drives {driver_voltage}V into "
            f"{result.receiver_pin_name} which is rated {receiver_threshold}V max."
        )


def _run_git_provenance() -> tuple[str | None, bool]:
    """(git_sha, git_dirty) via the SAME helper summary.json uses. Robust to import
    path (step_10 may run from either main.py or run_corpus_test)."""
    try:
        import os, sys
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from corpus_baseline import git_sha, git_dirty
        sha = git_sha()
        return sha, (git_dirty() if sha else False)
    except Exception:
        return None, False


def _build_report_provenance(extraction_metadata: dict, source_netlist: str) -> dict:
    """Report stamp (verified by schematic_checker_poc/provenance.check_report_provenance):
      - git_sha/git_dirty — free coarse signal.
      - cache_source_hashes {part→source_hash} — catches a report built against a since-changed
        datasheet cache (null for unresolved / legacy caches).
      - input_netlist_sha256 — catches a report built against a since-changed / stale-export
        input netlist (the recall export-cache footgun, TODO-109); null if unreadable."""
    sha, dirty = _run_git_provenance()
    try:
        from provenance import sha256_file
        input_sha = sha256_file(source_netlist) if source_netlist else None
    except Exception:
        input_sha = None
    try:
        from provenance import checker_code_hash
        code_hash = checker_code_hash()  # CONTENT hash of the verdict-affecting source set
    except Exception:
        code_hash = None
    return {
        "git_sha": sha,
        "git_dirty": dirty,
        "input_netlist_sha256": input_sha,
        "checker_code_hash": code_hash,   # code axis (3933272c-88f3-8181): a checker change → stale
        "cache_source_hashes": {
            part: (meta or {}).get("source_hash")
            for part, meta in (extraction_metadata or {}).items()
        },
        # TODO-368 Phase 2 (D368-E): the signed cache manifest's version, when one
        # exists. No manifest exists anywhere in this repo today — always None.
        "cache_version": _cache_manifest_version(),
    }


def build_report(
    source_netlist: str,
    results: list[CheckResult],
    confirmed_voltages: dict[str, float],
    extraction_metadata: dict,
    output_path: str = "report.json",
    components=None,
    supply_results: list[SupplyCheckResult] | None = None,
    structural_results: list[StructuralCheckResult] | None = None,
    peripheral_results: list[PeripheralFinding] | None = None,
    pullup_results: list | None = None,
    output_conflict_results: list[OutputConflictFinding] | None = None,
    pullup_presence_results: list | None = None,
    rail_candidates: list | None = None,
    rail_map_conflicts: list | None = None,
) -> dict:
    if components is None:
        components = []
    if supply_results is None:
        supply_results = []
    if structural_results is None:
        structural_results = []
    if peripheral_results is None:
        peripheral_results = []
    if pullup_results is None:
        pullup_results = []
    if output_conflict_results is None:
        output_conflict_results = []
    if pullup_presence_results is None:
        pullup_presence_results = []
    report_results = []
    for r in results:
        # TODO-337 2b-iii / TODO-368 Phase 2: this label cites the RECEIVER's pin
        # spec (VIH/abs-max — see step_08_checker's evaluate_compatibility), so
        # the receiver's cache is the one whose currency/doc-identity qualify it.
        # ORDER: tier transform FIRST, doc_identity qualify SECOND.
        _receiver_part = _get_part_number(r.receiver_refdes, components)
        _tiered_label, _tier = _apply_tier_or_never_cached(
            r.evidence_label, _receiver_part, extraction_metadata, _W_NO_CACHE_RESULTS)
        entry: dict = {
            "net": r.net_name,
            "status": r.status,
            "driver": {
                "refdes": r.driver_refdes,
                "part_number": _get_part_number(r.driver_refdes, components),
                "power_domain": None,
                "voltage_v": r.driver_voltage,
                "voltage_source": r.driver_voltage_source,
                "confidence": r.driver_confidence,
            },
            "receiver": {
                "refdes": r.receiver_refdes,
                "pin_name": r.receiver_pin_name,
                "VIH_max_v": r.receiver_VIH_max,
                "absolute_max_v": r.receiver_abs_max,
            },
            "confidence": r.combined_confidence,
            "evidence_label": _qualified(
                _tiered_label, _receiver_part, extraction_metadata),
            "evidence_tier": _tier,
            "unresolvable_reason": r.unresolvable_reason,
            "explanation": None,
            # S3-α: same part whose cache backs evidence_tier above (the RECEIVER).
            **_staged_marker(_receiver_part, extraction_metadata),
        }

        if r.status in ("FAIL", "WARN", "UNRESOLVABLE"):
            entry["explanation"] = _explain(r)

        report_results.append(entry)

    counts = {"pass": 0, "warn": 0, "fail": 0, "unresolvable": 0}
    for r in results:
        counts[r.status.lower()] = counts.get(r.status.lower(), 0) + 1

    supply_report_results = []
    for r in supply_results:
        # NOTE (TODO-337 2b-iii, extended TODO-368 Phase 2): the explanation is
        # generated FIRST, from the raw label on `r` itself — SUPPLY_EXPLANATION_
        # PROMPT interpolates {evidence_label}, and neither the doc_identity
        # qualifier NOR the evidence-tier rewrite may ever enter a prompt. Both
        # transforms happen below, on the emitted field only — `r.evidence_label`
        # itself is never mutated.
        explanation = _supply_explain(r)
        _tiered_label, _tier = _apply_tier_or_never_cached(
            r.evidence_label, r.part_number, extraction_metadata, _W_NETLIST_EVIDENCE_ONLY)
        supply_report_results.append({
            "refdes": r.refdes,
            "part_number": r.part_number,
            "supply_pin": f"{r.supply_pin_name} (pin {r.supply_pin_id})",
            "connected_net": r.connected_net,
            "actual_voltage_v": r.actual_voltage,
            "rated_min_v": r.rated_min,
            "rated_max_v": r.rated_max,
            "rated_abs_max_v": r.rated_abs_max,
            "status": r.status,
            "confidence": r.confidence,
            "evidence_label": _qualified(_tiered_label, r.part_number,
                                         extraction_metadata),
            "evidence_tier": _tier,
            "explanation": explanation,
            **_staged_marker(r.part_number, extraction_metadata),
        })

    structural_report_results = []
    for r in structural_results:
        _tiered_label, _tier = _apply_tier_or_never_cached(
            r.evidence_label, r.part_number, extraction_metadata, _W_NETLIST_EVIDENCE_ONLY)
        structural_report_results.append({
            "refdes": r.refdes,
            "part_number": r.part_number,
            "pin": f"{r.pin_name} (pin {r.pin_id})",
            "pin_kind": r.pin_kind,
            "connected_net": r.connected_net,
            "expected_kind": r.expected_kind,
            "status": r.status,
            "confidence": r.confidence,
            "evidence_label": _qualified(_tiered_label, r.part_number,
                                         extraction_metadata),
            "evidence_tier": _tier,
            "explanation": r.explanation,
            "finding_code": r.finding_code,
            **_staged_marker(r.part_number, extraction_metadata),
        })

    # KB/topology-derived buckets (TODO-368 Phase 2 c): no cache backs these
    # findings' evidence directly, so evidence_tier is the fixed
    # "not_cache_derived" marker — every finding still carries the field (no
    # omissions), just not a cache-currency-resolved one. TODO-380 Phase 2:
    # the text is no longer untouched — each site appends its own fixed
    # wording suffix (_W_PERIPHERAL_KB / _W_TOPOLOGY) naming WHY no cache
    # applies here, mirroring the wording the new never-cached branch adds
    # for structural/power_supply/results (see _apply_tier_or_never_cached).
    peripheral_report_results = [
        {
            "net": r.net,
            "violation": r.violation.value,
            "severity": r.severity.value,
            "pins": r.pins,
            "evidence": r.evidence + _W_PERIPHERAL_KB,
            "kb_provenance": [s.value for s in r.kb_provenance],
            "evidence_tier": TIER_NOT_CACHE_DERIVED,
        }
        for r in peripheral_results
    ]
    pullup_value_report_results = [
        {
            "net": r.net,
            "refdes": r.refdes,
            "violation": "PULLUP_VALUE_OUT_OF_RANGE",
            "severity": r.severity.value,
            "ohms": r.ohms,
            "value_str": r.value_str,
            "band": r.band,
            "pins": r.pins,
            "evidence": r.evidence + _W_TOPOLOGY,
            "evidence_tier": TIER_NOT_CACHE_DERIVED,
        }
        for r in pullup_results
    ]

    output_conflict_report_results = []
    for r in output_conflict_results:
        # TODO-337 2b-iii / TODO-368 Phase 2: this evidence names >=2 distinct
        # drivers, so it cites >=2 caches (each driver's pintype came from its
        # own). At most ONE tier / ONE doc_identity qualifier is applied — the
        # highest-precedence class present among the cited parts.
        _driver_parts = [_get_part_number(d.get("refdes"), components)
                          for d in (r.drivers or [])]
        _tiered_label, _tier = _apply_tier_many(
            r.evidence_label,
            [_cache_currency_of(p, extraction_metadata) for p in _driver_parts])
        output_conflict_report_results.append({
            "net": r.net,
            "violation": r.violation,
            "status": r.status,
            "severity": r.severity,
            "drivers": r.drivers,
            "evidence_label": doc_identity.qualify_many(
                _tiered_label,
                [_doc_identity_of(p, extraction_metadata) for p in _driver_parts]),
            "evidence_tier": _tier,
            **_staged_marker_many(_driver_parts, extraction_metadata),
        })

    pullup_presence_report_results = [
        {
            "net": r.net,
            "family": r.family,
            "violation": r.violation,
            "severity": r.severity,
            "pins": r.pins,
            "corroboration": r.corroboration,
            "evidence": r.evidence + _W_TOPOLOGY,
            # TODO-272 Phase 2b: Family 1's entry-path tag ("od_pintype" |
            # "i2c_net_name" | None for Families 2/3, which have no alternate
            # entry). Field addition only — no verdict logic here.
            "activation": r.activation,
            "evidence_tier": TIER_NOT_CACHE_DERIVED,
        }
        for r in pullup_presence_results
    ]

    # TODO-368 Phase 2 (D368-D): per-tier finding counts, over EVERY bucket that
    # now carries evidence_tier — named with _COUNT_SUFFIXES-compatible ("_count")
    # keys so they enter corpus_baseline's compare surface automatically once
    # run_corpus_test.py flattens them (mirrors the existing registry-derived
    # per-checker-count convention, TODO-248).
    evidence_tier_counts = {
        TIER_CONFIRMED_LOCAL: 0, TIER_CACHE_DRIFT: 0, TIER_CACHE_UNVERIFIED: 0,
        TIER_ASSUMED: 0, TIER_NOT_CACHE_DERIVED: 0,
    }
    for _bucket in (
        report_results, supply_report_results, structural_report_results,
        peripheral_report_results, pullup_value_report_results,
        output_conflict_report_results, pullup_presence_report_results,
    ):
        for _entry in _bucket:
            evidence_tier_counts[_entry["evidence_tier"]] = (
                evidence_tier_counts.get(_entry["evidence_tier"], 0) + 1)

    report = {
        "schema_version": "poc-1.4",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provenance": _build_report_provenance(extraction_metadata, source_netlist),
        "source_netlist": source_netlist,
        "summary": {
            "total_nets_checked": len(results),
            "pass": counts["pass"],
            "warn": counts["warn"],
            "fail": counts["fail"],
            "unresolvable": counts["unresolvable"],
            "supply_checks": {
                "total": len(supply_results),
                "pass": sum(1 for r in supply_results if r.status == "PASS"),
                "warn": sum(1 for r in supply_results if r.status == "WARN"),
                "fail": sum(1 for r in supply_results if r.status == "FAIL"),
                "unresolvable": sum(1 for r in supply_results if r.status == "UNRESOLVABLE"),
            },
            "structural_checks": {
                "total": len(structural_results),
                "pass": sum(1 for r in structural_results if r.status == "PASS"),
                "warn": sum(1 for r in structural_results if r.status == "WARN"),
                "fail": sum(1 for r in structural_results if r.status == "FAIL"),
            },
            "pullup_value_checks": {
                "total": len(pullup_results),
                "warn":         sum(1 for r in pullup_results if r.severity.value == "WARN"),
                "fail":         sum(1 for r in pullup_results if r.severity.value == "FAIL"),
                "unresolvable": sum(1 for r in pullup_results if r.severity.value == "UNRESOLVABLE"),
            },
            "peripheral_checks": {
                "total": len(peripheral_results),
                "pass":        sum(1 for r in peripheral_results if r.severity.value == "PASS"),
                "warn":        sum(1 for r in peripheral_results if r.severity.value == "WARN"),
                "fail":        sum(1 for r in peripheral_results if r.severity.value == "FAIL"),
                "unresolvable": sum(1 for r in peripheral_results if r.severity.value == "UNRESOLVABLE"),
            },
            "output_conflict_checks": {
                "total": len(output_conflict_results),
                "fail": sum(1 for r in output_conflict_results if r.status == "FAIL"),
            },
            "pullup_presence_checks": {
                "total": len(pullup_presence_results),
                "warn": sum(1 for r in pullup_presence_results if r.severity == "WARN"),
            },
            # TODO-368 Phase 2 (D368-D): "_count"-suffixed -> _COUNT_SUFFIXES-
            # compatible, automatically retained by corpus_baseline._extract_stats
            # once flattened into a per-netlist stats key (run_corpus_test.py).
            "evidence_tier_checks": {
                f"{tier}_count": n for tier, n in evidence_tier_counts.items()
            },
        },
        "results": report_results,
        "power_supply_results": supply_report_results,
        "structural_integrity_results": structural_report_results,
        "peripheral_integrity_results": peripheral_report_results,
        "pullup_value_results": pullup_value_report_results,
        "output_conflict_results": output_conflict_report_results,
        "pullup_presence_results": pullup_presence_report_results,
        "power_rails_confirmed": confirmed_voltages,
        "extraction_metadata": extraction_metadata,
    }
    # TODO-425: additive summary.total_pass/warn/fail/unresolvable grand totals
    # (the same cross-axis reach as the console RESULT box, plus "pass"). Purely
    # additive — no pre-existing summary key is touched or removed.
    report["summary"].update(_compute_summary_totals(report))
    # TODO-134: only present when supplied (keeps existing report shape inert).
    if rail_candidates is not None:
        report["rail_candidates"] = rail_candidates
    if rail_map_conflicts:
        report["rail_map_conflicts"] = rail_map_conflicts
    # TODO-368 Phase 2 (d): one report-level drift_warning field, present only
    # when at least one finding is cache_drift (keeps the common no-drift report
    # shape inert, mirroring the rail_candidates/rail_map_conflicts idiom above).
    if evidence_tier_counts[TIER_CACHE_DRIFT] > 0:
        report["drift_warning"] = DRIFT_WARNING_SENTENCE
    # TODO-386 Phase 3 (S3-α): the report-header staged banner. Same
    # present-only-when-relevant idiom as drift_warning above — absent (not
    # empty) on every non-staged run, so the common report shape is unchanged.
    # Derived from extraction_metadata, so it enumerates every part SERVED from
    # staging this run, including ones no finding happens to cite.
    _staged = _staged_stems(extraction_metadata)
    if _staged:
        report["staged_cache_notice"] = {
            "message": STAGED_CACHE_BANNER_SENTENCE,
            "staged_stems": _staged,
        }

    with open(output_path, "w") as f:
        json.dump(report, f, indent=2)
    logger.info(f"[STEP 10] Report written to {output_path}")

    _print_summary(report, output_path)
    return report


def _sum_verdict_field(report: dict, field: str) -> int:
    """Cross-axis total for ONE status field (TODO-425): the signal (step 08) bucket's
    own top-level summary count for ``field``, plus every VERDICT_MOVING checker's
    ``summary_key`` sub-dict count for that same field (registry-derived via
    checker_registry.derive_checker_counts, Todo 248's convention — a checker whose
    sub-dict has no such field, e.g. pullup_value_checks has no "pass", contributes
    nothing for it rather than erroring)."""
    s = report["summary"]
    total = s.get(field, 0)
    per_checker = _checker_registry.derive_checker_counts(s)
    for key, value in per_checker.items():
        if key.endswith(f"_{field}"):
            total += value
    return total


def _aggregate_verdict_counts(report: dict) -> dict[str, int]:
    """Cross-axis FAIL/WARN/UNRESOLVABLE totals (LT-21): the signal (step 08) bucket's
    own top-level summary counts, plus every other VERDICT_MOVING checker's
    ``summary_key`` sub-dict (registry-derived via checker_registry.derive_checker_counts,
    Todo 248's convention — a new checker's counts join this total the moment its
    CheckerSpec sets summary_key, no hand-maintained list here)."""
    return {field: _sum_verdict_field(report, field)
            for field in ("fail", "warn", "unresolvable")}


def _compute_summary_totals(report: dict) -> dict[str, int]:
    """Additive summary.total_pass/warn/fail/unresolvable (TODO-425): the same
    cross-axis aggregation _aggregate_verdict_counts uses for the console RESULT
    box, extended to also cover "pass" — which _aggregate_verdict_counts omits
    because the console box never prints a PASS line. Not a duplicate aggregation:
    both call the same _sum_verdict_field."""
    return {f"total_{field}": _sum_verdict_field(report, field)
            for field in ("pass", "warn", "fail", "unresolvable")}


def _print_summary(report: dict, output_path: str) -> None:
    # Lazy import (LT-21): pipeline.py imports this module at top level, so a
    # module-level `from pipeline import classify_report` here would be circular.
    # By the time build_report() is actually CALLED, pipeline.py has already
    # finished loading, so a call-time import resolves fine (same lazy-import
    # style already used by _run_git_provenance/_build_report_provenance above).
    try:
        from pipeline import classify_report
        status = classify_report(report)
    except Exception:
        status = "unknown"

    totals = _aggregate_verdict_counts(report)

    console.print("\n" + "─" * 37)
    console.print(f"  RESULT: {status}")
    console.print("─" * 37)

    # TODO-386 Phase 3 (S3-α): the banner is a REPORT header field first
    # (report["staged_cache_notice"]); this is its console rendering, printed
    # before the counts so it cannot be missed by someone reading only the tail
    # of a corpus log.
    _staged_notice = report.get("staged_cache_notice")
    if _staged_notice:
        banner = Text(
            f"  STAGED CACHE: {len(_staged_notice['staged_stems'])} stem(s) served "
            f"from the unpromoted staging tier\n"
            f"                {', '.join(_staged_notice['staged_stems'])}"
        )
        banner.stylize("bold yellow")
        console.print(banner)
        console.print("─" * 37)

    fail_text = Text(f"  FAIL:            {totals['fail']}")
    if totals["fail"] > 0:
        fail_text.stylize("bold red")
    console.print(fail_text)
    console.print(f"  WARN:            {totals['warn']}")
    console.print(f"  UNRESOLVABLE:    {totals['unresolvable']}")

    # LT-21: output_path is already absolute for every live driver (run_corpus_test's
    # --board/corpus paths); prefixing "./" unconditionally doubled the path
    # (".//home/..."). Only relative callers (e.g. main.py's "report.json" default)
    # still get the "./" prefix.
    out = Path(output_path)
    display_path = str(out) if out.is_absolute() else f"./{output_path}"
    console.print(f"\n  Full report: {display_path}")
    console.print("─" * 37 + "\n")

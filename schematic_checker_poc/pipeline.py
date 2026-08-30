#!/usr/bin/env python3
"""Unified pipeline assembly (Review F1, registry design spec Phase 1).

`run_board()` is the ONE assembly function; `main.py` and (Phase 3) the corpus
driver / recall harness become thin drivers over it. This module OWNS the three
assembly-sequence divergences the driver-agreement recon found latent between the
two hand-maintained assemblies (`FINDINGS.md` §5):

  * **D1 — resolver config.** The canonical, absolute datasheet/parse dirs and the
    recursive PDF finder (what `run_one` monkey-patched and `main.py` never did) are
    the library default here. This FIXES the previously-broken unpatched `main.py`
    (legacy 14-PDF dir + non-recursive finder). `--datasheets-dir` is a real override.
  * **D2 — rail_map.** Sidecar discovery + `--rail-map` override live in the assembly,
    so every driver honours a `<netlist>.rails.json` sidecar (verdict-inert today —
    0 sidecars in corpus — the first sidecar changing behaviour is BY DESIGN).
  * **D3/D4 — passive order.** Passive-bridge propagation runs at ONE fixed point:
    BEFORE step_08 (DECISION D4, `main.py`'s native order — information-superior; the
    signal checker sees passive-derived driver rails).

Failure SEMANTICS are unified here; PRESENTATION stays per-driver (Phase 4, spec §3.4):
`run_board` RETURNS a `BoardOutcome` — the report dict on success, or a typed
`PipelineFailure(kind, stage, detail)` on a fatal step. It never raises or `sys.exit`s
(a `SystemExit` would escape the corpus driver's `except Exception`), so each driver maps
the returned failure its own way (main.py → stderr+exit1; corpus → pipeline_error result
/ --board nonzero exit). Non-fatal step behaviours (step_03 per-part FileNotFoundError, a
non-rail-map step_06 error, a step_07b passive error) are tolerated internally exactly as
`main.py` did. The full driver-divergence table is the comment block below PipelineFailure.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from steps import (
    step_02_parser,
    step_03_resolver,
    step_04b_extractor,
    step_05_validator,
    step_06_power,
    step_07_confirm,
    step_08_checker,          # RailProvenance ctor; the checkers dispatch via checker_registry
    step_10_report,
)
from steps import extractors
from steps import rail_map as rail_map_mod
from steps import passive_traversal
from steps.passive_traversal import Confidence
from steps import checker_registry as reg

logger = logging.getLogger(__name__)

# ── Canonical locations (D1) ────────────────────────────────────────────────────
# Absolute so the library is CWD-independent (main.py may run from anywhere; the
# recon harness ran from a scratch dir with these exact absolute values).
_POC_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _POC_DIR.parent
DEFAULT_DATASHEETS_DIR = str((_PROJECT_ROOT / "netlist_corpus" / "datasheets").resolve())
DEFAULT_PARSED_DIR = str((_POC_DIR / "datasheets_parsed").resolve())
# TODO-386 Phase 3 (S1): the staging tier — same layout, different base, a
# SIBLING of datasheets_parsed/ (never nested inside it: the TODO-320
# production-tree tripwire and every directory-walking census tool root
# themselves at DEFAULT_PARSED_DIR). Gitignored; created lazily on first write.
DEFAULT_STAGING_DIR = str((_POC_DIR / "datasheets_staged").resolve())
# TODO-388 Phase 2 (R-β): where a case-variant stem directory is MOVED when
# _reconcile_mineru_output can neither merge it into the canonical directory
# nor is left holding an empty shell. A third sibling, for the same reason as
# the second — but deliberately NOT added to the TODO-320 tripwire's guarded
# roots (tests/conftest.py), because unlike those two this tree receives
# mid-run moves by design and a "nothing changed" assertion over it would fire
# on correct behaviour.
DEFAULT_QUARANTINE_DIR = str((_POC_DIR / "datasheets_quarantine").resolve())

# TODO-380 Phase 2 (d) / TODO-381 visibility mitigation: a coarse heuristic for
# "this part_number looks like a connector/mechanical part" -- debug-log
# visibility ONLY (see the receiver_components filter below), never a
# behavior gate. Deliberately loose; false negatives just mean an extra
# (harmless) debug line for a genuine connector, never a missed real IC.
_CONNECTOR_LIKE_PART_RE = re.compile(
    r"conn|usb|micro|pin|header|jst|terminal|jack|socket|mount|shield|"
    r"switch|^sw[_-]|hdmi|rj\d|db\d|molex|edge",
    re.IGNORECASE,
)

# Peripheral KB — loaded once at import (mirrors main.py's module-level load). Path
# is PROJECT_ROOT/kb/vendor regardless of CWD.
_KB_ROOT = str(_PROJECT_ROOT / "kb" / "vendor")
try:
    from steps.peripheral_kb import load_peripheral_kb as _load_peripheral_kb
    _PERIPHERAL_KB, _PERIPHERAL_ROUTING = _load_peripheral_kb(_KB_ROOT)
except Exception as _e:  # pragma: no cover - KB present in-repo
    print(f"[STEP 08d] WARNING: Peripheral KB unavailable ({_e}); I2C checks disabled")
    _PERIPHERAL_KB, _PERIPHERAL_ROUTING = {}, {}


def get_peripheral_kb() -> dict:
    """Public accessor for the module-loaded peripheral KB. External consumers (the
    recall harness's swap-detectability probe) must use this instead of reaching a
    private attribute on a driver module — the KB load moved here in Phase 3, and the
    old `run_corpus_test._peripheral_kb` reach silently broke (TODO-189)."""
    return _PERIPHERAL_KB


def get_peripheral_routing() -> dict:
    """Public accessor for the module-loaded peripheral routing map (companion to
    get_peripheral_kb)."""
    return _PERIPHERAL_ROUTING


@dataclass(frozen=True)
class PipelineFailure:
    """Typed, RETURNED (never raised) fatal-step outcome — the other half of
    BoardOutcome (spec §3.4). The pipeline SEMANTICS are unified here; the
    PRESENTATION stays per-driver (see the driver map below) and is now explicit:

      driver              maps PipelineFailure to
      ─────────────────── ─────────────────────────────────────────────────────
      main.py (CLI)       stderr `[STEP {stage}] {detail}` + exit(1)   (unchanged UX)
      corpus full-run     result {status: pipeline_error, failed_at_step: stage} →
                          _write_summary's skipped/pipeline_error counters (unchanged)
      corpus --board      stderr the failure + exit NONZERO  (Phase-4 D7b fix — it
                          used to print `0 PASS 0 WARN 0 FAIL` and exit 0)

    `kind` is the coarse semantic category; `stage` is the step id used for CLI
    presentation (`[STEP {stage}]`); `detail` is the stderr body.
    """
    kind: str      # "parse" | "confirm" | "internal" | "refresh"
    stage: str     # step id, e.g. "02" | "06" | "07" | "08" | "10"
    detail: str    # stderr body, e.g. "ERROR: <msg>" / "RAIL-MAP ERROR: <msg>"


# BoardOutcome = report dict | PipelineFailure  (run_board's return type).

# ─────────────────────────────────────────────────────────────────────────────
# Driver-divergence table (driver-agreement recon FINDINGS.md §5, verbatim). The S
# items are unified in this module; the P items are per-driver BY DESIGN and named
# here so the split is explicit, not accidental:
#
#   Must unify (S): D1 (resolver config — single assembly should own resolver
#     patching), D2 (rail_map — only M honors sidecars/overrides; C silently ignores
#     them), D3 (passive-traversal must sit at ONE fixed point relative to step_08,
#     in both).
#   May stay per-driver (P): D4/D5 (rail_candidates/rail_map_conflicts are a
#     report-enrichment the single-board UX wants and the corpus aggregate doesn't),
#     D6/D7 (abort-vs-skip-vs-capture is a legitimate driver-context choice — BUT the
#     `--board`-swallows-pipeline_error surface (D7b) is a genuine reporting gap worth
#     fixing regardless), D8/D9/D10/D11 (paths, naming, tracing).
#
# Realized: D1/D2/D3 unified in run_board (Phase 1); D4 rail_candidates gated by
# ctx.include_rail_candidates (main True, corpus False); D6/D7 presentation via
# PipelineFailure above; D7b fixed in the corpus --board handler (Phase 4); D11
# tracing threaded via ctx.tracer_net.
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class PipelineContext:
    """Inputs/config + accumulated pipeline state. run_board() populates the
    accumulated fields in place; the Phase-2 checker-registry adapters read them.

    The field set is the union of what the two drivers thread by hand (recon step-1
    static-diff): netlist path + confirm strategy + resolver/rail-map config as
    inputs; ir / confirmed_voltages / pin_specs / seen_parts / provenance maps as
    accumulated state.
    """
    # inputs / config
    netlist_path: str
    skip_confirm: bool = True
    rail_map_path: str | None = None          # D2: --rail-map override
    datasheets_dir: str | None = None         # D1: --datasheets-dir override (None → canonical)
    output_path: str = "report.json"
    include_rail_candidates: bool = True       # D4-P: report enrichment (main-only; corpus sets False)
    tracer_net: str | None = None              # corpus-only --trace-net hook (D11); None = no tracing
    refresh_stems: frozenset = field(default_factory=frozenset)  # d2 --refresh (TODO-367); case-insensitive
    # TODO-386 Phase 3 (S2): staging-tier READ activation. None = defer to the
    # SCHECKER_STAGING env var (i.e. OFF unless set); True/False = explicit
    # --staging override. Writes are unaffected — they always go to staging (R-C).
    staging: bool | None = None
    peripheral_kb: dict | None = None
    peripheral_routing: dict | None = None
    # accumulated during run_board
    ir: object = None
    confirmed_voltages: dict = field(default_factory=dict)
    derived_provenance: dict = field(default_factory=dict)
    rail_provenance: dict = field(default_factory=dict)
    pin_specs: dict = field(default_factory=dict)
    seen_parts: dict = field(default_factory=dict)
    extraction_metadata: dict = field(default_factory=dict)


def _step(n: str, msg: str) -> None:
    print(f"[STEP {n}] ✓ {msg}")


def _format_no_datasheet_warning(part_number: str) -> str:
    """LT-21: the console line for a 'No datasheet found' resolver miss, WITHOUT the
    PDFs-searched directory listing (that stays in the full exception message used
    for resolve_errors/_resolve_err_memo — file-side logging only)."""
    return f"[STEP 03] WARNING: No datasheet found for '{part_number}'."


def _format_short_mpn_warning(values: list[str]) -> str:
    """LT-21: one aggregated line for every too-short-MPN resolver miss, instead of
    one stderr line per generic passive value."""
    return (
        f"[STEP 03] WARNING: {len(values)} components without MPN fields "
        f"({', '.join(values)}) — resolved as generic passives, skipped."
    )


# ── Resolver config (D1) ────────────────────────────────────────────────────────

# L1 family-prefix fallback tier, rule R2 (investigation/experiments/l1_family_tier/
# REPORT.md, Step 6): vendor-scoped so it can never bridge unrelated part families
# (e.g. Winbond W25Q32JV/JW, rejected as R4 in the same design pass) onto the wrong
# datasheet. Fires only after both existing substring passes miss.
# Single definition lives in step_03_resolver (TODO-407) — the PDF-free cache
# lookup (_scan_cache_root) applies the SAME family tier to cache-directory
# names, and two copies of this regex would be free to drift apart. Bound here
# so _find_pdf_recursive below reads unchanged.
_L1_FAMILY_TOKEN_RE = step_03_resolver._L1_FAMILY_TOKEN_RE
_L1_FAMILY_TOKEN_SPLIT_RE = step_03_resolver._L1_FAMILY_TOKEN_SPLIT_RE
_L1_FAMILY_MIN_STEM = step_03_resolver._L1_FAMILY_MIN_STEM


def _find_pdf_recursive(part_number: str) -> str | None:
    """Recursive replacement for step_03_resolver._find_pdf. Walks manufacturer
    subdirs; reads step_03_resolver.DATASHEETS_DIR at call time. The canonical copy:
    run_checks's _init_resolver delegates here via configure_resolver (Phase 3)."""
    pdfs: list[tuple[str, str]] = []
    for dirpath, _, filenames in os.walk(step_03_resolver.DATASHEETS_DIR):
        for f in filenames:
            if f.lower().endswith(".pdf"):
                pdfs.append((f, os.path.join(dirpath, f)))
    base_mpn = step_03_resolver.BASE_SUFFIX_RE.sub("", part_number)
    for name, path in pdfs:
        if base_mpn.lower() in name.lower():
            return path
    for name, path in pdfs:
        if part_number.lower() in name.lower():
            return path
    part_upper = part_number.upper()
    matched_files: set[str] = set()
    for name, path in pdfs:
        stem = os.path.splitext(name)[0]
        for token in _L1_FAMILY_TOKEN_SPLIT_RE.split(stem):
            if (
                len(token) >= _L1_FAMILY_MIN_STEM
                and _L1_FAMILY_TOKEN_RE.match(token)
                and part_upper.startswith(token.upper())
            ):
                matched_files.add(path)
                break
    if len(matched_files) == 1:
        return matched_files.pop()
    return None  # zero or >1 distinct files — refuse rather than guess (permanent)


def configure_resolver(datasheets_dir: str | None = None,
                       staging: bool | None = None) -> None:
    """Set step_03_resolver module state to the canonical config (D1). A provided
    datasheets_dir overrides the canonical netlist_corpus/datasheets default and is
    resolved to absolute; PARSED_DIR and the recursive finder are always canonical.

    TODO-388 Phase 2 (R-β): QUARANTINE_DIR is rebound on the same line of
    reasoning as STAGING_DIR — it is a third CWD-relative module default that
    must become an absolute on every real run, or a reconcile-time move would
    land relative to whatever directory the driver happened to start in.

    TODO-386 Phase 3 (S1): STAGING_DIR is rebound alongside PARSED_DIR, so the
    resolver's CWD-relative module defaults are BOTH replaced by absolutes on
    every real run. `staging` is the S2 activation switch: None leaves the
    resolver's own default in place (which defers to the SCHECKER_STAGING env
    var, i.e. OFF unless set), True/False is an explicit driver override."""
    if datasheets_dir:
        step_03_resolver.DATASHEETS_DIR = str(Path(datasheets_dir).resolve())
    else:
        step_03_resolver.DATASHEETS_DIR = DEFAULT_DATASHEETS_DIR
    step_03_resolver.PARSED_DIR = DEFAULT_PARSED_DIR
    step_03_resolver.STAGING_DIR = DEFAULT_STAGING_DIR
    step_03_resolver.QUARANTINE_DIR = DEFAULT_QUARANTINE_DIR
    if staging is not None:
        step_03_resolver.STAGING_ENABLED = bool(staging)
    step_03_resolver._find_pdf = _find_pdf_recursive


# ── TODO-362 Phase 1: PDF-optional warm path ──────────────────────────────────
#
# See investigation/recon_reports/todo362_pdf_free_phase1_recon.md for the
# call-chain evidence this is built on: on a warm bucket-A (_pin_groups.json)
# cache hit, resolve_and_parse's PDF-find (bucket C, hard-required) and PDF-
# parse (bucket B .md, read unconditionally) are both provably verdict-inert
# — the parsed markdown is discarded unused whenever the cache is warm. This
# section lets that case skip resolve_and_parse entirely instead of paying
# for a find+parse whose result is thrown away.

def _try_warm_cache_shortcut(part_number: str, refresh_wanted: frozenset) -> dict | None:
    """Tried before the unconditional resolve_and_parse call in run_board's
    Step 03 loop. Returns a resolve_and_parse-shaped doc on a HIT, else None
    (never raises) — every MISS reason (MPN too short, no matching cache
    dir, ambiguous cache-dir match, a --refresh-requested stem, or no
    loadable cache) falls through to today's unconditional resolve_and_parse
    call unchanged, exactly as if this function didn't exist (Q5 cold-path
    invariant from the recon).

    On a HIT, still attempts _find_pdf (module-attribute lookup, so it
    respects configure_resolver's recursive-finder swap): if a PDF is found,
    `pdf_path`/`mineru_used` etc. are populated as usual and
    check_cache_provenance runs its normal PDF-present comparison inside
    load_cached_pin_groups. If no PDF is found, `pdf_path` is honestly None
    and `mineru_used` is False — check_cache_provenance takes its new
    pdf_path=None branch (current_no_pdf_verify, trusting the cache's own
    stamped source_hash) rather than being bypassed."""
    part_number = part_number.strip()
    base_pn = step_03_resolver.BASE_SUFFIX_RE.sub("", part_number).strip()
    if len(base_pn) < step_03_resolver.MIN_MPN_LENGTH:
        return None  # let resolve_and_parse raise its normal short-MPN error
    cache_dir = step_03_resolver.find_cached_dir_for_part(part_number)
    if cache_dir is None:
        return None
    stem = cache_dir.name
    if stem in refresh_wanted:
        return None  # --refresh needs a real PDF parse, not a cache read
    pdf_path = step_03_resolver._find_pdf(part_number)
    # The two lookups walk different stores (DATASHEETS_DIR vs PARSED_DIR) via
    # independent substring matches; an MPN that happens to substring-match
    # two different documents across the two stores must not be silently
    # paired. Disagreement -> MISS, falls through to a full resolve_and_parse
    # against the PDF that WAS found, which (re)builds a correctly-paired
    # cache under its own stem.
    if pdf_path is not None and step_03_resolver.canonical_pdf_stem(pdf_path) != stem:
        print(
            f"[STEP 03] PDF-optional cache lookup for {part_number}: matched "
            f"cache dir {stem!r} disagrees with the found PDF's stem "
            f"({step_03_resolver.canonical_pdf_stem(pdf_path)!r}) — falling "
            f"through to full resolve_and_parse.", file=sys.stderr,
        )
        return None
    cached = step_03_resolver.load_cached_pin_groups(pdf_path, stem=stem)
    if not cached:
        return None
    return {
        "part_number": part_number,
        "pdf_path": pdf_path,
        "pdf_stem": stem,
        "markdown_path": None,
        "markdown_text": None,
        "mineru_used": False,
        "placeholder_patches": [],
        "resolver": "l1_local" if pdf_path is not None else "cache_warm_no_pdf",
        "resolved_mpn": None,
    }


def _pdf_stem_for_doc(doc: dict) -> str:
    """doc's own pdf_stem when it came from _try_warm_cache_shortcut
    (pdf_path may be None there); else derive it from pdf_path exactly as
    before (canonical_pdf_stem requires a real path string)."""
    return doc.get("pdf_stem") or step_03_resolver.canonical_pdf_stem(doc["pdf_path"])


def _load_cached_pin_groups_for_doc(doc: dict, pdf_stem: str) -> dict | None:
    """load_cached_pin_groups keyed by pdf_path when one exists — the
    UNCHANGED single-arg call shape, preserving existing test-monkeypatch
    compatibility — else by the doc's own stem when this doc came from
    _try_warm_cache_shortcut with no PDF found (pdf_path is None there)."""
    if doc["pdf_path"] is not None:
        return step_03_resolver.load_cached_pin_groups(doc["pdf_path"])
    return step_03_resolver.load_cached_pin_groups(None, stem=pdf_stem)


# ── Assembly ────────────────────────────────────────────────────────────────────

def run_board(ctx: PipelineContext):
    """Run the full pipeline for one board. Returns a BoardOutcome: the report dict on
    success, or a PipelineFailure on a fatal step (parse / rail-map / confirm /
    signal-check / report). Does NOT raise, sys.exit, or compute an exit code — failure
    PRESENTATION is the driver's job (see PipelineFailure)."""
    configure_resolver(ctx.datasheets_dir, staging=ctx.staging)
    # Resolve the KB into the context so the registry's peripheral adapter reads it.
    ctx.peripheral_kb = ctx.peripheral_kb if ctx.peripheral_kb is not None else _PERIPHERAL_KB
    ctx.peripheral_routing = (
        ctx.peripheral_routing if ctx.peripheral_routing is not None else _PERIPHERAL_ROUTING)

    # Optional per-net execution trace (corpus --trace-net, D11). The driver sets the
    # trace target via trace.writer.set_target and flushes; run_board only emits at the
    # step points below. write_trace is a no-op unless a target is set, so gating on
    # ctx.tracer_net just avoids the per-board drain overhead in the common (no-trace) case.
    _tw = None
    _traced_refdes: set[str] = set()
    if ctx.tracer_net:
        from trace import writer as _tw  # lazy: keep the library's import graph clean

    # Per-step wall-clock timing (TODO-223 jetson attribution). Additive-only: a
    # dict of step_name -> elapsed seconds since the previous mark, attached to
    # the report on success. Not populated on a PipelineFailure return (the
    # driver's existing wall_time_seconds already covers that case).
    step_times: dict[str, float] = {}
    _t_prev = time.monotonic()

    def _mark(step_name: str) -> None:
        nonlocal _t_prev
        now = time.monotonic()
        step_times[step_name] = round(now - _t_prev, 4)
        _t_prev = now

    # ── Step 2: Parse netlist ───────────────────────────────────────────────
    try:
        ir = step_02_parser.parse(ctx.netlist_path)
    except Exception as e:
        return PipelineFailure("parse", "02", f"ERROR: {e}")
    ctx.ir = ir
    _step("02", f"Parsed {len(ir.components)} components, {len(ir.nets)} nets")

    if _tw:
        _traced_net_obj = next((n for n in ir.nets if n.name == ctx.tracer_net), None)
        _traced_refdes = {r for r, _ in _traced_net_obj.pins} if _traced_net_obj else set()
        _tw.write_trace(
            net_name=ctx.tracer_net, step_id="step_02_parser",
            step_display_name="Parser", step_type="deterministic",
            decision_path="parsed_from_nets_section",
            inputs={"netlist_path": Path(ctx.netlist_path).name},
            outputs={"net_count": len(ir.nets), "component_count": len(ir.components)},
            notes=f"Parsed {len(ir.nets)} nets, {len(ir.components)} components",
        )
    _mark("02_parse")

    # ── Step 3: Resolve datasheets ──────────────────────────────────────────
    # Only process components that appear on signal nets (skip pure connectors)
    signal_refdes: set[str] = set()
    for net in ir.nets:
        for refdes, _ in net.pins:
            signal_refdes.add(refdes)

    receiver_components: list[step_02_parser.ComponentIR] = []
    for comp in ir.components:
        if comp.refdes in signal_refdes:
            has_non_power_pin = any(
                not p.pin_name.lower().startswith("vcc")
                and not p.pin_name.lower().startswith("vdd")
                and p.pin_name.lower() not in {"gnd", "vss"}
                for p in comp.pins
            )
            if has_non_power_pin and comp.part_number not in {"Connector_2Pin"}:
                receiver_components.append(comp)
            elif not has_non_power_pin and not _CONNECTOR_LIKE_PART_RE.search(
                    comp.part_number or ""):
                # TODO-380 Phase 2 (d) / TODO-381 visibility mitigation: this
                # part is dropped from resolution entirely (never queued --
                # root cause 2 of the confirmed_local fail-open, TODO-380
                # Phase 1 recon) because every wired pin is power-typed, yet
                # its part_number doesn't look like the connector/mechanical
                # part this filter's own comment assumes (e.g. a 2-pin supply
                # supervisor with only GND+VCC wired -- MAX16150AUT+T /
                # TPS3840PL30DBVR on jetson som_power.net). Debug-only, no
                # behavior change.
                logger.debug(
                    "[STEP 03] %s (%s) dropped from resolution: only "
                    "power-typed pins wired, not a recognized connector "
                    "pattern", comp.refdes, comp.part_number)

    resolved: dict[str, dict | None] = {}          # keyed by refdes
    resolve_errors: dict[str, str] = {}
    resolver_provenance: dict[str, str] = {}       # refdes -> l1_local|l1_gemma|l2_vendor|none
    # Per-board memoization (TODO-226): resolve_and_parse is deterministic in its
    # (stripped) part number — an L1 filename walk + L2 vendor lookup + PDF parse,
    # none of which depend on the refdes. Repeated refdes instances of the same part
    # (jetson boards repeat a part up to 10×; 03_resolve is ~99% of corpus wall time,
    # TODO-224) previously re-did all of it. This LOCAL, per-board cache keyed on
    # part_number.strip() computes each unique part once and reuses the result for its
    # repeat instances. The doc is provably read-only across every downstream consumer
    # (aliasing gate: no consumer mutates it, and it never escapes run_board's scope),
    # so the object is SHARED, not copied. Failures are memoized too, so a repeated
    # unresolvable part doesn't re-hit L2 vendor lookup each instance. Fresh dict per call
    # → no cross-board leakage.
    _resolve_memo: dict[str, dict | None] = {}     # part_number.strip() -> doc | None
    _resolve_err_memo: dict[str, str] = {}         # part_number.strip() -> error string
    # LT-21: too-short-MPN is the high-volume warning class (every generic passive
    # value trips it) — collected here and folded into one summary line after the
    # loop instead of one stderr line per part. `str(e)` is unaffected (still the
    # full message) so resolve_errors/_resolve_err_memo keep their existing content.
    _short_mpn_values: list[str] = []
    # d2 --refresh (TODO-367): case-insensitive against canonical_pdf_stem's
    # lower-cased key. Computed here (moved up from just above the Step 04b
    # loop, TODO-362 Phase 1) so the Step 03 warm-cache shortcut below can
    # refuse a --refresh-requested stem — a refresh needs a real PDF parse,
    # which the shortcut deliberately skips.
    _refresh_wanted = frozenset(s.strip().lower() for s in ctx.refresh_stems)
    for comp in receiver_components:
        part_key = comp.part_number.strip()
        if part_key in _resolve_memo:
            doc = _resolve_memo[part_key]
            resolved[comp.refdes] = doc
            comp.resolved_mpn = doc.get("resolved_mpn") if doc else None
            if doc is not None:
                resolver_provenance[comp.refdes] = doc.get("resolver", "none")
            else:
                resolve_errors[comp.refdes] = _resolve_err_memo[part_key]
                resolver_provenance[comp.refdes] = "none"
            continue

        # TODO-362 Phase 1: PDF-optional warm-cache shortcut, tried before the
        # unconditional resolve_and_parse below. A hit skips resolve_and_parse's
        # PDF-find (bucket C) and PDF-parse (bucket B .md read) entirely.
        warm_doc = _try_warm_cache_shortcut(comp.part_number, _refresh_wanted)
        if warm_doc is not None:
            _resolve_memo[part_key] = warm_doc
            resolved[comp.refdes] = warm_doc
            comp.resolved_mpn = warm_doc.get("resolved_mpn")
            resolver_provenance[comp.refdes] = warm_doc.get("resolver", "none")
            continue

        try:
            doc = step_03_resolver.resolve_and_parse(comp.part_number)
            _resolve_memo[part_key] = doc
            resolved[comp.refdes] = doc
            comp.resolved_mpn = doc.get("resolved_mpn")
            resolver_provenance[comp.refdes] = doc.get("resolver", "none")
        except FileNotFoundError as e:
            msg = str(e)
            if "is too short to be a real MPN" in msg:
                _short_mpn_values.append(part_key)
            elif msg.startswith("No datasheet found for"):
                # LT-21: drop the "PDFs searched: [...]" directory listing from
                # stdout — `msg` (stored below) keeps it for any file-side logging.
                print(_format_no_datasheet_warning(part_key), file=sys.stderr)
            else:
                print(f"[STEP 03] WARNING: {msg}", file=sys.stderr)
            _resolve_memo[part_key] = None
            _resolve_err_memo[part_key] = msg
            resolved[comp.refdes] = None
            resolve_errors[comp.refdes] = msg
            resolver_provenance[comp.refdes] = "none"
    if _short_mpn_values:
        print(_format_short_mpn_warning(_short_mpn_values), file=sys.stderr)
    _step("03", f"Resolved {sum(1 for v in resolved.values() if v is not None)} datasheet(s)")

    if _tw:
        for refdes in _traced_refdes:
            doc = resolved.get(refdes)
            _tw.write_trace(
                net_name=ctx.tracer_net, step_id="step_03_resolver",
                step_display_name="Part resolver", step_type="hybrid",
                decision_path="resolved" if doc else "unresolved",
                inputs={"refdes": refdes, "part_number": next(
                    (c.part_number for c in ir.components if c.refdes == refdes), "?")},
                outputs={"pdf_path": doc.get("pdf_path") if doc else None,
                         "mineru_used": doc.get("mineru_used") if doc else None},
            )
    _mark("03_resolve")

    # ── Step 6: Infer power nets (early — needed to filter supply pins) ─────
    # D2: rail_map sidecar discovery + --rail-map override live here.
    try:
        rail_map = rail_map_mod.load_rail_map(ctx.netlist_path, ctx.rail_map_path)
        power_rails, ground_nets = step_06_power.infer_power_nets(ir, rail_map=rail_map)
    except rail_map_mod.RailMapError as e:
        return PipelineFailure("internal", "06", f"RAIL-MAP ERROR: {e}")
    except Exception as e:
        print(f"[STEP 06] ERROR: {e}", file=sys.stderr)
        power_rails, ground_nets = [], []
    ir.ground_nets = ground_nets
    ir.power_nets = [r.net_name for r in power_rails]
    power_rails_for_confirm = [
        {"net_name": r.net_name, "voltage_v": r.voltage_v,
         "confidence": r.confidence, "source": r.source}
        for r in power_rails
    ]
    # v2_10.4: carry rail-classification provenance (source/confidence) past
    # step_07's flatten so step_08 can calibrate drv_conf.
    ctx.rail_provenance = {
        r.net_name: step_08_checker.RailProvenance(source=r.source, confidence=r.confidence)
        for r in power_rails
    }
    _step("06", f"Inferred {len(power_rails)} power rail(s), {len(ground_nets)} ground net(s)")
    _mark("06_power")

    # ── Step 7: Confirm voltages ────────────────────────────────────────────
    try:
        confirmed_voltages = step_07_confirm.confirm_voltages(
            power_rails_for_confirm, skip=ctx.skip_confirm)
    except Exception as e:
        return PipelineFailure("confirm", "07", f"ERROR: {e}")
    ctx.confirmed_voltages = confirmed_voltages
    _step("07", f"Confirmed voltages: {confirmed_voltages}")
    _mark("07_confirm")

    # ── Step 7b: Passive-bridge rail propagation (D3/D4: BEFORE step_08) ─────
    # Propagate a confirmed rail's voltage across a single series passive
    # (ferrite/0Ω/inductor/diode/small-R) to an aux-supply net that name
    # inference left null — e.g. +5V → ferrite → AVCC. Only voltage-KNOWN rails
    # are passed as the traversal's confirmed set, so name-recognized-but-null
    # rails (AVCC, +1V1 pre-resolve) are themselves eligible derivation targets.
    derived_provenance: dict[str, dict] = {}
    try:
        known_voltages = {n: v for n, v in confirmed_voltages.items() if v is not None}
        derived_by_net, _ = passive_traversal.traverse_passive_bridges(
            ir, known_voltages, ir.ground_nets
        )
        for net, sources in derived_by_net.items():
            if confirmed_voltages.get(net) is not None:
                continue  # already has a real voltage
            if len(sources) != 1:
                continue  # multi-rail / ambiguous source (e.g. battery-OR-USB) — don't assert
            src = sources[0]
            if src.voltage is None or src.confidence != Confidence.HIGH:
                continue  # only lossless bridges (0Ω/ferrite/inductor) merged
            confirmed_voltages[net] = src.voltage
            derived_provenance[net] = {
                "confidence": src.confidence.value,
                "rail": src.rail,
                "voltage": src.voltage,
                "bridge": src.bridge.refdes,
                "bridge_type": src.bridge.bridge_type.value,
            }
    except Exception as e:
        print(f"[STEP 07b] passive-traversal error: {e}", file=sys.stderr)
        derived_provenance = {}
    ctx.derived_provenance = derived_provenance
    if derived_provenance:
        _step("07b", f"Propagated {len(derived_provenance)} rail voltage(s) across passive bridges")
    _mark("07b_passive")

    # ── Steps 4a + 4b + 5: Per-part group extraction (Option E) ─────────────
    pin_specs: dict[tuple[str, str], step_05_validator.ValidatedPinSpec] = {}
    extraction_metadata: dict = {}
    seen_parts: dict[str, dict | None] = {}  # part_number → pin_groups_result
    # d2 --refresh (TODO-367): _refresh_wanted itself now computed above, before
    # the Step 03 loop (TODO-362 Phase 1). _refresh_matched tracks which
    # requested stems were actually seen on this board, so an unmatched
    # (typo'd) name is a clear error below, never a silent no-op.
    _refresh_matched: set[str] = set()
    _stems_seen: set[str] = set()

    for comp in receiver_components:
        part = comp.part_number
        doc = resolved.get(comp.refdes)

        if doc is None:
            if part not in extraction_metadata:
                extraction_metadata[part] = {
                    "pdf_path": None,
                    "resolved": False,
                    "resolve_error": resolve_errors.get(comp.refdes, "Unknown error"),
                }
            continue

        if part not in seen_parts:
            pdf_stem = _pdf_stem_for_doc(doc)
            _stems_seen.add(pdf_stem)
            force_refresh = pdf_stem in _refresh_wanted
            if force_refresh:
                _refresh_matched.add(pdf_stem)
                cached = None  # bypass at load time; save_pin_groups_cache's normal
                               # overwrite path (d3) sidecars whatever was there
            else:
                cached = _load_cached_pin_groups_for_doc(doc, pdf_stem)
            if cached:
                print(f"[STEP 04b] Using cached pin groups for {part}")
                pin_groups_result = cached
            else:
                if force_refresh:
                    print(f"[STEP 04b] --refresh: re-extracting {part} ({pdf_stem})")
                ctx_extract = extractors.PartContext(
                    part_number=part, pdf_path=doc["pdf_path"],
                    markdown_text=doc["markdown_text"])
                pin_groups_result = extractors.run_extractor(
                    extractors.get_extractor(), ctx_extract)
                if pin_groups_result is not None:
                    _step("04b", f"Extracted pin groups for {part}")
                else:
                    print(f"[STEP 04b] extraction produced no pin groups for "
                          f"{part} — part will report UNRESOLVABLE")
            seen_parts[part] = pin_groups_result
        else:
            print(f"[STEP 04b] Reusing pin groups for {part} (instance {comp.refdes})")
            pin_groups_result = seen_parts[part]

        if part not in extraction_metadata:
            validated_groups = step_05_validator.validate_pin_groups(part, pin_groups_result)
            groups_meta = []
            for g in (pin_groups_result or {}).get("pin_groups", []):
                gname = g.get("group_name", "")
                vspec = validated_groups.get(gname)
                groups_meta.append({
                    "group_name": gname,
                    "pin_type": g.get("pin_type"),
                    "applies_to": g.get("applies_to"),
                    "example_pins": g.get("example_pins", []),
                    "VIH_min": g.get("VIH_min"),
                    "absolute_max_voltage": g.get("absolute_max_voltage"),
                    "signal_score": vspec.signal_score if vspec else 0,
                    "confidence": vspec.confidence if vspec else "low",
                })
            extraction_metadata[part] = {
                "pdf_path": doc["pdf_path"],
                "mineru_used": doc["mineru_used"],
                "extraction_method": "whole_part_groups",
                "pin_groups": groups_meta,
                "source_hash": (pin_groups_result or {}).get("provenance", {}).get("source_hash"),
                # TODO-337 Phase 2b-iii: the cited cache's doc-identity verdict,
                # lifted from the SAME provenance block as source_hash above. This
                # is the linkage the evidence-label qualifier reads in
                # step_10_report — the part is already known here, so no cache is
                # re-resolved to find it. None for a pre-2b-ii cache (no key).
                "doc_identity": (pin_groups_result or {}).get("provenance", {}).get("doc_identity"),
                # TODO-368 Phase 2: step_03_resolver.load_cached_pin_groups' captured
                # check_cache_provenance status ("current" | "stale" |
                # "legacy_unverified" | "current_no_pdf_verify"), lifted straight off
                # pin_groups_result (a TOP-LEVEL key, not nested under "provenance" —
                # it is not part of the hash-signed provenance block). None when this
                # part's pin groups came from a fresh extraction this run rather than
                # a cache load (nothing to report a currency status on).
                "cache_currency": (pin_groups_result or {}).get("cache_currency"),
                # TODO-386 Phase 3 (S3-α): which cache tier served this part —
                # "canonical" | "staged", or None for a fresh this-run extraction
                # (nothing was served). Lifted off pin_groups_result exactly as
                # cache_currency above is, and read by step_10_report to stamp
                # the per-finding marker + the report-header staged banner.
                "cache_tier": (pin_groups_result or {}).get("cache_tier"),
                # The cache-dir stem this part resolved to — what the banner
                # enumerates (a stem, not an MPN: the staging tree is keyed by
                # stem, and that is what promote_staged.py operates on).
                "cache_stem": _pdf_stem_for_doc(doc),
            }
            if _tw and comp.refdes in _traced_refdes:
                _cache_hit = bool(_load_cached_pin_groups_for_doc(doc, pdf_stem))
                _tw.write_trace(
                    net_name=ctx.tracer_net, step_id="step_04a_04b_05",
                    step_display_name="Section locator + Extractor + Validator", step_type="llm",
                    decision_path="cache_hit" if _cache_hit else "cache_miss_extracted",
                    inputs={"part_number": part, "pdf_path": doc.get("pdf_path"),
                            "mineru_used": doc.get("mineru_used")},
                    outputs={"groups_extracted": len(groups_meta),
                             "group_names": [g["group_name"] for g in groups_meta]},
                    details={"cache_hit": _cache_hit},
                )

        for pin in comp.pins:
            pin_low = pin.pin_name.lower()
            if (pin_low in {"vdd", "vcc", "gnd", "vss"}
                    or pin_low.startswith("vcc")
                    or pin_low.startswith("vdd")
                    or pin.net in ir.power_nets
                    or pin.net in ir.ground_nets
                    or not pin.net):
                continue

            raw_group = step_04b_extractor.resolve_pin_spec_from_groups(
                pin_id=pin.pin_id,
                pin_name=pin.pin_name,
                pin_groups_result=pin_groups_result,
            )
            if raw_group and raw_group.get("pin_type") != "power":
                spec = step_05_validator.validate(
                    part_number=part,
                    pin_name=pin.pin_name,
                    pin_id=pin.pin_id,
                    VIH_max=None,
                    VIH_min=step_05_validator._to_float(raw_group.get("VIH_min")),
                    absolute_max_voltage=step_05_validator._to_float(
                        raw_group.get("absolute_max_voltage")
                    ),
                    pin_type=raw_group.get("pin_type"),
                    source_snippet=raw_group.get("VIH_source") or "",
                )
                spec.pin_type_exact, spec.open_drain = (
                    step_04b_extractor.pin_match_meta(
                        pin.pin_id, pin.pin_name, pin_groups_result))
                pin_specs[(comp.refdes, pin.pin_name)] = spec

    ctx.pin_specs = pin_specs
    ctx.seen_parts = seen_parts
    ctx.extraction_metadata = extraction_metadata

    # d2 --refresh (TODO-367): a named stem that never matched any part's PDF on
    # this board is a clear error, not a silent no-op (a typo'd --refresh value
    # would otherwise just run a normal cache-hit board with no visible effect).
    _refresh_unknown = sorted(_refresh_wanted - _refresh_matched)
    if _refresh_unknown:
        return PipelineFailure(
            "refresh", "04b",
            f"ERROR: --refresh named stem(s) not found on this board: "
            f"{_refresh_unknown}. Known stem(s) on this board: {sorted(_stems_seen)}."
        )

    _step("04b+05", f"Extracted and validated specs for {len(pin_specs)} pin(s)")
    _mark("04b_05_extract")

    # ── Steps 8/8b/8c/8d/8e: dispatch via the checker registry ──────────────
    # The registry adapters call the same checker functions with the same args as
    # the hand-wired calls did (verified byte-identical by the golden gate); the
    # signal checker keeps its fatal-error guard, the rest run unguarded as before.
    try:
        results = reg.BY_STEP["08"].run(ctx)
    except Exception as e:
        return PipelineFailure("internal", "08", f"ERROR: {e}")
    fail_count = sum(1 for r in results if r.status == "FAIL")
    _step("08", f"Checked {len(results)} net(s) — {fail_count} FAIL(s)")
    _mark("08_checker")

    # ── Step 8b: Check supply voltages ──────────────────────────────────────
    supply_results = reg.BY_STEP["08b"].run(ctx)
    s_pass = sum(1 for r in supply_results if r.status == "PASS")
    s_warn = sum(1 for r in supply_results if r.status == "WARN")
    s_fail = sum(1 for r in supply_results if r.status == "FAIL")
    s_unres = sum(1 for r in supply_results if r.status == "UNRESOLVABLE")
    print(f"[STEP 08b] Supply checks: {s_pass} PASS  {s_warn} WARN  {s_fail} FAIL  {s_unres} UNRESOLVABLE")
    _mark("08b_supply")

    # ── Step 8c: Structural integrity checks ───────────────────────────────
    structural_results = reg.BY_STEP["08c"].run(ctx)
    struct_pass = sum(1 for r in structural_results if r.status == "PASS")
    struct_warn = sum(1 for r in structural_results if r.status == "WARN")
    struct_fail = sum(1 for r in structural_results if r.status == "FAIL")
    print(f"[STEP 08c] Structural checks: {struct_pass} PASS  {struct_warn} WARN  {struct_fail} FAIL")
    _mark("08c_structural")

    # ── Step 8d: I2C peripheral checks ─────────────────────────────────────
    peripheral_results = reg.BY_STEP["08d"].run(ctx)
    p_pass = sum(1 for r in peripheral_results if r.severity.value == "PASS")
    p_warn = sum(1 for r in peripheral_results if r.severity.value == "WARN")
    p_fail = sum(1 for r in peripheral_results if r.severity.value == "FAIL")
    p_unres = sum(1 for r in peripheral_results if r.severity.value == "UNRESOLVABLE")
    print(f"[STEP 08d] Peripheral checks: {p_pass} PASS  {p_warn} WARN  {p_fail} FAIL  {p_unres} UNRESOLVABLE")
    _mark("08d_peripheral")

    # ── Step 8e: I2C pull-up VALUE-range checks (M4-pullup, VERDICT_MOVING) ──
    pullup_results = reg.BY_STEP["08e"].run(ctx)
    pu_fail = sum(1 for r in pullup_results if r.severity.value == "FAIL")
    pu_warn = sum(1 for r in pullup_results if r.severity.value == "WARN")
    pu_unres = sum(1 for r in pullup_results if r.severity.value == "UNRESOLVABLE")
    print(f"[STEP 08e] Pull-up value checks: {pu_fail} FAIL  {pu_warn} WARN  {pu_unres} UNRESOLVABLE")
    _mark("08e_pullup")

    # ── Step 8f: M2 output-conflict checks (VERDICT_MOVING, FAIL-only) ──────
    output_conflict_results = reg.BY_STEP["08f"].run(ctx)
    oc_fail = sum(1 for r in output_conflict_results if r.status == "FAIL")
    print(f"[STEP 08f] Output-conflict checks: {oc_fail} FAIL")
    _mark("08f_output_conflict")

    # ── Step 8g: pull-up PRESENCE checks (Todo 243+212, VERDICT_MOVING, WARN-only) ──
    pullup_presence_results = reg.BY_STEP["08g"].run(ctx)
    pp_warn = sum(1 for r in pullup_presence_results if r.severity == "WARN")
    print(f"[STEP 08g] Pull-up presence checks: {pp_warn} WARN")
    _mark("08g_pullup_presence")

    # ── TODO-134 V3 hook: full rail-candidate artifact (D4-P, main-only) ────
    rail_candidates = None
    if ctx.include_rail_candidates:
        _cand_seen = set()
        rail_candidates = []
        for c in getattr(ir, "rail_candidates_step06", []) or []:
            if c["net"] not in _cand_seen:
                _cand_seen.add(c["net"])
                rail_candidates.append({**c, "source": "step06_unclassified"})
        for r in structural_results:
            if r.status == "WARN" and r.connected_net not in _cand_seen:
                _cand_seen.add(r.connected_net)
                rail_candidates.append({
                    "net": r.connected_net, "refdes": [r.refdes],
                    "inferred_voltage": None, "source": "step08c_warn",
                    "pin": f"{r.pin_name} ({r.refdes})"})

    # ── Step 10: Build report ───────────────────────────────────────────────
    try:
        report = step_10_report.build_report(
            source_netlist=ctx.netlist_path,
            results=results,
            confirmed_voltages=confirmed_voltages,
            extraction_metadata=extraction_metadata,
            output_path=ctx.output_path,
            components=ir.components,
            supply_results=supply_results,
            structural_results=structural_results,
            peripheral_results=peripheral_results,
            pullup_results=pullup_results,
            output_conflict_results=output_conflict_results,
            pullup_presence_results=pullup_presence_results,
            rail_candidates=rail_candidates,
            rail_map_conflicts=getattr(ir, "rail_map_conflicts", None),
        )
    except Exception as e:
        return PipelineFailure("internal", "10", f"ERROR: {e}")
    _mark("10_report")
    report["step_times_seconds"] = step_times

    # TODO-403: per-finding supply evidence, sourced ONLY from the built
    # report's already-tiered "power_supply_results" (evidence_label has been
    # through step_10_report's _apply_tier_or_never_cached/_rewrite_for_tier
    # by this point) — never from the raw `supply_results` list above, whose
    # .evidence_label is still the pre-tier "Confirmed — " string every
    # non-UNRESOLVABLE finding gets unconditionally from
    # step_08b_supply_checker.evaluate_supply (D2 shape, TODO-398), regardless
    # of whether the cache backing it is actually locally verified. The
    # RESULT box is printed from inside build_report() itself
    # (step_10_report._print_summary, called at step_10_report.py:755, before
    # build_report returns) rather than from this file, so per TODO-403 Phase
    # 1 recon P1 this block runs after build_report() returns instead of
    # immediately before that box. Plain print()/stdout, matching :808's
    # style — not rich `console`.
    print("[STEP 08b] Supply evidence:")
    power_supply_results = report.get("power_supply_results") or []
    if not power_supply_results:
        print("  (none)")
    else:
        for entry in power_supply_results:
            ident_parts = [
                str(entry[key]) for key in ("refdes", "part_number")
                if entry.get(key)
            ]
            # TODO-403 follow-up: thread the supply pin into the identity
            # (refdes+part_number+connected_net alone collide across a
            # multi-pin supply group, e.g. STM32F103C8T6's four +3.3V VDD
            # pins on U2 above) — supply_pin already renders as
            # "NAME (pin N)". Falls back to the plain connected_net shape
            # exactly when supply_pin is absent.
            if entry.get("supply_pin"):
                ident_parts.append(str(entry["supply_pin"]))
                if entry.get("connected_net"):
                    ident_parts.append("<-")
                    ident_parts.append(str(entry["connected_net"]))
            elif entry.get("connected_net"):
                ident_parts.append(str(entry["connected_net"]))
            ident = " ".join(ident_parts)
            print(f"  {entry['status']:<12}  {ident}  —  {entry['evidence_label']}")

    if _tw:
        for r in results:
            if r.net_name == ctx.tracer_net and r.status in ("FAIL", "WARN", "UNRESOLVABLE"):
                _tw.write_trace(
                    net_name=ctx.tracer_net, step_id="step_10_report",
                    step_display_name="Report builder", step_type="hybrid",
                    decision_path="explanation_for_" + r.status.lower(),
                    inputs={"status": r.status, "driver_voltage": r.driver_voltage,
                            "receiver_abs_max": r.receiver_abs_max,
                            "evidence_label": r.evidence_label},
                    outputs={"report_path": ctx.output_path},
                    notes="See report JSON for full explanation text",
                )
        # NOTE: flush + the [TRACE] print stay in the corpus driver (run_one) — the
        # trace SINK (file, run_label) is a corpus concern; run_board only emits records.

    return report


# ── Verdict classification (relocated from run_corpus_test.classify_netlist_status) ──

def classify_report(report: dict) -> str:
    """Board-verdict status, registry-driven. Reproduces the old
    ``run_corpus_test.classify_netlist_status`` VERBATIM (proven by the fixture-matrix
    test), but reads the VERDICT_MOVING checkers' report buckets instead of the
    summary sub-dicts, so a checker's verdict participation is declared once (in the
    registry) rather than hand-listed here.

    Status values / precedence (as executed — NOT the spec's linear prose):
      pipeline_error  → set when the pipeline raised
      no_checkable_nets → every VERDICT_MOVING bucket empty (08e joined this set in TODO-164)
      then, over all VERDICT_MOVING results: FAIL > WARN > PASS > UNRESOLVABLE
      (i.e. `all_pass` outranks `has_unresolvable_only`; a board with a PASS and an
      UNRESOLVABLE classifies all_pass — matching the legacy function, whose branch
      order checks passes before unres).
    """
    if report.get("pipeline_error"):
        return "pipeline_error"

    statuses: list[str] = []
    for spec in reg.VERDICT_MOVING:
        bucket = report.get(spec.report_bucket) or []
        statuses.extend(spec.extract_statuses(bucket))

    if not statuses:               # no VERDICT_MOVING results at all
        return "no_checkable_nets"
    if statuses.count("FAIL") > 0:
        return "has_fail"
    if statuses.count("WARN") > 0:
        return "has_warn"
    if statuses.count("PASS") > 0:
        return "all_pass"
    if statuses.count("UNRESOLVABLE") > 0:
        return "has_unresolvable_only"
    return "no_checkable_nets"     # buckets non-empty but no recognized status

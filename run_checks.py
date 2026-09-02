#!/usr/bin/env python3
"""
Corpus test runner for the schematic checker pipeline.

Runs the full pipeline against every eligible netlist in netlist_corpus/,
writing per-netlist report.json files and aggregate stats to corpus_results/.

Usage:
    python run_checks.py
    python run_checks.py --dry-run
    python run_checks.py --workers 2
    python run_checks.py --skip-confirm
"""

import argparse
import csv
import json
import logging
import os
import re
import shutil
import sys
import time
import traceback

import corpus_baseline
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# ── Path setup ───────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent
POC_DIR      = PROJECT_ROOT / "schematic_checker_poc"
sys.path.insert(0, str(POC_DIR))

from steps import (                     # noqa: E402
    # Phase 3: the pipeline assembly (steps 02–10 + the checkers + the KB) now lives in
    # pipeline.run_board; the corpus driver keeps only step_02/step_03 for its own
    # eligibility scan + provenance check.
    step_02_parser,
    step_03_resolver,
)
from steps import checker_registry as _checker_registry  # noqa: E402 (registry-derived per-checker counts, Todo 248)
from pipeline import (  # noqa: E402  (single pipeline assembly, Phase 3)
    run_board as _run_board,
    PipelineContext as _PipelineContext,
    PipelineFailure as _PipelineFailure,
    configure_resolver as _configure_resolver,
    classify_report as _classify_report,
)
from llm import ollama_client            # noqa: E402  (diagnostic log context)
from trace import writer as _trace_writer  # noqa: E402
from llm.ollama_client import enable_tracing as _trace_enable, disable_tracing as _trace_disable  # noqa: E402

# The peripheral KB is now loaded once inside pipeline.py (used by the checker registry's
# step_08d adapter); the corpus driver no longer loads its own copy (Phase 3).

DEFAULT_CORPUS_DIR     = PROJECT_ROOT / "netlist_corpus"
DEFAULT_DATASHEETS_DIR = PROJECT_ROOT / "netlist_corpus" / "datasheets"
DEFAULT_OUTPUT_DIR     = PROJECT_ROOT / "corpus_results"

# Boards skipped during corpus runs. Matched by Path.name.
#
# History: both vme-wren boards were originally skipped as chronic step_06
# Ollama-timeout cases (64+ ambiguous nets in one Gemma call). Fix γ (v2_10.5)
# removed Gemma from step_06 entirely (now fully deterministic, 0 LLM calls), so
# that chronic-timeout cause no longer exists.
#
# io_driver_32x.net was re-evaluated on branch skip-list-reeval-v2-10-5 and
# UNSKIPPED: it now runs end-to-end in 0.4s (step_06 0.0s, 0 Gemma calls),
# 71 PASS / 0 FAIL / 128 UNRESOLVABLE (coverage gap, not defect).
#
# vme-wren.net remains skipped, but for a *different* reason than the original
# step_06 timeout: 73 uncached active parts (incl. a Zynq UltraScale FPGA) mean a
# cold re-eval would be dominated by step_03 datasheet-parse cost (MinerU), not
# step_06. Full re-eval is blocked on a broader datasheet pre-warm strategy
# (TODO-4 territory).
# See investigation/skip_list_reeval_v2_10_5/SUMMARY.md.
CORPUS_SKIP_LIST = {
    "vme-wren.net",
}

logging.basicConfig(level=logging.WARNING, format="%(message)s")

# ── Passive value filter ──────────────────────────────────────────────────────

PASSIVE_VALUE_RE = re.compile(
    r'^(?:'
    r'\d+[pnumkMGTPFHΩRKohm]*'           # numeric: 100n, 10k
    r'|\d+\.\d+[pnumkMGTPFHΩRKohm]*'     # decimal+unit: 2.2uF, 4.7nF, 5.1k, 4.7u, 13.7k
    r'|\d+,\d+[pnumkMGTPFHΩRKohm]*'      # comma-decimal (French locale): 4,7K, 2,2k
    r'|\d+[unmpkKMG]\d+'                  # European spelling: 4u7, 5k1, 1n5, 2u2
    r'|(?:\d+(?:\.\d+)?|\d+,\d+)[pnumkMGTPFHΩRKohm]*@.*'  # trailing-condition: 5pF@1MHz, 100nF@25V
    r'|[RCLD]\d*'                         # bare R, C, L, D
    r'|~'                                 # KiCad no-value
    r'|TestPoint|TESTPAD'
    r'|Fiducial|MountingHole.*'
    r'|Mounting_hole.*'
    r'|PWR_FLAG|VCC|GND|\+3V3|\+5V'
    r'|USB_B_Micro|USB_A|Conn.*'
    r'|Logo.*|DNP'
    r'|CC\d{4}_.*'
    r'|\d+.*?[/\\].*'
    r'|(?:Opened|DNF|NF|NM|TBD)'
    r'|LED[/_].*'
    r'|FB\d+.*'
    r'|NA[\s(_/].*|^NA$'
    r'|[A-Z]\d{4}_.*'
    r'|GRM\d+.*|GRT\d+.*|GRJ\d+.*'
    r'|^LED$|^YELLOW$|^GREEN$|^RED$|^BLUE$|^WHITE$|^ORANGE$'  # bare LED colors
    r'|\d+[kKmM]?Ω?@\d+.*'              # ferrite bead values: 1kΩ@100MHz
    r'|BLM\d+.*|MMZ\d+.*|CBW\d+.*'      # Murata/TDK ferrite bead MPNs
    r'|^DNP$|^DNF$|^NF$|^NM$'           # do-not-populate markers
    r'|IND_.*|IND\d+_.*'
    r'|CAER_.*|CAEM.*_.*|CTEB_.*'
    r'|BOSSARD_.*|HARWIN_.*|SAMTEC_.*'
    r'|FIDUCIAL_.*|SMD_PAD_.*|PLATED_HOLE.*'
    r'|0R\(.*\)'
    r'|Closed\(.*\)|Opened\(.*\)|^Closed$|^Opened$'
    r'|SolderJumper.*|NetTie.*'
    r'|^LED$|^YELLOW-LED$|^GREEN-LED$|^RED-LED$'    # hyphenated LED color variants
    r'|(?:GREEN|YELLOW|RED|BLUE|WHITE|ORANGE)\s+LED$'  # "GREEN LED" spaced variants
    r')$',
    re.IGNORECASE,
)


def is_passive_value(part_number: str) -> bool:
    """Returns True if part_number is a generic schematic value, not a real MPN."""
    return bool(PASSIVE_VALUE_RE.match(part_number.strip()))


# ── Non-IC part classifier ────────────────────────────────────────────────────

PAREN_SUFFIX_RE = re.compile(r'\s*\(.*\)$')
MIN_MPN_LENGTH  = 4

# Patterns that identify non-IC parts — unresolvable instances of these
# are non-blocking and don't affect eligibility
NON_IC_RE = re.compile(
    r'^(?:'
    # Connectors — all forms
    r'Conn.*|.*Connector.*|USB_.*|MISB.*|TFC-.*|WPM.*|'
    r'RJL.*|RJLB.*|RJLD.*|RJLBC.*|'               # RJ45 variants: RJLD, RJLBC
    r'PWRJ.*|PJ-.*|MJ\d+.*|H-WX-.*|'              # barrel jacks: MJ6, H-WX-1x2
    r'BH\d+S?|M\d+-\d+|PPTC.*|'
    r'TB\d+.*|DG\d+.*|'
    r'RA1206.*|'                                    # resistor arrays
    # Crystals — by description AND by MPN prefix
    r'Q\d+MHz.*|Q\d+kHz.*|.*MHz.*ppm.*|.*kHz.*ppm.*|'
    r'Crystal.*|XTAL.*|'
    r'ECS-.*|NDK.*|ABM.*|FA-.*|NX.*|TSX.*|'        # crystal MPN prefixes
    r'ABS\d+.*|CX\d+.*|LFXTAL.*|ASTX.*|'
    r'CSTNE.*|CSTCC.*|CSTCR.*|YIC-.*|'             # Murata CSTNE/CSTCC/CSTCR + Yueqing YIC- resonators
    # LED MPN families (KiCad value field carries vendor MPN, not "LED")
    r'LTST-.*|LiteOn-.*|'
    # Additional discrete TVS / BJT / Schottky families not in BAT/BAS/BC blocks
    r'CD1206-.*|BAR\d+.*|MPSA\d+.*|CGRB\d+.*|PMR\d+EZ.*|'
    # KiCad pseudo-symbols leaking from value field
    r'Header_\d+.*|SW_Push|SW_DIP_.*|'
    # JST-style connector MPNs (parenthesized package codes stripped by PAREN_SUFFIX_RE)
    r'SM\d+B-.*|'
    # Discretes — diodes, transistors
    r'1N\d+.*|2N\d+.*|BAT\d+.*|BAT54.*|SMBJ.*|PESD.*|'
    r'BC\d+.*|IRLML.*|NTR.*|DMP\d+.*|'
    r'HN[12]\w+|'                                    # Hitachi BJT arrays: HN1x19, HN2xxx
    r'BAS.*|BZX.*|MMSZ.*|PRTR.*|'
    r'SD\d+.*|SS\d+.*|'
    r'NCP303.*|BD52.*|'                             # voltage supervisors (simple)
    # Relays
    r'T\d+\d+A.*|.*relay.*|HJR.*|G\d+[A-Z]+.*relay.*|'
    r'T1107.*|T7[VN].*|'                            # Olimex relay MPNs
    # IR receivers
    r'VS\d+B.*|IT-\d+.*|TS\d+-\d+.*|'
    # Mechanical
    r'.*Fiducial.*|.*MountingHole.*|.*TestPoint.*|.*Logo.*|'
    r'T\d+x\d+.*|ERJ.*|CR\d+.*|GZ\d+.*|'
    r'TP_.*|SMD_PAD.*|PLATED_HOLE.*|'
    # Power symbols and test artifacts
    r'VSIN|VPULSE|VPWL|IBIS_.*|'
    r'^_.*|^Test_Symbol.*'
    r')$',
    re.IGNORECASE
)


# Net-label / signal-name leakage from step_02_parser: schematics where the
# component value field carries a net or symbol name instead of an MPN. The
# upstream fix in step_02_parser is tracked separately (Path B); this
# literal denylist is the Path-A safety net so the acquisition list is clean.
NET_LABEL_DENYLIST = {
    "SWD", "RESET", "BOOT0", "DBG_LED", "POW_LED", "AREF_SEL",
    "UBUT", "UBUT_SEL", "VR1_CE", "DEB_EN", "EXP8",
}


def is_non_ic_part(part_number: str) -> bool:
    """
    Returns True if the part is a connector, discrete, crystal,
    or mechanical component that has no meaningful VIH/VIL spec.
    Unresolvable instances of these do not block eligibility.
    """
    stripped = PAREN_SUFFIX_RE.sub("", part_number.strip())
    if stripped.upper() in NET_LABEL_DENYLIST:
        return True
    return bool(NON_IC_RE.match(stripped))


# ── Provenance-based pseudo-part recognizer (eligibility gate only) ────────────
# A real test point / solder jumper / connector / crystal whose VALUE is a signal
# name (e.g. a TestPoint valued "SPI1 MOSI", a SolderJumper valued "SDA", a
# connector valued "CANBUS") reads as a signal-shaped IC under the value-string
# heuristic and wrongly counts as a BLOCKING unresolvable part, inflating the
# eligibility blocking ratio. The distinguishing signal is PROVENANCE — the
# libsource part / footprint / refdes prefix — NOT the value string (keying on the
# value misfires on genuinely oddly-named real ICs like ME6210-SOT89 or BL4054B).
# This recognizes those pseudo-parts so they do not block eligibility. It is used
# ONLY by is_eligible — it does NOT alter the IR consumed by the checkers.
# See investigation/scoping_step02_netlabel_leakage_2026-06-15.md.
_REFDES_PREFIX_RE = re.compile(r'^[A-Za-z#]+')


def pseudo_part_provenance(refdes: str, footprint: str, libsource_part: str) -> str | None:
    """Return the pseudo-part class (testpoint/jumper/connector/crystal/switch/
    buzzer/mounting) inferred from PROVENANCE, else None for a genuine IC.

    Keyed on libsource part / footprint / refdes prefix, never the value string.
    """
    ref = refdes or ""
    m = _REFDES_PREFIX_RE.match(ref)
    refpre = m.group(0) if m else ""
    lp = (libsource_part or "")
    lpl = lp.lower()
    fpl = (footprint or "").lower()

    if lp == "TestPoint" or fpl.startswith("testpoint:") or refpre == "TP":
        return "testpoint"
    if "mountinghole" in lpl or "mountinghole" in fpl or "fiducial" in lpl or "fiducial" in fpl:
        return "mounting"
    if (lpl.startswith("solderjumper") or lpl == "sj" or refpre in ("JP", "SJ")
            or "jumper" in lpl or "solder_jumper" in fpl):
        return "jumper"
    if (refpre in ("J", "P", "CON", "CAN", "BH") or "connector" in lpl
            or fpl.startswith("connector:") or re.match(r"(?i)conn?\d", lp)
            or re.match(r"(?i)bh\d", lp) or "_connectors" in fpl or "uext" in fpl):
        return "connector"
    if (refpre in ("Y", "X") or lpl.startswith("crystal") or fpl.startswith("crystal:")
            or lpl.startswith("resonator")):
        return "crystal"
    if refpre == "SW" or "switch" in lpl:
        return "switch"
    if refpre == "BZ" or "buzzer" in lpl:
        return "buzzer"
    return None


def is_pseudo_part_by_provenance(comp) -> bool:
    """True when a ComponentIR is a non-IC pseudo-part by provenance — must not
    count toward the eligibility blocking-unresolvable numerator."""
    return pseudo_part_provenance(
        getattr(comp, "refdes", "") or "",
        getattr(comp, "footprint", "") or "",
        getattr(comp, "libsource_part", "") or "",
    ) is not None


# ── Resolver / CWD setup ──────────────────────────────────────────────────────

def _init_resolver(datasheets_dir_str: str, cwd_str: str) -> None:
    """Set CWD + resolver state for the main process and each pool worker. The
    resolver config (canonical absolute dirs + recursive finder, D1) is now OWNED by
    the library — delegate to pipeline.configure_resolver. Only the chdir remains a
    corpus concern (the CWD-relative L2 vendor-cache CACHE_DIR, TODO-178, and report paths).
    run_board re-applies configure_resolver per call, so this is belt-and-suspenders
    for the eligibility scan that runs before the first run_board."""
    os.chdir(cwd_str)
    _configure_resolver(datasheets_dir_str)


# ── Eligibility check ─────────────────────────────────────────────────────────

def is_eligible(
    netlist_path: Path,
    datasheets_dir: Path,
) -> tuple[bool, list[str], list[str], list[str]]:
    """
    Returns (eligible, resolved_parts, blocking_unresolved, non_blocking_unresolved).

    Eligibility rules:
    1. Netlist must parse without error
    2. At least 1 non-passive part must resolve to a datasheet
    3. No more than 80% of non-passive, non-connector parts are
       blocking-unresolvable (i.e. ICs with real electrical specs)
    """
    try:
        ir = step_02_parser.parse(str(netlist_path))
    except Exception as e:
        return False, [], [f"PARSE_ERROR: {e}"], []

    pdfs: list[Path] = list(datasheets_dir.rglob("*.pdf"))
    resolved:             list[str] = []
    blocking_unresolved:  list[str] = []
    non_blocking_unresolved: list[str] = []
    seen: set[str] = set()

    for comp in ir.components:
        pn = comp.part_number
        if pn in seen:
            continue
        seen.add(pn)

        if is_passive_value(pn):
            continue

        # Provenance pseudo-parts (test points / jumpers / connectors / crystals
        # whose value is a signal name) are non-IC regardless of the value string,
        # so they never block eligibility. Recognized BEFORE the value-string
        # heuristic; a genuine unresolved IC (no such provenance) still blocks.
        non_ic = is_non_ic_part(pn) or is_pseudo_part_by_provenance(comp)

        base_pn = PAREN_SUFFIX_RE.sub("", pn).strip()
        base_pn = step_03_resolver.BASE_SUFFIX_RE.sub("", base_pn).strip()
        match = next(
            (p for p in pdfs
             if len(base_pn) >= MIN_MPN_LENGTH
             and base_pn.lower() in p.stem.lower()),
            None,
        )

        if match:
            resolved.append(pn)
        elif non_ic:
            non_blocking_unresolved.append(pn)
        else:
            blocking_unresolved.append(pn)

    total_ic_parts = len(resolved) + len(blocking_unresolved)

    if len(resolved) == 0:
        return False, resolved, blocking_unresolved, non_blocking_unresolved

    if total_ic_parts > 0 and len(blocking_unresolved) / total_ic_parts > 0.80:
        return False, resolved, blocking_unresolved, non_blocking_unresolved

    return True, resolved, blocking_unresolved, non_blocking_unresolved


# ── Filename derivation ───────────────────────────────────────────────────────

def report_filename(netlist_path: Path, corpus_dir: Path) -> str:
    rel   = netlist_path.relative_to(corpus_dir)
    parts = [p.lower() for p in rel.parts]
    stem  = "__".join(parts)
    return (
        stem
        .replace(".net", "")
        .replace(".kicad_sch", "")
        .replace(".sch", "")
        + ".json"
    )


# ── Status classification ─────────────────────────────────────────────────────

def classify_netlist_status(report: dict) -> str:
    """Board-verdict status. Relocated to ``pipeline.classify_report`` (registry-driven,
    Phase 2); kept here as a stable alias for existing callers. The corpus assembly is
    untouched this phase — ``run_one`` migrates to ``pipeline.run_board`` in Phase 3."""
    return _classify_report(report)


def newest_checker_source_mtime() -> float:
    """Most-recent mtime across the checker source tree.

    Used by ``--resume`` as a freshness guard: a report older than the newest
    source file was produced by code that has since changed, so it must be
    re-run rather than reused. One mtime comparison covers both resume cases —
    a genuine mid-run crash (reports written during the run are newer than the
    code → reused) and a resume after a code edit (stale reports are older than
    the just-edited code → re-run) — and is robust to git-dirty state, unlike a
    git-sha/provenance stamp.
    """
    newest = 0.0
    roots = [POC_DIR / "steps", POC_DIR / "llm", POC_DIR / "trace"]
    for root in roots:
        for py in root.rglob("*.py"):
            try:
                newest = max(newest, py.stat().st_mtime)
            except OSError:
                continue
    # The runner itself, corpus_baseline, and the repo-root role modules.
    for extra in (Path(__file__), PROJECT_ROOT / "corpus_baseline.py",
                  *PROJECT_ROOT.glob("peripheral_*.py")):
        try:
            newest = max(newest, extra.stat().st_mtime)
        except OSError:
            continue
    return newest


def _derive_evidence_tier_counts(summary: dict) -> dict[str, int]:
    """TODO-368 Phase 2 (D368-D): flatten report["summary"]["evidence_tier_checks"]
    (already "_count"-suffixed, step_10_report.build_report) into top-level
    per-netlist stats keys, prefixed so they can't collide with a per-checker
    name from checker_registry.derive_checker_counts. Mirrors that Todo-248
    convention exactly: a key ending in _COUNT_SUFFIXES is picked up by
    corpus_baseline._extract_stats automatically — no corpus_baseline.py edit
    needed for these to enter the baseline compare surface."""
    return {
        f"evidence_tier_{k}": int(v)
        for k, v in (summary.get("evidence_tier_checks") or {}).items()
    }


def result_from_report(report_path: Path) -> dict:
    """Reconstruct a per-netlist ``result`` dict from a saved report JSON.

    Lets ``--resume`` count a skipped board into ``run_results`` (and therefore
    into the summary / baseline comparison) without re-running the pipeline. The
    report's ``summary`` block carries every field ``_record_result`` /
    ``_write_summary`` need; ``wall_time_seconds`` is 0.0 since the board was not
    run this session, and ``resumed`` marks the entry as reused-from-disk.
    """
    report = json.loads(report_path.read_text())
    s = report.get("summary", {})
    stats = {
        "status":             classify_netlist_status(report),
        "fail_count":         s.get("fail", 0),
        "warn_count":         s.get("warn", 0),
        "pass_count":         s.get("pass", 0),
        "unresolvable_count": s.get("unresolvable", 0),
        "nets_checked":       s.get("total_nets_checked", 0),
        "wall_time_seconds":  0.0,
        "step_times_seconds": report.get("step_times_seconds", {}),
        "resumed":            True,
    }
    stats.update(_checker_registry.derive_checker_counts(s))
    stats.update(_derive_evidence_tier_counts(s))
    return stats


# ── Single-netlist pipeline runner ────────────────────────────────────────────

def run_one(
    netlist_path: Path,
    corpus_dir: Path,
    datasheets_dir: Path,
    output_path: Path,
    skip_confirm: bool,
    tracer_net: str | None = None,
) -> dict:
    """Run the full pipeline on one netlist via pipeline.run_board. Returns a result
    dict for the summary/baseline. Never raises — a typed PipelineFailure from run_board
    (or any unexpected exception) is captured and returned as a pipeline_error result
    (the corpus-context failure PRESENTATION, spec §3.4)."""
    original_cwd = os.getcwd()
    t0           = time.monotonic()

    try:
        # CWD is a corpus concern (the CWD-relative L2 vendor-cache CACHE_DIR, TODO-178, and
        # report paths). The resolver config (D1) is owned by the library (run_board).
        os.chdir(str(POC_DIR))

        # Attribute every Gemma call from this board (diagnostic only; inert unless
        # GEMMA_LOG_FILE is set).
        try:
            ollama_client.set_log_context(
                board=str(netlist_path.relative_to(corpus_dir)))
        except ValueError:
            ollama_client.set_log_context(board=netlist_path.name)

        # ── One assembly for both drivers (Phase 3) ──────────────────────────
        output_path.parent.mkdir(parents=True, exist_ok=True)
        ctx = _PipelineContext(
            netlist_path=str(netlist_path),
            skip_confirm=skip_confirm,
            datasheets_dir=str(datasheets_dir),
            output_path=str(output_path),
            include_rail_candidates=False,   # D4-P: rail_candidates is a main-only enrichment
            tracer_net=tracer_net,
        )
        outcome = _run_board(ctx)

        # Typed failure PRESENTATION for the corpus context (spec §3.4): a fatal step is
        # a pipeline_error result → _write_summary's pipeline_error/skip counters.
        if isinstance(outcome, _PipelineFailure):
            return _pipeline_error_result(f"step_{outcome.stage}", outcome.detail, t0)
        report = outcome

        # Trace SINK (flush + record count) is a corpus concern; run_board only emits.
        if tracer_net:
            trace_path = _trace_writer.flush(tracer_net)
            if trace_path:
                print(f"[TRACE] {len(_trace_writer._records)} records → {trace_path}")

        return _result_from_report_dict(report, t0)

    except Exception as e:  # unexpected bug in the assembly (not a typed PipelineFailure)
        return _pipeline_error_result("run_board", str(e), t0)
    finally:
        os.chdir(original_cwd)


def _result_from_report_dict(report: dict, t0: float) -> dict:
    """Build the summary/baseline result dict from a fresh in-memory report. Counts come
    from the report summary; status from classify_report(report). build_report keeps the
    summary and the result arrays consistent, so status and counts agree.

    Per-checker pass/warn/fail/unresolvable counts are REGISTRY-DERIVED (Todo 248,
    checker_registry.derive_checker_counts) rather than hand-picked per checker — this
    is what gives step_08e/08f/08g (previously invisible to compare_baseline()) a
    compare-tooling surface with zero edits here when the next checker lands."""
    s = report["summary"]
    stats = {
        "status":             _classify_report(report),
        "fail_count":         s["fail"],
        "warn_count":         s["warn"],
        "pass_count":         s["pass"],
        "unresolvable_count": s["unresolvable"],
        "nets_checked":       s["total_nets_checked"],
        "wall_time_seconds":  round(time.monotonic() - t0, 1),
        "step_times_seconds": report.get("step_times_seconds", {}),
    }
    stats.update(_checker_registry.derive_checker_counts(s))
    stats.update(_derive_evidence_tier_counts(s))
    return stats


def _format_board_console_summary(result: dict) -> str:
    """--board mode console summary: classify_report()'s aggregate status plus each
    VERDICT_MOVING checker's own P/W/F/U counts, registry-driven — the same per-checker
    surface corpus-mode summary.json's per_netlist entries already carry via
    checker_registry.derive_checker_counts (Todo 248). Fixes the acruxcz case: a board
    can be has_fail (peripheral FAIL) while the signal bucket alone is 0/0/0 — the old
    printer read only fail_count/warn_count/pass_count (the signal bucket) and showed
    "0 FAIL" for a board classify_report calls has_fail."""
    lines = [f"Status: {result['status']}"]
    for spec in _checker_registry.VERDICT_MOVING:
        if spec.summary_key is None:
            # signal (08): its counts ARE the top-level aggregate, not a summary_key sub-dict.
            counts = {
                "pass": result.get("pass_count", 0), "warn": result.get("warn_count", 0),
                "fail": result.get("fail_count", 0), "unresolvable": result.get("unresolvable_count", 0),
            }
        else:
            counts = {
                field: result[f"{spec.name}_{field}"]
                for field in ("pass", "warn", "fail", "unresolvable")
                if f"{spec.name}_{field}" in result
            }
        parts = "  ".join(f"{v} {field.upper()}" for field, v in counts.items())
        lines.append(f"  {spec.name:<16} {parts}")
    return "\n".join(lines)


# ── FINDINGS section (LT-21, --board mode only) ──────────────────────────────
# Presentation only — no verdict logic, no report-schema reads beyond fields
# build_report already emits. (axis label, report bucket key, severity field,
# identifier field, detail-line callback). Order matches _format_board_console_
# summary's per-axis table above.

def _peripheral_detail(r: dict) -> list[str]:
    lines = [r.get("evidence")]
    if r.get("kb_provenance"):
        lines.append(f"KB sources: {', '.join(r['kb_provenance'])}")
    return lines


def _supply_detail(r: dict) -> list[str]:
    return [
        f"{r.get('refdes')} ({r.get('part_number')})  pin={r.get('supply_pin')}  net={r.get('connected_net')}",
        f"{r.get('actual_voltage_v')}V vs rated {r.get('rated_min_v')}V-{r.get('rated_max_v')}V "
        f"(abs max {r.get('rated_abs_max_v')}V)",
        f"evidence: {r.get('evidence_label')}",
        f"confidence: {r.get('confidence')}",
    ]


def _structural_detail(r: dict) -> list[str]:
    lines = [
        f"{r.get('refdes')}  pin={r.get('pin')}  net={r.get('connected_net')}",
        f"evidence: {r.get('evidence_label')}",
    ]
    if r.get("finding_code"):
        lines.append(f"finding_code: {r['finding_code']}")
    return lines


def _signal_detail(r: dict) -> list[str]:
    driver = r.get("driver") or {}
    receiver = r.get("receiver") or {}
    return [
        f"driver={driver.get('refdes')}  receiver={receiver.get('refdes')} "
        f"pin={receiver.get('pin_name')}",
        f"evidence: {r.get('evidence_label')}",
        f"confidence: {r.get('confidence')}",
    ]


def _pullup_value_detail(r: dict) -> list[str]:
    return [
        f"{r.get('refdes')}  pins={r.get('pins')}  {r.get('value_str')} "
        f"({r.get('ohms')}Ω, band={r.get('band')})",
        r.get("evidence"),
    ]


def _output_conflict_detail(r: dict) -> list[str]:
    return [f"drivers={r.get('drivers')}", f"evidence: {r.get('evidence_label')}"]


def _pullup_presence_detail(r: dict) -> list[str]:
    return [f"family={r.get('family')}  pins={r.get('pins')}", r.get("evidence")]


# identifier_field is a callable r -> str (not a bare field name): most axes just look
# up one key, but supply's identity needs three (refdes/pin/net) to distinguish
# multiple UNRESOLVABLE pins on the same component — TODO-417 half 1 (renderer-only;
# the report JSON already carried this, run_checks.py just wasn't reading it, see
# logs/recon_417_428.md Q3).
def _id(field: str):
    return lambda r: r.get(field)


def _supply_id(r: dict) -> str:
    return f"{r.get('refdes')} {r.get('supply_pin')} <- {r.get('connected_net')}"


# (axis_label, report_bucket_key, severity_field, identifier_fn, detail_fn)
_FINDINGS_AXES = (
    ("signal",           "results",                      "status",   _id("net"),    _signal_detail),
    ("supply",           "power_supply_results",         "status",   _supply_id,    _supply_detail),
    ("structural",       "structural_integrity_results", "status",   _id("refdes"), _structural_detail),
    ("peripheral",       "peripheral_integrity_results",  "severity", _id("net"),    _peripheral_detail),
    ("pullup_value",     "pullup_value_results",          "severity", _id("net"),    _pullup_value_detail),
    ("output_conflict",  "output_conflict_results",       "status",   _id("net"),    _output_conflict_detail),
    ("pullup_presence",  "pullup_presence_results",       "severity", _id("net"),    _pullup_presence_detail),
)


def _collect_findings(report: dict, severity: str) -> list[tuple]:
    found = []
    for axis, bucket_key, sev_field, id_field, detail_fn in _FINDINGS_AXES:
        for r in (report.get(bucket_key) or []):
            if r.get(sev_field) == severity:
                found.append((axis, id_field, detail_fn, r))
    return found


def _count_pass(report: dict) -> int:
    """Total VERDICT_MOVING entries at PASS across all axes — never itemized
    individually (see _render_findings), only counted for the hidden-PASS note."""
    n = 0
    for axis, bucket_key, sev_field, id_field, detail_fn in _FINDINGS_AXES:
        for r in (report.get(bucket_key) or []):
            if r.get(sev_field) == "PASS":
                n += 1
    return n


def _unresolvable_line(r: dict) -> str:
    """The cause, then its citation: when the entry carries a structured
    unresolvable_reason (signal axis today), lead with it and keep the evidence
    label as a bracketed source rather than dropping it. Other axes are
    unchanged (evidence, then evidence_label, then unresolvable_reason)."""
    reason = r.get("unresolvable_reason")
    if reason:
        label = r.get("evidence_label")
        return f"{reason} [{label}]".strip() if label else reason.strip()
    text = r.get("evidence") or r.get("evidence_label") or r.get("unresolvable_reason") or ""
    return text.strip()


def _render_findings(report: dict) -> str:
    """Every FAIL and WARN across all axes (evidence-backed), FAILs first; then each
    UNRESOLVABLE entry rendered the same way (header line + the axis's own indented
    detail lines, reusing detail_fn rather than a separate compact format); then, if
    any VERDICT_MOVING entries were PASS and therefore not itemized, one summary line
    with the count. PASS entries are never itemized individually."""
    lines: list[str] = []
    fails = _collect_findings(report, "FAIL")
    warns = _collect_findings(report, "WARN")
    unresolvables = _collect_findings(report, "UNRESOLVABLE")

    if fails or warns:
        lines.append("FINDINGS")
        for severity, group in (("FAIL", fails), ("WARN", warns)):
            for axis, id_field, detail_fn, r in group:
                lines.append(f"\n  [{severity}] {axis} — {id_field(r)}")
                for detail_line in detail_fn(r):
                    if detail_line:
                        lines.append(f"    {detail_line}")

    if unresolvables:
        lines.append("\nUNRESOLVABLE" if fails or warns else "UNRESOLVABLE")
        for axis, id_field, detail_fn, r in unresolvables:
            header = f"\n  {axis} {id_field(r)} — {_unresolvable_line(r)}"
            lines.append(header)
            header_text = header.strip()
            # Header already states the axis/id/reason line — drop any detail_fn
            # line that just repeats it verbatim (e.g. peripheral's detail_fn
            # leads with the same evidence string _unresolvable_line already used
            # as the header, since that axis has no separate unresolvable_reason).
            for detail_line in detail_fn(r):
                if detail_line and detail_line.strip() not in header_text:
                    lines.append(f"    {detail_line}")

    n_pass = _count_pass(report)
    if n_pass:
        if lines:
            lines.append("")
        lines.append(f"  ({n_pass} PASS finding(s) not shown — full detail in the JSON report)")

    return "\n".join(lines)


def _pipeline_error_result(failed_at_step: str, error: str, t0: float) -> dict:
    """The corpus-context presentation of a pipeline failure: a result dict with
    status=pipeline_error and zeroed counts (unchanged from the pre-refactor shape)."""
    return {
        "status":                  "pipeline_error",
        "failed_at_step":          failed_at_step,
        "error":                   error,
        "traceback":               traceback.format_exc(),
        "fail_count":              0,
        "warn_count":              0,
        "pass_count":              0,
        "unresolvable_count":      0,
        "nets_checked":            0,
        "peripheral_pass":         0,
        "peripheral_warn":         0,
        "peripheral_fail":         0,
        "peripheral_unresolvable": 0,
        "wall_time_seconds":       round(time.monotonic() - t0, 1),
        "step_times_seconds":      {},
    }


# ── Summary I/O ───────────────────────────────────────────────────────────────

def _write_summary(
    run_results: list[dict],
    skipped_details: list[dict],
    corpus_dir: Path,
    datasheets_dir: Path,
    output_dir: Path,
    total_found: int,
    total_elapsed: float,
) -> None:
    counts = {"all_pass": 0, "has_warn": 0, "has_fail": 0, "has_unresolvable_only": 0, "no_checkable_nets": 0, "pipeline_error": 0}
    for r in run_results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    # Per-step wall-clock aggregate (TODO-223 jetson attribution). Additive-only —
    # sums each named step's seconds across every netlist that produced timing data
    # (pipeline_error / --resume entries contribute an empty dict and are skipped).
    step_times_totals_seconds: dict[str, float] = {}
    for r in run_results:
        for step_name, secs in r.get("step_times_seconds", {}).items():
            step_times_totals_seconds[step_name] = step_times_totals_seconds.get(step_name, 0.0) + secs
    step_times_totals_seconds = {k: round(v, 2) for k, v in step_times_totals_seconds.items()}

    parse_errs   = sum(1 for s in skipped_details if s["reason"] == "parse_error")
    unresolvable = sum(
        1 for s in skipped_details
        if s["reason"] in ("no_resolved_ics", "too_many_blocking_unresolved")
    )
    skipped_corpus_skip = sum(
        1 for s in skipped_details if s["reason"] == "corpus_skip_list"
    )
    ran          = len(run_results)
    completed    = sum(1 for r in run_results if r["status"] != "pipeline_error")
    pipe_errors  = ran - completed

    summary = {
        "generated_at":            datetime.now(timezone.utc).isoformat(),
        "label":                   f"run {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}",
        "corpus_dir":              str(corpus_dir),
        "datasheets_dir":          str(datasheets_dir),
        "total_netlists_found":    total_found,
        "eligible":                ran,
        "skipped_unresolvable":    unresolvable,
        "skipped_parse_error":     parse_errs,
        "skipped_corpus_skip_list": skipped_corpus_skip,
        "ran":                     ran,
        "pipeline_errors":         pipe_errors,
        "completed":               completed,
        "results_by_status":       counts,
        "total_wall_time_seconds": round(total_elapsed, 1),
        "avg_wall_time_seconds":   round(total_elapsed / ran, 1) if ran else 0,
        "step_times_totals_seconds": step_times_totals_seconds,
        "skipped_details":         skipped_details,
        "per_netlist":             run_results,
    }

    with open(output_dir / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    runs_dir = output_dir / "runs"
    runs_dir.mkdir(exist_ok=True)
    existing_runs = sorted(runs_dir.glob("run_*.json"))
    next_num = (int(existing_runs[-1].stem.split("_")[1]) + 1) if existing_runs else 1
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_path = runs_dir / f"run_{next_num:03d}_{run_ts}.json"
    shutil.copy(output_dir / "summary.json", archive_path)
    print(f"Archived run snapshot: {archive_path}")

    with open(output_dir / "summary.csv", "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "netlist", "status", "fail_count", "warn_count", "pass_count",
                "nets_checked", "components", "wall_time_seconds", "report_path",
            ],
        )
        writer.writeheader()
        for r in run_results:
            writer.writerow({
                "netlist":           r.get("netlist", ""),
                "status":            r.get("status", ""),
                "fail_count":        r.get("fail_count", 0),
                "warn_count":        r.get("warn_count", 0),
                "pass_count":        r.get("pass_count", 0),
                "nets_checked":      r.get("nets_checked", 0),
                "components":        ";".join(r.get("components", [])),
                "wall_time_seconds": r.get("wall_time_seconds", 0),
                "report_path":       r.get("report", ""),
            })


# ── Dry-run display ───────────────────────────────────────────────────────────

def _print_netlist_entry(
    netlist_rel: str,
    resolved: list[str],
    blocking_unresolved: list[str],
    non_blocking_unresolved: list[str],
    eligible: bool,
    reason: str = "",
) -> None:
    print(f"  {netlist_rel}")
    if resolved:
        for pn in resolved[:5]:
            print(f"    IC resolved:     {pn} ✓")
        if len(resolved) > 5:
            print(f"    ... and {len(resolved) - 5} more resolved")
    if blocking_unresolved:
        for pn in blocking_unresolved[:3]:
            print(f"    IC unresolved:   {pn} ✗  (will show as UNRESOLVABLE in report)")
        if len(blocking_unresolved) > 3:
            print(f"    ... and {len(blocking_unresolved) - 3} more IC-unresolved")
    if non_blocking_unresolved:
        print(
            f"    Non-IC skipped:  {len(non_blocking_unresolved)} parts "
            f"(connectors/discretes/crystals — no datasheet needed)"
        )
    if eligible:
        print(f"    → ELIGIBLE")
    else:
        print(f"    → SKIPPED ({reason})")


def _print_dry_run(
    eligible_list: list[tuple],
    skipped_list: list[dict],
    corpus_dir: Path,
) -> None:
    if eligible_list:
        print("[DRY RUN] Eligible netlists (would run):")
        for netlist_path, resolved, blocking_unresolved, non_blocking_unresolved in eligible_list:
            rel = str(netlist_path.relative_to(corpus_dir))
            _print_netlist_entry(rel, resolved, blocking_unresolved, non_blocking_unresolved, True)
        print()

    skipped_unresolvable = [
        s for s in skipped_list
        if s["reason"] in ("no_resolved_ics", "too_many_blocking_unresolved")
    ]
    if skipped_unresolvable:
        print("[DRY RUN] Skipped netlists (unresolvable ICs):")
        for entry in skipped_unresolvable:
            _print_netlist_entry(
                entry["netlist"],
                entry.get("resolved_ics", []),
                entry.get("blocking_unresolved", []),
                ["…"] * entry.get("non_blocking_skipped_count", 0),
                False,
                entry["reason"],
            )
        print()

    skipped_parse = [s for s in skipped_list if s["reason"] == "parse_error"]
    if skipped_parse:
        print("[DRY RUN] Skipped netlists (parse error):")
        for entry in skipped_parse:
            print(f"  {entry['netlist']}")
            for msg in entry.get("blocking_unresolved", []):
                print(f"    Error: {msg}")
            print("    → SKIPPED (parse error)")
        print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Corpus test runner for schematic checker")
    parser.add_argument("--corpus-dir",     default=str(DEFAULT_CORPUS_DIR),
                        help="Root of netlist corpus (default: ./netlist_corpus)")
    parser.add_argument("--datasheets-dir", default=str(DEFAULT_DATASHEETS_DIR),
                        help="Datasheets directory (default: ./netlist_corpus/datasheets)")
    parser.add_argument("--output-dir",     default=str(DEFAULT_OUTPUT_DIR),
                        help="Output directory for reports (default: ./corpus_results)")
    parser.add_argument("--dry-run",        action="store_true",
                        help="Show eligible netlists without running the pipeline")
    parser.add_argument("--workers",        type=int, default=1,
                        help="Parallel workers (default: 1, sequential)")
    parser.add_argument("--skip-confirm",   action="store_true",
                        help="Auto-accept inferred voltages (Step 07); required for non-interactive runs")
    parser.add_argument("--resume",         action="store_true",
                        help="Skip netlists whose report file already exists (resume after crash)")
    parser.add_argument("--filter",           default=None,
                        help="Regex filter applied to netlist paths (e.g. 'arduino|vme_interface')")
    parser.add_argument("--manifest",         default=None, metavar="FILE",
                        help="Newline-delimited file of corpus-relative netlist "
                             "paths; restricts the run to exactly those boards")
    parser.add_argument("--save-baseline",   action="store_true",
                        help="Save this run as the new baseline in corpus_results/baselines/")
    parser.add_argument("--compare-against", default=None, metavar="PATH_OR_LATEST",
                        help="Compare results against a saved baseline. "
                             "Use 'latest' for the most recently saved baseline, "
                             "or provide an explicit path. Exits with code 1 if regressions found.")
    parser.add_argument("--trace-net", default=None, metavar="NETNAME",
                        help="Write per-step trace records for this net to "
                             "corpus_results/traces/<run>/<net>.json. "
                             "Strongly recommended to pair with --board.")
    parser.add_argument("--board", default=None, metavar="PATH",
                        help="Run only the specified netlist (path relative to repo root or absolute). "
                             "Skips corpus iteration entirely. Use with --trace-net to debug one net.")
    parser.add_argument("--preprocess-export", action="store_true",
                        help="Run kicad-cli auto-export over --corpus-dir before the eligibility "
                             "scan (materializes .net files for .kicad_sch/.sch sources that lack one).")
    parser.add_argument("--staging", action="store_true",
                        help="Serve pin-group caches from the unpromoted staging tier "
                             "(schematic_checker_poc/datasheets_staged/) on a clean canonical "
                             "MISS. OFF by default — the precision gate and every baseline run "
                             "are staging-blind (R-B). TODO-386.")
    args = parser.parse_args()

    # TODO-386 Phase 3 (S2): exported rather than threaded through
    # PipelineContext because run_one executes in a worker process — the env var
    # is the one activation channel that survives both fork and spawn, and it is
    # what step_03_resolver.staging_enabled() falls back to. Set only when the
    # flag is given, so an unset run is byte-identical to pre-386 behaviour.
    if args.staging:
        os.environ[step_03_resolver.STAGING_ENV_VAR] = "1"
        print("[STAGING] staging-tier cache reads ENABLED for this run "
              "(unpromoted caches may back findings; they are marked "
              "cache_tier=staged).")

    corpus_dir     = Path(args.corpus_dir).resolve()
    datasheets_dir = Path(args.datasheets_dir).resolve()
    output_dir     = Path(args.output_dir).resolve()
    reports_dir    = output_dir / "reports"
    manifest_path  = Path(args.manifest).resolve() if args.manifest else None

    if not corpus_dir.exists():
        # --board runs one explicitly-named netlist and never iterates the corpus,
        # so an absent corpus dir costs it nothing — WARN and continue, the same
        # shape the missing-datasheets-dir case below uses (LT-20). Corpus mode has
        # nothing to walk, so it keeps the hard exit. Without this split the public
        # export's own README demo (a --board invocation) exits 1 on a fresh clone,
        # because netlist_corpus/ is private and never shipped. LT-15 Phase B, D2.
        if args.board:
            print(f"WARN: corpus dir not found: {corpus_dir} — "
                  f"proceeding anyway; --board runs a single named netlist and "
                  f"does not iterate the corpus.", file=sys.stderr)
        else:
            print(f"ERROR: corpus dir not found: {corpus_dir}", file=sys.stderr)
            sys.exit(1)
    if not datasheets_dir.exists():
        print(f"WARN: datasheets dir not found: {datasheets_dir} — "
              f"proceeding with 0 datasheets; datasheet-backed checks will use "
              f"the shipped cache where available, otherwise UNRESOLVABLE.",
              file=sys.stderr)

    if not args.dry_run:
        reports_dir.mkdir(parents=True, exist_ok=True)

    print("─" * 52)
    print("  CORPUS TEST RUNNER")
    print(f"  Corpus:     {corpus_dir}")
    print(f"  Datasheets: {datasheets_dir}")
    print(f"  Output:     {output_dir}")
    print("─" * 52)

    # Init resolver in main process (covers sequential path and dry-run)
    _init_resolver(str(datasheets_dir), str(POC_DIR))

    # ── Single-board mode (--board) ───────────────────────────────────────────
    if args.board:
        # _init_resolver changed CWD to POC_DIR; resolve relative paths from PROJECT_ROOT.
        _raw = Path(args.board)
        board_path = _raw if _raw.is_absolute() else (PROJECT_ROOT / _raw)
        board_path = board_path.resolve()
        if not board_path.exists():
            print(f"ERROR: --board path not found: {board_path}", file=sys.stderr)
            sys.exit(1)
        run_ts     = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        run_label  = f"board_{board_path.stem}_{run_ts}"
        if args.trace_net:
            _trace_writer.set_target(args.trace_net, output_dir, run_label)
            _trace_enable()
            print(f"[TRACE] Tracing net '{args.trace_net}' → corpus_results/traces/{run_label}/")
        elif args.workers > 1 and args.trace_net:
            print("WARNING: --trace-net is disabled when --workers > 1. Use --board to scope tracing.")
        reports_dir.mkdir(parents=True, exist_ok=True)
        out_path = reports_dir / (board_path.stem + ".json")
        result = run_one(board_path, corpus_dir, datasheets_dir, out_path,
                         args.skip_confirm, tracer_net=args.trace_net)
        if args.trace_net:
            _trace_disable()
        # D7b (Phase 4): a pipeline failure must SURFACE and exit nonzero — the old
        # code printed "Result: 0 PASS 0 WARN 0 FAIL" and exit(0), silently swallowing it.
        if result["status"] == "pipeline_error":
            print(f"\nPIPELINE ERROR at {result.get('failed_at_step', '?')}: "
                  f"{result.get('error', '')}", file=sys.stderr)
            sys.exit(1)
        print(f"\n{_format_board_console_summary(result)}")
        print(f"  ({result['wall_time_seconds']}s)")
        with open(out_path) as f:
            _board_report = json.load(f)
        findings_text = _render_findings(_board_report)
        if findings_text:
            print(f"\n{findings_text}")
        sys.exit(0)

    # ── Preprocess (optional) ────────────────────────────────────────────────
    # When --preprocess-export is set, materialize .net files for every
    # .kicad_sch / .sch source under corpus_dir that lacks a sibling .net,
    # so the eligibility scan below picks them up. Default OFF.
    if args.preprocess_export:
        from schematic_checker_poc.preprocess.kicad_cli_export import kicad_cli_export
        kicad_cli_export(corpus_dir)

    # ── Scan ─────────────────────────────────────────────────────────────────
    SCAN_EXTENSIONS = {".net", ".kicad_sch", ".sch"}
    all_netlists = sorted(
        p for p in corpus_dir.rglob("*")
        if p.suffix.lower() in SCAN_EXTENSIONS
        and p.is_file()
        and not str(p).startswith(str(datasheets_dir))
    )

    # ── Eligibility ───────────────────────────────────────────────────────────
    eligible_list: list[tuple[Path, list[str], list[str], list[str]]] = []
    skipped_list:  list[dict] = []

    for netlist_path in all_netlists:
        rel = str(netlist_path.relative_to(corpus_dir))

        if netlist_path.name in CORPUS_SKIP_LIST:
            print(f"[skip] {rel} — CORPUS_SKIP_LIST (see comment at definition / SUMMARY.md)")
            skipped_list.append({
                "netlist":                  rel,
                "reason":                   "corpus_skip_list",
                "resolved_ics":             [],
                "blocking_unresolved":      [],
                "non_blocking_skipped_count": 0,
            })
            continue

        eligible, resolved, blocking_unresolved, non_blocking_unresolved = is_eligible(
            netlist_path, datasheets_dir
        )

        if eligible:
            eligible_list.append((netlist_path, resolved, blocking_unresolved, non_blocking_unresolved))
        else:
            is_parse_error = any(u.startswith("PARSE_ERROR:") for u in blocking_unresolved)
            if is_parse_error:
                reason = "parse_error"
            elif len(resolved) == 0:
                reason = "no_resolved_ics"
            else:
                reason = "too_many_blocking_unresolved"
            skipped_list.append({
                "netlist":                  rel,
                "reason":                   reason,
                "resolved_ics":             resolved,
                "blocking_unresolved":      blocking_unresolved,
                "non_blocking_skipped_count": len(non_blocking_unresolved),
            })

    if args.filter:
        filter_re = re.compile(args.filter, re.IGNORECASE)
        eligible_list = [
            entry for entry in eligible_list
            if filter_re.search(str(entry[0].relative_to(corpus_dir)))
        ]

    if manifest_path:
        wanted = {
            ln.strip()
            for ln in manifest_path.read_text().splitlines()
            if ln.strip()
        }
        eligible_list = [
            entry for entry in eligible_list
            if str(entry[0].relative_to(corpus_dir)) in wanted
        ]
        missing = wanted - {
            str(e[0].relative_to(corpus_dir)) for e in eligible_list
        }
        if missing:
            print(f"WARNING: {len(missing)} manifest board(s) not eligible / "
                  f"not found:", file=sys.stderr)
            for m in sorted(missing):
                print(f"  - {m}", file=sys.stderr)

    parse_errs   = sum(1 for s in skipped_list if s["reason"] == "parse_error")
    unresolvable = sum(
        1 for s in skipped_list
        if s["reason"] in ("no_resolved_ics", "too_many_blocking_unresolved")
    )
    skipped_corpus_skip = sum(
        1 for s in skipped_list if s["reason"] == "corpus_skip_list"
    )

    print(f"[SCAN] Found {len(all_netlists)} netlists")
    print(
        f"[SCAN] Eligible: {len(eligible_list)}  |  "
        f"Skipped (unresolvable ICs): {unresolvable}  |  "
        f"Skipped (parse error): {parse_errs}  |  "
        f"Skipped (skip-list): {skipped_corpus_skip}"
    )
    print()

    # ── Dry run ───────────────────────────────────────────────────────────────
    if args.dry_run:
        _print_dry_run(eligible_list, skipped_list, corpus_dir)
        return

    if not eligible_list:
        print("No eligible netlists found. Nothing to run.")
        return

    # ── Warnings ─────────────────────────────────────────────────────────────
    if not args.skip_confirm:
        print(
            "WARNING: --skip-confirm not set. Step 07 will block waiting for "
            "stdin on every netlist. Use --skip-confirm for non-interactive runs.\n"
        )

    if args.workers > 1:
        print(
            "WARNING: parallel workers share one Ollama instance — "
            "may cause timeout errors on slow hardware.\n"
        )

    # ── Trace setup for full-corpus run ─────────────────────────────────────
    if args.trace_net:
        if args.workers > 1:
            print("WARNING: --trace-net is disabled for parallel workers. "
                  "Use --board --trace-net to scope tracing to one design.\n")
        else:
            _run_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            _run_label = f"run_{_run_ts}"
            _trace_writer.set_target(args.trace_net, output_dir, _run_label)
            _trace_enable()
            print(f"[TRACE] Tracing net '{args.trace_net}' across full corpus "
                  f"→ corpus_results/traces/{_run_label}/\n"
                  "WARNING: full-corpus trace runs for hours and may mix same-named nets "
                  "across different boards. Consider --board to scope to one design.\n")

    # ── Run ───────────────────────────────────────────────────────────────────
    total      = len(eligible_list)
    run_results: list[dict] = []
    t_start    = time.monotonic()

    # --resume freshness guard: a report older than the newest checker source was
    # produced by code that has since changed, so reuse only reports at least as
    # new as the source tree; re-run stale ones (see newest_checker_source_mtime).
    _resume_src_mtime = newest_checker_source_mtime() if args.resume else 0.0

    def _resume_reuse(out_path: Path, rel: str, i: int) -> bool:
        """For --resume: if a fresh report exists, count it from disk and return
        True (skip re-run). Stale/missing reports return False (re-run)."""
        if not (args.resume and out_path.exists()):
            return False
        if out_path.stat().st_mtime < _resume_src_mtime:
            print(f"[{i}/{total}] {rel}  (re-running — report older than checker code)")
            return False
        try:
            result = result_from_report(out_path)
        except (OSError, ValueError) as e:
            print(f"[{i}/{total}] {rel}  (re-running — unreadable report: {e})")
            return False
        # Cache-staleness (stale-report guard): LOG/WARN only on --resume — the
        # mtime guard above already covers code staleness; this surfaces a report
        # built against a since-changed CACHE without changing resume behavior.
        try:
            from provenance import check_report_provenance
            _verdict, _issues = check_report_provenance(
                json.loads(out_path.read_text()), step_03_resolver.PARSED_DIR)
            if _verdict == "stale":
                print(f"    [warn] {rel}: report cache-provenance STALE "
                      f"(parts={[i['part'] for i in _issues][:4]}) — resumed anyway (log-only)")
        except Exception:
            pass
        run_results.append({
            **result,
            "netlist":   rel,
            "report":    str(out_path.relative_to(PROJECT_ROOT)),
            "components": [],
        })
        print(f"[{i}/{total}] {rel}  (resumed from report — "
              f"{result['pass_count']} PASS, {result['warn_count']} WARN, "
              f"{result['fail_count']} FAIL)")
        return True

    def _record_result(result: dict, i: int, netlist_path: Path, parts: list[str]) -> None:
        rel      = str(netlist_path.relative_to(corpus_dir))
        fname    = report_filename(netlist_path, corpus_dir)
        out_path = reports_dir / fname
        result["netlist"]    = rel
        result["report"]     = str(out_path.relative_to(PROJECT_ROOT))
        result["components"] = parts
        run_results.append(result)

        elapsed = result["wall_time_seconds"]
        if result["status"] == "pipeline_error":
            step    = result.get("failed_at_step", "?")
            err_msg = (result.get("error") or "")[:80]
            print(f"[{i}/{total}] {rel}")
            print(f"       ✗ PIPELINE_ERROR at {step}  ({elapsed}s)")
            print(f"         {err_msg}\n")
        else:
            fc, wc, pc = result["fail_count"], result["warn_count"], result["pass_count"]
            print(f"[{i}/{total}] {rel}")
            print(f"       ✓ COMPLETE  —  {pc} PASS, {wc} WARN, {fc} FAIL  ({elapsed}s)\n")

    if args.workers == 1:
        for i, (netlist_path, resolved, blocking_unresolved, _non_blocking) in enumerate(eligible_list, 1):
            rel      = str(netlist_path.relative_to(corpus_dir))
            fname    = report_filename(netlist_path, corpus_dir)
            out_path = reports_dir / fname

            if _resume_reuse(out_path, rel, i):
                continue

            print(f"[{i}/{total}] {rel}")
            print(f"       Components: {', '.join(resolved)}")
            print("       Running pipeline...")
            result = run_one(netlist_path, corpus_dir, datasheets_dir, out_path,
                             args.skip_confirm, tracer_net=args.trace_net)
            _record_result(result, i, netlist_path, resolved)
    else:
        futures: dict = {}
        with ProcessPoolExecutor(
            max_workers=args.workers,
            initializer=_init_resolver,
            initargs=(str(datasheets_dir), str(POC_DIR)),
        ) as pool:
            for i, (netlist_path, resolved, blocking_unresolved, _non_blocking) in enumerate(eligible_list, 1):
                fname    = report_filename(netlist_path, corpus_dir)
                out_path = reports_dir / fname
                if _resume_reuse(out_path, str(netlist_path.relative_to(corpus_dir)), i):
                    continue
                future   = pool.submit(
                    run_one, netlist_path, corpus_dir, datasheets_dir, out_path, args.skip_confirm
                )
                futures[future] = (i, netlist_path, resolved)

            completed_count = 0
            for future in as_completed(futures):
                completed_count += 1
                i, netlist_path, parts = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    result = {
                        "status":            "pipeline_error",
                        "failed_at_step":    "executor",
                        "error":             str(e),
                        "traceback":         traceback.format_exc(),
                        "fail_count":        0,
                        "warn_count":        0,
                        "pass_count":        0,
                        "nets_checked":      0,
                        "wall_time_seconds": 0,
                    }
                _record_result(result, completed_count, netlist_path, parts)

    total_elapsed = time.monotonic() - t_start
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_summary(
        run_results, skipped_list, corpus_dir, datasheets_dir,
        output_dir, len(all_netlists), total_elapsed,
    )

    # Enrich summary.json with git metadata (used by baseline compare).
    summary_path = output_dir / "summary.json"
    try:
        _summary_data = json.loads(summary_path.read_text())
        _sha = corpus_baseline.git_sha(cwd=PROJECT_ROOT)
        if _sha:
            _summary_data["git_sha"]   = _sha
            _summary_data["git_dirty"] = corpus_baseline.git_dirty(cwd=PROJECT_ROOT)
        summary_path.write_text(json.dumps(_summary_data, indent=2))
    except Exception:
        pass  # non-fatal — compare will show sha as "unknown"

    # ── Final banner ──────────────────────────────────────────────────────────
    ran          = len(run_results)
    completed    = sum(1 for r in run_results if r["status"] != "pipeline_error")
    pipe_errors  = ran - completed
    all_pass_ct     = sum(1 for r in run_results if r["status"] == "all_pass")
    has_warn_ct     = sum(1 for r in run_results if r["status"] == "has_warn")
    has_fail_ct     = sum(1 for r in run_results if r["status"] == "has_fail")
    no_checkable_ct = sum(1 for r in run_results if r["status"] == "no_checkable_nets")

    mins, secs  = divmod(int(total_elapsed), 60)
    elapsed_str = f"{mins}m {secs}s" if mins else f"{secs}s"

    print("─" * 52)
    print("  DONE")
    print(f"  Ran: {ran}  |  Completed: {completed}  |  Pipeline errors: {pipe_errors}")
    print(f"  Results: {all_pass_ct} all_pass  |  {has_warn_ct} has_warn  |  {has_fail_ct} has_fail  |  {no_checkable_ct} no_checkable_nets")
    print(f"  Total time: {elapsed_str}")
    print(f"  Summary: {output_dir / 'summary.json'}")
    print(f"           {output_dir / 'summary.csv'}")
    print("─" * 52)

    # ── Baseline save ─────────────────────────────────────────────────────────
    if args.save_baseline:
        saved = corpus_baseline.save_baseline(
            summary_path,
            output_dir / "baselines",
            repo_root=PROJECT_ROOT,
        )
        print(f"  Baseline saved: {saved}")
        print(f"  Latest symlink: {output_dir / 'baselines' / 'latest.json'}")

    # ── Baseline compare ──────────────────────────────────────────────────────
    if args.compare_against:
        baseline_path = corpus_baseline.resolve_baseline_path(
            args.compare_against, output_dir
        )
        if not baseline_path.exists():
            print(f"ERROR: baseline not found: {baseline_path}", file=sys.stderr)
            sys.exit(1)
        print("─" * 52)
        print("  BASELINE COMPARISON")
        print("─" * 52)
        has_regressions = corpus_baseline.compare_baseline(
            summary_path, baseline_path, output_dir
        )
        print("─" * 52)
        if has_regressions:
            sys.exit(1)


if __name__ == "__main__":
    main()

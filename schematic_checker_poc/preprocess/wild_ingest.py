"""
Wild-board ingestion wrapper (TODO-277, v1).

Per-repo root-sheet selection + kicad-cli export for an arbitrary freshly-cloned
external KiCad repo — the gap ``campaign_design_recon/REPORT.md`` §1 named ("nothing
today wraps kicad-cli's mechanics with (a) a root-sheet heuristic for an unfamiliar
repo and (b) a validated single-netlist output"). This is a NEW, independent per-repo
entry point, not a replacement for :mod:`kicad_cli_export`'s corpus-wide batch driver —
that module's un-scoped "export everything, filter downstream" behavior stays correct
for its own purpose (growing the in-corpus backlog); this module's job is the opposite:
pick exactly one correct root per repo, cheaply verified, for wild ingestion. Per that
same report, this module REUSES :mod:`kicad_cli_export`'s subprocess mechanics
(``_run_one``) rather than re-implementing the kicad-cli invocation.

Implements ``investigation/experiments/root_heuristic_scoping/RECON.md`` §4 as the
spec, constrained by three Nelson v1 policy decisions recorded on the TODO-277 card:

  1. **Hard-stop ALL multi-project repos.** RECON §4 step 2.2 proposed auto-picking
     when exactly one candidate wins unanimously on a naming signal (repo-name match
     or ordinal/``main``-token directory naming) — v1 does NOT implement this. Every
     repo with more than one ``.kicad_pro`` hard-stops as MULTI_PROJECT, full stop, no
     naming-signal logic attempted at all. (RECON's own sample found this signal
     inconsistent across only 3 multi-project repos — 2 different, mutually
     incompatible conventions plus one, PumaFPV, resolvable only from README prose —
     not enough evidence to trust any general auto-pick rule.)
  2. **Hard-stop on any within-project filename/topology disagreement** (RECON §4
     step 1.4) — never observed in-sample, but not guessed at if it occurs.
  3. **Legacy pre-v6 EESchema / Eagle-XML ``.sch`` content: detect, skip, tally per
     class — no conversion.** Conversion is TODO-279 (gated), out of scope here.

Root-sheet selection within a single-project repo uses only the two load-bearing
RECON §4 step-1 signals — filename stem-match and Sheetfile topology
cross-validation (8/8 agreement in-sample) — never the advisory ``sheets[0]=="Root"``
label marker (RECON: "advisory only... never gate on this alone", 7/8 present,
1 confirmed harmless counterexample in-sample).

CLI usage (must be invoked as a module — this file uses a relative import to reuse
``kicad_cli_export``'s subprocess primitive, which breaks under direct script
invocation):

    python3 -m schematic_checker_poc.preprocess.wild_ingest \\
        <repo_root> [<repo_root> ...] --output-dir <scratch_dir> [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .kicad_cli_export import EXCLUDE_DIRS as _BASE_EXCLUDE_DIRS
from .kicad_cli_export import _run_one

# Extends kicad_cli_export's exclusion set with .history — a KiCad IDE backup/undo
# directory observed in several wild-board clones (e.g. acruxcz_TRNXSDR-carrier) that
# is not itself part of the project structure.
EXCLUDE_DIRS = _BASE_EXCLUDE_DIRS | {".history"}

# Matches both the modern one-word "Sheetfile" spelling and the legacy two-word
# "Sheet file" spelling (older KiCad versions, e.g. antmicro/snapdragon-845-baseboard,
# TODO-289) — a single optional literal space between "Sheet" and "file", nothing
# looser (no \s* / no other property names).
_SHEETFILE_RE = re.compile(r'\(property\s+"Sheet ?file"\s+"([^"]+)"')
_COMP_RE = re.compile(r'^\s*\(comp\b', re.MULTILINE)
_NET_RE = re.compile(r'^\s*\(net\b', re.MULTILINE)


@dataclass
class RepoClassification:
    """Result of classify_repo(). `status` is one of: CLEAR, MULTI_PROJECT,
    AMBIGUOUS, LEGACY_FORMAT, EAGLE_FORMAT, NO_KICAD_PROJECT."""
    status: str
    reason: str
    root: Path | None = None
    candidates: list = field(default_factory=list)
    tally: dict = field(default_factory=dict)


def _walk_files(root: Path, suffixes: set, exclude_dirs: set = EXCLUDE_DIRS) -> Iterator[Path]:
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
        for name in filenames:
            if Path(name).suffix.lower() in suffixes:
                yield Path(dirpath) / name


def _sniff_sch_format(path: Path) -> str:
    """Classify a .sch/.kicad_sch file's content by its header signature. Returns
    "modern_kicad" | "legacy_eeschema" | "eagle_xml" | "unknown"."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(1000)
    except OSError:
        return "unknown"
    stripped = head.lstrip()
    if stripped.startswith("(kicad_sch"):
        return "modern_kicad"
    if stripped.startswith("EESchema Schematic File Version") or stripped.startswith("EESchema-DOCLIB"):
        return "legacy_eeschema"
    if stripped.startswith("<?xml") and "<eagle" in head.lower():
        return "eagle_xml"
    return "unknown"


def _parse_sheet_refs(path: Path) -> set:
    """Return the bare filenames this .kicad_sch's `(property "Sheetfile" "...")`
    (or legacy two-word `"Sheet file"`, TODO-289) blocks reference. KiCad stores
    sheet children as siblings in the same directory as the referencing file
    (confirmed against every sampled RECON.md repo)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    return {os.path.basename(m) for m in _SHEETFILE_RE.findall(text)}


def _topology_root(kicad_sch_files: list) -> tuple:
    """Return (root_path_or_None, reason). Root is the unique file with zero
    incoming Sheetfile references whose transitive closure covers every file in
    kicad_sch_files. Dedups by FILE IDENTITY, not per-instance reference count —
    RECON's acruxcz finding: `.kicad_pro`'s `sheets` array counts per-placement
    instances (27 for 20 files), never usable as a file/sheet count."""
    by_name = {f.name: f for f in kicad_sch_files}
    refs_by_file = {f: _parse_sheet_refs(f) for f in kicad_sch_files}
    referenced = set().union(*refs_by_file.values()) if refs_by_file else set()

    roots = [f for f in kicad_sch_files if f.name not in referenced]
    if len(roots) != 1:
        return None, "topology found %d file(s) with zero incoming Sheetfile references (need exactly 1): %s" % (
            len(roots), sorted(r.name for r in roots))
    root = roots[0]

    seen = set()
    stack = [root]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for ref_name in refs_by_file.get(cur, set()):
            child = by_name.get(ref_name)
            if child is not None and child not in seen:
                stack.append(child)

    missing = set(kicad_sch_files) - seen
    if missing:
        return None, "topology root %s does not reach %d file(s): %s" % (
            root.name, len(missing), sorted(m.name for m in missing))
    return root, "topology: %s has zero incoming refs and reaches all %d file(s)" % (root.name, len(kicad_sch_files))


def classify_repo(repo_root) -> RepoClassification:
    """Classify a cloned repo per the v1 spec. Never raises; never picks a root when
    the signals disagree or more than one project exists — returns a hard-stop
    status instead (MULTI_PROJECT / AMBIGUOUS / LEGACY_FORMAT / EAGLE_FORMAT)."""
    repo_root = Path(repo_root).resolve()
    kicad_pro_files = sorted(_walk_files(repo_root, {".kicad_pro"}))

    if len(kicad_pro_files) > 1:
        candidates = []
        for kp in kicad_pro_files:
            sch_count = sum(1 for _ in _walk_files(kp.parent, {".kicad_sch"}))
            candidates.append({
                "kicad_pro": str(kp.relative_to(repo_root)),
                "stem": kp.stem,
                "kicad_sch_count": sch_count,
            })
        return RepoClassification(
            status="MULTI_PROJECT",
            reason="%d .kicad_pro files found; v1 policy hard-stops all multi-project "
                   "repos (no naming-signal auto-pick)" % len(kicad_pro_files),
            candidates=candidates,
        )

    if len(kicad_pro_files) == 1:
        kicad_pro = kicad_pro_files[0]
        kicad_sch_files = sorted(_walk_files(kicad_pro.parent, {".kicad_sch"}))
        if not kicad_sch_files:
            return RepoClassification(status="AMBIGUOUS",
                                       reason="no .kicad_sch files found under %s" % kicad_pro.parent)

        stem_matches = [f for f in kicad_sch_files if f.stem == kicad_pro.stem]
        if len(stem_matches) != 1:
            return RepoClassification(
                status="AMBIGUOUS",
                reason="stem-match found %d candidate(s) for .kicad_pro stem %r (need exactly 1): %s" % (
                    len(stem_matches), kicad_pro.stem, sorted(f.name for f in stem_matches)),
            )
        filename_root = stem_matches[0]

        topology_root, topo_reason = _topology_root(kicad_sch_files)
        if topology_root is None:
            return RepoClassification(status="AMBIGUOUS", reason=topo_reason)

        if filename_root != topology_root:
            return RepoClassification(
                status="AMBIGUOUS",
                reason="filename-match root (%s) disagrees with topology root (%s)" % (
                    filename_root.name, topology_root.name),
            )

        fmt = _sniff_sch_format(filename_root)
        if fmt == "legacy_eeschema":
            return RepoClassification(status="LEGACY_FORMAT",
                                       reason="resolved root %s is pre-v6 EESchema format" % filename_root.name)
        if fmt == "eagle_xml":
            return RepoClassification(status="EAGLE_FORMAT",
                                       reason="resolved root %s is Eagle XML format" % filename_root.name)

        return RepoClassification(status="CLEAR", root=filename_root,
                                   reason="filename-match + topology agree: %s" % filename_root.name)

    # len(kicad_pro_files) == 0 — no modern KiCad project. Sniff bare .sch files for
    # the two non-convertible legacy classes; per-class tally regardless of verdict.
    bare_sch_files = sorted(_walk_files(repo_root, {".sch"}))
    if not bare_sch_files:
        return RepoClassification(status="NO_KICAD_PROJECT",
                                   reason="no .kicad_pro and no .sch files found under %s" % repo_root)

    fmt_counts = Counter(_sniff_sch_format(f) for f in bare_sch_files)
    tally = dict(fmt_counts)
    has_legacy = fmt_counts.get("legacy_eeschema", 0) > 0
    has_eagle = fmt_counts.get("eagle_xml", 0) > 0
    if has_legacy and has_eagle:
        return RepoClassification(status="AMBIGUOUS",
                                   reason="mixed legacy_eeschema + eagle_xml .sch files, no .kicad_pro",
                                   tally=tally)
    if has_legacy:
        return RepoClassification(status="LEGACY_FORMAT",
                                   reason="%d pre-v6 EESchema .sch file(s), no .kicad_pro — detect/skip/tally, "
                                          "no conversion (TODO-279, gated)" % fmt_counts["legacy_eeschema"],
                                   tally=tally)
    if has_eagle:
        return RepoClassification(status="EAGLE_FORMAT",
                                   reason="%d Eagle XML .sch file(s), no .kicad_pro — detect/skip/tally, "
                                          "no conversion (TODO-279, gated)" % fmt_counts["eagle_xml"],
                                   tally=tally)
    return RepoClassification(status="NO_KICAD_PROJECT",
                               reason="%d .sch file(s) present but none matched a known legacy/eagle "
                                      "signature" % len(bare_sch_files),
                               tally=tally)


def _count_components_and_nets(net_path) -> tuple:
    try:
        text = Path(net_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, 0
    return len(_COMP_RE.findall(text)), len(_NET_RE.findall(text))


def _passes_gate(comp_count: int, net_count: int) -> bool:
    return comp_count > 0 and net_count > 0


def export_repo(repo_root, output_dir) -> dict:
    """Classify repo_root; if CLEAR, export via kicad-cli and apply the comp>0/net>0
    validation gate. Never raises. Always returns a result dict with a "status" key."""
    repo_root = Path(repo_root).resolve()
    classification = classify_repo(repo_root)
    result = {"repo": str(repo_root), "status": classification.status, "reason": classification.reason}
    if classification.candidates:
        result["candidates"] = classification.candidates
    if classification.tally:
        result["tally"] = classification.tally
    if classification.status != "CLEAR":
        return result

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_net = output_dir / (classification.root.stem + ".net")
    status, elapsed, stderr_tail = _run_one(classification.root, out_net)
    result["kicad_cli_status"] = status
    result["elapsed_seconds"] = round(elapsed, 3)
    if status != "exported":
        result["status"] = "EXPORT_FAILED"
        if stderr_tail:
            result["stderr_tail"] = stderr_tail
        return result

    comp_count, net_count = _count_components_and_nets(out_net)
    result["comp_count"] = comp_count
    result["net_count"] = net_count
    if not _passes_gate(comp_count, net_count):
        result["status"] = "EMPTY_EXPORT"
        return result

    result["status"] = "EXPORTED"
    result["output"] = str(out_net)
    return result


def ingest_repos(repo_roots, output_dir, dry_run: bool = False) -> dict:
    """Run classify (+ export, unless dry_run) over each repo_root. Returns a
    per-run summary dict: {"total", "counts" (per-status tally), "results"}."""
    output_dir = Path(output_dir)
    repo_roots = list(repo_roots)
    results = []
    counts: Counter = Counter()
    for repo_root in repo_roots:
        repo_root = Path(repo_root)
        if dry_run:
            classification = classify_repo(repo_root)
            result = {"repo": str(repo_root), "status": classification.status,
                      "reason": classification.reason}
            if classification.candidates:
                result["candidates"] = classification.candidates
            if classification.tally:
                result["tally"] = classification.tally
        else:
            result = export_repo(repo_root, output_dir / repo_root.name)
        results.append(result)
        counts[result["status"]] += 1
    return {"total": len(repo_roots), "counts": dict(counts), "results": results}


def _cli() -> int:
    p = argparse.ArgumentParser(
        description="Wild-board per-repo root-sheet selection + kicad-cli export (TODO-277 v1).",
    )
    p.add_argument("repo_roots", nargs="+", help="One or more cloned repo root directories.")
    p.add_argument("--output-dir", required=True, help="Scratch dir for exported .net files.")
    p.add_argument("--dry-run", action="store_true", help="Classify only; do not invoke kicad-cli.")
    args = p.parse_args()

    summary = ingest_repos(args.repo_roots, args.output_dir, dry_run=args.dry_run)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())

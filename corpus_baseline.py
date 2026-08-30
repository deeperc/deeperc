"""
Baseline save and comparison for the corpus regression runner.

Public API consumed by run_checks.py:
  git_sha(cwd)            → Optional[str]
  git_dirty(cwd)          → bool
  save_baseline(...)      → Path   (writes baselines/<sha>_<date>.json + latest.json)
  resolve_baseline_path() → Path   (resolves "latest" alias)
  compare_baseline(...)   → bool   (True → regressions found → caller should sys.exit(1))
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ── Git helpers ──────────────────────────────────────────────────────────────

def git_sha(cwd: Optional[Path] = None) -> Optional[str]:
    """Return the short HEAD SHA, or None if git is unavailable."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, cwd=cwd,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def git_dirty(cwd: Optional[Path] = None) -> bool:
    """Return True if the working tree has uncommitted changes."""
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=5, cwd=cwd,
        )
        return bool(r.stdout.strip())
    except Exception:
        return False


# ── Baseline save ────────────────────────────────────────────────────────────

def save_baseline(
    summary_path: Path,
    baselines_dir: Path,
    repo_root: Optional[Path] = None,
) -> Path:
    """
    Copy summary_path into baselines_dir with a stable name and update latest.json.

    Name format:
      baseline_<sha>[_dirty]_<YYYYMMDD>.json   — when git is available
      baseline_nogit_<YYYYMMDD>_<HHMMSS>.json  — fallback
    """
    baselines_dir.mkdir(parents=True, exist_ok=True)

    sha   = git_sha(cwd=repo_root)
    dirty = git_dirty(cwd=repo_root) if sha else False
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")

    if sha:
        dirty_suffix = "-dirty" if dirty else ""
        name = f"baseline_{sha}{dirty_suffix}_{date_str}.json"
        if dirty:
            print(f"WARNING: working tree is dirty; baseline saved as {name}")
    else:
        ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        name = f"baseline_nogit_{ts}.json"
        print(f"WARNING: git not available; baseline saved as {name}")

    dest = baselines_dir / name
    shutil.copy2(summary_path, dest)

    latest = baselines_dir / "latest.json"
    shutil.copy2(summary_path, latest)

    return dest


# ── Path resolver ─────────────────────────────────────────────────────────────

def resolve_baseline_path(path_or_latest: str, output_dir: Path) -> Path:
    """Resolve '--compare-against' argument to a concrete Path."""
    if path_or_latest == "latest":
        return output_dir / "baselines" / "latest.json"
    return Path(path_or_latest)


# ── Per-netlist stat extraction ───────────────────────────────────────────────
#
# Todo 248 (compare_staleness_recon, ID-247): the old fixed _NUMERIC_STAT_KEYS
# allowlist silently dropped any key it didn't name (peripheral_unresolvable /
# supply_unresolvable were computed but never consulted — the dcdc.net miss) and
# had no way to see a new checker's counts (step_08e/08f/08g) without a hand
# edit here every time. Replaced with a naming CONVENTION: any per-netlist key
# ending in one of _COUNT_SUFFIXES is a verdict-relevant count and is retained;
# `_polarity()` (below) decides how each one votes, again by convention, not by
# a hand-maintained list. A checker gains full compare-tooling visibility the
# moment its report flattens into a `{name}_{pass|warn|fail|unresolvable}` key
# (see steps.checker_registry.derive_checker_counts) — zero edits needed here.
_COUNT_SUFFIXES = ("_pass", "_warn", "_fail", "_unresolvable", "_count")

# The four whole-board net-level aggregates (satisfy pass+warn+fail+unresolvable
# == nets_checked for the "signal"/step_08 checker) get individual polarity;
# every other counted key is classified purely by suffix.
_BASE_POLARITY = {
    "pass_count":         "pass",
    "warn_count":         "fail_warn",
    "fail_count":         "fail_warn",
    "unresolvable_count": "unresolvable_agg",   # net-level aggregate, offset rule below
}


def _polarity(key: str) -> Optional[str]:
    """How a count key votes: 'pass' (increase=improvement), 'fail_warn' (increase=
    regression, verdict-severity), 'unresolvable' (increase=regression, coverage —
    tagged distinctly in the output), 'unresolvable_agg' (the net-level aggregate,
    handled separately), or None (present but not consulted by classification —
    surfaces via the residual-delta invariant instead of being silently dropped)."""
    if key in _BASE_POLARITY:
        return _BASE_POLARITY[key]
    if key.endswith("_pass"):
        return "pass"
    if key.endswith("_warn") or key.endswith("_fail"):
        return "fail_warn"
    if key.endswith("_unresolvable"):
        return "unresolvable"
    return None


def _extract_stats(entry: dict) -> dict:
    stats = {
        k: int(v) for k, v in entry.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
        and k.endswith(_COUNT_SUFFIXES)
    }
    for k in _BASE_POLARITY:
        stats.setdefault(k, 0)
    stats["status"] = entry.get("status", "unknown")
    return stats


# ── Comparison logic ──────────────────────────────────────────────────────────

# Verdict-status severity on the FAIL/WARN axis, mirroring pipeline.classify_report's
# precedence (FAIL > WARN > everything else). The board `status` IS the registry-derived
# verdict — classify_report iterates the checker registry's VERDICT_MOVING specs — so
# scoring its transition covers EVERY verdict-moving checker, including dimensions the
# per-netlist summary doesn't break out into granular counts. This is the derive-don't-
# maintain closure of the ID-95-class blind spot: a future verdict_role promotion is
# scored here with ZERO comparator edits. Non-warn/fail statuses (all_pass,
# has_unresolvable_only, no_checkable_nets, pipeline_error) map to 0 — coverage/
# unresolvable moves among those stay governed by the granular unresolvable_count
# logic, so they are not double-counted.
_STATUS_SEVERITY = {"has_fail": 2, "has_warn": 1}


def _classify_diff(before: dict, after: dict) -> tuple[str, dict, dict]:
    """
    Classify one netlist as unchanged / improved / regressed / mixed.
    Returns (category, non-zero-deltas dict, residual_deltas dict).

    residual_deltas holds every non-zero delta that was computed but NOT consulted
    by the improved/regressed vote below — populated UNCONDITIONALLY, including when
    the netlist classifies "unchanged". This is the permanent audit net for the
    ID-247 bug class (a real movement silently invisible to the bucket counts): any
    future key that doesn't fit the _polarity() convention still surfaces here
    instead of vanishing.
    """
    deltas: dict[str, int] = {}
    for key in sorted(set(before) | set(after)):
        if key == "status":
            continue
        d = int(after.get(key, 0)) - int(before.get(key, 0))
        if d:
            deltas[key] = d

    # A board that previously errored out (0 checks of every kind) now running
    # looks like a fail/pass increase against the baseline's zeros — that is the
    # board becoming evaluable, not a real regression/improvement. Don't score
    # deltas measured off a pipeline_error baseline (CLAUDE.md deferred note).
    if before.get("status") == "pipeline_error":
        return "unchanged", deltas, dict(deltas)

    improved  = False
    regressed = False
    scored: set[str] = set()

    for key, d in deltas.items():
        pol = _polarity(key)
        if pol == "pass":
            scored.add(key)
            if d > 0:
                improved = True
            elif d < 0:
                regressed = True
        elif pol == "fail_warn":
            scored.add(key)
            if d < 0:
                improved = True
            elif d > 0:
                regressed = True
        elif pol == "unresolvable":
            # peripheral_unresolvable / supply_unresolvable (Todo 248 fix): same
            # decreasing=improvement / increasing=regression polarity as fail_warn,
            # but tagged distinctly (coverage_deltas) — see compare_baseline().
            scored.add(key)
            if d < 0:
                improved = True
            elif d > 0:
                regressed = True

    # Net-level aggregate unresolvable_count: decrease = improvement; increase =
    # regression UNLESS fully offset by a matching pass_count increase (an
    # UNRESOLVABLE→PASS transition on the same nets).
    if "unresolvable_count" in deltas:
        scored.add("unresolvable_count")
        unres_d = deltas["unresolvable_count"]
        if unres_d < 0:
            improved = True
        elif unres_d > 0:
            pass_d = deltas.get("pass_count", 0)
            if pass_d < unres_d:
                regressed = True
            # else: offset — don't flag as regression

    # Registry-derived verdict-status transition (the ID-95-class blind-spot closer).
    # A board whose classify_report verdict worsens on the FAIL/WARN axis regressed;
    # one that improves, improved — catching verdict moves driven by a dimension that
    # has no granular summary key. Recorded as a `status_severity` delta so a
    # pure-status move is visible in the JSON entry, not just the numeric deltas.
    sev_d = _STATUS_SEVERITY.get(after.get("status", ""), 0) \
        - _STATUS_SEVERITY.get(before.get("status", ""), 0)
    if sev_d:
        deltas["status_severity"] = sev_d
        scored.add("status_severity")
        if sev_d > 0:
            regressed = True
        else:
            improved = True

    residual = {k: v for k, v in deltas.items() if k not in scored}

    if improved and regressed:
        cat = "mixed"
    elif improved:
        cat = "improved"
    elif regressed:
        cat = "regressed"
    else:
        cat = "unchanged"
    return cat, deltas, residual


def _coverage_deltas(deltas: dict) -> dict:
    """The subset of `deltas` that are coverage movements (peripheral_unresolvable /
    supply_unresolvable), tagged distinctly from verdict-severity (fail/warn) moves —
    per Todo 248's design (both share polarity, but are a different KIND of movement:
    a resolvable-vs-not classification does not necessarily change the board verdict)."""
    return {k: v for k, v in deltas.items() if _polarity(k) == "unresolvable"}


# ── Human-readable diff entry ─────────────────────────────────────────────────

def _print_diff_entry(entry: dict) -> None:
    b, a = entry["before"], entry["after"]
    print(f"  {entry['netlist']}:")
    if b.get("status") != a.get("status"):
        print(f"    status: {b.get('status')} → {a.get('status')}")
    for key, d in sorted(entry.get("deltas", {}).items()):
        sign = "+" if d > 0 else ""
        print(f"    {key}: {sign}{d}")


# ── Main compare function ─────────────────────────────────────────────────────

def _compact_timestamp(ts: str) -> str:
    """A filesystem-safe, sortable stamp from an ISO timestamp (or any string)."""
    try:
        return datetime.fromisoformat(ts).strftime("%Y%m%dT%H%M%S")
    except (ValueError, TypeError):
        cleaned = re.sub(r"[^0-9A-Za-z]+", "", str(ts))
        return cleaned or "unknown"


def compare_baseline(
    current_summary_path: Path,
    baseline_path: Path,
    output_dir: Path,
) -> bool:
    """
    Diff current_summary_path against baseline_path.

    Prints a human-readable summary and writes:
      - output_dir/comparisons/compare_<baselineSHA>_vs_<runid>.json  (retained, one
        file per compare — the primary artifact)
      - output_dir/baseline_comparison.json  (fixed-path compatibility copy, same
        content, overwritten every call — existing callers/tests read this)
    Returns True if any regressions (or mixed) were found — caller should sys.exit(1).
    """
    current  = json.loads(current_summary_path.read_text())
    baseline = json.loads(baseline_path.read_text())

    cur_by_nl  = {e["netlist"]: _extract_stats(e) for e in current.get("per_netlist", [])}
    base_by_nl = {e["netlist"]: _extract_stats(e) for e in baseline.get("per_netlist", [])}

    all_netlists = sorted(set(cur_by_nl) | set(base_by_nl))

    buckets: dict[str, list] = {k: [] for k in ("new", "removed", "unchanged", "improved", "regressed", "mixed")}
    details: dict[str, list] = {"improved": [], "regressed": [], "mixed": []}
    residual_deltas: list[dict] = []

    for netlist in all_netlists:
        if netlist not in base_by_nl:
            buckets["new"].append(netlist)
        elif netlist not in cur_by_nl:
            buckets["removed"].append(netlist)
        else:
            before = base_by_nl[netlist]
            after  = cur_by_nl[netlist]
            cat, deltas, residual = _classify_diff(before, after)
            buckets[cat].append(netlist)
            if cat in details:
                details[cat].append({
                    "netlist":         netlist,
                    "before":          before,
                    "after":           after,
                    "deltas":          deltas,
                    "coverage_deltas": _coverage_deltas(deltas),
                })
            # RESIDUAL-DELTA INVARIANT (Todo 248, unconditional): a netlist bucketed
            # "unchanged" that still carries a non-zero, unscored delta is exactly the
            # ID-247 dcdc.net failure mode — surface it, never drop it silently.
            if cat == "unchanged" and residual:
                residual_deltas.append({
                    "netlist":         netlist,
                    "before":          before,
                    "after":           after,
                    "residual_deltas": residual,
                })

    # Human summary
    baseline_sha = baseline.get("git_sha", "unknown")
    current_sha  = current.get("git_sha", "unknown")
    baseline_ts  = baseline.get("generated_at", "unknown")
    current_ts   = current.get("generated_at", "unknown")

    print()
    print(f"  Baseline: {baseline_path}")
    print(f"            sha {baseline_sha}, generated {baseline_ts}")
    print(f"  Current:  {current_summary_path}")
    print(f"            sha {current_sha}, generated {current_ts}")
    print()
    for cat in ("new", "removed", "unchanged", "improved", "regressed", "mixed"):
        count = len(buckets[cat])
        print(f"  {cat + ':':<12} {count} netlist{'s' if count != 1 else ''}")

    if details["regressed"]:
        print()
        print("REGRESSIONS:")
        for entry in details["regressed"]:
            _print_diff_entry(entry)

    if details["mixed"]:
        print()
        print("MIXED (improvements and regressions on same netlist):")
        for entry in details["mixed"]:
            _print_diff_entry(entry)

    if details["improved"]:
        print()
        print(f"IMPROVEMENTS ({len(details['improved'])} netlist{'s' if len(details['improved']) != 1 else ''}):")
        for entry in details["improved"]:
            _print_diff_entry(entry)

    if residual_deltas:
        print()
        print(f"WARNING: {len(residual_deltas)} netlist(s) classified 'unchanged' but carry "
              f"unscored non-zero deltas — compare-tooling blind spot (Todo 247/248):")
        for entry in residual_deltas:
            print(f"  {entry['netlist']}: {entry['residual_deltas']}")

    result = {
        "baseline_path":      str(baseline_path),
        "baseline_sha":       baseline_sha,
        "baseline_timestamp": baseline_ts,
        "current_sha":        current_sha,
        "current_timestamp":  current_ts,
        "counts":             {cat: len(lst) for cat, lst in buckets.items()},
        "regressions":        details["regressed"],
        "improvements":       details["improved"],
        "mixed":              details["mixed"],
        "new":                buckets["new"],
        "removed":            buckets["removed"],
        "residual_deltas":    residual_deltas,
    }

    result_json = json.dumps(result, indent=2)

    comparisons_dir = output_dir / "comparisons"
    comparisons_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"{current_sha}_{_compact_timestamp(current_ts)}"
    timestamped_path = comparisons_dir / f"compare_{baseline_sha}_vs_{run_id}.json"
    timestamped_path.write_text(result_json)

    legacy_path = output_dir / "baseline_comparison.json"
    legacy_path.write_text(result_json)

    print()
    print(f"  Comparison written to: {timestamped_path}")
    print(f"                    and: {legacy_path}  (compatibility copy)")

    return bool(details["regressed"] or details["mixed"])

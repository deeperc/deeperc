"""
kicad-cli auto-export preprocessor.

Walks a corpus directory for ``.kicad_sch`` and ``.sch`` files that lack a
sibling ``.net``, then invokes ``kicad-cli sch export netlist --format
kicadsexpr`` per file to materialize the netlist. Per-file failures are
captured to a structured JSON report under
``corpus_results/preprocess/kicad_cli_export_<ISO>.json`` and never raised —
the goal is to maximize yield across the whole corpus in one pass.

Phase 1 of TODO-4 (corpus expansion). Default OFF in ``run_checks.py``
via the ``--preprocess-export`` flag.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


# Directory basenames anywhere in the path that we never descend into.
EXCLUDE_DIRS = {
    "corpus_results",
    "wip",
    "investigation",
    ".git",
    "node_modules",
    "__pycache__",
}

# Source extensions that kicad-cli can convert to a netlist.
SOURCE_SUFFIXES = {".kicad_sch", ".sch"}

# Per-file timeout in seconds. Long enough for hierarchical schematics with
# many sub-sheets, short enough that one wedged file can't hang the corpus.
PER_FILE_TIMEOUT_S = 60

DEFAULT_CORPUS_ROOT = Path.home() / "schecker" / "netlist_corpus"
DEFAULT_REPORT_DIR = Path.home() / "schecker" / "corpus_results" / "preprocess"


def _walk_candidates(corpus_root: Path) -> Iterator[Path]:
    """Yield every .kicad_sch / .sch under corpus_root, skipping EXCLUDE_DIRS."""
    for dirpath, dirnames, filenames in os.walk(corpus_root):
        # In-place prune so os.walk doesn't descend into excluded subtrees.
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for name in filenames:
            suffix = Path(name).suffix.lower()
            if suffix in SOURCE_SUFFIXES:
                yield Path(dirpath) / name


def _sibling_net(src: Path) -> Path:
    """Return the sibling .net path for a .kicad_sch / .sch source."""
    return src.with_suffix(".net")


def _run_one(src: Path, out: Path) -> tuple[str, float, str]:
    """
    Invoke kicad-cli for a single file.

    Returns (status, elapsed_seconds, stderr_tail).
    status ∈ {"exported", "failed", "timed_out"}; stderr_tail empty on success.
    """
    cmd = [
        "kicad-cli", "sch", "export", "netlist",
        "--format", "kicadsexpr",
        "--output", str(out),
        str(src),
    ]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=PER_FILE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        elapsed = time.monotonic() - t0
        tail = (e.stderr or "")[-500:] if isinstance(e.stderr, str) else ""
        return "timed_out", elapsed, tail
    except Exception as e:  # noqa: BLE001 — never raise from per-file work
        elapsed = time.monotonic() - t0
        return "failed", elapsed, f"{type(e).__name__}: {e}"[-500:]

    elapsed = time.monotonic() - t0
    if proc.returncode == 0 and out.exists():
        return "exported", elapsed, ""
    return "failed", elapsed, (proc.stderr or proc.stdout or "")[-500:]


def kicad_cli_export(
    corpus_root: Path,
    dry_run: bool = False,
    force: bool = False,
    report_dir: Path | None = None,
) -> dict:
    """
    Bulk-export .net files for every .kicad_sch / .sch under corpus_root
    that lacks a sibling .net (or all of them if force=True).

    Always returns a report dict. Writes a JSON copy of the report under
    report_dir/kicad_cli_export_<ISO>.json unless dry_run is True.

    Never raises on per-file failure. Per-file failures and timeouts are
    captured in the report so the caller can inspect yield.
    """
    corpus_root = Path(corpus_root).resolve()
    report_dir = (report_dir or DEFAULT_REPORT_DIR).resolve()

    t_start = time.monotonic()
    per_file: list[dict] = []
    counts = {"exported_new": 0, "skipped_exists": 0, "failed": 0, "timed_out": 0}

    candidates = sorted(_walk_candidates(corpus_root))
    total = len(candidates)
    print(f"[kicad_cli_export] root={corpus_root}  candidates={total}  "
          f"dry_run={dry_run}  force={force}", flush=True)

    for i, src in enumerate(candidates, 1):
        out = _sibling_net(src)
        rel = src.relative_to(corpus_root) if src.is_relative_to(corpus_root) else src

        if out.exists() and not force:
            status, elapsed, stderr_tail = "skipped_exists", 0.0, ""
        elif dry_run:
            # Dry-run still classifies what *would* happen so the caller sees
            # the candidate count and per-file targets without invoking kicad-cli.
            status, elapsed, stderr_tail = "exported", 0.0, ""  # would-export
            counts["exported_new"] += 1
            print(f"[{i}/{total}] {rel} -> would-export (dry-run)", flush=True)
            per_file.append({
                "input": str(src),
                "output": str(out),
                "status": "would_export",
                "elapsed_seconds": 0.0,
            })
            continue
        else:
            status, elapsed, stderr_tail = _run_one(src, out)

        # Map per-file status to the report's bucket name.
        bucket = {
            "exported": "exported_new",
            "skipped_exists": "skipped_exists",
            "failed": "failed",
            "timed_out": "timed_out",
        }[status]
        counts[bucket] += 1

        entry: dict = {
            "input": str(src),
            "output": str(out),
            "status": status,
            "elapsed_seconds": round(elapsed, 3),
        }
        if status in ("failed", "timed_out") and stderr_tail:
            entry["stderr_tail"] = stderr_tail
        per_file.append(entry)

        print(f"[{i}/{total}] {rel} -> {status} ({elapsed:.2f}s)", flush=True)

    elapsed_total = time.monotonic() - t_start

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "corpus_root": str(corpus_root),
        "total_candidates": total,
        "exported_new": counts["exported_new"],
        "skipped_exists": counts["skipped_exists"],
        "failed": counts["failed"],
        "timed_out": counts["timed_out"],
        "elapsed_seconds": round(elapsed_total, 3),
        "per_file": per_file,
        "dry_run": dry_run,
        "force": force,
    }

    if not dry_run:
        report_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        report_path = report_dir / f"kicad_cli_export_{ts}.json"
        report_path.write_text(json.dumps(report, indent=2))
        report["report_path"] = str(report_path)
        print(f"[kicad_cli_export] report -> {report_path}", flush=True)

    print(
        f"[kicad_cli_export] done in {elapsed_total:.1f}s  "
        f"exported_new={counts['exported_new']}  "
        f"skipped_exists={counts['skipped_exists']}  "
        f"failed={counts['failed']}  timed_out={counts['timed_out']}",
        flush=True,
    )
    return report


def _cli() -> int:
    p = argparse.ArgumentParser(
        description="Auto-export .net files from .kicad_sch / .sch sources using kicad-cli.",
    )
    p.add_argument("--corpus-root", default=str(DEFAULT_CORPUS_ROOT),
                   help=f"Root to walk (default: {DEFAULT_CORPUS_ROOT})")
    p.add_argument("--dry-run", action="store_true",
                   help="Walk + classify, but do not invoke kicad-cli or write a report.")
    p.add_argument("--force", action="store_true",
                   help="Re-export even when a sibling .net already exists.")
    args = p.parse_args()

    if shutil.which("kicad-cli") is None:
        print("ERROR: kicad-cli not found on PATH", file=sys.stderr)
        return 2

    corpus_root = Path(args.corpus_root).expanduser()
    if not corpus_root.exists():
        print(f"ERROR: corpus-root does not exist: {corpus_root}", file=sys.stderr)
        return 2

    kicad_cli_export(corpus_root, dry_run=args.dry_run, force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())

#!/usr/bin/env python3
"""
Schematic Checker PoC — end-to-end pipeline runner (thin CLI over pipeline.run_board).
Usage: python main.py --netlist test_netlist/stm32_5v_violation.net
"""
import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(message)s")

import pipeline


def main() -> None:
    parser = argparse.ArgumentParser(description="Schematic Checker PoC")
    parser.add_argument("--netlist", required=True, help="Path to KiCad .net file")
    parser.add_argument("--datasheets-dir", default=None,
                        help="Override the canonical netlist_corpus/datasheets store "
                             "(default: the library canonical, resolved absolute). D1.")
    parser.add_argument("--skip-confirm", action="store_true", help="Auto-accept inferred voltages (CI mode)")
    parser.add_argument("--rail-map", default=None,
                        help="Path to a confirmed rail map (overrides the auto-discovered "
                             "<netlist>.rails.json sidecar). TODO-134.")
    parser.add_argument("--refresh", action="append", default=[], metavar="PART_STEM",
                        help="Force re-extraction for this part's datasheet, bypassing the "
                             "pin-groups cache (repeatable; case-insensitive datasheet-PDF "
                             "stem, e.g. --refresh STM32F103C8T6). The prior cache is "
                             "preserved as a .pre_reextract sidecar, never deleted. Use when "
                             "the PDF on disk changed — cache absence is otherwise the only "
                             "re-extraction trigger. TODO-367.")
    parser.add_argument("--staging", action="store_true",
                        help="Serve pin-group caches from the unpromoted staging tier "
                             "(schematic_checker_poc/datasheets_staged/) on a clean "
                             "canonical MISS. OFF by default; findings backed by a "
                             "staged cache are marked cache_tier=staged and the report "
                             "carries a staged-cache banner. TODO-386.")
    args = parser.parse_args()

    ctx = pipeline.PipelineContext(
        netlist_path=args.netlist,
        skip_confirm=args.skip_confirm,
        rail_map_path=args.rail_map,
        datasheets_dir=args.datasheets_dir,
        output_path="report.json",
        refresh_stems=frozenset(args.refresh),
        staging=True if args.staging else None,   # None = defer to SCHECKER_STAGING
    )
    outcome = pipeline.run_board(ctx)
    if isinstance(outcome, pipeline.PipelineFailure):
        # Per-driver failure PRESENTATION (spec §3.4): the interactive CLI aborts to
        # stderr + exit 1. (The corpus driver maps the same typed failure to a
        # pipeline_error result / --board nonzero exit.)
        print(f"[STEP {outcome.stage}] {outcome.detail}", file=sys.stderr)
        sys.exit(1)
    report = outcome

    # main.py never classified before (recon: banner only); it now prints the same
    # registry-driven verdict the corpus driver records — new, small, free.
    verdict = pipeline.classify_report(report)
    print(f"[VERDICT] {verdict}")

    # TODO-426: exit code gated on the cross-axis classify_report() verdict, not the
    # signal-only (step_08) fail bucket — a peripheral/supply/structural-only FAIL
    # (fail_count==0 in the old signal-bucket read) used to exit 0 here.
    sys.exit(1 if verdict == "has_fail" else 0)


if __name__ == "__main__":
    main()

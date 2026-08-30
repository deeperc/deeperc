import json
import hashlib
import os
import re
import shutil
import signal
import subprocess
import logging
from pathlib import Path
from datetime import datetime, timezone

import pdfplumber

import cache_paths
from llm.ollama_client import generate_json, MODEL as OLLAMA_MODEL

logger = logging.getLogger(__name__)

DATASHEETS_DIR = "./datasheets"
PARSED_DIR = "./datasheets_parsed"

# ── staging tier (TODO-386 Phase 3, signed design S1/S2/R-C) ────────────────
#
# A SECOND pin-group cache root, laid out identically (<stem>/auto/<stem>_
# pin_groups.json — the same cache_paths formula, a different base) and
# gitignored. It is deliberately a SIBLING of PARSED_DIR, never nested inside
# it: the TODO-320 production-tree tripwire (tests/conftest.py) and every
# directory-walking census tool root themselves at PARSED_DIR, so a nested
# staging tree would trip `make test` and inflate unrelated cache-diff counts
# (cache_config_point_recon.md §Step 4).
#
# Two independent switches, deliberately not one:
#   * WRITE (R-C) is UNCONDITIONAL — extraction always lands in staging, so
#     the canonical tree is never written by the extract path at all. See
#     cache_write_target().
#   * READ (S2) is OFF BY DEFAULT and only ever consulted on a CLEAN canonical
#     MISS. Canonical ambiguity never falls through; within-staging ambiguity
#     refuses. See find_cached_dir_for_part / load_cached_pin_groups.
#
# Consequence of that asymmetry, recorded deliberately: with the read switch
# OFF, a fresh extraction is used for the board that triggered it (the
# pipeline's in-memory per-board memo) but is NOT re-served on a later board or
# a later run — canonical is now append-only via the promotion gate
# (export/promote_staged.py), which is the point of the tier.
STAGING_DIR = "./datasheets_staged"

# ── quarantine root (TODO-388 Phase 2, R-β) ─────────────────────────────────
#
# Where a case-variant stem directory goes when _reconcile_mineru_output cannot
# merge it into the canonical directory (or has just emptied it by merging).
# NEVER a delete: CLAUDE.md's cache rule is absolute, and the whole point of the
# TODO-321/376 defect class is that the evidence of what MinerU actually wrote
# is the audit trail. A sibling of both cache roots for the same reason staging
# is (the TODO-320 tripwire and every census tool root at PARSED_DIR).
#
# Deliberately NOT tripwire-guarded, unlike PARSED_DIR/STAGING_DIR: this tree
# RECEIVES mid-run moves by design, so a session-scoped "nothing changed"
# assertion over it would fire on correct behaviour.
QUARANTINE_DIR = "./datasheets_quarantine"

STAGING_ENV_VAR = "SCHECKER_STAGING"
_TRUTHY = {"1", "true", "yes", "on"}

# None = defer to the environment; True/False = an explicit driver override
# (pipeline.configure_resolver, set from --staging). Checked at CALL time, not
# import time, so a driver that sets the env var after import still wins.
STAGING_ENABLED: bool | None = None

CACHE_TIER_CANONICAL = "canonical"
CACHE_TIER_STAGED = "staged"


def staging_enabled() -> bool:
    """Is the staging READ fallback active this run? (Writes never consult it.)"""
    if STAGING_ENABLED is not None:
        return bool(STAGING_ENABLED)
    return os.environ.get(STAGING_ENV_VAR, "").strip().lower() in _TRUTHY

BASE_SUFFIX_RE = re.compile(r"[-_]?[A-Z]{1,2}\d$", re.IGNORECASE)

# L1 family-prefix fallback tier, rule R2 (investigation/experiments/l1_family_tier/
# REPORT.md, Step 6). Vendor-scoped so it can never bridge unrelated part families.
# Hoisted here from pipeline.py (TODO-407) so the PDF finder
# (pipeline._find_pdf_recursive) and the PDF-free cache lookup (_scan_cache_root)
# share ONE definition rather than two drifting literals. pipeline.py imports
# these; step_03_resolver must not import pipeline (pipeline imports this module).
_L1_FAMILY_TOKEN_RE = re.compile(r"^STM32[FGHLU]\d{3}", re.IGNORECASE)
_L1_FAMILY_TOKEN_SPLIT_RE = re.compile(r"[\s_]+")
_L1_FAMILY_MIN_STEM = 6

PLACEHOLDER_RE = re.compile(r'^\s*!\[\]\(images/[a-f0-9]+\.jpg\)\s*$', re.MULTILINE)
PATCH_MARKER_RE = re.compile(r"<!-- pdfplumber patch: page (\d+) -->")

EXTRACTION_SCHEMA_VERSION = 1  # bump when the _pin_groups.json schema changes

MIN_MPN_LENGTH = 4  # anything shorter is not a real MPN
MAX_PDF_SIZE_FOR_MINERU = 3 * 1024 * 1024   # 3 MB — skip MinerU for large datasheets (ATmega etc. always time out anyway)

# Per-page cost guard (TODO-294): bounds the cost of any SINGLE
# page.extract_text() call — a real 86-page datasheet's page 15 alone cost
# ~21s / ~750MB in one call (known pdfminer.six failure mode on
# vector-graphics-dense pages). See _extract_page_text_guarded below. Both
# thresholds are well above every normal page observed (0.00-0.08s, single-digit
# MB) — the guard is inert unless a page is a >100x outlier.
PAGE_PARSE_TIMEOUT_SECONDS = 10.0            # wall-clock cap per page.extract_text() call
PAGE_PARSE_RSS_DELTA_LIMIT_KB = 400 * 1024   # ~400MB per-page RSS-delta cap (secondary guard)

# Per-DOCUMENT cumulative-RSS ceiling (TODO-227 Phase 3a). Retires the old
# static page-COUNT cap (MAX_PDFPLUMBER_PAGES=200): Phase 1b's RSS
# measurement (investigation/experiments/todo227_rss_probe/) showed real PDFs
# grow cumulative RSS roughly linearly with no plateau, and that a single
# pathological PDF can nearly exhaust a multi-GB ceiling independent of page
# count (MCP4726A0T-E_MAY.pdf: a recurring class of 700+MB single-page
# spikes, killed at page 22/86 having already hit 4.7GB) — a page-count cap
# was the wrong axis to bound cost on. Basis is this document's own RSS delta
# since ITS parse started, never an absolute RSS level — production parses
# many documents sequentially in one long-lived process, so an absolute
# check would false-trigger on document N+1 purely from residual RSS
# document N left behind. See _cumulative_rss_over_ceiling below.
PDFPLUMBER_RSS_CEILING_DELTA_KB = int(3.5 * 1024 * 1024)   # 3.5 GB per-document ceiling

# Section headings the placeholder-patch re-extracts (module-level so the cache
# provenance signature can fold them in — a TARGET_PHRASES change alters the .md,
# hence the extraction, and must show up as a stale-cache signal).
TARGET_PHRASES = [
    "Absolute maximum ratings",
    "Voltage characteristics",
    "Current characteristics",
    "I/O port characteristics",
    "General operating conditions",
    "Input voltage",
    "VIH",
    "VIL",
]

# Best-effort MinerU/magic-pdf version for the parse-config signature; "unknown"
# if not resolvable (still recomputable/comparable across runs on this machine).
try:  # pragma: no cover - environment dependent
    import importlib.metadata as _ilmd
    MINERU_VERSION = _ilmd.version("magic-pdf")
except Exception:  # pragma: no cover
    MINERU_VERSION = "unknown"

PROVENANCE_SCHEMA_VERSION = 1

# TODO-227 Phase 2b: persistent pdfplumber-cache schema (skip-manifest +
# patch_state) — see the "persistent pdfplumber cache" section below.
PDFPLUMBER_CACHE_SCHEMA_VERSION = 2  # v2 (TODO-227 Phase 3a): +manifest.truncated, +patch_state.truncated


def canonical_pdf_stem(pdf_path: str) -> str:
    """The canonical cache-path key for a PDF (TODO-321). ALL datasheets_parsed/
    path derivation goes through this helper — a PDF's on-disk filename case is
    never mutated (the L2 download layer still writes whatever case the query
    MPN string carried), only the cache-directory key derived from it is
    normalized, so an on-disk case difference (e.g. a re-download under a
    differently-cased MPN) can never again fork into two case-duplicate
    datasheets_parsed/ directories for the same physical part."""
    return Path(pdf_path).stem.lower()


# ── bucket-B path derivation (TODO-388 Phase 2, R-α) ────────────────────────
#
# "Bucket B" is the PARSE-ARTIFACT family: the MinerU <stem>/auto/ tree (.md,
# _content_list/_middle/_model.json, _origin/_layout/_spans.pdf, images/) and
# the pdfplumber-fallback <stem>/pdfplumber/ sibling (.md, .skipmanifest.json,
# patch_state.json). Bucket A is the <stem>/auto/<stem>_pin_groups.json the
# staging tier already governs (TODO-386 R-C).
#
# Before this, THREE independent derivations of the same <root>/<stem>/auto/
# path existed (_parse_pdf, resolve_and_parse, _reconcile_mineru_output) plus a
# fourth for the pdfplumber family — each hardcoding PARSED_DIR, so MinerU's
# output root was not redirectable by any existing knob and every parse wrote
# into the canonical tree regardless of tier (todo388_bucketb_containment_
# recon.md R1/R2). They all now go through stem_dir().
#
# Asymmetry with bucket A, deliberate: bucket A's write target is
# UNCONDITIONALLY staging (cache_write_target), because a pin-groups file is
# what backs a verdict and canonical must be append-only through the promotion
# gate. Bucket B's write target follows the SAME activation surface as the S2
# read switch instead (--staging / SCHECKER_STAGING), so a normal run's on-disk
# behaviour is byte-identical to before this change. Bucket B is a parse
# INPUT-cache, not evidence; routing it to staging unconditionally would strand
# every normal run's parse output in an unpromoted tree and force a re-parse of
# the whole corpus on the next run.


def bucket_b_write_root() -> str:
    """The cache root THIS run's fresh parse artifacts are written to."""
    return STAGING_DIR if staging_enabled() else PARSED_DIR


def bucket_b_read_roots() -> tuple[str, ...]:
    """Roots consulted, IN ORDER, when looking for an existing parse artifact.

    Mirrors S2's read semantics exactly: canonical is always tried first and a
    canonical hit always wins; staging is consulted only on a canonical miss,
    and only when staging reads are enabled. (S2's third rule — 'ambiguity is
    never rescued by staging' — has no bucket-B analogue: these are exact
    per-stem paths, not the substring scan find_cached_dir_for_part runs, so a
    lookup here is a plain exists() with no ambiguous outcome to rescue.)"""
    return (PARSED_DIR, STAGING_DIR) if staging_enabled() else (PARSED_DIR,)


def stem_dir(pdf_path: str, output_root=None) -> Path:
    """<root>/<canonical-stem>/ — the ONE bucket-B derivation point (R-α).

    `output_root` names the root explicitly (the promotion tooling and the
    tests pass it); None means 'this run's write root'."""
    root = bucket_b_write_root() if output_root is None else output_root
    return Path(root) / canonical_pdf_stem(pdf_path)


def mineru_auto_dir(pdf_path: str, output_root=None) -> Path:
    return stem_dir(pdf_path, output_root) / "auto"


def mineru_markdown_path(pdf_path: str, output_root=None) -> Path:
    """The FILENAME is the canonical (lower-cased) stem, while MinerU itself
    writes <VERBATIM-STEM>.md inside a <VERBATIM-STEM>/ directory. Reconcile
    renames the DIRECTORY only (test_case_canonicalization asserts exactly
    this), so for an uppercase-named PDF the canonical path legitimately does
    not exist even after a successful reconcile — the verbatim-cased .md sits
    right next to it. TODO-391 Phase 1b (f203c4c) proved this is verdict-inert
    under haiku_pdfplumber (the shipped extractor never reads MinerU markdown
    at all), so a case-insensitive probe here changes no parse INPUT today.
    (If gemma_mineru is ever promoted to default, feeding it the verbatim-cased
    file instead of a nonexistent canonical one WOULD change parse input for
    uppercase-stemmed parts — a fact for that future promotion to account for,
    not a reason to withhold this fix now.) The probe is read-only: it never
    renames or writes the verbatim file, so callers using this path as a WRITE
    target still get the canonical (possibly nonexistent) path unchanged."""
    auto_dir = mineru_auto_dir(pdf_path, output_root)
    canonical = auto_dir / f"{canonical_pdf_stem(pdf_path)}.md"
    if canonical.exists():
        return canonical
    if auto_dir.is_dir():
        variants = [
            entry for entry in auto_dir.iterdir()
            if entry.is_file() and entry.suffix == ".md"
            and entry.name.lower() == canonical.name.lower()
        ]
        if len(variants) == 1:
            return variants[0]
        if len(variants) > 1:
            logger.warning(
                "mineru_markdown_path: %d case-variant matches for %s in %s, "
                "not picking: %s",
                len(variants), canonical.name, auto_dir,
                [v.name for v in variants],
            )
    return canonical


def _first_existing_bucket_b(pdf_path: str, builder) -> Path | None:
    """First path that exists across bucket_b_read_roots(), else None."""
    for root in bucket_b_read_roots():
        candidate = builder(pdf_path, root)
        if candidate.exists():
            return candidate
    return None


def _warn_on_case_collision(pdf_path: str, root=None) -> None:
    """Bifurcation guard (TODO-321) — warn, never reject. Fires only until the
    Phase 2 data migration lands: if a top-level datasheets_parsed/ sibling
    directory already exists whose name matches this PDF's canonical stem
    case-insensitively but differs in exact case, log a WARNING naming both.
    Detects new instances of the same defect class the TODO-321 recon
    catalogued (case-insensitive PDF lookup feeding a case-preserving cache
    path, pre-canonicalization) without ever refusing or delaying the write —
    the canonical (lower-cased) directory is always the one this run targets.

    `root` (TODO-386 Phase 3) is the cache root actually being written — the
    staging tier for a fresh extraction (R-C), the canonical tree otherwise.
    It defaults to PARSED_DIR, preserving the original single-root behaviour."""
    stem = canonical_pdf_stem(pdf_path)
    root = PARSED_DIR if root is None else root
    try:
        existing = os.listdir(root)
    except OSError:
        return
    for name in existing:
        if name != stem and name.lower() == stem:
            logger.warning(
                "[STEP 03] TODO-321: case-insensitive cache-dir collision — "
                "existing directory %r differs in case from canonical target "
                "%r (both under %s). Writing to the canonical directory; the "
                "existing sibling is left untouched.",
                name, stem, root,
            )


def _quarantine_dest(stem: str) -> Path:
    """A free path under QUARANTINE_DIR for `stem`, never an existing one.

    Collisions get a -2/-3/... suffix rather than overwriting: a stem that
    re-quarantines on a later run must not silently destroy the tree the
    previous run preserved."""
    base = Path(QUARANTINE_DIR) / stem
    if not base.exists():
        return base
    n = 2
    while (candidate := base.with_name(f"{stem}-{n}")).exists():
        n += 1
    return candidate


def _quarantine_move(src: Path, stem: str, reason: str) -> Path:
    """Move a case-variant directory out of the cache root into quarantine.

    A MOVE, never a delete (CLAUDE.md's non-destructive cache rule): the tree
    stays fully inspectable, it simply stops being a second directory that
    makes `stem` ambiguous to find_cached_dir_for_part."""
    dest = _quarantine_dest(stem)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    logger.warning(
        "[STEP 03] TODO-388: quarantined case-variant directory %r → %s (%s). "
        "Moved, not deleted — the tree is preserved for inspection.",
        stem, dest, reason,
    )
    return dest


def _merge_stem_dir(src: Path, dst: Path) -> tuple[list, list]:
    """Entry-level move of src's top-level entries into dst.

    Returns (moved, collided). An entry whose name already exists under dst is
    NEVER clobbered — it stays in src and is reported as a collision, which
    routes the remainder to quarantine. Entry-level rather than whole-dir so a
    canonical directory holding only a pdfplumber/ tree can absorb MinerU's
    auto/ tree without either one overwriting the other."""
    moved, collided = [], []
    try:
        entries = sorted(os.listdir(src))
    except OSError:
        return moved, collided
    dst.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        target = dst / entry
        if target.exists():
            collided.append(entry)
            continue
        shutil.move(str(src / entry), str(target))
        moved.append(entry)
    return moved, collided


def _reconcile_mineru_output(pdf_path: str, output_root=None) -> None:
    """TODO-321: MinerU (`magic-pdf`) writes its output tree into
    <root>/<verbatim-stem>/ using the PDF's on-disk filename case, but this
    module keys every cache path off canonical_pdf_stem (lower-cased). Without
    this reconciliation the post-invocation existence check misses a successful
    MinerU run on any non-lowercase-named PDF and mislabels it "MinerU failed
    (rc=0)", silently falling back to pdfplumber.

    Canonical absent → move the WHOLE verbatim directory to the canonical name
    (a whole-dir move preserves the relative image references inside auto/).

    TODO-388 Phase 2 (R-β) — the BOTH-EXIST branch. It used to warn and leave
    the verbatim sibling in place forever, which is precisely what poisoned the
    stem: two directories differing only in case make find_cached_dir_for_part
    permanently AMBIGUOUS, so (1) the bucket-A warm shortcut misses on every
    later run and MinerU re-parses that PDF every time, and (2) the promotion
    gate refuses that stem (6 of 11 pilot stems, todo386_phase3_pilot.md P4).

    The common both-exist shape is not a genuine conflict at all: the canonical
    directory frequently holds ONLY a pdfplumber/ tree, because a run in which
    MinerU was unavailable took the fallback branch, whose write is case-safe
    by construction and pre-creates the canonical top-level directory. So:

      * canonical has no auto/ → MERGE the verbatim entries in, entry by entry,
        never clobbering; then quarantine whatever shell or colliding remainder
        is left, so the case-variant directory stops existing either way.
      * canonical already has auto/ → a genuine conflict. Canonical stays
        authoritative and the verbatim sibling is quarantined WHOLE.

    Never deletes anything in either path."""
    root = bucket_b_write_root() if output_root is None else output_root
    verbatim = Path(pdf_path).stem
    canonical = canonical_pdf_stem(pdf_path)
    if verbatim == canonical:
        return
    verbatim_dir = Path(root) / verbatim
    canonical_dir = Path(root) / canonical
    if not verbatim_dir.is_dir():
        return
    if not canonical_dir.exists():
        shutil.move(str(verbatim_dir), str(canonical_dir))
        logger.info(
            "[STEP 03] TODO-321: reconciled MinerU output directory %r → canonical %r",
            verbatim, canonical,
        )
        return

    if (canonical_dir / "auto").exists():
        _quarantine_move(verbatim_dir, verbatim,
                         f"canonical {canonical!r} already holds an auto/ tree")
        return

    moved, collided = _merge_stem_dir(verbatim_dir, canonical_dir)
    logger.info(
        "[STEP 03] TODO-388: merged %d entr(ies) from case-variant %r into "
        "canonical %r (%d collision(s) left behind)",
        len(moved), verbatim, canonical, len(collided),
    )
    _quarantine_move(
        verbatim_dir, verbatim,
        f"{len(collided)} colliding entr(ies) after merge" if collided
        else "empty shell after full merge",
    )


def _prune_staged_images(pdf_path: str, output_root) -> int:
    """TODO-388 Phase 2 (Step 5): drop the auto/images/ payload MinerU just
    emitted — in the STAGING root ONLY, immediately after a staging-mode parse.

    Why this is safe to drop rather than preserve: the images are write-only.
    Direct enumeration of the whole markdown-consumption path (pipeline ->
    run_extractor -> ExtractorImpl.build_model_input/extract) found NO consumer
    that resolves a markdown image reference to a file on disk. The only two
    `images/` references in the repo are PLACEHOLDER_RE, which matches the
    reference TEXT in the markdown and replaces it with pdfplumber-extracted
    page text (never opening the .jpg), and package_cache's BUCKET_BC_RE, a
    string-list membership tripwire that never walks disk. They were 407 of the
    504 canonical artifacts the TODO-386 pilot measured.

    Preferred over this would have been suppressing emission at the source, but
    magic-pdf exposes no such knob: neither its `--help` (-v/-p/-o/-m/-l/-d/
    -s/-e only) nor its magic-pdf.json config surface (bucket_info, models-dir,
    device-mode, layout-config, formula-config, table-config) has an
    image-writer toggle. Recorded either way, per the dispatch.

    Two independent guards keep this off the canonical tree, which is NEVER
    pruned (historical images/ trees are explicitly out of scope): the root
    must be the staging root by identity, AND staging must be active. Returns
    the number of files removed (0 whenever it declines)."""
    if not staging_enabled() or str(output_root) != str(STAGING_DIR):
        return 0
    images = mineru_auto_dir(pdf_path, output_root) / "images"
    if not images.is_dir():
        return 0
    try:
        n = sum(1 for _ in images.rglob("*") if _.is_file())
        shutil.rmtree(images)
    except OSError as e:
        logger.warning("[STEP 03] TODO-388: could not prune staged images %s: %s",
                       images, e)
        return 0
    logger.info("[STEP 03] TODO-388: pruned %d staged image file(s) from %s "
                "(no consumer reads them; canonical trees untouched).", n, images)
    return n


def _find_pdf(part_number: str) -> str | None:
    pdfs = [f for f in os.listdir(DATASHEETS_DIR) if f.lower().endswith(".pdf")]

    base_mpn = BASE_SUFFIX_RE.sub("", part_number)

    for pdf in pdfs:
        if base_mpn.lower() in pdf.lower():
            logger.info(f"[STEP 03] Resolved {part_number} → {pdf} (matched base MPN: {base_mpn})")
            return os.path.join(DATASHEETS_DIR, pdf)

    for pdf in pdfs:
        if part_number.lower() in pdf.lower():
            logger.info(f"[STEP 03] Resolved {part_number} → {pdf} (matched full MPN)")
            return os.path.join(DATASHEETS_DIR, pdf)

    return None


def _scan_cache_root(root, part_number: str) -> tuple[Path | None, str]:
    """One cache root's share of find_cached_dir_for_part's three-tier scan.

    Returns (hit, outcome) with outcome ∈ 'hit' | 'miss' | 'ambiguous'.
    'miss' means NOTHING matched in any tier — a clean miss, the only
    outcome the staging fallback is allowed to rescue (S2). 'ambiguous' means
    at least one tier matched >1 candidate and no tier resolved uniquely.

    Tier 3 (TODO-407) mirrors pipeline._find_pdf_recursive's family-prefix
    fallback, against cache-DIRECTORY names instead of PDF filenames, reusing
    the same _L1_FAMILY_* constants. It exists because the two stores were
    asymmetric: BASE_SUFFIX_RE only strips a trailing grade that ends in a
    DIGIT ('...T6'), so a KiCad stock-symbol Value carrying the generic 'x'
    placeholder ('STM32F103C8Tx') missed tiers 1 and 2 in both stores — but the
    PDF store had tier 3 to rescue it and the cache store did not. The result
    was an MPN that resolved with a PDF present and went UNRESOLVABLE PDF-free,
    which is exactly the shipped (PDF-less) configuration. STM32-only by
    construction: _L1_FAMILY_TOKEN_RE is vendor-scoped.

    Uniqueness rule is identical to the PDF path — >1 distinct directory means
    refuse rather than guess. It is reported as 'ambiguous' (not 'miss') for
    the same reason tiers 1/2 are: the canonical tree already holds two
    plausible answers, and a staged third must never break that tie."""
    try:
        names = [
            n for n in os.listdir(root)
            if os.path.isdir(os.path.join(root, n))
        ]
    except OSError:
        return None, "miss"

    base_mpn = BASE_SUFFIX_RE.sub("", part_number)
    ambiguous = False
    for probe in (base_mpn, part_number):
        candidates = {n for n in names if probe.lower() in n.lower()}
        if len(candidates) == 1:
            return Path(root) / next(iter(candidates)), "hit"
        if len(candidates) > 1:
            ambiguous = True
            logger.info(
                "[STEP 03] PDF-optional cache lookup for %s: %d candidate "
                "cache dirs matched (%s) — ambiguous, treating as MISS "
                "(falling through to full resolve_and_parse).",
                part_number, len(candidates), sorted(candidates),
            )
    if ambiguous:
        # Tiers 1/2 already hold two plausible answers — terminal, never
        # rescued by a broader tier (same reasoning as the staging refusal).
        return None, "ambiguous"

    # Tier 3: family-prefix fallback (STM32-only). Mirrors the PDF path.
    part_upper = part_number.upper()
    family_hits: dict[str, str] = {}   # dir name -> the token that matched it
    for n in names:
        for token in _L1_FAMILY_TOKEN_SPLIT_RE.split(n):
            if (
                len(token) >= _L1_FAMILY_MIN_STEM
                and _L1_FAMILY_TOKEN_RE.match(token)
                and part_upper.startswith(token.upper())
            ):
                family_hits[n] = token
                break
    if len(family_hits) == 1:
        hit_name, hit_token = next(iter(family_hits.items()))
        logger.info(
            "[STEP 03] cache: family-prefix match '%s' -> %s",
            hit_token, hit_name,
        )
        return Path(root) / hit_name, "hit"
    if len(family_hits) > 1:
        logger.info(
            "[STEP 03] PDF-optional cache lookup for %s: %d cache dirs matched "
            "on the family-prefix tier (%s) — ambiguous, treating as MISS "
            "(falling through to full resolve_and_parse).",
            part_number, len(family_hits), sorted(family_hits),
        )
        return None, "ambiguous"
    return None, "miss"


def find_cached_dir_for_part(part_number: str) -> Path | None:
    """PDF-optional cache-first locator (TODO-362 Phase 1): resolves a
    part_number to an existing datasheets_parsed/<stem>/ directory WITHOUT
    requiring a physical PDF on disk. Uses the same two-tier substring
    matching _find_pdf applies to PDF filenames (base-MPN first, then full
    MPN) — but against on-disk cache-directory names instead, which are
    already canonical/lower-cased (TODO-321).

    Unlike _find_pdf, which returns the first listdir-order match, this
    collects ALL matches per tier and refuses (returns None, i.e. MISS —
    the caller falls through to a full resolve_and_parse) on ambiguity:
    there is no PDF byte content downstream on this path to disambiguate a
    wrong pick the way a live parse would, so a silent walk-order pick here
    would be a materially riskier guess than the equivalent PDF-filename
    pick in _find_pdf.

    TODO-386 Phase 3 (S2): this is one of the two staging-fallback seams. The
    canonical root is scanned exactly as before; STAGING_DIR is consulted ONLY
    when staging reads are enabled AND the canonical scan was a CLEAN MISS.
    A canonical AMBIGUITY is terminal and is never rescued by staging — the
    canonical tree already holds two plausible answers, and letting a staged
    third break that tie would be the riskiest guess of all. Ambiguity WITHIN
    staging refuses on the same reasoning as the canonical scan."""
    hit, outcome = _scan_cache_root(PARSED_DIR, part_number)
    if hit is not None:
        return hit
    if outcome == "ambiguous":
        return None  # canonical ambiguity is terminal — never falls through
    if not staging_enabled():
        return None
    hit, staged_outcome = _scan_cache_root(STAGING_DIR, part_number)
    if hit is not None:
        logger.info("[STEP 03] staging-tier cache dir matched for %s: %s "
                    "(canonical clean miss).", part_number, hit.name)
    elif staged_outcome == "ambiguous":
        logger.info("[STEP 03] staging-tier lookup for %s was ambiguous — "
                    "refusing (falling through to full resolve_and_parse).",
                    part_number)
    return hit


def _has_token_overlap(part_number: str, filename: str) -> bool:
    """Reject Gemma matches where the filename shares no alphanumeric tokens with the part number."""
    part_tokens = set(re.findall(r"[a-z0-9]{3,}", part_number.lower()))
    file_tokens = set(re.findall(r"[a-z0-9]{3,}", filename.lower()))
    return bool(part_tokens & file_tokens)


def _gemma_fallback(part_number: str) -> str | None:
    """RETIRED 2026-07-10 (see done_todos_index TODO-221): call site removed.
    KNOWN-BROKEN: non-recursive os.listdir vs vendor-subdir store — must be
    fixed before any revival."""
    pdfs = [f for f in os.listdir(DATASHEETS_DIR) if f.lower().endswith(".pdf")]
    if not pdfs:
        return None
    pdf_list = "\n".join(pdfs)
    prompt = (
        f'You are a hardware component expert. A PCB netlist references the\n'
        f'part number "{part_number}". The following datasheet PDFs are\n'
        f"available locally:\n\n{pdf_list}\n\n"
        f"Which PDF filename is the most likely match for this part number?\n"
        f"Consider that datasheets often cover a device family (e.g.\n"
        f'"stm32f103c8.pdf" covers the STM32F103C8T6).\n\n'
        f"Reply ONLY with a JSON object. No explanation, no markdown fences.\n"
        f'Format:\n{{"match": "filename.pdf", "confidence": "high|medium|low", "reason": "one sentence"}}\n'
        f'If no file is a plausible match, return {{"match": null, "confidence": "low", "reason": "..."}}'
    )
    result = generate_json(prompt, step_hint="03_resolver")
    if result and result.get("match") and result.get("confidence") != "low":
        filename = result["match"]
        if not _has_token_overlap(part_number, filename):
            logger.warning(
                f"[STEP 03] Gemma suggested {filename} for {part_number} but no token overlap — rejecting"
            )
            return None
        reason = result.get("reason", "")
        logger.info(f"[STEP 03] Gemma resolved {part_number} → {filename} ({reason})")
        return os.path.join(DATASHEETS_DIR, filename)
    return None


# ── LT-23: single per-run L2 availability probe ─────────────────────────────
#
# Previously both L2 reach points below (_l2_vendor_resolve and the
# warm-path cache peek in resolve_and_parse) re-attempted `from steps import
# step_03_l2_ext` on EVERY part, so a hermetic environment missing the
# `requests` package logged "L2 vendor resolve unavailable for <part>: No
# module named 'requests'" once per unresolved MPN (8x in the LT-21 hermetic
# capture). Availability is a run-level property (not per-part), so it is now
# decided once per run and cached at module scope.
#
# Two separate cached decisions, not one, because the two reach points have
# different dependencies: the warm-path peek is a pure local-cache read that
# needs no credentials (only the import), while the network resolve needs
# both. Gating the peek on credentials too would silently regress warm-path
# MPN threading for boards whose PDF was already cached from an earlier,
# credentialed run — see test_warm_path_l1_hit_peeks_l2_cache_for_confirmed_
# expansion, which deliberately exercises the peek with no credentials set.
#
# LT-35 Phase 2: this file no longer names vendor-specific code, env-var
# names, or endpoint hosts at all — both reach points go through
# `steps.step_03_l2_ext`, a private-tier-only seam (excluded from the public
# export manifest) that wraps the real vendor client. See that module's
# docstring for why importing it is still an equivalent availability probe.

_L2_IMPORT_OK: bool | None = None  # None = unprobed this run

_L2_DISABLED_LOGGED = False  # LT-29: unified L2-off notice, at most once per run


def _log_l2_disabled_once() -> None:
    """Unified user-facing 'L2 off' notice (LT-29) — emitted at most once per
    run regardless of which reach point (import probe vs credential check)
    first discovers it. Per-site reason detail stays at debug level."""
    global _L2_DISABLED_LOGGED
    if not _L2_DISABLED_LOGGED:
        logger.warning(
            "[STEP 03] L2 vendor lookup: off (optional — the vendor-lookup "
            "extension isn't installed, or no credentials are configured). "
            "Using local datasheets only."
        )
        _L2_DISABLED_LOGGED = True


def _l2_import_ok() -> bool:
    """Whether `steps.step_03_l2_ext` can be imported at all — decided
    once per run, independent of credentials. Gates the warm-path cache peek
    directly, and feeds _l2_resolve_ok below."""
    global _L2_IMPORT_OK
    if _L2_IMPORT_OK is None:
        try:
            from steps import step_03_l2_ext  # noqa: F401 — import-only probe
        except Exception as e:
            logger.debug(
                "[STEP 03] L2 vendor resolver disabled (import failed: %s) — "
                "datasheet resolution limited to local cache.", e,
            )
            _log_l2_disabled_once()
            _L2_IMPORT_OK = False
        else:
            _L2_IMPORT_OK = True
    return _L2_IMPORT_OK


_L2_RESOLVE_OK: bool | None = None  # None = unprobed this run


def _l2_resolve_ok() -> bool:
    """Whether the L2 network-resolve tier (_l2_vendor_resolve) is available
    this run — decided once, via the shared import probe first (memoized,
    so this costs nothing extra), then a credential check delegated to
    steps.step_03_l2_ext (this file names no vendor env-var directly)."""
    global _L2_RESOLVE_OK
    if _L2_RESOLVE_OK is None:
        if not _l2_import_ok():
            _L2_RESOLVE_OK = False  # already logged "off" by _l2_import_ok
        else:
            from steps import step_03_l2_ext
            if not step_03_l2_ext.available():
                logger.debug(
                    "[STEP 03] L2 vendor resolver disabled (no credentials configured) — "
                    "datasheet resolution limited to local cache."
                )
                _log_l2_disabled_once()
                _L2_RESOLVE_OK = False
            else:
                _L2_RESOLVE_OK = True
    return _L2_RESOLVE_OK


def _l2_vendor_resolve(part_number: str, *, expansion_out: dict | None = None) -> str | None:
    """Tier-2 fallback: look up the MPN via the L2 vendor seam and download its
    datasheet into DATASHEETS_DIR/<manufacturer>/ so the downstream parse path
    runs on it (and L1 finds it next run). Availability (credentials +
    importable client) is gated once per run via _l2_resolve_ok, not
    re-probed per part. Never raises."""
    if not _l2_resolve_ok():
        return None
    try:
        from steps import step_03_l2_ext
        return step_03_l2_ext.resolve_and_download(
            part_number, DATASHEETS_DIR, expansion_out=expansion_out)
    except Exception as e:  # an unexpected per-part L2 error (not import/creds) → L1-miss behavior
        logger.warning(f"[STEP 03] L2 vendor resolve unavailable for {part_number}: {e}")
        return None


class _PageParseTimeout(Exception):
    """Raised inside the SIGALRM handler when a single page.extract_text()
    call exceeds PAGE_PARSE_TIMEOUT_SECONDS. Never escapes _extract_page_text_guarded."""


def _read_rss_kb() -> int | None:
    """Best-effort current-process RSS in KB from /proc/self/status. Returns
    None if unreadable (e.g. non-Linux) — the RSS-delta guard then simply
    never trips, leaving the wall-clock guard as the sole (still load-bearing)
    mechanism."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])  # kB
    except (OSError, ValueError, IndexError):
        return None
    return None


def _cumulative_rss_over_ceiling(baseline_kb: int | None) -> bool:
    """TODO-227 Phase 3a: per-document cumulative-RSS ceiling check, shared
    by BOTH pdfplumber page-parsing loops (_parse_pdf's fallback branch and
    patch_image_placeholders). `baseline_kb` must be THIS document's own
    parse-start RSS reading (captured once, before that document's loop
    begins) — never an absolute RSS level; see PDFPLUMBER_RSS_CEILING_DELTA_KB's
    definition for why a delta basis is mandatory. Returns False (never
    trips) if RSS is unreadable (e.g. non-Linux) — same fail-open convention
    as the per-page RSS-delta guard above."""
    if baseline_kb is None:
        return False
    current_kb = _read_rss_kb()
    if current_kb is None:
        return False
    return (current_kb - baseline_kb) >= PDFPLUMBER_RSS_CEILING_DELTA_KB


def _extract_page_text_guarded(page, page_num: int, pdf_path: str, *,
                                skip_sink: list | None = None) -> str:
    """Shared per-page cost guard for BOTH pdfplumber page-parsing loops
    (_parse_pdf's fallback branch and patch_image_placeholders). Runs
    page.extract_text() under a per-page wall-clock guard (SIGALRM, primary)
    and a per-page RSS-delta guard (secondary, belt-and-suspenders).

    On EITHER guard tripping: skip the page (return ""), log a WARNING naming
    the PDF, the 1-indexed page number, and which guard tripped. Pages that
    complete under both thresholds are completely unaffected — same return
    value, same control flow, no observable difference (verdict-safety
    requirement: this guard must be inert on every board in the precision
    corpus).

    `skip_sink` (TODO-227 Phase 2b): an optional caller-supplied accumulator
    list — a guard trip appends {"page": page_num, "reason": "timeout"|"rss"}
    to it. Chosen over changing the return type to a (text, skip) tuple: that
    would force every existing direct-call test (test_guard_inert_on_fast_page
    and its siblings) to unpack a tuple for no behavioral reason. This is
    strictly additive — every caller that omits skip_sink (the default, None)
    sees byte-identical behavior to before this parameter existed. Feeds the
    persistent pdfplumber-cache skip-manifest (_save_pdfplumber_cache).

    Uses signal.setitimer(signal.ITIMER_REAL, ...) rather than signal.alarm()
    directly: both arm the same SIGALRM-based wall-clock mechanism, but
    setitimer accepts a float, which alarm() does not (alarm() only takes
    whole seconds — PAGE_PARSE_TIMEOUT_SECONDS would be unpatchable to a fast
    sub-second value for tests otherwise). Only safe to call from the main
    thread of the main process — true here: run_checks.py's --workers
    uses ProcessPoolExecutor (separate processes), never threading, so every
    worker process's own main thread runs this call.

    Catches the timeout via a flag, not `except _PageParseTimeout` alone:
    pdfplumber's own `.layout`/`.chars` properties wrap ANY exception raised
    mid-parse (including our injected `_PageParseTimeout`) into their own
    `pdfminer.utils.exceptions.PdfminerException` — confirmed live against
    MCP4726A0T-E_MAY.pdf's actual pathological page. `except Exception:`
    gated on the `_timed_out` flag catches the timeout regardless of any
    such wrapping, while still re-raising any genuinely unrelated parse
    error unchanged (never silently swallowed).
    """
    pdf_name = os.path.basename(pdf_path)
    rss_before = _read_rss_kb()

    timed_out = False

    def _on_alarm(signum, frame):
        nonlocal timed_out
        timed_out = True
        raise _PageParseTimeout()

    old_handler = signal.signal(signal.SIGALRM, _on_alarm)
    signal.setitimer(signal.ITIMER_REAL, PAGE_PARSE_TIMEOUT_SECONDS)
    try:
        text = page.extract_text() or ""
    except Exception:
        if not timed_out:
            raise  # genuinely unrelated parse error — never silently swallowed
        logger.warning(
            "[STEP 03] Skipping page %d of %s — wall-clock guard tripped "
            "(page.extract_text() exceeded %.1fs)",
            page_num, pdf_name, PAGE_PARSE_TIMEOUT_SECONDS,
        )
        if skip_sink is not None:
            skip_sink.append({"page": page_num, "reason": "timeout"})
        return ""
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)

    rss_after = _read_rss_kb()
    if rss_before is not None and rss_after is not None:
        delta = rss_after - rss_before
        if delta > PAGE_PARSE_RSS_DELTA_LIMIT_KB:
            logger.warning(
                "[STEP 03] Skipping page %d of %s — RSS-delta guard tripped "
                "(+%dKB > %dKB limit)",
                page_num, pdf_name, delta, PAGE_PARSE_RSS_DELTA_LIMIT_KB,
            )
            if skip_sink is not None:
                skip_sink.append({"page": page_num, "reason": "rss"})
            return ""

    return text


def patch_image_placeholders(markdown_text: str, pdf_path: str, *,
                              skip_sink: list | None = None,
                              truncation_out: dict | None = None) -> tuple[str, list[int]]:
    """Idempotency (TODO-227 Phase 2a): a page whose patch marker already
    exists in markdown_text is never re-patched — `already_patched_pages` is
    read BEFORE `candidate_pages` is built, and excluded from it, so a page
    patched by an earlier call can never be re-selected by zip() on a later
    call against this same (or supersets of this) text. This is what makes
    re-invoking this function on its own output a byte-identical no-op, and
    what stops a placeholder/candidate-count mismatch from producing one more
    duplicate round of the same markers every time the pipeline touches this
    part (the corpus-observed defect: up to 119x duplication on one part,
    116/757 cached parts affected — see investigation/recon_reports/
    todo227_pdfplumber_cache_recon.md §4).

    `truncation_out` (TODO-227 Phase 3a): an optional caller-supplied dict —
    set to {"truncated": True} if the per-document cumulative-RSS ceiling
    stops this pass from reaching every page. Chosen as an accumulator-style
    out-param (mirroring skip_sink) rather than widening the return tuple, to
    stay additive for every existing caller. On a truncation, pages beyond
    the stop point are simply absent from this pass's candidates — they
    remain as raw placeholders for a future retry (truncation is never
    "done"; see _save_patch_state's truncated field).
    """
    if not PLACEHOLDER_RE.search(markdown_text):
        return markdown_text, []

    placeholder_matches = list(PLACEHOLDER_RE.finditer(markdown_text))
    already_patched_pages = {int(n) for n in PATCH_MARKER_RE.findall(markdown_text)}
    logger.info(f"[STEP 03] MinerU produced {len(placeholder_matches)} image placeholder(s) — patching with pdfplumber fallback")

    page_texts = []
    with pdfplumber.open(pdf_path) as pdf:
        rss_baseline = _read_rss_kb()
        for i, page in enumerate(pdf.pages):
            if _cumulative_rss_over_ceiling(rss_baseline):
                logger.warning(
                    "[STEP 03] pdfplumber cumulative-RSS ceiling (%d KB) reached "
                    "at page %d while patching placeholders in %s — stopping "
                    "further candidate patching this pass",
                    PDFPLUMBER_RSS_CEILING_DELTA_KB, i + 1, os.path.basename(pdf_path),
                )
                if truncation_out is not None:
                    truncation_out["truncated"] = True
                break
            page_texts.append(_extract_page_text_guarded(page, i + 1, pdf_path, skip_sink=skip_sink))

    candidate_pages = [
        (i, text) for i, text in enumerate(page_texts)
        if any(phrase in text for phrase in TARGET_PHRASES)
        and (i + 1) not in already_patched_pages
    ]

    if not candidate_pages:
        if already_patched_pages:
            logger.warning(
                "[STEP 03] %d raw placeholder(s) remain in %s but all %d "
                "candidate page(s) are already patched — nothing further to "
                "patch this pass",
                len(placeholder_matches), os.path.basename(pdf_path),
                len(already_patched_pages),
            )
        else:
            logger.warning("[STEP 03] No candidate pages found to patch placeholders")
        return markdown_text, []

    # Explicit mismatch handling (never silently zip()-truncate): patch
    # whatever is mappable this pass and log both counts plus the remainder
    # so a persistent under-count is visible instead of discovered later as
    # an ever-growing pile of duplicate markers.
    if len(placeholder_matches) != len(candidate_pages):
        n_patch = min(len(placeholder_matches), len(candidate_pages))
        n_remainder = abs(len(placeholder_matches) - len(candidate_pages))
        logger.warning(
            "[STEP 03] Placeholder/candidate-page count mismatch for %s: "
            "%d raw placeholder(s) vs %d candidate page(s) — patching %d, "
            "%d will remain unpatched this pass",
            os.path.basename(pdf_path), len(placeholder_matches),
            len(candidate_pages), n_patch, n_remainder,
        )

    patched = markdown_text
    patched_pages = []
    for match, (page_idx, page_text) in zip(placeholder_matches, candidate_pages):
        replacement = (
            f"\n<!-- pdfplumber patch: page {page_idx + 1} -->\n"
            f"```\n{page_text}\n```\n"
        )
        patched = patched.replace(match.group(0), replacement, 1)
        patched_pages.append(page_idx + 1)
        logger.info(f"[STEP 03] Patched placeholder → page {page_idx + 1} ({len(page_text)} chars)")

    return patched, patched_pages


def _parse_pdf(pdf_path: str, *, skip_sink: list | None = None,
               path_out: dict | None = None) -> tuple[str, bool]:
    """Returns (markdown_text, mineru_used). `skip_sink` (TODO-227 Phase 2b):
    forwarded to the fallback loop's _extract_page_text_guarded calls — see
    that function's docstring. Optional, additive; omitted by every caller
    that predates this parameter.

    `path_out` (TODO-388 Phase 2, R-α): an optional caller-supplied dict that
    receives {"markdown_path": <str>} naming the MinerU markdown this call
    actually used — which root it came from is now a runtime question (a
    canonical hit, a staged hit, or a fresh parse into the write root), and
    resolve_and_parse must patch the file it really read rather than
    re-deriving a path that may name a different tier. Same additive out-param
    idiom as skip_sink / truncation_out / expansion_out; unset on the
    pdfplumber-fallback path, where there is no MinerU markdown at all."""
    output_root = bucket_b_write_root()
    mineru_out = str(mineru_markdown_path(pdf_path, output_root))

    # READ: canonical always wins; staging only on a canonical miss (S2 shape).
    cached_md = _first_existing_bucket_b(pdf_path, mineru_markdown_path)
    if cached_md is not None:
        logger.info(f"[STEP 03] Using cached MinerU output: {cached_md}")
        if path_out is not None:
            path_out["markdown_path"] = str(cached_md)
        with open(cached_md) as f:
            return f.read(), True

    # Try MinerU — skip for large PDFs (multi-product family sheets)
    pdf_size = os.path.getsize(pdf_path)
    if pdf_size > MAX_PDF_SIZE_FOR_MINERU:
        logger.info(
            f"[STEP 03] PDF too large for MinerU "
            f"({pdf_size / 1024 / 1024:.1f} MB > {MAX_PDF_SIZE_FOR_MINERU // (1024 * 1024)} MB) — using pdfplumber directly"
        )
    else:
        try:
            result = subprocess.run(
                ["magic-pdf", "-p", pdf_path, "-o", str(output_root), "-m", "auto"],
                capture_output=True,
                text=True,
                timeout=600,
            )
            # MinerU writes into <output_root>/<verbatim-stem>/ using the PDF's
            # on-disk filename case; our cache key is canonical (lower-cased).
            # Reconcile so the existence check below — and every later reader —
            # finds the output under the canonical directory (TODO-321), and so
            # no case-variant sibling is left to ambiguate the stem (TODO-388).
            _reconcile_mineru_output(pdf_path, output_root=output_root)
            _prune_staged_images(pdf_path, output_root)
            if result.returncode == 0 and os.path.exists(mineru_out):
                logger.info(f"[STEP 03] MinerU parsed {pdf_path} → {mineru_out}")
                if path_out is not None:
                    path_out["markdown_path"] = mineru_out
                with open(mineru_out) as f:
                    return f.read(), True
            else:
                logger.warning(f"[STEP 03] MinerU failed (rc={result.returncode}), falling back to pdfplumber")
        except FileNotFoundError as e:
            logger.debug(f"[STEP 03] MinerU unavailable ({e}), falling back to pdfplumber")
            logger.warning(
                f"[STEP 03] Parsing {pdf_path} with pdfplumber (MinerU not "
                "installed; optional, improves table fidelity)."
            )
        except subprocess.TimeoutExpired as e:
            logger.debug(f"[STEP 03] MinerU unavailable ({e}), falling back to pdfplumber")
            logger.warning(
                f"[STEP 03] Parsing {pdf_path} with pdfplumber (MinerU timed "
                "out on this file; falling back)."
            )

    # Fallback: pdfplumber (capped to avoid OOM on large datasheets). Persistent
    # cross-run cache (TODO-227 Phase 2b) — see the "persistent pdfplumber
    # cache" section above.
    cached = _load_pdfplumber_cache(pdf_path)
    if cached is not None:
        text, _manifest = cached
        logger.info(
            "[STEP 03] Using cached pdfplumber fallback output: %s",
            _first_existing_bucket_b(pdf_path, _pdfplumber_md_path)
            or _pdfplumber_md_path(pdf_path),
        )
        return text, False

    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            pages_total = len(pdf.pages)
            skips = skip_sink if skip_sink is not None else []
            rss_baseline = _read_rss_kb()
            texts = []
            truncated = None
            for i, page in enumerate(pdf.pages):
                if _cumulative_rss_over_ceiling(rss_baseline):
                    truncated = {"page": i + 1, "reason": "rss_ceiling"}
                    logger.warning(
                        "[STEP 03] pdfplumber cumulative-RSS ceiling (%d KB) reached "
                        "at page %d of %d for %s — truncating this document's parse",
                        PDFPLUMBER_RSS_CEILING_DELTA_KB, i + 1, pages_total,
                        os.path.basename(pdf_path),
                    )
                    break
                texts.append(_extract_page_text_guarded(page, i + 1, pdf_path, skip_sink=skips))
            text = "\n".join(texts)
        logger.info(f"[STEP 03] pdfplumber parsed {pdf_path}")
        _save_pdfplumber_cache(
            pdf_path, text, pages_total=pages_total, pages_parsed=len(texts), skips=skips,
            truncated=truncated)
        return text, False
    except ImportError:
        raise RuntimeError("Neither MinerU nor pdfplumber is available for PDF parsing")


def get_pin_groups_cache_path(pdf_path: str) -> Path:
    """Returns path for cached pin groups JSON alongside the MinerU markdown."""
    pdf_stem = canonical_pdf_stem(pdf_path)
    return cache_paths.pin_groups_cache_path(PARSED_DIR, pdf_stem)


def cache_write_target(pdf_path: str) -> Path:
    """Where a FRESH extraction is written (TODO-386 Phase 3, R-C).

    Always the staging tier, unconditionally — the read switch
    (`staging_enabled`) governs serving only. Making this conditional would
    reintroduce exactly the thing the tier exists to remove: an extract path
    that can mutate the canonical tree. The canonical tree changes only via
    export/promote_staged.py."""
    return cache_paths.pin_groups_cache_path(STAGING_DIR, canonical_pdf_stem(pdf_path))


def persist_provenance_if_missing(pdf_path: str | None, cached: dict,
                                  cache_path=None) -> str:
    """Self-healing backfill: if a loaded cache lacks provenance.source_hash and its source PDF
    resolves, persist a computed provenance block into the cache file. VERDICT-INERT — writes ONLY
    the `provenance` block; `pin_groups` and `extraction_meta` (the real extractor value) are left
    byte-identical. Non-destructive: copy-before-write to a `.prebackfill` sidecar. The resolver
    already computes build_provenance to stamp reports; this just persists it so the null-ratio
    caveat / export combine-rule / recall data-era coverage close at the root.

    `cache_path` (TODO-386 Phase 3) names the file to heal. It defaults to the
    CANONICAL path exactly as before; the staging serve path passes its own
    staged path so a staged serve can never backfill — i.e. write — into the
    canonical tree.

    Returns 'stamped' | 'already' | 'no_pdf' | 'skipped'."""
    prov = (cached or {}).get("provenance")
    if prov and prov.get("source_hash"):
        return "already"
    # source_hash needs a resolvable PDF; without one the cache stays legacy_unverified (honest).
    if _pdf_sha256(pdf_path) is None:
        return "no_pdf"
    cache_path = Path(cache_path) if cache_path is not None \
        else get_pin_groups_cache_path(pdf_path)
    if not cache_path.exists():
        return "skipped"
    try:
        on_disk = json.loads(cache_path.read_text())
    except (json.JSONDecodeError, OSError):
        return "skipped"
    if (on_disk.get("provenance") or {}).get("source_hash"):
        return "already"  # already stamped on disk (race)
    try:
        shutil.copy2(cache_path, cache_path.with_name(cache_path.name + ".prebackfill"))
    except OSError:
        pass  # backup best-effort; the write itself only ADDS a field, never mutates pin_groups
    on_disk["provenance"] = build_provenance(pdf_path)  # pin_groups + extraction_meta untouched
    cache_path.write_text(json.dumps(on_disk, indent=2))
    logger.info("[STEP 03] backfilled provenance for legacy cache %s (extractor preserved)",
                cache_path.name)
    return "stamped"


def load_cached_pin_groups(pdf_path: str | None, *, stem: str | None = None) -> dict | None:
    """TODO-362 Phase 1: pdf_path may be None when the caller already resolved
    a cache directory (find_cached_dir_for_part) without a physical PDF on
    disk — pass its stem explicitly via `stem` in that case (bypasses
    canonical_pdf_stem, which requires a real path string to derive a stem
    from). When pdf_path is given, the stem is derived from it exactly as
    before and the stem kwarg is ignored. Returns None if neither identifies
    a cache (i.e. pdf_path is None and stem is not given).

    TODO-386 Phase 3 (S2): the second staging-fallback seam. Canonical is
    always tried first; staging is consulted only when staging reads are
    enabled AND the canonical outcome was 'absent' — a clean miss. A canonical
    manifest REFUSAL or an unreadable canonical cache is terminal and is never
    rescued from staging: rescuing a refusal would let an unpromoted file
    stand in for an integrity-failed shipped one, which is the exact substitution
    verify_against_manifest exists to prevent.

    Every dict this returns carries BOTH `cache_currency` (R-D — set on every
    serve path, staged included) and `cache_tier` ('canonical' | 'staged')."""
    resolved_stem = canonical_pdf_stem(pdf_path) if pdf_path is not None else stem
    if resolved_stem is None:
        return None
    cached, outcome = _load_pin_groups_from_root(PARSED_DIR, resolved_stem, pdf_path)
    if cached is not None:
        cached["cache_tier"] = CACHE_TIER_CANONICAL
        return cached
    if outcome != "absent" or not staging_enabled():
        return None
    cached, _ = _load_pin_groups_from_root(STAGING_DIR, resolved_stem, pdf_path)
    if cached is not None:
        cached["cache_tier"] = CACHE_TIER_STAGED
        logger.info("[STEP 03] serving %s from the STAGING tier (unpromoted) — "
                    "canonical had no cache for this stem.", resolved_stem)
    return cached


def _load_pin_groups_from_root(root, resolved_stem: str,
                               pdf_path: str | None) -> tuple[dict | None, str]:
    """load_cached_pin_groups' body, against ONE cache root.

    Returns (cached, outcome) with outcome ∈ 'ok' | 'absent' | 'unreadable' |
    'refused'. Only 'absent' is a clean miss (see the caller)."""
    cache_path = cache_paths.pin_groups_cache_path(root, resolved_stem)
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
        except json.JSONDecodeError:
            return None, "unreadable"
        # Stale-detection: log-only. Does NOT auto-invalidate or re-extract — the
        # cache is still returned and used. Acting on staleness is M4's targeted
        # re-extract; this is just the detector (cache-schema-hardening).
        #
        # TODO-368 Phase 2: `status` used to be discarded after the self-heal
        # decision below — computed, consulted for one branch, then dropped. It is
        # now captured and attached to the returned dict as `cache_currency` so
        # step_10_report's evidence-tier layer can read it (the only other
        # existing per-refdes signal, pipeline.run_board's `resolver_provenance`,
        # is dead code that never reaches build_report — see
        # investigation/experiments/todo368_label_consumer_survey/report.md §6).
        # LT-38 Phase 3b: shipped-cache manifest integrity, BEFORE the self-heal
        # below — a refusal must not be preceded by a write to the very file
        # being refused.
        serve, _manifest_state_reason = verify_against_manifest(
            cache_path, pdf_path, cached)
        if not serve:
            # honest miss -> UNRESOLVABLE / datasheet-required path. NOT 'absent':
            # a refusal must never fall through to the staging tier.
            return None, "refused"
        governed = manifest_entry_for(cache_path) is not None
        # TODO-386 Phase 3: a staged file lives outside PARSED_DIR, so
        # manifest_entry_for's relative_to() cannot resolve it -> None ->
        # `unlisted` in verify_against_manifest above (R-A: staged entries ride
        # the existing unlisted branch; CACHE_MANIFEST.json is untouched).
        status = None
        try:
            status = check_cache_provenance(pdf_path, cached)
            if status == "legacy_unverified" and governed:
                # A manifest-governed file is an integrity-pinned artifact: the
                # backfill would rewrite it and invalidate its own manifest
                # entry, refusing every subsequent load. Inert on the current
                # ship-list (all 114 already carry provenance.source_hash, so
                # this branch cannot be reached by them) — a guard, not a fix.
                logger.debug("[STEP 03] cache %s is manifest-governed and "
                             "legacy_unverified; skipping provenance backfill.",
                             cache_path.name)
            elif status == "legacy_unverified":
                # Self-healing: persist computed provenance if a PDF resolves (verdict-inert).
                # cache_path is passed EXPLICITLY (TODO-386 Phase 3): the default
                # would heal the canonical path even when serving from staging.
                outcome = persist_provenance_if_missing(pdf_path, cached,
                                                        cache_path=cache_path)
                if outcome == "stamped":
                    cached = json.loads(cache_path.read_text())  # return the stamped dict
                else:
                    logger.debug("[STEP 03] cache %s legacy_unverified (backfill: %s)",
                                 cache_path.name, outcome)
        except Exception as e:  # detection/backfill must never break the load path
            logger.debug("[STEP 03] provenance check failed for %s: %s", cache_path.name, e)
        # Set UNCONDITIONALLY (TODO-368 banked caveat): a missing key reads as
        # None downstream, and _tier_for_status(None) fails OPEN to
        # confirmed_local. Every serve path leaves this key present.
        cached["cache_currency"] = status
        return cached, "ok"
    return None, "absent"


def build_extraction_meta(
    extractor: str,
    model: str | None = None,
    source: str | None = None,
) -> dict:
    """Provenance stamp for a _pin_groups.json file.

    `extractor` is the canonical producer: "gemma", "haiku_pdfplumber",
    "manual", or "unknown". Shape-based guessing of provenance is unreliable
    (see the SN74HC595 / STM32F103C8 cases) — the only trustworthy source is
    the writer stamping itself at write time. "claude-code" is a legacy
    on-disk value (pre-TODO-365 P0-4 stamps; see extractors.py's
    HaikuPdfplumberExtractor, the producer both values refer to).
    """
    if model is None and extractor == "gemma":
        model = OLLAMA_MODEL
    return {
        "extractor": extractor,
        "model": model,
        "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "schema_version": EXTRACTION_SCHEMA_VERSION,
    }


# ── cache provenance (stale-detection; cache-schema-hardening) ───────────────

def parse_config() -> dict:
    """Current parse-config that determines the extraction INPUT (the .md). A
    change here means a cached .md/pin_groups is no longer reproducible — the
    stale-cache signal the lucky-parse + M2 drift exposed."""
    return {
        "max_pdf_size_for_mineru": MAX_PDF_SIZE_FOR_MINERU,
        "target_phrases": sorted(TARGET_PHRASES),
        "mineru_version": MINERU_VERSION,
        "provenance_schema_version": PROVENANCE_SCHEMA_VERSION,
    }


# Which of parse_config()'s keys actually influence extraction INPUT, per
# extractor impl (TODO-397; investigation/recon_reports/
# todo397_398_currency_label_recon.md, Part A). haiku_pdfplumber
# (extractors.py DEFAULT_EXTRACTOR) reads the PDF directly via
# extract_pdfplumber_full() and never touches ctx.markdown_text — none of
# these four keys are causally connected to what it parses, confirmed by a
# full call-path trace in the recon. gemma_mineru is the only impl whose
# input is gated by MAX_PDF_SIZE_FOR_MINERU (MinerU-vs-pdfplumber routing)
# and TARGET_PHRASES (patch_image_placeholders), so parse_config() stays
# fully authoritative for it. Fingerprinting haiku's OWN actual inputs
# (pdfplumber version, extract_pdfplumber_full's own logic) is a distinct,
# deferred card — this table only stops parse_config() from asserting
# currency claims it can't back for the shipped extractor.
CURRENCY_KEYS_BY_EXTRACTOR = {
    "haiku_pdfplumber": (),
    "gemma_mineru": ("max_pdf_size_for_mineru", "target_phrases",
                     "mineru_version", "provenance_schema_version"),
}


def _pdf_sha256(pdf_path: str | None) -> str | None:
    """None (TODO-362 Phase 1: no physical PDF for this part) short-circuits
    to None without attempting to open anything — every existing caller
    already treats a None return as 'can't verify from the PDF', the same
    outcome a missing-file OSError produces, so this is not a new code path
    for them, just a new way to reach the existing one."""
    if pdf_path is None:
        return None
    try:
        h = hashlib.sha256()
        with open(pdf_path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _source_hash(pdf_sha: str | None, cfg: dict) -> str | None:
    """SHA256 of (PDF content + parse-config). Recomputable on load WITHOUT
    re-parsing; catches both PDF change and parse-config change."""
    if pdf_sha is None:
        return None
    canon = json.dumps(cfg, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((pdf_sha + "|" + canon).encode()).hexdigest()


def _current_parse_quality(pdf_path: str) -> str:
    """Fullness current code's parse would have for this PDF: always
    'current_code_full' — MinerU-eligible PDFs get a full MinerU parse, and
    the pdfplumber fallback (TODO-227 Phase 3a) no longer has a static
    page-count cap to under-fill against; any truncation is now a dynamic
    per-document cumulative-RSS-ceiling event that can't be predicted from
    the PDF alone (see PDFPLUMBER_RSS_CEILING_DELTA_KB). (Backfill still
    overrides to 'full_precap' for pre-cap parses recorded before this code
    existed — measured complete, not necessarily "richer": TODO-328 Phase 0
    (corpus_results/todo328_phase0_probe_evidence.md) found the page-count
    cap this label predates was never actually enforced on the
    extract_pdfplumber_full() path, and every full_precap cache probed
    reached the last page of its PDF.)"""
    return "current_code_full"


def build_provenance(pdf_path: str, *, parse_quality: str | None = None,
                     extractor_impl: str | None = None,
                     input_path: str | None = None,
                     doc_identity: dict | None = None) -> dict:
    cfg = parse_config()
    pdf_sha = _pdf_sha256(pdf_path)
    try:
        size = os.path.getsize(pdf_path)
    except OSError:
        size = None
    prov = {
        "source_hash": _source_hash(pdf_sha, cfg),
        "source_pdf_sha256": pdf_sha,
        "pdf_size": size,
        "parse_config": cfg,
        "parse_quality": parse_quality or _current_parse_quality(pdf_path),
        "stamped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    # Extractor-stage identity (impl + input-path), recorded as sibling fields.
    # Deliberately OUTSIDE the hash-signed parse_config: parse_config signs what
    # changes the parsed BYTES; impl identity is producer metadata. Keeping it out
    # of the hash avoids flagging every pre-interface cache as "stale" on load.
    if extractor_impl is not None:
        prov["extractor_impl"] = extractor_impl
    if input_path is not None:
        prov["input_path"] = input_path
    # Doc-identity evidence (TODO-337 Phase 2b): does this cache's stem actually
    # appear in the text of the PDF it claims to come from? Same category as the
    # two keys above — producer metadata about WHICH DOCUMENT, not about the
    # parsed bytes — so it takes the same slot, OUTSIDE the hash-signed
    # parse_config. Adding it inside parse_config would flip every existing cache
    # stale (the TODO-227 Phase 3a episode). Evidence only: nothing refuses,
    # quarantines or invalidates on it.
    if doc_identity is not None:
        prov["doc_identity"] = doc_identity
    return prov


def check_cache_provenance(pdf_path: str | None, cached: dict) -> str:
    """Load-time stale detector — LOG ONLY (never auto-invalidates/re-extracts).
    Returns 'current' | 'stale' | 'legacy_unverified' | 'current_no_pdf_verify'.
    Cheap in the common case: compares parse_config + pdf_size (no hashing)
    and only full-hashes on a mismatch to confirm.

    pdf_path=None (TODO-362 Phase 1, PDF-optional warm path): no physical PDF
    is on disk for this part to verify against. A stamped source_hash is
    trusted as-is — 'current_no_pdf_verify', never silently 'current' (that
    would claim a byte comparison that never happened) and never
    'legacy_unverified' (that status means the CACHE itself lacks a hash to
    trust — an orthogonal condition PDF-absence doesn't change)."""
    prov = (cached or {}).get("provenance")
    if not prov or not prov.get("source_hash"):
        return "legacy_unverified"
    if pdf_path is None:
        logger.debug(
            "[STEP 03] PDF-optional cache load (parse_quality=%s) — no PDF on "
            "disk to verify currency against; trusting the cache's stamped "
            "source_hash.",
            prov.get("parse_quality"),
        )
        return "current_no_pdf_verify"

    ext = prov.get("extractor_impl")
    try:
        cur_size = os.path.getsize(pdf_path)
    except OSError:
        cur_size = None

    if ext == "haiku_pdfplumber":
        # TODO-397: haiku_pdfplumber reads the PDF directly (extract_pdfplumber_
        # full) and never touches parse_config()'s keys (CURRENCY_KEYS_BY_
        # EXTRACTOR above) — comparing parse_config would false-flag "stale" on
        # a byte-identical PDF whenever an unrelated MinerU-only setting (e.g.
        # mineru_version, install-dependent) differs, which is exactly what a
        # stranger (MinerU-absent) install hit. Currency for this impl is
        # PDF identity alone: source_pdf_sha256 is the sole authority. A
        # pdf_size mismatch is a cost-saver short-circuit to stale (a
        # different-sized file cannot hash equal) — it never needs to be
        # authoritative on its own; the sha256 compare below always is.
        if (prov.get("pdf_size") is not None and cur_size is not None
                and prov.get("pdf_size") != cur_size):
            logger.debug(
                "[STEP 03] STALE cache for %s — pdf_size mismatch under "
                "haiku_pdfplumber currency (extractor_impl=%r); skipping "
                "the sha256 hash as a cost-saver (different size cannot "
                "hash equal). Detector only; not auto-invalidated.",
                os.path.basename(pdf_path), ext,
            )
        elif prov.get("source_pdf_sha256") == _pdf_sha256(pdf_path):
            return "current"
        else:
            logger.debug(
                "[STEP 03] STALE cache for %s — source_pdf_sha256 mismatch "
                "under haiku_pdfplumber currency (extractor_impl=%r); "
                "predates the current PDF on disk. Detector only; not "
                "auto-invalidated.",
                os.path.basename(pdf_path), ext,
            )
        # Reached only when the PDF's own bytes provably differ (size or
        # sha256 mismatch) — accurate to say so unconditionally, unlike the
        # shared parse_config path below where a pure config change (no PDF
        # change at all) could also land here.
        logger.warning(
            "[STEP 03] Note: cached pin data for %s was built from a different "
            "copy of this datasheet (vendor PDFs vary between downloads). Using "
            "cached pin data (currency not checked — use --refresh <part-stem> "
            "if the PDF changed).",
            os.path.basename(pdf_path),
        )
        return "stale"

    # ext == "gemma_mineru" or ext is None: existing fast path + fallback hash,
    # byte-for-byte unchanged.
    cur_cfg = parse_config()
    if prov.get("parse_config") == cur_cfg and prov.get("pdf_size") == cur_size:
        return "current"
    if _source_hash(_pdf_sha256(pdf_path), cur_cfg) == prov.get("source_hash"):
        return "current"
    logger.debug(
        "[STEP 03] STALE cache for %s — source_hash mismatch "
        "(cache parse_quality=%s); predates current PDF/parse-config. "
        "Detector only; not auto-invalidated.",
        os.path.basename(pdf_path), prov.get("parse_quality"),
    )
    logger.warning(
        "[STEP 03] Note: cached pin data for %s was built from a different "
        "copy of this datasheet (vendor PDFs vary between downloads). Using "
        "cached pin data (currency not checked — use --refresh <part-stem> "
        "if the PDF changed).",
        os.path.basename(pdf_path),
    )
    return "stale"


# ── shipped-cache integrity manifest (LT-38 Phase 3b) ───────────────────────
#
# datasheets_parsed/CACHE_MANIFEST.json is written by export/package_cache.py
# and pins a sha256 for every cache file the public tier ships. It governs
# ONLY the files it lists: anything else on disk is a user-local cache and is
# served under the existing tier machinery, untouched.
#
# This axis is INDEPENDENT of the PDF-currency axis (check_cache_provenance /
# TODO-367 d1). Manifest integrity answers "are these the bytes we published?";
# PDF currency answers "does the local PDF still match what was parsed?". A
# manifest refusal must never alter d1's serve+log behaviour, and d1 never
# suppresses a manifest refusal.

CACHE_MANIFEST_NAME = "CACHE_MANIFEST.json"

_manifest_state = {"dir": None, "data": None, "notice_logged": False}


def load_cache_manifest(parsed_dir=None) -> dict | None:
    """Read + memoize <parsed_dir>/CACHE_MANIFEST.json. None when absent or
    unreadable — absence is the normal state for a private/dev tree and is not
    an error. Memoized per directory so a corpus run hashes nothing twice."""
    d = str(parsed_dir if parsed_dir is not None else PARSED_DIR)
    if _manifest_state["dir"] != d:
        path = Path(d) / CACHE_MANIFEST_NAME
        data = None
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("[STEP 03] cache manifest at %s is unreadable (%s) "
                               "— treating as absent.", path, e)
        _manifest_state.update({"dir": d, "data": data, "notice_logged": False})
    if _manifest_state["data"] is None and not _manifest_state["notice_logged"]:
        _manifest_state["notice_logged"] = True
        logger.info("[STEP 03] no shipped-cache manifest at %s/%s — cache "
                    "integrity is not manifest-governed this run (unchanged "
                    "behaviour; report cache_version will be null).",
                    d, CACHE_MANIFEST_NAME)
    return _manifest_state["data"]


def cache_manifest_version(parsed_dir=None) -> str | None:
    """The manifest's version string, or None when no manifest exists. Read by
    step_10_report._cache_manifest_version for the report header."""
    return (load_cache_manifest(parsed_dir) or {}).get("cache_version")


def manifest_entry_for(cache_path) -> str | None:
    """The expected sha256 for `cache_path`, or None when the file is not
    manifest-governed (a user-local cache)."""
    manifest = load_cache_manifest()
    if not manifest:
        return None
    try:
        rel = Path(cache_path).resolve().relative_to(
            Path(str(PARSED_DIR)).resolve()).as_posix()
    except ValueError:
        return None
    return (manifest.get("files") or {}).get(rel)


def verify_against_manifest(cache_path, pdf_path: str | None,
                            cached: dict) -> tuple[bool, str]:
    """F4a. Returns (serve, reason).

    - not listed / no manifest -> (True, "unlisted"|"manifest_absent"): served
      under the existing tier machinery, no manifest governance.
    - listed and the on-disk sha256 matches -> (True, "manifest_verified").
    - listed and MISMATCHED -> never served AS SHIPPED CACHE. It is served only
      if a local PDF is present and the existing source_hash currency check
      confirms it (a legitimate local re-extraction of a governed part), i.e.
      (True, "manifest_mismatch_pdf_verified"). Otherwise (False, "refused"):
      a loud error naming the file and both hashes, and the part follows the
      honest UNRESOLVABLE / datasheet-required path.

    Hashing is lazy — only files that are actually served AND listed are read a
    second time to hash."""
    expected = manifest_entry_for(cache_path)
    if expected is None:
        return (True, "manifest_absent" if not load_cache_manifest() else "unlisted")
    actual = _file_sha256(cache_path)
    if actual == expected:
        return (True, "manifest_verified")
    if pdf_path is not None and check_cache_provenance(pdf_path, cached) == "current":
        logger.warning(
            "[STEP 03] shipped-cache manifest MISMATCH for %s (expected %s, "
            "found %s) — but the local PDF verifies this cache's source_hash, "
            "so it is served as a legitimate local re-extraction, NOT as "
            "shipped cache.", cache_path, expected, actual)
        return (True, "manifest_mismatch_pdf_verified")
    logger.error(
        "[STEP 03] REFUSING cache %s — shipped-cache manifest integrity check "
        "FAILED (expected sha256 %s, found %s) and no local PDF verifies it. "
        "This part will be reported UNRESOLVABLE; supply the datasheet PDF or "
        "restore the published cache file.", cache_path, expected, actual)
    return (False, "refused")


def _file_sha256(path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def would_degrade_parse(pdf_path: str, cached: dict) -> bool:
    """Commit-B quarantine: True when re-extracting under current code would LOWER
    a full_precap cache's parse fullness. Computed from PDF size + current config;
    no re-parse needed. Current code can only reproduce a *full* parse when MinerU
    runs (PDF <= cap); a full_precap cache is >cap by construction, so current code
    (pdfplumber fallback, shallower than MinerU) degrades it — the MinerU→pdfplumber
    skip loses content on its own (e.g. lm358n 312KB→113KB). TODO-227 Phase 3a
    retired the old static page-count cap as a second degrade axis: any pdfplumber
    truncation is now a dynamic per-document RSS-ceiling event, not something this
    function can predict statically from PDF size alone."""
    prov = (cached or {}).get("provenance")
    if not prov or prov.get("parse_quality") != "full_precap":
        return False
    try:
        return os.path.getsize(pdf_path) > MAX_PDF_SIZE_FOR_MINERU
    except OSError:
        return True  # conservative: protect if we can't size it


def save_pin_groups_cache(
    pdf_path: str,
    pin_groups: dict,
    *,
    extractor: str = "unknown",
    model: str | None = None,
    source: str | None = None,
    parse_quality: str | None = None,
    extractor_impl: str | None = None,
    input_path: str | None = None,
    doc_identity: dict | None = None,
    dest_path: str | None = None,
) -> None:
    # dest_path overrides the real cache location (scratch / dry-run preview): it
    # writes a faithful cache-shaped file elsewhere and skips the quarantine guard,
    # so the real cache is never touched.
    #
    # TODO-386 Phase 3 (R-C): the DEFAULT destination is the staging tier, not the
    # canonical tree — see cache_write_target. Everything below (quarantine guard,
    # case-collision warning, .pre_reextract sidecar) now applies to the staging
    # file, which is the file this call is actually about to overwrite.
    cache_path = Path(dest_path) if dest_path else cache_write_target(pdf_path)
    # Quarantine guard (Commit B): refuse to overwrite a full_precap cache with a
    # degraded re-extraction (would_degrade_parse's MinerU-vs-pdfplumber-fallback
    # shallowness axis, not a page-count cap -- TODO-328 Phase 0
    # (corpus_results/todo328_phase0_probe_evidence.md) confirmed no such cap was
    # ever enforced on extract_pdfplumber_full()) -- keep the full_precap cache
    # until the extraction-depth limit is fixed. Skipped when the caller
    # explicitly stamps parse_quality (intentional backfill/stamp, not a
    # degrading re-extract) or writes to a dest_path override (scratch preview).
    if dest_path is None and parse_quality is None and cache_path.exists():
        try:
            existing = json.loads(cache_path.read_text())
        except (json.JSONDecodeError, OSError):
            existing = None
        if existing and would_degrade_parse(pdf_path, existing):
            logger.warning(
                "[STEP 03] QUARANTINE: refusing to overwrite full_precap cache for "
                "%s with a shallower (fallback-axis) re-extract — keeping the "
                "probe-complete pre-cap cache (no page cap: see "
                "todo328_phase0_probe_evidence.md). Release when the "
                "extraction-depth limit is fixed.",
                cache_path.name,
            )
            return
    if dest_path is None:
        # root is unambiguously the staging tier here: this branch is
        # `dest_path is None`, i.e. exactly the case cache_write_target serves.
        _warn_on_case_collision(pdf_path, root=STAGING_DIR)
        # TODO-367 d3: copy-before-write sidecar on the overwrite path only (the
        # quarantine REFUSAL above already returned, so this never fires for a
        # refused overwrite). Single level, like .prebackfill: each subsequent
        # overwrite replaces the sidecar rather than accumulating history.
        if cache_path.exists():
            try:
                shutil.copy2(cache_path, cache_path.with_name(cache_path.name + ".pre_reextract"))
            except OSError as e:
                logger.warning(
                    "[STEP 03] Could not write pre_reextract sidecar for %s: %s "
                    "(overwrite proceeds; best-effort backup only)",
                    cache_path.name, e,
                )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    data = dict(pin_groups)  # shallow copy — don't mutate the caller's in-memory dict
    if source is None:
        source = f"{canonical_pdf_stem(pdf_path)}.md"
    data["extraction_meta"] = build_extraction_meta(extractor, model, source)
    data["provenance"] = build_provenance(
        pdf_path, parse_quality=parse_quality,
        extractor_impl=extractor_impl, input_path=input_path,
        doc_identity=doc_identity)
    cache_path.write_text(json.dumps(data, indent=2))


# ── TODO-227 Phase 2b: persistent pdfplumber cache ───────────────────────────
#
# Two structurally distinct engagements (recon report §1), two persistence
# mechanisms below:
#
#   Branch A — the full pdfplumber fallback (_parse_pdf's "Fallback: pdfplumber"
#   section). Was NEVER persisted before this: every resolve_and_parse call
#   re-ran the entire capped multi-hundred-page parse from scratch. New sibling
#   cache dir datasheets_parsed/<stem>/pdfplumber/ (deliberately NOT reusing
#   the MinerU-owned <stem>/auto/ tree — see recon §8 fork (b)) holds:
#     <stem>.md                 — the parsed markdown
#     <stem>.skipmanifest.json  — ALWAYS written alongside the .md, even when
#                                  skips == [] (see _save_pdfplumber_cache)
#   Keyed on source_hash (existing parse_config()-based mechanism, UNCHANGED —
#   parse_config() itself is never touched by this cycle) PLUS a separate
#   guard_config_hash (the two per-page guard constants, hashed independently
#   via _guard_config_hash — NOT folded into parse_config()/source_hash, so a
#   guard-threshold change can supersede a guard-tripped cache without
#   spuriously invalidating every other cache keyed by the same source_hash).
#
#   Branch B — the placeholder-patch pass (patch_image_placeholders). Already
#   persisted (overwrites the MinerU .md when patched_pages is non-empty,
#   Phase 2a made this idempotent), but re-scans the WHOLE PDF on every run
#   that still has a raw placeholder, even when nothing further can be
#   gained. patch_state.json (same sibling dir) records the raw-placeholder
#   count left over after the last real scan; a later run whose current raw
#   count matches that recorded residual (and whose source_hash/guard hash
#   still match) skips the whole-PDF re-scan entirely — see the call site in
#   resolve_and_parse.
#
# TODO-227 Phase 3a extension: the per-document cumulative-RSS ceiling
# (PDFPLUMBER_RSS_CEILING_DELTA_KB, retiring the old page-COUNT cap) can stop
# either loop before every page is attempted. Branch A's manifest gains a
# `truncated` field — {"page": N, "reason": "rss_ceiling"} or None — distinct
# from a per-page `skips` entry (a truncation means "the rest of the document
# was never attempted", not "one page was skipped and the rest completed");
# the supersede rule extends to fire on it too. Branch B's patch_state gains
# a `truncated` bool that is NEVER treated as a skippable match — a
# truncated patch pass always re-runs next time (the ceiling grants a fresh
# RSS-delta budget per document-parse, so a retry may get further, or may
# not; either way it must never be silently accepted as "done").

def _guard_config() -> dict:
    """The per-page guard thresholds PLUS the per-document RSS ceiling
    (TODO-227 Phase 3a's PDFPLUMBER_RSS_CEILING_DELTA_KB) — deliberately
    separate from parse_config() (recon §2/§8: folding them in would
    silently change what source_hash means for the unrelated pin_groups
    cache, which shares parse_config())."""
    return {
        "timeout_s": PAGE_PARSE_TIMEOUT_SECONDS,
        "rss_delta_limit_kb": PAGE_PARSE_RSS_DELTA_LIMIT_KB,
        "rss_ceiling_delta_kb": PDFPLUMBER_RSS_CEILING_DELTA_KB,
    }


def _guard_config_hash() -> str:
    canon = json.dumps(_guard_config(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


def _pdfplumber_cache_dir(pdf_path: str, output_root=None) -> Path:
    # TODO-388 R-α: rooted at stem_dir (the one derivation point) rather than a
    # fourth independent `Path(PARSED_DIR) / stem / ...` construction. This
    # branch's write is case-safe by construction (canonical_pdf_stem always),
    # which is exactly why it pre-creates the canonical top-level directory and
    # so triggers _reconcile_mineru_output's both-exist branch on a later
    # MinerU success — see that function.
    return stem_dir(pdf_path, output_root) / "pdfplumber"


def _pdfplumber_md_path(pdf_path: str, output_root=None) -> Path:
    return _pdfplumber_cache_dir(pdf_path, output_root) / f"{canonical_pdf_stem(pdf_path)}.md"


def _pdfplumber_manifest_path(pdf_path: str, output_root=None) -> Path:
    return (_pdfplumber_cache_dir(pdf_path, output_root)
            / f"{canonical_pdf_stem(pdf_path)}.skipmanifest.json")


def _pdfplumber_patch_state_path(pdf_path: str, output_root=None) -> Path:
    return _pdfplumber_cache_dir(pdf_path, output_root) / "patch_state.json"


def _atomic_write_text(path: Path, text: str) -> None:
    """temp+os.replace: the write is atomic on POSIX — a crash/kill mid-write
    leaves either the old final file (untouched) or nothing at the final
    path, never a truncated one. Non-destructive per CLAUDE.md's cache rule."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)


def _load_pdfplumber_cache(pdf_path: str) -> tuple[str, dict] | None:
    """Branch-A persistent-cache read. Returns (markdown_text, manifest) on a
    fresh HIT, else None (MISS — including a bare-manifest-no-.md orphan,
    which _save_pdfplumber_cache's write order makes possible only via a
    kill between writes, never a bare-.md-no-manifest orphan).

    HIT requires: .md AND manifest both exist, manifest.source_hash matches
    the current source_hash, AND (manifest.skips is empty AND manifest is not
    truncated — fast path, the guard-config comparison is skipped entirely
    since nothing was skipped/truncated for a guard-config change to rescue
    — OR guard_config_hash matches). Any other case is a MISS (supersede
    rule, extended in TODO-227 Phase 3a: nonempty skips OR a recorded
    truncation, combined with a changed guard-config hash)."""
    # TODO-388 R-α: canonical first, staging only on a canonical miss (S2
    # shape). Both halves must come from the SAME root — a manifest from one
    # tier paired with a .md from the other would silently validate the wrong
    # bytes against the wrong source_hash.
    for root in bucket_b_read_roots():
        hit = _load_pdfplumber_cache_from_root(pdf_path, root)
        if hit is not None:
            return hit
    return None


def _load_pdfplumber_cache_from_root(pdf_path: str, output_root) -> tuple[str, dict] | None:
    """_load_pdfplumber_cache's body, against ONE root."""
    manifest_path = _pdfplumber_manifest_path(pdf_path, output_root)
    md_path = _pdfplumber_md_path(pdf_path, output_root)
    if not manifest_path.exists() or not md_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    current_source_hash = _source_hash(_pdf_sha256(pdf_path), parse_config())
    if manifest.get("source_hash") != current_source_hash:
        return None

    skips = manifest.get("skips") or []
    truncated = manifest.get("truncated") is not None
    if (skips or truncated) and manifest.get("guard_config_hash") != _guard_config_hash():
        return None

    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError:
        return None
    return text, manifest


def _save_pdfplumber_cache(pdf_path: str, markdown_text: str, *,
                            pages_total: int, pages_parsed: int, skips: list,
                            truncated: dict | None = None) -> None:
    """Branch-A persistent-cache write. Manifest FIRST, then .md (both via
    _atomic_write_text) — a .md without a manifest must be impossible to
    observe; an orphan manifest (manifest exists, .md doesn't — the only
    state a kill between the two writes can produce) is harmless and is
    treated as a MISS by _load_pdfplumber_cache.

    `truncated` (TODO-227 Phase 3a): {"page": N, "reason": "rss_ceiling"} if
    the per-document RSS ceiling stopped this parse before pages_total, else
    None."""
    manifest = {
        "schema_version": PDFPLUMBER_CACHE_SCHEMA_VERSION,
        "source_hash": _source_hash(_pdf_sha256(pdf_path), parse_config()),
        "guard_config_hash": _guard_config_hash(),
        "guard_config": _guard_config(),
        "pages_total": pages_total,
        "pages_parsed": pages_parsed,
        "skips": skips,
        "truncated": truncated,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "producer": "pdfplumber_fallback",
    }
    write_root = bucket_b_write_root()
    _warn_on_case_collision(pdf_path, root=write_root)
    _atomic_write_text(_pdfplumber_manifest_path(pdf_path, write_root),
                       json.dumps(manifest, indent=2))
    _atomic_write_text(_pdfplumber_md_path(pdf_path, write_root), markdown_text)


def _load_patch_state(pdf_path: str) -> dict | None:
    path = _first_existing_bucket_b(pdf_path, _pdfplumber_patch_state_path)
    if path is None:
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _patch_state_matches(pdf_path: str, raw_placeholder_count: int) -> bool:
    """Branch-B skip check: True when a prior patch pass already established
    that raw_placeholder_count more raw placeholders are unpatchable (nothing
    new to gain from re-scanning), AND the PDF/parse-config/guard-config
    haven't changed since.

    TODO-227 Phase 3a: a truncated pass (the RSS ceiling stopped it before
    reaching every candidate) is NEVER a match, regardless of how well
    everything else lines up — it always falls through to a real re-run,
    because the ceiling grants a fresh RSS-delta budget per document-parse,
    so a retry might get further (or might not; either way "truncated" must
    never be silently accepted as "done")."""
    state = _load_patch_state(pdf_path)
    if state is None:
        return False
    if state.get("truncated"):
        return False
    current_source_hash = _source_hash(_pdf_sha256(pdf_path), parse_config())
    return (
        state.get("source_hash") == current_source_hash
        and state.get("guard_config_hash") == _guard_config_hash()
        and state.get("residual_unpatchable") == raw_placeholder_count
    )


def _save_patch_state(pdf_path: str, *, residual_unpatchable: int,
                       patched_pages_total: int, truncated: bool = False) -> None:
    state = {
        "schema_version": PDFPLUMBER_CACHE_SCHEMA_VERSION,
        "source_hash": _source_hash(_pdf_sha256(pdf_path), parse_config()),
        "guard_config_hash": _guard_config_hash(),
        "residual_unpatchable": residual_unpatchable,
        "patched_pages_total": patched_pages_total,
        "truncated": truncated,
        "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    write_root = bucket_b_write_root()
    _warn_on_case_collision(pdf_path, root=write_root)
    _atomic_write_text(_pdfplumber_patch_state_path(pdf_path, write_root),
                       json.dumps(state, indent=2))


def resolve_and_parse(part_number: str) -> dict:
    os.makedirs(DATASHEETS_DIR, exist_ok=True)
    os.makedirs(PARSED_DIR, exist_ok=True)
    # TODO-388: magic-pdf's -o and the pdfplumber writers both target this
    # run's bucket-B write root, which is the staging tier when staging is
    # active — created lazily here, same as PARSED_DIR above.
    os.makedirs(bucket_b_write_root(), exist_ok=True)
    # LT-23: decide L2 network-resolve availability once, at the first
    # resolver call of the run — eager so the disable line (if any) always
    # appears near STEP 03's start, regardless of which part first misses L1.
    _l2_resolve_ok()

    # Schematic MPN fields occasionally carry stray leading/trailing
    # whitespace (KiCad field-entry artifact). Left unstripped, it survives
    # BASE_SUFFIX_RE (which anchors on end-of-string) and breaks _find_pdf's
    # substring match against on-disk PDF filenames, so resolution misses
    # entirely even when the datasheet is present.
    part_number = part_number.strip()

    base_pn = BASE_SUFFIX_RE.sub("", part_number).strip()
    if len(base_pn) < MIN_MPN_LENGTH:
        raise FileNotFoundError(
            f"Part number '{part_number}' is too short to be a real MPN "
            f"(length {len(base_pn)} < {MIN_MPN_LENGTH}) — "
            f"likely a generic value. Add MPN field to schematic component."
        )

    # L1: local PDF cache (filename match).
    resolver_used = "l1_local"
    pdf_path = _find_pdf(part_number)
    if pdf_path is None:
        resolver_used = "none"
    # L2: vendor-lookup fallback — fetch the datasheet on an L1 miss.
    expansion: dict = {}
    if pdf_path is not None and _l2_import_ok():
        # Warm-path identity (card 262 / investigation/experiments/warm_path_recon,
        # option ii): an L1 hit means this MPN's PDF is already local, but if it
        # got there via L2's Case-1 placeholder expansion on an EARLIER run, the
        # placeholder's raw name never gets re-normalized to the concrete MPN on
        # this or any later run — resolved_mpn would silently regress to None
        # forever. Peek the L2 cache record for a previously-confirmed expansion
        # and re-stamp it here so :532's existing "resolved_mpn":
        # expansion.get("concrete") serves both the cold and warm paths from one
        # source of truth. Read-only (step_03_l2_ext.peek_cached_placeholder_
        # expansion only reads _load_entry, never writes) and no live API call.
        # Coordination: if card 261's naming fix changes _cache_path's key
        # scheme, this peek's key must change with it. Gated on _l2_import_ok
        # (LT-23), NOT credentials — this is a local-cache-only read.
        try:
            from steps import step_03_l2_ext
            cached_expansion = step_03_l2_ext.peek_cached_placeholder_expansion(part_number)
        except Exception as e:  # missing lib/creds or any L2 error → behave as if unset
            logger.warning(f"[STEP 03] Warm-path L2 cache peek unavailable for {part_number}: {e}")
            cached_expansion = None
        if cached_expansion:
            expansion.update(cached_expansion)
    if pdf_path is None:
        pdf_path = _l2_vendor_resolve(part_number, expansion_out=expansion)
        if pdf_path:
            resolver_used = "l2_vendor"
    if pdf_path is None:
        pdfs = os.listdir(DATASHEETS_DIR)
        raise FileNotFoundError(
            f"No datasheet found for '{part_number}'. "
            f"PDFs searched: {pdfs if pdfs else '(none in datasheets/)'}"
        )

    md_info: dict = {}
    markdown_text, mineru_used = _parse_pdf(pdf_path, path_out=md_info)

    pdf_stem = canonical_pdf_stem(pdf_path)
    # TODO-388 R-α: the path _parse_pdf ACTUALLY used, so the patched-markdown
    # write below lands on the file that was read rather than on a re-derived
    # path that may name a different tier. Falls back to this run's write-root
    # derivation when _parse_pdf produced no MinerU markdown (the pdfplumber
    # branch), which is what the unconditional derivation always returned.
    markdown_path = md_info.get("markdown_path") or str(
        mineru_markdown_path(pdf_path, bucket_b_write_root()))

    patched_pages = []
    if mineru_used:
        # Branch-B skip (TODO-227 Phase 2b): if a prior patch pass already
        # established that this many raw placeholders are unpatchable under
        # the current PDF/parse-config/guard-config, re-scanning the whole
        # PDF again would find nothing new — skip it. Any mismatch (source
        # changed, guard config changed, or the residual count itself
        # changed) falls through to a real (already-idempotent, Phase 2a)
        # pass, which then refreshes patch_state.
        raw_placeholder_count = len(list(PLACEHOLDER_RE.finditer(markdown_text)))
        skip_patch_scan = raw_placeholder_count > 0 and _patch_state_matches(
            pdf_path, raw_placeholder_count)
        if skip_patch_scan:
            logger.info(
                "[STEP 03] Skipping whole-PDF patch re-scan for %s — patch_state "
                "matches (residual_unpatchable=%d unchanged, nothing new to gain)",
                pdf_stem, raw_placeholder_count,
            )
        else:
            trunc_info = {}
            markdown_text, patched_pages = patch_image_placeholders(
                markdown_text, pdf_path, truncation_out=trunc_info)
            if patched_pages:
                with open(markdown_path, "w", encoding="utf-8") as f:
                    f.write(markdown_text)
                logger.warning(f"[STEP 03] Wrote patched markdown to {markdown_path}")
            residual = len(list(PLACEHOLDER_RE.finditer(markdown_text)))
            _save_patch_state(pdf_path, residual_unpatchable=residual,
                               patched_pages_total=len(patched_pages),
                               truncated=trunc_info.get("truncated", False))

    return {
        "part_number": part_number,
        "pdf_path": pdf_path,
        "markdown_path": markdown_path,
        "markdown_text": markdown_text,
        "mineru_used": mineru_used,
        "placeholder_patches": patched_pages,
        "resolver": resolver_used,
        # None unless L2 resolved this MPN via Case-1 placeholder expansion
        # (kb_identity_gap/REPORT.md) — additive, every other caller of this
        # dict is unaffected.
        "resolved_mpn": expansion.get("concrete"),
    }

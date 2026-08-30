"""Report-provenance verification (stale-report guard).

Mirrors the cache-provenance precedent (`step_03_resolver.check_cache_provenance`) but for
per-netlist REPORTS: a report stamps the `source_hash` of each cache it consumed
(`build_report` → `report["provenance"]["cache_source_hashes"]`), and this module verifies a
report against the CURRENT caches on read — so a report produced against a since-changed cache
(the RP2040 Phase-B bite) is detectable rather than silently trusted.

Verification compares STORED source_hashes (the report's stamp vs the current cache file's
`provenance.source_hash`) — it does NOT recompute from the PDF, so it needs neither
`pdfplumber`/`ollama` (which `step_03_resolver` pulls in) nor the parse-config constants.
It imports only json/pathlib plus the lightweight, pure-python `steps.checker_registry`
and `cache_paths` (from which `CHECKER_SOURCE_FILES` and the pin_groups cache-path
formula are derived, respectively — neither pulls in `pdfplumber`/`ollama`). That keeps
it cheap to import into the recall harness.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cache_paths


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str | None:
    """sha256 of a file's bytes, or None if unreadable. Shared by export_net (harness)
    and build_report/check_report_provenance — one light place for content hashing."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


# ── code axis: verdict-affecting source set (Notion 3933272c-88f3-8181) ──────
# Modules whose CHANGE can move a verdict on a FIXED set of cached specs + a fixed netlist.
# Split in two: CORE_PIPELINE_FILES are the non-checker verdict-affecting modules (validator,
# power/voltage inference + confirm, verdict assembly, the net-voltage lever, the peripheral
# KB/roles/coherence, rail-map); the five report-producing checkers are DERIVED from the checker
# registry (steps/checker_registry.py). Deliberately EXCLUDED: orchestration (main.py /
# run_checks.py / pipeline.py — CLI/assembly churn would over-invalidate), and the M-gate
# (peripheral_detectability changes recall HIT/MISS classification — recomputed each harness run —
# but does NOT change the report).
#
# A new/removed checker updates this tuple by editing ONE registry entry — and the
# step_08e-omitted-from-provenance bug class (TODO-168) becomes structurally impossible
# (enforced by test_provenance_registry). A new NON-checker verdict module goes in
# CORE_PIPELINE_FILES below.
from steps import checker_registry as _reg

CORE_PIPELINE_FILES = (
    "steps/step_05_validator.py",
    "steps/step_06_power.py",
    "steps/step_07_confirm.py",
    "steps/step_10_report.py",
    "steps/passive_traversal.py",
    "steps/net_name_utils.py",
    "steps/peripheral_roles.py",
    "steps/peripheral_coherence.py",
    "steps/peripheral_kb.py",
    # Phase 3 (M14 capability misroute): step_08d now emits a VERDICT-MOVING
    # capability FAIL derived from these two modules (cross-net bus pairing +
    # consensus/capability evaluator). They are verdict-affecting but NOT checker-
    # registry entries (step_08d owns the emit, like M6/M12), so they belong here in
    # CORE, not in the registry-derived SOURCE_FILES. Adding them moves
    # checker_code_hash → an announced code-era break (full recall re-measure).
    "steps/peripheral_bus_pairing.py",
    "steps/peripheral_consensus.py",
    # TODO-93 Phase 2 (M15 UART wrong-destination): step_08d now also emits a
    # VERDICT-MOVING UART_CAPABILITY_MISMATCH FAIL decided by this module. Same shape
    # as the two above — verdict-affecting, but step_08d owns the emit, so it is not a
    # checker-registry entry and belongs here in CORE rather than in the registry-derived
    # SOURCE_FILES. Adding it moves checker_code_hash → an announced code-era break.
    "steps/uart_capability.py",
    "steps/rail_map.py",
)

# CHECKER_SOURCE_FILES = CORE + the registry-derived checker files (08/08b/08c/08d/08e).
# step_08e ENTERS the code axis here for the first time = the folded TODO-168 stopgap (a single
# era break, DECISION D2). From this commit forward every cached report reads stale on the code
# axis — BY DESIGN; the full recall re-run is Phase 5, not this task. (checker_code_hash sorts the
# tuple before hashing, so only the ADDED member — step_08e — moves the era, not the reordering.)
CHECKER_SOURCE_FILES = CORE_PIPELINE_FILES + _reg.SOURCE_FILES


def checker_code_hash(base_dir=None) -> str | None:
    """sha256 over the CONTENTS of the verdict-affecting source set — sorted rel-paths, each
    ``path\\0bytes\\0`` concatenated (deterministic). CONTENT-hash, NOT git_sha: git_sha
    over-invalidates (any commit) and lies on a dirty tree (the retired-mtime-guard reasoning).
    base_dir defaults to this module's dir (= the POC dir where steps/ lives). None if not one
    source file is readable — an unlocatable base degrades to 'no code axis', never a false hash."""
    base = Path(base_dir) if base_dir else Path(__file__).resolve().parent
    h = hashlib.sha256()
    any_read = False
    for rel in sorted(CHECKER_SOURCE_FILES):
        try:
            data = (base / rel).read_bytes()
        except OSError:
            continue
        any_read = True
        h.update(rel.encode("utf-8")); h.update(b"\0"); h.update(data); h.update(b"\0")
    return h.hexdigest() if any_read else None


def _cache_path_for_pdf(pdf_path: str, parsed_dir: str) -> Path:
    """Same layout as step_03_resolver.get_pin_groups_cache_path (pure path derivation,
    TODO-343: both now delegate to cache_paths.pin_groups_cache_path). Imports
    step_03_resolver.canonical_pdf_stem (TODO-321) LOCALLY rather than at module level:
    that module pulls in pdfplumber + llm.ollama_client, which this module's docstring
    deliberately avoids at import time (cheap import into the recall harness)."""
    from steps.step_03_resolver import canonical_pdf_stem
    stem = canonical_pdf_stem(pdf_path)
    return cache_paths.pin_groups_cache_path(parsed_dir, stem)


def current_cache_source_hash(pdf_path: str, parsed_dir: str) -> tuple[str | None, str]:
    """Returns (source_hash | None, status) for the CURRENT on-disk cache of this part.
    status ∈ 'ok' | 'missing' | 'unreadable' | 'no_hash'."""
    cp = _cache_path_for_pdf(pdf_path, parsed_dir)
    if not cp.exists():
        return None, "missing"
    try:
        d = json.loads(cp.read_text())
    except (OSError, json.JSONDecodeError):
        return None, "unreadable"
    h = (d.get("provenance") or {}).get("source_hash")
    return (h, "ok") if h else (None, "no_hash")


def check_report_provenance(report: dict, parsed_dir: str,
                            base_dir=None) -> tuple[str, list[dict]]:
    """Verify a report against current state on THREE axes:

    - **cache axis** (`provenance.cache_source_hashes`): each stamped part-hash vs the current
      datasheet cache's stored source_hash (catches a since-changed cache; 832d548).
    - **input axis** (`provenance.input_netlist_sha256`): the stamped netlist hash vs the current
      `source_netlist` file (catches a since-changed / stale-export input; fa2a6e3).
    - **code axis** (`provenance.checker_code_hash`): the stamped verdict-affecting source hash vs
      a current recompute (catches a checker-code change — else the recall harness reuses a report
      built by OLD code and a new detector never runs on its own baselined mutants → false 0 HITs).

    Combine rule: **'stale' if ANY present axis mismatches; 'current' only if ALL THREE axes are
    present AND match; otherwise 'legacy_unverified'.** The code axis is load-bearing: a pre-1.4
    report (no `checker_code_hash`) can NOT read 'current' even with matching cache+input — it
    routes to legacy_unverified so the harness re-runs it under the new code. (All post-1.4 reports
    stamp all three fields, so matching runs still read 'current' → idempotent.)
    "Can't confirm current" (cache/netlist/source missing or unreadable) counts as a mismatch.

    Returns (verdict, issues) where issues is a list of dicts for logging.
    """
    prov = report.get("provenance") or {}
    issues: list[dict] = []
    mismatch = False

    # ── cache axis ──────────────────────────────────────────────────────────
    stamped = prov.get("cache_source_hashes")
    cache_present = stamped is not None
    if cache_present:
        em = report.get("extraction_metadata") or {}
        for part, stamped_hash in stamped.items():
            if stamped_hash is None:
                continue  # unresolved / legacy — nothing to compare (vacuous match)
            pdf_path = (em.get(part) or {}).get("pdf_path")
            if not pdf_path:
                issues.append({"axis": "cache", "part": part, "reason": "no_pdf_path_in_report",
                               "stamped": stamped_hash, "current": None})
                mismatch = True
                continue
            cur, status = current_cache_source_hash(pdf_path, parsed_dir)
            if status != "ok":
                issues.append({"axis": "cache", "part": part, "reason": f"cache_{status}",
                               "stamped": stamped_hash, "current": None})
                mismatch = True
            elif cur != stamped_hash:
                issues.append({"axis": "cache", "part": part, "reason": "hash_mismatch",
                               "stamped": stamped_hash, "current": cur})
                mismatch = True

    # ── input axis (netlist identity) ───────────────────────────────────────
    stamped_input = prov.get("input_netlist_sha256")
    input_present = stamped_input is not None
    if input_present:
        cur_input = sha256_file(report.get("source_netlist")) if report.get("source_netlist") else None
        if cur_input is None:
            issues.append({"axis": "input", "reason": "netlist_missing_or_unreadable",
                           "stamped": stamped_input, "current": None})
            mismatch = True
        elif cur_input != stamped_input:
            issues.append({"axis": "input", "reason": "input_hash_mismatch",
                           "stamped": stamped_input, "current": cur_input})
            mismatch = True

    # ── code axis (verdict-affecting source identity) ────────────────────────
    stamped_code = prov.get("checker_code_hash")
    code_present = stamped_code is not None
    if code_present:
        cur_code = checker_code_hash(base_dir)
        if cur_code is None:
            issues.append({"axis": "code", "reason": "checker_source_unreadable",
                           "stamped": stamped_code, "current": None})
            mismatch = True
        elif cur_code != stamped_code:
            issues.append({"axis": "code", "reason": "checker_code_hash_mismatch",
                           "stamped": stamped_code, "current": cur_code})
            mismatch = True

    if mismatch:
        return "stale", issues
    if cache_present and input_present and code_present:
        return "current", issues
    return "legacy_unverified", issues

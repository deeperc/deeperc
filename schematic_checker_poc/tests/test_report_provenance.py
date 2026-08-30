"""Stale-report guard: check_report_provenance + the recall-harness run_pipeline decision.

All fixtures are built in tmp dirs — NEVER reads or mutates real corpus reports or caches.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))  # repo root: run_recall_harness

from provenance import check_report_provenance, checker_code_hash, sha256_file  # noqa: E402

_LIVE_CODE = checker_code_hash()  # current verdict-affecting source hash (real POC dir)


def _add_full_axes(rep, tmp_path, netlist_text="(export (version E))"):
    """Add matching input + code axes so a report reads 'current' under the 3-axis rule
    (all three present + match). Writes a real netlist file for the input axis."""
    net = tmp_path / "board.net"
    net.write_text(netlist_text)
    rep["source_netlist"] = str(net)
    rep["provenance"]["input_netlist_sha256"] = sha256_file(net)
    rep["provenance"]["checker_code_hash"] = _LIVE_CODE
    return rep


def _make_cache(parsed_dir, stem, source_hash):
    p = parsed_dir / stem / "auto" / f"{stem}_pin_groups.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    prov = {"source_hash": source_hash} if source_hash is not None else {}
    p.write_text(json.dumps({"pin_groups": [], "provenance": prov}))
    return p


def _report(cache_source_hashes, parts_pdf):
    return {
        "provenance": {"git_sha": "abc123", "git_dirty": False,
                       "cache_source_hashes": cache_source_hashes},
        "extraction_metadata": {p: {"pdf_path": pdf} for p, pdf in parts_pdf.items()},
    }


def test_current_all_match(tmp_path):
    _make_cache(tmp_path, "foo", "hashA")
    rep = _add_full_axes(_report({"FOO": "hashA"}, {"FOO": "/ds/FOO.pdf"}), tmp_path)
    verdict, issues = check_report_provenance(rep, str(tmp_path))
    assert verdict == "current" and issues == []


def test_cache_only_no_code_axis_is_legacy(tmp_path):
    # cache matches but NO input/code axis (pre-1.4 report) → must NOT read current
    _make_cache(tmp_path, "foo", "hashA")
    rep = _report({"FOO": "hashA"}, {"FOO": "/ds/FOO.pdf"})
    verdict, _ = check_report_provenance(rep, str(tmp_path))
    assert verdict == "legacy_unverified"


def test_code_axis_absent_never_current(tmp_path):
    # THE load-bearing rule: cache+input match but code axis ABSENT → legacy_unverified, not current
    _make_cache(tmp_path, "foo", "hashA")
    rep = _report({"FOO": "hashA"}, {"FOO": "/ds/FOO.pdf"})
    net = tmp_path / "b.net"; net.write_text("x")
    rep["source_netlist"] = str(net)
    rep["provenance"]["input_netlist_sha256"] = sha256_file(net)
    # deliberately NO checker_code_hash
    verdict, _ = check_report_provenance(rep, str(tmp_path))
    assert verdict == "legacy_unverified"


def test_code_axis_mismatch_is_stale(tmp_path):
    _make_cache(tmp_path, "foo", "hashA")
    rep = _add_full_axes(_report({"FOO": "hashA"}, {"FOO": "/ds/FOO.pdf"}), tmp_path)
    rep["provenance"]["checker_code_hash"] = "STALE_CODE_HASH"   # a checker changed since
    verdict, issues = check_report_provenance(rep, str(tmp_path))
    assert verdict == "stale" and any(i["axis"] == "code" for i in issues)


def test_code_axis_present_matching_contributes_current(tmp_path):
    # code axis alone can't be current (needs all three) — but its presence+match is required
    _make_cache(tmp_path, "foo", "hashA")
    rep = _add_full_axes(_report({"FOO": "hashA"}, {"FOO": "/ds/FOO.pdf"}), tmp_path)
    assert check_report_provenance(rep, str(tmp_path))[0] == "current"


def test_stale_hash_mismatch(tmp_path):
    _make_cache(tmp_path, "foo", "hashB")           # cache changed since the report
    rep = _report({"FOO": "hashA"}, {"FOO": "/ds/FOO.pdf"})
    verdict, issues = check_report_provenance(rep, str(tmp_path))
    assert verdict == "stale"
    assert issues[0]["part"] == "FOO" and issues[0]["reason"] == "hash_mismatch"


def test_stale_cache_missing(tmp_path):
    # no cache file for FOO at all → can't confirm current → stale (not trusted)
    rep = _report({"FOO": "hashA"}, {"FOO": "/ds/FOO.pdf"})
    verdict, issues = check_report_provenance(rep, str(tmp_path))
    assert verdict == "stale" and issues[0]["reason"] == "cache_missing"


def test_stale_cache_hashless(tmp_path):
    _make_cache(tmp_path, "foo", None)              # legacy cache, no source_hash
    rep = _report({"FOO": "hashA"}, {"FOO": "/ds/FOO.pdf"})
    verdict, _ = check_report_provenance(rep, str(tmp_path))
    assert verdict == "stale"


def test_legacy_no_stamp(tmp_path):
    rep = {"extraction_metadata": {}}               # pre-poc-1.2 report, no provenance
    verdict, issues = check_report_provenance(rep, str(tmp_path))
    assert verdict == "legacy_unverified" and issues == []


def test_legacy_all_null(tmp_path):
    # every part stamped null (all unresolved / legacy caches) → nothing verifiable
    rep = _report({"FOO": None, "BAR": None}, {"FOO": "/ds/FOO.pdf", "BAR": "/ds/BAR.pdf"})
    verdict, _ = check_report_provenance(rep, str(tmp_path))
    assert verdict == "legacy_unverified"


def test_case_upper_pdf_path_freshness_uses_canonical_dir(tmp_path):
    """TODO-321: a report's stamped extraction_metadata pdf_path may carry a
    different letter case than whatever case the cache dir was written under
    (case-preserving PDF store; the cache-path deriver now canonicalizes via
    canonical_pdf_stem — recon section 0.2/2). The cache axis must resolve
    through that canonicalized (lower-cased) key, not the report's literal-case
    pdf_path — else a since-canonicalized cache directory reads as missing
    (false 'stale') even though it is current. Pre-fix, this scenario would
    have looked up 'FOOBAR' (Path(pdf_path).stem, uncanonicalized) and missed
    the actual 'foobar' cache entirely → cache_missing → stale."""
    _make_cache(tmp_path, "foobar", "hashA")  # canonical (lower-cased) cache dir
    rep = _add_full_axes(_report({"FOO": "hashA"}, {"FOO": "/ds/FOOBAR.pdf"}), tmp_path)
    verdict, issues = check_report_provenance(rep, str(tmp_path))
    assert verdict == "current" and issues == []


def test_mixed_null_and_match_is_current(tmp_path):
    # one null (skipped), one matching → the verifiable one matches → current (with full axes)
    _make_cache(tmp_path, "foo", "hashA")
    rep = _add_full_axes(_report({"FOO": "hashA", "BAR": None},
                                 {"FOO": "/ds/FOO.pdf", "BAR": "/ds/BAR.pdf"}), tmp_path)
    verdict, _ = check_report_provenance(rep, str(tmp_path))
    assert verdict == "current"


# ── harness integration: run_pipeline reuses only when current, else re-runs ──
def test_harness_run_pipeline_rejects_stale(tmp_path, monkeypatch):
    rrh = pytest.importorskip("run_recall_harness")  # heavy import; skip if env can't
    # point the guard at a tmp caches dir where FOO's current hash != the report's stamp
    _make_cache(tmp_path, "foo", "CURRENT_HASH")
    monkeypatch.setattr(rrh, "_PARSED_DIR_ABS", str(tmp_path))
    rrh._REPORT_FRESHNESS.update(reused_fresh=0, generated_new=0, rerun_stale=[])

    report_path = tmp_path / "rep.json"
    report_path.write_text(json.dumps(_report({"FOO": "OLD_HASH"}, {"FOO": "/ds/FOO.pdf"})
                                      | {"marker": "STALE"}))

    def fake_run_one(**kw):  # "re-run" writes a fresh report
        report_path.write_text(json.dumps(_report({"FOO": "CURRENT_HASH"}, {"FOO": "/ds/FOO.pdf"})
                                          | {"marker": "FRESH"}))
    monkeypatch.setattr(rrh.rct, "run_one", fake_run_one)

    out = rrh.run_pipeline(tmp_path / "x.net", report_path)
    assert out.get("marker") == "FRESH"          # did NOT return the stale content
    assert len(rrh._REPORT_FRESHNESS["rerun_stale"]) == 1


def test_harness_run_pipeline_reuses_fresh(tmp_path, monkeypatch):
    rrh = pytest.importorskip("run_recall_harness")
    _make_cache(tmp_path, "foo", "HASH1")
    monkeypatch.setattr(rrh, "_PARSED_DIR_ABS", str(tmp_path))
    rrh._REPORT_FRESHNESS.update(reused_fresh=0, generated_new=0, rerun_stale=[])
    net = tmp_path / "in.net"; net.write_text("(export)")
    rep = _report({"FOO": "HASH1"}, {"FOO": "/ds/FOO.pdf"}) | {"marker": "REUSED",
                                                               "source_netlist": str(net)}
    rep["provenance"]["input_netlist_sha256"] = sha256_file(net)   # full 3-axis → current → reused
    rep["provenance"]["checker_code_hash"] = _LIVE_CODE
    report_path = tmp_path / "rep.json"
    report_path.write_text(json.dumps(rep))

    def fail_run_one(**kw):
        raise AssertionError("run_one must NOT be called for a fresh report")
    monkeypatch.setattr(rrh.rct, "run_one", fail_run_one)

    out = rrh.run_pipeline(tmp_path / "x.net", report_path)
    assert out.get("marker") == "REUSED"
    assert rrh._REPORT_FRESHNESS["reused_fresh"] == 1


# ── Part D: check_report_provenance input axis ───────────────────────────────
def _report_input(input_sha, source_netlist, cache_hashes=None, parts_pdf=None, add_code=True):
    prov = {"git_sha": "abc", "git_dirty": False, "input_netlist_sha256": input_sha}
    # cache axis present (empty = vacuous match) + code axis present, so a matching input reads
    # 'current' under the 3-axis rule (all three present). Tests that need a real cache pass it.
    prov["cache_source_hashes"] = cache_hashes if cache_hashes is not None else {}
    if add_code:
        prov["checker_code_hash"] = _LIVE_CODE
    rep = {"provenance": prov, "source_netlist": str(source_netlist)}
    if parts_pdf:
        rep["extraction_metadata"] = {p: {"pdf_path": pdf} for p, pdf in parts_pdf.items()}
    return rep


def test_input_axis_match_is_current(tmp_path):
    from provenance import sha256_file
    net = tmp_path / "board.net"; net.write_text("(export foo)")
    rep = _report_input(sha256_file(net), net)
    assert check_report_provenance(rep, str(tmp_path))[0] == "current"


def test_input_axis_mismatch_is_stale(tmp_path):
    from provenance import sha256_file
    net = tmp_path / "board.net"; net.write_text("(export foo)")
    stamped = sha256_file(net)
    net.write_text("(export BAR)")                     # input changed since the report
    verdict, issues = check_report_provenance(_report_input(stamped, net), str(tmp_path))
    assert verdict == "stale" and issues[0]["axis"] == "input"


def test_input_axis_missing_file_is_stale(tmp_path):
    rep = _report_input("deadbeef", tmp_path / "gone.net")
    assert check_report_provenance(rep, str(tmp_path))[0] == "stale"


def test_input_axis_absent_is_legacy_under_three_axis_rule(tmp_path):
    # cache matches but input + code axes absent → NOT current (3-axis rule: all three required)
    _make_cache(tmp_path, "foo", "hashA")
    rep = _report({"FOO": "hashA"}, {"FOO": "/ds/FOO.pdf"})
    assert check_report_provenance(rep, str(tmp_path))[0] == "legacy_unverified"


def test_combine_cache_current_input_stale_is_stale(tmp_path):
    from provenance import sha256_file
    _make_cache(tmp_path, "foo", "hashA")              # cache axis current
    net = tmp_path / "b.net"; net.write_text("v1"); stamped = sha256_file(net)
    net.write_text("v2")                                # input axis stale
    rep = _report_input(stamped, net, cache_hashes={"FOO": "hashA"},
                        parts_pdf={"FOO": "/ds/FOO.pdf"})
    assert check_report_provenance(rep, str(tmp_path))[0] == "stale"


# ── Part A: export_net stamp-and-verify (use .net src → no kicad-cli) ────────
def _reset_export(rrh):
    rrh._EXPORT_FRESHNESS.update(exported=0, reused_fresh=0, reexported_stale=0)


def test_export_net_stamp_and_verify(tmp_path):
    rrh = pytest.importorskip("run_recall_harness")
    _reset_export(rrh)
    src = tmp_path / "m.net"; src.write_text("MUT_V1")   # .net src → shutil.copy path
    dest = tmp_path / "exp" / "m.net"
    srch = tmp_path / "exp" / "m.net.srchash"

    ok, reason = rrh.export_net(src, dest)               # fresh export
    assert ok and reason == "exported" and srch.exists()
    assert rrh._EXPORT_FRESHNESS["exported"] == 1

    ok, reason = rrh.export_net(src, dest)               # unchanged → cached
    assert ok and reason == "cached" and rrh._EXPORT_FRESHNESS["reused_fresh"] == 1

    src.write_text("MUT_V2")                             # content changed under same name
    ok, reason = rrh.export_net(src, dest)               # → re-exported stale
    assert ok and reason == "reexported_stale"
    assert rrh._EXPORT_FRESHNESS["reexported_stale"] == 1
    assert dest.read_text() == "MUT_V2"                  # fresh content
    from provenance import sha256_file
    assert srch.read_text().strip() == sha256_file(src)  # sidecar updated


def test_export_net_legacy_no_sidecar_reexports(tmp_path):
    rrh = pytest.importorskip("run_recall_harness")
    _reset_export(rrh)
    src = tmp_path / "m.net"; src.write_text("X")
    dest = tmp_path / "exp" / "m.net"; dest.parent.mkdir(parents=True)
    dest.write_text("STALE_LEGACY")                      # export exists, NO .srchash
    ok, reason = rrh.export_net(src, dest)
    assert ok and reason == "reexported_stale" and dest.read_text() == "X"


def test_compose_export_change_makes_report_stale(tmp_path):
    # end-to-end: export a mutant, "report" stamps the exported .net hash; then the mutant
    # changes → export re-exports AND the old report reads stale (input axis).
    rrh = pytest.importorskip("run_recall_harness")
    from provenance import sha256_file
    _reset_export(rrh)
    src = tmp_path / "m.net"; src.write_text("MUT_A")
    dest = tmp_path / "exp" / "m.net"
    rrh.export_net(src, dest)
    old_report = _report_input(sha256_file(dest), dest)  # report built on export v1
    assert check_report_provenance(old_report, str(tmp_path))[0] == "current"

    src.write_text("MUT_B")                              # mutant regenerated (same name)
    ok, reason = rrh.export_net(src, dest)
    assert reason == "reexported_stale"                  # (A) re-exported
    assert check_report_provenance(old_report, str(tmp_path))[0] == "stale"  # (D) report now stale

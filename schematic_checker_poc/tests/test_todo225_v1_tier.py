"""TODO-225 — V1 normalised cache-directory tier (steps/part_bindings.py +
step_03_resolver._scan_cache_root tier 4).

Every test isolates both cache roots into tmp_path; the real trees are never
read or written.
"""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
POC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

import pipeline  # noqa: E402
from steps import part_bindings  # noqa: E402
from steps import step_03_resolver as r3  # noqa: E402


@pytest.fixture
def roots(tmp_path, monkeypatch):
    canonical = tmp_path / "parsed"
    staging = tmp_path / "staged"
    datasheets = tmp_path / "datasheets"
    for d in (canonical, staging, datasheets):
        d.mkdir()
    monkeypatch.setattr(r3, "PARSED_DIR", str(canonical))
    monkeypatch.setattr(r3, "STAGING_DIR", str(staging))
    monkeypatch.setattr(r3, "STAGING_ENABLED", False)
    monkeypatch.setattr(r3, "DATASHEETS_DIR", str(datasheets))
    monkeypatch.setattr(r3, "_manifest_state",
                        {"dir": None, "data": None, "notice_logged": False})
    return {"canonical": canonical, "staging": staging, "datasheets": datasheets}


def _mkdirs(root, *names):
    for n in names:
        (root / n).mkdir(parents=True, exist_ok=True)


def _name(hit):
    return os.path.basename(str(hit)) if hit else None


def _write_cache(parsed_dir, stem):
    d = parsed_dir / stem / "auto"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stem}_pin_groups.json").write_text(json.dumps({
        "pin_groups": [{"group_name": "Supply (VCC)", "pin_type": "power",
                        "example_pins": ["VCC"]}],
        "default_group": "Supply (VCC)",
        "extraction_meta": {"extractor": "claude-code", "source": "foo.md",
                            "schema_version": 1,
                            "r1_correction": {"date": "2026-08-14"}},
        "provenance": {"source_hash": "deadbeef", "parse_config": {}, "pdf_size": 1},
    }, indent=2))


# (1) clean miss + one V1 dir -> hit ------------------------------------------

@pytest.mark.parametrize("part, dirs, want", [
    ("BAT54A(SOT23-3)", ["bat54a", "bat54c"], "bat54a"),
    ("R7FA4M1AB3CFM#AA0", ["r7fa4m1ab3cfm_aa0"], "r7fa4m1ab3cfm_aa0"),
    ("LAN8710A-EZC(QFN32)", ["lan8710a-ezc"], "lan8710a-ezc"),
    # BASE_SUFFIX_RE strips nothing from a string ending in ")", "(qfn32)"
    # probes match no dir, and N("lan8710a-ezc-abc") equals neither V1 key.
    ("LAN8710A-EZC(QFN32)", ["lan8710a-ezc", "lan8710a-ezc-abc"], "lan8710a-ezc"),
])
def test_v1_hit_on_clean_miss(roots, part, dirs, want):
    _mkdirs(roots["canonical"], *dirs)
    assert _name(r3.find_cached_dir_for_part(part)) == want


def test_paren_free_two_dir_case_is_tier12_ambiguous(roots):
    _mkdirs(roots["canonical"], "lan8710a-ezc", "lan8710a-ezc-abc")
    assert r3.find_cached_dir_for_part("LAN8710A-EZC") is None
    assert r3._scan_cache_root(roots["canonical"], "LAN8710A-EZC") == (None, "ambiguous")


# (2) tiers 1-3 ambiguous is terminal; V1 not consulted -----------------------

@pytest.mark.parametrize("part, dirs", [
    ("BAT54", ["bat54", "bat54a", "bat54c"]),
    ("TL072", ["tl072", "tl072cd"]),
])
def test_tier123_ambiguous_is_terminal_and_skips_v1(roots, monkeypatch, part, dirs):
    _mkdirs(roots["canonical"], *dirs)
    calls = []
    real = part_bindings.v1_match
    monkeypatch.setattr(part_bindings, "v1_match",
                        lambda *a, **k: calls.append(a) or real(*a, **k))
    assert r3.find_cached_dir_for_part(part) is None
    assert calls == []


# (3) >1 V1 match -> ambiguous ------------------------------------------------

def test_multiple_v1_matches_are_ambiguous(roots, monkeypatch):
    _mkdirs(roots["canonical"], "abcd-1", "abcd_1")
    assert r3._scan_cache_root(roots["canonical"], "ABCD.1") == (None, "ambiguous")
    assert r3.find_cached_dir_for_part("ABCD.1") is None
    # staging on, matching staged dir: still None (ambiguity never rescued)
    _mkdirs(roots["staging"], "abcd.1")
    monkeypatch.setattr(r3, "STAGING_ENABLED", True)
    assert r3.find_cached_dir_for_part("ABCD.1") is None


# (4) staging order -----------------------------------------------------------

def test_canonical_v1_hit_wins_over_staged_v1_hit(roots, monkeypatch):
    _mkdirs(roots["canonical"], "bat54a")
    _mkdirs(roots["staging"], "bat54a")
    monkeypatch.setattr(r3, "STAGING_ENABLED", True)
    assert r3.find_cached_dir_for_part("BAT54A(SOT23-3)") == roots["canonical"] / "bat54a"


def test_staged_v1_hit_after_canonical_clean_miss(roots, monkeypatch):
    _mkdirs(roots["staging"], "bat54a")
    assert r3.find_cached_dir_for_part("BAT54A(SOT23-3)") is None
    monkeypatch.setattr(r3, "STAGING_ENABLED", True)
    assert r3.find_cached_dir_for_part("BAT54A(SOT23-3)") == roots["staging"] / "bat54a"


# (5) key length --------------------------------------------------------------

def test_short_keys_never_reach_a_dir(roots):
    _mkdirs(roots["canonical"], "a1")
    assert r3.find_cached_dir_for_part("A-1") is None
    assert r3._scan_cache_root(roots["canonical"], "A-1") == (None, "miss")


def test_v1_keys_drop_short_keep_long():
    assert part_bindings.v1_keys("A-1", 4) == set()
    # full-string key is long, paren-stripped key ("ab") is short
    assert part_bindings.v1_keys("AB(XYZ12)", 4) == {"abxyz12"}


# (6) PAREN strip -------------------------------------------------------------

def test_paren_strip_keys(roots):
    _mkdirs(roots["canonical"], "sm04b-srss-tb_lf_sn_", "sc0914_13_")
    assert _name(r3.find_cached_dir_for_part("SM04B-SRSS-TB(LF)(SN)")) == "sm04b-srss-tb_lf_sn_"
    assert _name(r3.find_cached_dir_for_part("SC0914(13)")) == "sc0914_13_"
    _mkdirs(roots["canonical"], "foo9999")
    assert _name(r3.find_cached_dir_for_part("FOO9999(SOT23)")) == "foo9999"


# (7) tiers 1-3 hit -> V1 not consulted ---------------------------------------

def test_tier12_hit_does_not_consult_v1(roots, monkeypatch):
    _mkdirs(roots["canonical"], "esp32-wroom-32e-n4")
    monkeypatch.setattr(part_bindings, "v1_match",
                        lambda *a, **k: pytest.fail("V1 consulted on a tier-1/2 hit"))
    assert _name(r3.find_cached_dir_for_part("ESP32-WROOM-32E-N4")) == "esp32-wroom-32e-n4"


# (8) warm shortcut end to end ------------------------------------------------

def test_warm_shortcut_serves_v1_hit_without_a_pdf(roots):
    _write_cache(roots["canonical"], "lan8710a-ezc")
    doc = pipeline._try_warm_cache_shortcut("LAN8710A-EZC(QFN32)", frozenset())
    assert doc is not None
    assert doc["pdf_stem"] == "lan8710a-ezc"
    assert doc["pdf_path"] is None
    assert doc["resolver"] == "cache_warm_no_pdf"


# (9)/(10) import direction and single definition -----------------------------

def test_part_bindings_does_not_import_resolver():
    code = ("import sys; sys.path.insert(0, %r); import steps.part_bindings; "
            "assert 'steps.step_03_resolver' not in sys.modules" % POC_DIR)
    r = subprocess.run([sys.executable, "-I", "-B", "-c", code],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_run_checks_paren_suffix_re_is_the_same_object():
    sys.path.insert(0, REPO_ROOT)
    import run_checks
    assert run_checks.PAREN_SUFFIX_RE is part_bindings.PAREN_SUFFIX_RE


# (11) pure functions ---------------------------------------------------------

def test_normalise():
    assert part_bindings.normalise("LAN8710A-EZC(QFN32)") == "lan8710aezcqfn32"
    assert part_bindings.normalise("R7FA#AA0_x y") == "r7faaa0xy"
    assert part_bindings.normalise("") == ""
    assert part_bindings.normalise("---") == ""


def test_v1_keys_both_forms_and_strip():
    assert part_bindings.v1_keys("  BAT54A(SOT23-3) ", 4) == {"bat54asot233", "bat54a"}
    assert part_bindings.v1_keys("", 4) == set()


def test_v1_match_order_independent_and_sorted():
    names = ["abcd_1", "abcd-1", "zzzz", "abcd1"]
    a = part_bindings.v1_match("ABCD.1", names, 4)
    b = part_bindings.v1_match("ABCD.1", list(reversed(names)), 4)
    assert a == b == ["abcd-1", "abcd1", "abcd_1"]


def test_v1_match_empty_names_and_no_keys():
    assert part_bindings.v1_match("BAT54A", [], 4) == []
    assert part_bindings.v1_match("A-1", ["a1"], 4) == []
    assert part_bindings.v1_match("BAT54A", ["", "---"], 4) == []

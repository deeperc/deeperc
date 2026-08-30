"""Unit tests for cache_paths.py (TODO-343): formula equivalence against the
pre-change, independently-implemented construction, plus the PARSED_DIR
runtime-patch regression this consolidation could realistically introduce.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import cache_paths  # noqa: E402
from steps import step_03_resolver as r3  # noqa: E402


def test_formula_string_equals_prechange_construction():
    parsed_dir = "/tmp/whatever_parsed_dir"
    stems = [
        "stm32f103c8t6",           # ordinary MPN-derived stem
        "74hc595",                 # leading digit
        "part.with.dots",          # dotted "name" portion (Path.stem only strips the LAST suffix)
        "ds28e17-p",                # hyphenated
    ]
    for stem in stems:
        pre_change = Path(parsed_dir) / stem / "auto" / f"{stem}_pin_groups.json"
        got = cache_paths.pin_groups_cache_path(parsed_dir, stem)
        assert got == pre_change
        assert str(got) == str(pre_change)


def test_accepts_str_or_path_parsed_dir():
    stem = "foo123"
    as_str = cache_paths.pin_groups_cache_path("/a/b/parsed", stem)
    as_path = cache_paths.pin_groups_cache_path(Path("/a/b/parsed"), stem)
    assert as_str == as_path


def test_get_pin_groups_cache_path_still_respects_runtime_patched_parsed_dir(
        monkeypatch, tmp_path):
    """The regression this consolidation could realistically introduce (pipeline.py:219
    patches step_03_resolver.PARSED_DIR at runtime) — get_pin_groups_cache_path must
    keep reading PARSED_DIR fresh at call time, not a value captured before delegation."""
    monkeypatch.setattr(r3, "PARSED_DIR", str(tmp_path))
    result = r3.get_pin_groups_cache_path("/somewhere/FOO123.pdf")
    assert result == tmp_path / "foo123" / "auto" / "foo123_pin_groups.json"

    # And a SECOND patch takes effect too — proves it's not cached/memoized anywhere.
    other = tmp_path / "other_parsed_dir"
    monkeypatch.setattr(r3, "PARSED_DIR", str(other))
    result2 = r3.get_pin_groups_cache_path("/somewhere/FOO123.pdf")
    assert result2 == other / "foo123" / "auto" / "foo123_pin_groups.json"

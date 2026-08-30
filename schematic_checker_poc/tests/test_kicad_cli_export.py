"""
Tests for schematic_checker_poc.preprocess.kicad_cli_export.

These tests touch the real kicad-cli binary and are skipped on hosts where
it isn't installed. Each test runs against a tmp_path so the corpus is
never mutated.
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import pytest

# Make ``preprocess`` importable without requiring an installed package layout;
# matches the convention used by test_parser.py and friends in this directory.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from preprocess.kicad_cli_export import kicad_cli_export  # noqa: E402


FIXTURE_SRC = Path.home() / (
    "schecker/netlist_corpus/rp2040/raspberrypipress-pico-kicad"
    "/eg/ch11/rp2040_game_con/con_button1.kicad_sch"
)

pytestmark = pytest.mark.skipif(
    shutil.which("kicad-cli") is None,
    reason="kicad-cli not installed on PATH",
)


def _copy_fixture(dest_dir: Path) -> Path:
    """Copy a known-good .kicad_sch into dest_dir and return the new path."""
    if not FIXTURE_SRC.exists():
        pytest.skip(f"fixture not present: {FIXTURE_SRC}")
    target = dest_dir / FIXTURE_SRC.name
    shutil.copy(FIXTURE_SRC, target)
    return target


def test_exports_missing_net(tmp_path):
    """A .kicad_sch with no sibling .net should be exported."""
    src = _copy_fixture(tmp_path)
    report = kicad_cli_export(tmp_path, report_dir=tmp_path / "reports")

    assert report["total_candidates"] == 1
    assert report["exported_new"] == 1
    assert report["skipped_exists"] == 0
    assert report["failed"] == 0
    assert report["timed_out"] == 0

    out = src.with_suffix(".net")
    assert out.exists(), "exporter should have written sibling .net"
    assert out.stat().st_size > 100, "exported .net should not be empty"
    assert "(export" in out.read_text()[:100]


def test_skips_when_net_exists(tmp_path):
    """A second run over the same dir should skip (force=False)."""
    src = _copy_fixture(tmp_path)
    kicad_cli_export(tmp_path, report_dir=tmp_path / "reports")
    out = src.with_suffix(".net")
    first_mtime = out.stat().st_mtime

    # Sleep just enough that any re-write would produce a distinct mtime.
    time.sleep(1.1)
    report = kicad_cli_export(tmp_path, report_dir=tmp_path / "reports")

    assert report["total_candidates"] == 1
    assert report["skipped_exists"] == 1
    assert report["exported_new"] == 0
    assert out.stat().st_mtime == first_mtime, "skipped file should be untouched"


def test_force_re_exports(tmp_path):
    """force=True should re-export even when the sibling .net exists."""
    src = _copy_fixture(tmp_path)
    kicad_cli_export(tmp_path, report_dir=tmp_path / "reports")
    out = src.with_suffix(".net")
    first_mtime = out.stat().st_mtime

    time.sleep(1.1)
    report = kicad_cli_export(tmp_path, force=True, report_dir=tmp_path / "reports")

    assert report["exported_new"] == 1
    assert report["skipped_exists"] == 0
    assert out.stat().st_mtime > first_mtime, "force=True should rewrite the file"


def test_empty_dir_returns_cleanly(tmp_path):
    """An empty corpus dir should report zero candidates and no failure."""
    report = kicad_cli_export(tmp_path, report_dir=tmp_path / "reports")
    assert report["total_candidates"] == 0
    assert report["exported_new"] == 0
    assert report["failed"] == 0
    assert report["per_file"] == []


def test_bogus_source_recorded_as_failed(tmp_path):
    """A malformed .kicad_sch should be captured as failed, not raised."""
    bogus = tmp_path / "bogus.kicad_sch"
    bogus.write_bytes(b"not a sch")  # 9 bytes of garbage
    report = kicad_cli_export(tmp_path, report_dir=tmp_path / "reports")

    assert report["total_candidates"] == 1
    assert report["failed"] == 1
    assert report["exported_new"] == 0
    # stderr_tail should be populated on failure entries
    failure_entries = [e for e in report["per_file"] if e["status"] == "failed"]
    assert len(failure_entries) == 1
    assert "stderr_tail" in failure_entries[0]

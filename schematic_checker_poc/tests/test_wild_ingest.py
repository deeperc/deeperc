"""
Tests for schematic_checker_poc.preprocess.wild_ingest (TODO-277 v1).

Hermetic: all classification tests use hand-written synthetic .kicad_pro/.kicad_sch
fixtures under tmp_path, no dependency on kicad-cli or any real cloned repo. The one
export-path test that does invoke real kicad-cli is skipped when it isn't installed
(matching the convention in test_kicad_cli_export.py); the gate-rejection test is
still hermetic — it feeds a hand-written empty .net file directly to the counter,
never invoking kicad-cli.
"""
from __future__ import annotations

import os
import shutil
import sys

import pytest

# Make ``preprocess`` importable without requiring an installed package layout;
# matches the convention used by test_kicad_cli_export.py in this directory.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from preprocess.wild_ingest import (  # noqa: E402
    classify_repo,
    export_repo,
    _count_components_and_nets,
    _passes_gate,
)


def _kicad_sch(sheetfile_refs=(), property_name="Sheetfile") -> str:
    """Minimal modern .kicad_sch content: a `(kicad_sch` header plus one `(sheet ...)`
    block per Sheetfile reference (repeats allowed, for reused-subsheet fixtures).
    `property_name` lets a fixture use the legacy two-word "Sheet file" spelling
    (TODO-289) instead of the modern one-word "Sheetfile" spelling."""
    lines = ['(kicad_sch (version 20231120) (generator "eeschema")']
    for ref in sheetfile_refs:
        lines.append('  (sheet (at 0 0) (size 10 10)')
        lines.append('    (property "%s" "%s" (at 0 0 0) (effects (font (size 1 1))))' % (property_name, ref))
        lines.append('  )')
    lines.append(')')
    return "\n".join(lines)


_LEGACY_EESCHEMA = 'EESchema Schematic File Version 4\nLIBS:power\n$EndSCHEMATC\n'
_EAGLE_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<!DOCTYPE eagle SYSTEM "eagle.dtd">\n'
    '<eagle version="6.5.0">\n<drawing></drawing>\n</eagle>\n'
)


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_single_flat_project_is_clear(tmp_path):
    repo = tmp_path / "flat_repo"
    _write(repo / "flat_repo.kicad_pro", "{}")
    _write(repo / "flat_repo.kicad_sch", _kicad_sch(["power.kicad_sch", "mcu.kicad_sch"]))
    _write(repo / "power.kicad_sch", _kicad_sch())
    _write(repo / "mcu.kicad_sch", _kicad_sch())

    result = classify_repo(repo)
    assert result.status == "CLEAR"
    assert result.root.name == "flat_repo.kicad_sch"


def test_legacy_two_word_sheet_file_property_is_clear(tmp_path):
    """TODO-289: older KiCad versions (e.g. antmicro/snapdragon-845-baseboard) write
    the sheet-reference property as two words, `"Sheet file"`, not the modern
    one-word `"Sheetfile"`. _SHEETFILE_RE must match both spellings so a genuinely
    single-root repo using the legacy spelling still resolves CLEAR instead of
    hard-stopping AMBIGUOUS on a false "N roots" reading."""
    repo = tmp_path / "legacy_prop_repo"
    _write(repo / "legacy_prop_repo.kicad_pro", "{}")
    _write(repo / "legacy_prop_repo.kicad_sch",
           _kicad_sch(["power.kicad_sch", "mcu.kicad_sch"], property_name="Sheet file"))
    _write(repo / "power.kicad_sch", _kicad_sch())
    _write(repo / "mcu.kicad_sch", _kicad_sch())

    result = classify_repo(repo)
    assert result.status == "CLEAR"
    assert result.root.name == "legacy_prop_repo.kicad_sch"


def test_deep_hierarchy_with_reused_subsheets_acruxcz_shape(tmp_path):
    """root -> childA (3 separate placements) + childB (1 placement). 3 unique
    FILES but 4 Sheetfile property INSTANCES — asserts we resolve on file identity,
    never on a naive len(sheets)/reference-count basis (the acruxcz gotcha:
    .kicad_pro's own `sheets` array declared 27 instances for only 20 files)."""
    repo = tmp_path / "acruxcz_shape"
    _write(repo / "acruxcz_shape.kicad_pro", "{}")
    _write(repo / "acruxcz_shape.kicad_sch",
           _kicad_sch(["childA.kicad_sch", "childA.kicad_sch", "childA.kicad_sch", "childB.kicad_sch"]))
    _write(repo / "childA.kicad_sch", _kicad_sch())
    _write(repo / "childB.kicad_sch", _kicad_sch())

    result = classify_repo(repo)
    assert result.status == "CLEAR"
    assert result.root.name == "acruxcz_shape.kicad_sch"
    # 3 unique files total, not 4 (the raw Sheetfile-instance count) and not 5.
    all_sch = sorted(p.name for p in repo.glob("*.kicad_sch"))
    assert all_sch == ["acruxcz_shape.kicad_sch", "childA.kicad_sch", "childB.kicad_sch"]


def test_stem_mismatch_is_ambiguous(tmp_path):
    repo = tmp_path / "mismatched"
    _write(repo / "mismatched.kicad_pro", "{}")
    # No .kicad_sch whose stem equals the .kicad_pro stem "mismatched".
    _write(repo / "top_level.kicad_sch", _kicad_sch(["child.kicad_sch"]))
    _write(repo / "child.kicad_sch", _kicad_sch())

    result = classify_repo(repo)
    assert result.status == "AMBIGUOUS"
    assert "stem-match" in result.reason


def test_topology_disagreement_is_ambiguous(tmp_path):
    """Filename-match picks the .kicad_pro-stem file, but topology finds a
    DIFFERENT file with zero incoming references — a genuine disagreement."""
    repo = tmp_path / "disagree"
    _write(repo / "disagree.kicad_pro", "{}")
    # "disagree.kicad_sch" matches the .kicad_pro stem but is itself referenced by
    # "actual_root.kicad_sch", which has zero incoming references -> topology root.
    _write(repo / "actual_root.kicad_sch", _kicad_sch(["disagree.kicad_sch"]))
    _write(repo / "disagree.kicad_sch", _kicad_sch())

    result = classify_repo(repo)
    assert result.status == "AMBIGUOUS"
    assert "disagrees" in result.reason


def test_multi_project_hard_stops_no_auto_pick(tmp_path):
    """Even when one candidate would trivially win on a naming signal (main-token
    directory, dumpling-shaped), v1 must NOT auto-pick — always MULTI_PROJECT."""
    repo = tmp_path / "multi"
    _write(repo / "01_main-board" / "Main.kicad_pro", "{}")
    _write(repo / "01_main-board" / "Main.kicad_sch", _kicad_sch())
    _write(repo / "02_sensor" / "Sensor.kicad_pro", "{}")
    _write(repo / "02_sensor" / "Sensor.kicad_sch", _kicad_sch())

    result = classify_repo(repo)
    assert result.status == "MULTI_PROJECT"
    assert result.root is None
    assert len(result.candidates) == 2
    stems = {c["stem"] for c in result.candidates}
    assert stems == {"Main", "Sensor"}


def test_legacy_eeschema_header_skip_and_tally(tmp_path):
    repo = tmp_path / "legacy_repo"
    _write(repo / "old_board.sch", _LEGACY_EESCHEMA)

    result = classify_repo(repo)
    assert result.status == "LEGACY_FORMAT"
    assert result.root is None
    assert result.tally.get("legacy_eeschema") == 1


def test_eagle_xml_sch_is_distinct_class(tmp_path):
    repo = tmp_path / "eagle_repo"
    _write(repo / "old_board.sch", _EAGLE_XML)

    result = classify_repo(repo)
    assert result.status == "EAGLE_FORMAT"
    assert result.root is None
    assert result.tally.get("eagle_xml") == 1
    # Distinct from the legacy_eeschema class.
    assert result.tally.get("legacy_eeschema", 0) == 0


def test_no_kicad_project_when_nothing_found(tmp_path):
    repo = tmp_path / "empty_repo"
    _write(repo / "README.md", "nothing here")

    result = classify_repo(repo)
    assert result.status == "NO_KICAD_PROJECT"


def test_gate_rejects_empty_netlist():
    """Hermetic — never invokes kicad-cli. comp>0/net>0 gate on hand-written .net
    content mirroring the real kicadsexpr export shape."""
    assert _passes_gate(0, 0) is False
    assert _passes_gate(5, 0) is False
    assert _passes_gate(0, 5) is False
    assert _passes_gate(5, 5) is True


def test_count_components_and_nets_on_empty_export(tmp_path):
    net_path = tmp_path / "empty.net"
    net_path.write_text(
        "(export\n  (version \"E\")\n  (components\n  )\n  (nets\n  )\n)\n",
        encoding="utf-8",
    )
    comp_count, net_count = _count_components_and_nets(net_path)
    assert (comp_count, net_count) == (0, 0)


def test_count_components_and_nets_does_not_overcount_ref_in_nodes(tmp_path):
    """A net's per-node (ref "...") entries must not be miscounted as (comp
    blocks — this is exactly the substring trap a naive counter would hit."""
    net_path = tmp_path / "populated.net"
    net_path.write_text(
        "(export\n"
        "  (components\n"
        "    (comp (ref \"C1\"))\n"
        "  )\n"
        "  (nets\n"
        "    (net (code \"1\") (name \"VCC\")\n"
        "      (node (ref \"C1\") (pin \"1\"))\n"
        "    )\n"
        "  )\n"
        ")\n",
        encoding="utf-8",
    )
    comp_count, net_count = _count_components_and_nets(net_path)
    assert (comp_count, net_count) == (1, 1)


def test_export_repo_empty_export_gate_hermetic(tmp_path, monkeypatch):
    """Exercises export_repo()'s CLEAR -> gate path without invoking real kicad-cli:
    monkeypatch _run_one to simulate an exit-0-but-empty export."""
    import preprocess.wild_ingest as wi

    repo = tmp_path / "clear_repo"
    _write(repo / "clear_repo.kicad_pro", "{}")
    _write(repo / "clear_repo.kicad_sch", _kicad_sch())

    def fake_run_one(src, out):
        out.write_text("(export\n  (components\n  )\n  (nets\n  )\n)\n", encoding="utf-8")
        return "exported", 0.01, ""

    monkeypatch.setattr(wi, "_run_one", fake_run_one)
    result = export_repo(repo, tmp_path / "out")
    assert result["status"] == "EMPTY_EXPORT"
    assert result["comp_count"] == 0
    assert result["net_count"] == 0


def test_export_repo_multi_project_never_calls_kicad_cli(tmp_path, monkeypatch):
    import preprocess.wild_ingest as wi

    repo = tmp_path / "multi2"
    _write(repo / "A" / "A.kicad_pro", "{}")
    _write(repo / "A" / "A.kicad_sch", _kicad_sch())
    _write(repo / "B" / "B.kicad_pro", "{}")
    _write(repo / "B" / "B.kicad_sch", _kicad_sch())

    def fail_run_one(src, out):
        raise AssertionError("kicad-cli must never be invoked for a MULTI_PROJECT hard-stop")

    monkeypatch.setattr(wi, "_run_one", fail_run_one)
    result = export_repo(repo, tmp_path / "out2")
    assert result["status"] == "MULTI_PROJECT"


pytestmark_kicad_cli = pytest.mark.skipif(
    shutil.which("kicad-cli") is None,
    reason="kicad-cli not installed on PATH",
)


@pytestmark_kicad_cli
def test_export_repo_real_kicad_cli_clear_project(tmp_path):
    """Non-hermetic sanity check that the full CLEAR -> kicad-cli -> gate pipeline
    wires together against a real minimal schematic. Skipped when kicad-cli is
    absent; the hermetic tests above already cover the gate logic in isolation."""
    from pathlib import Path
    fixture_src = None
    for candidate in (
        str(Path.home() / (
            "schecker/netlist_corpus/rp2040/raspberrypipress-pico-kicad"
            "/eg/ch11/rp2040_game_con/con_button1.kicad_sch"
        )),
    ):
        if Path(candidate).exists():
            fixture_src = Path(candidate)
            break
    if fixture_src is None:
        pytest.skip("no real .kicad_sch fixture available")

    repo = tmp_path / "real_repo"
    repo.mkdir()
    dest_sch = repo / (fixture_src.stem + ".kicad_sch")
    shutil.copy(fixture_src, dest_sch)
    _write(repo / (fixture_src.stem + ".kicad_pro"), "{}")

    result = export_repo(repo, tmp_path / "real_out")
    assert result["status"] in ("EXPORTED", "EMPTY_EXPORT")
    assert result["kicad_cli_status"] == "exported"

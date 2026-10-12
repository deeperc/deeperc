"""Unit test for the TODO-403 supply-evidence console block (pipeline.py,
printed after build_report() returns — see the TODO-403 Phase 1 recon P1
finding for why it isn't printed immediately before the RESULT box). Runs the
real demo netlist (examples/my_stm32_board_i2c_swap) with ctx.datasheets_dir
pointed at an empty tmp dir so zero PDFs are found. This reproduces the LT-33
C0 zero-PDF/cache_unverified shape without touching the real corpus_results/
output path (output_path is redirected to tmp_path). No network, no live API.

The cache the run reads is built in tmp_path, not the machine's own
datasheets_parsed/: it holds copies of exactly the two shipped stems this board
binds (5272_ams1117-3.3 for U1, stm32f103c8 for U2). pipeline.configure_resolver
rebinds step_03_resolver.PARSED_DIR from pipeline.DEFAULT_PARSED_DIR on every
run, so the fixture monkeypatches that global (and restores step_03_resolver's
own afterwards). That makes the result independent of whatever else sits in a
developer's cache: an untracked sibling directory such as `ams1117-3.3/` next to
the shipped `5272_ams1117-3.3/` makes the lookup for 'AMS1117-3.3' ambiguous, so
U1 never binds (5 evidence rows) while a clean tree binds it (6 rows). The
expected state asserted here is the clean one — U1's VI pin is admitted
(TODO-484), its evidence line carries the doc-identity MISS suffix, and U2's five
lines are Cache-sourced and not locally verified. One extra test plants the
sibling directory on purpose and pins the degraded, ambiguity state.

pipeline.py is not a checker_code_hash member (CORE_PIPELINE_FILES,
provenance.py), so this console-only fix carries no era break.
"""
import os
import shutil
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
POC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, POC)
sys.path.insert(0, REPO)

import pipeline  # noqa: E402
from steps import step_03_resolver  # noqa: E402

DEMO_NETLIST = os.path.join(
    REPO, "examples", "my_stm32_board_i2c_swap", "my_stm32_board_i2c_swap.net")
SHIPPED_STEMS = ("5272_ams1117-3.3", "stm32f103c8")
SHIPPED_PARSED = os.path.join(POC, "datasheets_parsed")

pytestmark = [
    pytest.mark.skipif(not os.path.exists(DEMO_NETLIST), reason="demo netlist not present"),
    pytest.mark.skipif(
        not all(os.path.isdir(os.path.join(SHIPPED_PARSED, s)) for s in SHIPPED_STEMS),
        reason="shipped cache stems not present"),
]


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    """A tmp cache root holding copies of the two shipped stems this board binds.

    Also forces the L2 vendor tier off: with an exported vendor-API credential pair these tests
    placed live lookups and downloaded a PDF into the tmp datasheets dir.
    """
    root = tmp_path / "datasheets_parsed"
    root.mkdir()
    for stem in SHIPPED_STEMS:
        shutil.copytree(os.path.join(SHIPPED_PARSED, stem), root / stem)
    monkeypatch.setattr(pipeline, "DEFAULT_PARSED_DIR", str(root))
    # configure_resolver overwrites this global on each run; the setattr registers
    # the pre-test value so monkeypatch restores it afterwards.
    monkeypatch.setattr(step_03_resolver, "PARSED_DIR", step_03_resolver.PARSED_DIR)
    monkeypatch.setattr(step_03_resolver, "_l2_resolve_ok", lambda: False)
    return root


def _run_zero_pdf(tmp_path):
    empty_datasheets = tmp_path / "no_datasheets"
    empty_datasheets.mkdir()
    ctx = pipeline.PipelineContext(
        netlist_path=DEMO_NETLIST,
        skip_confirm=True,
        datasheets_dir=str(empty_datasheets),
        output_path=str(tmp_path / "report.json"),
    )
    return pipeline.run_board(ctx)


def _evidence_lines(out: str) -> list[str]:
    lines = out.splitlines()
    header_idx = lines.index("[STEP 08b] Supply evidence:")
    evidence = []
    for line in lines[header_idx + 1:]:
        if not line.startswith("  "):
            break
        evidence.append(line)
    return evidence


def test_supply_evidence_header_and_six_tiered_lines(tmp_path, cache_root, capsys):
    outcome = _run_zero_pdf(tmp_path)
    assert not isinstance(outcome, pipeline.PipelineFailure), outcome

    out = capsys.readouterr().out
    assert "[STEP 08b] Supply evidence:" in out

    evidence = _evidence_lines(out)
    assert len(evidence) == 6, evidence


def test_supply_evidence_labels_are_tier2_cache_sourced(tmp_path, cache_root, capsys):
    outcome = _run_zero_pdf(tmp_path)
    assert not isinstance(outcome, pipeline.PipelineFailure), outcome

    out = capsys.readouterr().out
    evidence = _evidence_lines(out)
    assert len(evidence) == 6, evidence
    for line in evidence:
        assert "Cache-sourced — " in line, line
        assert "(not locally verified)" in line, line

    u1 = [ln for ln in evidence if " U1 AMS1117-3.3 VI (pin 3)" in ln]
    u2 = [ln for ln in evidence if " U2 STM32F103C8T6 " in ln]
    assert len(u1) == 1 and len(u2) == 5, evidence
    assert u1[0].rstrip().endswith("; source-text MPN match: none"), u1[0]
    for line in u2:
        assert line.rstrip().endswith("(not locally verified)"), line


def test_existing_aggregate_line_still_present_unchanged(tmp_path, cache_root, capsys):
    outcome = _run_zero_pdf(tmp_path)
    assert not isinstance(outcome, pipeline.PipelineFailure), outcome

    out = capsys.readouterr().out
    assert "[STEP 08b] Supply checks: 6 PASS  0 WARN  0 FAIL  0 UNRESOLVABLE" in out


def test_supply_evidence_lines_are_pairwise_distinct(tmp_path, cache_root, capsys):
    """TODO-403 follow-up: refdes+part_number+connected_net alone collides
    across U2's four +3.3V VDD pins — the supply_pin field (threaded into the
    identity as `<supply_pin> <- <connected_net>`) is what makes each of the
    6 demo lines unique."""
    outcome = _run_zero_pdf(tmp_path)
    assert not isinstance(outcome, pipeline.PipelineFailure), outcome

    out = capsys.readouterr().out
    evidence = _evidence_lines(out)
    assert len(evidence) == 6, evidence
    assert len(evidence) == len(set(evidence)), evidence


def test_sibling_cache_dir_makes_ams1117_ambiguous_and_u1_unbound(
        tmp_path, cache_root, capsys):
    """The degraded state, pinned on purpose: a second directory whose name contains
    'ams1117-3.3' beside the shipped stem makes the cache lookup ambiguous (canonical
    ambiguity is terminal), so U1 binds nothing — 5 evidence rows, all U2, and the
    'No datasheet found' warning for AMS1117-3.3."""
    (cache_root / "ams1117-3.3").mkdir()
    outcome = _run_zero_pdf(tmp_path)
    assert not isinstance(outcome, pipeline.PipelineFailure), outcome

    captured = capsys.readouterr()
    out = captured.out
    evidence = _evidence_lines(out)
    assert len(evidence) == 5, evidence
    assert all(" U2 STM32F103C8T6 " in ln for ln in evidence), evidence
    assert "[STEP 08b] Supply checks: 5 PASS  0 WARN  0 FAIL  0 UNRESOLVABLE" in out
    assert "No datasheet found for 'AMS1117-3.3'" in out + captured.err

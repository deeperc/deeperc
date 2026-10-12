"""Unit test for the step 04b console-honesty fix: the "[STEP 04b] ✓ Extracted
pin groups for {part}" line used to print unconditionally after
extractors.run_extractor() returned, even when the return was None (0 pin
groups extracted -> part reports UNRESOLVABLE). Console/log-only defect;
pipeline.py is not a checker_code_hash member (see CORE_PIPELINE_FILES in
provenance.py), so this fix carries no era break.

No network, no live API -- the extractor impl is mocked; a single
self-contained synthetic 1-component board + tmp datasheets/parsed dirs
(never the real datasheets_parsed/ cache). Mirrors test_todo367_refresh.py's
board_fixture pattern.
"""
import os
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
POC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, POC)
sys.path.insert(0, REPO)

import pipeline  # noqa: E402
from steps import extractors as ex  # noqa: E402
from steps import step_03_resolver as r3  # noqa: E402

_BOARD_TEMPLATE = '''(export (version "E")
  (components
    (comp (ref "U1")
      (value "{mpn}")
      (fields
        (field (name "MPN") "{mpn}"))
      (pins
        (pin (num "10") (name "PA0") (type "bidirectional"))
        (pin (num "23") (name "VDD") (type "power_in"))
        (pin (num "22") (name "GND") (type "power_in")))))
  (nets
    (net (name "VCC_3V3")
      (node (ref "U1") (pin "23")))
    (net (name "GND")
      (node (ref "U1") (pin "22")))
    (net (name "SIGNAL_QA")
      (node (ref "U1") (pin "10")))))
'''


class _FakeImpl(ex.ExtractorImpl):
    name = "console_honesty_test_fake"
    input_path = "fake_input"
    extractor_tag = "fake"
    model = "fake-model"

    def __init__(self, result):
        self._result = result
        self.calls = 0

    def available(self):
        return True

    def build_model_input(self, ctx):
        return "INPUT"

    def extract(self, model_input, ctx):
        self.calls += 1
        return self._result


@pytest.fixture
def board_fixture(tmp_path, monkeypatch):
    """Self-contained 1-component board: fake PDF + pre-seeded MinerU .md (so
    _parse_pdf never touches the fake PDF bytes) under tmp datasheets/parsed
    dirs. Redirects pipeline's module-level default dirs so run_board's
    configure_resolver() never points at the real corpus/cache. No pin-groups
    cache is pre-seeded -- extraction always runs, exercising the 04b call
    site under test."""
    mpn = "CONSOLEHONESTYFAKE1"
    stem = mpn.lower()
    datasheets_dir = tmp_path / "datasheets"
    parsed_dir = tmp_path / "parsed"
    datasheets_dir.mkdir()
    parsed_dir.mkdir()

    (datasheets_dir / f"{mpn}.pdf").write_bytes(b"%PDF-1.4 fake\n")
    mineru_dir = parsed_dir / stem / "auto"
    mineru_dir.mkdir(parents=True)
    (mineru_dir / f"{stem}.md").write_text("# fake datasheet md\n")

    board = tmp_path / "board.net"
    board.write_text(_BOARD_TEMPLATE.format(mpn=mpn))

    monkeypatch.setattr(pipeline, "DEFAULT_DATASHEETS_DIR", str(datasheets_dir))
    monkeypatch.setattr(pipeline, "DEFAULT_PARSED_DIR", str(parsed_dir))
    monkeypatch.setattr(r3, "PARSED_DIR", str(parsed_dir))
    monkeypatch.setattr(r3, "DATASHEETS_DIR", str(datasheets_dir))
    monkeypatch.setattr(r3, "STAGING_DIR", str(parsed_dir))
    monkeypatch.setattr(pipeline, "DEFAULT_STAGING_DIR", str(parsed_dir))
    quarantine_dir = tmp_path / "quarantine"
    monkeypatch.setattr(r3, "QUARANTINE_DIR", str(quarantine_dir))
    monkeypatch.setattr(pipeline, "DEFAULT_QUARANTINE_DIR", str(quarantine_dir))

    return {"mpn": mpn, "stem": stem, "board": board}


def _run(board_path, tmp_path):
    ctx = pipeline.PipelineContext(
        netlist_path=str(board_path), skip_confirm=True,
        output_path=str(tmp_path / "report.json"))
    return pipeline.run_board(ctx)


def test_none_extraction_result_emits_honest_line_not_success_line(
        board_fixture, monkeypatch, capsys, tmp_path):
    b = board_fixture
    fake = _FakeImpl(None)  # run_extractor's own guards collapse falsy/no-groups to None
    monkeypatch.setattr(pipeline.extractors, "get_extractor", lambda *a, **kw: fake)

    outcome = _run(b["board"], tmp_path)
    assert not isinstance(outcome, pipeline.PipelineFailure), outcome
    assert fake.calls == 1

    out = capsys.readouterr().out
    assert (f"[STEP 04b] extraction produced no pin groups for {b['mpn']} "
            "— part will report UNRESOLVABLE") in out
    assert f"[STEP 04b] ✓ Extracted pin groups for {b['mpn']}" not in out


def test_non_none_extraction_result_emits_success_line(board_fixture, monkeypatch, capsys, tmp_path):
    b = board_fixture
    fake = _FakeImpl({"pin_groups": [{"group_name": "GRP"}]})
    monkeypatch.setattr(pipeline.extractors, "get_extractor", lambda *a, **kw: fake)

    outcome = _run(b["board"], tmp_path)
    assert not isinstance(outcome, pipeline.PipelineFailure), outcome
    assert fake.calls == 1

    out = capsys.readouterr().out
    assert f"[STEP 04b] ✓ Extracted pin groups for {b['mpn']}" in out
    assert "extraction produced no pin groups" not in out

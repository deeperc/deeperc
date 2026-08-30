"""Unit tests for d2 --refresh (TODO-367): targeted cache-bypass at load time,
routed onto the existing (unchanged) extraction path. No network, no live API —
the extractor impl is mocked; a single self-contained synthetic 1-component
board + tmp datasheets/parsed dirs (never the real datasheets_parsed/ cache).

The MinerU-output .md is pre-seeded so _parse_pdf short-circuits at its cache
check (step_03_resolver.py) without touching pdfplumber/MinerU on the fake PDF
bytes at all — deterministic and fast.
"""
import json
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
    name = "refresh_test_fake"
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
    """Builds a self-contained 1-component board: fake PDF + pre-seeded MinerU
    .md (so _parse_pdf never touches the fake PDF bytes) under tmp datasheets/
    parsed dirs. Redirects pipeline's module-level default dirs so run_board's
    configure_resolver() never points at the real corpus/cache."""
    mpn = "REFRESHFAKE1"
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
    # Also patch r3 directly: cache pre-seeding below (via r3.save_pin_groups_cache)
    # happens BEFORE run_board's configure_resolver() would apply the DEFAULT_*
    # redirect above — without this, pre-seeding could hit the REAL cache dir.
    monkeypatch.setattr(r3, "PARSED_DIR", str(parsed_dir))
    monkeypatch.setattr(r3, "DATASHEETS_DIR", str(datasheets_dir))
    # TODO-386 Phase 3: extraction writes default to STAGING_DIR (R-C), and
    # configure_resolver rebinds it from pipeline.DEFAULT_STAGING_DIR. Both are
    # pointed at the same parsed_dir so --refresh keeps being tested on its own
    # terms (re-extract + .pre_reextract sidecar), not on tier routing — that is
    # tests/test_staging_tier.py's job.
    monkeypatch.setattr(r3, "STAGING_DIR", str(parsed_dir))
    monkeypatch.setattr(pipeline, "DEFAULT_STAGING_DIR", str(parsed_dir))
    # TODO-388 Phase 2 (R-β): third root, isolated on the same reasoning —
    # both the module global and the pipeline default configure_resolver
    # rebinds it from.
    quarantine_dir = tmp_path / "quarantine"
    monkeypatch.setattr(r3, "QUARANTINE_DIR", str(quarantine_dir))
    monkeypatch.setattr(pipeline, "DEFAULT_QUARANTINE_DIR", str(quarantine_dir))

    return {
        "mpn": mpn, "stem": stem, "board": board,
        "pdf_path": str(datasheets_dir / f"{mpn}.pdf"),
        "cache_path": mineru_dir / f"{stem}_pin_groups.json",
        "sidecar_path": mineru_dir / f"{stem}_pin_groups.json.pre_reextract",
    }


def _run(board_path, refresh_stems=frozenset()):
    ctx = pipeline.PipelineContext(
        netlist_path=str(board_path), skip_confirm=True,
        refresh_stems=refresh_stems,
    )
    return pipeline.run_board(ctx)


def test_named_stem_reextracts_and_sidecars_old_cache(board_fixture, monkeypatch):
    b = board_fixture
    r3.save_pin_groups_cache(
        b["pdf_path"],
        {"pin_groups": [{"group_name": "OLD"}]}, extractor="gemma")
    # dest_path resolution above uses get_pin_groups_cache_path internally via
    # PARSED_DIR (already redirected by the fixture), so this seeds the SAME
    # cache_path the pipeline will read/overwrite.
    assert b["cache_path"].exists()
    old_bytes = b["cache_path"].read_bytes()

    fake = _FakeImpl({"pin_groups": [{"group_name": "NEW"}]})
    monkeypatch.setattr(pipeline.extractors, "get_extractor", lambda *a, **kw: fake)

    outcome = _run(b["board"], refresh_stems=frozenset({b["stem"]}))
    assert not isinstance(outcome, pipeline.PipelineFailure), outcome

    assert fake.calls == 1  # re-extracted, not served from cache
    assert b["sidecar_path"].exists()
    assert b["sidecar_path"].read_bytes() == old_bytes
    assert json.loads(b["cache_path"].read_text())["pin_groups"] == [{"group_name": "NEW"}]


def test_unnamed_stem_serves_cache_untouched(board_fixture, monkeypatch):
    b = board_fixture
    r3.save_pin_groups_cache(
        b["pdf_path"],
        {"pin_groups": [{"group_name": "CACHED"}]}, extractor="gemma")
    old_bytes = b["cache_path"].read_bytes()

    fake = _FakeImpl({"pin_groups": [{"group_name": "SHOULD_NOT_APPEAR"}]})
    monkeypatch.setattr(pipeline.extractors, "get_extractor", lambda *a, **kw: fake)

    outcome = _run(b["board"], refresh_stems=frozenset())  # no --refresh at all
    assert not isinstance(outcome, pipeline.PipelineFailure), outcome

    assert fake.calls == 0  # never invoked — cache served
    assert not b["sidecar_path"].exists()
    assert b["cache_path"].read_bytes() == old_bytes  # byte-identical, untouched


def test_unknown_stem_returns_clear_pipeline_failure(board_fixture, monkeypatch):
    b = board_fixture
    r3.save_pin_groups_cache(
        b["pdf_path"],
        {"pin_groups": [{"group_name": "CACHED"}]}, extractor="gemma")

    fake = _FakeImpl({"pin_groups": [{"group_name": "SHOULD_NOT_APPEAR"}]})
    monkeypatch.setattr(pipeline.extractors, "get_extractor", lambda *a, **kw: fake)

    outcome = _run(b["board"], refresh_stems=frozenset({"totally_unknown_stem_xyz"}))
    assert isinstance(outcome, pipeline.PipelineFailure)  # clear error, not a silent no-op
    assert outcome.kind == "refresh"
    assert "totally_unknown_stem_xyz" in outcome.detail
    assert b["stem"] in outcome.detail  # names the known stem(s) to help the retry
    assert fake.calls == 0
    assert not b["sidecar_path"].exists()  # nothing was touched


def test_refresh_match_is_case_insensitive(board_fixture, monkeypatch):
    b = board_fixture
    r3.save_pin_groups_cache(
        b["pdf_path"],
        {"pin_groups": [{"group_name": "OLD"}]}, extractor="gemma")

    fake = _FakeImpl({"pin_groups": [{"group_name": "NEW"}]})
    monkeypatch.setattr(pipeline.extractors, "get_extractor", lambda *a, **kw: fake)

    outcome = _run(b["board"], refresh_stems=frozenset({b["stem"].upper()}))
    assert not isinstance(outcome, pipeline.PipelineFailure), outcome
    assert fake.calls == 1


def test_main_py_has_refresh_flag_no_refresh_all():
    """V3: --refresh is documented on the located CLI surface (main.py); no
    --refresh-all exists anywhere in the repo's Python sources."""
    import subprocess
    r = subprocess.run([sys.executable, os.path.join(POC, "main.py"), "--help"],
                       capture_output=True, text=True)
    assert "--refresh" in r.stdout
    assert "--refresh-all" not in r.stdout

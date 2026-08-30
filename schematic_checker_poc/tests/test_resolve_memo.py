"""Unit tests for pipeline.py's per-board memoization of resolve_and_parse
(TODO-226).

The step-3 resolve loop caches each unique (stripped) part number's
`resolve_and_parse` result for the duration of one board, so repeated refdes
instances of the same part reuse the first resolution instead of re-running the
L1 walk + L2 vendor lookup + PDF parse (03_resolve is ~99% of corpus wall
time — TODO-224).

Aliasing path taken: SHARE (not copy). The resolution doc is provably read-only
across every downstream consumer (it never escapes run_board's scope and no
consumer mutates it — PRE-FLIGHT aliasing gate). The aliasing-SAFETY test below
therefore proves IMMUTABILITY: after the heaviest real consumer chain (the full
downstream pipeline) runs over the shared docs, each doc is byte-identical to a
pre-run snapshot — so sharing one object across refdes instances is safe.

Synthetic boards drive the call-count / leakage tests with a mocked resolver so
they are deterministic and make 0 live network/LLM calls; the immutability test
uses a real cache-warm board (the same one test_step_timing.py uses) so it
exercises the ACTUAL consumers.
"""
import copy
import re
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
POC = REPO / "schematic_checker_poc"
sys.path.insert(0, str(POC))
sys.path.insert(0, str(REPO))

import pipeline  # noqa: E402
from steps import step_03_resolver  # noqa: E402

_PRODUCTION_PARSED_DIR = POC / "datasheets_parsed"


def _fake_doc(part: str) -> dict:
    """A resolve_and_parse-shaped doc with the keys the downstream pipeline reads."""
    return {
        "part_number": part.strip(),
        "pdf_path": f"/fake/{part.strip()}.pdf",
        "markdown_path": f"/fake/{part.strip()}.md",
        "markdown_text": "# fake",
        "mineru_used": False,
        "placeholder_patches": [],
        "resolver": "l1_local",
    }


def _write_board(tmp_path: Path, name: str, comps: list[tuple[str, str]]) -> Path:
    """Write a minimal KiCad-.net board where every (refdes, part) is a receiver:
    each component sits on signal net SIG1 (via a non-power pin IO1) plus GND."""
    comp_blocks = "\n".join(
        f'    (comp (ref "{ref}") (value "{part}")\n'
        f'      (pins (pin (num "1") (name "IO1")) (pin (num "2") (name "GND"))))'
        for ref, part in comps
    )
    sig_nodes = " ".join(f'(node (ref "{ref}") (pin "1"))' for ref, _ in comps)
    gnd_nodes = " ".join(f'(node (ref "{ref}") (pin "2"))' for ref, _ in comps)
    text = (
        f'(export (version "E")\n'
        f'  (design (source "{name}.net"))\n'
        f'  (components\n{comp_blocks}\n  )\n'
        f'  (nets\n'
        f'    (net (code "1") (name "SIG1") {sig_nodes})\n'
        f'    (net (code "2") (name "GND") {gnd_nodes})\n'
        f'  )\n)\n'
    )
    path = tmp_path / f"{name}.net"
    path.write_text(text)
    return path


def _install_mock_resolver(monkeypatch) -> list[str]:
    """Patch resolve_and_parse with a call-recording spy + stub the pin-group cache
    so the extractor never runs live. Returns the list that records call args."""
    calls: list[str] = []

    def spy(part_number: str) -> dict:
        calls.append(part_number)
        return _fake_doc(part_number)

    monkeypatch.setattr(step_03_resolver, "resolve_and_parse", spy)
    monkeypatch.setattr(step_03_resolver, "load_cached_pin_groups",
                        lambda pdf_path: {"pin_groups": []})
    return calls


def _run(board: Path, tmp_path: Path):
    ctx = pipeline.PipelineContext(
        netlist_path=str(board), skip_confirm=True,
        output_path=str(tmp_path / "r.json"))
    out = pipeline.run_board(ctx)
    assert not isinstance(out, pipeline.PipelineFailure), out
    return ctx


def test_same_part_two_refdes_resolves_once(tmp_path, monkeypatch):
    """Two refdes of the SAME part → resolve_and_parse called exactly once, and
    both instances' shared part carries resolution data (drove extraction)."""
    calls = _install_mock_resolver(monkeypatch)
    board = _write_board(tmp_path, "same",
                         [("U1", "MEMOPART_ALPHA"), ("U2", "MEMOPART_ALPHA")])
    ctx = _run(board, tmp_path)
    assert calls == ["MEMOPART_ALPHA"], calls
    # equivalent resolution data: the single shared doc resolved into extraction
    assert "MEMOPART_ALPHA" in ctx.extraction_metadata
    assert ctx.extraction_metadata["MEMOPART_ALPHA"]["pdf_path"] == "/fake/MEMOPART_ALPHA.pdf"


def test_whitespace_variants_share_one_resolution(tmp_path, monkeypatch):
    """Keying is on part_number.strip(), matching resolve_and_parse's own internal
    strip — so 'X' and ' X ' are one unique part and resolve once."""
    calls = _install_mock_resolver(monkeypatch)
    board = _write_board(tmp_path, "ws",
                         [("U1", "MEMOPART_ALPHA"), ("U2", " MEMOPART_ALPHA ")])
    _run(board, tmp_path)
    assert len(calls) == 1, calls


def test_distinct_parts_resolve_separately(tmp_path, monkeypatch):
    """Distinct parts → distinct resolve_and_parse calls (memo must not over-merge)."""
    calls = _install_mock_resolver(monkeypatch)
    board = _write_board(tmp_path, "distinct",
                         [("U1", "PART_ALPHA"), ("U2", "PART_BETA")])
    _run(board, tmp_path)
    assert sorted(calls) == ["PART_ALPHA", "PART_BETA"], calls


def test_no_memo_leakage_between_boards(tmp_path, monkeypatch):
    """The memo is fresh per run_board call: a part shared across two sequentially
    processed boards is resolved once PER board (twice total), not once — proving
    the cache is board-local, not module-level."""
    calls = _install_mock_resolver(monkeypatch)
    board_a = _write_board(tmp_path, "a", [("U1", "SHARED_PART"), ("U2", "OTHER_A")])
    board_b = _write_board(tmp_path, "b", [("U1", "SHARED_PART"), ("U2", "OTHER_B")])
    _run(board_a, tmp_path)
    _run(board_b, tmp_path)
    assert calls.count("SHARED_PART") == 2, calls


# ── Aliasing safety (SHARE path → immutability proof) ────────────────────────
_REAL_BOARD = (REPO / "netlist_corpus" / "exported" / "avr_arduino"
               / "KiCad-Arduino-Boards" / "KiCad Projects" / "Arduino Leonardo"
               / "Headers.net")


def _board_cache_warm_parts(board: Path) -> list[str]:
    """Every distinct (comp (value "...")) on the board that has a matching
    production datasheets_parsed/<value> cache dir — i.e. the real parts this
    board's real resolve_and_parse calls will hit, as opposed to connector/
    footprint-shaped values with no part cache at all."""
    text = board.read_text()
    values = set(re.findall(r'\(value "([^"]*)"\)', text))
    # Match canonically: post-TODO-321 the production cache dir is lower-cased
    # regardless of the (case-varied) schematic value string.
    return [v for v in values if v and (_PRODUCTION_PARSED_DIR / v.lower()).is_dir()]


def _copy_cache_canonicalized(src: Path, dst: Path, stem: str) -> None:
    """Copy a production cache dir into a canonical (lower-cased) location AND
    lower-case the stem prefix of every file inside it. The resolver derives
    BOTH the cache-dir name and the inner .md / _pin_groups.json filenames from
    canonical_pdf_stem() (TODO-321), so a verbatim-cased inner filename is a
    cache miss exactly like a verbatim-cased dir would be — the copy must be
    fully canonical for the warm-cache path to be exercised."""
    shutil.copytree(src, dst)
    canon = stem.lower()
    if canon == stem:
        return
    for p in list(dst.rglob("*")):
        if p.is_file() and p.name.startswith(stem):
            p.rename(p.with_name(canon + p.name[len(stem):]))


@pytest.mark.skipif(not _REAL_BOARD.exists(), reason="fixture board not on disk")
def test_shared_doc_not_mutated_by_real_consumers(tmp_path, monkeypatch):
    """Aliasing safety for the SHARE path: run the FULL real pipeline (the heaviest
    real consumer chain) over the resolution docs and assert each doc is
    byte-identical to a pre-run deep-copy snapshot. This proves no downstream
    consumer mutates a resolution doc, so sharing one object across repeat refdes
    instances cannot let one instance corrupt another's data.

    PARSED_DIR isolation (TODO-227 Phase 2b follow-up): run_board's own
    configure_resolver() unconditionally resets step_03_resolver.PARSED_DIR to
    pipeline.DEFAULT_PARSED_DIR, so monkeypatching step_03_resolver.PARSED_DIR
    directly would be undone before resolve_and_parse ever runs. Instead this
    copies each board part's real (cache-warm) production cache dir into a tmp
    location and monkeypatches pipeline.DEFAULT_PARSED_DIR itself, so the real
    resolve_and_parse still hits a warm cache (exercising the actual read
    path — no live network/MinerU/pdfplumber calls) but any incidental write
    (e.g. a self-heal backfill or a Branch-B patch_state.json refresh) lands in
    the tmp copy, never in production datasheets_parsed/."""
    isolated_parsed = tmp_path / "datasheets_parsed_isolated"
    isolated_parsed.mkdir()
    cache_warm_parts = _board_cache_warm_parts(_REAL_BOARD)
    assert cache_warm_parts, "no cache-warm parts found for this board — isolation setup is stale"
    for part in cache_warm_parts:
        _copy_cache_canonicalized(
            _PRODUCTION_PARSED_DIR / part.lower(), isolated_parsed / part.lower(), part.lower())
    monkeypatch.setattr(pipeline, "DEFAULT_PARSED_DIR", str(isolated_parsed))
    # TODO-362 shortcut intercepts warm boards; forced MISS so this test still
    # exercises resolve_and_parse's own path.
    monkeypatch.setattr(step_03_resolver, "find_cached_dir_for_part", lambda part_number: None)

    real_resolve = step_03_resolver.resolve_and_parse
    captured: list[tuple[dict, dict]] = []

    def wrapper(part_number: str) -> dict:
        doc = real_resolve(part_number)
        captured.append((doc, copy.deepcopy(doc)))  # (live object, snapshot)
        return doc

    monkeypatch.setattr(step_03_resolver, "resolve_and_parse", wrapper)
    ctx = pipeline.PipelineContext(
        netlist_path=str(_REAL_BOARD), skip_confirm=True,
        output_path=str(tmp_path / "r.json"))
    out = pipeline.run_board(ctx)
    assert not isinstance(out, pipeline.PipelineFailure), out

    assert captured, "board resolved no parts — immutability test would be vacuous"
    for live, snapshot in captured:
        assert live == snapshot, "a downstream consumer mutated a shared resolution doc"


@pytest.mark.skipif(not _REAL_BOARD.exists(), reason="fixture board not on disk")
def test_shortcut_doc_not_mutated_by_real_consumers(tmp_path, monkeypatch):
    """TODO-362 Phase 1 sibling: same immutability proof as
    test_shared_doc_not_mutated_by_real_consumers above, but for the SHORTCUT
    path (_try_warm_cache_shortcut) instead of the resolve_and_parse
    fallthrough — the shortcut is the path a warm-cache board actually takes
    by default (unforced), so it needs its own aliasing-safety proof. Same
    isolated-cache setup; wraps _try_warm_cache_shortcut itself (the function
    pipeline.run_board actually calls) to capture each doc it produces."""
    isolated_parsed = tmp_path / "datasheets_parsed_isolated"
    isolated_parsed.mkdir()
    cache_warm_parts = _board_cache_warm_parts(_REAL_BOARD)
    assert cache_warm_parts, "no cache-warm parts found for this board — isolation setup is stale"
    for part in cache_warm_parts:
        _copy_cache_canonicalized(
            _PRODUCTION_PARSED_DIR / part.lower(), isolated_parsed / part.lower(), part.lower())
    monkeypatch.setattr(pipeline, "DEFAULT_PARSED_DIR", str(isolated_parsed))

    real_shortcut = pipeline._try_warm_cache_shortcut
    captured: list[tuple[dict, dict]] = []

    def wrapper(part_number: str, refresh_wanted: frozenset) -> dict | None:
        doc = real_shortcut(part_number, refresh_wanted)
        if doc is not None:
            captured.append((doc, copy.deepcopy(doc)))  # (live object, snapshot)
        return doc

    monkeypatch.setattr(pipeline, "_try_warm_cache_shortcut", wrapper)
    ctx = pipeline.PipelineContext(
        netlist_path=str(_REAL_BOARD), skip_confirm=True,
        output_path=str(tmp_path / "r.json"))
    out = pipeline.run_board(ctx)
    assert not isinstance(out, pipeline.PipelineFailure), out

    assert captured, "board resolved no parts via the shortcut — test would be vacuous"
    for live, snapshot in captured:
        assert live == snapshot, "a downstream consumer mutated a shared shortcut doc"

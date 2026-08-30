"""Unit tests for the pluggable extractor-stage interface (Commit A).

No network, no Gemma, no API — uses a fake impl and a tmp scratch path. Never
touches the real datasheets_parsed cache.

Exception: the tail `gemma_smoke`-marked test DOES exercise the dormant
`gemma_mineru` impl end-to-end against a local Ollama (auto-skips when Ollama is
unreachable). It writes only to a tmp scratch path — the real cache is untouched.
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from steps import extractors as ex  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_extractor_unavailable_log(monkeypatch):
    """LT-31: _EXTRACTOR_UNAVAILABLE_LOGGED caches its decision at module scope
    (mirrors step_03_resolver's _L2_DISABLED_LOGGED, LT-23/LT-29) — reset before
    every test so cross-test log order can't leak a stale "already logged" state."""
    monkeypatch.setattr(ex, "_EXTRACTOR_UNAVAILABLE_LOGGED", False)


def test_default_and_registry():
    # Commit C: default flipped to haiku_pdfplumber; gemma_mineru dormant-registered.
    assert ex.DEFAULT_EXTRACTOR == "haiku_pdfplumber"
    assert {"gemma_mineru", "haiku_pdfplumber"} <= set(ex.registered())
    imp = ex.get_extractor()
    assert imp.name == "haiku_pdfplumber"
    g = ex.get_extractor("gemma_mineru")
    assert g.input_path == "mineru_pdfplumber_patched"
    assert g.extractor_tag == "gemma"


def test_haiku_is_default_gemma_dormant():
    # Commit C: haiku_pdfplumber is the default; gemma_mineru stays registered
    # (dormant) so the seam is preserved and can be re-selected via config/env.
    assert ex.DEFAULT_EXTRACTOR == "haiku_pdfplumber"
    assert "gemma_mineru" in ex.registered()
    h = ex.get_extractor("haiku_pdfplumber")
    assert h.input_path == "pdfplumber_full"
    assert h.model == "claude-haiku-4-5-20251001"
    assert h.extractor_tag == "haiku_pdfplumber"
    # availability is purely key-gated; never raises.
    assert isinstance(h.available(), bool)


def test_get_extractor_env_override(monkeypatch):
    monkeypatch.setenv("SCHECKER_EXTRACTOR", "gemma_mineru")
    assert ex.get_extractor().name == "gemma_mineru"
    monkeypatch.setenv("SCHECKER_EXTRACTOR", "does_not_exist")
    try:
        ex.get_extractor()
        assert False, "expected KeyError"
    except KeyError:
        pass


# ── _api_key precedence (LT-31: .env-first, ANTHROPIC_API_KEY env-var fallback) ──
# A4-1 in extractor_public_scoping_recon.md: the .env-only original silently
# returned None whenever python-dotenv wasn't importable, even with a valid
# env var set. These tests lock the new precedence AND the cause classification
# (C1/C2/C3) that feeds unavailable_reason() / the once-per-run log line.

def test_api_key_dotenv_beats_env_var(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=from-dotenv\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-envvar")
    monkeypatch.setattr(ex, "_repo_root", lambda: tmp_path)
    h = ex.HaikuPdfplumberExtractor()
    assert h._api_key() == "from-dotenv"
    assert h.available() is True
    assert h.unavailable_reason() is None


def test_api_key_env_var_used_when_dotenv_file_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-envvar")
    monkeypatch.setattr(ex, "_repo_root", lambda: tmp_path)  # no .env written
    h = ex.HaikuPdfplumberExtractor()
    assert h._api_key() == "from-envvar"


def test_api_key_env_var_used_when_dotenv_keyless(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("SOME_OTHER_VAR=x\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-envvar")
    monkeypatch.setattr(ex, "_repo_root", lambda: tmp_path)
    h = ex.HaikuPdfplumberExtractor()
    assert h._api_key() == "from-envvar"


def test_api_key_all_absent_cause_c2(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(ex, "_repo_root", lambda: tmp_path)  # no .env
    h = ex.HaikuPdfplumberExtractor()
    assert h._api_key() is None
    assert h.available() is False
    assert h.unavailable_reason() == "no .env found and ANTHROPIC_API_KEY not set"


def test_api_key_dotenv_present_keyless_cause_c3(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("SOME_OTHER_VAR=x\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(ex, "_repo_root", lambda: tmp_path)
    h = ex.HaikuPdfplumberExtractor()
    assert h._api_key() is None
    assert h.unavailable_reason() == ".env found but contains no ANTHROPIC_API_KEY"


def test_api_key_broken_dotenv_import_dead_when_env_var_set(tmp_path, monkeypatch):
    """A4-1 dead: python-dotenv missing must no longer silently swallow a valid
    ANTHROPIC_API_KEY env var into None."""
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=from-dotenv\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-envvar")
    monkeypatch.setattr(ex, "_repo_root", lambda: tmp_path)
    monkeypatch.setitem(sys.modules, "dotenv", None)
    h = ex.HaikuPdfplumberExtractor()
    assert h._api_key() == "from-envvar"
    assert h.available() is True
    assert h.unavailable_reason() is None


def test_api_key_broken_dotenv_import_no_fallback_cause_c1(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=from-dotenv\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(ex, "_repo_root", lambda: tmp_path)
    monkeypatch.setitem(sys.modules, "dotenv", None)
    h = ex.HaikuPdfplumberExtractor()
    assert h._api_key() is None
    assert h.unavailable_reason() == "python-dotenv not installed — .env cannot be read"


# ── import-location (LT-31: extractors.py no longer sys.path-hacks to repo root) ──

def test_extractors_has_no_sys_path_hack():
    """V2: extractors.py must not mutate sys.path to reach the extraction prompt
    template / input-path function — it imports steps.extraction_input directly."""
    import inspect
    assert "sys.path" not in inspect.getsource(ex)


def test_extraction_input_independent_of_batch_extract_with_claude(monkeypatch):
    """P4: extractors.py's import path for EXTRACTION_PROMPT_TEMPLATE /
    extract_pdfplumber_full must not require batch_extract_with_claude.py to be
    importable at all — steps.extraction_input is self-contained."""
    import importlib
    monkeypatch.setitem(sys.modules, "batch_extract_with_claude", None)
    mod = importlib.reload(importlib.import_module("steps.extraction_input"))
    assert hasattr(mod, "EXTRACTION_PROMPT_TEMPLATE")
    assert hasattr(mod, "extract_pdfplumber_full")


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[2] / "batch_extract_with_claude.py").exists(),
    reason="batch_extract_with_claude.py not present (excluded from the public export)")
def test_legacy_batch_extract_reexport_resolves():
    """batch_extract_with_claude.py (repo root) must keep exposing
    EXTRACTION_PROMPT_TEMPLATE and extract_pdfplumber_full — verbatim re-exports
    from steps.extraction_input — so any existing importer keeps working.

    Guarded on the shim's own presence: this test covers the SHIM, not the
    extractor, so where the shim isn't shipped there is nothing to assert. The
    packaged-module side is covered unconditionally by
    test_extraction_input_independent_of_batch_extract_with_claude above, which is
    the assertion that actually matters in the exported tree. LT-15 Phase B.1.
    """
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    import batch_extract_with_claude as bxc
    from steps import extraction_input as ei
    assert bxc.EXTRACTION_PROMPT_TEMPLATE is ei.EXTRACTION_PROMPT_TEMPLATE
    assert bxc.extract_pdfplumber_full is ei.extract_pdfplumber_full


class _FakeImpl(ex.ExtractorImpl):
    name = "fake"
    input_path = "fake_input"
    extractor_tag = "fake"
    model = "fake-model"

    def __init__(self, *, avail=True, result=None, build_raises=False):
        self._avail = avail
        self._result = result
        self._build_raises = build_raises

    def available(self):
        return self._avail

    def build_model_input(self, ctx):
        if self._build_raises:
            raise RuntimeError("boom")
        return "INPUT"

    def extract(self, model_input, ctx):
        assert model_input == "INPUT"
        return self._result


def _ctx():
    return ex.PartContext(part_number="FOO", pdf_path="/nonexistent/FOO.pdf",
                          markdown_text="md")


def test_run_extractor_dry_run_writes_scratch(tmp_path):
    dest = tmp_path / "out.json"
    good = {"pin_groups": [{"group_name": "VCC", "pin_type": "power",
                            "supply_min": 2.0, "supply_max": 3.6}]}
    out = ex.run_extractor(_FakeImpl(result=good), _ctx(), dry_run_to=str(dest))
    assert out == good
    assert json.loads(dest.read_text())["pin_groups"][0]["supply_min"] == 2.0


def test_unavailable_impl_returns_none():
    assert ex.run_extractor(_FakeImpl(avail=False), _ctx()) is None


def test_empty_extract_returns_none(tmp_path):
    assert ex.run_extractor(_FakeImpl(result=None), _ctx(),
                            dry_run_to=str(tmp_path / "x.json")) is None
    assert ex.run_extractor(_FakeImpl(result={"pin_groups": []}), _ctx(),
                            dry_run_to=str(tmp_path / "y.json")) is None


def test_build_input_failure_returns_none(tmp_path):
    assert ex.run_extractor(_FakeImpl(build_raises=True, result={"pin_groups": [1]}),
                            _ctx(), dry_run_to=str(tmp_path / "z.json")) is None


def test_advisory_guard_is_non_destructive(tmp_path):
    # a 0/0 GND group would be flagged by validate_supply_group, but the advisory
    # guard must NOT drop it — the cached content is unchanged (guard is log-only).
    dest = tmp_path / "g.json"
    raw = {"pin_groups": [
        {"group_name": "VIN", "pin_type": "power", "supply_min": 2.5, "supply_max": 6.0},
        {"group_name": "GND", "pin_type": "power", "supply_min": 0.0, "supply_max": 0.0},
    ]}
    out = ex.run_extractor(_FakeImpl(result=raw), _ctx(), dry_run_to=str(dest))
    assert len(out["pin_groups"]) == 2  # nothing dropped
    assert len(json.loads(dest.read_text())["pin_groups"]) == 2


# ── doc-identity threading (TODO-337 Phase 2b) ───────────────────────────────

class _SlicingImpl(_FakeImpl):
    """Mimics HaikuPdfplumberExtractor's big-doc path: records the PRE-slice text
    on the ctx, then returns a slice that no longer contains the stem."""
    name = "slicing_fake"

    def build_model_input(self, ctx):
        ctx.source_text = "FOO123 Datasheet — full document text"
        return "<!-- section-slice -->\nonly window content, no part name here"

    def extract(self, model_input, ctx):
        return self._result


def _pdf_ctx(tmp_path):
    pdf = tmp_path / "FOO123.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake\n")
    return ex.PartContext(part_number="FOO123", pdf_path=str(pdf), markdown_text="md")


def test_doc_identity_written_from_preslice_text(tmp_path):
    """The verdict must be computed on the FULL text, not the section slice —
    otherwise every big datasheet false-MISSes on its own slice."""
    dest = tmp_path / "sliced.json"
    ex.run_extractor(_SlicingImpl(result={"pin_groups": [{"group_name": "VCC"}]}),
                     _pdf_ctx(tmp_path), dry_run_to=str(dest))
    prov = json.loads(dest.read_text())["provenance"]
    assert prov["doc_identity"]["class"] == "STRONG"   # `foo123` IS in the full text
    assert prov["doc_identity"]["tier"] == 1


def test_doc_identity_unverifiable_without_text(tmp_path):
    """An impl that records no pre-slice text and returns a non-string input gets
    `unverifiable` — never a guessed class."""
    class _NoTextImpl(_FakeImpl):
        def build_model_input(self, ctx):
            return {"not": "a string"}

        def extract(self, model_input, ctx):
            return self._result

    dest = tmp_path / "notext.json"
    ex.run_extractor(_NoTextImpl(result={"pin_groups": [{"group_name": "VCC"}]}),
                     _pdf_ctx(tmp_path), dry_run_to=str(dest))
    prov = json.loads(dest.read_text())["provenance"]
    assert prov["doc_identity"]["class"] == "unverifiable"


def test_doc_identity_miss_is_recorded_not_refused(tmp_path):
    """A MISS is EVIDENCE — the write still happens, nothing is quarantined."""
    class _WrongDocImpl(_FakeImpl):
        def build_model_input(self, ctx):
            ctx.source_text = "PIC12F508/509 In-Circuit Programming Specification"
            return ctx.source_text

        def extract(self, model_input, ctx):
            return self._result

    dest = tmp_path / "wrong.json"
    out = ex.run_extractor(_WrongDocImpl(result={"pin_groups": [{"group_name": "VCC"}]}),
                           _pdf_ctx(tmp_path), dry_run_to=str(dest))
    assert out is not None                    # not refused
    assert dest.exists()                      # cache still written
    assert json.loads(dest.read_text())["provenance"]["doc_identity"]["class"] == "MISS"


def test_parse_model_json_tolerates_fences_and_preamble():
    assert ex.parse_model_json('here you go:\n```json\n{"pin_groups": [1]}\n```')["pin_groups"] == [1]
    assert ex.parse_model_json('{"a": 1}')["a"] == 1
    try:
        ex.parse_model_json("no json here")
        assert False
    except ValueError:
        pass


# ── gemma_mineru dormant-path smoke test (Review §9) ──────────────────────────
# The gemma_mineru extractor is dormant (haiku_pdfplumber is the default). "Dormant-
# preserved" only means "working comparison path" if it is actually exercised — an
# untested dormant path bit-rots into a fossil that merely looks like a comparison
# path. This smoke test runs it end-to-end on a tiny fixture so an interface/import
# break (step_04a/step_04b/ollama_client rename or signature drift) fails LOUDLY
# rather than silently degrading to a no-op. No behaviour change to the impl itself.

# A tiny synthetic datasheet excerpt — enough real structure (abs-max + pin table +
# a supply range) for Gemma to emit at least one pin group, but trivially small.
_SMOKE_MD = """# ACME1234 Dual Non-Inverting Buffer

## Absolute Maximum Ratings
Supply Voltage (VCC) .......... -0.5 to 7.0 V
Input Voltage (any pin) ....... -0.5 to VCC + 0.5 V

## Pin Description
| Pin | Name | Type           |
|-----|------|----------------|
| 1   | VCC  | Power          |
| 2   | A1   | Digital Input  |
| 3   | Y1   | Digital Output |
| 4   | GND  | Power          |

## DC Electrical Characteristics
Operating supply voltage VCC = 4.5 to 5.5 V.
High-level input voltage VIH (min) = 2.0 V at VCC = 5.0 V.
"""


def _ollama_reachable(timeout: float = 2.0) -> bool:
    """True iff the local Ollama server answers on its base URL. Importing
    OLLAMA_URL here also gives the smoke path a light import check of llm.ollama_client."""
    try:
        from llm.ollama_client import OLLAMA_URL
    except Exception:
        return False
    base = OLLAMA_URL.rsplit("/api/", 1)[0]  # http://localhost:11434
    try:
        with urllib.request.urlopen(base, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


@pytest.mark.gemma_smoke
def test_gemma_mineru_smoke_end_to_end(tmp_path):
    """Exercise the dormant gemma_mineru impl end-to-end (locate_sections →
    extract_part_pin_groups → provenance-stamped scratch write) on a tiny fixture.
    Skipped when Ollama is unreachable (CI-less runs); runnable locally via the
    default `make test` gate or explicitly with `pytest -m gemma_smoke`."""
    if not _ollama_reachable():
        pytest.skip("Ollama not reachable at localhost:11434 — dormant gemma_mineru smoke skipped")

    impl = ex.get_extractor("gemma_mineru")
    assert impl.name == "gemma_mineru"
    ctx = ex.PartContext(
        part_number="ACME1234",
        pdf_path=str(tmp_path / "acme1234.pdf"),  # need not exist (scratch write)
        markdown_text=_SMOKE_MD,
    )

    # Direct method exercise — RAISES on import/interface bit-rot (the honesty guard).
    # The gemma path passes the resolver-parsed markdown straight through unchanged.
    # Do not switch this to run_extractor() — its tail swallows exceptions to None and
    # would silently defeat this test's bit-rot detection.
    model_input = impl.build_model_input(ctx)
    assert model_input == _SMOKE_MD
    # This calls Ollama twice (section locate + pin-group extract). A tiny fixture may
    # yield 0+ groups or, on an unparseable reply, None — so assert the SHAPE the
    # pipeline depends on, not the content. Bit-rot here would raise, not return None.
    raw = impl.extract(model_input, ctx)
    assert raw is None or (isinstance(raw, dict) and "pin_groups" in raw)

    # Full public path incl. the shared provenance-stamped cache write, routed to a
    # tmp scratch file so the real datasheets_parsed cache is never touched.
    dest = tmp_path / "acme1234_smoke.json"
    out = ex.run_extractor(impl, ctx, dry_run_to=str(dest))
    if out is not None:
        assert dest.exists(), "run_extractor should have written the scratch cache file"
        assert "pin_groups" in json.loads(dest.read_text())

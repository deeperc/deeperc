"""Unit tests for llm/ollama_client.py's log-write isolation (TODO-221 Commit B).

Covers: a log-write failure must never raise/masquerade as "Ollama unreachable";
a genuine network failure must still raise RuntimeError as before; a relative
GEMMA_LOG_FILE must resolve to an absolute path once at import time so a later
os.chdir() (e.g. run_checks's per-board chdir) can't break it. No network.
"""

import importlib
import json
import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from llm import ollama_client as oc  # noqa: E402


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_log_write_failure_does_not_raise_or_misreport(monkeypatch, tmp_path, caplog):
    """A log-write failure (unwritable path) must not raise, and must not be
    reported as 'Ollama unreachable' — the call itself succeeds."""
    bogus_log = tmp_path / "no_such_parent_dir_because_a_file_is_there"
    bogus_log.write_text("not a directory")  # makes os.makedirs(dirname) fail if dirname == this path
    unwritable_log_path = str(bogus_log / "gemma.jsonl")  # parent is a file, not a dir

    monkeypatch.setattr(oc, "_GEMMA_LOG_FILE", unwritable_log_path)

    monkeypatch.setattr(
        oc.urllib.request, "urlopen",
        lambda req, timeout=300: _FakeResponse({"response": "ok", "prompt_eval_count": 1, "eval_count": 1}),
    )

    result = oc.generate("hello", step_hint="test")

    assert result == "ok"


def test_genuine_network_failure_still_raises_runtimeerror(monkeypatch):
    """Network failures must still surface as RuntimeError('Ollama unreachable: ...')."""
    def _raise_network_error(req, timeout=300):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(oc.urllib.request, "urlopen", _raise_network_error)

    with pytest.raises(RuntimeError, match="Ollama unreachable"):
        oc.generate("hello", step_hint="test")


def test_relative_gemma_log_file_resolved_absolute_at_import(monkeypatch, tmp_path):
    """GEMMA_LOG_FILE set as a relative path must be resolved to an absolute
    path once at import time, so a later os.chdir() elsewhere in the process
    can't break it."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GEMMA_LOG_FILE", "logs/gemma.jsonl")

    mod = importlib.reload(oc)
    try:
        assert mod._GEMMA_LOG_FILE == str(tmp_path / "logs" / "gemma.jsonl")

        # Simulate a later chdir (e.g. run_checks's per-board os.chdir) —
        # the already-resolved absolute path must still be correct.
        other_dir = tmp_path / "elsewhere"
        other_dir.mkdir()
        monkeypatch.chdir(other_dir)

        monkeypatch.setattr(
            mod.urllib.request, "urlopen",
            lambda req, timeout=300: _FakeResponse({"response": "ok", "prompt_eval_count": 1, "eval_count": 1}),
        )
        mod.generate("hello", step_hint="test")

        written = (tmp_path / "logs" / "gemma.jsonl")
        assert written.exists()
        record = json.loads(written.read_text().splitlines()[0])
        assert record["prompt"] == "hello"
    finally:
        monkeypatch.delenv("GEMMA_LOG_FILE", raising=False)
        importlib.reload(oc)  # restore the real module state for other tests

"""Unit tests for llm/ollama_client.py's TODO-519 legs: short env-overridable
timeout, DEEPERC_NO_LOCAL_LLM opt-out, and the per-process fail-fast breaker.

Authored by Claude.ai as a verbatim grader for the C-flow pilot (plan section 4: tests
are ADDED as verbatim files, never authored by CC). No network: every test
patches urllib.request.urlopen at the same seam tests/test_ollama_client.py
uses, so the existing floor tests and these share one contract: generate()
must keep routing through urllib.request.urlopen(req, timeout=...).
"""

import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from llm import ollama_client as oc  # noqa: E402


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_OK_BODY = b'{"response": "ok", "prompt_eval_count": 1, "eval_count": 1}'


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Every test starts with the breaker closed and neither env var set."""
    monkeypatch.delenv("DEEPERC_NO_LOCAL_LLM", raising=False)
    monkeypatch.delenv("DEEPERC_LLM_TIMEOUT_S", raising=False)
    oc.reset_breaker()
    yield
    oc.reset_breaker()


def _install_recorder(monkeypatch, outcome):
    """Patch urlopen with a fake that records each call's timeout kwarg and
    either returns an OK response or raises `outcome` (an exception instance)."""
    calls = []

    def _fake_urlopen(req, timeout=None):
        calls.append(timeout)
        if isinstance(outcome, BaseException):
            raise outcome
        return _FakeResponse(_OK_BODY)

    monkeypatch.setattr(oc.urllib.request, "urlopen", _fake_urlopen)
    return calls


def test_default_timeout_is_60s():
    assert oc.DEFAULT_TIMEOUT_S == 60.0
    assert oc.llm_timeout_s() == 60.0


def test_timeout_read_from_env_at_call_time(monkeypatch):
    calls = _install_recorder(monkeypatch, None)
    monkeypatch.setenv("DEEPERC_LLM_TIMEOUT_S", "7.5")
    assert oc.generate("p") == "ok"
    assert calls == [7.5]


def test_timeout_env_invalid_falls_back_to_default(monkeypatch):
    calls = _install_recorder(monkeypatch, None)
    monkeypatch.setenv("DEEPERC_LLM_TIMEOUT_S", "soon")
    assert oc.generate("p") == "ok"
    assert calls == [60.0]


def test_opt_out_raises_without_opening_a_socket(monkeypatch):
    calls = _install_recorder(monkeypatch, None)
    monkeypatch.setenv("DEEPERC_NO_LOCAL_LLM", "1")
    with pytest.raises(RuntimeError, match="Ollama unreachable"):
        oc.generate("p")
    assert calls == [], "opt-out must not call urlopen"
    assert oc.breaker_tripped() is False, "opt-out is not a failure; it must not trip the breaker"


def test_opt_out_zero_is_not_opted_out(monkeypatch):
    calls = _install_recorder(monkeypatch, None)
    monkeypatch.setenv("DEEPERC_NO_LOCAL_LLM", "0")
    assert oc.generate("p") == "ok"
    assert calls == [60.0]


def test_connection_failure_trips_breaker_and_skips_later_calls(monkeypatch):
    err = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    calls = _install_recorder(monkeypatch, err)
    with pytest.raises(RuntimeError, match="Ollama unreachable"):
        oc.generate("first")
    assert oc.breaker_tripped() is True
    with pytest.raises(RuntimeError, match="Ollama unreachable"):
        oc.generate("second")
    with pytest.raises(RuntimeError, match="Ollama unreachable"):
        oc.generate("third")
    assert len(calls) == 1, "after the first failure no further socket is opened"


def test_read_timeout_trips_breaker(monkeypatch):
    calls = _install_recorder(monkeypatch, TimeoutError("timed out"))
    with pytest.raises(RuntimeError, match="Ollama unreachable"):
        oc.generate("first")
    assert oc.breaker_tripped() is True
    with pytest.raises(RuntimeError):
        oc.generate("second")
    assert len(calls) == 1


def test_http_error_does_not_trip_breaker(monkeypatch):
    """Ollama answering with an HTTP error (e.g. 404 unknown model) is a fast,
    answered failure, not a hang, so it must not silence later calls."""
    err = urllib.error.HTTPError("http://localhost:11434/api/generate", 404,
                                 "Not Found", {}, None)
    calls = _install_recorder(monkeypatch, err)
    with pytest.raises(RuntimeError, match="Ollama unreachable"):
        oc.generate("first")
    assert oc.breaker_tripped() is False
    with pytest.raises(RuntimeError):
        oc.generate("second")
    assert len(calls) == 2


def test_success_never_trips_and_reset_reopens(monkeypatch):
    calls = _install_recorder(monkeypatch, None)
    assert oc.generate("p") == "ok"
    assert oc.breaker_tripped() is False
    # trip it with a real failure, then confirm reset_breaker() reopens the path
    _install_recorder(monkeypatch, TimeoutError("timed out"))
    with pytest.raises(RuntimeError):
        oc.generate("q")
    assert oc.breaker_tripped() is True
    oc.reset_breaker()
    calls2 = _install_recorder(monkeypatch, None)
    assert oc.generate("r") == "ok"
    assert calls == [60.0] and calls2 == [60.0]

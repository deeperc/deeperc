"""TODO-531 BUILD-B: batch draws with persisted results and resume (extractors.batch_extract_parts).

A fake Anthropic-compatible client is injected (create / retrieve / results); the model input is a monkeypatched
pdfplumber text. No network, no key: _api_key raises if anything asks for one.
"""
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from steps import extraction_input, extractors  # noqa: E402

ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
REPLY = json.dumps({"pin_groups": [{"group_name": "IO", "pin_type": "digital_input", "example_pins": ["1|IN"],
                                    "VIH_min": 2.0}], "default_group": "IO"})


class FakeClient:
    """Records every call in order; `status` drives retrieve(); `rows` overrides per custom id:
    ("stop", reason) / ("errored",) / ("text", text). Optional hooks observe the results dir."""

    def __init__(self, status="ended", rows=None, on_create=None, on_retrieve=None):
        self.events, self.requests, self.status = [], [], status
        self.rows, self.on_create, self.on_retrieve = rows or {}, on_create, on_retrieve
        self.messages = SimpleNamespace(batches=self)

    def create(self, requests):
        self.events.append("create")
        if self.on_create:
            self.on_create()
        self.requests = list(requests)
        return SimpleNamespace(id="msgbatch_fake1")

    def retrieve(self, batch_id):
        self.events.append(("retrieve", batch_id))
        if self.on_retrieve:
            self.on_retrieve()
        return SimpleNamespace(processing_status=self.status)

    def results(self, batch_id):
        self.events.append(("results", batch_id))
        for r in self.requests:
            cid = r["custom_id"]
            o = self.rows.get(cid, ("text", REPLY))
            if o[0] == "errored":
                yield SimpleNamespace(custom_id=cid, result=SimpleNamespace(
                    type="errored", error=SimpleNamespace(type="error", error={"type": "overloaded_error"})))
                continue
            text, stop = (o[1], "end_turn") if o[0] == "text" else (REPLY[:40], o[1])
            yield SimpleNamespace(custom_id=cid, result=SimpleNamespace(type="succeeded", message=SimpleNamespace(
                model="claude-haiku-4-5-20251001", stop_reason=stop,
                content=[SimpleNamespace(type="text", text=text)],
                usage=SimpleNamespace(input_tokens=100, output_tokens=10, service_tier="batch"))))


@pytest.fixture
def no_key(monkeypatch):
    def refuse(self):
        raise AssertionError("a key was read")
    monkeypatch.setattr(type(extractors.get_extractor("haiku_pdfplumber")), "_api_key", refuse)
    monkeypatch.setattr(extraction_input, "extract_pdfplumber_full", lambda p: f"VCC 1.8V to 3.6V {Path(p).name}")
    monkeypatch.setattr(extractors, "_POLL_INTERVAL_S", 0)


def ctxs(tmp_path, names=("FOO123", "BAR456")):
    out = []
    for n in names:
        p = tmp_path / "pdf" / f"{n}.pdf"
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(b"%PDF-fake")
        out.append(extractors.PartContext(part_number=n.split(".")[0].upper(), pdf_path=str(p)))
    return out


def test_defaults_reproduce_todays_requests_and_return(tmp_path, no_key):
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request
    impl = extractors.get_extractor("haiku_pdfplumber")
    cs = ctxs(tmp_path)
    fake = FakeClient()
    out = extractors.batch_extract_parts(cs, client=fake)
    want = [Request(custom_id=f"part-{i}", params=MessageCreateParamsNonStreaming(
        model=impl.model, max_tokens=impl._MAX_TOKENS, temperature=impl._TEMPERATURE,
        messages=[{"role": "user", "content": impl._prompt(c, f"VCC 1.8V to 3.6V {Path(c.pdf_path).name}")}]))
        for i, c in enumerate(cs)]
    assert fake.requests == want
    assert set(out) == {"FOO123", "BAR456", "__truncated__"} and out["__truncated__"] == []
    assert set(out["FOO123"]) == {"pin_groups", "default_group", "extractor_fingerprint", "request_sha256",
                                  "input_regime", "model_input"}
    assert out["FOO123"]["extractor_fingerprint"] == extractors.extractor_fingerprint(impl, draws=1,
                                                                                      aggregate="single")
    assert fake.events == ["create", ("retrieve", "msgbatch_fake1"), ("results", "msgbatch_fake1")]
    assert list(tmp_path.iterdir()) == [tmp_path / "pdf"]          # writes nothing


def test_bad_arguments(tmp_path, no_key):
    with pytest.raises(ValueError):
        extractors.batch_extract_parts(ctxs(tmp_path), draws=3, client=FakeClient())     # no results_dir
    with pytest.raises(ValueError):
        extractors.batch_extract_parts(ctxs(tmp_path), draws=0, results_dir=tmp_path / "r", client=FakeClient())
    dup = ctxs(tmp_path, ("FOO123",)) + ctxs(tmp_path, ("foo123",))
    fake = FakeClient()
    with pytest.raises(ValueError, match="duplicate"):
        extractors.batch_extract_parts(dup, draws=3, results_dir=tmp_path / "r", client=fake)
    assert fake.events == []


def test_draws_3_ids_manifest_order_and_return(tmp_path, no_key):
    rd = tmp_path / "res"
    seen = {}

    def at_create():
        seen["create"] = json.loads((rd / "manifest.json").read_text())

    def at_retrieve():
        seen.setdefault("first_poll", json.loads((rd / "manifest.json").read_text()))
    fake = FakeClient(on_create=at_create, on_retrieve=at_retrieve)
    cs = ctxs(tmp_path)
    out = extractors.batch_extract_parts(cs, draws=3, results_dir=rd, client=fake)
    ids = [r["custom_id"] for r in fake.requests]
    assert len(ids) == 6 and all(ID_RE.match(i) for i in ids) and ids[0] == "s0000-d1"
    assert seen["create"]["batch_id"] is None and len(seen["create"]["requests"]) == 6
    assert seen["first_poll"]["batch_id"] == "msgbatch_fake1"
    assert fake.events[0] == "create"
    prompts = {r["params"]["messages"][0]["content"] for r in fake.requests[:3]}
    assert len(prompts) == 1                                        # the three draws of a stem share one prompt
    man = json.loads((rd / "manifest.json").read_text())
    r0 = man["requests"]["s0000-d2"]
    assert (r0["stem"], r0["part_number"], r0["draw"], r0["input_regime"]) == ("foo123", "FOO123", 2, "whole")
    assert man["extractor_fingerprint"]["fields"]["draws"] == 1
    assert (rd / "inputs" / "0000.txt").read_text() == "VCC 1.8V to 3.6V FOO123.pdf"
    assert sorted(p.name for p in (rd / "raw").iterdir()) == sorted(f"{i}.json" for i in ids)
    assert set(out) == {"draws", "__truncated__", "__failed__", "batch_id", "usage"}
    assert out["batch_id"] == "msgbatch_fake1" and out["__truncated__"] == [] and out["__failed__"] == []
    d = out["draws"]["foo123"][2]
    assert d["pin_groups"][0]["VIH_min"] == 2.0 and d["stop_reason"] == "end_turn"
    assert d["usage"]["output_tokens"] == 10 and d["model_input"] == "VCC 1.8V to 3.6V FOO123.pdf"
    assert out["usage"]["input_tokens"] == 600 and out["usage"]["service_tier"] == ["batch"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["pdf", "res"]


def test_deadline_raises_pending_then_resume_does_not_create(tmp_path, no_key):
    rd = tmp_path / "res"
    fake = FakeClient(status="in_progress")
    with pytest.raises(extractors.BatchPending) as e:
        extractors.batch_extract_parts(ctxs(tmp_path), draws=3, results_dir=rd, client=fake, poll_deadline_s=0)
    assert e.value.batch_id == "msgbatch_fake1"
    assert json.loads((rd / "manifest.json").read_text())["batch_id"] == "msgbatch_fake1"
    fake.status, fake.events = "ended", []
    out = extractors.batch_extract_parts(ctxs(tmp_path), draws=3, results_dir=rd, client=fake)
    assert "create" not in fake.events
    assert fake.events[0] == ("retrieve", "msgbatch_fake1")
    assert out["draws"]["bar456"][3]["pin_groups"]


def test_resume_with_a_changed_prompt_raises(tmp_path, no_key):
    rd = tmp_path / "res"
    fake = FakeClient()
    extractors.batch_extract_parts(ctxs(tmp_path), draws=3, results_dir=rd, client=fake)
    cs = ctxs(tmp_path)
    cs[1].part_number = "BAR456-X"                  # a different prompt for the same custom ids
    fake.events = []
    with pytest.raises(ValueError, match="s0001-d1"):
        extractors.batch_extract_parts(cs, draws=3, results_dir=rd, client=fake)
    assert fake.events == []


def test_manifest_without_batch_id_refuses(tmp_path, no_key):
    rd = tmp_path / "res"
    rd.mkdir()
    (rd / "manifest.json").write_text(json.dumps({"batch_id": None, "requests": {}}))
    fake = FakeClient()
    with pytest.raises(ValueError, match="no batch id"):
        extractors.batch_extract_parts(ctxs(tmp_path), draws=3, results_dir=rd, client=fake)
    assert fake.events == []


def test_every_row_persisted_and_listed(tmp_path, no_key):
    rd = tmp_path / "res"
    fake = FakeClient(rows={"s0000-d2": ("stop", "max_tokens"), "s0000-d3": ("errored",),
                            "s0001-d1": ("text", "I could not find a table.")})
    out = extractors.batch_extract_parts(ctxs(tmp_path), draws=3, results_dir=rd, client=fake)
    assert len(list((rd / "raw").iterdir())) == 6
    cut = json.loads((rd / "raw" / "s0000-d2.json").read_text())
    assert cut["stop_reason"] == "max_tokens" and cut["text"] and cut["usage"]["service_tier"] == "batch"
    err = json.loads((rd / "raw" / "s0000-d3.json").read_text())
    assert err["result_type"] == "errored" and err["error"]["error"]["error"]["type"] == "overloaded_error"
    assert json.loads((rd / "raw" / "s0001-d1.json").read_text())["text"] == "I could not find a table."
    assert out["__truncated__"] == [["foo123", 2]]
    assert out["__failed__"] == [["foo123", 3, "result_type:errored"], ["bar456", 1, "unparseable"]]
    assert out["draws"]["foo123"] == {1: out["draws"]["foo123"][1], 2: None, 3: None}
    assert out["draws"]["bar456"][1] is None and out["draws"]["bar456"][2] is not None


def test_stems_with_a_space_and_dots_round_trip(tmp_path, no_key):
    rd = tmp_path / "res"
    names = ("mpu-6000 mpu-6050", "ds-000292-icm-42605-v1.7_0")
    fake = FakeClient()
    out = extractors.batch_extract_parts(ctxs(tmp_path, names), draws=3, results_dir=rd, client=fake)
    assert set(out["draws"]) == set(names)
    assert all(ID_RE.match(r["custom_id"]) for r in fake.requests)
    man = json.loads((rd / "manifest.json").read_text())
    assert {r["stem"] for r in man["requests"].values()} == set(names)
    assert all(out["draws"][s][d] is not None for s in names for d in (1, 2, 3))

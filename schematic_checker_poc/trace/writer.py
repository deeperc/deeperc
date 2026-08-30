"""Per-net trace writer.

Usage:
    from trace.writer import set_target, write_trace, flush

Call set_target() once per run (from the corpus runner) to enable tracing for
one net.  write_trace() is a no-op when tracing is disabled or when the
supplied net_name does not match the target — zero overhead for non-traced nets.
flush() serialises the accumulated records to disk.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

_target_net: str | None = None
_output_dir: Path | None = None
_run_label: str = ""
_sequence: int = 0
_records: list = []


def set_target(net_name: str, output_dir: Path, run_label: str) -> None:
    global _target_net, _output_dir, _run_label, _sequence, _records
    _target_net = net_name
    _output_dir = Path(output_dir)
    _run_label = run_label
    _sequence = 0
    _records = []


def clear_target() -> None:
    global _target_net, _output_dir, _run_label, _sequence, _records
    _target_net = None
    _output_dir = None
    _run_label = ""
    _sequence = 0
    _records = []


def _sanitize(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]", "_", name).strip("_")
    if s and s[0].isdigit():
        s = "n_" + s
    return s or "unnamed"


def write_trace(
    *,
    net_name: str,
    step_id: str,
    step_display_name: str,
    step_type: str,
    decision_path: str,
    inputs: dict,
    outputs: dict,
    llm_calls: list | None = None,
    details: dict | None = None,
    notes: str = "",
    duration_ms: int | None = None,
) -> None:
    """Append one trace record for the target net.  No-op when not tracing."""
    if _target_net is None:
        return

    # Always drain so calls from non-target nets (e.g. step_06 power inference)
    # don't accumulate and bleed into the next matching write_trace call.
    try:
        from llm import ollama_client
        drained = ollama_client.drain_llm_calls()
    except Exception:
        drained = []

    if net_name != _target_net and net_name != "_component_scope":
        return  # discard drained calls — they belong to a different net

    global _sequence
    _sequence += 1

    _records.append({
        "sequence":         _sequence,
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "net_name":         net_name,
        "step_id":          step_id,
        "step_display_name": step_display_name,
        "step_type":        step_type,
        "decision_path":    decision_path,
        "inputs":           inputs,
        "outputs":          outputs,
        "llm_calls":        (llm_calls if llm_calls is not None else drained),
        "details":          details,
        "notes":            notes,
        "duration_ms":      duration_ms,
    })


def flush(net_name: str) -> Path | None:
    """Write accumulated records to disk.  Returns the path written, or None."""
    if _target_net is None or not _records:
        return None
    assert _output_dir is not None
    trace_dir = _output_dir / "traces" / _sanitize(_run_label)
    trace_dir.mkdir(parents=True, exist_ok=True)
    out_path = trace_dir / (_sanitize(net_name) + ".json")
    out_path.write_text(json.dumps(_records, indent=2), encoding="utf-8")
    return out_path

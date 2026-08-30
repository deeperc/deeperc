import json
import logging
import os
import threading
import time
import urllib.request

logger = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "gemma3:4b"

# Set GEMMA_LOG_FILE to a .jsonl path to capture all Gemma calls (diagnostic only).
# Resolved to an absolute path once at import time so a later os.chdir() (e.g.
# run_checks's per-board chdir) can't break a relative path mid-run.
_raw_gemma_log_file = os.environ.get("GEMMA_LOG_FILE")
_GEMMA_LOG_FILE = os.path.abspath(_raw_gemma_log_file) if _raw_gemma_log_file else None

# Per-board context merged into every log record (set by the corpus runner so
# calls can be attributed to a netlist when all boards stream to one file).
_log_context: dict = {}

# Trace buffer: populated when tracing is enabled; drained by trace.writer.
_tracing_active: bool = False
_trace_buffer = threading.local()


def enable_tracing() -> None:
    global _tracing_active
    _tracing_active = True


def disable_tracing() -> None:
    global _tracing_active
    _tracing_active = False
    if hasattr(_trace_buffer, "calls"):
        _trace_buffer.calls.clear()


def drain_llm_calls() -> list:
    """Return and clear the thread-local LLM call buffer."""
    calls = getattr(_trace_buffer, "calls", [])
    result = list(calls)
    if hasattr(_trace_buffer, "calls"):
        _trace_buffer.calls.clear()
    return result


def set_log_context(**kw) -> None:
    """Replace the diagnostic log context (e.g. board=<rel path>). No-op on logging
    behaviour unless GEMMA_LOG_FILE is set."""
    _log_context.clear()
    _log_context.update(kw)


def _log_call(step_hint: str | None, prompt: str, response: str,
               temperature: float, latency_ms: int,
               tokens_in: int = 0, tokens_out: int = 0) -> None:
    if _tracing_active:
        if not hasattr(_trace_buffer, "calls"):
            _trace_buffer.calls = []
        _trace_buffer.calls.append({
            "model":       MODEL,
            "purpose":     step_hint or "unknown",
            "prompt":      prompt,
            "response":    response,
            "tokens_in":   tokens_in,
            "tokens_out":  tokens_out,
            "duration_ms": latency_ms,
            "cache_hit":   False,
        })
    if not _GEMMA_LOG_FILE:
        return
    record = {
        "ts": time.time(),
        "step_hint": step_hint,
        **_log_context,
        "prompt": prompt,
        "response": response,
        "temperature": temperature,
        "latency_ms": latency_ms,
    }
    # Isolated from the caller's network try/except: a log-write failure must
    # never fail the LLM call or be misreported as "Ollama unreachable".
    try:
        os.makedirs(os.path.dirname(_GEMMA_LOG_FILE), exist_ok=True)
        with open(_GEMMA_LOG_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as e:
        logger.warning("GEMMA_LOG_FILE write failed (%s): %s", _GEMMA_LOG_FILE, e)


def generate(prompt: str, temperature: float = 0.0,
             step_hint: str | None = None) -> str:
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature},
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        OLLAMA_URL,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        t0 = time.monotonic()
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read())
            response = result["response"].strip()
        latency_ms = int((time.monotonic() - t0) * 1000)
        tokens_in  = result.get("prompt_eval_count", 0)
        tokens_out = result.get("eval_count", 0)
    except Exception as e:
        raise RuntimeError(f"Ollama unreachable: {e}")

    _log_call(step_hint, prompt, response, temperature, latency_ms,
              tokens_in=tokens_in, tokens_out=tokens_out)
    return response


def generate_json(prompt: str, retries: int = 1,
                  step_hint: str | None = None) -> dict | None:
    for attempt in range(retries + 1):
        temp = 0.0 if attempt == 0 else 0.2
        raw = generate(prompt, temperature=temp, step_hint=step_hint)
        clean = (
            raw.strip()
            .removeprefix("```json")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
        try:
            return json.loads(clean)
        except json.JSONDecodeError:
            continue
    return None

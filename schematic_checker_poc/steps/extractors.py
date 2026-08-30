"""Pluggable extraction-stage interface — input-path + model as ONE unit.

The swappable unit is the *whole* pin_groups extraction stage: how a datasheet PDF
becomes model input (MinerU-patched markdown | pdfplumber-full | a future VLM) AND
which model reads it. The eval (`investigation/headless_eval/eval_report.md`) showed
input-path and model co-vary, so they are registered together as one `ExtractorImpl`.

Impls register by name; `run_extractor()` is the shared guard + provenance + cache tail
that is identical for every impl. The default impl is config-driven
(`SCHECKER_EXTRACTOR` env, else `DEFAULT_EXTRACTOR`), so promoting a future impl is a
config change. A failing or unavailable impl yields `None` → UNRESOLVABLE; it NEVER
silently downgrades to another impl.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Default extraction impl. Config-driven (also `SCHECKER_EXTRACTOR` env) so
# promoting a future impl is a one-line/env change. Default = haiku_pdfplumber
# (the eval-validated Haiku-over-pdfplumber-full stage); gemma_mineru stays
# registered but dormant. Because extraction is cache-gated, flipping the default
# is verdict-neutral until a part actually re-extracts (cache-miss / invalidated).
DEFAULT_EXTRACTOR = "haiku_pdfplumber"


@dataclass
class PartContext:
    """Everything an impl needs to extract one part, resolver-agnostic."""
    part_number: str
    pdf_path: str
    markdown_text: str | None = None  # resolver-parsed .md (the gemma_mineru input)
    # Set BY the impl inside build_model_input: the full PRE-slice document text
    # this extraction read. Exists so the doc-identity evidence pass (TODO-337)
    # tests the whole document, never a section slice — a slice would false-MISS
    # any part whose name appears only outside the selected windows. Left None by
    # an impl that has no such text; the verdict is then `unverifiable`, never a
    # guessed one.
    source_text: str | None = None


class ExtractorImpl:
    """One extraction stage = build_model_input (input-path) + extract (model)."""
    name: str = "base"
    input_path: str = "unknown"        # recorded in provenance
    extractor_tag: str = "unknown"     # extraction_meta.extractor (producer identity)
    model: str | None = None

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str | None:
        """Human-readable cause for the last `available()` == False, or None
        when the impl has no cause detail to add (e.g. it is unconditionally
        available)."""
        return None

    def build_model_input(self, ctx: PartContext):
        raise NotImplementedError

    def extract(self, model_input, ctx: PartContext) -> dict | None:
        raise NotImplementedError


# ── registry ─────────────────────────────────────────────────────────────────
_REGISTRY: dict[str, ExtractorImpl] = {}


def register(impl: ExtractorImpl) -> ExtractorImpl:
    _REGISTRY[impl.name] = impl
    return impl


def get_extractor(name: str | None = None) -> ExtractorImpl:
    name = name or os.environ.get("SCHECKER_EXTRACTOR") or DEFAULT_EXTRACTOR
    if name not in _REGISTRY:
        raise KeyError(
            f"unknown extractor {name!r}; registered: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


def registered() -> list[str]:
    return sorted(_REGISTRY)


# ── shared tail — identical for every impl ───────────────────────────────────
def _advisory_guard(raw: dict, ctx: PartContext) -> None:
    """Run the deterministic supply-plausibility gate over supply groups and LOG
    flags — NON-DESTRUCTIVE. `validate_supply_group` still gates per-pin at
    use-time in step_08b; this earlier pass is advisory so the wiring stays
    verdict-neutral (it does not drop groups or change the cached content)."""
    try:
        from steps.step_05_validator import validate_supply_group
    except Exception:
        return
    for g in raw.get("pin_groups", []):
        is_supply = (g.get("pin_type") == "power"
                     or g.get("supply_min") is not None
                     or g.get("supply_max") is not None)
        if not is_supply:
            continue
        try:
            v = validate_supply_group(g)
        except Exception:
            continue
        if not v.ok:
            logger.info(
                "[extract] advisory guard flag %s / %s: %s "
                "(non-destructive; step_08b gates at use-time)",
                ctx.part_number, g.get("supply_rail_name") or g.get("group_name"),
                v.reason,
            )


def _doc_identity_verdict(ctx: PartContext, model_input) -> dict | None:
    """Doc-identity evidence for this write (TODO-337 Phase 2b) — NEVER a gate.

    Tests the cache stem this write will land under against the PRE-slice document
    text the impl recorded on `ctx.source_text`. Falls back to `model_input` only
    when it is a string AND the impl recorded no separate pre-slice text (i.e. the
    two are the same object anyway); an impl that sliced without recording gets
    `unverifiable` rather than a slice-based false MISS.

    Returns the persisted dict, or None if the evidence pass itself failed — a
    failure here must never block a cache write.
    """
    try:
        from steps import doc_identity, step_03_resolver
        text = ctx.source_text
        if text is None and isinstance(model_input, str):
            text = model_input
        stem = step_03_resolver.canonical_pdf_stem(ctx.pdf_path)
        di = doc_identity.classify(stem, text)
        logger.info("[extract] doc-identity %s: %s (tier=%s depth=%s ratio=%s)",
                    stem, di.doc_class, di.tier, di.depth, di.ratio)
        return di.to_provenance()
    except Exception as e:  # evidence is advisory — never break the write path
        logger.warning("[extract] doc-identity pass failed for %s: %s",
                       ctx.part_number, e)
        return None


# LT-31: once-per-run unified unavailability notice — mirrors step_03_resolver's
# _L2_DISABLED_LOGGED/_log_l2_disabled_once (LT-23/LT-29). A cache-miss-heavy run
# would otherwise repeat the same cause once per part; the cause doesn't change
# mid-run, so it is worth logging at WARNING only the first time.
_EXTRACTOR_UNAVAILABLE_LOGGED = False


def _log_extractor_unavailable_once(impl_name: str, reason: str | None) -> None:
    global _EXTRACTOR_UNAVAILABLE_LOGGED
    if _EXTRACTOR_UNAVAILABLE_LOGGED:
        return
    if reason:
        logger.warning("[extract] extractor %s unavailable: %s → UNRESOLVABLE",
                       impl_name, reason)
    else:
        logger.warning("[extract] extractor %s unavailable → UNRESOLVABLE", impl_name)
    _EXTRACTOR_UNAVAILABLE_LOGGED = True


def run_extractor(impl: ExtractorImpl, ctx: PartContext, *,
                  dry_run_to: str | None = None) -> dict | None:
    """Shared extract → advisory-guard → provenance-stamped cache-write tail.

    Returns the pin_groups dict, or None (→ UNRESOLVABLE) if the impl is
    unavailable, errors, or produces no usable groups. NEVER downgrades to
    another impl. When `dry_run_to` is set, writes the result there (scratch)
    instead of the real cache — used by the opt-in smoke path.
    """
    if not impl.available():
        reason = impl.unavailable_reason()
        logger.debug("[extract] impl %s unavailable for %s%s",
                     impl.name, ctx.part_number, f": {reason}" if reason else "")
        _log_extractor_unavailable_once(impl.name, reason)
        return None
    try:
        model_input = impl.build_model_input(ctx)
    except Exception as e:  # input-path failure → UNRESOLVABLE, never crash
        logger.warning("[extract] %s build_model_input failed for %s: %s",
                       impl.name, ctx.part_number, e)
        return None
    try:
        raw = impl.extract(model_input, ctx)
    except Exception as e:
        logger.warning("[extract] %s extract failed for %s: %s",
                       impl.name, ctx.part_number, e)
        return None
    if not raw or not raw.get("pin_groups"):
        return None  # → UNRESOLVABLE (matches current "if pin_groups_result:" gate)

    _advisory_guard(raw, ctx)

    from steps import step_03_resolver
    di = _doc_identity_verdict(ctx, model_input)
    # Same faithful cache-shaped write for both paths — dest_path routes the
    # dry-run/smoke preview to scratch instead of the real datasheets_parsed cache.
    step_03_resolver.save_pin_groups_cache(
        ctx.pdf_path, raw,
        extractor=impl.extractor_tag, model=impl.model,
        extractor_impl=impl.name, input_path=impl.input_path,
        doc_identity=di,
        dest_path=dry_run_to,
    )
    if dry_run_to is not None:
        logger.info("[extract] dry-run wrote %s (scratch; real cache untouched)",
                    dry_run_to)
    return raw


# ── impl: gemma_mineru (the existing path, wrapped) ──────────────────────────
class GemmaMineruExtractor(ExtractorImpl):
    """The existing MinerU/pdfplumber-patched markdown + Gemma path, behind the
    interface. Input = the resolver-parsed `.md` already on `ctx.markdown_text`
    (no re-parse — preserves current behaviour exactly). Dormant-capable: it is
    a normal registered impl, selected only when it is the configured default."""
    name = "gemma_mineru"
    input_path = "mineru_pdfplumber_patched"
    extractor_tag = "gemma"

    @property
    def model(self):  # lazy — avoid importing ollama model string at import time
        try:
            from steps.step_03_resolver import OLLAMA_MODEL
            return OLLAMA_MODEL
        except Exception:
            return None

    def build_model_input(self, ctx: PartContext) -> str:
        # The resolver already produced the .md (MinerU-or-pdfplumber, patched).
        text = ctx.markdown_text or ""
        ctx.source_text = text  # no slicing on this path — input IS the full text
        return text

    def extract(self, model_input: str, ctx: PartContext) -> dict | None:
        from steps import step_04a_locator, step_04b_extractor
        try:
            sections = step_04a_locator.locate_sections(ctx.part_number, model_input)
        except Exception:
            sections = {"pin_table": None, "abs_max": None, "elec_chars": None}
        return step_04b_extractor.extract_part_pin_groups(
            part_number=ctx.part_number,
            sections=sections,
            markdown_text=model_input,
        )


register(GemmaMineruExtractor())


# ── shared defensive JSON parse (used by model-returning impls) ───────────────
def parse_model_json(text: str) -> dict:
    """Extract the JSON object from a model reply (tolerate ```json fences /
    preamble). Raises ValueError if no object is present."""
    t = (text or "").strip()
    if "```" in t:
        for seg in t.split("```"):
            seg = seg.lstrip()
            if seg.startswith("json"):
                seg = seg[4:]
            seg = seg.strip()
            if seg.startswith("{"):
                t = seg
                break
    i, j = t.find("{"), t.rfind("}")
    if i == -1 or j == -1:
        raise ValueError("no JSON object in model reply")
    return json.loads(t[i:j + 1])


# ── impl: haiku_pdfplumber (registered, NOT default) ─────────────────────────
def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ── big-datasheet composite section-slicer (Phase B) ─────────────────────────
# The supply spec in a big MCU datasheet is SPLIT: the operating-voltage *range* is
# a front-page features bullet ("2.7V to 5.5V"), while abs-max is deep in the doc.
# So the slice is composite: front-matter + abs-max window + DC/electrical windows.
# Deliberately NOT "Recommended Operating Conditions" — for atmega that heading matches
# the clock-prescaler prose ("CKDIV8 fuse…"), for RP2040 the regulator prose ("set the
# regulator to 1.10V"); neither is the supply table. Multi-hit headings (TOC + prose +
# real table) are disambiguated by DIGIT DENSITY of the following window — the real
# table is digit-dense, TOC/prose hits are sparse.
SLICE_THRESHOLD_CHARS = 390_000   # ~150K est-tokens; big-doc-only trigger
_FRONT_CHARS = 8_000
_WINDOW_CHARS = 5_000
_DENSITY_PROBE = 3_500

_ABS_MAX_RE = re.compile(r"absolute\s+maximum\s+ratings?", re.I)
_DC_CHAR_RE = re.compile(
    r"(dc\s+characteristics|dc\s+electrical\s+characteristics|electrical\s+characteristics)",
    re.I)
# Supply-rail names — anchor windows on the digit-dense OPERATING tables where the
# supply min/max/nominal range actually lives (heading-based slicing misses it: the
# RP2040 IOVDD 1.8–3.3V operating table is @1.298M under an IO-timing heading, not
# under "Absolute Maximum Ratings"). Density selection picks the table rows over the
# many prose/pin-list mentions.
_SUPPLY_NAME_RE = re.compile(r"\b(IOVDD|DVDD|AVDD|VDDA|VDD|AVCC|VCCA|VCC|USB_?VDD)\b")
_SUPPLY_WINDOW_CHARS = 3_000
# A supply OPERATING window is one rich in voltage-range tokens ("1.8V to 3.3V",
# "1.62 1.8 / 3.3 3.63", "nominal voltage") — NOT merely digit-dense (IO-delay tables
# are dense but carry no supply range). Score supply windows by range-token count so
# the real operating table wins over timing/delay tables.
_VRANGE_RE = re.compile(
    r"\d\.\d+\s*(?:V\b|to\b|[–\-/]\s*)\s*\d\.?\d*|nominal\s+voltage", re.I)


def _supply_windows(text: str, n: int = 3, window: int = _SUPPLY_WINDOW_CHARS) -> list[str]:
    """Windows around supply-rail names, ranked by voltage-RANGE density (then digit
    density), so the operating min/max table wins over dense delay/timing tables."""
    hits = list(_SUPPLY_NAME_RE.finditer(text))
    if not hits:
        return []

    def score(m):
        w = text[max(0, m.start() - 200): m.start() + window]
        return (len(_VRANGE_RE.findall(w)), _digit_density(w))

    ranked = sorted(hits, key=score, reverse=True)
    out: list[str] = []
    starts: list[int] = []
    for m in ranked:
        if len(out) >= n:
            break
        if score(m)[0] == 0:      # no voltage range here — not a supply table
            continue
        s = max(0, m.start() - 200)
        if any(abs(s - u) < window for u in starts):
            continue
        out.append(text[s: s + window])
        starts.append(s)
    return out


def _digit_density(s: str) -> int:
    return sum(c.isdigit() for c in s)


def _best_windows(text: str, rx: "re.Pattern", n: int = 1,
                  window: int = _WINDOW_CHARS) -> list[str]:
    """Up to n windows around the most digit-dense (real-table) matches of rx,
    skipping windows that overlap an already-selected one."""
    hits = list(rx.finditer(text))
    if not hits:
        return []
    ranked = sorted(hits, key=lambda m: _digit_density(text[m.start():m.start() + _DENSITY_PROBE]),
                    reverse=True)
    out: list[str] = []
    starts: list[int] = []
    for m in ranked:
        if len(out) >= n:
            break
        s = m.start()
        if any(abs(s - u) < window for u in starts):
            continue
        out.append(text[s:s + window])
        starts.append(s)
    return out


def section_slice(text: str) -> str:
    """Composite deterministic slice for a big datasheet — front-matter (operating-
    voltage features) + abs-max window + up to 2 DC/electrical windows, each selected
    by digit density. Target ~8-10K tokens, well under context."""
    parts = [f"<!-- section-slice: front-matter -->\n{text[:_FRONT_CHARS]}"]
    for label, wins in (
        ("absolute-maximum-ratings", _best_windows(text, _ABS_MAX_RE, n=1)),
        ("dc-electrical-characteristics", _best_windows(text, _DC_CHAR_RE, n=2)),
        ("supply-rail-tables", _supply_windows(text, n=3)),
    ):
        for i, w in enumerate(wins, 1):
            parts.append(f"<!-- section-slice: {label} #{i} -->\n{w}")
    return "\n\n".join(parts)


class HaikuPdfplumberExtractor(ExtractorImpl):
    """Claude Haiku 4.5 over pdfplumber-full input — the config the quality eval
    (`investigation/headless_eval/eval_report.md`) proved clears every hard case
    (wrong-rail, empty, multi-rail, imaged) the Gemma+MinerU path fails.

    Registered but NOT selected unless it is the configured default. The API key
    is resolved .env-first (via dotenv_values(), which does NOT populate
    os.environ — never export the key globally: it would hijack the CC
    subscription), then falls back to the ANTHROPIC_API_KEY env var (LT-31: the
    .env-only original silently returned None whenever python-dotenv wasn't
    installed, even with a valid env var set — A4-1 in
    investigation/recon_reports/extractor_public_scoping_recon.md). Absent key
    → available() is False → run_extractor yields UNRESOLVABLE, never a crash."""
    name = "haiku_pdfplumber"
    input_path = "pdfplumber_full"
    extractor_tag = "haiku_pdfplumber"
    model = "claude-haiku-4-5-20251001"
    _MAX_TOKENS = 8192  # eval lesson: 4096 truncated verbose multi-pin datasheets

    _UNAVAILABLE_MESSAGES = {
        "C1": "python-dotenv not installed — .env cannot be read",
        "C2": "no .env found and ANTHROPIC_API_KEY not set",
        "C3": ".env found but contains no ANTHROPIC_API_KEY",
    }

    def __init__(self):
        self._last_key_cause: str | None = None

    def _api_key(self) -> str | None:
        """Resolve the key: .env (via python-dotenv) first, ANTHROPIC_API_KEY
        env var as fallback. On failure, records the cause on
        self._last_key_cause (see _UNAVAILABLE_MESSAGES) for unavailable_reason()."""
        self._last_key_cause = None
        envfile = _repo_root() / ".env"
        dotenv_importable = True
        try:
            from dotenv import dotenv_values
        except Exception:
            dotenv_importable = False
        else:
            if envfile.exists():
                vals = dotenv_values(envfile) or {}
                dotenv_key = vals.get("ANTHROPIC_API_KEY") or None
                if dotenv_key:
                    return dotenv_key

        env_key = os.environ.get("ANTHROPIC_API_KEY") or None
        if env_key:
            return env_key

        if not dotenv_importable and envfile.exists():
            self._last_key_cause = "C1"
        elif not envfile.exists():
            self._last_key_cause = "C2"
        else:
            self._last_key_cause = "C3"
        return None

    def available(self) -> bool:
        return bool(self._api_key())

    def unavailable_reason(self) -> str | None:
        return self._UNAVAILABLE_MESSAGES.get(self._last_key_cause)

    def build_model_input(self, ctx: PartContext) -> str:
        # pdfplumber-full over the whole PDF (no page cap) — reuse the shared
        # function so the input-path matches exactly what the eval validated.
        from steps.extraction_input import extract_pdfplumber_full
        text = extract_pdfplumber_full(Path(ctx.pdf_path))
        # Record the PRE-slice text for the doc-identity evidence pass (TODO-337)
        # BEFORE the big-doc branch below can replace it with a section slice.
        ctx.source_text = text
        # Big-doc path (Phase B): a datasheet whose pdfplumber-full exceeds the context
        # margin can't be fed whole → deterministic composite section-slice. Self-
        # selecting by size, so normal parts feed whole and never regress.
        if len(text) > SLICE_THRESHOLD_CHARS:
            sliced = section_slice(text)
            logger.info("[extract] %s: big doc %d chars → section-slice %d chars",
                        ctx.part_number, len(text), len(sliced))
            return sliced
        return text

    def _prompt(self, ctx: PartContext, model_input: str) -> str:
        from steps.extraction_input import EXTRACTION_PROMPT_TEMPLATE
        return EXTRACTION_PROMPT_TEMPLATE.format(
            part_number=ctx.part_number,
            output_path="(return the JSON in your reply)",
            markdown_content=model_input,
            source_md=f"{Path(ctx.pdf_path).stem}.md",
        )

    def extract(self, model_input: str, ctx: PartContext) -> dict | None:
        key = self._api_key()
        if not key:
            return None  # → UNRESOLVABLE
        import anthropic
        client = anthropic.Anthropic(api_key=key)  # explicit key; env untouched
        resp = client.messages.create(
            model=self.model, max_tokens=self._MAX_TOKENS, temperature=0,
            messages=[{"role": "user", "content": self._prompt(ctx, model_input)}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            parsed = parse_model_json(text)
        except ValueError:
            logger.warning("[extract] haiku unparseable JSON for %s", ctx.part_number)
            return None
        return {"pin_groups": parsed.get("pin_groups", []),
                "default_group": parsed.get("default_group")}


register(HaikuPdfplumberExtractor())


def batch_extract_parts(ctxs: list[PartContext], *,
                        impl_name: str = "haiku_pdfplumber") -> dict[str, dict]:
    """Batch-API bulk extraction for the SEPARATE corpus re-extraction cycle
    (−50% cost, ≤24h async). NOT used by the per-part pipeline path (which
    extracts lazily/sync via run_extractor). Submits one Message Batch for all
    ctxs, polls to completion, returns {part_number: raw pin_groups}. Provided so
    the follow-on Haiku re-extraction cycle has a ready bulk driver; exercised
    by the closed Phase A/B re-extraction cycles (done_todos_index ID 150);
    current callers are investigation/phaseB_driver.py and
    investigation/reextract_driver.py."""
    impl = get_extractor(impl_name)
    if not impl.available():
        raise RuntimeError(f"{impl_name} unavailable (no API key)")
    import anthropic
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request
    key = impl._api_key()  # type: ignore[attr-defined]
    client = anthropic.Anthropic(api_key=key)
    by_id: dict[str, PartContext] = {}
    reqs = []
    for i, ctx in enumerate(ctxs):
        cid = f"part-{i}"
        by_id[cid] = ctx
        prompt = impl._prompt(ctx, impl.build_model_input(ctx))  # type: ignore[attr-defined]
        reqs.append(Request(custom_id=cid, params=MessageCreateParamsNonStreaming(
            model=impl.model, max_tokens=impl._MAX_TOKENS, temperature=0,
            messages=[{"role": "user", "content": prompt}])))
    batch = client.messages.batches.create(requests=reqs)
    import time
    while client.messages.batches.retrieve(batch.id).processing_status != "ended":
        time.sleep(30)
    out: dict[str, dict] = {}
    for res in client.messages.batches.results(batch.id):
        if res.result.type != "succeeded":
            continue
        text = next((b.text for b in res.result.message.content if b.type == "text"), "")
        try:
            parsed = parse_model_json(text)
        except ValueError:
            continue
        out[by_id[res.custom_id].part_number] = {
            "pin_groups": parsed.get("pin_groups", []),
            "default_group": parsed.get("default_group")}
    return out

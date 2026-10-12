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
    # TODO-401 observed provenance, set BY the impl the same way source_text is.
    # Stamped beside the extractor fingerprint, never inside its hash.
    # input_regime: the branch build_model_input took ("whole" | "section_slice").
    # request_sha256: sha256 of the exact request body string sent (TODO-351).
    # service_tier: off the response usage when the API reports one.
    input_regime: str | None = None
    request_sha256: str | None = None
    service_tier: str | None = None


class ExtractorImpl:
    """One extraction stage = build_model_input (input-path) + extract (model)."""
    name: str = "base"
    input_path: str = "unknown"        # recorded in provenance
    extractor_tag: str = "unknown"     # extraction_meta.extractor (producer identity)
    model: str | None = None
    # True for an impl that has an extractor_fingerprint() regime (TODO-401).
    # False = nothing new stamped; the field stays absent (reads as legacy).
    stamps_fingerprint: bool = False

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

    from steps import extraction_validator, step_03_resolver
    # TODO-509 fork (d): citations + hard rules + coercions + NA/rating/504 filters on
    # the draw, before anything reads it; the report rides in provenance.validation.
    raw, vreport = extraction_validator.validate(
        raw, model_input=model_input if isinstance(model_input, str) else "",
        source_text=ctx.source_text,
        stem=step_03_resolver.canonical_pdf_stem(ctx.pdf_path))

    _advisory_guard(raw, ctx)

    di = _doc_identity_verdict(ctx, model_input)
    # TODO-401: the sync path is today's single draw.
    stamp = {}
    if impl.stamps_fingerprint:
        stamp = dict(
            extractor_fingerprint=extractor_fingerprint(impl, draws=1, aggregate="single"),
            pdfplumber_version=pdfplumber_version(),
            service_tier=ctx.service_tier,
            request_sha256=ctx.request_sha256,
            input_regime=ctx.input_regime,
        )
    # Same faithful cache-shaped write for both paths — dest_path routes the
    # dry-run/smoke preview to scratch instead of the real datasheets_parsed cache.
    step_03_resolver.save_pin_groups_cache(
        ctx.pdf_path, raw,
        extractor=impl.extractor_tag, model=impl.model,
        extractor_impl=impl.name, input_path=impl.input_path,
        doc_identity=di,
        dest_path=dry_run_to,
        validation=vreport,
        **stamp,
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
    # eval lesson: 4096 truncated verbose multi-pin datasheets; TODO-509 fork (d): the quotes
    # block raised pilot 3 output to 8912 tokens on one cell, past 8192 -> 16384. A reply that
    # still stops on max_tokens is truncated JSON: UNRESOLVABLE (sync) / skipped (batch).
    _MAX_TOKENS = 16384
    _TEMPERATURE = 0    # sent on every call (sync + batch); fingerprinted (TODO-401)
    stamps_fingerprint = True

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
            ctx.input_regime = "section_slice"
            return sliced
        ctx.input_regime = "whole"
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
        prompt = self._prompt(ctx, model_input)
        ctx.request_sha256 = request_sha256(prompt)
        resp = client.messages.create(
            model=self.model, max_tokens=self._MAX_TOKENS, temperature=self._TEMPERATURE,
            messages=[{"role": "user", "content": prompt}],
        )
        ctx.service_tier = _service_tier(resp)
        if getattr(resp, "stop_reason", None) == "max_tokens":
            logger.warning("[extract] truncated reply for %s (max_tokens)", ctx.part_number)
            return None  # → UNRESOLVABLE
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            parsed = parse_model_json(text)
        except ValueError:
            logger.warning("[extract] haiku unparseable JSON for %s", ctx.part_number)
            return None
        return {"pin_groups": parsed.get("pin_groups", []),
                "default_group": parsed.get("default_group")}


register(HaikuPdfplumberExtractor())


# ── extractor fingerprint (TODO-401; TODO-351 folded) ────────────────────────
_SLICE_INT_CONSTANTS = ("SLICE_THRESHOLD_CHARS", "_FRONT_CHARS", "_WINDOW_CHARS",
                        "_DENSITY_PROBE", "_SUPPLY_WINDOW_CHARS")
_SLICE_REGEXES = ("_ABS_MAX_RE", "_DC_CHAR_RE", "_SUPPLY_NAME_RE", "_VRANGE_RE")


def extractor_fingerprint(impl: ExtractorImpl, *, draws: int, aggregate: str) -> dict:
    """Hashed identity of the haiku_pdfplumber extraction regime.

    Returns {"fields": {...}, "sha256": <sha256 of the canonical fields JSON>}.
    Fields come from repo-shipped constants and the call config this module
    actually builds, never from the local environment (TODO-397 stranger
    constraint): model, temperature_sent, thinking, max_tokens, template_sha256,
    the section-slice config (five size constants + pattern/flags of all four
    slice regexes), input_path, glue_version (the subscript-glue revision of the
    pdfplumber input path, extraction_input.GLUE_VERSION; TODO-534), the regime
    (draws, aggregate), and the post-extraction validator's identity (validator_version, validator_sha256 =
    sha256 of steps/extraction_validator.py at import; TODO-509 fork (d)).

    Equal fingerprints mean "produced under the same NAMED regime", NOT "same
    bytes out": Haiku 4.5 at temperature 0 varied on 11.6% of slots across
    repeat draws (TODO-486). Observed values (pdfplumber version, service tier,
    request sha, input regime) are stamped beside this dict, never inside it.
    """
    if impl.name != "haiku_pdfplumber":
        raise ValueError(f"no fingerprint regime for extractor {impl.name!r}")
    import hashlib
    from steps import extraction_validator
    from steps.extraction_input import EXTRACTION_PROMPT_TEMPLATE, GLUE_VERSION
    g = globals()
    slice_cfg = {k: g[k] for k in _SLICE_INT_CONSTANTS}
    slice_cfg.update({k: {"pattern": g[k].pattern, "flags": int(g[k].flags)}
                      for k in _SLICE_REGEXES})
    temperature = getattr(impl, "_TEMPERATURE", None)
    fields = {
        "model": impl.model,
        "temperature_sent": "NONE" if temperature is None else temperature,
        "thinking": "NONE",  # no thinking parameter is sent on either path
        "max_tokens": impl._MAX_TOKENS,  # type: ignore[attr-defined]
        "template_sha256": hashlib.sha256(EXTRACTION_PROMPT_TEMPLATE.encode()).hexdigest(),
        "slice": slice_cfg,
        "input_path": impl.input_path,
        "glue_version": GLUE_VERSION,
        "draws": draws,
        "aggregate": aggregate,
        "validator_version": extraction_validator.VALIDATOR_VERSION,
        "validator_sha256": extraction_validator.VALIDATOR_SHA256,
    }
    canon = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return {"fields": fields, "sha256": hashlib.sha256(canon.encode()).hexdigest()}


def request_sha256(prompt: str) -> str:
    """sha256 of the exact request body string (what _prompt() returns) — the
    TODO-351/354 corrected target, not build_model_input's return value."""
    import hashlib
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def pdfplumber_version() -> str | None:
    """Observed, not hashed: the installed pdfplumber version, None on failure."""
    try:
        from importlib.metadata import version
        return version("pdfplumber")
    except Exception:
        return None


def _service_tier(resp) -> str | None:
    tier = getattr(getattr(resp, "usage", None), "service_tier", None)
    return tier if isinstance(tier, str) else None




# ── batch path (TODO-401 fingerprint; TODO-531 draws / persisted results / resume) ──
# TODO-531 ruling Y7: a multi-draw batch is resumable. custom ids are index-based (never built from a stem:
# stems carry spaces and dots) and must match the Batch API's id rule; the installed SDK does not encode it,
# so the documented ^[a-zA-Z0-9_-]{1,64}$ is checked here.
_CUSTOM_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_POLL_INTERVAL_S = 30
BATCH_MANIFEST_SCHEMA = "todo531-batch-draws/1"


class BatchPending(Exception):
    """The batch had not ended when poll_deadline_s elapsed. Nothing is lost: with a results_dir the manifest
    holds the batch id, and the same call again resumes polling (it never creates a second batch)."""

    def __init__(self, batch_id: str, results_dir: str | None = None):
        super().__init__(f"batch {batch_id} has not ended (poll deadline)"
                         + (f"; resume from {results_dir}" if results_dir else ""))
        self.batch_id = batch_id
        self.results_dir = results_dir


def _batch_client(impl: ExtractorImpl, client):
    """An injected client (tests) is used as is; None -> today's construction (key resolved .env-first)."""
    if client is not None:
        return client
    if not impl.available():
        raise RuntimeError(f"{impl.name} unavailable (no API key)")
    import anthropic
    key = impl._api_key()  # type: ignore[attr-defined]
    return anthropic.Anthropic(api_key=key)


def _poll_batch(client, batch_id: str, poll_deadline_s: float | None, results_dir=None) -> None:
    import time
    start = time.monotonic()
    while client.messages.batches.retrieve(batch_id).processing_status != "ended":
        if poll_deadline_s is not None and time.monotonic() - start >= poll_deadline_s:
            raise BatchPending(batch_id, str(results_dir) if results_dir is not None else None)
        time.sleep(_POLL_INTERVAL_S)


def batch_extract_parts(ctxs: list[PartContext], *,
                        impl_name: str = "haiku_pdfplumber",
                        draws: int = 1, results_dir: str | Path | None = None,
                        poll_deadline_s: float | None = None,
                        client=None) -> dict[str, dict]:
    """Batch-API bulk extraction for the SEPARATE corpus re-extraction cycle
    (−50% cost, ≤24h async). NOT used by the per-part pipeline path (which
    extracts lazily/sync via run_extractor). Submits one Message Batch for all
    ctxs, polls to completion, returns {part_number: raw pin_groups}. Provided so
    the follow-on Haiku re-extraction cycle has a ready bulk driver; exercised
    by the closed Phase A/B re-extraction cycles (done_todos_index ID 150);
    current callers are investigation/phaseB_driver.py and
    investigation/reextract_driver.py.

    TODO-401: each per-part dict also carries `extractor_fingerprint` (today's
    single draw: draws=1, aggregate="single"), `request_sha256` and
    `input_regime`. Writes nothing — the caller owns any cache write.

    TODO-509 fork (d): each per-part dict also carries `model_input` (the exact
    text the prompt inlined) so TODO-531 can run extraction_validator post-vote.
    A reply that stopped on max_tokens is skipped and its part number listed
    under out["__truncated__"] (always present, possibly empty). The return
    stays a dict rather than a tuple: both callers (phaseB_driver.py,
    reextract_driver.py) read it with results.get(part_number).

    TODO-531 (rulings Y7, P2). Keyword-only, all defaulted: a call without them is
    exactly the above (same requests, same return).
      client           an Anthropic-compatible client (tests); None -> today's
                       construction. An injected client skips the key lookup.
      poll_deadline_s  poll (30 s sleep) until ended or this many seconds have
                       passed -> BatchPending(batch_id); None = no deadline.
      results_dir      persisted, resumable run (required when draws > 1). Writes
                       nothing outside it: manifest.json (per custom id the stem,
                       part_number, draw, request_sha256, model-input sha256,
                       input_regime; the draws=1/"single" fingerprint; model,
                       max_tokens, temperature) and inputs/<index>.txt (plus
                       inputs/<index>.source.txt, the pre-slice text, when it
                       differs) BEFORE the batch is created; the batch id after
                       create and before the first poll (atomic replace);
                       raw/<custom_id>.json for EVERY result row before any
                       parse. A manifest that already holds a batch id is a
                       RESUME: no create; the rebuilt request_sha256 per custom
                       id must equal the manifest's (ValueError naming the first
                       difference). A manifest without a batch id raises (a
                       previous create may have gone through): no code path
                       submits twice into one results_dir.
      draws            requests per ctx (custom id s<index:04d>-d<draw>); all
                       draws of a ctx share one prompt.
    With results_dir the return is {"draws": {stem: {draw: per-draw dict | None}},
    "__truncated__": [[stem, draw]], "__failed__": [[stem, draw, reason]],
    "batch_id", "usage" (summed)}; stem = step_03_resolver.canonical_pdf_stem
    (duplicate stems -> ValueError). A per-draw dict is the per-part dict above
    plus "usage" and "stop_reason"; a truncated / non-succeeded / unparseable /
    missing draw is None and listed. Nothing is silently dropped."""
    if not isinstance(draws, int) or isinstance(draws, bool) or draws < 1:
        raise ValueError(f"draws must be a positive int, got {draws!r}")
    if draws > 1 and results_dir is None:
        raise ValueError("draws > 1 requires results_dir (a multi-draw batch must be persisted and resumable)")
    impl = get_extractor(impl_name)
    if results_dir is not None:
        return _batch_draws(impl, ctxs, draws=draws, results_dir=Path(results_dir),
                            poll_deadline_s=poll_deadline_s, client=client)
    client = _batch_client(impl, client)
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request
    fingerprint = extractor_fingerprint(impl, draws=1, aggregate="single")
    by_id: dict[str, PartContext] = {}
    inputs: dict[str, str] = {}
    reqs = []
    for i, ctx in enumerate(ctxs):
        cid = f"part-{i}"
        by_id[cid] = ctx
        inputs[cid] = impl.build_model_input(ctx)
        prompt = impl._prompt(ctx, inputs[cid])  # type: ignore[attr-defined]
        ctx.request_sha256 = request_sha256(prompt)
        reqs.append(Request(custom_id=cid, params=MessageCreateParamsNonStreaming(
            model=impl.model, max_tokens=impl._MAX_TOKENS,  # type: ignore[attr-defined]
            temperature=impl._TEMPERATURE,  # type: ignore[attr-defined]
            messages=[{"role": "user", "content": prompt}])))
    batch = client.messages.batches.create(requests=reqs)
    _poll_batch(client, batch.id, poll_deadline_s)
    out: dict = {}
    truncated: list[str] = []
    for res in client.messages.batches.results(batch.id):
        if res.result.type != "succeeded":
            continue
        if getattr(res.result.message, "stop_reason", None) == "max_tokens":
            part = by_id[res.custom_id].part_number
            logger.warning("[extract] truncated reply for %s (max_tokens)", part)
            truncated.append(part)
            continue
        text = next((b.text for b in res.result.message.content if b.type == "text"), "")
        try:
            parsed = parse_model_json(text)
        except ValueError:
            continue
        ctx = by_id[res.custom_id]
        out[ctx.part_number] = {
            "pin_groups": parsed.get("pin_groups", []),
            "default_group": parsed.get("default_group"),
            "extractor_fingerprint": fingerprint,
            "request_sha256": ctx.request_sha256,
            "input_regime": ctx.input_regime,
            "model_input": inputs[res.custom_id]}
    out["__truncated__"] = truncated
    return out


def _sha256_text(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _jsonable(x):
    """SDK objects (pydantic model_dump), SimpleNamespace fakes, dicts and lists -> plain JSON values."""
    if x is None or isinstance(x, (str, int, float, bool)):
        return x
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    dump = getattr(x, "model_dump", None)
    if callable(dump):
        try:
            return _jsonable(dump(mode="json"))
        except TypeError:
            return _jsonable(dump())
    if hasattr(x, "__dict__"):
        return _jsonable(vars(x))
    return str(x)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_json(path: Path, obj) -> None:
    _atomic_write(path, json.dumps(obj, indent=1, sort_keys=True))


def _row_record(res) -> dict:
    """One batch result row, persisted before any parsing."""
    r = res.result
    rec = {"custom_id": res.custom_id, "result_type": getattr(r, "type", None),
           "stop_reason": None, "usage": None, "model": None, "content": None, "text": None}
    if rec["result_type"] == "succeeded":
        m = r.message
        blocks = list(getattr(m, "content", None) or [])
        rec["stop_reason"] = getattr(m, "stop_reason", None)
        rec["usage"] = _jsonable(getattr(m, "usage", None))
        rec["model"] = getattr(m, "model", None)
        rec["content"] = [{"type": getattr(b, "type", None), "text": getattr(b, "text", None)} for b in blocks]
        # the parse input is today's: the first text block
        rec["text"] = next((b.text for b in blocks if getattr(b, "type", None) == "text"), "")
    else:
        rec["error"] = _jsonable(r)
    return rec


def _batch_draws(impl: ExtractorImpl, ctxs: list[PartContext], *, draws: int, results_dir: Path,
                 poll_deadline_s: float | None, client) -> dict:
    """batch_extract_parts with results_dir (see its docstring)."""
    from steps.step_03_resolver import canonical_pdf_stem
    stems = [canonical_pdf_stem(c.pdf_path) for c in ctxs]
    dup = sorted({s for s in stems if stems.count(s) > 1})
    if dup:
        raise ValueError(f"duplicate canonical stems in one batch: {dup}")
    man_path = results_dir / "manifest.json"
    prior = json.loads(man_path.read_text()) if man_path.exists() else None
    if prior is not None and not prior.get("batch_id"):
        raise ValueError(f"{man_path} holds no batch id: a previous create may have gone through; "
                         "refusing to submit again into this results_dir")
    client = _batch_client(impl, client)
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request
    fingerprint = extractor_fingerprint(impl, draws=1, aggregate="single")
    call = {"model": impl.model, "max_tokens": impl._MAX_TOKENS,  # type: ignore[attr-defined]
            "temperature": impl._TEMPERATURE}  # type: ignore[attr-defined]
    reqs, recs, texts = [], {}, {}
    for i, (ctx, stem) in enumerate(zip(ctxs, stems)):
        model_input = impl.build_model_input(ctx)
        prompt = impl._prompt(ctx, model_input)  # type: ignore[attr-defined]
        ctx.request_sha256 = request_sha256(prompt)
        src = ctx.source_text if isinstance(ctx.source_text, str) and ctx.source_text != model_input else None
        texts[i] = (model_input, src)
        for d in range(1, draws + 1):
            cid = f"s{i:04d}-d{d}"
            if not _CUSTOM_ID_RE.match(cid):
                raise ValueError(f"custom id {cid!r} breaks ^[a-zA-Z0-9_-]{{1,64}}$")
            recs[cid] = {"index": i, "stem": stem, "part_number": ctx.part_number, "pdf_path": str(ctx.pdf_path),
                         "draw": d, "request_sha256": ctx.request_sha256,
                         "model_input_sha256": _sha256_text(model_input), "input_regime": ctx.input_regime,
                         "input_file": f"inputs/{i:04d}.txt",
                         "source_text_file": f"inputs/{i:04d}.source.txt" if src is not None else None}
            reqs.append(Request(custom_id=cid, params=MessageCreateParamsNonStreaming(
                messages=[{"role": "user", "content": prompt}], **call)))
    if prior is not None:                                   # RESUME: never a second create
        for k in ("model", "max_tokens", "temperature", "draws"):
            want = draws if k == "draws" else call[k]
            if prior.get(k) != want:
                raise ValueError(f"resume: manifest {k} {prior.get(k)!r} != this call's {want!r}")
        old = {c: r["request_sha256"] for c, r in prior["requests"].items()}
        new = {c: r["request_sha256"] for c, r in recs.items()}
        for c in sorted(set(old) | set(new)):
            if old.get(c) != new.get(c):
                raise ValueError(f"resume: request set differs from {man_path} at {c} "
                                 f"(stem {(prior['requests'].get(c) or recs.get(c))['stem']!r}): "
                                 f"manifest {old.get(c)} != rebuilt {new.get(c)}")
        batch_id = prior["batch_id"]
    else:
        (results_dir / "inputs").mkdir(parents=True, exist_ok=True)
        for i, (model_input, src) in texts.items():
            _atomic_write(results_dir / "inputs" / f"{i:04d}.txt", model_input)
            if src is not None:
                _atomic_write(results_dir / "inputs" / f"{i:04d}.source.txt", src)
        man = {"schema": BATCH_MANIFEST_SCHEMA, "impl": impl.name, "draws": draws, **call,
               "extractor_fingerprint": fingerprint, "batch_id": None, "requests": recs}
        _write_json(man_path, man)                          # before create
        batch = client.messages.batches.create(requests=reqs)
        man["batch_id"] = batch.id
        _write_json(man_path, man)                          # after create, before the first poll
        batch_id = batch.id
    _poll_batch(client, batch_id, poll_deadline_s, results_dir)
    rows = [_row_record(res) for res in client.messages.batches.results(batch_id)]
    (results_dir / "raw").mkdir(exist_ok=True)
    for n, rec in enumerate(rows):                          # every row persisted before any parse
        name = rec["custom_id"] if _CUSTOM_ID_RE.match(str(rec["custom_id"])) else f"_unexpected_id_{n}"
        _write_json(results_dir / "raw" / f"{name}.json", rec)
    out_draws = {s: {d: None for d in range(1, draws + 1)} for s in stems}
    truncated, failed, usage, tiers, seen = [], [], {}, set(), set()
    for rec in sorted(rows, key=lambda x: str(x["custom_id"])):
        m = recs.get(rec["custom_id"])
        if m is None:
            failed.append([None, None, f"unknown_custom_id:{rec['custom_id']}"])
            continue
        seen.add(rec["custom_id"])
        stem, d = m["stem"], m["draw"]
        for k, v in (rec["usage"] or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                usage[k] = usage.get(k, 0) + v
            elif k == "service_tier" and isinstance(v, str):
                tiers.add(v)
        if rec["result_type"] != "succeeded":
            failed.append([stem, d, f"result_type:{rec['result_type']}"])
            continue
        if rec["stop_reason"] == "max_tokens":
            logger.warning("[extract] truncated reply for %s draw %d (max_tokens)", stem, d)
            truncated.append([stem, d])
            continue
        try:
            parsed = parse_model_json(rec["text"])
        except ValueError:
            failed.append([stem, d, "unparseable"])
            continue
        out_draws[stem][d] = {
            "pin_groups": parsed.get("pin_groups", []),
            "default_group": parsed.get("default_group"),
            "extractor_fingerprint": fingerprint,
            "request_sha256": m["request_sha256"],
            "input_regime": m["input_regime"],
            "model_input": texts[m["index"]][0],
            "usage": rec["usage"],
            "stop_reason": rec["stop_reason"]}
    for c in sorted(set(recs) - seen):
        failed.append([recs[c]["stem"], recs[c]["draw"], "missing_result"])
    if tiers:
        usage["service_tier"] = sorted(tiers)
    return {"draws": out_draws, "__truncated__": truncated, "__failed__": failed,
            "batch_id": batch_id, "usage": usage}


# ── majority materialisation (TODO-531 rulings Y2 / Y3 / Y6 / Z1) ────────────
# The three VALIDATED draws of one stem are voted per (pin, field) and one group-shaped cache is materialised
# from the vote. The projection and the vote key re-implement the extractor gate's scorer v2 at 3f9c14e
# (tools/extractor_gate/step5_diff.alts_expr / norm, scoring.vote_key / majority): never imported, since this
# module ships and tools/ does not; tools/extractor_gate/tests/test_majority_equivalence.py holds them equal.
MAJORITY_FIELDS = ("supply_min", "supply_max", "supply_abs_max", "absolute_max_voltage",
                   "VIH_min", "VIL_max", "VOH_min", "VOL_max")
MAJORITY_DRAWS = 3
MAJORITY_AGGREGATE = "majority_2of3_null_on_disagree; citation=base_replicate; one_entry_alts=plain"
# top-level keys of a batch per-draw dict that never reach a cache file
_MAJORITY_STRIP = ("model_input", "extractor_fingerprint", "request_sha256", "input_regime", "usage",
                   "stop_reason", "__truncated__")


class MajorityInvariantError(AssertionError):
    """The materialised cache re-projects to a value that is neither the aggregate nor None."""


def _mj_norm(v):
    """= step5_diff.norm: numeric (or numeric string, trailing V dropped) -> float rounded 1e-6; any other
    non-null -> "REL:"+text; null -> None."""
    if v is None:
        return None
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        return round(float(v), 6)
    s = str(v).strip()
    try:
        return round(float(s.rstrip("Vv ").strip()), 6)
    except ValueError:
        return "REL:" + s


def _mj_form(g: dict, f: str) -> str:
    """The form production reads (step_05_validator.resolve_field precedence, ruling Z1): a non-empty
    <f>_alts list, else a non-empty <f>_expr string, else the numeric (a coexisting numeric is discarded)."""
    a = g.get(f + "_alts")
    if isinstance(a, list) and a:
        return "alts"
    e = g.get(f + "_expr")
    if isinstance(e, str) and e.strip():
        return "expr"
    return "numeric"


def _mj_value(g: dict, f: str):
    """= step5_diff.alts_expr then norm: "ALTS:"+json[[condition, value, expr], ...] | "REL:"+expr | the
    normalised numeric | None."""
    form = _mj_form(g, f)
    if form == "expr":
        return "REL:" + g[f + "_expr"].strip()
    if form == "alts":
        ents = []
        for x in g[f + "_alts"]:
            x = x if isinstance(x, dict) else {"value": x}
            val = x.get("value")
            nvv = _mj_norm(val) if val is not None else None
            e = x.get("expr")
            if isinstance(nvv, str) and nvv.startswith("REL:"):
                e, nvv = (e or nvv[4:]), None
            ents.append([x.get("condition"), nvv, e.strip() if isinstance(e, str) and e.strip() else None])
        return "ALTS:" + json.dumps(ents, sort_keys=True)
    return _mj_norm(g.get(f))


def _mj_nsym(s) -> str:
    """rail-symbol normalisation: spaces / '_' / '-' dropped, upper-cased, one leading '+' stripped."""
    s = re.sub(r"[\s_\-]", "", str(s)).upper()
    return s[1:] if s.startswith("+") else s


def _mj_ptuple(expr, parse):
    if not isinstance(expr, str):
        return None
    t = parse(expr, allow_bare=True)
    return None if t is None else (t[0], t[1], _mj_nsym(t[2]))


def _mj_ent_key(e, parse):
    _cond, val, ex = e
    if isinstance(val, float):
        return ("V", round(val, 6))
    t = _mj_ptuple(ex, parse)
    return ("E", round(t[0], 6), round(t[1], 3), t[2]) if t else ("S", ex)


def _mj_one_alt(v):
    """the single alternative [condition, value, expr] of a one-entry ALTS projection, else None."""
    if isinstance(v, str) and v.startswith("ALTS:"):
        ents = json.loads(v[5:])
        if len(ents) == 1:
            return ents[0]
    return None


def _mj_one_alt_plain(v):
    """= scoring.one_alt_plain (TODO-531 V1): the plain projected value a one-entry ALTS stands for: its entry's
    numeric value, else "REL:"+its expr; None when v is not a one-entry ALTS or its entry holds neither."""
    e = _mj_one_alt(v)
    if e is None:
        return None
    _cond, val, ex = e
    if isinstance(val, float):
        return val
    if isinstance(ex, str) and ex.strip():
        return "REL:" + ex.strip()
    return None


def _mj_key(v, parse) -> str:
    """= scoring.vote_key (ruling Y6): a REL votes on its parse_rail_expr(allow_bare=True) tuple (k 1e-6,
    c 1e-3, normalised symbol), an unparseable one by its raw string; an ALTS as the label-agnostic set of its
    alternatives' keys; everything else by value (repr; null is a value). V1 (TODO-531): a one-entry ALTS votes
    as the plain value it stands for (_mj_one_alt_plain); two or more alternatives are unchanged."""
    p = _mj_one_alt_plain(v)
    if p is not None:
        v = p
    if isinstance(v, str) and v.startswith("REL:"):
        t = _mj_ptuple(v[4:], parse)
        return repr(("E", round(t[0], 6), round(t[1], 3), t[2]) if t else ("S", v))
    if isinstance(v, str) and v.startswith("ALTS:"):
        return repr(("A", tuple(sorted({_mj_ent_key(e, parse) for e in json.loads(v[5:])}, key=repr))))
    return repr(v)


def _mj_votes_raw(v, parse) -> bool:
    p = _mj_one_alt_plain(v)
    if p is not None:
        v = p
    if isinstance(v, str) and v.startswith("REL:"):
        return _mj_ptuple(v[4:], parse) is None
    if isinstance(v, str) and v.startswith("ALTS:"):
        return any(_mj_ent_key(e, parse)[0] == "S" for e in json.loads(v[5:]))
    return False


def _mj_pick(vals: list, ks: list, top: str):
    """= scoring.majority's pick (TODO-531 V1n): the literal standing for the winning key `top` — the first value
    holding it that is not a one-entry ALTS; else the first one's plain form (_mj_one_alt_plain), else that ALTS."""
    win = [x for x, k in zip(vals, ks) if k == top]
    plain = [x for x in win if _mj_one_alt(x) is None]
    if plain:
        return plain[0]
    p = _mj_one_alt_plain(win[0])
    return win[0] if p is None else p


def _mj_pins(draws: dict, reps: list, pins) -> tuple[list, str, dict]:
    """Ruling Y2. -> ([(pin_id, pin_name)], source, per-replicate entry count). pins given (the gate's
    universe: dicts with pin_id / pin_name, or 2-tuples) -> that list. Else the ordered union over replicates
    in index order of every example_pins entry, split by pin_match.split_entry (the matcher's own splitter):
    "<num>|<name>" -> (num, name); "?|<name>" -> ("?", name); a bare entry -> (entry, entry)."""
    from steps.pin_match import split_entry
    out, seen = [], set()

    def add(p):
        k = f"{p[0]}|{p[1]}"
        if k not in seen:
            seen.add(k)
            out.append(p)
    if pins is not None:
        for p in pins:
            add((p["pin_id"], p["pin_name"]) if isinstance(p, dict) else (p[0], p[1]))
        return out, "explicit", {}
    per = {}
    for r in reps:
        n = 0
        for g in draws[r].get("pin_groups") or []:
            for e in (g.get("example_pins") or []) if isinstance(g, dict) else []:
                if not isinstance(e, str) or not e.strip():
                    continue
                kind, num, name = split_entry(e)
                n += 1
                add((num, name) if kind == "pair" else ("?", name) if kind == "name" else (e, e))
        per[r] = n
    return out, "union_example_pins", per


_MJ_SUPPLY = ("supply_min", "supply_max", "supply_abs_max")


def _mj_supply_tuple(g: dict) -> tuple:
    """a power group's (supply_min, supply_max, supply_abs_max), each _mj_norm'd: the numerics step_08b reads."""
    return tuple(_mj_norm(g.get(f)) for f in _MJ_SUPPLY)


def _mj_supply_keep(support: dict) -> bool:
    """AA1: a NOVOTE power group keeps its supply fields when its tuple is SUPPORTED (>= 2 valid draws) for
    every pin that lists it in the base. support {pin_key: draws holding the tuple for that pin}."""
    return all(n >= 2 for n in support.values())


def _mj_project(parsed: dict, pins: list, resolve) -> dict:
    """pin_key -> (resolved group | None, {field: projected value})."""
    out = {}
    for pid, pname in pins:
        g = resolve(pid, pname, parsed) if parsed else None
        out[f"{pid}|{pname}"] = (g, {f: (_mj_value(g, f) if g else None) for f in MAJORITY_FIELDS})
    return out


# the fallback probe (TODO-531 AA2): a pin whose id and name no example_pins entry names, so it resolves by each
# draw's fallback (default_group, else the first signal group, else the first non-power group)
_MJ_PROBE = ("mj-probe-0", "MJ_FALLBACK_PROBE")


def _mj_probe(draws: dict, reps: list, resolve, key) -> dict:
    """AA2, union pin list only. -> {pin, dropped, group_per_draw, agg {f: value}, agreement {rep: n}}: the probe
    projected in every valid draw, its 2-of-3 per field (all distinct -> None) and each draw's agreement with it.
    A draw whose example_pins match the probe (pin_match.match_groups is not ("none", [])) drops it."""
    from steps.pin_match import match_groups
    pid, pname = _MJ_PROBE
    out = {"pin": f"{pid}|{pname}", "dropped": False, "group_per_draw": {}, "agg": {}, "agreement": {}}
    if any(match_groups(pid, pname, draws[r].get("pin_groups") or []) != ("none", []) for r in reps):
        out["dropped"] = True
        return out
    proj = {r: _mj_project(draws[r], [_MJ_PROBE], resolve)[out["pin"]] for r in reps}
    out["group_per_draw"] = {r: (proj[r][0].get("group_name") if proj[r][0] else None) for r in reps}
    for f in MAJORITY_FIELDS:
        vals = [proj[r][1][f] for r in reps]
        ks = [key(x) for x in vals]
        top = max(set(ks), key=ks.count)
        out["agg"][f] = vals[ks.index(top)] if ks.count(top) >= 2 else None
    out["agreement"] = {r: sum(key(proj[r][1][f]) == key(out["agg"][f]) for f in MAJORITY_FIELDS) for r in reps}
    return out


def _majority_vote(draws: dict, pins=None) -> dict | None:
    """The vote materialise_majority runs (= scoring.majority with key=vote_key). -> None when fewer than two
    draws are valid, else {reps, pins, pks, pin_source, per_replicate, proj, agg {(pk, f): value},
    voters {(pk, f): [rep]}, disagree [(pk, f)], vote_raw [(pk, f)], base, base_without_probe, probe}. On the
    union pin list (pins None) each draw's agreement with the fallback probe (_mj_probe) is added to its base
    score; the probe is in no other output. An explicit pin list has no probe (probe None).
    Every matcher call on the voted pins passes part_pins = that list as (pin_id, pin_name, "") (TODO-531 N2u:
    returned as part_pins, with the resolve that carries it); the probe's calls do not."""
    import functools
    from steps.pin_match import resolve_pin_spec_from_groups
    from steps.step_05_validator import parse_rail_expr
    reps = sorted(r for r, d in draws.items() if isinstance(d, dict) and isinstance(d.get("pin_groups"), list))
    if len(reps) < 2:
        return None
    plist, source, per = _mj_pins(draws, reps, pins)
    pks = [f"{a}|{b}" for a, b in plist]
    part_pins = [(a, b, "") for a, b in plist]
    resolve = functools.partial(resolve_pin_spec_from_groups, part_pins=part_pins)
    proj = {r: _mj_project(draws[r], plist, resolve) for r in reps}

    def key(v):
        return _mj_key(v, parse_rail_expr)
    agg, voters, dis, raw = {}, {}, [], []
    for pk in pks:
        for f in MAJORITY_FIELDS:
            vals = [proj[r][pk][1][f] for r in reps]
            ks = [key(v) for v in vals]
            if any(_mj_votes_raw(v, parse_rail_expr) for v in vals):
                raw.append((pk, f))
            top = max(set(ks), key=ks.count)
            if ks.count(top) >= 2:
                agg[(pk, f)] = _mj_pick(vals, ks, top)
                voters[(pk, f)] = [r for r, k in zip(reps, ks) if k == top]
            else:
                agg[(pk, f)], voters[(pk, f)] = None, []
                dis.append((pk, f))
    probe = _mj_probe(draws, reps, resolve_pin_spec_from_groups, key) if pins is None else None
    bonus = (probe or {}).get("agreement") or {}
    scores = {r: sum(key(proj[r][pk][1][f]) == key(agg[(pk, f)]) for pk in pks for f in MAJORITY_FIELDS)
              for r in reps}
    # most (pin, field) agreement with the aggregate (+ the probe's), ties -> lowest index (reps is sorted and
    # max keeps the first maximum)
    base0 = max(reps, key=lambda r: scores[r])
    base = max(reps, key=lambda r: scores[r] + bonus.get(r, 0))
    return {"reps": reps, "pins": plist, "pks": pks, "pin_source": source, "per_replicate": per, "proj": proj,
            "agg": agg, "voters": voters, "disagree": dis, "vote_raw": raw, "base": base,
            "base_without_probe": base0, "probe": probe, "key": key, "part_pins": part_pins, "resolve": resolve}


def _mj_cit_keys(cits: dict, f: str) -> list:
    return [k for k in cits if k in (f, f + "_expr") or k.startswith(f + "_alts[")]


def _mj_form_keys(g: dict, f: str, form: str) -> list:
    if form == "alts":
        return [f"{f}_alts[{k}]" for k in range(len(g.get(f + "_alts") or []))]
    return [f + "_expr"] if form == "expr" else [f]


def _mj_alts_plain(sg: dict, f: str):
    """V1n: a one-entry <f>_alts of group sg in its plain form -> ("numeric", value) | ("expr", text), or None
    (not one entry, or the entry holds neither). The numeric keeps the entry's own value unless it is a string
    (then its _mj_norm'd float); the expr is the projection's (_mj_value), so the plain form re-projects to the
    key the ALTS voted with."""
    import copy
    a = sg.get(f + "_alts")
    if not (isinstance(a, list) and len(a) == 1):
        return None
    p = _mj_one_alt_plain(_mj_value({f + "_alts": a}, f))
    if p is None:
        return None
    if isinstance(p, float):
        x = a[0] if isinstance(a[0], dict) else {"value": a[0]}
        val = x.get("value")
        return "numeric", (p if isinstance(val, str) else copy.deepcopy(val))
    return "expr", p[4:]


def _mj_write(g: dict, f: str, sg: dict) -> str:
    """Write source group sg's value of field f into g in ONE form: the numeric; or <f>_expr with the numeric
    null and no <f>_alts; or <f>_alts with the numeric null and no <f>_expr. A one-entry <f>_alts is written in
    its plain form (V1n, _mj_alts_plain); a one-entry <f>_alts is never written. -> the form written."""
    import copy
    form = _mj_form(sg, f)
    g.pop(f + "_expr", None)
    g.pop(f + "_alts", None)
    plain = _mj_alts_plain(sg, f) if form == "alts" else None
    if plain is not None:
        if plain[0] == "numeric":
            g[f] = plain[1]
            return "numeric"
        g[f] = None
        g[f + "_expr"] = plain[1]
        return "expr"
    if form == "alts":
        g[f] = None
        g[f + "_alts"] = copy.deepcopy(sg[f + "_alts"])
    elif form == "expr":
        g[f] = None
        g[f + "_expr"] = sg[f + "_expr"]
    else:
        g[f] = copy.deepcopy(sg.get(f))
    return form


def _mj_null(g: dict, bg: dict, f: str, status: str) -> bool:
    """Null field f in g (numeric None, _expr / _alts removed). When the base group held a value, its
    citation(s) of that form get "status": "nulled:<status>" and "rejected_value" (a missing citation is
    created as no-quote). Base citations of f's other forms that carry no status are dropped (their value is
    not in the cache). -> True when a base value was rejected."""
    import copy
    held = _mj_value(bg, f) is not None
    form = _mj_form(bg, f)
    g[f] = None
    g.pop(f + "_expr", None)
    g.pop(f + "_alts", None)
    cits = g.get("citations")
    if not isinstance(cits, dict):
        if not held:
            return False
        cits = g["citations"] = {}
    keep = set(_mj_form_keys(bg, f, form)) if held else set()
    for k in _mj_cit_keys(cits, f):
        if k not in keep and not (isinstance(cits[k], dict) and cits[k].get("status")):
            del cits[k]
    if not held:
        return False
    alts = bg.get(f + "_alts") if form == "alts" else None
    for n, k in enumerate(sorted(keep)):
        c = cits.get(k) if isinstance(cits.get(k), dict) else {"quote": None, "page": None, "section": None,
                                                              "match": "no-quote"}
        c["status"] = f"nulled:{status}"
        if form == "alts":
            idx = int(k[len(f) + 6:-1])
            c["rejected_value"] = copy.deepcopy(alts[idx])
        else:
            c["rejected_value"] = bg.get(f + "_expr") if form == "expr" else bg.get(f)
        cits[k] = c
    return True


def materialise_majority(draws: dict, *, pins=None) -> tuple[dict | None, dict]:
    """TODO-531: one group-shaped cache from the majority of the VALIDATED draws of one stem.

    draws {replicate: parsed} — each the output of extraction_validator.validate; a None / non-dict draw is
    not valid. Fewer than two valid draws -> (None, report with reason "fewer_than_two_valid_draws").
      Y2 pin list   `pins` given (the gate's universe) or the ordered union of example_pins (_mj_pins).
      Z1 projection per pin, the group pin_match.resolve_pin_spec_from_groups resolves; per field: a non-empty
                    <f>_alts -> ALTS, else a non-empty <f>_expr -> REL, else the numeric (a non-numeric string
                    -> REL) or None.
      Y6 vote       per (pin, field) a key held by >= 2 valid draws wins (null is a value); all distinct ->
                    None, DISAGREE. Two draws must agree. A one-entry ALTS votes as its plain value (V1, _mj_key).
      N2u           every matcher call on the voted pins passes part_pins = the voted pin list.
      base          the draw agreeing with the aggregate on the most (pin, field), ties -> lowest index; on the
                    union pin list each draw's agreement with the fallback probe's 2-of-3 is added (AA2,
                    _mj_probe: fallback routing enters base selection; the probe is voted on nothing else). The
                    cache is the base's groups verbatim (every non-voted key) and its default_group.
      supply set    (AA1) per valid draw and pin, the _mj_norm'd (supply_min, supply_max, supply_abs_max) of
                    every group pin_match.listing_supply_groups lists the pin under (step_08b's reading path);
                    a tuple is SUPPORTED for a pin when >= 2 valid draws hold it.
      fill          per base group x field over the pins that resolve to that group in the base: none ->
                    GROUP-NOVOTE (nulled), except that a group listing_supply_groups lists for some pin in the
                    base keeps its three supply fields verbatim (base citations) when its tuple is SUPPORTED for
                    every such pin, else nulls them "nulled:supply_set" (SUPPLY-SET-KEPT / SUPPLY-SET-NULL; its
                    other five fields stay GROUP-NOVOTE); one aggregate key -> written in ONE form, the literal
                    taken from the lowest-index draw that voted it (the base if it did), with that draw's citation(s) (plus
                    "from_replicate" when not the base); V1n (TODO-531): when a voter holds the winning key as a
                    one-entry <f>_alts, the lowest-index voter holding it as a numeric or a REL instead, and when
                    every voter holds a one-entry <f>_alts it is written as the numeric or <f>_expr (never a
                    one-entry <f>_alts), its <f>_alts[0] citation re-keyed to the plain field with "from_alts" true
                    (WRITTEN-PLAIN-FROM-ALTS); a null aggregate over a base value -> nulled
                    ("nulled:disagree" when a pin of the group is DISAGREE, else "nulled:majority"); several
                    keys -> GROUP-SPLIT (nulled). A nulled base value keeps its citation with status and
                    rejected_value. VIH_source / abs_max_source are re-derived with the validator's own
                    labeller (extraction_validator._source_label) on validated groups.
      invariant     the cache re-projected on the same pin list gives, per (pin, field), the aggregate's key or
                    None — never a third value; and every supply tuple a pin reads (listing_supply_groups on the
                    cache) from a kept supply-set group is SUPPORTED for that pin (MajorityInvariantError).
    -> (cache {"pin_groups", "default_group"} | None, report)."""
    import copy
    from collections import Counter
    from steps.pin_match import listing_supply_groups
    rep = {"valid_draws": sorted(r for r, d in draws.items()
                                 if isinstance(d, dict) and isinstance(d.get("pin_groups"), list)),
           "n_draws": len(draws), "base_replicate": None}
    v = _majority_vote(draws, pins)
    if v is None:
        rep["reason"] = "fewer_than_two_valid_draws"
        return None, rep
    key, agg, voters, pks, base = v["key"], v["agg"], v["voters"], v["pks"], v["base"]
    dis = set(v["disagree"])
    bparsed = draws[base]
    bgroups = bparsed["pin_groups"]
    gidx = {}
    for pk in pks:
        g = v["proj"][base][pk][0]
        gidx[pk] = next((i for i, h in enumerate(bgroups) if h is g), None)
    cache = {"pin_groups": copy.deepcopy(bgroups), "default_group": bparsed.get("default_group")}
    counts = Counter()
    per_field = {f: Counter() for f in MAJORITY_FIELDS}
    for pk, f in v["disagree"]:
        per_field[f]["DISAGREE"] += 1
    for pk, f in v["vote_raw"]:
        per_field[f]["vote_raw_string"] += 1
    for pk in pks:
        for f in MAJORITY_FIELDS:
            if gidx[pk] is None and agg[(pk, f)] is not None:
                per_field[f]["base_unresolved_pin_fields"] += 1
    # AA1: the supply reading path (step_08b via pin_match.listing_supply_groups) per draw and pin
    pp = v["part_pins"]
    sets = {r: {pk: {_mj_supply_tuple(h) for h in listing_supply_groups(pid, pname, draws[r], part_pins=pp)}
                for pk, (pid, pname) in zip(pks, v["pins"])} for r in v["reps"]}

    def support(pk, t):
        return sum(t in sets[r][pk] for r in v["reps"])
    blist = {}
    for pk, (pid, pname) in zip(pks, v["pins"]):
        ls = listing_supply_groups(pid, pname, bparsed, part_pins=pp)
        blist[pk] = {i for i, h in enumerate(bgroups) if any(x is h for x in ls)}
    supply_set = {"kept": [], "nulled": []}
    kept = set()
    for gi, g in enumerate(cache["pin_groups"]):
        if not isinstance(g, dict):
            continue
        bg = bgroups[gi]
        gp = [pk for pk in pks if gidx[pk] == gi]
        listers = [pk for pk in pks if gi in blist[pk]] if not gp else []
        if listers:
            t = _mj_supply_tuple(bg)
            sup = {pk: support(pk, t) for pk in listers}
            keep = _mj_supply_keep(sup)
            supply_set["kept" if keep else "nulled"].append(
                {"group": bg.get("group_name"), "index": gi, "tuple": list(t), "pins": sup})
            for f in _MJ_SUPPLY:
                if keep:
                    per_field[f]["SUPPLY-SET-KEPT"] += 1
                else:
                    per_field[f]["SUPPLY-SET-NULL"] += 1
                    per_field[f]["SUPPLY-SET-NULL_valued"] += _mj_null(g, bg, f, "supply_set")
            if keep:
                kept.add(gi)
        for f in MAJORITY_FIELDS:
            if listers and f in _MJ_SUPPLY:
                continue
            if not gp:
                per_field[f]["GROUP-NOVOTE"] += 1
                per_field[f]["GROUP-NOVOTE_valued"] += _mj_null(g, bg, f, "group_novote")
                continue
            ks = {key(agg[(pk, f)]) for pk in gp}
            if len(ks) > 1:
                per_field[f]["GROUP-SPLIT"] += 1
                per_field[f]["GROUP-SPLIT_valued"] += _mj_null(g, bg, f, "group_split")
                continue
            if agg[(gp[0], f)] is None:
                if _mj_value(bg, f) is not None:
                    st = "disagree" if any((pk, f) in dis for pk in gp) else "majority"
                    _mj_null(g, bg, f, st)
                    per_field[f][f"nulled_{st}"] += 1
                continue
            vr = sorted({r for pk in gp for r in voters[(pk, f)]})

            def held(r):
                return v["proj"][r][next(pk for pk in gp if r in voters[(pk, f)])][1][f]
            # V1n: when a voter holds the winning key as a one-entry ALTS, the literal comes from the lowest-index
            # voter holding it as a numeric or a REL; when every voter holds a one-entry ALTS, as today
            plain_vr = [r for r in vr if _mj_one_alt(held(r)) is None]
            src = plain_vr[0] if len(plain_vr) < len(vr) and plain_vr else (base if base in vr else vr[0])
            spk = next(pk for pk in gp if src in voters[(pk, f)])
            sg = v["proj"][src][spk][0]
            bcits = g.get("citations") if isinstance(g.get("citations"), dict) else None
            old = {k: bcits[k] for k in _mj_cit_keys(bcits, f)} if bcits is not None else {}
            from_alts = _mj_form(sg, f) == "alts"
            form = _mj_write(g, f, sg)
            from_alts = from_alts and form != "alts"
            scits = sg.get("citations") if isinstance(sg.get("citations"), dict) else {}
            if from_alts:
                # DD2: the entry's citation, re-keyed to the plain field, marked from_alts
                c0 = scits.get(f + "_alts[0]")
                new = ({_mj_form_keys(g, f, form)[0]: dict(copy.deepcopy(c0), from_alts=True)}
                       if isinstance(c0, dict) else {})
                per_field[f]["written_plain_from_alts"] += 1
            else:
                new = {k: copy.deepcopy(scits[k]) for k in _mj_form_keys(sg, f, form)
                       if isinstance(scits.get(k), dict)}
            if src != base:
                for c in new.values():
                    c["from_replicate"] = src
                per_field[f]["citation_from_other_replicate"] += 1
            if bcits is None and new:
                bcits = g["citations"] = {}
            if bcits is not None:
                for k in old:
                    del bcits[k]
                for k, c in old.items():       # a base citation of another form keeps only when it has a status
                    if (k not in new and src == base and isinstance(c, dict) and c.get("status")
                            and not (from_alts and k.startswith(f + "_alts["))):
                        bcits[k] = c
                bcits.update(new)
    labeller = None
    try:
        from steps.extraction_validator import _source_label as labeller
    except Exception:  # pragma: no cover - the validator ships beside this module
        labeller = None
    for g in cache["pin_groups"]:
        if labeller is not None and isinstance(g, dict) and isinstance(g.get("citations"), dict):
            cits = g["citations"]
            g["VIH_source"] = labeller(cits.get("VIH_min") or cits.get("VIH_min_expr"))
            g["abs_max_source"] = labeller(cits.get("absolute_max_voltage") or cits.get("absolute_max_voltage_expr"))
    # invariant: never a third value
    re_proj = _mj_project(cache, v["pins"], v["resolve"])
    for pk in pks:
        for f in MAJORITY_FIELDS:
            got = re_proj[pk][1][f]
            if got is not None and key(got) != key(agg[(pk, f)]):
                raise MajorityInvariantError(
                    f"materialised {pk} {f} = {got!r}: neither the aggregate {agg[(pk, f)]!r} nor None")
    # AA1 invariant: every supply tuple a pin reads from a kept NOVOTE group is SUPPORTED for that pin
    cgroups = cache["pin_groups"]
    for pk, (pid, pname) in zip(pks, v["pins"]):
        for h in listing_supply_groups(pid, pname, cache, part_pins=pp):
            gi = next(i for i, x in enumerate(cgroups) if x is h)
            if gi in kept and support(pk, _mj_supply_tuple(h)) < 2:
                raise MajorityInvariantError(
                    f"materialised {pk} reads supply tuple {_mj_supply_tuple(h)!r} from kept group "
                    f"{h.get('group_name')!r}: supported by {support(pk, _mj_supply_tuple(h))} draw(s)")
    for c in per_field.values():
        counts.update(c)
    names = ("DISAGREE", "GROUP-SPLIT", "GROUP-SPLIT_valued", "GROUP-NOVOTE", "GROUP-NOVOTE_valued",
             "SUPPLY-SET-KEPT", "SUPPLY-SET-NULL", "SUPPLY-SET-NULL_valued",
             "nulled_disagree", "nulled_majority", "vote_raw_string", "citation_from_other_replicate",
             "base_unresolved_pin_fields", "written_plain_from_alts")
    probe = v["probe"]
    rep.update({
        "base_replicate": base,
        "base_without_probe": v["base_without_probe"],
        "probe": None if probe is None else {
            "pin": probe["pin"], "dropped": probe["dropped"],
            "group_per_draw": {str(r): n for r, n in probe["group_per_draw"].items()},
            "agreement": {str(r): n for r, n in probe["agreement"].items()},
            "base_changed": base != v["base_without_probe"]},
        "supply_set": supply_set,
        "pin_list": {"size": len(pks), "source": v["pin_source"],
                     "entries_per_replicate": {str(r): n for r, n in v["per_replicate"].items()}},
        "counts": {k: counts.get(k, 0) for k in names},
        "per_field": {f: {k: c.get(k, 0) for k in names} for f, c in per_field.items()},
        "disagree": [{"pin": pk, "field": f, "values": {str(r): v["proj"][r][pk][1][f] for r in v["reps"]}}
                     for pk, f in v["disagree"]],
        "source_labels": ("re-derived with extraction_validator._source_label" if labeller is not None
                          else "left as the base's (validator labeller not importable)"),
    })
    return cache, rep


def majority_validation(reports: dict, majority_report: dict, request_sha256s: dict | None = None) -> dict:
    """provenance.validation for a majority cache: the base draw's extraction_validator report plus a
    "majority" key holding materialise_majority's report, the draws' request_sha256s and each draw's
    validator counts. reports: {replicate: validator report}."""
    import copy
    base = majority_report["base_replicate"]
    out = copy.deepcopy(reports[base])
    out["majority"] = dict(copy.deepcopy(majority_report),
                           request_sha256s={str(r): s for r, s in (request_sha256s or {}).items()},
                           draw_counts={str(r): copy.deepcopy((rp or {}).get("counts"))
                                        for r, rp in sorted(reports.items())})
    return out


def write_majority_cache(ctx: PartContext, cache: dict, *, validation: dict,
                         dest_path: str | None = None, request_sha256s: dict | None = None) -> None:
    """run_extractor's tail on a materialised majority cache (TODO-531). The 3-draw fingerprint
    (draws=3, aggregate=MAJORITY_AGGREGATE) is stamped ONLY here, on validated, voted output: `validation`
    must carry the "majority" report (majority_validation), so a raw batch dict cannot reach
    save_pin_groups_cache through this function. request_sha256 = the base draw's (request_sha256s, else
    ctx.request_sha256); service_tier "batch". dest_path None writes the staging tier, as run_extractor."""
    import copy
    from steps import step_03_resolver
    if not isinstance(cache, dict) or not isinstance(cache.get("pin_groups"), list):
        raise ValueError("write_majority_cache: no materialised cache (pin_groups list) to write")
    if not isinstance(validation, dict) or not isinstance(validation.get("majority"), dict):
        raise ValueError("write_majority_cache: validation lacks the majority report; only validated, voted "
                         "output is written under the majority fingerprint")
    impl = get_extractor("haiku_pdfplumber")
    raw = {k: v for k, v in cache.items() if k not in _MAJORITY_STRIP}
    validation = copy.deepcopy(validation)
    maj = validation["majority"]
    if request_sha256s:
        maj["request_sha256s"] = {str(r): s for r, s in request_sha256s.items()}
    rs = (maj.get("request_sha256s") or {}).get(str(maj.get("base_replicate"))) or ctx.request_sha256
    _advisory_guard(raw, ctx)
    di = _doc_identity_verdict(ctx, ctx.source_text)
    step_03_resolver.save_pin_groups_cache(
        ctx.pdf_path, raw,
        extractor=impl.extractor_tag, model=impl.model,
        extractor_impl=impl.name, input_path=impl.input_path,
        doc_identity=di,
        dest_path=dest_path,
        validation=validation,
        extractor_fingerprint=extractor_fingerprint(impl, draws=MAJORITY_DRAWS, aggregate=MAJORITY_AGGREGATE),
        pdfplumber_version=pdfplumber_version(),
        service_tier="batch",
        request_sha256=rs,
        input_regime=ctx.input_regime,
    )

"""Doc-identity evidence layer (TODO-337 Phase 2b-i) — VERDICT-NEUTRAL.

Answers exactly one question, and records the answer as evidence: **does the cache
stem a `_pin_groups.json` was written under actually appear in the text of the PDF
it claims to come from?**

Motivation of record: TODO-37's `pic12c508a` incident, where the cached PDF was
DS41227D (a PIC12F508/509 *programming spec* with zero mentions of the part) — a
wrong document that no provenance hash could catch, because the hash signs "which
bytes did we parse", never "are these the right bytes".

Nothing here moves a verdict. `classify()` is pure; its result is stamped into
`provenance.doc_identity` as a sibling key (deliberately OUTSIDE the hash-signed
`parse_config` — see `step_03_resolver.build_provenance`) and read by the
read-only audit tool (`tools/doc_identity_audit.py`). No refusal, no quarantine,
no read-time invalidation: the log-only convention of `check_cache_provenance`
stands.

Phase 2b-iii adds the qualifier layer at the bottom of this module: a MISS or
`unverifiable` cache appends fixed text to the `evidence_label` of any finding
whose spec it supplied (`step_10_report.build_report`). Text only — no status,
severity, confidence or verdict changes, and the qualifier is applied AFTER the
LLM explanation path has been fed the raw label, so it never enters a prompt.

## The matcher

Promoted verbatim in behaviour from the Phase-1/1b sweep's matcher of record
(`investigation/experiments/todo337_docidentity/todo337_sweep.py` tiers 1/2 +
`todo337_tier3_analysis_v2.py` tier 3a), so the classes this module produces
reproduce that sweep's measured distribution (STRONG 407 / WEAK 141 / MISS 156 /
unverifiable 4 over 708 caches).

- **tier 1** — the raw stem appears in the text.
- **tier 2** — the stem with a trailing package suffix stripped appears.
- **tier 3** — progressive two-token trailing strip (`-`/`_` delimited), shallowest
  hit wins; STRONG only at strip depth 1 with a head covering >= 60% of the stem.

Anything else is MISS. `unverifiable` is reserved for "no text to test against"
(source PDF missing, sha mismatch, extraction failed) — never conflated with MISS,
which is a real negative.

That includes an **image-only scan**: a PDF that links correctly but yields zero
non-whitespace characters is `unverifiable`, NOT MISS. The Phase-1 sweep called
these MISS (it tested the stem against an empty string and read the negative as
real); this module does not, because nothing was measured. Settled by Nelson
2026-08-03 — do not "restore" the sweep's reading. Affected stems today:
`kusbx-sl2-cs1n24-b-tr`, `ph1-15-ua`, `ph1-17-ua`.

## Deliberate non-reuse of BASE_SUFFIX_RE

`step_03_resolver.BASE_SUFFIX_RE` is a *filename-resolution* helper (it decides
which PDF `_find_pdf_recursive` selects for an MPN). Widening or importing it here
would couple doc identity to PDF resolution, whose blast radius is corpus-wide.
This module therefore owns a private copy of the suffix shape and never touches
the resolution one. Keep them separate.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

_logger = logging.getLogger(__name__)

# ── classification vocabulary (fixed — the audit tool and the backfill agree) ──
STRONG = "STRONG"
WEAK = "WEAK"
MISS = "MISS"
UNVERIFIABLE = "unverifiable"

# The STRONG bar: a depth-1 head must cover at least this fraction of the stem.
# 0.60 is the sweep's measured bar; `rp2040_stamp` (head `rp2040`, ratio 0.500)
# is the canonical WEAK just below it.
STRONG_STEM_RATIO = 0.60

# A candidate head shorter than this is not discriminating enough to test.
MIN_HEAD_CHARS = 5

# Tier-2 floor: a suffix-stripped base shorter than this is not a real MPN.
MIN_BASE_STEM_CHARS = 4

# PRIVATE copy of the trailing package-suffix shape. Intentionally NOT imported
# from step_03_resolver — see the module docstring. Matches a trailing 1-2 letters
# followed by a digit, optionally preceded by `-`/`_` (e.g. STM32F103C8`T6`).
_BASE_SUFFIX_RE = re.compile(r"[-_]?[A-Z]{1,2}\d$", re.IGNORECASE)

# Two-token strip: keep the separators so a head can be rebuilt exactly.
_TOKEN_SPLIT_RE = re.compile(r"([-_])")


@dataclass(frozen=True)
class DocIdentity:
    """The doc-identity verdict for one (stem, text) pair. Evidence, not a gate."""

    doc_class: str                 # STRONG | WEAK | MISS | unverifiable
    tier: int | None = None        # 1 | 2 | 3, None when unverifiable/MISS
    depth: int | None = None       # trailing-strip depth (0 for tiers 1/2)
    ratio: float | None = None     # matched head length / stem length
    head: str | None = None        # the head that matched (diagnostic)

    def to_provenance(self) -> dict:
        """The persisted shape — the four fields of the Design of Record."""
        return {
            "class": self.doc_class,
            "tier": self.tier,
            "depth": self.depth,
            "ratio": self.ratio,
        }


def compute_heads(stem: str) -> list[str]:
    """Progressive two-token trailing strip of `stem`, longest first.

    `rp2040_stamp` -> `['rp2040_stamp', 'rp2040']`
    `ds-000292-icm-42605-v1.7_0` -> the full stem, then one head per 2-token strip.
    """
    tokens = _TOKEN_SPLIT_RE.split(stem)
    heads: list[str] = []
    end = len(tokens)
    while end >= 1:
        heads.append("".join(tokens[:end]))
        if end >= 3:
            end -= 2
        else:
            break
    return heads


def deepest_valid_head(stem: str) -> str:
    """The shortest head still >= MIN_HEAD_CHARS — the family-group key.

    Two stems backed by the same PDF that disagree on this are, for the tier-3b
    signal, plausibly different parts sharing one document (a family sheet) rather
    than one part under two names. Used by `tools/doc_identity_audit.py`.
    """
    heads = compute_heads(stem)
    valid = [h for h in heads if len(h) >= MIN_HEAD_CHARS]
    return valid[-1] if valid else heads[0]


def base_stem(stem: str) -> str:
    """The tier-2 candidate: `stem` with one trailing package suffix stripped."""
    return _BASE_SUFFIX_RE.sub("", stem.upper()).strip()


def classify(stem: str | None, text: str | None) -> DocIdentity:
    """Classify the doc-identity relationship between a cache stem and PDF text.

    `text` is the document's extracted text (pdftotext / pdfplumber-full — the
    PRE-slice text, never a section slice: a slice would false-MISS any part whose
    name appears only outside the selected windows). `None`/empty text means there
    is nothing to test against -> `unverifiable`, never MISS.
    """
    if not stem or not text or not text.strip():
        return DocIdentity(UNVERIFIABLE)

    stem_l = stem.lower()
    text_l = text.lower()

    # ── tier 1: raw stem present ─────────────────────────────────────────────
    if stem_l in text_l:
        return DocIdentity(STRONG, tier=1, depth=0, ratio=1.0, head=stem_l)

    # ── tier 2: package-suffix-stripped stem present ─────────────────────────
    base = base_stem(stem)
    if len(base) >= MIN_BASE_STEM_CHARS and base.lower() in text_l:
        ratio = round(len(base) / len(stem), 3) if stem else None
        return DocIdentity(STRONG, tier=2, depth=0, ratio=ratio, head=base.lower())

    # ── tier 3: progressive two-token trailing strip ─────────────────────────
    heads = compute_heads(stem_l)
    for depth in range(1, len(heads)):
        head = heads[depth]
        if len(head) < MIN_HEAD_CHARS:
            break
        if head in text_l:
            ratio = round(len(head) / len(stem), 3)
            cls = STRONG if (depth == 1 and ratio >= STRONG_STEM_RATIO) else WEAK
            return DocIdentity(cls, tier=3, depth=depth, ratio=ratio, head=head)

    return DocIdentity(MISS)


# ── evidence-label qualifiers (TODO-337 Phase 2b-iii) ─────────────────────────
#
# A finding's `evidence_label` cites a spec that came out of one datasheet cache.
# When that cache's doc-identity class is not positive, the label says so — in
# fixed, deterministic text. This is ANNOTATION ONLY: no status, severity,
# confidence or verdict is touched, and the qualifier never reaches the LLM
# explanation path (see `step_10_report._supply_explain` — the prompt is formatted
# from the raw label, before qualification).
#
# STRONG and WEAK add nothing: both are real positives (the stem, or a
# discriminating head of it, was found in the document's own text).

QUALIFIER_MISS = "; source-text MPN match: none"
QUALIFIER_UNVERIFIABLE = "; source document unverifiable"

# MISS outranks unverifiable: a measured negative is more informative than
# "nothing could be measured". Only consulted when one finding cites several
# caches (the M2 output-conflict bucket, whose evidence names ≥2 drivers).
_QUALIFIER_PRECEDENCE = (MISS, UNVERIFIABLE)
_QUALIFIERS = {MISS: QUALIFIER_MISS, UNVERIFIABLE: QUALIFIER_UNVERIFIABLE}

_ABSENT_LOGGED = False


def _log_absent_once() -> None:
    """A cache with no `doc_identity` key at all — pre-2b-ii, or an unresolved part.
    Expected and inert (no qualifier); logged ONCE at debug so a corpus run does not
    emit one line per finding."""
    global _ABSENT_LOGGED
    if not _ABSENT_LOGGED:
        _ABSENT_LOGGED = True
        _logger.debug(
            "[doc-identity] some cited caches carry no provenance.doc_identity key "
            "— no qualifier applied for those (logged once per process)")


def _reset_absent_log() -> None:
    """Test hook — restores the once-only debug latch."""
    global _ABSENT_LOGGED
    _ABSENT_LOGGED = False


def qualifier_for(doc_identity: dict | None) -> str | None:
    """The qualifier text for one cache's stored `doc_identity`, or None.

    None is returned for STRONG, WEAK, an absent key, and any unrecognised class —
    the label is then emitted byte-identically to how it was before this layer.
    """
    if not doc_identity:
        _log_absent_once()
        return None
    return _QUALIFIERS.get(doc_identity.get("class"))


def qualify(label: str | None, doc_identity: dict | None) -> str | None:
    """`label` with the cited cache's qualifier appended, or `label` unchanged."""
    q = qualifier_for(doc_identity)
    if not q or not label:
        return label
    return f"{label}{q}"


def qualify_many(label: str | None, doc_identities) -> str | None:
    """As `qualify`, for a finding citing several caches — appends at most ONE
    qualifier, the highest-precedence class present among them."""
    classes = {(d or {}).get("class") for d in doc_identities if d}
    if not classes:
        _log_absent_once()
        return label
    for cls in _QUALIFIER_PRECEDENCE:
        if cls in classes and label:
            return f"{label}{_QUALIFIERS[cls]}"
    return label

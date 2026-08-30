"""Unit tests for the doc-identity evidence layer (TODO-337 Phase 2b-i).

The fixture set is the Phase-1b prediction set: each case below reproduces a real
(stem, document) pair from the 708-cache sweep, with the document text replaced by
a faithful short excerpt of the real PDF's own opening lines (captured with
`pdftotext -l 2`). Expected classes/depths/ratios are the values
`investigation/experiments/todo337_docidentity/todo337_tier3_out_v2.json` records
for those stems — so a drift in this matcher shows up here as a fixture failure,
not silently as a shifted corpus distribution.

No network, no PDF reading, no cache writes.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from steps import doc_identity as di  # noqa: E402


# ── real-document excerpts (pdftotext -l 2 of the linked PDF) ────────────────

# netlist_corpus/datasheets/microchip/atmega32u4-xu.pdf — the family sheet: the
# ordering suffix `-XU` never appears in the text, the base `ATmega32U4` does (82x).
ATMEGA32U4_TEXT = """\
ATmega16U4/ATmega32U4
8-bit Microcontroller with 16/32K bytes of ISP Flash and
USB Controller
DATASHEET
Features
"""

# netlist_corpus/datasheets/microchip/pic12c508a.pdf — DS40139E, the CORRECT
# document for this part (the wrong-document incident of TODO-37 was DS41227D,
# a PIC12F508/509 programming spec with zero `PIC12C508A` mentions).
PIC12C5XX_DS40139E_TEXT = """\
PIC12C5XX
8-Pin, 8-Bit CMOS Microcontrollers
Devices included in this Data Sheet:
• PIC12C508 • PIC12C508A
• PIC12C509 • PIC12C509A
"""

# netlist_corpus/datasheets/misc/SC0914_13_.pdf (byte-identical to
# raspberrypi/rp2040_stamp.pdf) — the RP2040 silicon datasheet. `SC0914` is
# Raspberry Pi's own orderable MPN for the RP2040 QFN and appears in-text.
RP2040_TEXT = """\
RP2040 Datasheet
Colophon
© 2020-2025 Raspberry Pi Ltd (formerly Raspberry Pi (Trading) Ltd.)
Ordering information: SC0914(13) RP2040 QFN-56
"""

# netlist_corpus/datasheets/misc/SLB9673AU20FW2610XTMA1.pdf — the RIGHT document,
# but the part number is SPACED in the extracted text (`SL B 9673`), so no
# substring of the stem can ever match. Documented known limit, not a bug.
SLB9673_TEXT = """\
OPTIGA™ TPM
SL B 9673 TPM2.0
Data Sheet
Devices
SLB 9673XU2.0 FW26.xx
"""


# ── tier 1: raw stem present ─────────────────────────────────────────────────

def test_tier1_raw_hit_pic12c508a():
    r = di.classify("pic12c508a", PIC12C5XX_DS40139E_TEXT)
    assert r.doc_class == di.STRONG
    assert r.tier == 1
    assert r.depth == 0
    assert r.ratio == 1.0


# ── tier 2: package-suffix-stripped stem present ─────────────────────────────

def test_tier2_package_suffix_strip():
    # STM32F103C8T6 -> STM32F103C8 (the one shape BASE_SUFFIX_RE does strip)
    r = di.classify("stm32f103c8t6", "STM32F103C8 Medium-density performance line")
    assert r.doc_class == di.STRONG
    assert r.tier == 2
    assert r.depth == 0


def test_tier2_floor_rejects_short_base():
    # A base below MIN_BASE_STEM_CHARS is not a real MPN and must not be tested.
    assert di.MIN_BASE_STEM_CHARS == 4
    r = di.classify("abc1", "abc appears here but is only three chars")
    assert r.doc_class == di.MISS


# ── tier 3: progressive two-token trailing strip ─────────────────────────────

def test_tier3_strong_depth1_atmega32u4_xu():
    """The ordering-suffix case the resolution-layer BASE_SUFFIX_RE cannot strip."""
    r = di.classify("atmega32u4-xu", ATMEGA32U4_TEXT)
    assert r.doc_class == di.STRONG
    assert r.tier == 3
    assert r.depth == 1
    assert r.ratio == 0.769          # len("atmega32u4") / len("atmega32u4-xu")
    assert r.head == "atmega32u4"


def test_tier3_weak_rp2040_stamp_ratio_below_bar():
    """Depth 1 but the head covers only half the stem -> WEAK, not STRONG."""
    r = di.classify("rp2040_stamp", RP2040_TEXT)
    assert r.doc_class == di.WEAK
    assert r.tier == 3
    assert r.depth == 1
    assert r.ratio == 0.5            # 6/12, just under STRONG_STEM_RATIO


def test_tier3_weak_sc0914_depth2_at_the_bar():
    """Ratio is exactly at the bar, but the hit is at depth 2 -> WEAK."""
    r = di.classify("sc0914_13_", RP2040_TEXT)
    assert r.doc_class == di.WEAK
    assert r.tier == 3
    assert r.depth == 2
    assert r.ratio == 0.6            # 6/10 == STRONG_STEM_RATIO, but depth != 1
    assert r.head == "sc0914"


def test_strong_bar_is_a_named_constant():
    assert di.STRONG_STEM_RATIO == 0.60
    assert di.MIN_HEAD_CHARS == 5


# ── documented known limit ───────────────────────────────────────────────────

def test_known_limit_spaced_mpn_is_a_miss():
    """SLB9673: RIGHT document, MISS verdict.

    The part number is spaced in the extracted text (`SL B 9673`), so no
    contiguous substring of the stem can match at any strip depth. This is a
    known limit of substring matching, recorded here so the behaviour is
    intentional and visible — NOT evidence of a wrong document.
    """
    r = di.classify("slb9673au20fw2610xtma1", SLB9673_TEXT)
    assert r.doc_class == di.MISS
    assert r.tier is None


# ── unverifiable: nothing to test against ────────────────────────────────────

@pytest.mark.parametrize("text", [None, "", "   \n  "])
def test_unverifiable_when_no_text(text):
    r = di.classify("rp2040_stamp", text)
    assert r.doc_class == di.UNVERIFIABLE
    assert r.tier is None


def test_unverifiable_when_no_stem():
    assert di.classify(None, RP2040_TEXT).doc_class == di.UNVERIFIABLE
    assert di.classify("", RP2040_TEXT).doc_class == di.UNVERIFIABLE


def test_unverifiable_is_distinct_from_miss():
    """A real negative and an unmeasurable one must never collapse together."""
    assert di.UNVERIFIABLE != di.MISS
    assert di.classify("rp2040_stamp", None).doc_class != di.MISS


# ── head computation ─────────────────────────────────────────────────────────

def test_compute_heads_strips_two_tokens_at_a_time():
    assert di.compute_heads("rp2040_stamp") == ["rp2040_stamp", "rp2040"]
    assert di.compute_heads("sc0914_13_") == ["sc0914_13_", "sc0914_13", "sc0914"]
    assert di.compute_heads("rp2040") == ["rp2040"]


def test_min_head_chars_stops_the_descent():
    # `abc-de-fg`: depth-1 head `abc-de` (6 chars) is tested and absent; depth-2
    # head `abc` (3 chars) is below MIN_HEAD_CHARS and must NOT be tested even
    # though it IS present in the text.
    r = di.classify("abc-de-fg", "abc appears on its own in this document")
    assert r.doc_class == di.MISS


# ── persisted shape ──────────────────────────────────────────────────────────

def test_to_provenance_shape():
    p = di.classify("atmega32u4-xu", ATMEGA32U4_TEXT).to_provenance()
    assert set(p) == {"class", "tier", "depth", "ratio"}
    assert p["class"] == "STRONG"
    assert p["tier"] == 3 and p["depth"] == 1 and p["ratio"] == 0.769


def test_to_provenance_class_vocabulary():
    for text, expected in ((None, "unverifiable"), (SLB9673_TEXT, "MISS")):
        assert di.classify("slb9673au20fw2610xtma1", text).to_provenance()["class"] == expected

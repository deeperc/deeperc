"""Extraction input-path + prompt template — moved from repo-root
batch_extract_with_claude.py so the extractor (steps/extractors.py) is
package-internal and importable without a repo-root sys.path hack (LT-31).
"""

import re
import statistics
from pathlib import Path


# Prompt template — markdown content is inlined so Claude reasons over the same
# text the pipeline uses, not a fresh re-parse of the PDF.
EXTRACTION_PROMPT_TEMPLATE = """\
Extract pin electrical specifications from the {part_number} datasheet markdown below.

The markdown was generated from the PDF by MinerU and pdfplumber. Focus on the
Absolute Maximum Ratings, Recommended Operating Conditions, and DC Characteristics
sections. Group pins that share identical electrical specs together.

For SUPPLY voltage extraction (critical):
- Look in "Recommended Operating Conditions" or "Operating Conditions" for the
  nominal supply range (e.g. VCC = 2.0V to 3.6V)
- Look in "Absolute Maximum Ratings" for the supply damage threshold
- For split-supply devices (VCCA/VCCB), give separate groups per supply rail
- Supply voltage range applies to supply pins (VCC/VDD) NOT signal pins
- A voltage rating of a passive or discrete part (capacitor/resistor rated voltage, LED or diode VF or VR,
  VRRM, MOSFET VDS or VGS, transistor VCE or VBE, connector or contact rating) is not a pin specification:
  leave supply_min, supply_max, supply_abs_max AND absolute_max_voltage null for it.
- An input, common-mode or differential voltage range is not a supply range: it never fills supply_*.

For SIGNAL voltage extraction:
- VIH_min: minimum input voltage that registers as logic HIGH (lower bound)
- VIL_max: maximum input voltage that registers as logic LOW (upper bound)
- VOH_min: minimum output HIGH voltage
- VOL_max: maximum output LOW voltage
- absolute_max_voltage: maximum voltage applicable without damage (from abs max table)

CRITICAL: VIH_min is a LOWER bound. absolute_max_voltage is an UPPER bound.
Never confuse them — they come from different tables.

Numeric fields must be floats in Volts, or null for values not found. Never
compute a formula. For VIH_min, VIL_max, VOH_min, VOL_max and
absolute_max_voltage only:
- If the value is written relative to a supply rail, leave the numeric field null and put ONE expression
  in "<field>_expr". An expression has exactly one of these shapes, with one rail symbol and nothing else
  (no words, units, "@" conditions, ";" lists or footnote markers):
    "<coef>*<RAIL>"        e.g. "0.7*VCC"
    "<RAIL>+<c>"           e.g. "VCC+0.3"
    "<RAIL>-<c>"           e.g. "VDD-0.4"
    "<coef>*<RAIL>+<c>"    e.g. "0.2*VCC+0.9"     (also with "-")
    "<coef>*(<RAIL>-<c>)+<c>"  e.g. "0.41*(VDD-2)+1.3"   (copy the datasheet's arithmetic; never evaluate it)
  A bare number is never an expression: a plain voltage goes in the numeric field. If the text does not
  reduce to one of these shapes, use "<field>_alts" or leave the field null.
- If the value differs by condition (separate rows or columns at different supply voltages, load currents,
  temperature ranges or device grades), leave the numeric field null, do NOT use "_expr", and list every
  row in "<field>_alts" as [{{"condition": "...", "value": <float or null>, "expr": <string or null>, "quote": "verbatim row"}}],
  one entry per condition; each entry's "expr" follows the shapes above.
The _expr and _alts keys are optional; omit them when unused. supply_* fields
never take _expr or _alts.
For non-power groups, leave all four supply_* fields as null.

PINS (required): in "example_pins" list EVERY pin of the group, not a sample. Write each entry as
"<NUMBER>|<NAME>": NUMBER is the package pin number or BGA ball (e.g. "14", "A3") from the datasheet's pin
configuration or pinout table; NAME is the pin name exactly as that table spells it. One entry per physical
pin: a name on several pins gets one entry per pin ("5|GND", "12|GND"). If the datasheet gives no number
for a pin, write "?|<NAME>". If it shows several packages with different numbering, use the package that
{part_number} orders if the datasheet identifies it, otherwise the first pinout table, and name that
package in "applies_to".

QUOTES (required): for every field you fill in -- each numeric field, each <field>_expr, and each
_alts entry -- copy into "quotes" (for _alts, into that entry's "quote") the exact text from the
markdown that holds the value: the whole table row or sentence, from the parameter name or symbol
through the number, character for character, at most about 200 characters. Do not paraphrase,
reformat, fix spacing or merge rows. Several fields may quote the same row.

PROVENANCE: always include the extraction_meta block exactly as shown in the
schema, with "extractor" set to "claude-code", "model" set to the Claude model
you are running as (e.g. claude-opus-4-7), "extracted_at" the current UTC time
in ISO 8601, "source" set to "{source_md}", and "schema_version" 1. Do not
omit this block — it is how provenance is tracked.

Write the result to: {output_path}

Schema:
{{
  "pin_groups": [
    {{
      "group_name": "descriptive name",
      "pin_type": "bidirectional | digital_input | digital_output | power | analog",
      "example_pins": ["1|VCC", "2|GND", ...],
      "applies_to": "brief description of which pins",
      "VIH_min": null,
      "VIL_max": null,
      "VOH_min": null,
      "VOL_max": null,
      "absolute_max_voltage": null,
      "supply_min": null,
      "supply_max": null,
      "supply_abs_max": null,
      "supply_rail_name": null,
      "quotes": {{"<field>": "verbatim row for that field"}}
    }}
  ],
  "default_group": "group_name to use when pin not found in any group",
  "extraction_meta": {{
    "extractor": "claude-code",
    "model": "claude-opus-4-7",
    "extracted_at": "2026-01-01T00:00:00+00:00",
    "source": "{source_md}",
    "schema_version": 1
  }}
}}

--- BEGIN DATASHEET MARKDOWN ---

{markdown_content}

--- END DATASHEET MARKDOWN ---
"""


# Subscript glue (TODO-534): "V3 + X4" -- the TODO-509 pilot-3 "G" config (R1 same-line small-token
# merge, R2 subscript-line merge, exclusions X1-X3) plus X4. 0 = plain page.extract_text() (the
# pre-534 path); >= 1 = glued. Enters the extractor fingerprint as "glue_version".
GLUE_VERSION = 1

_PURENUM = re.compile(r"^[-+]?\d[\d.,]*$")
_SUBTOK = re.compile(r"^[A-Z0-9]{1,4}$")


def _token_geo(line):
    """line (extract_text_lines(return_chars=True, strip=False)) -> per space-separated token
    {"text", "x0", "x1", "top", "size"} (only "x0": None for an empty token), or None when the line's
    chars do not spell its text (rotated / ligature text): such a line takes part in no merge."""
    toks = line["text"].split(" ")
    chars = line["chars"]
    if "".join(c["text"] for c in chars) != "".join(toks):
        return None
    out, k = [], 0
    for t in toks:
        cs, n = [], 0
        while n < len(t):
            cs.append(chars[k])
            n += len(chars[k]["text"])
            k += 1
        out.append({"text": t, "x0": min(c["x0"] for c in cs), "x1": max(c["x1"] for c in cs),
                    "top": min(c["top"] for c in cs), "size": max(c["size"] for c in cs)} if cs else
                   {"text": t, "x0": None})
    return out


def _xblock(host, sub):
    """X1: host is a pure number; X2: digit-led sub onto a host ending in a digit or '.'."""
    return bool(_PURENUM.match(host)) or (bool(re.match(r"\d", sub)) and bool(re.search(r"[\d.]$", host)))


def _x4_block(host, sub):
    """X4 (TODO-534): R1 glues only a [A-Z0-9]{1,4} token onto a host whose first alphanumeric
    character is a letter."""
    first = next((c for c in host if c.isalnum()), "")
    return not (_SUBTOK.fullmatch(sub) and first.isalpha())


def _glue_page(page, *, x4=True, log=None):
    """One page's text with subscripts merged back onto their hosts (APPEND, no space).

    Post-processes extract_text's own lines (page.extract_text_lines(return_chars=True, strip=False),
    whose texts joined by "\\n" equal page.extract_text()), with token geometry from each line's chars.
    log: optional dict; every R1 candidate merge is appended to log["r1"] as
    {page, host, sub, outcome} with outcome kept / blocked_x1 / blocked_x2 / blocked_x3 / blocked_x4."""
    lines = page.extract_text_lines(return_chars=True, strip=False)
    pno = getattr(page, "page_number", None)
    out = []                                          # [(tokens, geo_ok)]
    ltxt = [ln["text"] for ln in lines]
    for li, ln in enumerate(lines):
        fig_near = any("Figure" in t for t in ltxt[max(0, li - 3):li + 4])                    # X3
        geo = _token_geo(ln)
        if geo is None:
            out.append(([{"text": t, "x0": None} for t in ln["text"].split(" ")], False))
            continue
        cur = []
        sizes = [g["size"] for g in geo if g["x0"] is not None]
        med = statistics.median(sizes) if sizes else 0
        for g in geo:
            cand = bool(cur) and g["x0"] is not None and cur[-1]["x0"] is not None and \
                g["size"] <= 0.8 * med and g["x0"] - cur[-1]["x1"] <= 2
            if cand:
                host = cur[-1]["text"]
                if _PURENUM.match(host):
                    outcome = "blocked_x1"
                elif re.match(r"\d", g["text"]) and re.search(r"[\d.]$", host):
                    outcome = "blocked_x2"
                elif x4 and _x4_block(host, g["text"]):
                    outcome = "blocked_x4"
                elif fig_near:
                    outcome = "blocked_x3"
                else:
                    outcome = "kept"
                if log is not None:
                    log.setdefault("r1", []).append({"page": pno, "host": host, "sub": g["text"],
                                                     "outcome": outcome})
                if outcome == "kept":
                    cur[-1] = dict(cur[-1], text=host + g["text"], x1=max(cur[-1]["x1"], g["x1"]))
                    continue
            cur.append(dict(g))
        nonempty = [g for g in cur if g["text"]]
        if out and out[-1][1] and nonempty and all(_SUBTOK.match(g["text"]) for g in nonempty):  # R2
            above = out[-1][0]
            hosts = []
            for g in nonempty:
                cands = [i for i, h in enumerate(above) if h["x0"] is not None and
                         abs(g["x0"] - h["x1"]) <= 3 and 0 < g["top"] - h["top"] <= 0.75 * h["size"] and
                         not _xblock(h["text"], g["text"])]
                if not cands:
                    hosts = None
                    break
                hosts.append(min(cands, key=lambda i: abs(g["x0"] - above[i]["x1"])))
            if hosts and len(set(hosts)) == len(hosts):
                for g, i in zip(nonempty, hosts):
                    above[i] = dict(above[i], text=above[i]["text"] + g["text"])
                continue
        out.append((cur, True))
    return "\n".join(" ".join(g["text"] for g in toks) for toks, _ in out)


def extract_pdfplumber_full(pdf_path: Path) -> str:
    """
    Extract markdown from the entire PDF.

    For batch extraction we want complete coverage — electrical
    characteristics may be anywhere (front for simple chips, page 250+
    for ATmega, etc.) so we extract all pages with page break markers.

    With GLUE_VERSION >= 1 each page goes through _glue_page: pdfplumber emits a
    subscript or trailing unit ("V" + "DD", "t" + "PLH") as its own line or token,
    and the glue appends it back onto its host so the model reads "VDD" (TODO-534).
    GLUE_VERSION 0 is the plain page.extract_text() path.
    """
    import pdfplumber

    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        print(f"  Extracting all {total} pages")

        sections = []
        for page_num in range(1, total + 1):
            page = pdf.pages[page_num - 1]
            text = (_glue_page(page) if GLUE_VERSION >= 1 else page.extract_text()) or ""
            if text.strip():
                sections.append(f"\n<!-- page {page_num} -->\n{text}")

        return ("<!-- generated by batch_extract_with_claude.py via pdfplumber -->\n"
                + "\n".join(sections))

"""Post-extraction validator for one pin_groups draw (TODO-509 fork (d)).

`validate(raw, model_input=..., source_text=..., stem=...)` -> (raw', report). Pure: it deep-copies `raw`,
never raises on bad input (a malformed group is passed through unchanged and counted) and writes nothing.
`run_extractor` (steps/extractors.py) calls it between the model reply and the cache write; the report is
stamped as provenance["validation"] (observed, not hashed).

NOT an era member (not in provenance.CORE_PIPELINE_FILES, not a checker-registry source). It runs at
extraction time, before the cache write, so what it changes is cached DATA, not report logic. Its identity
travels with that data instead: VALIDATOR_VERSION and VALIDATOR_SHA256 (this file's bytes) are fields of
extractors.extractor_fingerprint, so a validator change re-keys the extraction regime the way a template or
max_tokens change does. Making it a member would stale every cached report for a change that cannot move a
verdict until a part re-extracts.

Stages, in order (each counted in the report):
  3-pre F4 BARE-NUMBER COERCION (TODO-534, VALIDATOR_VERSION 3). Per group, before 3a: a bare-number
     `_expr` moves to the numeric field when that is null (its quote, when only the `_expr` has one, becomes
     the field's quote), else is dropped; the `_expr` and its quote are deleted either way. A moved value is
     then cited and rule-checked by 3a/3b like any numeric field.
  3a CITATIONS. Per group, the model's `quotes` ({field: row}) and each `_alts[i].quote` become
     group["citations"][key] = {quote, page, section, match}, key = the field ("VIH_min"), the expression
     key ("VIH_min_expr") or the alts entry ("VIH_min_alts[0]"). `quotes` and each entry's `quote` are then
     deleted. match is one of verbatim / layout / partial / foreign / no-quote, the pilot match classes
     (investigation/experiments/todo509_forkd_pilot2/c2.py, which refines todo509_forkd_pilot/diag_v2.py):
       verbatim  N(quote) is a substring of N(model input), N = NFKC, dashes to "-", whitespace collapsed;
       layout    some window of <= 4 consecutive non-empty input lines that holds the value contains every
                 token of N(quote), a token matching one window token or two ADJACENT window tokens glued;
       partial   the best such window holds >= 75% of the quote tokens;
       foreign   otherwise; no-quote: no quote for the field and no sibling quote.
     Q1 sibling fallback: a field without its own quote borrows the quote of another field of the same group
     whose value is equal (counted). It only derives match/page/section; nothing is nulled on match.
     page = the nearest `<!-- page N -->` marker above the cited window's value line (for a section-slice
     input with no marker above it, the quote is located in `source_text` instead); section = the nearest
     in-text caption ("Table N." / a numbered heading) within 60 lines above that line, else None. VIH_source / abs_max_source are
     re-derived from the VIH_min / absolute_max_voltage citations (section, else "p. N", else None).
  3b HARD RULES on each numeric value, over value-holding window lines: the windows the field's OWN quote
     matched; when it has no own quote or that quote matches none, every window holding the value (a
     sibling quote never selects the windows); no window at all -> pass:
     (a) expression guard: the value must occur on some line other than as the coefficient or offset of a
         rail symbol (steps.step_05_validator.parse_rail_expr decides the expression);
     (f) column binding: on a line with >= 2 numbers the value must sit in a column whose nearest MIN/TYP/MAX
         header (+ part-family grade tokens) agrees with the field's side and the stem's grade; no resolvable
         header -> UNDECIDABLE -> pass.
     A failure nulls the numeric field; its citation gets status "nulled:a" / "nulled:f" and
     rejected_value. (b) row-label / pin binding (with the except / other-than exclusion) is computed and
     reported only (report["b_flags"]); it never nulls.
  3c F4 RAILLESS NULLING (the other half of F4; the bare-number half is 3-pre). An
     `_expr` or `_alts[i].expr` with no usable rail symbol (a bare rail name such as "ADC_AVDD", or rail-less
     arithmetic such as "1.2+0.215") is nulled.
  3d POST-FILTERS: RE-TYPE (example_pins naming a supply rail on a non-power group carrying supply_*) ->
     NA FILTER (fields that do not apply to the group's pin_type; GROUND groups keep absolute_max_voltage
     only) -> RATING GUARD (discrete / passive ratings never fill supply_* or absolute_max_voltage) ->
     504 CHECKS (default_group naming a missing or power group; blanket example_pins; null example_pins).
  example_pins entries may be "<num>|<name>" pairs (TODO-488): every name read above (RE-TYPE, the 504
  blanket drop, the GROUND test, rule (b)'s own/other names) uses the name half (entry_name). An entry with
  "|" and an empty half is counted as example_pins_malformed and kept.
"""
from __future__ import annotations

import bisect
import copy
import hashlib
import re
import unicodedata
from pathlib import Path

from steps.step_05_validator import parse_rail_expr

VALIDATOR_VERSION = "3"
VALIDATOR_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

LOGIC = ("VIH_min", "VIL_max", "VOH_min", "VOL_max")
EXPR_FIELDS = LOGIC + ("absolute_max_voltage",)          # the fields that may carry _expr / _alts
SUPPLY = ("supply_min", "supply_max", "supply_abs_max")
NUMERIC = EXPR_FIELDS + SUPPLY
RATED = SUPPLY + ("absolute_max_voltage",)
MATCH_CLASSES = ("verbatim", "layout", "partial", "foreign", "no-quote")
WIN = 4                 # window: at most this many consecutive non-empty lines
PARTIAL_FRAC = 0.75
SECTION_REACH = 60      # a caption further above than this is not the cited window's heading

# ------------------------------------------------------------------ normalisation and numbers
_DASH_N = str.maketrans({"–": "-", "—": "-", "−": "-"})
_DASHES = str.maketrans({"–": "-", "−": "-", "—": "-", "′": "'"})
_PAGE = re.compile(r"<!--\s*page\s+(\d+)\s*-->")
NUM_RE = re.compile(r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?![\w.])")
# NUM_RE extended to numbers glued to a unit ("6.0V") and after leader dots ("....6.0V").
NUM_X = re.compile(r"(?<![\w])(?<!\d\.)[-+]?\d+(?:\.\d+)?(?=(?:V|mV|mA|µA|μA|°C)?(?![\w.]|\.\d))")
# expression numbers: a digit glued to a letter belongs to a rail name (VDD1), not to the expression
_EXPR_NUM = re.compile(r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?(?![0-9]|\.\d)")
# quote/line numbers: a coefficient may be glued to a multiplication sign ("0.7xV DDIO")
_Q_NUM = re.compile(r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?(?=(?:[xX×*]|V|mV)?(?![A-Za-z0-9.]|\.\d)|[xX×*])")


def N(s) -> str:
    s = unicodedata.normalize("NFKC", str(s)).translate(_DASH_N)
    return re.sub(r"\s+", " ", s).strip()


def _num_readings(text):
    for m in NUM_X.finditer(text):
        x = float(m.group())
        yield x
        s = m.start()
        if not m.group().startswith(("-", "+")) and s > 0 and text[s - 1] == "-":
            yield -x


def _value_in(text, v):
    return any(abs(x - v) < 1e-6 for x in _num_readings(text))


def occurrences(line, v):
    """sign-aware occurrences of v on the line -> [(start, end)]; a '-', en dash or minus sign directly
    before the digits makes the number negative."""
    out = []
    for m in NUM_X.finditer(line):
        x = float(m.group())
        s = m.start()
        if not m.group().startswith(("-", "+")) and s > 0 and line[s - 1] in "–−-":
            x = -x
        if abs(x - v) < 1e-6:
            out.append((m.start(), m.end()))
    return out


# ------------------------------------------------------------------ rail symbols (pilot rules.py)
_SUBS = r"(?:CC|DD|DDIO|DDA|DDO|IO|CCIO|S|BAT|REF|A|BD|IN)"
RAILSYM = (r"(?:\+V(?:[A-Z]{1,3}|\([A-Z]{1,3}\))?(?![a-z])"
           r"|V(?=\s*\))"
           r"|V_?\(?" + _SUBS + r"\)?[A-Z0-9_]*(?![a-z])"
           r"|V\s" + _SUBS + r"(?![A-Za-z0-9])"
           r"|(?:IO|D|A|P|U)V(?:DD|CC)[A-Z0-9_]*|V\+)")
# operand-only form: a bare V counts as a rail only beside an operator; "0.6V" stays a unit
RAILOPND = r"(?:" + RAILSYM + r"|\(V\s*\)|(?<![\d.])V(?![A-Za-z0-9]))"
_OP = r"\s*[+\-–−]\s*"
_MUL = r"\s*[*×xX]\s*"


def _parses_as(span, v):
    s = re.sub(RAILOPND, " RAIL ", span.translate(_DASHES))
    s = s.replace("(", " ( ").replace(")", " ) ")
    f = parse_rail_expr(s)
    if f is None:
        return False
    k, c, _ = f
    return (k != 1.0 and abs(abs(k) - abs(v)) < 1e-6) or bool(c and abs(abs(c) - abs(v)) < 1e-6)


def _expr_constituent(line, span, v):
    """True iff the number at span is the coefficient / offset of a rail symbol on this line."""
    a, b = span
    ns = a + 1 if line[a] in "+-" else a
    left, numtxt, right = line[:ns], line[ns:b], line[b:]
    tests = []
    m = re.search(r"(?:" + RAILOPND + r")\)?" + _OP + r"$", left)
    if m:
        tests.append(left[m.start():] + numtxt)
    m = re.match(_MUL + r"\(?\s*(?:" + RAILOPND + r")", right)
    if m:
        tests.append(numtxt + right[:m.end()])
    m = re.match(r"(?:" + RAILSYM + r")", right)
    if m:
        tests.append(numtxt + " " + right[:m.end()])
    m = re.search(r"(?:" + RAILOPND + r")" + _MUL + r"$", left)
    if m:
        tests.append(left[m.start():] + numtxt)
    return any(_parses_as(t, v) for t in tests)


def _expr_numbers(expr):
    return [float(x) for x in _EXPR_NUM.findall(N(expr))]


def _expr_in_quote(expr, qn):
    ok = all(any(abs(y - x) < 1e-6 for y in (float(z) for z in _Q_NUM.findall(qn))) for x in _expr_numbers(expr))
    m = re.search(RAILSYM, N(expr))
    if m:
        ok = ok and re.sub(r"[\s_(){}]", "", m.group()) in re.sub(r"[\s_(){}]", "", qn)
    return ok


# ------------------------------------------------------------------ captions
_CAP_TABLE = re.compile(r"^\s*Table\s+(\d+(?:[-.]\d+)*)\s*[.:]")
_CAP_SECT = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,3})\.?\s+([A-Z][A-Za-z][A-Za-z0-9 ,/&()\-]{2,70})\**\s*$")
_CAP_EXCLUDE = re.compile(r"Copyright|Submit Document|Feedback|www\.|\bpage\b", re.I)


def caption_of(line):
    """-> ("table", number, text) | ("section", number, text) | None."""
    if line.startswith("[[SECTION"):
        return None
    m = _CAP_TABLE.match(line)
    if m:
        return ("table", m.group(1), line.strip())
    m = _CAP_SECT.match(line)
    if m and not _CAP_EXCLUDE.search(line):
        words = re.findall(r"[A-Za-z]{3,}", m.group(2))
        if words and not re.search(r"\d\s*(?:V|mV|mA|µA|μA|ns|MHz|°C)\b", m.group(2)):
            return ("section", m.group(1), line.strip())
    return None


class Doc:
    """one input text: raw lines, captions, number index, normalised text with a char -> line map, pages."""

    def __init__(self, text: str):
        self.lines = text.split("\n")
        self.caps = [caption_of(ln) for ln in self.lines]
        self.cap_idx = [i for i, c in enumerate(self.caps) if c]
        self.xlines: dict[float, set[int]] = {}
        for i, ln in enumerate(self.lines):
            for m in NUM_X.finditer(ln):
                x = round(float(m.group()), 6)
                self.xlines.setdefault(x, set()).add(i)
                if m.start() > 0 and ln[m.start() - 1] in "–−" and not m.group().startswith(("-", "+")):
                    self.xlines.setdefault(-x, set()).add(i)
        self.nonempty = [i for i, ln in enumerate(self.lines) if ln.strip()]
        self.pos = {i: k for k, i in enumerate(self.nonempty)}
        parts, self.starts, self.lines_of = [], [], []
        p = 0
        for i, ln in enumerate(self.lines):
            n = N(ln)
            if not n:
                continue
            if parts:
                p += 1
            self.starts.append(p)
            self.lines_of.append(i)
            parts.append(n)
            p += len(n)
        self.ntext = " ".join(parts)
        self.pages, cur = [], None
        for ln in self.lines:
            m = _PAGE.search(ln)
            if m:
                cur = int(m.group(1))
            self.pages.append(cur)
        self._win: dict[str, list] = {}

    def find(self, q):
        """first occurrence of normalised q -> list of the line indexes it overlaps, or None."""
        k = self.ntext.find(q) if q else -1
        if k == -1:
            return None
        a = bisect.bisect_right(self.starts, k) - 1
        b = bisect.bisect_right(self.starts, k + len(q) - 1) - 1
        return self.lines_of[a:b + 1]

    def prev_caption(self, i, kind=None):
        for j in range(i, -1, -1):
            c = self.caps[j]
            if c and (kind is None or c[0] == kind):
                return j, c
        return None, None

    def region(self, i):
        s, _ = self.prev_caption(i)
        if s is None:
            return None
        e = next((j for j in self.cap_idx if j > i), len(self.lines))
        return s, e

    def section(self, i):
        j, c = self.prev_caption(i)
        return c[2] if c is not None and i - j <= SECTION_REACH else None

    def holds(self, i, value):
        ln = self.lines[i]
        if isinstance(value, float):
            return bool(occurrences(ln, value)) or i in self.xlines.get(round(value, 6), ())
        nums = _expr_numbers(value)
        if nums:
            return any(abs(float(x) - y) < 1e-6 for x in _Q_NUM.findall(ln) for y in nums)
        m = re.search(RAILSYM, N(value))
        return bool(m) and re.sub(r"[\s_(){}]", "", m.group()) in re.sub(r"[\s_(){}]", "", ln)

    def windows(self, value):
        """every window of <= WIN consecutive non-empty lines holding a line that holds value ->
        [(lines, tokens, token_set, glued_pairs, value_lines)]."""
        key = repr(value)
        if key in self._win:
            return self._win[key]
        if isinstance(value, float):
            cand = set()
            for x in (round(value, 6), round(-value, 6)):
                cand |= self.xlines.get(x, set())
            hold = [i for i in sorted(cand) if i in self.pos and self.holds(i, value)]
        else:
            hold = [i for i in self.nonempty if self.holds(i, value)]
        seen, out = set(), []
        for h in hold:
            k = self.pos[h]
            for s in range(max(0, k - WIN + 1), k + 1):
                w = tuple(self.nonempty[s:s + WIN])
                if w in seen:
                    continue
                seen.add(w)
                wl = N(" ".join(self.lines[i] for i in w)).split()
                glued = {wl[j] + wl[j + 1] for j in range(len(wl) - 1)}
                out.append((list(w), wl, set(wl), glued, [i for i in w if self.holds(i, value)]))
        self._win[key] = out
        return out


# ------------------------------------------------------------------ rule (a): expression guard
def rule_a(doc, i, v):
    line = doc.lines[i]
    occ = occurrences(line, v)
    if not occ:
        return "FAIL"
    return "PASS" if any(not _expr_constituent(line, sp, v) for sp in occ) else "FAIL"


# ------------------------------------------------------------------ rule (b): row label <-> pin (report only)
_EXCL = re.compile(r"\b(?:except(?:\s+for)?|other\s+than|excluding|but\s+not)\b(.*?)(?=[;)\]]|$)", re.I)


def _label_text(line):
    m = NUM_RE.search(line)
    return line[:m.start()] if m else line


def _names_in(text, names):
    t = text.translate(_DASHES)
    return {n for n in names if re.search(r"(?<![A-Za-z0-9])" + re.escape(n) + r"(?![A-Z0-9])", t)}


def rule_b(text, own, other):
    lab = _label_text(text).translate(_DASHES)
    excluded = " ".join(m.group(1) for m in _EXCL.finditer(lab))
    lab = _EXCL.sub(" ", lab)
    if _names_in(excluded, own):
        return "FAIL"
    lab = re.sub(r"\b[A-Za-z0-9+\-]+\s*=\s*\S*", " ", lab)
    lab = re.sub(r"\bto\s+\S+", " ", lab)
    lab = re.sub(r"(?<=[a-z])to(?=[A-Z+\-])\S*", " ", lab)
    o, x = _names_in(lab, own), _names_in(lab, other)
    return "FAIL" if x and not o else "PASS"


# ------------------------------------------------------------------ rule (f): column binding
SIDE = {"VIH_min": "MIN", "VOH_min": "MIN", "supply_min": "MIN",
        "VIL_max": "MAX", "VOL_max": "MAX", "absolute_max_voltage": "MAX", "supply_max": "MAX",
        "supply_abs_max": "MAX"}
_HDR_TOK = re.compile(r"(?<![A-Za-z])(MIN|TYP|NOM|MAX)(?![A-Za-z])\.?", re.I)
_UNIT_TAIL = re.compile(r"^(?:V|mV|µV|μV|mA|µA|μA|nA|pA|A|%|ns|µs|MHz|kHz|°C|Ω|kΩ|pF|mW|W|dB)$")
_FAMILY = re.compile(r"^[A-Z]{1,5}\d{2,}[A-Z0-9\-]*$")
_PLACEHOLDER = {"-", "–", "—", "−", "n/a", "N/A"}


def _header_columns(doc, i):
    """nearest header above line i (within its caption region, else 40 lines) with >= 2 MIN/TYP/MAX
    tokens -> ([(grade|None, side)], header line) or (None, None)."""
    reg = doc.region(i)
    lo = max(reg[0] if reg else 0, i - 40)
    for j in range(i - 1, lo - 1, -1):
        toks = [{"NOM": "TYP"}.get(t.upper(), t.upper()) for t in _HDR_TOK.findall(doc.lines[j])]
        other = re.sub(_HDR_TOK, " ", doc.lines[j])
        if len(toks) >= 2 and not NUM_RE.search(other):
            groups, cur = [], []
            order = {"MIN": 0, "TYP": 1, "MAX": 2}
            for t in toks:
                if cur and order[t] <= order[cur[-1]]:
                    groups.append(cur)
                    cur = []
                cur.append(t)
            groups.append(cur)
            fams = []
            for k in range(j, max(lo, j - 3) - 1, -1):
                for w in re.findall(r"[A-Za-z0-9\-]+", doc.lines[k]):
                    if _FAMILY.match(w) and w not in fams:
                        fams.append(w)
            labels = [None] * len(groups)
            if 2 <= len(fams) <= len(groups):
                labels[len(groups) - len(fams):] = fams          # grades label the LAST groups
            return [(labels[g], s) for g, grp in enumerate(groups) for s in grp], j
    return None, None


def _value_cells(line, note_col=False):
    toks = re.findall(r"\S+", line)
    if note_col and len(toks) >= 2 and (re.fullmatch(r"\d", toks[-1]) or toks[-1] in _PLACEHOLDER) \
            and _UNIT_TAIL.match(toks[-2]):
        toks = toks[:-1]
    while toks and _UNIT_TAIL.match(toks[-1]):
        toks.pop()
    cells = []
    k = len(toks) - 1
    while k >= 0:
        t = toks[k]
        tt = t.translate(_DASHES).lstrip("±")
        if t in _PLACEHOLDER:
            cells.append(None)
        elif re.fullmatch(r"[-+]?\d+(?:\.\d+)?", tt):
            if tt[0] in "+-" and k > 0 and re.fullmatch(RAILOPND, toks[k - 1]):
                cells.append(None)
                k -= 1
            elif k >= 2 and toks[k - 1] == "/" and re.fullmatch(r"[-+]?\d+(?:\.\d+)?", toks[k - 2]):
                cells.append(("/", float(toks[k - 2]), float(tt)))
                k -= 2
            else:
                cells.append(float(tt))
        elif re.fullmatch(r"(?:" + RAILOPND + r")[+\-]\d+(?:\.\d+)?", tt):
            cells.append(None)
        else:
            break
        k -= 1
    return list(reversed(cells))


def _cell_has(cell, v):
    if cell is None:
        return False
    if isinstance(cell, tuple):
        return any(abs(x - v) < 1e-6 for x in cell[1:])
    return abs(cell - v) < 1e-6


def _grade_ok(grade, stem):
    if grade is None:
        return True
    g = grade.lower()
    return g in stem or stem.startswith(g)


def rule_f(doc, i, v, field, stem):
    """-> "SINGLE" (not a multi-number line: pass) | "BOUND" (header resolved, column agrees: pass) |
    "FAIL" | "UNDEC" (no resolvable header / binding: pass)."""
    line = doc.lines[i]
    if len(NUM_X.findall(line)) < 2:
        return "SINGLE"
    cols, j = _header_columns(doc, i)
    if not cols:
        return "UNDEC"
    note = j is not None and bool(re.search(r"\bNotes?\b", " ".join(doc.lines[max(0, j - 2): j + 1]), re.I))
    cells = _value_cells(line, note)
    want = SIDE[field]
    opp = "MAX" if want == "MIN" else "MIN"
    options = [cols, [c for c in cols if c[1] != opp], [c for c in cols if c[1] == want]]
    bind = next((o for o in options if o and len(o) == len(cells)), None)
    if bind is None and len(cells) > len(cols):
        cells, bind = cells[-len(cols):], cols
    if bind is None:
        return "UNDEC"
    return "BOUND" if any(_cell_has(c, v) and side == want and _grade_ok(grade, stem)
                          for c, (grade, side) in zip(cells, bind)) else "FAIL"


# ------------------------------------------------------------------ citations
def _num(x):
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        try:
            return float(x.strip())
        except ValueError:
            return None
    return None


def _nonblank(s):
    return isinstance(s, str) and bool(s.strip())


def _quote_candidates(g, quotes, f, value):
    """-> [(sibling_key|None, quote)]: the field's own quote, else every same-group sibling quote whose
    field holds an equal value."""
    is_expr = isinstance(value, str)
    for k in ((f + "_expr", f) if is_expr else (f,)):
        if _nonblank(quotes.get(k)):
            return [(None, quotes[k])]
    sib = []
    for k, s in sorted(quotes.items()):
        if not _nonblank(s) or k in (f, f + "_expr"):
            continue
        base = k[:-5] if k.endswith("_expr") else k
        if base == f:
            continue
        if is_expr:
            e = g.get(base + "_expr")
            if isinstance(e, str) and e.strip() == value.strip() and g.get(base) is None:
                sib.append((k, s))
        else:
            gv = _num(g.get(base))
            if gv is not None and abs(gv - value) < 1e-6:
                sib.append((k, s))
    return sib


def _eval_quote(doc, q, value, field, stem, own, other):
    """one quote -> {match, windows (matching), window (cited), Q3, pass, rule statuses}."""
    is_expr = isinstance(value, str)
    qn = N(q)
    qt = qn.split()
    r = {"quote": q}
    r["Q3"] = (_expr_in_quote(value, qn) if is_expr else _value_in(qn, value))
    verbatim = bool(qn) and doc.find(qn) is not None
    best, bw, passing = -1.0, None, []
    for w, wl, ws, glued, vl in doc.windows(value):
        frac = sum((t in ws or t in glued) for t in qt) / max(1, len(qt))
        if frac > best:
            best, bw = frac, w
        if frac == 1.0:
            passing.append((w, vl))
    r["match"] = ("verbatim" if verbatim else
                  "layout" if passing else "partial" if best >= PARTIAL_FRAC else "foreign")
    r["passing"] = passing
    r["window"] = passing[0][0] if passing else bw
    ok = False
    for w, vl in passing:
        a = fr = "NA"
        if not is_expr and field in SIDE:
            a = "PASS" if any(rule_a(doc, i, value) == "PASS" for i in vl) else "FAIL"
            fr = "PASS" if any(rule_f(doc, i, value, field, stem) != "FAIL" for i in vl) else "FAIL"
        b = "PASS" if any(rule_b(" ".join(doc.lines[w[0]:i + 1]), own, other) == "PASS" for i in vl) else "FAIL"
        if all(x in ("PASS", "NA") for x in (a, b, fr)):
            ok = True
            r["window"] = w
            break
    r["pass"] = ok and r["Q3"]
    return r


def _cite(doc, src_doc, g, quotes, key, f, value, stem, own, other, rep):
    """-> citation dict for one populated value (also returns the picked evaluation)."""
    cands = _quote_candidates(g, quotes, f, value)
    if not cands:
        return {"quote": None, "page": None, "section": None, "match": "no-quote"}, None
    evals = [(k, _eval_quote(doc, q, value, f, stem, own, other)) for k, q in cands]
    pick = next(((k, e) for k, e in evals if e["pass"]), None)
    if pick is None:
        pick = max(evals, key=lambda ke: (bool(ke[1]["passing"]) + ke[1]["Q3"]))
    k, e = pick
    if k is not None:
        rep["counts"]["sibling_fallback"] += 1
    w = e["window"]
    if w is None:                                   # value in no window: cite where the quote itself sits
        w = doc.find(N(e["quote"]))
        at = w[0] if w else None
    else:                                           # the window's value line: a window may start above
        at = next((i for i in w if doc.holds(i, value)), w[0])     # its page marker / caption
    page = doc.pages[at] if at is not None else None
    if page is None and src_doc is not None:
        hit = src_doc.find(N(e["quote"])) or (src_doc.find(N(doc.lines[at])) if at is not None else None)
        if hit:
            page = src_doc.pages[hit[0]]
    cit = {"quote": e["quote"], "page": page, "section": doc.section(at) if at is not None else None,
           "match": e["match"]}
    if k is not None:
        cit["sibling"] = k
    return cit, e


def _source_label(cit):
    if not cit:
        return None
    if cit.get("section"):
        return cit["section"]
    return f"p. {cit['page']}" if cit.get("page") is not None else None


# ------------------------------------------------------------------ F4 helpers
_BARE_NUM = re.compile(r"^\s*[-+]?\d+(?:\.\d+)?\s*(?:V|mV)?\s*$")
_BARE_RAIL = re.compile(r"^\s*\(?\s*\+?[A-Za-z][A-Za-z0-9_]*[+\-]?\s*\)?\s*$")


def _bare_number(s):
    if not isinstance(s, str) or not _BARE_NUM.match(s):
        return None
    v = float(re.sub(r"\s*(?:V|mV)?\s*$", "", s.strip()))
    return v / 1000 if s.strip().endswith("mV") else v


def _railless(s):
    """a bare rail name ("ADC_AVDD") or rail-less arithmetic ("1.2+0.215") -> True."""
    if not isinstance(s, str) or not s.strip():
        return False
    if _BARE_RAIL.match(s):
        return True
    t = re.sub(r"(?<=[\d.])\s*[mM]?[Vv](?![A-Za-z])", "", s)
    return not re.search(r"[A-Za-z]", t) and bool(re.search(r"\d", t)) and parse_rail_expr(s) is None


# ------------------------------------------------------------------ 3d filters
RETYPE_PIN = re.compile(r"(?i)^\+?(A?VDD[A-Z0-9_]*|VDDA|VCC[A-Z0-9_]*|AVCC|IOVDD|DVDD|VREG_?V?IN|USB_?VDD|"
                        r"VBAT|VBUS|VIN|V\+|VS)$")
GROUND_RE = re.compile(r"(?i)\b(GND|VSS|VSSA|AGND|DGND|PGND|GROUND|EP)\b")
NONGROUND_RE = re.compile(r"(?i)\b(A?VDD[A-Z0-9_]*|VCC[A-Z0-9_]*|AVCC|IOVDD|DVDD)\b|V\+")
RATEW_RE = re.compile(r"(?i)\brat(ed|ing)\b|\bV_?F\b|\bV_?R\b|VRRM|\bV_?DSS?\b|\bV_?GS\b|\bV_?CE\b|\bV_?BE\b|DWV|"
                      r"working|contact|blocking|breakdown|\bVZ\b")
TERM_RE = re.compile(r"(?i)\b(drain|source|gate|anode|cathode|collector|emitter|base)\b")
BLANKET = re.compile(r"(?i)^\s*all\s+(signal\s+)?pins\s*$")


def entry_name(x):
    """The name half of a "<num>|<name>" / "?|<name>" example_pins entry (split on the first "|", both halves
    non-empty), else x unchanged."""
    if isinstance(x, str) and "|" in x:
        num, name = (h.strip() for h in x.split("|", 1))
        if num and name:
            return name
    return x


def is_malformed_entry(x):
    """An example_pins entry with "|" and an empty half ("5|", "|VCC")."""
    return isinstance(x, str) and "|" in x and not all(h.strip() for h in x.split("|", 1))


def _texts(g):
    out = [g.get("group_name"), g.get("supply_rail_name")]
    ex = g.get("example_pins")
    if isinstance(ex, list):
        out += [entry_name(x) for x in ex]
    return [x for x in out if isinstance(x, str)]


def is_ground(g):
    t = _texts(g)
    return (g.get("pin_type") == "power" and any(GROUND_RE.search(x) for x in t)
            and not any(NONGROUND_RE.search(x) for x in t))


def should_retype(g):
    if g.get("pin_type") == "power" or not any(g.get(f) is not None for f in SUPPLY):
        return False
    ex = g.get("example_pins")
    return isinstance(ex, list) and any(isinstance(x, str) and RETYPE_PIN.match(entry_name(x).strip()) for x in ex)


def rating_match(g):
    pt = g.get("pin_type")
    if not (pt in ("power", "analog") or pt is None):
        return None
    for k in ("supply_rail_name", "group_name", "applies_to"):
        if isinstance(g.get(k), str):
            m = RATEW_RE.search(g[k])
            if m:
                return m.group()
    if isinstance(g.get("group_name"), str):
        m = TERM_RE.search(g["group_name"])
        if m:
            return m.group()
    return None


def na_fields(g):
    """-> (rule, fields) the NA filter nulls on this group."""
    if is_ground(g):
        return "ground", [f for f in NUMERIC if f != "absolute_max_voltage"]
    if g.get("pin_type") == "power":
        return "power", list(LOGIC)
    return "non_power", list(SUPPLY)


# ------------------------------------------------------------------ the validator
def _new_report(stem):
    counts = {k: 0 for k in (
        "groups", "malformed_groups", "citations", "sibling_fallback", "nulled_a", "nulled_f",
        "expr_bare_coerced", "expr_bare_moved", "expr_bare_dropped", "expr_railless_nulled",
        "retyped", "na_nulled", "rating_nulled", "default_group_nulled", "example_pins_blanket_dropped",
        "example_pins_null_to_empty", "example_pins_malformed")}
    return {"validator_version": VALIDATOR_VERSION, "validator_sha256": VALIDATOR_SHA256, "stem": stem,
            "counts": counts, "match": {c: 0 for c in MATCH_CLASSES},
            "groups_touched": {k: [] for k in ("a", "f", "f4", "retype", "na", "rating", "504")},
            "nulled": [], "coerced": [], "retyped": [], "b_flags": [], "f_outcomes": []}


def _names(groups, gi):
    own = {entry_name(x) for x in (groups[gi].get("example_pins") or []) if isinstance(x, str) and x.strip()}
    other = set()
    for j, h in enumerate(groups):
        if j != gi and isinstance(h, dict) and isinstance(h.get("example_pins"), list):
            other |= {entry_name(x) for x in h["example_pins"] if isinstance(x, str) and x.strip()}
    return own, other - own


def _touch(rep, stage, name):
    if name not in rep["groups_touched"][stage]:
        rep["groups_touched"][stage].append(name)


def cite_and_rules(g, gi, groups, doc, src_doc, stem, rep):
    """3a + 3b on one group (mutates g)."""
    quotes = g.get("quotes") if isinstance(g.get("quotes"), dict) else {}
    own, other = _names(groups, gi)
    cits = {}
    gname = g.get("group_name")
    for f in NUMERIC:
        v = _num(g.get(f))
        if v is not None:
            cit, e = _cite(doc, src_doc, g, quotes, f, f, v, stem, own, other, rep)
            cits[f] = cit
            # 3b hard rules over the windows the field's OWN quote matched; a sibling-derived quote
            # only derives the citation, so without an own match: every window holding the value
            own_match = e and e["passing"] and "sibling" not in cit
            wins = e["passing"] if own_match else [(w, vl) for w, _, _, _, vl in doc.windows(v)]
            vlines = sorted({i for _, vl in wins for i in vl})
            if not vlines:
                fo = "NO-WINDOW"
                a_fail = f_fail = False
            else:
                a_fail = all(rule_a(doc, i, v) == "FAIL" for i in vlines)
                fs = [rule_f(doc, i, v, f, stem) for i in vlines]
                f_fail = all(x == "FAIL" for x in fs)
                fo = ("NULLED" if f_fail else "PASS-BOUND" if "BOUND" in fs else
                      "PASS-SINGLE" if "SINGLE" in fs else "UNDECIDABLE")
                b = [rule_b(" ".join(doc.lines[w[0]:i + 1]), own, other) for w, vl in wins for i in vl]
                if b and all(x == "FAIL" for x in b):
                    rep["b_flags"].append({"group": gname, "field": f, "value": v})
            rep["f_outcomes"].append({"group": gname, "index": gi, "field": f, "value": v, "outcome": fo,
                                      "a_fail": a_fail})
            rule = "a" if a_fail else "f" if f_fail else None
            if rule:
                g[f] = None
                cit["status"] = f"nulled:{rule}"
                cit["rejected_value"] = v
                rep["counts"][f"nulled_{rule}"] += 1
                rep["nulled"].append({"group": gname, "index": gi, "field": f, "value": v, "rule": rule})
                _touch(rep, rule, gname)
    for f in EXPR_FIELDS:
        e = g.get(f + "_expr")
        if _nonblank(e):
            cits[f + "_expr"], _ = _cite(doc, src_doc, g, quotes, f + "_expr", f, e, stem, own, other, rep)
        alts = g.get(f + "_alts")
        if isinstance(alts, list):
            for k, ent in enumerate(alts):
                if not isinstance(ent, dict):
                    continue
                q = ent.pop("quote", None)
                val = _num(ent.get("value"))
                val = val if val is not None else (ent.get("expr") if _nonblank(ent.get("expr")) else None)
                key = f"{f}_alts[{k}]"
                if val is None:
                    continue
                cits[key], _ = _cite(doc, src_doc, {}, {key: q} if _nonblank(q) else {}, key, key, val, stem,
                                     own, other, rep)
    for cit in cits.values():
        rep["match"][cit["match"]] += 1
        rep["counts"]["citations"] += 1
    g.pop("quotes", None)
    g["citations"] = cits
    g["VIH_source"] = _source_label(cits.get("VIH_min") or cits.get("VIH_min_expr"))
    g["abs_max_source"] = _source_label(cits.get("absolute_max_voltage") or cits.get("absolute_max_voltage_expr"))


def f4_bare(g, rep):
    """3-pre on one group (mutates g): the bare-number half of F4, run before cite_and_rules."""
    gname = g.get("group_name")
    quotes = g.get("quotes") if isinstance(g.get("quotes"), dict) else None
    for f in EXPR_FIELDS:
        e = g.get(f + "_expr")
        bv = _bare_number(e)
        if bv is None:
            continue
        rep["counts"]["expr_bare_coerced"] += 1
        moved = g.get(f) is None
        if moved:
            g[f] = bv
            if quotes is not None and f + "_expr" in quotes and f not in quotes:
                quotes[f] = quotes[f + "_expr"]
        rep["counts"]["expr_bare_moved" if moved else "expr_bare_dropped"] += 1
        rep["coerced"].append({"group": gname, "field": f, "expr": e, "moved": moved})
        del g[f + "_expr"]
        if quotes is not None:
            quotes.pop(f + "_expr", None)
        _touch(rep, "f4", gname)


def f4(g, rep):
    """3c on one group (mutates g): railless nulling (the bare-number half is f4_bare)."""
    gname = g.get("group_name")
    for f in EXPR_FIELDS:
        e = g.get(f + "_expr")
        if _railless(e):
            g[f + "_expr"] = None
            rep["counts"]["expr_railless_nulled"] += 1
            rep["nulled"].append({"group": gname, "field": f + "_expr", "value": e, "rule": "f4_railless"})
            _touch(rep, "f4", gname)
        alts = g.get(f + "_alts")
        if isinstance(alts, list):
            for k, ent in enumerate(alts):
                if isinstance(ent, dict) and _railless(ent.get("expr")):
                    rep["nulled"].append({"group": gname, "field": f"{f}_alts[{k}].expr", "value": ent["expr"],
                                          "rule": "f4_railless"})
                    ent["expr"] = None
                    rep["counts"]["expr_railless_nulled"] += 1
                    _touch(rep, "f4", gname)


def _null(g, fields, rep, stage, counter):
    n = 0
    cits = g.get("citations") or {}
    for f in fields:
        if g.get(f) is not None:
            if f in cits:
                cits[f].setdefault("status", f"nulled:{stage}")
            g[f] = None
            n += 1
        for k in (f + "_expr", f + "_alts"):
            if k in g:
                if g[k] not in (None, [], ""):
                    n += 1
                del g[k]
    if n:
        rep["counts"][counter] += n
        _touch(rep, stage, g.get("group_name"))
    return n


def retype(g, rep):
    if should_retype(g):
        rep["retyped"].append({"group": g.get("group_name"), "old_pin_type": g.get("pin_type"),
                               "example_pins": g.get("example_pins")})
        g["pin_type"] = "power"
        rep["counts"]["retyped"] += 1
        _touch(rep, "retype", g.get("group_name"))


def na_filter(g, rep):
    _null(g, na_fields(g)[1], rep, "na", "na_nulled")


def rating_guard(g, rep):
    if rating_match(g) is not None:
        _null(g, RATED, rep, "rating", "rating_nulled")


def checks_504(out, rep):
    groups = [g for g in out.get("pin_groups") or [] if isinstance(g, dict)]
    dg = out.get("default_group")
    if dg is not None:
        tgt = [g for g in groups if g.get("group_name") == dg]
        if not tgt or all(g.get("pin_type") == "power" for g in tgt):
            out["default_group"] = None
            rep["counts"]["default_group_nulled"] += 1
            _touch(rep, "504", f"default_group:{dg}")
    for g in groups:
        ex = g.get("example_pins")
        if ex is None or "example_pins" not in g:
            g["example_pins"] = []
            rep["counts"]["example_pins_null_to_empty"] += 1
            continue
        if isinstance(ex, list):
            keep = [x for x in ex if not (isinstance(x, str) and BLANKET.match(entry_name(x)))]
            if len(keep) != len(ex):
                rep["counts"]["example_pins_blanket_dropped"] += len(ex) - len(keep)
                _touch(rep, "504", g.get("group_name"))
                g["example_pins"] = keep


def _per_group(out, fn, rep):
    groups = out.get("pin_groups")
    if not isinstance(groups, list):
        return
    for gi, g in enumerate(groups):
        if not isinstance(g, dict) or gi in rep["_bad"]:
            continue
        work = copy.deepcopy(g)
        try:
            fn(work, gi, groups)
        except Exception as exc:                    # malformed group: passed through unchanged, counted
            rep["_bad"].add(gi)
            rep["counts"]["malformed_groups"] += 1
            rep.setdefault("malformed", []).append({"index": gi, "error": f"{type(exc).__name__}: {exc}"})
            continue
        groups[gi] = work


def validate(raw: dict, *, model_input: str, source_text: str | None, stem: str) -> tuple[dict, dict]:
    """-> (raw', report). raw is never mutated; any failure leaves the affected group as it was."""
    rep = _new_report(stem)
    rep["_bad"] = set()
    try:
        out = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    except Exception:
        out = raw
    if not isinstance(out, dict) or not isinstance(out.get("pin_groups"), list):
        rep.pop("_bad")
        rep["counts"]["malformed_groups"] += 1
        return (raw if not isinstance(out, dict) else out), rep
    groups = out["pin_groups"]
    rep["counts"]["groups"] = len(groups)
    for gi, g in enumerate(groups):
        if not isinstance(g, dict):
            rep["_bad"].add(gi)
            rep["counts"]["malformed_groups"] += 1
        elif isinstance(g.get("example_pins"), list):
            rep["counts"]["example_pins_malformed"] += sum(map(is_malformed_entry, g["example_pins"]))
    try:
        doc = Doc(model_input if isinstance(model_input, str) else "")
        src_doc = Doc(source_text) if isinstance(source_text, str) and source_text else None
    except Exception:
        doc, src_doc = Doc(""), None
    stem_l = str(stem or "").lower()
    _per_group(out, lambda g, gi, gs: f4_bare(g, rep), rep)
    _per_group(out, lambda g, gi, gs: cite_and_rules(g, gi, gs, doc, src_doc, stem_l, rep), rep)
    _per_group(out, lambda g, gi, gs: f4(g, rep), rep)
    _per_group(out, lambda g, gi, gs: retype(g, rep), rep)
    _per_group(out, lambda g, gi, gs: na_filter(g, rep), rep)
    _per_group(out, lambda g, gi, gs: rating_guard(g, rep), rep)
    try:
        checks_504(out, rep)
    except Exception as exc:
        rep.setdefault("malformed", []).append({"index": None, "error": f"504: {type(exc).__name__}: {exc}"})
    rep.pop("_bad")
    return out, rep

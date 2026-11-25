"""Package key vocabulary (TODO-550 D2/D3): one key space for datasheet package
headers, CubeMX Package attributes and board footprints.

Key = "FAMILY-N", N = signal pin count (an exposed pad is not counted). Families
and their aliases (longest alias matched first):

  SOIC (SOIC, SOP, SO)            MSOP (MSOP, VSSOP)        TSSOP (TSSOP, HTSSOP)
  TVSOP                           SSOP                      SOT-23 (SOT-23, SOT23, TSOT-23, IPC SOT95P)
  SC-70 (SC-70, SOT-323, SOT-353 = SC-70-5, SOT-363 = SC-70-6, IPC SOT65P)
  SOT-223   SOT-89
  QFN (QFN, VQFN, WQFN, UQFN, UFQFPN, DHVQFN, MLF, VQFN-MLF)
  DFN (DFN, SON, USON, WSON, VSON, TDSON)
  LQFP (LQFP, TQFP)   DIP (DIP, PDIP)   TO-92   TO-220   BGA   LGA

TI letter designators (D, DBV, DGK, PW, DGG, DGV) are read only from datasheet
headers (decode_header), never from footprints.

pick(footprint, netlist_pin_count, doc_packages) chooses the one document package
a board footprint names, or None (module docstring of pick). Ported from
investigation/experiments/todo550_projection_v2/b2.py with the 2026-11-19 Q1
pin-count rule added. Pure functions, no I/O.
"""
import re
from typing import NamedTuple

FAM = [
    ('SOT-223', ['SOT-?223']), ('SOT-89', ['SOT-?89']),
    ('SC-70', ['SOT-?323', 'SOT-?353', 'SOT-?363', 'SC-?70', 'SOT65P']),
    ('SOT-23', ['TSOT-?23', 'SOT-?23', 'SOT95P']),
    ('TVSOP', ['TVSOP']), ('TSSOP', ['HTSSOP', 'TSSOP']), ('MSOP', ['VSSOP', 'MSOP']), ('SSOP', ['SSOP']),
    ('SOIC', ['SOIC', 'SOP', 'SO']),
    ('QFN', ['VQFN-MLF', 'UFQFPN', 'DHVQFN', 'VQFN', 'WQFN', 'UQFN', 'QFN', 'MLF']),
    ('DFN', ['TDSON', 'USON', 'WSON', 'VSON', 'DFN', 'SON']),
    ('LQFP', ['LQFP', 'TQFP']), ('DIP', ['PDIP', 'DIP']), ('TO-92', ['TO-?92']), ('TO-220', ['TO-?220']),
    ('BGA', ['[A-Z]{0,2}BGA']), ('LGA', ['LGA']),
]
_ALT = sorted(((a, f) for f, al in FAM for a in al), key=lambda x: -len(x[0]))
_ALTS = '|'.join(a for a, _ in _ALT)
RE_FP = re.compile(r'(?<![A-Z0-9])(' + _ALTS + r')(?![A-Z])')
RE_TOK = re.compile(r'^(' + _ALTS + r')(?:-?(\d+))?$')
IMPLIED = {'SOT353': 5, 'SOT363': 6}
NAMEFIRST = {'SOT-23', 'SC-70', 'SOT-223', 'SOT-89', 'TO-92', 'TO-220'}
EP_FAMS = {'QFN', 'DFN', 'SOIC', 'TSSOP', 'MSOP', 'SSOP', 'TVSOP'}
DESIG = {'D': 'SOIC', 'DBV': 'SOT-23', 'DGK': 'MSOP', 'PW': 'TSSOP', 'DGG': 'TSSOP', 'DGV': 'TVSOP'}
UNK_DESIG = {'DDU', 'DCK', 'DCT', 'DCY', 'DDC', 'DRL', 'DRC', 'DSG', 'DRV', 'RGT', 'RPJ', 'DB', 'DW', 'NS', 'DR',
             'DBQ', 'DYY', 'DQE'}
_RE_EP = re.compile(r'-\d*EP(?![A-Z])')


class Footprint(NamedTuple):
    family: str
    n: int | None          # the string's count, or the netlist pin count for a count-less name
    exposed_pad: bool      # the footprint string marks an exposed pad (KiCad "-1EP")
    ipc: bool              # IPC-7351 name (FAMILYpitchPbodyX...-NN): NN may include the pad
    count_from_string: bool


def fold(s) -> str:
    """Fold U+2212 / U+2013 / U+2010 to "-"."""
    return (s or '').replace('−', '-').replace('–', '-').replace('‐', '-')


def fam_of(alias: str) -> str | None:
    for a, f in _ALT:
        if re.fullmatch(a, alias):
            return f
    return None


def _count(fam, alias, rest):  # b2.py _count
    mi = re.match(r'(?:\d+P)?\d+X\d+(?:X\d+)?-(\d+)', rest)
    if mi and (alias.endswith('P') or re.match(r'\d+P', rest) or rest[:1].isdigit()):
        return int(mi.group(1)), True
    if re.sub('-', '', alias) in IMPLIED:
        return IMPLIED[re.sub('-', '', alias)], False
    mc = re.match(r'-(\d+)(?![\d.])', rest)
    if mc:
        return int(mc.group(1)), False
    if fam not in NAMEFIRST:
        mc = re.match(r'(\d+)(?![\d.X])', rest)
        if mc:
            return int(mc.group(1)), False
    return None, False


def decode_footprint(footprint, netlist_pin_count=None) -> Footprint | None:
    """Board footprint -> Footprint(family, n, exposed_pad, ipc, count_from_string), or None
    when no family word is found. `Lib:` is dropped; a count-less family name takes
    n = netlist_pin_count. (b2.py decode_fp)"""
    if not footprint:
        return None
    t = fold(footprint).upper().split(':', 1)[-1]
    m = RE_FP.search(t)
    if not m:
        return None
    alias = m.group(1)
    fam = fam_of(alias)
    n, ipc = _count(fam, alias, t[m.end():])
    from_string = n is not None
    if n is None:
        n = netlist_pin_count
    return Footprint(fam, n, bool(_RE_EP.search(t[m.end():])), ipc, from_string)


def decode_token(tok):
    """A whole header token -> (family, N | None) or None (a bare "SO" and tokens
    shorter than 3 characters excluded). (b2.py decode_token)"""
    t = fold(tok).upper()
    if t == 'SO' or len(t) < 3:
        return None
    m = RE_TOK.match(t)
    if not m:
        return None
    fam = fam_of(m.group(1))
    n = int(m.group(2)) if m.group(2) else IMPLIED.get(re.sub('-', '', m.group(1)))
    return fam, n


UNKNOWN = 'unknown'


def decode_header(text):
    """A datasheet package-header token (a package name or a TI designator) -> [key],
    or UNKNOWN for a known-unknown TI designator, or [] when the token names no
    package. A count-less name gives "FAMILY-?" (the caller resolves N)."""
    t = fold(text).strip('()[],;:')
    d = decode_token(t) if len(t) >= 3 else None
    if d:
        return [key(d[0], d[1])]
    if t in DESIG:
        return [key(DESIG[t], None)]
    if t in UNK_DESIG:
        return UNKNOWN
    return []


def key(fam, n) -> str:
    return '%s-%s' % (fam, n if n is not None else '?')


def key_fam(k: str) -> str:
    return k.rsplit('-', 1)[0]


def key_n(k: str) -> int | None:
    x = k.rsplit('-', 1)[1]
    return int(x) if x.isdigit() else None


def _count_ok(fp: Footprint, k: str, fam_keys) -> bool:
    """Q1: the footprint's signal pin count equals the key's N; N+1 is also accepted for
    an exposed-pad family, and for an IPC name only when the count still names exactly
    one document package of the family."""
    n, kn = fp.n, key_n(k)
    if n is None or kn is None:
        return False
    if n == kn:
        if fp.ipc and fp.family in EP_FAMS:
            hits = [x for x in fam_keys if key_n(x) is not None and key_n(x) in (n, n - 1)]
            return len(hits) == 1
        return True
    if n == kn + 1 and fp.family in EP_FAMS:
        if fp.ipc:
            hits = [x for x in fam_keys if key_n(x) is not None and key_n(x) in (n, n - 1)]
            return len(hits) == 1
        return True
    return False


def pick_reason(footprint, netlist_pin_count, doc_packages) -> tuple[str | None, str]:
    """pick() with the reason: (key | None, reason)."""
    docs = set(doc_packages or ())
    if not docs:
        return None, 'no-document-packages'
    fp = decode_footprint(footprint, netlist_pin_count)
    if fp:
        fk = sorted(k for k in docs if key_fam(k) == fp.family)
        if not fk:
            return None, 'family-not-in-document'
        if len(fk) == 1:
            cand, why = fk[0], 'family-match'
        else:
            c = [k for k in fk if key_n(k) is not None and fp.n is not None and key_n(k) == fp.n]
            if not c and fp.ipc and fp.family in EP_FAMS and fp.n is not None:
                c = [k for k in fk if key_n(k) is not None and key_n(k) + 1 == fp.n]
            if len(c) != 1:
                return None, 'ambiguous'
            cand, why = c[0], 'count-match'
        if not _count_ok(fp, cand, fk):
            return None, 'q1-count-mismatch'
        return cand, why
    if len(docs) == 1:
        # B3 single-package fallback: kept for clarity, but under Q1 a footprint with no
        # family word has no count and can never pass, so this branch never picks.
        return None, 'q1-single-package-fallback'
    return None, 'undecoded-multi'


def pick(footprint, netlist_pin_count, doc_packages) -> str | None:
    """The document package a board footprint names (TODO-550 D3 + Q1), else None.

    - footprint family in the document: that family's one package, or if several the
      one whose N equals the footprint's count (an IPC N+1 count when no N matches);
    - family not in the document, or no family word: None;
    - every pick also needs the Q1 count check (_count_ok).
    """
    return pick_reason(footprint, netlist_pin_count, doc_packages)[0]


def component_package(comp, doc_packages) -> str | None:
    """pick() for a parsed ComponentIR: footprint = comp.footprint, netlist pin count =
    len(comp.pins)."""
    if comp is None or not doc_packages:
        return None
    return pick(getattr(comp, 'footprint', '') or '', len(getattr(comp, 'pins', []) or []), doc_packages)


def cache_packages(pin_groups_result) -> list:
    """extraction_meta.pin_numbers_checked.packages of a loaded cache ([] if absent)."""
    meta = (pin_groups_result or {}).get('extraction_meta') or {}
    pnc = meta.get('pin_numbers_checked') if isinstance(meta, dict) else None
    pk = pnc.get('packages') if isinstance(pnc, dict) else None
    return list(pk) if isinstance(pk, list) else []

"""Spec-lookup matcher: which extracted pin group describes a board pin (TODO-504).

Moved out of step_04b_extractor.py, which re-exports resolve_pin_spec_from_groups
and pin_match_meta, so its callers are unchanged. This module is in
provenance.CORE_PIPELINE_FILES: the group a pin resolves to decides the limits
step_08 and step_08b check it against.

example_pins entries (TODO-488): "<num>|<name>" splits on the FIRST "|" (both
halves stripped, both non-empty) into a pair; "?|<name>" is a name with no
number; anything else is a bare token (the pre-TODO-488 form).

names(pin) = {pin_name if non-empty and != pin_id} U {symbol_name if non-empty
and != pin_id}, where symbol_name is the libparts (symbol) pin name the parser
attaches (read_libpart_pins). A pair's name half is compared to names(pin)
only; a bare token is also compared to pin_id, as before.

Matching stages, in order (name first, number last):

  S1 EXACT  upper() equality of an entry's name with a member of names(pin)
            (bare tokens: also pin_id). Every hit group; the first group in
            file order wins. With symbol_name "" and bare tokens this is the
            pre-TODO-504 rule, unchanged.
  S2 NORM   norm() on both sides (upper; delete "~{", "{", "}"; strip leading
            "/" and "~"; strip whitespace). Counts only if exactly one distinct
            group matches.
  S3 ALT    alternates of each name in names(pin): norm of each "/"-split
            part, plus the name with ONE trailing "( ... )" group stripped,
            plus each "/"-split part of that. Counts only if exactly one
            distinct group (S3 runs only when S2 hit nothing).
  NUMBER    only when S1-S3 found nothing, over the groups holding a pair
            whose norm(num) equals norm(pin_id) (none: "none", as before). A
            pin with a name gets "num_conflict" (no hit) unless the caller
            passes part_pins and the NAME-RELATED bind below holds. A nameless
            pin binds to that group when there is exactly one; in more than one
            it is "num_ambiguous" (no hit).
  NAME-RELATED number bind (TODO-531 N2u; amends TODO-488 J2, which stays the
            rule when the test fails or part_pins is not given). With E = the
            pair entries, over all groups, whose norm(num) == norm(pin_id), a
            named pin binds to that group ("number_name") when ALL hold:
              a. part_pins is given: (pin_id, pin_name, symbol_name) of EVERY
                 pin of the part;
              b. every entry of E sits in exactly one group;
              c. RELATED: some norm(name) a of an entry of E and some b in the
                 alternates of names(pin) have min(len(a), len(b)) >= 2 and one
                 is a prefix or a suffix of the other;
              d. UNIQUE: no entry of E names, by the S1/S2/S3 keys, a pin of
                 part_pins whose pin_id differs from this pin's.
  Fallback  P2 default_group (non-power), P3 first signal group, P4 any
            non-power group, as before; num_conflict / num_ambiguous fall
            through here exactly like "none". A pin resolved here has
            pin_type_exact False.

Normalized stages never pick: when S2 or S3 hits more than one group the pin
does not match, rather than choosing among them (the R-C6/R-C8 principle).

A group whose example_pins is null or missing is treated as having none: it is
never a match target and never raises.
"""
import re


def norm(s) -> str:
    """Upper-case; delete KiCad overbar/subscript markup ("~{", "{", "}"); strip
    leading "/" and "~" (active-low spellings); strip surrounding whitespace."""
    s = str(s).upper().replace("~{", "").replace("{", "").replace("}", "")
    return s.strip().lstrip("/~").strip()


def _examples(group: dict) -> list:
    return group.get("example_pins") or []


_PAREN = re.compile(r"\s*\([^()]*\)\s*$")


def split_entry(x) -> tuple[str, str, str]:
    """-> (kind, num, name). kind "pair" ("<num>|<name>", split on the first "|",
    both halves stripped and non-empty), "name" ("?|<name>"), else "bare" (num ""
    and name = the whole token)."""
    s = str(x)
    if "|" in s:
        num, name = (h.strip() for h in s.split("|", 1))
        if num and name:
            return ("name", "", name) if num == "?" else ("pair", num, name)
    return "bare", "", s


def pin_names(pin_id, pin_name, symbol_name="") -> list:
    """names(pin): the board name and the symbol name, each only if non-empty and
    different from pin_id."""
    pid, out = str(pin_id), []
    for n in (pin_name, symbol_name):
        n = str(n or "")
        if n.strip() and n != pid and n not in out:
            out.append(n)
    return out


def _parsed(group: dict) -> list:
    return [split_entry(p) for p in _examples(group)]


def _distinct(groups: list) -> list:
    seen, out = set(), []
    for g in groups:
        if id(g) not in seen:
            seen.add(id(g))
            out.append(g)
    return out


def _hits_exact(pin_id, pin_name, groups: list, symbol_name="") -> list:
    names = {n.upper() for n in pin_names(pin_id, pin_name, symbol_name)}
    bare = {str(pin_id).upper(), str(pin_name).upper()} | names
    out = []
    for group in groups:
        for kind, _num, name in _parsed(group):
            if name.upper() in (bare if kind == "bare" else names):
                out.append(group)
                break
    return out


def _hits_keys(names: set, bare: set, groups: list) -> list:
    names, bare = {k for k in names if k}, {k for k in bare if k}
    if not names and not bare:
        return []
    return _distinct([g for g in groups
                      if any(norm(name) in (bare if kind == "bare" else names)
                             for kind, _num, name in _parsed(g))])


def _hits_s2(pin_id, pin_name, groups: list, symbol_name="") -> list:
    names = {norm(n) for n in pin_names(pin_id, pin_name, symbol_name)}
    return _hits_keys(names, names | {norm(pin_id), norm(pin_name)}, groups)


def alternates(name) -> set:
    """S3 keys of one name: norm of each "/"-split part, plus the name with ONE
    trailing "( ... )" group stripped, plus each "/"-split part of that."""
    name = str(name)
    out = {norm(a) for a in name.split("/")}
    stripped = _PAREN.sub("", name, count=1).strip()
    if stripped != name.strip() and stripped:
        out.add(norm(stripped))
        out |= {norm(a) for a in stripped.split("/")}
    return {k for k in out if k}


def _hits_s3(pin_id, pin_name, groups: list, symbol_name="") -> list:
    keys = set()
    for n in pin_names(pin_id, pin_name, symbol_name):
        keys |= alternates(n)
    return _hits_keys(keys, keys, groups)


def _hits_number(pin_id, groups: list) -> list:
    pid = norm(pin_id)
    return _distinct([g for g in groups
                      if any(kind == "pair" and norm(num) == pid for kind, num, _name in _parsed(g))])


def _related(a: str, b: str) -> bool:
    return min(len(a), len(b)) >= 2 and (a.startswith(b) or b.startswith(a)
                                         or a.endswith(b) or b.endswith(a))


def _s123_keys(pin_id, pin_name, symbol_name="") -> set:
    """every key S1-S3 would match an entry name on: norm and alternates of names(pin)
    (S1's upper() equality implies norm equality)."""
    keys = set()
    for n in pin_names(pin_id, pin_name, symbol_name):
        keys.add(norm(n))
        keys |= alternates(n)
    return {k for k in keys if k}


def _number_name_bind(pin_id, pin_name, symbol_name, groups: list, part_pins) -> bool:
    """The NAME-RELATED number bind of a named pin (module docstring, a-d)."""
    if part_pins is None or len(_hits_number(pin_id, groups)) != 1:
        return False
    pid = norm(pin_id)
    A = {norm(name) for g in groups for kind, num, name in _parsed(g)
         if kind == "pair" and norm(num) == pid} - {""}
    B = set()
    for n in pin_names(pin_id, pin_name, symbol_name):
        B |= alternates(n)
    if not any(_related(a, b) for a in A for b in B):
        return False
    for q in part_pins:
        q = tuple(q) + ("",) * (3 - len(q))
        if str(q[0]) == str(pin_id):
            continue
        if A & _s123_keys(q[0], q[1], q[2] or ""):
            return False
    return True


def match_groups(pin_id: str, pin_name: str, groups: list, *,
                 symbol_name: str = "", part_pins=None) -> tuple[str, list]:
    """("exact", every S1 group in file order) | ("norm", [group]) |
    ("slash", [group]) (S3) | ("number", [group]) (nameless pin) |
    ("number_name", [group]) (named pin, NAME-RELATED number bind; only with
    part_pins) | ("num_conflict", []) | ("num_ambiguous", []) | ("none", []).
    part_pins: iterable of (pin_id, pin_name, symbol_name) for every pin of the
    part; None (the default) keeps TODO-488 J2: a named pin never binds by number."""
    groups = groups or []
    exact = _hits_exact(pin_id, pin_name, groups, symbol_name)
    if exact:
        return "exact", exact
    s2 = _hits_s2(pin_id, pin_name, groups, symbol_name)
    if len(s2) == 1:
        return "norm", s2
    if not s2:
        s3 = _hits_s3(pin_id, pin_name, groups, symbol_name)
        if len(s3) == 1:
            return "slash", s3
    nums = _hits_number(pin_id, groups)
    if not nums:
        return "none", []
    if pin_names(pin_id, pin_name, symbol_name):
        if _number_name_bind(pin_id, pin_name, symbol_name, groups, part_pins):
            return "number_name", nums
        return "num_conflict", []
    if len(nums) > 1:
        return "num_ambiguous", []
    return "number", nums


def resolve_pin_spec_from_groups(
    pin_id: str,
    pin_name: str,
    pin_groups_result: dict | None,
    *,
    symbol_name: str = "",
    part_pins=None,
) -> dict | None:
    if not pin_groups_result:
        return None

    groups = pin_groups_result.get("pin_groups", []) or []
    default_group_name = pin_groups_result.get("default_group")

    # Priority 1: S1 exact, else S2 normalized, else S3 alternates (TODO-504),
    # else the number stage for a nameless pin (TODO-488) or, with part_pins, a
    # NAME-RELATED number bind of a named pin (TODO-531)
    _kind, hits = match_groups(pin_id, pin_name, groups, symbol_name=symbol_name,
                               part_pins=part_pins)
    if hits:
        return hits[0]

    # Priority 2: default_group — but ONLY if it is not a power group
    if default_group_name:
        for group in groups:
            if (group.get("group_name") == default_group_name
                    and group.get("pin_type") != "power"):
                return group

    # Priority 3: first signal group — never return a power group as fallback
    SIGNAL_TYPES = {"bidirectional", "digital_input", "digital_output", "analog"}
    signal_groups = [g for g in groups
                     if g.get("pin_type", "").lower() in SIGNAL_TYPES]
    if signal_groups:
        return signal_groups[0]

    # Priority 4: absolute last resort — any non-power group
    non_power = [g for g in groups if g.get("pin_type") != "power"]
    if non_power:
        return non_power[0]

    return None


_OPEN_DRAIN_RE = re.compile(r"open[\s_-]?drain", re.IGNORECASE)


def _group_is_open_drain(group: dict | None) -> bool:
    """True when the group's metadata documents it as open-drain (e.g. an
    AP22615 FLG fault flag). An open-drain output sinks only — it never sources
    its VCC onto the net — so it must not be trusted as a push-pull driver that
    asserts an overvoltage."""
    if not group:
        return False
    text = f"{group.get('group_name', '')} {group.get('applies_to', '')}"
    return bool(_OPEN_DRAIN_RE.search(text))


def pin_match_meta(pin_id: str, pin_name: str,
                   pin_groups_result: dict | None, *,
                   symbol_name: str = "", part_pins=None) -> tuple[bool, bool]:
    """Returns (pin_type_exact, open_drain) for the group this pin resolves to.

    - pin_type_exact: the pin POSITIVELY matched a group's `example_pins` (True),
      vs. fell through to the default/first-signal/last-resort fallback (False).
      A pin whose "output" type came from a fallback (e.g. a ULN2003 base input
      landing on the `default_group` that happens to be the collector-output
      group) is NOT a trustworthy push-pull driver.
    - open_drain: the resolved group is documented open-drain.

    step_08 uses both to gate whether an output-typed driver pin may assert an
    OVERVOLTAGE (only a genuine, positively-matched, non-open-drain push-pull
    output can). Trap-proof for real drivers: 74HC595 QA positively matches its
    digital-output group, so its 5V overdrive is preserved.
    """
    if not pin_groups_result:
        return False, False
    kind, hits = match_groups(pin_id, pin_name, pin_groups_result.get("pin_groups", []),
                              symbol_name=symbol_name, part_pins=part_pins)
    if hits:
        return True, _group_is_open_drain(hits[0])
    # No positive match — resolves via fallback; report open-drain of the fallback
    # group for completeness, but pin_type_exact is False.
    grp = resolve_pin_spec_from_groups(pin_id, pin_name, pin_groups_result,
                                       symbol_name=symbol_name, part_pins=part_pins)
    return False, _group_is_open_drain(grp)


def _has_ceiling(g: dict) -> bool:
    return g.get("supply_max") is not None or g.get("supply_abs_max") is not None


def listing_supply_groups(pin_id, pin_name, groups_result, *,
                          symbol_name: str = "", part_pins=None) -> list[dict]:
    """Power groups with a ceiling (supply_max or supply_abs_max) that list this pin
    (step_08b's TODO-484 admission).

    S1 as before: every power group with a ceiling whose example_pins exactly lists
    the pin (match_groups' S1). If S1 lists none, S2 (or, when S2 hits nothing, S3;
    or, when S1-S3 hit nothing, the number stage: a nameless pin's "number" hit or,
    with part_pins, a named pin's "number_name" hit) admits only
    when EVERY group it hits is a power group with a ceiling; step_08b's
    _pick_by_voltage then chooses among them. Otherwise [].
    """
    groups = (groups_result or {}).get("pin_groups", []) or []
    pin_name = pin_name or ""
    exact = _hits_exact(pin_id, pin_name, groups, symbol_name)
    s1 = [g for g in exact if g.get("pin_type") == "power" and _has_ceiling(g)]
    if s1:
        return s1
    hits = (_hits_s2(pin_id, pin_name, groups, symbol_name)
            or _hits_s3(pin_id, pin_name, groups, symbol_name))
    if not hits and not exact:
        kind, hits = match_groups(pin_id, pin_name, groups, symbol_name=symbol_name,
                                  part_pins=part_pins)
        hits = hits if kind in ("number", "number_name") else []
    if hits and all(g.get("pin_type") == "power" and _has_ceiling(g) for g in hits):
        return hits
    return []


# ------------------------------------------------------------------ libparts reader (TODO-488 K4)
def _node(tree, key):
    for item in tree:
        if isinstance(item, list) and item and item[0] == key:
            return item
    return None


def _nodes(tree, key):
    return [item for item in tree if isinstance(item, list) and item and item[0] == key]


def _value(node, key):
    child = _node(node, key) if node else None
    return str(child[1]) if child and len(child) >= 2 else None


def read_libpart_pins(tree) -> dict:
    """(libparts (libpart (lib L) (part P) ... (pins (pin (num N) (name M) ...))))
    of a parsed netlist tree (step_02_parser.parse_sexp output; quoted and unquoted
    atoms parse alike) -> {(L, P): {N: M}}. Names are raw; an empty or "~" name maps
    to "". The first entry for a repeated pin number wins."""
    out = {}
    libparts = _node(tree, "libparts") if isinstance(tree, list) else None
    for lp in _nodes(libparts, "libpart") if libparts else []:
        key = (_value(lp, "lib") or "", _value(lp, "part") or "")
        pins = out.setdefault(key, {})
        pins_node = _node(lp, "pins")
        for p in _nodes(pins_node, "pin") if pins_node else []:
            num = _value(p, "num")
            if num is None or num in pins:
                continue
            name = _value(p, "name") or ""
            pins[num] = "" if name.strip() in ("", "~") else name
    return out

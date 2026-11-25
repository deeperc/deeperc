"""Explicit part-string -> cache-stem bindings (TODO-554) and the V1 normalised
cache-directory rule (TODO-225).

A binding maps one exact part string (after ``str.strip()``) to one cache
directory stem under ``datasheets_parsed/``. The match is exact and
case-sensitive; there is no substring, prefix or normalisation logic in the
binding table itself.

Why the mechanism exists: the resolver's substring lookup cannot place some
part strings. The first entry was an ``MPN(PACKAGE)`` value
("LAN8710A-EZC(QFN32)"): the base suffix rule strips nothing from a string
ending in ")", so the probes still carry "(qfn32)" and match no cache
directory. TODO-225's V1 tier (below) now places that string, so the table was
EMPTY after TODO-225. The mechanism (``bound_stem`` and the first-and-exclusive
read in ``find_cached_dir_for_part``) stays for part strings no rule can place;
TODO-556 decision 20 added the next entry (ESP32-WROOM-32D-4MB).

A bound part never falls through to the substring scan: the resolver reads the
binding first and exclusively (see ``find_cached_dir_for_part``). A bound stem
with no directory is a MISS.

V1 (TODO-225) is a pure, I/O-free rule used by ``step_03_resolver`` as a 4th
cache-directory tier, reached ONLY when tiers 1-3 are a clean miss:
  N(s) = s.casefold() with every character outside [a-z0-9] removed;
  keys = {N(P), N(P with its PAREN_SUFFIX_RE tail removed)}, keys shorter than
  the caller's minimum length dropped;
  matched EXACTLY against N(directory name). One distinct directory is a hit,
  more than one is ambiguous, none is a miss.
It is a cache-directory lookup only; there is no normalised tier in the PDF
lookup. This module must not import ``step_03_resolver`` (which imports this
module), so the minimum key length is an argument.

This module is a CORE_PIPELINE_FILES member (provenance.py), so any change to
BINDINGS or to the V1 rule moves ``checker_code_hash`` and stales old reports,
by design.
"""
import re

# The "(...)" package/grade tail of an MPN(PACKAGE) value. Defined here (moved
# from run_checks.py, TODO-225); run_checks re-exports the same object.
PAREN_SUFFIX_RE = re.compile(r'\s*\(.*\)$')

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]")

# Empty after TODO-225: the LAN8710A-EZC(QFN32) entry was retired when the V1
# tier began placing that string (its quarantined -abc cache left the tree).
# Add an entry only for a part string no rule can place.
# TODO-556 decision 20 (TODO-602 item 2): the Olimex string ESP32-WROOM-32D-4MB is
# the 4 MB flash variant of the module whose cache esp32-wroom-32d was extracted from
# the ESP32-WROOM-32D / 32U datasheet v2.6 (espressif/esp32-wroom-32d.pdf); no rule
# places it (the "-4MB" suffix is not a paren tail).
BINDINGS: dict[str, str] = {
    "ESP32-WROOM-32D-4MB": "esp32-wroom-32d",
}


def bound_stem(part_number: str) -> str | None:
    """Cache directory stem bound to this exact part string, else None."""
    return BINDINGS.get(part_number.strip())


def normalise(s: str) -> str:
    """casefold, then keep only [a-z0-9]."""
    return _NON_ALNUM_RE.sub("", s.casefold())


def v1_keys(part_number: str, min_len: int) -> set[str]:
    """V1 lookup keys: N(p) and N(p with its PAREN_SUFFIX_RE tail removed),
    for p = part_number.strip(); keys shorter than min_len are dropped."""
    p = part_number.strip()
    keys = {normalise(p), normalise(PAREN_SUFFIX_RE.sub("", p))}
    return {k for k in keys if len(k) >= min_len}


def v1_match(part_number: str, names, min_len: int) -> list[str]:
    """Sorted distinct directory names whose N(name) equals a V1 key. The
    result does not depend on the order of ``names``."""
    keys = v1_keys(part_number, min_len)
    if not keys:
        return []
    return sorted({n for n in names if normalise(n) in keys})

"""TODO-569: findings that rest on a MERGED reference designator.

step_02_parser detects a merged refdes (several (comp) records share it, or one of
its pins is a node on more than one (net) entry) and records it on
``NetlistIR.merged_refdes``. This module, called by the checker-registry adapters
around each checker, relabels or drops the findings that rest on such a ref and
builds the not_checked_parts disclosure. Pure functions over the checkers' result
objects; nothing is re-driven or re-evaluated.

  signal / supply                         -> status UNRESOLVABLE, reason MERGED_REFDES
  peripheral / pull-up value / presence   -> severity UNRESOLVABLE, reason MERGED_REFDES
  structural / output conflict            -> row dropped (no UNRESOLVABLE status)

Import direction: this module imports the checker modules for their vocabulary;
no checker module imports it (checker_registry imports both).
"""
from __future__ import annotations

from steps import step_08_checker, step_08b_supply_checker
from steps import step_08d_peripheral_checker, step_08e_pullup_value_checker
from steps import step_08g_pullup_presence

MERGED_REFDES = "MERGED_REFDES"

_LABEL = "Unresolvable — merged reference designator: "


def _merged(ir) -> dict:
    return getattr(ir, "merged_refdes", None) or {}


def netlist_refs(ir) -> set[str]:
    """Every (comp) ref plus every net-node ref."""
    refs = {c.refdes for c in getattr(ir, "components", []) or []}
    for net in getattr(ir, "nets", []) or []:
        refs |= {ref for ref, _ in net.pins}
    return refs


def token_refs(tokens, refs) -> set[str]:
    """Refdes of "REF.PIN" tokens: the longest netlist ref r with
    token.startswith(r + "."); else, for a token holding ".", the text before
    the first "."."""
    out: set[str] = set()
    for t in tokens or []:
        if not isinstance(t, str):
            continue
        best = None
        for r in refs:
            if t.startswith(r + ".") and (best is None or len(r) > len(best)):
                best = r
        if best is None and "." in t:
            best = t.split(".", 1)[0]
        if best is not None:
            out.add(best)
    return out


def _refs_text(hit) -> str:
    return ", ".join(sorted(hit))


def _label(hit) -> str:
    return _LABEL + _refs_text(hit)


def _detail(hit) -> str:
    return ("Not evaluated: " + _refs_text(hit) + " is a merged reference designator "
            "in this netlist (several parts share it, or one of its pins is on more "
            "than one net), so its pin-to-net data cannot be trusted. See "
            "not_checked_parts.")


def _evidence(hit) -> str:
    return _label(hit) + "; this check is not evaluated on this net"


def relabel_signal(results: list, ir) -> list:
    m = _merged(ir)
    if not m:
        return results
    for r in results:
        hit = {x for x in (r.driver_refdes, r.receiver_refdes) if x} & set(m)
        if not hit:
            continue
        r.status = "UNRESOLVABLE"
        r.unresolvable_reason = step_08_checker.SignalUnresolvableReason.MERGED_REFDES
        r.evidence_label = _label(hit)
        r.unresolvable_detail = _detail(hit)
    return results


def relabel_supply(results: list, ir) -> list:
    m = _merged(ir)
    if not m:
        return results
    for r in results:
        if not r.refdes or r.refdes not in m:
            continue
        r.status = "UNRESOLVABLE"
        r.unresolvable_reason = step_08b_supply_checker.SupplyUnresolvableReason.MERGED_REFDES
        r.evidence_label = _label({r.refdes})
    return results


def relabel_peripheral(results: list, ir) -> list:
    m = _merged(ir)
    if not m:
        return results
    refs = netlist_refs(ir)
    for r in results:
        hit = token_refs(r.pins, refs) & set(m)
        if not hit:
            continue
        r.severity = step_08d_peripheral_checker.Severity.UNRESOLVABLE
        r.reason = step_08d_peripheral_checker.FindingReason.MERGED_REFDES
        r.evidence = _evidence(hit)
    return results


def relabel_pullup_value(results: list, ir) -> list:
    m = _merged(ir)
    if not m:
        return results
    for r in results:
        named = {x for x in (r.pins or []) if isinstance(x, str)}
        if r.refdes:
            named.add(r.refdes)
        hit = named & set(m)
        if not hit:
            continue
        r.severity = step_08e_pullup_value_checker.PullupBand.UNRESOLVABLE
        r.reason = MERGED_REFDES
        r.evidence = _evidence(hit)
    return results


def relabel_pullup_presence(results: list, ir) -> list:
    m = _merged(ir)
    if not m:
        return results
    refs = netlist_refs(ir)
    for r in results:
        hit = token_refs(r.pins, refs) & set(m)
        if not hit:
            continue
        r.severity = "UNRESOLVABLE"
        r.reason = step_08g_pullup_presence.REASON_MERGED_REFDES
        r.evidence = _evidence(hit)
    return results


def drop_structural(results: list, ir) -> list:
    m = _merged(ir)
    if not m:
        return results
    results[:] = [r for r in results if not (r.refdes and r.refdes in m)]
    return results


def drop_output_conflict(results: list, ir) -> list:
    m = _merged(ir)
    if not m:
        return results
    results[:] = [r for r in results
                  if not any(isinstance(d, dict) and d.get("refdes") in m
                             for d in (r.drivers or []))]
    return results


def disclosures(ir) -> list[dict]:
    """One not_checked_parts entry per MERGED ref, sorted by ref (decisions 2, 12)."""
    m = _merged(ir)
    first = {}
    for c in getattr(ir, "components", []) or []:
        first.setdefault(c.refdes, c)
    out = []
    for ref in sorted(m):
        x = m[ref]
        kept = first.get(ref)
        out.append({
            "refdes": ref,
            "part_number": kept.part_number if (x.records and kept is not None) else None,
            "checker": "all",
            "reason": MERGED_REFDES,
            "records": x.records,
            "sheet_paths": list(x.sheet_paths),
            "part_numbers": list(x.part_numbers),
            "collided_pins": [{"pin": p, "nets": list(x.collided_pins[p])}
                              for p in sorted(x.collided_pins)],
            "test": x.test,
        })
    return out

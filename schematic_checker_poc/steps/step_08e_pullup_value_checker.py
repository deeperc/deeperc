"""Step 08e — I2C pull-up resistor VALUE-range check (mutation operator M4-pullup).

Deterministic value-range checker: for each I2C pull-up resistor (a resistor
with one pin on an SDA/SCL net and the other on a positive supply rail), parse
its resistance and flag out-of-band values. This is the first *value-range*
checker in the pipeline — prior siblings (08b/08c/08d) are topology/role based.

Sibling of step_08{b,c,d}: consumes the step_02 IR (`.components` + `.nets`),
emits its own findings block aggregated by step_10. It REUSES (imports, does not
fork) step_08d's I2C net-name classification (`_net_signal_hint`, `_I2C_NET_RE`)
and rail recognition (`_is_power_rail_net`), and the shared `resistance_ohms`
value parser (`steps.component_values`).

Thresholds (Nelson's fixed-band spec; mirrors the step_08b overvoltage FAIL vs
within-margin WARN split):
  * FAIL  — resistance outside 250 Ω … 500 kΩ (inclusive band OK)
  * WARN  — resistance outside 1 kΩ … 10 kΩ  (inclusive band OK)
  * PASS  — inside 1 kΩ … 10 kΩ  (no finding emitted; silent on success)
Unparseable value → UNRESOLVABLE for that site (never a silent PASS —
CLAUDE.md failure-handling rule).

On the 18 M4-pullup mutants this makes the `100` (Ω) variant FAIL and the
`100k` variant WARN. Two of the nine seed boards are ACCEPTED-SUSPECT (recon
Step 1 / Fork 4, NOT chased here, corpus deliberately not re-targeted):
  * kit-dev-coldfire-xilinx_5213 — `DSCLK` is a debug/BDM serial clock, not I2C
    (matched the generator's SCL regex); R108 is not a real I2C pull-up.
  * recovery_usb — R280 original value is 200 kΩ, not a plausible pull-up.
These are documented non-clean; ~16/18 is the realistic recall ceiling.
"""

from dataclasses import dataclass, field
from enum import Enum

from steps.component_values import resistance_ohms
# Reuse step_08d primitives (import, do not fork) — I2C net-name classification
# and positive-rail recognition.
from steps.step_08d_peripheral_checker import (
    _net_signal_hint,
    _is_power_rail_net,
    PULLUP_MIN_OHMS,
)

# Fixed-band thresholds (ohms). Bands are inclusive at the boundary.
FAIL_MIN_OHMS = 250.0
FAIL_MAX_OHMS = 500_000.0
WARN_MIN_OHMS = 1_000.0
WARN_MAX_OHMS = 10_000.0


class PullupBand(Enum):
    FAIL         = "FAIL"
    WARN         = "WARN"
    UNRESOLVABLE = "UNRESOLVABLE"
    PASS         = "PASS"


@dataclass
class PullupValueFinding:
    net:       str
    refdes:    str
    value_str: str
    ohms:      float | None
    severity:  PullupBand
    band:      str            # human-readable which-band-violated
    evidence:  str
    pins:      list = field(default_factory=list)  # ["R47", "U3"] refdes on the net


def classify_ohms(ohms: float | None):
    """Map a resistance (or None) to (PullupBand, band-description)."""
    if ohms is None:
        return PullupBand.UNRESOLVABLE, "value unparseable"
    if ohms < FAIL_MIN_OHMS or ohms > FAIL_MAX_OHMS:
        return PullupBand.FAIL, f"outside {FAIL_MIN_OHMS:.0f}Ω–{FAIL_MAX_OHMS:.0f}Ω"
    if ohms < WARN_MIN_OHMS or ohms > WARN_MAX_OHMS:
        return PullupBand.WARN, f"outside {WARN_MIN_OHMS:.0f}Ω–{WARN_MAX_OHMS:.0f}Ω"
    return PullupBand.PASS, "within 1kΩ–10kΩ"


def _pullup_sites(netlist):
    """Yield (net, resistor_component) for every I2C pull-up site: a 2-pin R*/FB*
    with one pin on an SDA/SCL net and the other on a positive supply rail.

    Mirrors step_08d._has_pullup, but yields the SITE (so the value can be read)
    instead of a presence bool.
    """
    comps_by_refdes = {c.refdes: c for c in netlist.components}
    for net in netlist.nets:
        if _net_signal_hint(net.name) is None:   # not an SDA/SCL net
            continue
        net_pins_set = {(refdes, pin_id) for refdes, pin_id in net.pins}
        for refdes, _pin_id in net.pins:
            comp = comps_by_refdes.get(refdes)
            if comp is None:
                continue
            r = comp.refdes.upper()
            if not (r.startswith("R") or r.startswith("FB")):
                continue
            if len(comp.pins) != 2:
                continue
            other_pins = [p for p in comp.pins
                          if (comp.refdes, p.pin_id) not in net_pins_set]
            if not other_pins:
                continue
            if not _is_power_rail_net(other_pins[0].net):
                continue
            # Jumper-class floor (shared with step_08d._has_pullup): a 0Ω link /
            # shunt to the rail is not a pull-up — never value-check it. An
            # UNPARSEABLE value is kept (reaches check_pullup_values as
            # UNRESOLVABLE); the 100Ω M4 mutants (100 > PULLUP_MIN_OHMS) survive.
            ohms = resistance_ohms(comp.value)
            if ohms is not None and ohms < PULLUP_MIN_OHMS:
                continue
            yield net, comp


def check_pullup_values(netlist) -> list[PullupValueFinding]:
    """Check I2C pull-up resistor values against the fixed band.

    Emits one finding per FAIL/WARN/UNRESOLVABLE pull-up site (silent on PASS).
    De-duplicates by (net, refdes) so a resistor bridging both an SDA and SCL
    view is reported once.
    """
    findings: list[PullupValueFinding] = []
    seen: set[tuple[str, str]] = set()
    for net, comp in _pullup_sites(netlist):
        key = (net.name, comp.refdes)
        if key in seen:
            continue
        seen.add(key)
        ohms = resistance_ohms(comp.value)
        severity, band = classify_ohms(ohms)
        if severity is PullupBand.PASS:
            continue  # silent on success
        ohms_txt = "unparseable" if ohms is None else f"{ohms:.0f}Ω"
        pins = sorted({rd for rd, _pid in net.pins})
        if severity is PullupBand.UNRESOLVABLE:
            evidence = (f"Assumed — pull-up {comp.refdes} on I2C net {net.name} has "
                        f"value '{comp.value}' that could not be parsed to a resistance")
        else:
            evidence = (f"Confirmed — pull-up {comp.refdes} on I2C net {net.name} is "
                        f"{ohms_txt} (from '{comp.value}'), {band}; "
                        f"I2C pull-ups should be 1kΩ–10kΩ")
        findings.append(PullupValueFinding(
            net=net.name,
            refdes=comp.refdes,
            value_str=comp.value,
            ohms=ohms,
            severity=severity,
            band=band,
            evidence=evidence,
            pins=pins,
        ))
    return findings

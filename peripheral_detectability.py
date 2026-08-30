"""Shared DETECTABLE-IN-PRINCIPLE criterion for swap-operator mutants.

Swap operators — M5 (UART TX/RX), M6 (I2C SDA/SCL), M12 (SPI MOSI/MISO) — relabel
which net a pin sits on. When the swapped pins carry *no role assertion independent
of the net name* (generic pin functions like ``MP0_7`` / ``Pin_3_3`` / ``A2_3`` and a
component absent from the peripheral KB), the role lived **only** in the net name —
which is exactly what the swap rewrites. After the swap the net is internally
coherent again and **no detector, present or future, can recover the original
intent**. Such a mutant is ``UNDETECTABLE_BY_DESIGN``.

This module defines that criterion **once** so two consumers share a single
definition:
  * the recall harness (``run_recall_harness.py``) — scoring: exclude
    undetectable swaps from the recall denominator (gate #0a), and
  * the corpus regenerator (``generate_bad_corpus.py``, gate #0b) — targeting
    swaps onto role-bearing pins so the corpus is measurable.

Criterion (recon `recon_peripheral_track_2026-06-08.md`, gate #0a Deliverable 1):
a swap mutant is **DETECTABLE-IN-PRINCIPLE** iff at least one swapped pin retains a
role assertion — from the peripheral KB `possible_roles` *or* a role-bearing pin
function — that **contradicts** its post-swap net name, where both the pin role and
the net-name role are the operator's swapped signal pair. Otherwise it is
**UNDETECTABLE_BY_DESIGN**.

The criterion is principled, not fitted: it encodes the detection signature
(pin-role vs net-name incoherence at a swapped pin), then reports whatever split
that produces. It does not target the recon's headline numbers.
"""
from __future__ import annotations

import os
import re
import sys

# ``steps`` lives under schematic_checker_poc/. Importers that already put it on
# the path (run_checks inserts POC_DIR) get the plain import; standalone /
# test importers fall back to the sibling subdir relative to this file.
try:
    from steps.peripheral_roles import (  # noqa: E402
        Peripheral, Role, classify_net_name, classify_pin_function,
    )
except ImportError:  # pragma: no cover - path bootstrap
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "schematic_checker_poc"))
    from steps.peripheral_roles import (  # noqa: E402
        Peripheral, Role, classify_net_name, classify_pin_function,
    )


# ── operator → swapped signal pair ────────────────────────────────────────────

SWAP_OPERATORS = frozenset({
    "M5_UART_TX_TX",
    "M6_I2C_SDA_SCL_SWAP",
    "M12_SPI_MOSI_MISO_SWAP",
    "M99_SPI_SCK_SWAP",
})

# The two roles each operator transposes. Detectability requires a surviving role
# assertion that is one of THESE (a stray UART_CTS pin does not make a TX/RX swap
# detectable), contradicting the net it now sits on.
SWAPPED_SIGNALS: dict[str, frozenset] = {
    "M5_UART_TX_TX":          frozenset({Role.UART_TX, Role.UART_RX}),
    "M6_I2C_SDA_SCL_SWAP":    frozenset({Role.I2C_DATA, Role.I2C_CLOCK}),
    "M12_SPI_MOSI_MISO_SWAP": frozenset({Role.SPI_DATA_OUT, Role.SPI_DATA_IN}),
    # Todo 99: SCK-vs-data swap. Mutation operator stays per-pair (SCK<->MOSI) —
    # the checker's coherence GROUP is the 3-member widen in peripheral_coherence.
    "M99_SPI_SCK_SWAP":       frozenset({Role.SPI_CLOCK, Role.SPI_DATA_OUT}),
}


# ── DI/DO alias (cheap; shared with the future M12 build) ─────────────────────
# `peripheral_roles.PIN_FUNCTION_PATTERNS` knows MOSI/MISO/SCK/CS but not the
# flash-style data names DI/DO/SDI/SDO/DIN/DOUT. From the controller-net frame a
# peripheral's data-IN pin (DI) belongs on the MOSI net; its data-OUT pin (DO) on
# the MISO net. Anchored at a token boundary so DIODE / DONE / DISABLE don't match.
_SPI_DI_RE = re.compile(r'(?i)(?:^|[/_~{])(?:S?DI|DIN)(?:[/_}\d]|$)')   # DI  → MOSI net
_SPI_DO_RE = re.compile(r'(?i)(?:^|[/_~{])(?:S?DO|DOUT)(?:[/_}\d]|$)')  # DO  → MISO net


def _spi_alias_role(pin_function: str):
    """Map a DI/DO-style flash data pin to its SPI net role, else None."""
    if _SPI_DO_RE.search(pin_function):
        return Role.SPI_DATA_IN    # DO sits on the MISO net
    if _SPI_DI_RE.search(pin_function):
        return Role.SPI_DATA_OUT   # DI sits on the MOSI net
    return None


# ── role assertions (the two independent sources) ─────────────────────────────

def role_from_pin_function(pin_function: str | None):
    """Independent pin role from a pin-function string.

    `peripheral_roles.classify_pin_function` first (anchored I2C/UART/SPI/RTS
    regex), then the DI/DO SPI alias. Returns a `Role` or None. Generic functions
    (``MP0``, ``Pin_3``, ``A2``, numeric) return None — they assert no role.
    """
    if not pin_function:
        return None
    ra = classify_pin_function(pin_function)
    if ra is not None:
        return ra.role
    return _spi_alias_role(pin_function)


def role_from_net_name(net_name: str | None):
    """Role implied by a net name (`peripheral_roles.classify_net_name`)."""
    if not net_name:
        return None
    ra = classify_net_name(net_name)
    return ra.role if ra else None


# ── CAN/LIN false-positive guard (Part A) ─────────────────────────────────────
# The v2 peripheral re-scan's "TX+RX both present" UART heuristic misclassified a
# SIT1050T CAN transceiver (ESP32-EVB Rev L) as a UART part — its TxD/RxD pins are
# the CAN-controller serial lines, on a net literally named /GPIO5/CAN-TX. A UART
# role/seed must be SUPPRESSED when either signal says "this is a CAN/LIN bus, not
# a UART": a CAN net token, or a known CAN/LIN transceiver part value. role_from_*
# can't see component/net context, so the guard lives here and is applied by the
# context-bearing callers (op_M5 in the generator + the v2-json seed-selection
# filter). Returns (suppressed, reason) so suppression is auditable as
# UART_SUPPRESSED_CAN.
#
# CAN net token: matches CANH/CANL/CAN_TX/CAN-RX/.../CAN as a whole token but NOT
# "CANDIDATE"/"SCANNER" (boundary-anchored, no bare-substring match).
_CAN_NET_RE = re.compile(r'(?i)(^|[_/-])CAN([_/-]|$|[HL]|TX|RX)')
# Known CAN/LIN transceiver families (component value).
_CAN_XCVR_RE = re.compile(
    r'(?i)(sit10[0-9]{2}|tja10[0-9]{2}|mcp251[0-9]|sn65hvd2[0-9]{2}'
    r'|tcan[0-9]{3}|ncv7[0-9]{3}|atad[0-9])'
)


def uart_role_suppressed_by_can(component_value=None, net_names=None):
    """True when a UART role/seed should be suppressed because the evidence points
    at a CAN/LIN bus, not a UART. Returns ``(suppressed: bool, reason: str|None)``.

    component_value: the candidate component's value (CAN/LIN transceiver match).
    net_names: iterable of net names on the candidate pin/component (CAN token).
    """
    for nm in (net_names or []):
        if nm and _CAN_NET_RE.search(nm):
            return True, f"CAN net token in '{nm}'"
    if component_value and _CAN_XCVR_RE.search(component_value):
        return True, f"CAN/LIN transceiver value '{component_value}'"
    return False, None


# ── unconnected-net exclusion (TODO-313, third-consumer containment) ──────────
# peripheral_coherence.find_coherence_violations and peripheral_bus_pairing's
# signal-2 loop both veto KiCad's synthetic "unconnected-(...)" net names before
# classifying them (TODO-303 Phase 2a) — a floating pin's fabricated label is not
# a real bus signal, even though classify_net_name now returns a truthful role for
# one by design (peripheral_roles.py's D2-unwrap docstring names those two as "the
# ONLY containment layer for unconnected nets"). detectable_in_principle is a
# THIRD, independent consumer of role_from_net_name that was never added to that
# containment list, and credited the StickHub M6 mutant as detectable solely
# because one swapped pin's post-swap net was an unconnected-(...) wrapper whose
# D2-unwrapped payload happened to embed the other signal's role token. Mirrored
# here verbatim (same regex, same skip-this-pin semantics) rather than imported,
# per this codebase's per-checker name-marker convention.
_UNCONNECTED_NET_RE = re.compile(r'unconnected[-_(]', re.IGNORECASE)


# ── the criterion ─────────────────────────────────────────────────────────────

def detectable_in_principle(operator_id: str, swapped_pins, kb_role_lookup=None):
    """Apply the DETECTABLE-IN-PRINCIPLE criterion to one swap mutant.

    Args:
        operator_id: a member of `SWAP_OPERATORS`.
        swapped_pins: iterable of ``(refdes, pin_id, pin_function, net_name)`` for
            the swapped component's pins, with their *post-swap* nets.
        kb_role_lookup: optional ``(refdes, pin_id) -> set[Role]`` returning a
            pin's peripheral-KB `possible_roles` (already mapped to
            `peripheral_roles.Role`). The second independent source; for the
            current corpus it is empty (swaps never land on KB-covered MCUs).

    Returns:
        ``(detectable: bool, evidence: dict)``. ``detectable`` is True iff some
        swapped pin's independent role is in the swapped pair AND differs from the
        role its current net name implies (also in the pair) — the surviving
        contradiction a detector could fire on.
    """
    if operator_id not in SWAPPED_SIGNALS:
        raise ValueError(f"{operator_id!r} is not a swap operator")
    pair = SWAPPED_SIGNALS[operator_id]

    for refdes, pin_id, pin_function, net_name in swapped_pins:
        if net_name and _UNCONNECTED_NET_RE.search(net_name):
            continue  # synthetic floating-pin label, not a real bus signal
        net_role = role_from_net_name(net_name)
        if net_role not in pair:
            continue  # this pin's net is not one of the swapped role-nets

        # source 1: role-bearing pin function (incl. DI/DO alias)
        pin_role = role_from_pin_function(pin_function)
        source = "pin_function"
        # source 2: peripheral-KB possible_roles (empty for the current corpus)
        if pin_role not in pair and kb_role_lookup is not None:
            kb_roles = kb_role_lookup(refdes, pin_id) or set()
            pin_role = next((r for r in kb_roles if r in pair), None)
            if pin_role is not None:
                source = "kb_possible_roles"

        if pin_role in pair and pin_role != net_role:
            return True, {
                "refdes": refdes, "pin_id": pin_id, "pin_function": pin_function,
                "net": net_name, "pin_role": pin_role.value,
                "net_role": net_role.value, "source": source,
            }

    return False, {
        "reason": "no swapped pin carries a role assertion contradicting its "
                  "net name (role survived only in the swapped net name)",
    }

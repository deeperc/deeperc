"""TODO-93 — UART wrong-destination CAPABILITY classifier.

LIVE / VERDICT-MOVING since Phase 2 (2026-07-11). ``step_08d_peripheral_checker``
lazily imports ``classify_netlist_uart`` and emits a
``PeripheralViolation.UART_CAPABILITY_MISMATCH`` FAIL for each finding (the M14
precedent: step_08d owns the emit, this module owns the decision; the lazy import
breaks the cycle, since this module imports ``_resolve_pin`` back out of step_08d).
The module is verdict-affecting but is NOT a checker-registry entry, so it lives in
``provenance.CORE_PIPELINE_FILES`` — adding it moved ``checker_code_hash`` (an
announced code-era break).

THE INVARIANT (structural, and re-asserted at the emit site): a FAIL fires ONLY on
KB-CONFIRMED incapability. A pin becomes a ``UartNetFinding`` ONLY from a resolved KB
lookup (``_LookupStatus.OK``) whose ``possible_roles`` contain ZERO ``Peripheral.UART``
entries. A matrix/free-mux pin (PERIPHERAL_UNCONSTRAINED — ESP32, nRF52) and a non-KB'd
pin (KB_MISSING / PIN_NOT_IN_KB) can NEVER become incapable: both route to
``ClassifyResult.cleared`` as UNRESOLVABLE-class. A net with no confirmed-USART endpoint
(including one suppressed by the CAN/LIN guard) is never evaluated at all. Emission is
FAIL-only — ``cleared`` and unconfirmed nets emit nothing.

Phases 1/1.5 (2026-07-11) folded in two corrections, both validated on the
constructed OLIMEXINO+CH340C seed (``phase1_5_seed/``, see that directory's
README for why a constructed fixture was necessary):
  * B1 — a source-(c) pin-function match is now routed through
    ``peripheral_detectability.uart_role_suppressed_by_can`` before it can
    confirm a net as USART, so a CAN/LIN transceiver's own TXD/RXD pin
    (e.g. a SIT1050T on a real OLIMEXINO-STM32F3 board) never false-confirms
    a CAN net as UART. Without this guard the seed's own on-board CAN
    transceiver mis-fires on ``/D24_CANTX`` — proven live, see
    ``phase1_diagnostic/REPORT_v3.md``.
  * ``_resolve_pin`` is now called with ``peripheral=Peripheral.UART`` (the
    matrix short-circuit generalized in ``8eceea5``), so a matrix-routed
    UART part is correctly treated as unresolvable rather than falling
    through to a stale per-pin dict lookup keyed for I2C.

Recon basis: ``investigation/experiments/uart_wrong_destination_recon/
REPORT.md`` Q1/Q2. Generalizes M14's ``CAPABILITY_MISMATCH`` role-COUNT gate
(``step_08d_peripheral_checker.py`` ~L396-415: ``len(i2c_roles) == 0`` -> FAIL)
from I2C to UART, substituting ``Peripheral.UART`` for ``Peripheral.I2C``
unchanged — this is a role-count check ("does this pin have ANY UART role at
all"), never a role-IDENTITY check ("is this pin specifically TX vs RX").

THE MAKE-OR-BREAK (recon Q2): a "confirmed USART net" is established ONLY via
endpoint confirmation, NEVER via net-name matching. A prior TODO-93 attempt
(``investigation/experiments/todo93_uart_coherence/STOP_REPORT.md``, reverted
commit ``7924d53``) used net-name-derived role assertion and mis-fired on
correctly-wired USB-serial bridges (a bridge's TXD pin legitimately sits on a
net *named* "RX", named from the MCU's receiving perspective — UART is
device-relative, unlike I2C/SPI bus signals). This module never calls
``peripheral_roles.classify_net_name`` / ``role_from_net_name`` and carries no
net-name regex at all. The ONE signal that can confirm a net is:

  (c) a member's own literal pin-function string resolving to
      ``peripheral_detectability.Role.UART_TX`` / ``UART_RX`` via
      ``role_from_pin_function`` — source "pinfunc_endpoint" — UNLESS
      ``peripheral_detectability.uart_role_suppressed_by_can`` says the
      evidence actually points at a CAN/LIN bus (a known transceiver value,
      e.g. SIT1050T/TJA1050/MCP251x, or a CAN net-name token on the same
      net), in which case this source is suppressed and does NOT confirm
      (B1 guard, Phase 1.5).

KB ``possible_roles`` CANNOT confirm a net (option (a), Phase 2 STOP →
2026-07-11). A prior source (b) — "a KB-resolved endpoint whose possible_roles
include a UART role" — was REMOVED when wiring the check live: a KB
``possible_roles`` list is an ALTERNATE-FUNCTION MAP (what the pin COULD be muxed
to), not evidence of what the design DID mux it to. On the real
``STM32F103C(8-B)Tx`` KB, ``PB6 = [i2c/I2C1/scl, tim/TIM4/ch, uart/USART1/tx,
gpio]`` and ``PB7 = [i2c/I2C1/sda, ..., uart/USART1/rx, gpio]`` — which IS the
standard F103 I2C1 pinout, so every normally-wired STM32 I2C bus was being
"confirmed" as a USART net, and any KB-resolved zero-UART-role pin on it (an
EEPROM's SDA) became a false wrong-destination FAIL. It fired on six correct-I2C
unit fixtures the moment the check went live, and 255 of the good corpus's 261
"confirmed USART" nets were source-(b)-only (297 pins spared a verdict solely by
KB sparsity — the live peripheral KB holds 5 MCU-only parts, which is why Phases
1/1.5 measured a clean 0). See ``phase2/STOP_REPORT.md``.

The asymmetry is deliberate and sound: possible_roles may only ever CONDEMN, never
CONFIRM. "This pin has ZERO UART roles" is a hard fact about silicon (no mux
setting reaches UART); "this pin has A UART role" says only that a mux setting
exists. So the KB still decides the incapable/victim side unchanged, and the
confirming side rests on an ACTUAL function — a bridge chip's ``TXD`` pin IS a TX
pin. The cost: an MCU↔MCU UART link with no pin-function-named party is
unconfirmable and therefore silent (the corpus contains zero such links).

Per net (>=2 non-passive members required — a "wrong destination" question
needs at least two parties):
  1. Find >=1 CONFIRMED-USART endpoint via (c) above. None found -> net
     not evaluated, no finding (not even UNRESOLVABLE — there is nothing to be
     unresolved ABOUT).
  2. For every OTHER non-passive member on that net:
       - KB-absent (``_LookupStatus`` != OK, e.g. KB_MISSING / PIN_NOT_IN_KB)
         OR matrix-routed (PERIPHERAL_UNCONSTRAINED, via
         ``peripheral=Peripheral.UART`` — Phase 1.5) -> EXPLICITLY cleared as
         UNRESOLVABLE-class (recorded in ``ClassifyResult.cleared``), never
         counted as incapable.
       - KB-resolved AND has >=1 ``Peripheral.UART`` role -> capable, cleared,
         not counted (recorded with reason "HAS_UART_ROLE").
       - KB-resolved AND has ZERO ``Peripheral.UART`` roles -> INCAPABLE ->
         a ``UartNetFinding`` (would-fire).

Local, isolated UART pin-function-role vocabulary. Deliberately does NOT
extend ``peripheral_coherence._KB_SIGNAL_TO_ROLE`` — that table IS consumed
by the LIVE M6 emit path (``step_08d_peripheral_checker.py`` ->
``check_i2c_coherence`` -> ``kb_role_lookup_from``), so widening it would move
M6/M12 verdicts too. Keeping this vocabulary local is what makes the UART wiring
additive: it cannot perturb any pre-existing checker's findings.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .peripheral_kb import Peripheral
from .step_08d_peripheral_checker import (
    _resolve_pin, _is_passive_refdes, _LookupStatus, canonicalize_mpn_for_kb,  # noqa: F401
)

try:
    import peripheral_detectability as _pdet
except ImportError:  # pragma: no cover - path bootstrap, mirrors peripheral_coherence.py
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    import peripheral_detectability as _pdet

# Local, isolated — source (c). NOT peripheral_coherence._KB_SIGNAL_TO_ROLE.
_UART_PINFUNC_ROLES = (_pdet.Role.UART_TX, _pdet.Role.UART_RX)


def uart_kb_roles(entry) -> list:
    """[PinRole, ...] with peripheral == Peripheral.UART, any Signal/instance.
    Mirrors M14's ``i2c_roles = [r for r in entry.roles if r.peripheral ==
    Peripheral.I2C]`` (step_08d_peripheral_checker.py) with UART substituted."""
    if entry is None:
        return []
    return [r for r in entry.roles if r.peripheral == Peripheral.UART]


@dataclass
class ConfirmingEndpoint:
    refdes:            str
    pin_id:             str
    mpn:                str | None
    pin_name:           str | None
    source:             str = "pinfunc_endpoint"   # the ONLY confirming source (see module docstring)
    pinfunc_role:       str | None = None


@dataclass
class IncapableEndpoint:
    refdes:                 str
    pin_id:                  str
    mpn:                     str
    pin_name:                str | None
    kb_possible_peripherals: list = field(default_factory=list)   # e.g. ["ADC","GPIO"] — empty of UART
    kb_sources:              list = field(default_factory=list)   # KBSource.value per role — the FAIL's provenance


@dataclass
class UartNetFinding:
    board:               str | None
    net:                  str
    status:               str = "WOULD_FIRE"
    confirming_endpoints: list = field(default_factory=list)   # list[ConfirmingEndpoint]
    incapable_endpoint:   IncapableEndpoint = None


@dataclass
class ClassifyResult:
    findings:              list          # list[UartNetFinding] — the would-fire nets
    confirmed_usart_nets:   int           # count of nets with >=1 confirmed endpoint
    cleared:               list          # list[dict] — non-incapable candidates + WHY


def classify_netlist_uart(netlist, kb, peripheral_routing, board: str | None = None) -> ClassifyResult:
    """Per-net UART wrong-destination capability scan over one parsed netlist.

    Returns a ``ClassifyResult``; it does not construct findings itself. The caller
    (``step_08d_peripheral_checker``) turns each ``UartNetFinding`` into a
    UART_CAPABILITY_MISMATCH FAIL. Keeping the decision here and the emit there is
    what lets the classifier stay unit-testable in isolation (tests/test_uart_capability.py)
    and lets the emit site re-assert the invariant independently."""
    comps_by_refdes = {c.refdes: c for c in netlist.components}
    pinname_by_ref_id = {
        (c.refdes, p.pin_id): p.pin_name
        for c in netlist.components for p in c.pins
    }

    findings: list[UartNetFinding] = []
    cleared: list[dict] = []
    confirmed_usart_nets = 0

    for net in netlist.nets:
        non_passive = [
            (rd, pid) for rd, pid in net.pins
            if comps_by_refdes.get(rd) is not None and not _is_passive_refdes(rd)
        ]
        if len(non_passive) < 2:
            continue

        pin_info = []   # (refdes, pin_id, mpn, pin_name, status, entry, pf_role)
        confirming: list[ConfirmingEndpoint] = []
        for refdes, pin_id in non_passive:
            comp = comps_by_refdes[refdes]
            pname = pinname_by_ref_id.get((refdes, pin_id))
            status, entry = _resolve_pin(comp.effective_mpn, pin_id, kb, peripheral_routing, pname,
                                          peripheral=Peripheral.UART)
            pf_role = _pdet.role_from_pin_function(pname)
            pin_info.append((refdes, pin_id, comp.effective_mpn, pname, status, entry, pf_role))

            # NOTE: a KB `possible_roles` UART entry does NOT confirm this net (the
            # removed source (b) — an alt-fn map is not a wiring decision; see the
            # module docstring). The KB lookup above is kept because it decides the
            # INCAPABLE side below, where a zero-UART-role list IS a hard fact.
            if pf_role in _UART_PINFUNC_ROLES:
                suppressed, _reason = _pdet.uart_role_suppressed_by_can(
                    component_value=getattr(comp, "value", None), net_names=[net.name],
                )
                if suppressed:
                    continue
                confirming.append(ConfirmingEndpoint(
                    refdes=refdes, pin_id=pin_id, mpn=comp.effective_mpn, pin_name=pname,
                    source="pinfunc_endpoint", pinfunc_role=pf_role.value,
                ))

        if not confirming:
            continue   # no confirmed-USART endpoint on this net -> not evaluated
        confirmed_usart_nets += 1

        confirmed_keys = {(c.refdes, c.pin_id) for c in confirming}
        for refdes, pin_id, mpn, pname, status, entry, pf_role in pin_info:
            if (refdes, pin_id) in confirmed_keys:
                continue   # this pin IS a confirming endpoint itself, not a candidate victim
            if status != _LookupStatus.OK or entry is None:
                # KB-absent or matrix-unresolvable -> EXPLICIT UNRESOLVABLE, never incapable.
                cleared.append({
                    "board": board, "net": net.name, "refdes": refdes, "pin_id": pin_id,
                    "mpn": mpn, "reason": "KB_ABSENT_OR_MATRIX_UNRESOLVABLE",
                    "lookup_status": status.value,
                })
                continue
            u_roles = uart_kb_roles(entry)
            if u_roles:
                cleared.append({
                    "board": board, "net": net.name, "refdes": refdes, "pin_id": pin_id,
                    "mpn": mpn, "reason": "HAS_UART_ROLE",
                    "uart_signals": sorted({r.signal.value for r in u_roles}),
                })
                continue
            findings.append(UartNetFinding(
                board=board, net=net.name, status="WOULD_FIRE",
                confirming_endpoints=confirming,
                incapable_endpoint=IncapableEndpoint(
                    refdes=refdes, pin_id=pin_id, mpn=mpn, pin_name=pname,
                    kb_possible_peripherals=sorted({r.peripheral.value for r in entry.roles}),
                    kb_sources=[r.source for r in entry.roles],
                ),
            ))

    return ClassifyResult(findings=findings, confirmed_usart_nets=confirmed_usart_nets, cleared=cleared)

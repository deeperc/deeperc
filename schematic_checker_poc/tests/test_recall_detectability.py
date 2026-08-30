"""Tests for the gate #0a DETECTABLE-IN-PRINCIPLE criterion (peripheral_detectability).

The criterion decides whether a swap mutant (M5 UART, M6 I2C, M12 SPI) retains
any role assertion independent of the swapped net name. Undetectable-by-design
mutants are excluded from the recall denominator by run_recall_harness.

These are unit tests over the criterion's tuple interface
``(refdes, pin_id, pin_function, net_name)`` — no kicad-cli / parsing needed.
"""
import os
import sys

import pytest

# repo root (peripheral_detectability) + schematic_checker_poc (steps) on the path
_HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_HERE, ".."))        # schematic_checker_poc → steps
sys.path.insert(0, os.path.join(_HERE, "..", ".."))  # repo root → peripheral_detectability

import peripheral_detectability as pd  # noqa: E402
from steps.peripheral_roles import Role  # noqa: E402

M6 = "M6_I2C_SDA_SCL_SWAP"
M5 = "M5_UART_TX_TX"
M12 = "M12_SPI_MOSI_MISO_SWAP"
M99 = "M99_SPI_SCK_SWAP"


# ── Test 1: surviving role-bearing pin function → detectable-in-principle ──────
def test_role_bearing_pin_function_is_detectable():
    # An SDA-function pin sitting on an SCL-named net (the stamp_and_module U3 case).
    swapped = [("U3", "2", "SDA_2", "/SCL"),
               ("U3", "3", "SCL_3", "/SDA")]
    detectable, ev = pd.detectable_in_principle(M6, swapped)
    assert detectable is True
    assert ev["source"] == "pin_function"
    assert ev["pin_role"] == Role.I2C_DATA.value
    assert ev["net_role"] == Role.I2C_CLOCK.value


# ── Test 2: only generic pin functions → UNDETECTABLE_BY_DESIGN ────────────────
def test_generic_pin_functions_are_undetectable():
    # One-Air-Max U8 / Connectors J5: swapped pins are generic; role lived only in
    # the (now swapped) net name, so nothing contradicts.
    swapped = [("U8", "7", "MP0_7", "/SCL"),
               ("U8", "5", "MP1_5", "/SDA")]
    detectable, ev = pd.detectable_in_principle(M6, swapped)
    assert detectable is False
    assert "net name" in ev["reason"]


def test_generic_uart_connector_is_undetectable():
    # M5 Debugger/CON2: nRF52 GPIO-port pin name (A2_3) is generic.
    swapped = [("U6", "3", "A2_3", "/UART_TX_NRF52")]
    detectable, _ = pd.detectable_in_principle(M5, swapped)
    assert detectable is False


# ── Test 3: DI/DO alias makes a DI/DO-named SPI swap detectable ────────────────
def test_di_do_alias_detects_spi_flash_swap():
    # SD-card / flash data pins: CMD/DI is the MOSI-side pin, DAT0/DO the MISO-side.
    # After the swap they land on the opposite nets (OLIMEXINO MICRO_SD1 case).
    swapped = [("MICRO_SD1", "3", "CMD/DI", "/D33_MISO2"),
               ("MICRO_SD1", "7", "DAT0/DO", "/D34_MOSI2")]
    detectable, ev = pd.detectable_in_principle(M12, swapped)
    assert detectable is True
    assert ev["pin_role"] in (Role.SPI_DATA_OUT.value, Role.SPI_DATA_IN.value)

    # The alias is the load-bearing part: the base classifier alone returns None.
    from steps.peripheral_roles import classify_pin_function
    assert classify_pin_function("CMD/DI") is None
    assert pd.role_from_pin_function("CMD/DI") == Role.SPI_DATA_OUT   # DI → MOSI net
    assert pd.role_from_pin_function("DAT0/DO") == Role.SPI_DATA_IN   # DO → MISO net


def test_di_do_alias_does_not_match_unrelated_tokens():
    # Anchored alias must not fire on DIODE / DONE / DISABLE-style names.
    for noise in ("DIODE", "DONE_5", "DISABLE", "RADIO", "MP0"):
        assert pd.role_from_pin_function(noise) is None


# ── Test 4: non-swap operators are unaffected by the new bucket ────────────────
def test_swap_operator_set_excludes_non_swap_ops():
    assert pd.SWAP_OPERATORS == {M5, M6, M12, M99}
    for non_swap in ("M7_VDD_FLOATING", "M1_VOLTAGE_CROSSOVER", "M3_MISSING_I2C_PULLUP",
                     "M9_ESP32_STRAPPING"):
        assert non_swap not in pd.SWAP_OPERATORS


def test_criterion_rejects_non_swap_operator():
    with pytest.raises(ValueError):
        pd.detectable_in_principle("M7_VDD_FLOATING", [("U1", "1", "VDD", "VDD")])


# ── KB possible_roles as the second independent source ────────────────────────
def test_kb_role_source_detects_when_pin_function_generic():
    # If the swapped pin is a KB-covered MCU pin (generic name, but KB asserts SDA)
    # on an SCL net, the KB source still makes it detectable. No such mutant exists
    # in the current corpus, but the source must work for future fixed-I2C seeds.
    swapped = [("U1", "PB7", "PB7_42", "/SCL")]
    detectable, ev = pd.detectable_in_principle(
        M6, swapped, kb_role_lookup=lambda r, p: {Role.I2C_DATA})
    assert detectable is True
    assert ev["source"] == "kb_possible_roles"


# ── dot-delimited peripheral nets ARE recognized (dot-widen landed) ───────────
def test_dot_delimited_spi_net_now_recognized():
    # The Phase-1.1.c dot-widen of classify_net_name closed the gap that kept
    # nRF54L15 U4 ('SPI.MOSI'/'SPI.MISO', dot separator) undetectable: its DI/DO
    # pins are role-bearing and the dotted nets now classify, so the swap is
    # detectable-in-principle. (Was test_dot_delimited_spi_net_is_unrecognized.)
    assert pd.role_from_net_name("/SPI.MOSI") == Role.SPI_DATA_OUT
    swapped = [("U4", "2", "DO/IO_{1}", "/SPI.MOSI"),
               ("U4", "5", "DI/IO_{0}", "/SPI.MISO")]
    detectable, _ = pd.detectable_in_principle(M12, swapped)
    assert detectable is True


# ── Part A: CAN/LIN false-positive guard ──────────────────────────────────────
def test_can_transceiver_is_suppressed_as_uart():
    # SIT1050T (ESP32-EVB Rev L) — TxD/RxD on CAN-controller serial nets. Both the
    # transceiver value AND the CAN net token must trigger suppression.
    suppressed, reason = pd.uart_role_suppressed_by_can(
        component_value="SIT1050T(SOIC-8_150mil)",
        net_names=["/GPIO5/CAN-TX", "/GPIO35/CAN-RX"])
    assert suppressed is True
    assert reason  # auditable

    # value alone (plain nets) still suppresses — it IS a CAN transceiver
    s2, _ = pd.uart_role_suppressed_by_can("SIT1050T", net_names=["TXD", "RXD"])
    assert s2 is True
    # CAN net token alone (unknown value) still suppresses
    s3, _ = pd.uart_role_suppressed_by_can(None, net_names=["/CANH", "/CANL"])
    assert s3 is True


def test_legitimate_uart_bridge_not_over_suppressed():
    # CH340C (Amit_Amp) — a real USB-UART bridge on plain TXD/RXD nets must NOT be
    # suppressed; the guard must not over-fire on legitimate bridges.
    suppressed, reason = pd.uart_role_suppressed_by_can(
        component_value="CH340C", net_names=["TXD", "RXD"])
    assert suppressed is False
    assert reason is None
    # "CANDIDATE"/"SCANNER" are not CAN tokens (boundary anchoring)
    s2, _ = pd.uart_role_suppressed_by_can("MAX3232", net_names=["SCANNER_TX", "CANDIDATE"])
    assert s2 is False


# ── TODO-313: unconnected-net exclusion (third-consumer containment) ──────────
def test_unconnected_wrapped_net_is_undetectable():
    # StickHub U1 shape: pin 2 (SDA-role, TEST#/SDA_2) is genuinely floating both
    # before and after the swap — its post-swap net is still its OWN pre-swap
    # unconnected-(...) wrapper (the swap only relocates pin 3 onto it). Pin 3
    # (SCL-role, LED1/SCL_3) lands on /LED1, a real but role-silent net. Without
    # the guard, classify_net_name's D2 unwrap would extract "SDA" from the
    # wrapper and falsely credit this as detectable (the actual TODO-313 bug).
    swapped = [("U1", "2", "TEST#/SDA_2", "/LED1"),
               ("U1", "3", "LED1/SCL_3", "unconnected-(U1-TEST#{slash}SDA-Pad2)")]
    detectable, ev = pd.detectable_in_principle(M6, swapped)
    assert detectable is False
    assert "net name" in ev["reason"]


def test_ordinary_nonfloating_swap_still_detectable():
    # Regression guard: an ordinary swap where both role nets are real (no
    # unconnected-(...) wrapper anywhere) must be unaffected by the new guard.
    swapped = [("U3", "2", "SDA_2", "/SCL"),
               ("U3", "3", "SCL_3", "/SDA")]
    detectable, ev = pd.detectable_in_principle(M6, swapped)
    assert detectable is True
    assert ev["source"] == "pin_function"


def test_unconnected_net_regex_matches_peripheral_coherence():
    # Symmetry assertion: the two independent per-checker copies of this regex
    # (TODO-303 Phase 2a's containment guard, and this module's TODO-313 mirror)
    # must stay byte-identical, or a future edit to one silently reopens the gap
    # this fix closes. Precedent: TODO-310's stub<->real inspect.signature test.
    from steps.peripheral_coherence import _UNCONNECTED_NET_RE as coherence_re
    assert pd._UNCONNECTED_NET_RE.pattern == coherence_re.pattern
    assert pd._UNCONNECTED_NET_RE.flags == coherence_re.flags


def test_unconnected_net_regex_matches_step08g_pullup_presence():
    # TODO-272 Phase 2a: step_08g_pullup_presence becomes a FOURTH consumer of
    # classify_net_name (via Family 1's new name-based alternate entry) and
    # reuses this module's/peripheral_coherence's own _UNCONNECTED_NET_RE
    # PATTERN TEXT verbatim (not a new copy) as that entry's veto -- same
    # "stay byte-identical or a future edit silently reopens the gap" discipline
    # as the comparison above.
    from steps.step_08g_pullup_presence import _UNCONNECTED_NET_RE as step08g_re
    assert pd._UNCONNECTED_NET_RE.pattern == step08g_re.pattern
    assert pd._UNCONNECTED_NET_RE.flags == step08g_re.flags
    # The exact TRNXSDR-adjacent shape from step_08g's own test suite (case iii,
    # test_pullup_presence.py's test_fam1_name_path_unconnected_veto) -- prove
    # the copies agree on THIS shape, not just in the abstract.
    assert step08g_re.search("unconnected-(U1-SDA_2-Pad5)")
    assert pd._UNCONNECTED_NET_RE.search("unconnected-(U1-SDA_2-Pad5)")

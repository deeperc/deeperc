import logging
import pytest
from steps.step_08c_structural_checker import (
    check_structural_integrity,
    _is_power_pin_name,
    _is_ground_pin_name,
    _is_rail_name,
    _is_synthetic_unconnected_net,
)
from steps.step_02_parser import ComponentIR, PinIR, NetIR


def test_power_pin_name_recognition():
    assert _is_power_pin_name("VDD")
    assert _is_power_pin_name("vcc")
    assert _is_power_pin_name("AVDD")
    assert _is_power_pin_name("VCCA")
    assert _is_power_pin_name("IOVDD")
    assert not _is_power_pin_name("PA0")
    assert not _is_power_pin_name("")
    assert not _is_power_pin_name("RESET")


def test_ground_pin_name_recognition():
    assert _is_ground_pin_name("GND")
    assert _is_ground_pin_name("vss")
    assert _is_ground_pin_name("AGND")
    assert _is_ground_pin_name("DGND")
    assert not _is_ground_pin_name("VDD")
    assert not _is_ground_pin_name("")


def test_synthetic_unconnected_detection():
    assert _is_synthetic_unconnected_net("unconnected-(U1-Pad14)")
    assert _is_synthetic_unconnected_net("unconnected-(U2-Pad7-VCC)")
    # Net-(...) and N$... are no longer in the regex — floating is detected
    # via pin count, not name pattern
    assert not _is_synthetic_unconnected_net("Net-(R1-Pad1)")
    assert not _is_synthetic_unconnected_net("N$42")
    assert not _is_synthetic_unconnected_net("VCC_3V3")
    assert not _is_synthetic_unconnected_net("GND")
    assert _is_synthetic_unconnected_net("")  # empty net = floating


def test_floating_vdd_produces_fail():
    components = [
        ComponentIR(refdes="U1", part_number="STM32", value="", pins=[
            PinIR(pin_id="23", pin_name="VDD", net="unconnected-(U1-Pad23)"),
        ]),
    ]
    nets = [NetIR(name="unconnected-(U1-Pad23)", pins=[("U1", "23")])]
    confirmed_voltages = {"VCC_3V3": 3.3}
    ground_nets = ["GND"]

    results = check_structural_integrity(
        components, confirmed_voltages, ground_nets, nets
    )
    assert len(results) == 1
    assert results[0].status == "FAIL"
    assert "floating" in results[0].evidence_label.lower()


def test_floating_gnd_produces_fail():
    components = [
        ComponentIR(refdes="U1", part_number="STM32", value="", pins=[
            PinIR(pin_id="22", pin_name="VSS", net="unconnected-(U1-Pad22)"),
        ]),
    ]
    nets = [NetIR(name="unconnected-(U1-Pad22)", pins=[("U1", "22")])]
    results = check_structural_integrity(components, {}, [], nets)
    assert len(results) == 1
    assert results[0].status == "FAIL"


def test_correctly_wired_vdd_passes():
    components = [
        ComponentIR(refdes="U1", part_number="STM32", value="", pins=[
            PinIR(pin_id="23", pin_name="VDD", net="VCC_3V3"),
        ]),
    ]
    nets = [NetIR(name="VCC_3V3", pins=[("U1", "23"), ("U2", "8")])]
    confirmed_voltages = {"VCC_3V3": 3.3}
    results = check_structural_integrity(components, confirmed_voltages, [], nets)
    assert len(results) == 1
    assert results[0].status == "PASS"


def test_normalized_power_lookup():
    """+3.3V net name should match when confirmed_voltages uses same key."""
    components = [
        ComponentIR(refdes="U1", part_number="STM32", value="", pins=[
            PinIR(pin_id="23", pin_name="VDD", net="+3.3V"),
        ]),
    ]
    nets = [NetIR(name="+3.3V", pins=[("U1", "23"), ("U2", "8")])]
    confirmed_voltages = {"+3.3V": 3.3}
    results = check_structural_integrity(components, confirmed_voltages, [], nets)
    assert results[0].status == "PASS"


def test_kicad_power_flag_net_not_flagged_as_floating():
    """Pins on KiCad power flag synthetic nets should not be FAIL."""
    components = [
        ComponentIR(refdes="IC3", part_number="CP2105", value="", pins=[
            PinIR(pin_id="6", pin_name="VDD",
                  net="unconnected-(#FLG026-pwr-Pad1)"),
        ]),
        ComponentIR(refdes="#FLG026", part_number="PWR_FLAG", value="", pins=[
            PinIR(pin_id="1", pin_name="pwr",
                  net="unconnected-(#FLG026-pwr-Pad1)"),
        ]),
    ]
    nets = [
        NetIR(name="unconnected-(#FLG026-pwr-Pad1)",
              pins=[("IC3", "6"), ("#FLG026", "1")]),
    ]
    results = check_structural_integrity(components, {}, [], nets)
    # The #FLG026 pseudo-component is skipped entirely
    flg_results = [r for r in results if r.refdes == "#FLG026"]
    assert len(flg_results) == 0
    # IC3's VDD is on a #FLG net — treated as synthetic, not a hard FAIL
    ic3_results = [r for r in results if r.refdes == "IC3"]
    assert len(ic3_results) == 1
    assert ic3_results[0].status != "FAIL"


def test_pseudo_component_pins_skipped():
    """Components with refdes starting with # are skipped entirely."""
    components = [
        ComponentIR(refdes="#FLG001", part_number="PWR_FLAG", value="", pins=[
            PinIR(pin_id="1", pin_name="VDD", net="unconnected-(#FLG001)"),
        ]),
    ]
    nets = [NetIR(name="unconnected-(#FLG001)", pins=[("#FLG001", "1")])]
    results = check_structural_integrity(components, {}, [], nets)
    assert len(results) == 0


def test_bare_vcc_rail_passes_when_confirmed():
    """A bare VCC net in confirmed_voltages (voltage=None) should produce PASS."""
    components = [
        ComponentIR(refdes="U1", part_number="74LS125", value="", pins=[
            PinIR(pin_id="14", pin_name="VCC", net="VCC"),
        ]),
    ]
    nets = [NetIR(name="VCC", pins=[("U1", "14"), ("U2", "7")])]
    confirmed_voltages = {"VCC": None}   # recognized by step_06, voltage unknown
    results = check_structural_integrity(components, confirmed_voltages, [], nets)
    assert len(results) == 1
    assert results[0].status == "PASS"
    assert "unconfirmed" in results[0].evidence_label


def test_unrecognized_multipin_net_warns():
    """Power pin on multi-pin unrecognized net produces WARN, not FAIL.
    Net name is deliberately NON-rail-named (no power-token prefix, no _VDD/_VCC
    suffix) so branch 2.5 (_is_rail_name) does not intercept — this exercises the
    branch-6 custom-rail WARN path."""
    components = [
        ComponentIR(refdes="U1", part_number="STM32", value="", pins=[
            PinIR(pin_id="20", pin_name="AVCC", net="FILTERED_SUPPLY_NODE"),
        ]),
    ]
    nets = [NetIR(name="FILTERED_SUPPLY_NODE",
                  pins=[("U1", "20"), ("U2", "8"), ("C1", "1")])]
    confirmed_voltages = {"VCC_3V3": 3.3}
    results = check_structural_integrity(components, confirmed_voltages, [], nets)
    assert len(results) == 1
    assert results[0].status == "WARN"
    assert "may be a custom rail" in results[0].evidence_label


def test_multipin_auto_named_net_not_flagged_as_floating():
    """Net-(D1-K) with multiple pins should NOT be FAIL.
    Regression test: nRFmicro power-OR circuit where Net-(D1-K) connects
    D1 cathode + Q1 source + U2 VIN — a valid 3-pin net, not floating.
    """
    components = [
        ComponentIR(refdes="U2", part_number="AP2112K", value="", pins=[
            PinIR(pin_id="1", pin_name="VIN", net="Net-(D1-K)"),
        ]),
        ComponentIR(refdes="D1", part_number="DIODE", value="", pins=[
            PinIR(pin_id="1", pin_name="K", net="Net-(D1-K)"),
        ]),
        ComponentIR(refdes="Q1", part_number="MOSFET", value="", pins=[
            PinIR(pin_id="2", pin_name="S", net="Net-(D1-K)"),
        ]),
    ]
    nets = [NetIR(name="Net-(D1-K)", pins=[("U2", "1"), ("D1", "1"), ("Q1", "2")])]

    results = check_structural_integrity(components, {}, [], nets)
    u2_results = [r for r in results if r.refdes == "U2"]
    assert len(u2_results) == 1
    assert u2_results[0].status == "WARN", \
        f"Expected WARN, got {u2_results[0].status}: {u2_results[0].evidence_label}"


def test_single_pin_auto_named_net_still_flagged():
    """Net-(U1-VDD) with only one pin should still be FAIL (pin-count check)."""
    components = [
        ComponentIR(refdes="U1", part_number="STM32", value="", pins=[
            PinIR(pin_id="23", pin_name="VDD", net="Net-(U1-VDD)"),
        ]),
    ]
    nets = [NetIR(name="Net-(U1-VDD)", pins=[("U1", "23")])]

    results = check_structural_integrity(components, {}, [], nets)
    assert len(results) == 1
    assert results[0].status == "FAIL"
    assert "single-pin" in results[0].evidence_label.lower()


def test_unconnected_explicit_pattern_still_fail():
    """unconnected-(...) explicit marker always produces FAIL regardless of pin count."""
    components = [
        ComponentIR(refdes="U1", part_number="STM32", value="", pins=[
            PinIR(pin_id="23", pin_name="VDD", net="unconnected-(U1-Pad23)"),
        ]),
    ]
    nets = [NetIR(name="unconnected-(U1-Pad23)", pins=[("U1", "23")])]
    results = check_structural_integrity(components, {}, [], nets)
    assert len(results) == 1
    assert results[0].status == "FAIL"


def test_leading_slash_net_normalization():
    """KiCad hierarchical paths like /+5V_USB should match +5V_USB in confirmed_voltages.
    Regression for ESP32-PoE2 where /+5V_USB was flagged WARN despite being a confirmed rail.
    """
    components = [
        ComponentIR(refdes="U9", part_number="CH340X", value="", pins=[
            PinIR(pin_id="7", pin_name="VCC", net="/+5V_USB"),
        ]),
    ]
    nets = [NetIR(name="/+5V_USB", pins=[("U9", "7"), ("C5", "1"), ("R1", "2")])]
    confirmed_voltages = {"+5V_USB": 5.0}

    results = check_structural_integrity(components, confirmed_voltages, [], nets)
    assert len(results) == 1
    assert results[0].status == "PASS", \
        f"Expected PASS, got {results[0].status}: {results[0].evidence_label}"


def test_normalize_strips_combined_prefixes():
    """normalize_net_name handles /, +, - prefix combinations."""
    from steps.net_name_utils import normalize_net_name
    assert normalize_net_name("/+5V_USB") == "5V_USB"
    assert normalize_net_name("+3.3V")    == "3.3V"
    assert normalize_net_name("/GND")     == "GND"
    assert normalize_net_name("VCC_3V3")  == "VCC_3V3"
    assert normalize_net_name("/-12V")    == "12V"
    assert normalize_net_name("+/3V3")    == "3V3"
    assert normalize_net_name("")         == ""
    assert normalize_net_name(None)       == ""


def test_recognized_rail_passes_even_when_single_pin_on_sheet():
    """
    Per-sheet hierarchical netlist exports show a global rail as single-pin
    on a sub-sheet. Confirmed rail membership beats pin count — should PASS.

    Regression for Arduino UNO R4 WiFi: TXB0108 VCCB on +5V was FAIL because
    the regulator and decoupling live on Power.net, not the ESP32-S3-MINI sheet.
    """
    components = [
        ComponentIR(refdes="U4", part_number="TXB0108DQSR", value="", pins=[
            PinIR(pin_id="16", pin_name="VCCB", net="+5V"),
        ]),
    ]
    nets = [NetIR(name="+5V", pins=[("U4", "16")])]   # single-pin on this sheet
    confirmed_voltages = {"+5V": 5.0}

    results = check_structural_integrity(components, confirmed_voltages, [], nets)
    assert len(results) == 1
    assert results[0].status == "PASS", \
        f"Expected PASS for recognized rail, got {results[0].status}: {results[0].evidence_label}"


def test_recognized_ground_passes_even_when_single_pin_on_sheet():
    """Same as above for ground nets."""
    components = [
        ComponentIR(refdes="U4", part_number="TXB0108DQSR", value="", pins=[
            PinIR(pin_id="8", pin_name="GND", net="GND"),
        ]),
    ]
    nets = [NetIR(name="GND", pins=[("U4", "8")])]
    ground_nets = ["GND"]

    results = check_structural_integrity(components, {}, ground_nets, nets)
    assert len(results) == 1
    assert results[0].status == "PASS"


def test_unrecognized_single_pin_power_still_fails():
    """Reorder didn't break genuine-floating detection — unrecognized single-pin → FAIL."""
    components = [
        ComponentIR(refdes="U1", part_number="STM32", value="", pins=[
            PinIR(pin_id="23", pin_name="VDD", net="MYSTERY_NET"),
        ]),
    ]
    nets = [NetIR(name="MYSTERY_NET", pins=[("U1", "23")])]
    confirmed_voltages = {"+3.3V": 3.3}   # MYSTERY_NET not in confirmed_voltages

    results = check_structural_integrity(components, confirmed_voltages, [], nets)
    assert len(results) == 1
    assert results[0].status == "FAIL"
    assert "single-pin" in results[0].evidence_label.lower()


# ── prefix-broadening of power-pin recognition + FAIL-only guard (step_08c) ────

def test_decorated_power_pin_names_recognized():
    """Decorated names (prefix-only) are now recognized; exact-set still 'exact'."""
    from steps.step_08c_structural_checker import _power_pin_recognition
    for nm in ("VDD2A", "VDD_IO", "VCC+", "VCCPLL", "VCCO_45", "VDD33", "VDD5"):
        assert _is_power_pin_name(nm), nm
        assert _power_pin_recognition(nm) == "prefix", nm
    # exact-set members stay 'exact' (invariant)
    assert _power_pin_recognition("VDD") == "exact"
    assert _power_pin_recognition("VCC") == "exact"
    # non-power unaffected
    assert _power_pin_recognition("PA0") is None


def test_prefix_only_pin_single_pin_net_fails():
    """Prefix-only decorated pin on a single-pin/unconnected net → FAIL (recall recovery)."""
    components = [
        ComponentIR(refdes="U1", part_number="ESP32", value="", pins=[
            PinIR(pin_id="1", pin_name="VDD2A", net="/FLOATING_U1_1"),
        ]),
    ]
    nets = [NetIR(name="/FLOATING_U1_1", pins=[("U1", "1")])]
    confirmed_voltages = {"+3.3V": 3.3}
    results = check_structural_integrity(components, confirmed_voltages, [], nets)
    assert len(results) == 1
    assert results[0].status == "FAIL"
    assert "single-pin" in results[0].evidence_label.lower()


def test_prefix_only_pin_multipin_unconfirmed_net_suppressed():
    """Prefix-only decorated pin on a multi-pin unconfirmed rail → NO finding (guard).
    Net name is NON-rail-named so branch 2.5 does not intercept first — this
    isolates the 268c938 prefix-WARN guard. (A prefix-only pin on a *rail-named*
    multi-pin net now PASSes via branch 2.5; see
    test_rail_named_multipin_net_passes / recon §F.)"""
    components = [
        ComponentIR(refdes="U1", part_number="XC7A", value="", pins=[
            PinIR(pin_id="K10", pin_name="VCCINT_IO_K10", net="FPGA_CORE_NODE"),
        ]),
    ]
    nets = [NetIR(name="FPGA_CORE_NODE",
                  pins=[("U1", "K10"), ("U2", "L10"), ("C1", "1")])]
    confirmed_voltages = {"+3.3V": 3.3}   # net NOT confirmed by step_06
    results = check_structural_integrity(components, confirmed_voltages, [], nets)
    assert results == [], f"prefix-only multi-pin pin should be suppressed, got {results}"


def test_exact_set_pin_multipin_unconfirmed_still_warns_invariant():
    """INVARIANT: an exact-set pin (VDD) on a multi-pin unconfirmed NON-rail-named
    net still WARNs exactly as before — the guard must affect prefix-only pins
    only. (Net is non-rail-named so branch 2.5 does not convert it to PASS.)"""
    components = [
        ComponentIR(refdes="U1", part_number="STM32", value="", pins=[
            PinIR(pin_id="1", pin_name="VDD", net="FILTERED_SUPPLY_NODE"),
        ]),
    ]
    nets = [NetIR(name="FILTERED_SUPPLY_NODE",
                  pins=[("U1", "1"), ("U2", "8"), ("C1", "1")])]
    confirmed_voltages = {"+3.3V": 3.3}
    results = check_structural_integrity(components, confirmed_voltages, [], nets)
    assert len(results) == 1
    assert results[0].status == "WARN"


# ── Option ii: step_08c-local rail-NAME recognition (branch 2.5) ──────────────
# recon_step06_rail_confirmation_2026-06-08.md §E/§G. Recovers the 2 seed-masked
# M7 misses by PASSing a recognized power pin on a rail-named multi-pin net that
# step_06 left unconfirmed, without asserting any voltage.

def test_is_rail_name_recognizer():
    """Matches decorated/vendor rail names; rejects floating/synthetic/signal."""
    # Positives (the §E recognizer test vectors) — decorated & vendor rails.
    for n in ("/DVDD", "/nRF52_VDD", "/IOVDD", "/ADC_AVDD", "/USB_VDD",
              "VBUS", "VDD"):
        assert _is_rail_name(n), f"{n!r} should be recognized as a rail name"
    # Pre-widen recovered cases that must keep matching (regression guard).
    for n in ("P3V3_CLK", "5VREG", "+5V_AON", "VDD33"):
        assert _is_rail_name(n), f"{n!r} should still be recognized (regression)"
    # TODO-133 name-form widen positives — the recon's ~8 clean name-form rails,
    # tested via general FORMS (path-basename reduction, whole-word tokens, new
    # VSW/_PWR/_VBUS/_AUX+voltage forms), NOT literal-string membership.
    for n in (
        "/pic_sockets/VCC_PIC",              # hierarchical-prefix, basename VCC_*
        "/CAN bus/CAN12V_prot",             # path seg + domain-prefixed \d+V
        "/CAN12V_prot",                     # domain-prefixed voltage token
        "/Power Supply/Boost Converter Vin",  # whole-word VIN, path + spaces
        "/Buck Vin",                         # whole-word VIN
        "/Expansion connector/+3V3_AUX",     # \d+V token, path-prefixed
        "/USB Debug, PD/USBC0_VBUS",         # _VBUS suffix, path + spaces
        "/USB_VBUS",                         # _VBUS suffix
        "FLASH_PWR",                         # _PWR suffix
        "+VSW",                              # VSW switched-rail keyword
        # generalized siblings (so it's the FORM, not the 16 literals):
        "/sheet1/+12V_MOTOR",               # path-prefixed voltage rail
        "SENSOR_PWR",                        # _PWR suffix sibling
        "OTG_VBUS",                          # _VBUS suffix sibling
        "+5V_AUX",                           # \d+V + _AUX sibling
    ):
        assert _is_rail_name(n), f"{n!r} should be recognized as a rail name"
    # Negatives — generator floating nets, KiCad synthetics, signals, ground,
    # AND the recon's leave-as-WARN cases (topology / ambiguous / power-pin-only)
    # which must KEEP firing the structural WARN.
    for n in ("/FLOATING_U6_23", "/FLOATING_U5_18", "Net-(C1-Pad1)",
              "/SWDIO", "/GND", "/RESET",
              "Net-(D2-K)", "Net-(U1-gnd)",  # topology synthetic — leave WARN
              "Spare1", "Spare2",            # DNP/ambiguous — leave WARN
              "/ESP_EN", "/Power input/IN", "/IN",  # signal/ambiguous — leave WARN
              "+12C",                        # malformed (no V) — leave WARN
              "/SPI1_MOSI", "/UART_TX"):     # plausible signal nets — never a rail
        assert not _is_rail_name(n), f"{n!r} should NOT be a rail name"
    assert not _is_rail_name("")


def test_rail_named_multipin_net_passes():
    """Recognized power pin on a rail-named MULTI-pin net (step_06-unconfirmed)
    → PASS via branch 2.5. Mirrors the rp2040 /DVDD and Debugger /nRF52_VDD seeds."""
    components = [
        ComponentIR(refdes="U6", part_number="RP2040", value="", pins=[
            PinIR(pin_id="23", pin_name="DVDD", net="/DVDD"),
        ]),
    ]
    # Multi-pin net, but NOT in confirmed_voltages (step_06 missed the /-decoration).
    nets = [NetIR(name="/DVDD", pins=[("U6", "23"), ("C40", "1"), ("C41", "1")])]
    results = check_structural_integrity(components, {"+3V3": 3.3}, ["GND"], nets)
    assert len(results) == 1
    assert results[0].status == "PASS"
    assert "recognized rail name" in results[0].evidence_label.lower()


def test_rail_named_single_pin_net_still_fails():
    """MANDATORY gate regression: a rail-named SINGLE-pin net must still FAIL.
    The pin_count >= 2 gate preserves real-float detection — proves branch 2.5
    does not mask single-pin floats on coincidentally rail-named nets."""
    components = [
        ComponentIR(refdes="U6", part_number="RP2040", value="", pins=[
            PinIR(pin_id="23", pin_name="DVDD", net="/DVDD"),
        ]),
    ]
    nets = [NetIR(name="/DVDD", pins=[("U6", "23")])]  # single-pin → floating
    results = check_structural_integrity(components, {"+3V3": 3.3}, ["GND"], nets)
    assert len(results) == 1
    assert results[0].status == "FAIL"
    assert "floating" in results[0].evidence_label.lower()


def test_non_rail_named_multipin_unchanged():
    """Invariant: a non-rail-named multi-pin net routes through branch 6 → WARN
    exactly as before (branch 2.5 changes nothing for non-rail names)."""
    components = [
        ComponentIR(refdes="U1", part_number="STM32", value="", pins=[
            PinIR(pin_id="23", pin_name="VDD", net="/CUSTOM_SUPPLY_NODE"),
        ]),
    ]
    nets = [NetIR(name="/CUSTOM_SUPPLY_NODE", pins=[("U1", "23"), ("U2", "8")])]
    results = check_structural_integrity(components, {"+3V3": 3.3}, [], nets)
    assert len(results) == 1
    assert results[0].status == "WARN"
    assert "may be a custom rail" in results[0].evidence_label


# ── F5/TODO-172: defensive tripwire on the prefix-only `continue` branch ───────
# The branch at step_08c :288-289 emits no finding. The F5 recon found it is not
# reached by any bad-corpus mutant (M7 floats to a single-pin net → single-pin
# FAIL, never here) and near-zero on the good corpus — but it IS reachable by
# construction (see test_prefix_only_pin_multipin_unconfirmed_net_suppressed).
# These tests prove the tripwire fires and document, in code, the exact reach
# condition, while asserting the finding behavior (emit nothing) is unchanged.

def test_prefix_only_branch_tripwire_counts_and_logs(caplog):
    """A prefix-only decorated power pin (VCCINT_IO_K10) on a multi-pin, non-rail,
    step_06-unconfirmed net reaches the branch. Assert (1) it still emits NO
    finding (verdict-inert — guard behavior unchanged), (2) the tripwire counter
    increments, (3) exactly one WARNING fires naming the offending pin/refdes."""
    from steps.step_08c_structural_checker import (
        reset_unclaimed_power_pin_tripwire, unclaimed_power_pin_hits,
    )
    reset_unclaimed_power_pin_tripwire()
    components = [
        ComponentIR(refdes="U1", part_number="XC7A", value="", pins=[
            PinIR(pin_id="K10", pin_name="VCCINT_IO_K10", net="FPGA_CORE_NODE"),
        ]),
    ]
    nets = [NetIR(name="FPGA_CORE_NODE",
                  pins=[("U1", "K10"), ("U2", "L10"), ("C1", "1")])]
    confirmed_voltages = {"+3.3V": 3.3}   # net NOT confirmed by step_06
    with caplog.at_level(logging.WARNING,
                         logger="steps.step_08c_structural_checker"):
        results = check_structural_integrity(components, confirmed_voltages, [], nets)
    # (1) finding behavior unchanged — still suppressed
    assert results == [], f"branch must still emit no finding, got {results}"
    # (2) counter tripped
    assert unclaimed_power_pin_hits() == 1
    # (3) exactly one WARNING, naming the offending pin/refdes
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "VCCINT_IO_K10" in msg and "U1" in msg


def test_tripwire_logs_once_per_run(caplog):
    """Two branch hits in one run → counter=2 but only ONE log line (throttled)."""
    from steps.step_08c_structural_checker import (
        reset_unclaimed_power_pin_tripwire, unclaimed_power_pin_hits,
    )
    reset_unclaimed_power_pin_tripwire()
    components = [
        ComponentIR(refdes="U1", part_number="XC7A", value="", pins=[
            PinIR(pin_id="K10", pin_name="VCCINT_IO_K10", net="FPGA_CORE_NODE"),
        ]),
        ComponentIR(refdes="U3", part_number="XC7A", value="", pins=[
            PinIR(pin_id="A1", pin_name="VDD_AUXBANK", net="OTHER_CORE_NODE"),
        ]),
    ]
    nets = [
        NetIR(name="FPGA_CORE_NODE", pins=[("U1", "K10"), ("U2", "L10")]),
        NetIR(name="OTHER_CORE_NODE", pins=[("U3", "A1"), ("U4", "B2")]),
    ]
    with caplog.at_level(logging.WARNING,
                         logger="steps.step_08c_structural_checker"):
        results = check_structural_integrity(components, {"+3.3V": 3.3}, [], nets)
    assert results == []
    assert unclaimed_power_pin_hits() == 2
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1  # throttled to once per run


# ── NC gate (TODO-304 / FP-08C-NOCONN) ────────────────────────────────────────
# investigation/recon_reports/step08c_noconnect_recon.md — branch 4 only, gated
# on ir.pintypes carrying an exact "no_connect" token (compound pintype split
# on "+"). Branch 5 and every other branch are untouched by this gate.

def test_designer_nc_power_pin_demotes_to_warn():
    """kria U30 VDDA1P8 shape: power_in+no_connect on a synthetic unconnected
    net -> WARN with finding_code NC_POWER_PIN_INTENTIONAL, not FAIL."""
    components = [
        ComponentIR(refdes="U30", part_number="DP83867CRRGZR", value="", pins=[
            PinIR(pin_id="13", pin_name="VDDA1P8", net="unconnected-(U30-VDDA1P8-Pad13)"),
        ]),
    ]
    nets = [NetIR(name="unconnected-(U30-VDDA1P8-Pad13)", pins=[("U30", "13")])]
    pintypes = {("U30", "13"): "power_in+no_connect"}
    results = check_structural_integrity(components, {}, [], nets, pintypes=pintypes)
    assert len(results) == 1
    assert results[0].status == "WARN"
    assert results[0].finding_code == "NC_POWER_PIN_INTENTIONAL"
    assert "designer-asserted no-connect" in results[0].evidence_label
    assert "verify datasheet permits leaving this pin unconnected" in results[0].evidence_label


def test_designer_nc_ground_pin_demotes_to_warn():
    """interf_u BUS1 GND shape: passive+no_connect (ground pin, not power) on
    a synthetic unconnected net -> WARN, same finding_code."""
    components = [
        ComponentIR(refdes="BUS1", part_number="BUSPC", value="", pins=[
            PinIR(pin_id="10", pin_name="GND", net="unconnected-(BUS1-GND-Pad10)"),
        ]),
    ]
    nets = [NetIR(name="unconnected-(BUS1-GND-Pad10)", pins=[("BUS1", "10")])]
    pintypes = {("BUS1", "10"): "passive+no_connect"}
    results = check_structural_integrity(components, {}, [], nets, pintypes=pintypes)
    assert len(results) == 1
    assert results[0].status == "WARN"
    assert results[0].finding_code == "NC_POWER_PIN_INTENTIONAL"


def test_bare_pintype_still_fails():
    """Mitayi-Pico shape: pintype is bare 'power_in' (no no_connect suffix) on
    a synthetic unconnected net -> unchanged FAIL, no finding_code."""
    components = [
        ComponentIR(refdes="U1", part_number="RP2040", value="", pins=[
            PinIR(pin_id="8", pin_name="VCC", net="unconnected-(U1-VCC-Pad8)"),
        ]),
    ]
    nets = [NetIR(name="unconnected-(U1-VCC-Pad8)", pins=[("U1", "8")])]
    pintypes = {("U1", "8"): "power_in"}
    results = check_structural_integrity(components, {}, [], nets, pintypes=pintypes)
    assert len(results) == 1
    assert results[0].status == "FAIL"
    assert results[0].finding_code is None
    assert "truly floating" in results[0].evidence_label


def test_missing_pintypes_entry_still_fails():
    """(ref, pin) absent from the pintypes dict entirely -> unchanged FAIL,
    identical to omitting pintypes altogether (the pre-existing-caller path)."""
    components = [
        ComponentIR(refdes="U1", part_number="STM32", value="", pins=[
            PinIR(pin_id="23", pin_name="VDD", net="unconnected-(U1-Pad23)"),
        ]),
    ]
    nets = [NetIR(name="unconnected-(U1-Pad23)", pins=[("U1", "23")])]
    results = check_structural_integrity(components, {}, [], nets, pintypes={})
    assert len(results) == 1
    assert results[0].status == "FAIL"
    assert results[0].finding_code is None

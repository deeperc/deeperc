"""Direction 1 closed-set trailing-decorator-suffix tolerance (TODO-303 2b,
investigation/recon_reports/todo303_phase1_recon.md / wild_netname_recognition_recon.md
Shape B).

`classify_net_name`'s anchored NET_NAME_PATTERNS don't tolerate a trailing
decorator (voltage rail, camera/bus instance index) after the role token —
'CCI0_I2C_SCL_3V3', 'SCL_CAM0'. The fix strips a DELIBERATELY CLOSED suffix
set (voltage-shaped, _VBUS, _CONN, _CAM<n>) before classification, never an
open alnum charclass — an open charclass already produced a real false
positive one step further out (FP-08E-I3C precedent): 'DP1_TXD0_HDMI_TX2_N',
a DisplayPort/HDMI TMDS differential lane, must never be swallowed as a
UART TX match.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from steps.peripheral_roles import classify_net_name, is_i3c_net_name, Role


# ── Positives: closed-set suffix strips, remainder classifies ───────────────

def test_i2c_scl_camera_interface_voltage_and_instance_prefix():
    ra = classify_net_name('CCI0_I2C_SCL_3V3')
    assert ra is not None
    assert ra.role == Role.I2C_CLOCK


def test_i2c_scl_camera_index_suffix():
    ra = classify_net_name('SCL_CAM0')
    assert ra is not None
    assert ra.role == Role.I2C_CLOCK


def test_i2c_scl_hdmi_voltage_suffix():
    ra = classify_net_name('HDMI_SCL_3V3')
    assert ra is not None
    assert ra.role == Role.I2C_CLOCK


def test_i2c_sda_camera_interface_voltage_suffix():
    ra = classify_net_name('CCI0_I2C_SDA_3V3')
    assert ra is not None
    assert ra.role == Role.I2C_DATA


def test_iterative_strip_two_stacked_suffixes():
    # Two closed-set suffixes stacked ('_CAM0' then '_3V3') must both strip.
    ra = classify_net_name('SCL_CAM0_3V3')
    assert ra is not None
    assert ra.role == Role.I2C_CLOCK


def test_voltage_suffix_variants_all_strip():
    # _5V has no second digit group — \d* must be zero-or-more, not one-or-more.
    for suffix, name in [
        ('_3V3', 'SDA_3V3'), ('_5V', 'SDA_5V'), ('_1V8', 'SDA_1V8'),
        ('_1V2', 'SDA_1V2'), ('_2V5', 'SDA_2V5'), ('_12V', 'SDA_12V'),
        ('_1V1', 'SDA_1V1'),
    ]:
        ra = classify_net_name(name)
        assert ra is not None and ra.role == Role.I2C_DATA, f'{name} (suffix {suffix}) failed to strip/classify'


def test_vbus_and_conn_literal_suffixes_strip():
    assert classify_net_name('TXD_VBUS').role == Role.UART_TX
    assert classify_net_name('RXD_CONN').role == Role.UART_RX


# ── Mandatory regression negative: no open-charclass FP shape ───────────────

def test_hdmi_tmds_differential_lane_stays_unclassified():
    """The exact FP-08E-I3C-shaped precedent this closed-set design exists to
    avoid: 'DP1_TXD0_HDMI_TX2_N' must NOT match UART_TX. Trailing '_N' is not
    in the closed suffix set, so nothing strips and the unmodified anchored
    TX pattern (require TX/TXD at the literal end of string) still rejects
    it, exactly as before this change."""
    assert classify_net_name('DP1_TXD0_HDMI_TX2_N') is None


def test_open_charclass_would_have_matched_but_closed_set_does_not():
    # A generic non-closed-set mixed-alnum suffix must NOT strip — proves the
    # implementation is a closed set, not an open one hiding behind the tests
    # above. ('SCL_FOO' is excluded as a case here: it already matches TODAY,
    # unrelated to this change, via NET_NAME_PATTERNS' own pre-existing
    # trailing `_[A-Z]+` branch — a pure-letter suffix, not a mixed-alnum one.)
    assert classify_net_name('SCL_ABC123') is None
    assert classify_net_name('SDA_XYZ123') is None


# ── I3C gate stays upstream of the new suffix-stripped match path ───────────

def test_i3c_gate_wins_over_suffix_stripped_i2c_match():
    """'I3C1_SDA_1V8': '_1V8' strips cleanly to 'I3C1_SDA', which the bare I2C
    SDA pattern would otherwise match — but is_i3c_net_name must still exclude
    it. Proves the I3C gate runs on the POST-strip name, not just the
    pre-strip one, so widening the suffix tolerance cannot reopen the I3C band
    the card-252 fix closed."""
    assert is_i3c_net_name('I3C1_SDA_1V8')
    assert classify_net_name('I3C1_SDA_1V8') is None


def test_i3c_gate_wins_with_camera_and_voltage_suffix():
    assert classify_net_name('I3C0_SCL_CAM0') is None
    assert classify_net_name('MIPI_I3C0_SDA_3V3') is None


# ── Direction 2 (TODO-303 2c): Net-(...)/unconnected-(...) unwrap ───────────
# investigation/recon_reports/todo303_phase1_recon.md T2/T3. KiCad auto-
# generates a compound 'Net-(REF-PINFUNC{slash}PINFUNC...)' name for any pin
# with no user net label, embedding every alt-function the pin's symbol
# declares. Unwrap, classify each element, and either resolve (exactly one
# distinct role) or decline (0 or >1 distinct roles) — never guess an
# ordering-ambiguous chain (card requirement R2).

# The recon's exact 7 role-bearing Shape-F chains from the 8-board sample
# (T3): 6 single-role reproduce their stated role, 1 multi-role declines.

def test_d2_chain_sd_card_cd_dat3_cs():
    ra = classify_net_name('Net-(MICRO_SD1-CD{slash}DAT3{slash}CS)')
    assert ra is not None and ra.role == Role.SPI_CHIP_SELECT


def test_d2_chain_sd_card_clk_sclk():
    ra = classify_net_name('Net-(MICRO_SD1-CLK{slash}SCLK)')
    assert ra is not None and ra.role == Role.SPI_CLOCK


def test_d2_chain_rxd2_rmiisel():
    ra = classify_net_name('Net-(U7-RXD2{slash}RMIISEL)')
    assert ra is not None and ra.role == Role.UART_RX


def test_d2_chain_rxd3_phyad2():
    ra = classify_net_name('Net-(U7-RXD3{slash}PHYAD2)')
    assert ra is not None and ra.role == Role.UART_RX


def test_d2_chain_rxer_rxd4_phyad0():
    ra = classify_net_name('Net-(U7-RXER{slash}RXD4{slash}PHYAD0)')
    assert ra is not None and ra.role == Role.UART_RX


def test_d2_chain_unconnected_nint_txer_txd4():
    ra = classify_net_name('unconnected-(U7-NINT{slash}TXER{slash}TXD4-Pad18)')
    assert ra is not None and ra.role == Role.UART_TX


def test_d2_chain_multi_role_declines():
    """The recon's one multi-role example: SPI_SCK -> SPI_CLOCK and SDA ->
    I2C_DATA are 2 distinct roles in the same alt-fn chain — decline rather
    than guess which one is real (card requirement R2)."""
    assert classify_net_name('Net-(U4-PB53{slash}SPI_SCK{slash}MCK{slash}SDA)') is None


# ── Wrapper-grammar negatives ─────────────────────────────────────────────

def test_mid_string_net_dash_occurrence_is_not_treated_as_wrapper():
    """'Net-' appearing mid-string (not the anchored wrapper shape) must not
    be parsed as Direction-2 syntax — the name falls straight through to
    plain classification, proven here by a real SDA match surviving the
    embedded 'Net-' token unchanged."""
    ra = classify_net_name('I2C_Net-work_SDA')
    assert ra is not None and ra.role == Role.I2C_DATA


def test_unclosed_wrapper_falls_through_to_plain_classification():
    # No closing paren -> not wrapper-shaped -> plain path (no crash, no
    # mis-extraction). Plain path also finds no role in this fragment.
    assert classify_net_name('Net-(U1-SDA') is None


def test_empty_payload_wrapper_falls_through():
    # 'Net-()' -- empty parens have no payload ((.+) requires >=1 char) so
    # this isn't wrapper-shaped either; falls through without crashing.
    assert classify_net_name('Net-()') is None


# ── classify_net_name stays truthful on unconnected-(...); 2a's consumer-side
# guards are the containment layer, not a second refusal here ──────────────

def test_unconnected_wrapper_with_role_bearing_payload_now_classifies():
    """classify_net_name MUST NOT itself refuse an unconnected-(...) name —
    that containment is peripheral_coherence/peripheral_bus_pairing's job
    (Phase 2a). This function's only responsibility is truthful
    classification of what the wrapper actually contains."""
    ra = classify_net_name('unconnected-(U1-SDA-Pad5)')
    assert ra is not None and ra.role == Role.I2C_DATA


# ── Direction 4 (TODO-303 2d): '~{X}' active-low escape strip ─────────────

def test_d4_bare_tilde_brace_cs():
    ra = classify_net_name('~{CS}')
    assert ra is not None and ra.role == Role.SPI_CHIP_SELECT


def test_d4_tilde_brace_espi_cs1_matches_today():
    """'~{ESPI_CS1}' -> stripped 'ESPI_CS1' DOES match the SPI chip-select
    pattern -- verified directly, not assumed. The pattern's leading
    '(?:^|[_/])' only requires a '_'/'/'-boundary, not true string-start, so
    the '_CS1' tail of 'ESPI_CS1' satisfies it (pattern.search('ESPI_CS1')
    matches '_CS1'). This differs from Phase 1's T3 census, which recorded
    this exact net as a no-role Shape-F element -- that was true only
    because the brace was still present (the trailing '}' after 'CS1' broke
    the pattern's '$' anchor); D4 changes the outcome by removing the brace
    before matching, not by touching the pattern itself."""
    ra = classify_net_name('~{ESPI_CS1}')
    assert ra is not None and ra.role == Role.SPI_CHIP_SELECT


def test_d4_stacked_suffix_outside_braces():
    """'~{CS}_3V3' -- D1 suffix sits OUTSIDE the braces."""
    ra = classify_net_name('~{CS}_3V3')
    assert ra is not None and ra.role == Role.SPI_CHIP_SELECT


def test_d4_stacked_suffix_inside_braces():
    """'~{CS_3V3}' -- D1 suffix sits INSIDE the braces. D1's closed-set regex
    is end-anchored ($) and can't see past a trailing '}', so D4 must strip
    the brace BEFORE D1 strips the suffix for this shape to resolve -- this
    proves the ordering, not just the outcome."""
    ra = classify_net_name('~{CS_3V3}')
    assert ra is not None and ra.role == Role.SPI_CHIP_SELECT


def test_d4_interplay_with_d2_unwrap_per_element():
    """Tilde-brace inside a D2 compound-name element ('~{CS}' as one alt-fn
    in a Net-(...) chain) strips correctly via the same recursive
    classify_net_name call D2 already uses for Direction 1 -- no duplicated
    logic needed for D4 either."""
    ra = classify_net_name('Net-(REF-~{CS}{slash}PF20)')
    assert ra is not None and ra.role == Role.SPI_CHIP_SELECT


def test_d4_interplay_with_unconnected_wrapper_element():
    ra = classify_net_name('unconnected-(J12D-LPC_SERIRQ{slash}~{ESPI_CS1})')
    assert ra is not None and ra.role == Role.SPI_CHIP_SELECT


def test_d4_i3c_gate_holds_after_tilde_strip():
    """The I3C gate (is_i3c_net_name) must still exclude an I3C-shaped name
    even after D4's brace-strip would otherwise let 'SDA' reach the I2C
    pattern -- D4 must not create a new I3C-band leak (card 252)."""
    assert classify_net_name('~{I3C1_SDA}') is None


def test_d4_no_tilde_brace_untouched():
    """A name with no '~{...}' occurrence at all is completely unaffected by
    this phase's change -- pre-existing D1/D2 behavior stands."""
    ra = classify_net_name('CCI0_I2C_SCL_3V3')
    assert ra is not None and ra.role == Role.I2C_CLOCK

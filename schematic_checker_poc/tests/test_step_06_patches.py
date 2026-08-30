"""
Unit tests for Step-06 power-inference deterministic classification:
  Patch 1 — is_definitely_signal (upstream signal filter)
  Tier-1  — is_power_net_deterministic (rail-name classification)

No Ollama dependency — pure function tests.
"""
import pytest

from steps.step_06_power import (
    is_definitely_signal,
    is_power_net_deterministic,
)


# ── Patch 1: is_definitely_signal ─────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "/IO0", "/IO31", "/GPIO12", "/PORTB3",          # numbered GPIO/IO/port
    "/GPIO0/XTAL1/CLKIN",                            # compound GPIO (prefix match)
    "/PA0", "/PB3",                                  # true GPIO ports (narrowed bare-p)
    "Net-(C6-Pad1)", "Net-(U2-FB)", "Net-(U4-SW)",  # synthetic, no rail token
    "Net-(D1-K)", "Net-(F1-Pad2)",                   # synthetic, no rail token
    "NET_C1_PAD2",                                   # KiCad synthetic pad
    "unconnected-(#FLG06-pwr-Pad1)",                 # KiCad unconnected (scoped out)
    "/ESP_EN", "/MCU_RESET", "/USB_BOOT", "/SPI_CS", # enable/reset/boot/cs w/ sep
    "/EN", "RST", "/NRST", "BOOT",                   # standalone control
    "/I2C_SDA", "/SPI_MOSI", "/UART_TX", "/USB_DM",  # comm lines w/ sep
    "SDA", "/SCL", "MOSI", "/D+",                     # standalone comm
    "/MCU_CLK", "/SYS_XTAL1", "/REF_OSC2",           # clock lines w/ sep
    "CLK", "/XTAL", "/OSC1", "XI",                    # standalone clock
])
def test_definitely_signal_positive(name):
    assert is_definitely_signal(name) is True


@pytest.mark.parametrize("name", [
    "/+3V3", "/VCC", "/+5V_SOM", "/AVDD", "SYS_VIN_HV",  # real rails — never filter
    "/P3V3",                                              # P3V3 rail, not a port
    "VCC_3V3", "+5V", "VBAT", "PWR_MAIN",                 # more rails
    "",                                                   # empty
])
def test_definitely_signal_negative(name):
    assert is_definitely_signal(name) is False


def test_definitely_signal_case_insensitive():
    assert is_definitely_signal("/Io0") is True
    assert is_definitely_signal("/gPiO5") is True


# ── Patch 1 / Option B: rail-token rescue for synthetic nets ──────────────────

@pytest.mark.parametrize("name", [
    "Net-(U1-VBAT)", "Net-(U11-VIn)", "Net-(U6-VDD)",   # V-prefixed supply token
    "Net-(BAT1-Pad1)", "Net-(BAT/BUT1-Pad3)",           # battery token + digit/sep
    "Net-(BAT_SENS_E1-Pad1)",                            # battery token + underscore
    "Net-(5.0V/3.3V1-Pad2)",                             # decimal voltage literal
    "NET_VBAT_PAD1",                                     # NET_…_PAD synthetic shape
])
def test_synthetic_with_rail_token_rescued(name):
    # Option B: rail isolated behind a passive — hand to Gemma, do not filter.
    assert is_definitely_signal(name) is False


@pytest.mark.parametrize("name", [
    "Net-(C6-Pad1)", "Net-(U2-FB)", "Net-(U4-SW)",      # no rail token → filtered
    "Net-(D1-K)", "Net-(F1-Pad2)",                       # passive pad, no token
    "unconnected-(#FLG06-pwr-Pad1)",                     # 'pwr' but not synthetic shape
    "/3V3_EN", "/5V_RST",                                # rail token but signal suffix
])
def test_rail_token_rescue_is_scoped(name):
    # The rescue must not leak to non-synthetic shapes or token-free synthetics.
    assert is_definitely_signal(name) is True


# ── Patch 1 / voltage-prefix exemption (corpus_run_v2_10 regression fix) ──────

@pytest.mark.parametrize("name", [
    "P3V3_CLK", "P5V_CLK", "P12V_OSC",   # P-convention rail + signal suffix
    "V3V3_SCK",                          # V-convention rail + signal suffix
    "+5V_CLK", "+3V3_SCK", "-12V_XTAL",  # signed rail + signal suffix
    "VDD_OSC", "AVDD_CLK", "VCC_SCK",    # named rail + signal suffix
    "/P3V3_CLK",                         # leading-slash form
])
def test_voltage_prefixed_names_not_signal(name):
    # A name that starts like a voltage rail is never a signal, regardless of
    # the suffix token it carries.
    assert is_definitely_signal(name) is False


@pytest.mark.parametrize("name", [
    "/3V3_EN", "/5V_RST",                # bare \d+V_ strapping — stay filtered
    "MCU_CLK", "SDA",                    # no voltage prefix — regular signals
])
def test_voltage_prefix_exemption_is_scoped(name):
    # The exemption must not leak to bare-digit strapping names (locked
    # Option-B decision) or to ordinary signals.
    assert is_definitely_signal(name) is True


# ── Tier-1: decimal-form +N.NV rail names (v2_10 ESC_Board regression fix) ────

@pytest.mark.parametrize("name", [
    "+3.3V", "+5.0V", "-12.5V", "+1.8V",   # decimal form — newly classified
    "+3V3", "+5V", "-12V",                  # integer form — unchanged
    "V3V3", "P3V3", "VCC", "GND",           # other rail forms — unchanged
])
def test_decimal_voltage_names_classified_power(name):
    assert is_power_net_deterministic(name) is True


@pytest.mark.parametrize("name", [
    "3.3", "FOO", "+3.3VBUS_SENSE", "SDA", "/IO0",
])
def test_decimal_voltage_regex_anchored(name):
    # The decimal branch is anchored ^(?:...)$ — partial/embedded matches and
    # non-rail names must not be classified as power.
    assert is_power_net_deterministic(name) is False

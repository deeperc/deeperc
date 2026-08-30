"""TODO-303 2d — wild-shaped constructed fixtures (card fixture mandate).

Three hand-authored, minimal netlists under fixtures/todo303_wild_shaped/,
verified by calling the REAL production coherence functions
(peripheral_coherence.check_i2c_coherence / check_spi_coherence) against a
parsed IR (steps.step_02_parser.parse_netlist) with the real, disk-loaded
peripheral KB (pipeline.get_peripheral_kb/get_peripheral_routing) — the exact
call chain step_08d_peripheral_checker.py uses in production, just invoked
directly rather than through the full run_board pipeline.

SCOPING DECISION (documented, not silent): these fixtures are NOT wired into
generate_bad_corpus.py's SEM_SEEDS/freeze-manifest/EXPECTED_CHECKER_OUTCOME
machinery, and do NOT count toward the recall harness's M6/M12 HIT/MISS
accounting. That integration touches the frozen bad-corpus freeze (376 files,
seed=1234) and the generator's per-operator catalog — a much larger, riskier
change than "add 3 fixtures" warrants as a same-cycle addition, and is left as
an explicit follow-up (see the 2d report's discovered-work section) rather
than attempted in a single unreviewed pass. This test file is the actual
verification for this phase: it proves, with the REAL production check
functions and REAL KB, that (1) a wild-shaped (Net-(...)/D4-tilde) SDA/SCL
swap is caught, (2) a wild-shaped SPI MOSI/MISO swap is caught, and (3) a
correctly-wired KB'd-doubles collateral bus with wild+suffixed names produces
ZERO findings (the CLAUDE.md KB-evidence rule's FP-validation mandate).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pipeline
from steps.step_02_parser import parse_netlist
from steps.peripheral_coherence import check_i2c_coherence, check_spi_coherence
from steps.step_08d_peripheral_checker import canonicalize_mpn_for_kb

_FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "fixtures", "todo303_wild_shaped")


def _fixture(name):
    return parse_netlist(os.path.join(_FIXTURES_DIR, name))


def test_m6_wild_shaped_swap_is_caught():
    """SDA/SCL genuinely swapped, both nets named in the D2 auto-gen wrapper
    shape ('Net-(U1-I2C1_SCL{slash}PB6)' / 'Net-(U1-I2C1_SDA{slash}PB7)') —
    unrecognizable before this arc, caught now via classify_net_name's D2
    unwrap. Both swapped pins FAIL, each against the other's implied role."""
    kb = pipeline.get_peripheral_kb()
    routing = pipeline.get_peripheral_routing()
    violations = check_i2c_coherence(_fixture("m6_wild_shaped_swap.net"),
                                      kb, routing, canonicalize_mpn_for_kb)
    by_pin = {(v.refdes, v.pin_id): v for v in violations}
    assert len(violations) == 2
    assert by_pin[("U2", "1")].pin_role == "I2C_DATA"
    assert by_pin[("U2", "1")].net_role == "I2C_CLOCK"
    assert by_pin[("U2", "1")].status == "FAIL"
    assert by_pin[("U2", "2")].pin_role == "I2C_CLOCK"
    assert by_pin[("U2", "2")].net_role == "I2C_DATA"
    assert by_pin[("U2", "2")].status == "FAIL"


def test_m12_wild_shaped_swap_is_caught():
    """MOSI/MISO genuinely swapped, both nets named in a D2-wrapped + D4
    tilde-brace-escaped shape ('Net-(U1-~{SPI0_MOSI}{slash}PA7)' /
    'Net-(U1-~{SPI0_MISO}{slash}PA6)') — exercises D2+D4 together, not D4
    alone. Both swapped pins FAIL."""
    kb = pipeline.get_peripheral_kb()
    violations = check_spi_coherence(_fixture("m12_wild_shaped_swap.net"),
                                      kb, canonicalize_mpn_for_kb)
    by_pin = {(v.refdes, v.pin_id): v for v in violations}
    assert len(violations) == 2
    assert by_pin[("U2", "1")].pin_role == "SPI_DATA_OUT"
    assert by_pin[("U2", "1")].net_role == "SPI_DATA_IN"
    assert by_pin[("U2", "1")].status == "FAIL"
    assert by_pin[("U2", "2")].pin_role == "SPI_DATA_IN"
    assert by_pin[("U2", "2")].net_role == "SPI_DATA_OUT"
    assert by_pin[("U2", "2")].status == "FAIL"


def test_negative_kb_doubles_correctly_wired_bus_produces_zero_findings():
    """CLAUDE.md's KB-evidence rule: 'FP validation must include KB'd-doubles
    fixtures'. U1 is a real KB'd part (STM32F103C8T6) whose PB6/PB7 pin
    FUNCTION is deliberately generic ('PB6'/'PB7', not 'SCL'/'SDA') so the
    role assertion can ONLY come from the KB source (kb_role_lookup) — the
    exact source this arc's widened classify_net_name could newly agree OR
    disagree with. The bus is correctly wired (PB6->SCL-net, PB7->SDA-net),
    both nets wild-wrapped AND D1-suffix-decorated
    ('Net-(U1-I2C1_SCL_3V3{slash}PB6)') — must produce ZERO findings: a
    KB-sourced role that AGREES with a newly-recognized net name must stay
    silent, never manufacture a false FAIL out of newly-armed classification."""
    kb = pipeline.get_peripheral_kb()
    routing = pipeline.get_peripheral_routing()
    violations = check_i2c_coherence(_fixture("negative_kb_doubles_correct_i2c.net"),
                                      kb, routing, canonicalize_mpn_for_kb)
    assert violations == []

"""TODO-272 Phase 2b — serialization-only test: the step_08g Family 1
`activation` field (od_pintype | i2c_net_name | None) round-trips into the
live report JSON via `step_10_report.build_report`. Field addition only — no
verdict logic under test here (see step_08g_pullup_presence.py's own suite,
tests/test_pullup_presence.py, for the gate-logic coverage).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from steps.step_10_report import build_report
from steps.step_08g_pullup_presence import (
    PullupPresenceFinding,
    FAMILY_GENERIC_OD, FAMILY_FLASH_CS,
    VIOLATION_I2C_NAME_NO_PULLUP, VIOLATION_FLASH_CS_NO_PULLUP,
    ACTIVATION_I2C_NET_NAME, ACTIVATION_OD_PINTYPE,
)


def _build(tmp_path, findings):
    out = tmp_path / "report.json"
    return build_report(
        source_netlist="",
        results=[],
        confirmed_voltages={},
        extraction_metadata={},
        output_path=str(out),
        pullup_presence_results=findings,
    )


def test_pullup_presence_activation_field_round_trips_name_path(tmp_path):
    finding = PullupPresenceFinding(
        net="/X/I2C1_SDA_B", family=FAMILY_GENERIC_OD,
        violation=VIOLATION_I2C_NAME_NO_PULLUP,
        pins=["U1.1", "U2.2"], evidence="no pull-up",
        corroboration=["U1.SCL"], activation=ACTIVATION_I2C_NET_NAME,
    )
    report = _build(tmp_path, [finding])
    rows = report["pullup_presence_results"]
    assert len(rows) == 1
    assert rows[0]["activation"] == "i2c_net_name"
    assert rows[0]["violation"] == VIOLATION_I2C_NAME_NO_PULLUP


def test_pullup_presence_activation_field_round_trips_od_path():
    finding = PullupPresenceFinding(
        net="SIG", family=FAMILY_GENERIC_OD, violation="OPEN_DRAIN_NO_PULLUP",
        pins=["U1.13"], evidence="no pull-up", activation=ACTIVATION_OD_PINTYPE,
    )
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        report = build_report(
            source_netlist="", results=[], confirmed_voltages={},
            extraction_metadata={}, output_path=f"{d}/report.json",
            pullup_presence_results=[finding],
        )
    assert report["pullup_presence_results"][0]["activation"] == "od_pintype"


def test_pullup_presence_activation_field_none_for_families_without_alternate_entry(tmp_path):
    """Families 2/3 findings never set `activation` — the dataclass default
    (None) must serialize as JSON null, not be dropped or mislabeled."""
    finding = PullupPresenceFinding(
        net="/QSPI_SS", family=FAMILY_FLASH_CS,
        violation=VIOLATION_FLASH_CS_NO_PULLUP,
        pins=["U1.6"], evidence="no pull-up",
    )
    report = _build(tmp_path, [finding])
    assert report["pullup_presence_results"][0]["activation"] is None

"""I3C-into-I2C-band discrimination (card 252, D1 per
investigation/experiments/i3c_band_recon/REPORT.md).

I3C is a distinct protocol from I2C — push-pull for SDR signalling, no SCL
pull-up needed at all, and a high-value SDA pull-up (open-drain phases/hot-join
only) is normal. Before this fix, an I3C-named net (e.g. 'I3C0_SCL_3V3') was
misclassified as I2C at TWO independent layers: `peripheral_roles.classify_net_name`
(anchored, feeds M6/M14/M15 coherence+pairing) and step_08d's own bare-substring
`_I2C_NET_RE`/`_net_signal_hint`/`_classify_i2c_net` (the actual firing site for
the reported artix-dc-scm WARNs and step_08e's M4 pull-up-value check).

Four fixture groups, per the recon's Step 4 fixture set:
  1. Artix must-not-fire (the 8-net shape from the real board).
  2. I2C must-still-fire regression (direct classifier checks; full M4/M6
     suites are the broader regression net via `make test`).
  3. Adversarial name set (I3C token must win over embedded SDA/SCL; must NOT
     over-widen onto 'SDI3C_FOO').
  4. The false-FAIL locks: (a) the recon's live-verified PROTOCOL_MISMATCH
     repro (i3c_synthetic_probe.py topology, verbatim); (b) a genuinely
     name_prefix-paired bare I3C bus (KB instance omitted so pairing falls
     through to the name-prefix signal) locking the M14 CAPABILITY_MISMATCH
     path the recon code-traced but did not live-fixture.
"""
import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from steps.peripheral_roles import classify_net_name, is_i3c_net_name, Role
from steps.step_02_parser import ComponentIR, PinIR, NetIR, NetlistIR
from steps.step_08d_peripheral_checker import (
    check_i2c_peripheral, is_i2c_classified_net, _net_signal_hint,
    PeripheralViolation, Severity,
)
from steps.step_08e_pullup_value_checker import check_pullup_values
from steps.peripheral_kb import (
    Signal, KBSource, Peripheral, PinRole, PinFunctionEntry,
)


# ── Fixture 1: artix must-not-fire (the 8-net shape) ─────────────────────────
# Verbatim net names + value from
# investigation/experiments/wild_board_hunt/corpus_results_tranche1/reports/
# antmicro__artix-dc-scm.json (pullup_value_results) — bracket-index syntax,
# 357kOhm pull-ups, both endpoints KB-uncovered (real board: Artix-7 FPGA +
# generic level-shifter).

_ARTIX_I3C_NETS = [
    "I3C[0]_SCL_3V3", "I3C[0]_SDA_3V3",
    "I3C[1]_SCL_3V3", "I3C[1]_SDA_3V3",
    "I3C[2]_SCL_3V3", "I3C[2]_SDA_3V3",
    "I3C[3]_SCL_3V3", "I3C[3]_SDA_3V3",
]


def _artix_netlist():
    components = []
    nets = []
    for i, net_name in enumerate(_ARTIX_I3C_NETS):
        r_ref = f"R{45 + i}"
        r = ComponentIR(
            refdes=r_ref, part_number="", value="357k",
            pins=[PinIR("1", "", net_name), PinIR("2", "", "+3V3")],
        )
        components.append(r)
        nets.append(NetIR(name=net_name, pins=[(r_ref, "1"), ("U14", str(i)), ("U6", str(i))]))
    # U14 = XC7A100T-FGG484 (Artix-7 FPGA, soft-IP GPIO); U6 = NVT2008BQ,115
    # (generic level-shifter) — neither is peripheral-KB-covered.
    u14 = ComponentIR(refdes="U14", part_number="XC7A100T-FGG484", value="XC7A100T-FGG484",
                      pins=[PinIR(str(i), "", n) for i, n in enumerate(_ARTIX_I3C_NETS)])
    u6 = ComponentIR(refdes="U6", part_number="NVT2008BQ,115", value="NVT2008BQ,115",
                     pins=[PinIR(str(i), "", n) for i, n in enumerate(_ARTIX_I3C_NETS)])
    components.extend([u14, u6])
    nets.append(NetIR(name="+3V3", pins=[(f"R{45+i}", "2") for i in range(8)]))
    return NetlistIR(source_file="artix-dc-scm", components=components, nets=nets)


def test_artix_8net_shape_no_pullup_warn():
    """step_08e must emit ZERO findings on the real artix I3C nets (today: 8 WARN)."""
    findings = check_pullup_values(_artix_netlist())
    assert findings == []


def test_artix_8net_shape_not_i2c_classified():
    """step_08d's own per-net gate must not classify any of the 8 nets as I2C."""
    nl = _artix_netlist()
    for net in nl.nets:
        if net.name not in _ARTIX_I3C_NETS:
            continue
        assert is_i2c_classified_net(net, nl, {}, None) is False, net.name


# ── Fixture 2: I2C must-still-fire regression ────────────────────────────────
# Direct classifier-level regression net; the broader net is the full existing
# M4/M6 suites staying green under `make test`.

@pytest.mark.parametrize("net_name, expected_role", [
    ("I2C1_SDA", Role.I2C_DATA),
    ("I2C1_SCL", Role.I2C_CLOCK),
    ("SDA", Role.I2C_DATA),
    ("SCL", Role.I2C_CLOCK),
    ("TWI_SCL", Role.I2C_CLOCK),
])
def test_real_i2c_names_still_classify(net_name, expected_role):
    ra = classify_net_name(net_name)
    assert ra is not None, net_name
    assert ra.role == expected_role, net_name


@pytest.mark.parametrize("net_name, expected_signal", [
    ("I2C1_SDA", Signal.I2C_SDA),
    ("I2C1_SCL", Signal.I2C_SCL),
    ("SDA", Signal.I2C_SDA),
    ("SCL", Signal.I2C_SCL),
    ("TWI_SCL", Signal.I2C_SCL),
])
def test_real_i2c_names_still_signal_hint(net_name, expected_signal):
    assert _net_signal_hint(net_name) == expected_signal


def test_100ohm_i2c_pullup_still_fails():
    """A real I2C net (not I3C) with an out-of-band pull-up must still fire —
    the discrimination must not eat real I2C detection."""
    r1 = ComponentIR(refdes="R1", part_number="", value="100",
                     pins=[PinIR("1", "", "I2C1_SDA"), PinIR("2", "", "+3V3")])
    u1 = ComponentIR(refdes="U1", part_number="MCU", value="MCU",
                     pins=[PinIR("5", "SDA", "I2C1_SDA")])
    nl = NetlistIR(source_file="x", components=[r1, u1],
                   nets=[NetIR(name="I2C1_SDA", pins=[("R1", "1"), ("U1", "5")]),
                         NetIR(name="+3V3", pins=[("R1", "2")])])
    f = check_pullup_values(nl)
    assert len(f) == 1
    assert f[0].severity.name == "FAIL"


# ── Fixture 3: adversarial name set ──────────────────────────────────────────
# The I3C token must win over an embedded SDA/SCL substring; must NOT over-widen
# in the OTHER direction ('SDI3C_FOO' — I3C directly preceded by 'D', not a
# token boundary — stays unmatched by the I3C recognizer too).

@pytest.mark.parametrize("net_name", [
    "I3C0_SDA", "I3C0_SCL",              # bare, no rail suffix
    "I3C_SDA", "I3C_SCL",                # no instance digit
    "MIPI_I3C0_SCL",                     # prefixed
    "I3C_SCL_BUS",                       # trailing non-rail qualifier
    "I3C[0]_SCL_3V3", "I3C[0]_SDA_3V3",  # bracket-index (verbatim artix)
    "I3C[2]_SDA_3V3",
])
def test_adversarial_i3c_names_recognized(net_name):
    assert is_i3c_net_name(net_name) is True, net_name
    assert classify_net_name(net_name) is None, net_name
    assert _net_signal_hint(net_name) is None, net_name


def test_sdi3c_collision_not_recognized_as_i3c():
    """'SDI3C_FOO': 'I3C' is directly preceded by 'D' (no token boundary) — must
    not be recognized as I3C. It also matches no I2C pattern today (unchanged)."""
    assert is_i3c_net_name("SDI3C_FOO") is False
    assert classify_net_name("SDI3C_FOO") is None
    assert _net_signal_hint("SDI3C_FOO") is None


# ── Fixture 4: false-FAIL locks ───────────────────────────────────────────────

@dataclass
class _PinRef:
    pin_id: str
    net: str
    pin_name: str = ""


@dataclass
class _Component:
    refdes: str
    mpn: str
    pins: list
    @property
    def effective_mpn(self): return self.mpn


@dataclass
class _Net:
    name: str
    pins: list


@dataclass
class _Netlist:
    components: list
    nets: list
    ground_nets: list = field(default_factory=list)
    power_nets: list = field(default_factory=list)


def _make_kb(*entries):
    return {(e.mpn, e.pin_id): e for e in entries}


def _sensor(mpn, pin_id, signal, instance=None):
    return PinFunctionEntry(mpn, pin_id,
                             [PinRole(Peripheral.I2C, instance, signal, KBSource.VENDOR_XML)])


def _cap_fails(findings):
    return [f for f in findings
            if f.violation == PeripheralViolation.CAPABILITY_MISMATCH
            and f.severity == Severity.FAIL]


def _protocol_fails(findings):
    return [f for f in findings
            if f.violation == PeripheralViolation.PROTOCOL_MISMATCH
            and f.severity == Severity.FAIL]


def test_i3c_protocol_mismatch_lock():
    """Recon's live-verified repro (i3c_band_recon/i3c_synthetic_probe.py,
    verbatim topology): a bare-named I3C bus with one genuine fixed-function
    I2C device (U1) sharing the SCL net with an I3C-only KB'd device (U2, KB
    role = GPIO, no I2C) must NOT produce PROTOCOL_MISMATCH post-fix."""
    kb = _make_kb(
        _sensor("I2C_SENSOR", "P_SDA", Signal.I2C_SDA, "I2C0"),
        _sensor("I2C_SENSOR", "P_SCL", Signal.I2C_SCL, "I2C0"),
        PinFunctionEntry("MCU_I3C_HOST", "P_I3C_SCL",
                         [PinRole(Peripheral.GPIO, None, Signal.GPIO, KBSource.VENDOR_XML)]),
    )
    nl = _Netlist(
        components=[
            _Component("U1", "I2C_SENSOR", [_PinRef("P_SDA", "I3C0_SDA", "P_SDA"),
                                            _PinRef("P_SCL", "I3C0_SCL", "P_SCL")]),
            _Component("U2", "MCU_I3C_HOST", [_PinRef("P_I3C_SCL", "I3C0_SCL", "P_I3C_SCL")]),
        ],
        nets=[
            _Net("I3C0_SDA", [("U1", "P_SDA")]),
            _Net("I3C0_SCL", [("U1", "P_SCL"), ("U2", "P_I3C_SCL")]),
        ],
    )
    findings = check_i2c_peripheral(nl, kb, {})
    assert _protocol_fails(findings) == []
    assert all(f.violation != PeripheralViolation.MISSING_PERIPHERAL
              or f.severity != Severity.FAIL for f in findings), findings


def test_i3c_capability_mismatch_lock():
    """M14 variant the recon code-traced but did not live-fixture: a bare I3C
    bus that (pre-fix) name_prefix-pairs (KB instance omitted so the kb_instance
    signal doesn't consume the nets first) with a corroborating I2C-shaped
    voter and an incapable I3C-only member must NOT produce CAPABILITY_MISMATCH
    post-fix. Pre-fix this bus DOES name_prefix-pair (classify_net_name matches
    both bare 'I3C0_SDA'/'I3C0_SCL') and the capability path fires (has_incapable
    and >=1 voter) — this is the false-FAIL the recon code-traced."""
    kb = _make_kb(
        # No instance -> pair_buses' kb_instance (signal-1) skips these pins
        # entirely (r.instance is None), forcing pairing through name-prefix
        # (signal-2) if the net names classify as I2C — the exact bare-name
        # exposure the recon flagged in peripheral_bus_pairing's stem parsing.
        _sensor("I2C_SENSOR", "P_SDA", Signal.I2C_SDA, instance=None),
        _sensor("I2C_SENSOR", "P_SCL", Signal.I2C_SCL, instance=None),
        PinFunctionEntry("MCU_I3C_HOST", "P_I3C_SCL",
                         [PinRole(Peripheral.GPIO, None, Signal.GPIO, KBSource.VENDOR_XML)]),
    )
    nl = _Netlist(
        components=[
            _Component("U1", "I2C_SENSOR", [_PinRef("P_SDA", "I3C0_SDA", "P_SDA"),
                                            _PinRef("P_SCL", "I3C0_SCL", "P_SCL")]),
            _Component("U2", "MCU_I3C_HOST", [_PinRef("P_I3C_SCL", "I3C0_SCL", "P_I3C_SCL")]),
        ],
        nets=[
            _Net("I3C0_SDA", [("U1", "P_SDA")]),
            _Net("I3C0_SCL", [("U1", "P_SCL"), ("U2", "P_I3C_SCL")]),
        ],
    )
    assert _cap_fails(check_i2c_peripheral(nl, kb, {})) == []

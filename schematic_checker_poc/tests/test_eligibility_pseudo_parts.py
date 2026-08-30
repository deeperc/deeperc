"""Eligibility-gate provenance pseudo-part recognizer.

The gate must not count a real test point / solder jumper / connector whose VALUE
is a signal name (e.g. a TestPoint valued "SPI1 MOSI") as a blocking unresolvable
IC. Keyed on PROVENANCE (libsource part / footprint / refdes prefix), never the
value string — so genuinely oddly-named real ICs (ME6210-SOT89, BL4054B) still
block. See investigation/scoping_step02_netlabel_leakage_2026-06-15.md.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "schematic_checker_poc"))

import pytest
import run_checks as rct
from steps.step_02_parser import ComponentIR, parse

CORPUS = REPO / "netlist_corpus"
DATASHEETS = CORPUS / "datasheets"

# Two tests below read real boards out of the private netlist_corpus/, which is not
# shipped with the public export — guard them the way the rest of the suite guards
# board fixtures (test_missing_datasheets_dir.py, test_step_timing.py) so they skip
# rather than hard-fail where the corpus is absent. Tests 1-3 are pure unit tests
# over the recognizer and keep running everywhere. LT-15 Phase B.1.
STEMCELL_BOARD = CORPUS / "stm32/community/STeMCell/stemcell.net"
FLIGHT_COMPUTER_BOARD = CORPUS / "stm32/community/flight-computer/Flight Computer.net"


def _comp(refdes="", value="", footprint="", libsource_part=""):
    return ComponentIR(refdes=refdes, part_number=value, value=value, pins=[],
                       footprint=footprint, libsource_part=libsource_part)


# ── Test 1: TestPoint valued "SPI1 MOSI" → non-blocking pseudo-part ────────────
def test_testpoint_signal_value_is_pseudo_part():
    tp = _comp("TP4", "SPI1 MOSI", "TestPoint:TestPoint_Pad_D1.5mm", "TestPoint")
    assert rct.pseudo_part_provenance(tp.refdes, tp.footprint, tp.libsource_part) == "testpoint"
    assert rct.is_pseudo_part_by_provenance(tp) is True
    # by-refdes alone (legacy/no libsource) still recognized
    assert rct.pseudo_part_provenance("TP9", "", "") == "testpoint"


# ── Test 2: solder jumper + connector → non-blocking pseudo-parts ──────────────
def test_jumper_and_connector_are_pseudo_parts():
    jp = _comp("JP3", "SDA", "stemcell:SOLDER_JUMPER_3", "SolderJumper_3_Open")
    assert rct.is_pseudo_part_by_provenance(jp) is True
    sj = _comp("SJ3", "open", "OLIMEX_Jumpers-FP:SJ", "SJ")
    assert rct.is_pseudo_part_by_provenance(sj) is True
    conn = _comp("CAN1", "CANBUS", "OLIMEX_Connectors-FP:TB3-3.5mm", "CON3")
    assert rct.is_pseudo_part_by_provenance(conn) is True
    uext = _comp("CON2", "UEXT", "OLIMEX_Connectors-FP:UEXT_BH10R", "BH10S")
    assert rct.is_pseudo_part_by_provenance(uext) is True


# ── Test 3: over-filter guard — oddly-named REAL ICs must STILL block ──────────
def test_real_ics_with_odd_names_still_block():
    """Provenance, not name-shape: a real LDO/charger with U* refdes and an IC
    footprint must NOT be swept up as a pseudo-part."""
    me6210 = _comp("U5", "ME6210-SOT89", "Package_TO_SOT_SMD:SOT-89-3", "ME6210")
    bl4054 = _comp("U3", "BL4054B-42TPRN(SOT23-5)", "Package_TO_SOT_SMD:SOT-23-5", "BL4054B")
    assert rct.pseudo_part_provenance(me6210.refdes, me6210.footprint, me6210.libsource_part) is None
    assert rct.pseudo_part_provenance(bl4054.refdes, bl4054.footprint, bl4054.libsource_part) is None
    assert rct.is_pseudo_part_by_provenance(me6210) is False
    assert rct.is_pseudo_part_by_provenance(bl4054) is False
    # and a genuine MCU/flash IC
    assert rct.is_pseudo_part_by_provenance(
        _comp("U1", "W25Q128JVSIQ", "Package_SO:SOIC-8", "W25Q128JVS")) is False


# ── Test 4: corrected eligibility arithmetic on the two recon boards ──────────
@pytest.mark.skipif(not STEMCELL_BOARD.exists(), reason="corpus board fixture not on disk")
def test_stemcell_flips_eligible():
    """STeMCell: provenance fix removes 9 jumpers/connectors/crystal so the board
    crosses the 0.80 blocking-ratio threshold → eligible.

    The assertion is the eligibility *invariant* (eligible, blocking-ratio strictly
    below threshold), not an exact ratio: the precise value drifts with datasheet-
    cache contents (e.g. B5819W resolving lowered it 0.75→0.50) — a change orthogonal
    to the provenance recognizer under test (which only ever lowers the ratio, so the
    decision can only get more robustly eligible). The recognizer itself is covered
    directly by tests 1-3."""
    elig, resolved, blocking, _ = rct.is_eligible(
        CORPUS / "stm32/community/STeMCell/stemcell.net", DATASHEETS)
    total = len(resolved) + len(blocking)
    ratio = len(blocking) / total
    assert elig is True
    assert ratio < 0.80, f"got {ratio:.3f} — must be below the eligibility threshold"
    assert len(resolved) >= 1, "board must have at least one resolved real IC"


# ── Test: TODO-113 classifier tripwire + synthetic passive-format eligibility gate ─
#
# History (TODO-309, 2026-07-26): this test originally ran `is_eligible()` against
# the REAL Mitayi-Pico-D1.net + the live `netlist_corpus/datasheets/` cache and
# hardcoded the ratio it measured at calibration time (`eb299dc`, 2026-06-16):
# 0.867, ineligible. That expectation was live-cache-coupled and went stale: the
# TODO-304 cycle's wide 344-board corpus run (2026-07-23) legitimately downloaded
# `winbond/W25Q32JVSSIQ.pdf` (Mitayi shares this flash MPN with the already-eligible
# `tiny_tapeout` boards, and `is_eligible()`'s PDF-filename cache is shared across
# all boards by design), moving Mitayi's real ratio to exactly 0.800 — not `> 0.80`,
# so real-Mitayi eligibility is now `True`. That is CORRECT behavior, not a bug: see
# investigation/recon_reports/todo309_mitayi_eligibility_recon.md for the full
# per-part breakdown, cache-delta attribution, and verdict (the eligibility
# mechanism is fine; the test's hardcoded expectation was what went stale).
#
# The invariant actually worth protecting is not "the real board stays blocked"
# (now false) but the mechanism the recon isolated: LCSC/JLCPCB passive-format
# value-shapes (e.g. `0402B104K160CT`) are NOT recognized as passives/non-IC by
# this file's classifiers, so a board whose blocking ratio depends on them stays
# ineligible even after every other axis (provenance pseudo-parts, real datasheet
# resolution) is accounted for. This is rewritten fully synthetically — no read of
# `netlist_corpus/` or any live cache — so it is permanently immune to future
# corpus/datasheet-cache growth, and it is deliberately a TRIPWIRE for
# TODO-113 — live open Notion card (P2, Not started): LCSC/JLCPCB
# passive-format recognition. These assertions are DESIGNED to fail when 113
# lands; update them deliberately in that cycle, not as a regression to chase.
_LCSC_PASSIVE_VALUES = [
    "0402B104K160CT", "0402X105K160CT", "0603X106M6R3CT",
    "0402N471J500LT", "0402N270J500LT",
]


def _write_synthetic_ic_board(tmp_path: Path, values: list[str]) -> Path:
    """Minimal KiCad-.net board, one component per value, all on a shared signal
    net + GND (same minimal shape as test_resolve_memo.py's `_write_board`).
    Every refdes is `U<n>` so `pseudo_part_provenance` never fires (its prefixes
    are TP/JP/SJ/J/P/CON/CAN/BH/Y/X/SW/BZ, none of which is `U`) — every value is
    a plain, unclassified IC candidate, exactly matching the recon's live finding
    that these LCSC-format values sail through both the passive and non-IC
    filters untouched."""
    comps = [(f"U{i + 1}", v) for i, v in enumerate(values)]
    comp_blocks = "\n".join(
        f'    (comp (ref "{ref}") (value "{part}")\n'
        f'      (pins (pin (num "1") (name "IO1")) (pin (num "2") (name "GND"))))'
        for ref, part in comps
    )
    sig_nodes = " ".join(f'(node (ref "{ref}") (pin "1"))' for ref, _ in comps)
    gnd_nodes = " ".join(f'(node (ref "{ref}") (pin "2"))' for ref, _ in comps)
    text = (
        f'(export (version "E")\n'
        f'  (design (source "synthetic_mitayi_shaped.net"))\n'
        f'  (components\n{comp_blocks}\n  )\n'
        f'  (nets\n'
        f'    (net (code "1") (name "SIG1") {sig_nodes})\n'
        f'    (net (code "2") (name "GND") {gnd_nodes})\n'
        f'  )\n)\n'
    )
    path = tmp_path / "synthetic_mitayi_shaped.net"
    path.write_text(text)
    return path


def test_mitayi_stays_blocked_second_axis(tmp_path):
    """Synthetic, hermetic replacement (TODO-309) — see the module-level comment
    block directly above for the full history. No assertion here reads
    `netlist_corpus/` or any live cache."""
    # (a) CLASSIFIER TRIPWIRE — TODO-113. These 5 real LCSC/JLCPCB passive-format
    # values (drawn verbatim from the recon's live Mitayi-Pico-D1 breakdown) must
    # NOT be recognized as passive by is_passive_value. If this fails, TODO-113
    # has landed — come update this test deliberately, in the same cycle.
    for pn in _LCSC_PASSIVE_VALUES:
        assert rct.is_passive_value(pn) is False, (
            f"{pn}: is_passive_value now recognizes this LCSC/JLCPCB "
            "passive-format value — TODO-113 appears to have landed; update "
            "this tripwire deliberately rather than silencing it."
        )

    # (b) ELIGIBILITY GATE — a fully synthetic board whose blockers include those
    # 5 LCSC shapes plus 7 more opaque never-resolving IC-shaped values (12
    # blocking total), against 2 synthetically-resolved ICs (matching PDFs
    # written into a tmp datasheets dir) — 14 countable, ratio 12/14 ≈ 0.857,
    # comfortably above the 0.80 gate (not knife-edged at the boundary, unlike
    # the real board's now-exact 0.800).
    extra_blocking = [f"FAKEIC{i:03d}QFN" for i in range(7)]
    resolved_values = ["FAKEIC900QFN", "FAKEIC901QFN"]
    blocking_values = _LCSC_PASSIVE_VALUES + extra_blocking

    datasheets_dir = tmp_path / "datasheets"
    datasheets_dir.mkdir()
    for v in resolved_values:
        (datasheets_dir / f"{v}.pdf").write_bytes(b"%PDF-1.4 fake")

    board = _write_synthetic_ic_board(tmp_path, blocking_values + resolved_values)
    elig, resolved, blocking, _ = rct.is_eligible(board, datasheets_dir)

    total = len(resolved) + len(blocking)
    ratio = len(blocking) / total
    assert len(resolved) == 2, resolved
    assert len(blocking) == 12, blocking
    assert ratio == pytest.approx(0.857, abs=0.01), f"got {ratio:.3f}"
    assert elig is False


# ── Test 5: a 44-set already-eligible board stays eligible (gate-location) ─────
def test_already_eligible_board_with_pseudo_parts_unchanged():
    """An already-eligible board that carries pseudo-parts stays eligible; the
    classifier only ever REMOVES things from the blocking numerator, so an
    already-eligible board can never regress to ineligible. (Findings-byte-identity
    is proven by the gate-location property — no checker reads footprint/
    libsource_part — and empirically by the precision gate.)"""
    # jetson som_io carries TestPoints/connectors and is eligible.
    board = CORPUS / "kicad_official/demos/demos/jetson-agx-thor-baseboard/som_io.net"
    if not board.exists():
        pytest.skip("som_io.net not present")
    elig, resolved, blocking, _ = rct.is_eligible(board, DATASHEETS)
    assert elig is True
    assert len(resolved) >= 1


# ── Bonus: ComponentIR carries provenance after parse (additive enrichment) ───
@pytest.mark.skipif(not FLIGHT_COMPUTER_BOARD.exists(), reason="corpus board fixture not on disk")
def test_parser_captures_provenance_fields():
    ir = parse(str(FLIGHT_COMPUTER_BOARD))
    tp = next((c for c in ir.components if c.refdes == "TP4"), None)
    assert tp is not None
    assert tp.libsource_part == "TestPoint"
    assert tp.footprint.startswith("TestPoint:")

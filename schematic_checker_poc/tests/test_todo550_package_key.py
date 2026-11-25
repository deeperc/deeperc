"""TODO-550 D2/D3 + Q1: steps/package_key.py (package vocabulary, footprint decode, pick)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import provenance  # noqa: E402
from steps import package_key as pk  # noqa: E402

# (footprint, netlist pin count) -> (family, n)
FAMILY_CASES = [
    ("SOIC-8", 8, "SOIC", 8), ("SOP-8", 8, "SOIC", 8), ("SO-8", 8, "SOIC", 8),
    ("MSOP-8", 8, "MSOP", 8), ("VSSOP-8", 8, "MSOP", 8),
    ("TSSOP-14", 14, "TSSOP", 14), ("HTSSOP-20", 21, "TSSOP", 20), ("TVSOP-48", 48, "TVSOP", 48),
    ("SSOP-20", 20, "SSOP", 20),
    ("SOT-23-5", 5, "SOT-23", 5), ("SOT23-5", 5, "SOT-23", 5), ("TSOT-23-6", 6, "SOT-23", 6),
    ("SOT95P240X110-3N", 3, "SOT-23", 3),
    ("SC-70-5", 5, "SC-70", 5), ("SOT-323", 3, "SC-70", 3), ("SOT-353", 9, "SC-70", 5),
    ("SOT-363", 9, "SC-70", 6), ("SOT65P210X110-6N", 6, "SC-70", 6),
    ("SOT-223-3", 4, "SOT-223", 3), ("SOT-89-3", 3, "SOT-89", 3),
    ("QFN-32", 32, "QFN", 32), ("VQFN-20", 20, "QFN", 20), ("WQFN-16", 16, "QFN", 16),
    ("UQFN-16", 16, "QFN", 16), ("UFQFPN-48", 48, "QFN", 48), ("DHVQFN-14", 14, "QFN", 14),
    ("MLF-32", 32, "QFN", 32), ("VQFN-MLF-20", 20, "QFN", 20),
    ("DFN-8", 8, "DFN", 8), ("SON-8", 8, "DFN", 8), ("USON-8", 8, "DFN", 8), ("WSON-8", 8, "DFN", 8),
    ("VSON-10", 10, "DFN", 10), ("TDSON-8", 8, "DFN", 8),
    ("LQFP-48", 48, "LQFP", 48), ("TQFP-32", 32, "LQFP", 32),
    ("DIP-28_W7.62mm", 28, "DIP", 28), ("PDIP-8", 8, "DIP", 8),
    ("Package_TO_SOT_THT:TO-92_Inline", 3, "TO-92", 3), ("TO-220-3", 3, "TO-220", 3),
    ("BGA-100", 100, "BGA", 100), ("UFBGA-176", 176, "BGA", 176), ("LGA-16", 16, "LGA", 16),
]


@pytest.mark.parametrize("fp,npins,fam,n", FAMILY_CASES)
def test_decode_each_family_and_alias(fp, npins, fam, n):
    d = pk.decode_footprint(fp, npins)
    assert (d.family, d.n) == (fam, n)


CORPUS = [
    ("Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", 8, ("SOIC", 8, False, False)),
    ("Package_DFN_QFN:VQFN-32-1EP_5x5mm_P0.5mm_EP3.1x3.1mm", 33, ("QFN", 32, True, False)),
    ("ICs:SOIC127P600X175-8N", 8, ("SOIC", 8, False, True)),
    ("ICs:SOT95P240X110-3N", 3, ("SOT-23", 3, False, True)),
    ("ICs And Semiconductors SMD:QFN50P700X700X90-45N-S530", 22, ("QFN", 45, False, True)),
    ("Package_QFP:LQFP64_Pitch-0.5mm_10x10mm", 64, ("LQFP", 64, False, False)),
    ("RP2040-QFN-56", 57, ("QFN", 56, False, False)),
    ("INFINEON_PG-VQFN-56-900", 56, ("QFN", 56, False, False)),
    ("easyeda2kicad:LGA-14_L3.0-W2.5-P0.50-TL", 14, ("LGA", 14, False, False)),
]


@pytest.mark.parametrize("fp,npins,want", CORPUS)
def test_decode_corpus_footprints(fp, npins, want):
    d = pk.decode_footprint(fp, npins)
    assert (d.family, d.n, d.exposed_pad, d.ipc) == want


def test_decode_undecodable_and_countless():
    assert pk.decode_footprint("X:SOT23EBC", 3) is None          # no family word (b2.py behaviour)
    assert pk.decode_footprint("Resistor_SMD:R_0603", 2) is None
    assert pk.decode_footprint("", 8) is None
    d = pk.decode_footprint("Package_TO_SOT_SMD:SOT-23", 3)
    assert (d.family, d.n, d.count_from_string) == ("SOT-23", 3, False)  # count-less: netlist count
    d = pk.decode_footprint("Package_TO_SOT_THT:TO-92_Inline", 3)
    assert (d.family, d.n, d.count_from_string) == ("TO-92", 3, False)


def test_ti_designators_only_from_headers():
    assert pk.decode_header("DGK") == ["MSOP-?"]
    assert pk.decode_header("D") == ["SOIC-?"]
    assert pk.decode_header("PW") == ["TSSOP-?"]
    assert pk.decode_header("DBV") == ["SOT-23-?"]
    assert pk.decode_header("DGG") == ["TSSOP-?"]
    assert pk.decode_header("DGV") == ["TVSOP-?"]
    for fp in ("TI:DGK0008A", "Package:DBV", "PW", "D"):
        assert pk.decode_footprint(fp, 8) is None


def test_unknown_designators_and_header_tokens():
    assert pk.decode_header("DDU") == pk.UNKNOWN
    assert pk.decode_header("RGT") == pk.UNKNOWN
    assert pk.decode_header("XYZ") == []
    assert pk.decode_header("SO") == []
    assert pk.decode_header("SOT-23") == ["SOT-23-?"]
    assert pk.decode_header("UFQFPN48") == ["QFN-48"]
    assert pk.decode_header("UFBGA176") == ["BGA-176"]
    assert pk.decode_header("WLCSP49") == []


def test_stm32f411_qfn48_footprint_and_cubemx_ufqfpn48_same_key():
    d = pk.decode_footprint("Package_DFN_QFN:QFN-48-1EP_7x7mm_P0.5mm", 49)
    assert pk.key(d.family, d.n) == pk.decode_header("UFQFPN48")[0] == "QFN-48"


# ------------------------------------------------------------------ pick
def test_pick_family_single_package():
    assert pk.pick_reason("Package_SO:MSOP-8_3x3mm_P0.65mm", 8, {"MSOP-8", "SOIC-8"}) == ("MSOP-8", "family-match")


def test_pick_family_several_packages_by_count():
    assert pk.pick("Package_SO:TSSOP-14", 14, {"TSSOP-14", "TSSOP-16", "SOIC-14"}) == "TSSOP-14"
    assert pk.pick_reason("Package_SO:TSSOP-20", 20, {"TSSOP-14", "TSSOP-16"}) == (None, "ambiguous")


def test_pick_family_not_in_document():
    assert pk.pick_reason("Package_SO:SOIC-8", 8, {"MSOP-8"}) == (None, "family-not-in-document")


def test_pick_no_document_packages():
    assert pk.pick_reason("Package_SO:SOIC-8", 8, set()) == (None, "no-document-packages")
    assert pk.pick("Package_SO:SOIC-8", 8, None) is None


def test_pick_no_family_word_never_picks_even_single_package():
    assert pk.pick_reason("Package_TO_SOT_SMD:SC-59", 3, {"SOT-23-3"}) == (None, "q1-single-package-fallback")
    assert pk.pick_reason("easyeda2kicad:WIRELM-SMD_ESP32-C6-WROOM-1", 37, {"QFN-40"})[0] is None
    assert pk.pick_reason("X:Y", 3, {"SOT-23-3", "SOIC-8"}) == (None, "undecoded-multi")


def test_q1_count_mismatch_gives_none():
    assert pk.pick_reason("Package_SO:SOIC-8_5.3x5.3mm_P1.27mm", 8, {"SOIC-1"}) == (None, "q1-count-mismatch")
    assert pk.pick("antmicro-footprints:QFN-16-1EP_4x4mm_P0.65mm", 17, {"QFN-17"}) is None


@pytest.mark.parametrize("garbage,fps", [
    ("TO-92-50", ["Package_TO_SOT_THT:TO-92_Inline", "TO-92-3"]),
    ("SOT-23-600", ["Package_TO_SOT_SMD:SOT-23-5", "Package_TO_SOT_SMD:SOT-23", "SOT-23-6", "SOT95P240X110-3N"]),
    ("SOIC-1", ["Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "SOIC-14", "SOIC127P600X175-8N"]),
    ("SOT-223-44", ["Package_TO_SOT_SMD:SOT-223-3_TabPin2", "SOT-223"]),
])
def test_projection_v2_garbage_keys_unpickable(garbage, fps):
    for fp in fps:
        for npins in range(1, 9):
            assert pk.pick(fp, npins, {garbage}) is None


def test_exposed_pad_n_plus_one():
    # count-less EP family taking a netlist count that includes the pad
    assert pk.pick("Package_DFN_QFN:QFN", 33, {"QFN-32"}) == "QFN-32"
    # not for a non-EP family
    assert pk.pick("Package_QFP:LQFP", 65, {"LQFP-64"}) is None


def test_ipc_n_plus_one_only_when_unique():
    assert pk.pick("X:QFN50P400X400X90-17N", 17, {"QFN-16"}) == "QFN-16"
    assert pk.pick("X:QFN50P400X400X90-17N", 17, {"QFN-16", "QFN-17"}) is None


def test_dip28_board_on_tqfp_mlf_only_document():
    assert pk.pick("Package_DIP:DIP-28_W7.62mm", 28, {"LQFP-32", "QFN-32"}) is None


def test_component_package_and_cache_packages():
    class C:
        footprint = "easyeda2kicad:LGA-14_L3.0-W2.5-P0.50-TL"
        pins = [object()] * 14
    cache = {"extraction_meta": {"pin_numbers_checked": {"packages": ["LGA-14"]}}}
    assert pk.cache_packages(cache) == ["LGA-14"]
    assert pk.cache_packages({}) == [] and pk.cache_packages(None) == []
    assert pk.component_package(C(), pk.cache_packages(cache)) == "LGA-14"
    assert pk.component_package(C(), []) is None
    assert pk.component_package(None, ["LGA-14"]) is None


def test_package_key_is_a_core_pipeline_file():
    assert "steps/package_key.py" in provenance.CORE_PIPELINE_FILES
    assert "steps/package_key.py" in provenance.CHECKER_SOURCE_FILES
    assert "steps/pin_number_check.py" not in provenance.CHECKER_SOURCE_FILES

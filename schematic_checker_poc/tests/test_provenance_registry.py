"""Enforcement: provenance.CHECKER_SOURCE_FILES is derived from the checker registry
(Phase 2, spec §3.3). These assertions make the step_08e-omitted-from-provenance bug
class (TODO-168) structurally impossible: any new steps/step_08*.py that is not wired
into the registry fails the suite.
"""
import sys
from pathlib import Path

import provenance
from steps import checker_registry as reg

POC = Path(__file__).resolve().parent.parent


def test_checker_source_files_is_core_plus_registry():
    assert provenance.CHECKER_SOURCE_FILES == provenance.CORE_PIPELINE_FILES + reg.SOURCE_FILES


def test_every_step_08_file_on_disk_is_in_the_derived_tuple():
    on_disk = {f"steps/{p.name}" for p in (POC / "steps").glob("step_08*.py")}
    assert on_disk, "expected step_08*.py checkers on disk"
    missing = on_disk - set(provenance.CHECKER_SOURCE_FILES)
    assert not missing, f"step_08* checker(s) missing from CHECKER_SOURCE_FILES: {missing}"


def test_step_08e_is_now_present():
    # The folded TODO-168 fix: step_08e enters the code axis here.
    assert "steps/step_08e_pullup_value_checker.py" in provenance.CHECKER_SOURCE_FILES


def test_every_registry_source_file_is_in_the_tuple_and_exists():
    for s in reg.REGISTRY:
        assert s.source_file in provenance.CHECKER_SOURCE_FILES
        assert (POC / s.source_file).is_file()


def test_no_duplicate_source_files():
    files = provenance.CHECKER_SOURCE_FILES
    assert len(files) == len(set(files))


def test_core_and_registry_are_disjoint():
    # A checker file must live in the registry, never in CORE_PIPELINE_FILES.
    assert not (set(provenance.CORE_PIPELINE_FILES) & set(reg.SOURCE_FILES))


def test_checker_code_hash_is_stable_and_nonnull():
    # The code axis must still compute (all 15 files readable).
    h = provenance.checker_code_hash()
    assert h and len(h) == 64

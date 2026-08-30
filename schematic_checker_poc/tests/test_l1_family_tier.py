"""L1 matcher family-prefix fallback tier (R2), per design pass
investigation/experiments/l1_family_tier/REPORT.md (Step 6).

Tests pipeline._find_pdf_recursive directly against a tmp-dir store fixture
mirroring real filenames — never the live corpus store. Covers: the 3
must-fire payoff cases, permanent refuse-on-ambiguity behavior, and the
R4-rejection collision guard (W25Q32JV/JW) as a locked regression.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pipeline  # noqa: E402
from steps import step_03_resolver as r3  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(r3, "DATASHEETS_DIR", str(tmp_path))

    def make(*names):
        for name in names:
            (tmp_path / name).write_bytes(b"%PDF-1.4 fake")

    return make


def test_must_fire_stm32f405(store):
    store("STM32F405 STM32F407.pdf")
    result = pipeline._find_pdf_recursive("STM32F405RGTx")
    assert result == os.path.join(r3.DATASHEETS_DIR, "STM32F405 STM32F407.pdf")


def test_must_fire_stm32f411(store):
    store("STM32F411.pdf")
    result = pipeline._find_pdf_recursive("STM32F411CEUx")
    assert result == os.path.join(r3.DATASHEETS_DIR, "STM32F411.pdf")


def test_must_fire_stm32f103c8(store):
    store("stm32f103c8.pdf")
    result = pipeline._find_pdf_recursive("STM32F103C8Tx")
    assert result == os.path.join(r3.DATASHEETS_DIR, "stm32f103c8.pdf")


def test_must_refuse_ambiguous_family_stem(store):
    """Two distinct files whose tokens both satisfy the vendor regex and both
    prefix the same query MPN — modeled on the real rp2040/rp2040_stamp store
    collision (design pass Step 3), transposed into STM32 namespace. Silently
    picking one file would be wrong for whichever half of the time the store
    happens to iterate the other one first — refuse instead of guess."""
    store("STM32F999.pdf", "STM32F999 STM32F998.pdf")
    result = pipeline._find_pdf_recursive("STM32F999ABCx")
    assert result is None


def test_w25q32_jv_jw_collision_guard(store):
    """R4 (iterated suffix-strip) was rejected in the design pass precisely
    because it collapses Winbond's 3.3V (JV) and 1.8V (JW) flash lines onto
    one file — the same voltage-class distinction behind the artix-dc-scm
    overvoltage defect. R2 is vendor-scoped to STM32 and must never bridge
    this pair; locked here as a permanent regression guard."""
    store("W25Q32JWSSIQ.pdf")
    result = pipeline._find_pdf_recursive("W25Q32JVSSIQ")
    assert result is None

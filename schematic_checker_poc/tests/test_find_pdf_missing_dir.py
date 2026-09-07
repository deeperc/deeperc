"""_find_pdf must tolerate a missing DATASHEETS_DIR.

DATASHEETS_DIR is a directory the user populates themselves (README's own
install instructions have them `mkdir -p` it) -- a fresh clone, or any
--board run against a board with no local datasheets, never creates it.
A missing directory means the same thing as an empty one: no local PDF
found. It must not raise.

Also covers resolve_and_parse's directory-creation timing (part of the
same fix): a short/generic value must exit via the MIN_MPN_LENGTH raise
before ever touching the filesystem, so it works even when the parent of
DATASHEETS_DIR doesn't exist and isn't writable-into.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from steps import step_03_resolver


def test_find_pdf_returns_none_on_missing_directory(tmp_path, monkeypatch):
    missing = tmp_path / "does_not_exist"
    assert not missing.exists()
    monkeypatch.setattr(step_03_resolver, "DATASHEETS_DIR", str(missing))

    assert step_03_resolver._find_pdf("SOME_PART") is None


def test_find_pdf_still_finds_a_real_match(tmp_path, monkeypatch):
    ds_dir = tmp_path / "datasheets"
    ds_dir.mkdir()
    (ds_dir / "STM32F103C8T6.pdf").write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(step_03_resolver, "DATASHEETS_DIR", str(ds_dir))

    found = step_03_resolver._find_pdf("STM32F103C8T6")
    assert found is not None
    assert found.endswith("STM32F103C8T6.pdf")


def test_resolve_and_parse_short_value_never_touches_filesystem(tmp_path, monkeypatch):
    """A generic passive value ('10k') must raise before any os.makedirs --
    even when DATASHEETS_DIR's parent doesn't exist and can't be created."""
    unwritable_parent = tmp_path / "no_such_parent"
    fake_datasheets_dir = str(unwritable_parent / "datasheets")
    monkeypatch.setattr(step_03_resolver, "DATASHEETS_DIR", fake_datasheets_dir)
    monkeypatch.setattr(step_03_resolver, "PARSED_DIR", str(tmp_path / "parsed"))
    monkeypatch.setattr(step_03_resolver, "_l2_resolve_ok", lambda: False)

    with pytest.raises(FileNotFoundError, match="too short to be a real MPN"):
        step_03_resolver.resolve_and_parse("10k")

    # The short-value path must not have created anything.
    assert not unwritable_parent.exists()

"""resolve_and_parse's too-short-MPN message.

The length compared against MIN_MPN_LENGTH is base_pn's -- part_number
AFTER BASE_SUFFIX_RE strips a trailing grade/package suffix -- not
part_number's own length. The message must name both, since quoting only
the untouched part_number next to the stripped length reads as
self-contradictory ('flsh1' ... length 2 < 4).

pipeline.py substring-matches "is too short to be a real MPN" to bucket
this error into the aggregated short-MPN warning (LT-21) -- that phrase
must survive verbatim.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from steps import step_03_resolver


@pytest.fixture(autouse=True)
def _isolated_resolver_dirs(tmp_path, monkeypatch):
    """resolve_and_parse creates DATASHEETS_DIR/PARSED_DIR unconditionally
    before the length check -- point both at tmp_path so this test touches
    nothing in the repo tree."""
    monkeypatch.setattr(step_03_resolver, "DATASHEETS_DIR", str(tmp_path / "datasheets"))
    monkeypatch.setattr(step_03_resolver, "PARSED_DIR", str(tmp_path / "parsed"))
    monkeypatch.setattr(step_03_resolver, "_l2_resolve_ok", lambda: False)


def test_message_names_both_original_and_stripped_value():
    with pytest.raises(FileNotFoundError) as exc_info:
        step_03_resolver.resolve_and_parse("flsh1")

    msg = str(exc_info.value)
    # The exact phrase pipeline.py's aggregation logic matches on.
    assert "is too short to be a real MPN" in msg
    # Both the untouched value and the value actually measured must appear,
    # so the message doesn't look self-contradictory.
    assert "'flsh1'" in msg
    assert "'fl'" in msg
    assert "length 2 < 4" in msg


def test_short_generic_value_with_no_suffix_still_raises():
    with pytest.raises(FileNotFoundError) as exc_info:
        step_03_resolver.resolve_and_parse("10k")

    msg = str(exc_info.value)
    assert "is too short to be a real MPN" in msg
    assert "'10k'" in msg

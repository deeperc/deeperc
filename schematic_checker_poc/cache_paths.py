"""Single source of truth for the pin_groups cache-path formula (TODO-343).

CHEAP-IMPORT CONTRACT: this module may import ONLY pathlib (or other stdlib).
It must NEVER import pdfplumber, llm.ollama_client, anthropic, or anything that
transitively pulls those in — provenance.py and the recall harness depend on
this module staying cheap to import (the same contract provenance.py's own
`_cache_path_for_pdf` docstring already documents for its own local
canonical_pdf_stem import).

Takes an ALREADY-COMPUTED stem rather than a pdf_path or canonical_pdf_stem
call: `steps.step_03_resolver.canonical_pdf_stem` lives inside the heavy
step_03_resolver module (pdfplumber + llm.ollama_client at module level) —
importing it here would defeat the contract above. Each of the three call
sites already computes its own stem (canonical_pdf_stem, or the equivalent
`.stem.lower()`) before reaching this formula; that computation stays at the
call site, unconsolidated (TODO-342's site map, out of scope here).
"""
from __future__ import annotations

from pathlib import Path


def pin_groups_cache_path(parsed_dir, stem: str) -> Path:
    """The canonical <parsed_dir>/<stem>/auto/<stem>_pin_groups.json layout.
    `parsed_dir` may be a str or Path; `stem` must already be lower-cased
    (the canonical_pdf_stem / .stem.lower() contract) — this function does not
    itself normalize case."""
    return Path(parsed_dir) / stem / "auto" / f"{stem}_pin_groups.json"

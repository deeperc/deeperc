# Limitations

- Coverage is bounded by what's in `kb/` plus your cached extractions. Anything
  outside both is reported `UNRESOLVABLE`, by design — absence of evidence is
  never converted into a verdict.
- This tool complements KiCad's ERC; it does not replace it. It targets defect
  classes ERC cannot express (role/net-name coherence, per-part voltage limits,
  pull-up presence) and assumes you still run ERC for structural rule checking.
- MCU-side SPI role checks (MOSI/MISO/SCK on generic pin names such
  as PA6/PA7) come from the peripheral knowledge base, which currently
  carries SPI roles for STM32 F1, F3 and F4 only. RP2040 has the same
  fixed pin-function shape and can be added the same way; it is not
  populated yet. On matrix-routed MCUs (ESP32) an MCU-side SPI swap
  is not detected unless the peripheral IC's pin names carry the
  role. Chip-select (NSS/CS) is not checked.
- Report explanation text is LLM-generated where a local model is available;
  when it isn't, findings carry a one-line note instead. Verdicts are never
  affected either way.
- Sheet-local (`/`-prefixed) POWER labels are not classified as rails by the
  deterministic tier; affected supply checks report `UNRESOLVABLE`, never
  `FAIL`. Ground labels are handled. (TODO-409 power-side residual.)

## Extending coverage

A finding tagged `"evidence_tier": "not_cache_derived"` whose text ends
`"(no part data in the cache — see LIMITATIONS.md to add parts)"` (or the
structural/supply variant, `"(netlist evidence only — no part data in the
cache)"`) means exactly one thing: the part it cites never produced a cached
datasheet extraction this run — either no extraction was ever attempted for
it, or it was attempted and found nothing to serve. Nothing was hidden or
guessed; the check that would need that part's spec ran honestly with the
evidence it actually had (netlist topology only, or KB alt-function data).

**What "the cache" is.** A shipped, manifest-verified library of
datasheet-derived part data — a set of `pin_groups` extractions, one per
covered part, produced once by reading a vendor datasheet PDF and never
regenerated from anything else at check time. Datasheet-backed checks
(supply-voltage limits, structural pin-kind cross-checks, driver/receiver
voltage compatibility) consume this cache and only this cache; they never
touch KiCad's own schematic pin definitions or infer a value from context.

**Cache remedy — add the part's datasheet.** Two things close the gap for a
specific part:

1. Place the part's datasheet PDF under `netlist_corpus/datasheets/` (any
   subdirectory).
2. Run the checker once against a board that references the part, with an
   Anthropic API key configured (`.env`'s `ANTHROPIC_API_KEY`, or the
   environment variable directly):

   ```bash
   cd schematic_checker_poc
   python3 main.py \
       --netlist ../netlist_corpus/your_board.net \
       --datasheets-dir ../netlist_corpus/datasheets \
       --skip-confirm
   ```

   There is no separate "extract this one part" command — pointing
   `--netlist` at a board that cites the new part is how it gets extracted.
   The extraction is cached against the source PDF's content hash
   (`source_hash`); every subsequent run against that same PDF reads the
   cache and never calls the API again — key-free thereafter. If a vendor
   revises the datasheet later, `--refresh <pdf-filename-stem>` forces a
   fresh extraction without deleting the prior cache (it's kept as a
   `.pre_reextract` sidecar).

   See the main [README](README.md#extracting-your-own-parts) for the full
   procedure, including the no-key degradation behavior and the experimental
   local-only backend.

**KB remedy — author a knowledge-base entry.** The `results` and
`peripheral_integrity_results` findings whose evidence names a KB gap (e.g.
"MPN(s) not in KB") aren't fixed by an extraction at all — they need a
structured peripheral role/topology entry in `kb/`, a different axis from
the datasheet-extraction cache above (pin-group values vs. alt-function/role
data). The tooling used to build and extend the KB is not part of this
release; a documented authoring path is planned (see the main
[README](README.md#kb-authoring)) — until it lands, a KB gap for a part is
an honest, permanent `UNRESOLVABLE` for that specific check, not something
a local extraction run can close.

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
- Pull-up presence is decided by a walk that only traverses two-pin passives.
  Resistor networks and arrays (multi-pin `RN*`/`RP*` parts) are skipped
  entirely, because the netlist does not encode which internal element pairs
  with which package pin — so a real pull-up routed through a network reads as
  absent. Today the one known false case (an I²C bus pulled up through a
  network) is suppressed as a side effect of the name-path corroboration gate,
  not by understanding the network; the open-drain pin-type path has no such
  gate. An open-drain net whose only pull-up runs through a resistor network
  can therefore still produce a false "pull-up missing" finding. If your board
  uses networks for bus pull-ups, treat that finding as a prompt to look, not a
  verdict.
- KiCad names a no-connect pin's net `unconnected-(REFDES-PINNAME-PadN)`, which
  embeds the pin's role text. The net-name classifier reads those names
  truthfully on purpose; each peripheral checker then excludes them with its
  own containment guard (three copies, pinned to one pattern by a test). No
  shipped check is affected. The limitation is structural: a new consumer of
  net-name roles that does not add the guard will treat no-connect pins as bus
  members.
- Extracted datasheet values pass a small deterministic validation twice. At
  use time the check is visible: a supply range that fails it (for example a
  minimum equal to its maximum) makes every check on that pin report
  `UNRESOLVABLE`, with the reason named in the evidence. At extraction time
  the same check runs in advisory mode only: it emits an info-level log line,
  and the cache file is written as extracted. A cache entry therefore carries
  no mark that it was flagged, and a report built from a clean cache cannot
  show that a value was ever questioned. If you need that audit trail, run
  the extraction with info-level logging enabled (`main.py` does this by
  default; `run_checks.py` does not and has no flag to raise it).
- UART is checked as device-relative capability, not as pin-role coherence: a
  TX or RX signal landing on a KB-known pin with no UART capability is a FAIL,
  and nothing else is evaluated. A TX↔RX swap between two UART-capable
  devices produces no finding — not even `UNRESOLVABLE` — because each side is
  a plausible UART endpoint on its own and the check never compares the two.
  The generic output-conflict check can catch a TX-to-TX net only when KiCad
  types both pins strictly as outputs, and then reports it as an output
  conflict, not a UART issue.

## When the shipped cache refuses to serve a part

The cached extractions that ship in `schematic_checker_poc/datasheets_parsed/`
are listed, with a SHA-256 each, in `CACHE_MANIFEST.json` in that directory.
Every time a shipped cache file is about to be served, its on-disk hash is
checked against the manifest. Three outcomes:

1. **Match** — served as shipped cache. This is every run on a clean clone.
2. **Mismatch, but the part's datasheet PDF is on disk and its content hash
   matches the cache's recorded source** — served, with a warning, as a
   legitimate local re-extraction rather than as shipped cache. This is what
   you get after running `--refresh` on a shipped part.
3. **Mismatch and no PDF verifies it** — refused. The run log carries:

```
[STEP 03] REFUSING cache <path> — shipped-cache manifest integrity check
FAILED (expected sha256 <a>, found <b>) and no local PDF verifies it. This
part will be reported UNRESOLVABLE; supply the datasheet PDF or restore the
published cache file.
```

   and every datasheet-backed check on that part reports `UNRESOLVABLE` with
   the same " (no part data in the cache — see LIMITATIONS.md to add parts)"
   text as a part that was never cached. Nothing is served from a file that
   cannot be traced to either the published manifest or a datasheet you hold.

**What it means.** A shipped cache file was modified — edited by hand, partially
written, or replaced — and there is no local datasheet to vouch for the new
contents. It is not a tool fault and not a bad part; it is the check that keeps
"Cache-sourced" from meaning "whatever is on disk."

**How to recover.** Either restores the verdicts:

- Put the published file back:
  `git checkout -- schematic_checker_poc/datasheets_parsed/<stem>/<stem>_pin_groups.json`
- Or place the part's datasheet PDF under `netlist_corpus/datasheets/` and
  re-run; if the cache was produced from that PDF, outcome 2 applies. If it
  was not, run with `--refresh <stem>` to re-extract from it (needs an
  extraction backend; see the README).

**What it never touches.** Only files listed in the manifest are governed.
Caches you extract for your own parts are unlisted and are served under the
normal provenance rules, never refused. A tree with no manifest at all (a
development checkout) runs with no governance and reports `cache_version` as
null in the report header.

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

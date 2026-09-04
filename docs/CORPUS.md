# The verification corpus

DeepERC is verified two ways: against real open-hardware designs, where it has
to find defects that maintainers confirm, and against a synthetic known-bad
corpus, where every defect is planted and labeled so precision and recall can
be measured per defect class. This document describes both. Neither corpus is
redistributed with the repository (see the last section for why).

## Real-world findings

The strongest evidence that a checker works is a bug a maintainer did not know
about. DeepERC has three filed upstream findings across two organizations and
three repositories. Each was detected automatically from the board's exported
netlist, verified by hand in KiCad against the upstream HEAD, and reported with
full evidence. Every one is invisible to KiCad's ERC: the connections are
electrically legal, and the defect only exists relative to what the datasheet
says the pin or part requires.

| Board | Defect class | Upstream |
|---|---|---|
| Antmicro Jetson AGX Thor baseboard | I²C SDA/SCL swapped on the ID EEPROM | [jetson-agx-thor-baseboard #1](https://github.com/antmicro/jetson-agx-thor-baseboard/issues/1) |
| Antmicro Artix DC-SCM | 1.8 V flash ×4 on the 3.3 V rail | [artix-dc-scm #6](https://github.com/antmicro/artix-dc-scm/issues/6) |
| Tronex TRNXSDR carrier | I²C bus crossed at the root sheet | [TRNXSDR-carrier #1](https://github.com/acruxcz/TRNXSDR-carrier/issues/1) |

Full write-ups, including why each one survived an ERC-cleanup commit, are in
[FINDINGS.md](../FINDINGS.md). The third finding is worth a note here: it fired
with an empty knowledge base for every part on the bus. The verdict came from
pin-function evidence in the netlist plus the vendor datasheet alone.

## What the corpus is

The precision gate runs on **126 real boards**. They are exported KiCad
netlists from open-hardware projects, drawn from a few ecosystems:

- vendor-published open hardware (SoM carriers, FPGA and SDR boards, baseboards)
- community KiCad ports and derivatives of MCU development boards, mostly
  STM32- and ESP32-family
- small peripheral and breakout boards that exercise a single bus or supply
  topology

Boards are named in this repository only where the project is verifiably
open-licensed and already public in FINDINGS.md or a filed issue. Everything
else is described at the ecosystem level.

The 126 is a deliberate scope, not a sample of everything we have parsed. Every
board in the gate has been run repeatedly across checker eras, so a new FAIL on
one of them is a regression to explain, not a discovery to celebrate. The gate
exists to hold precision, and the credibility anchor for recall is the table
above — real bugs, not volume.

## Known-bad methodology

Real designs are mostly correct, which makes them poor at measuring recall. For
that, DeepERC maintains a synthetic known-bad corpus built by mutating
known-good boards:

- **Mutation operators.** Each operator plants exactly one defect class — for
  example a supply overvoltage (a part whose rated maximum is below the rail
  it is placed on), an I²C SDA/SCL swap, or two push-pull outputs tied
  together — and produces a mutant netlist from a known-good parent.
- **Ground-truth sidecars.** Every mutant ships with a JSON sidecar naming the
  planted defect: the net, the pins, the operator, and the verdict the checker
  is expected to produce. Nothing about the expected outcome lives in the
  netlist itself.
- **Per-class measurement.** Precision and recall are computed per operator
  against the sidecars, so a checker that catches supply faults but misses bus
  swaps is scored as exactly that, not averaged into a single number.
- **Frozen corpus + determinism gate.** The mutant set is frozen (seeded
  generation, manifest-pinned) so measurements are comparable across checker
  versions, and the pipeline is required to produce byte-identical reports on
  repeated runs of the same input before any measurement counts.

This corpus is not representative of every real-world design and is not meant
to be. Its absence of a topology never disqualifies a check; when the corpus
lacks the structure needed to exercise a valid check, a fixture is constructed
for it. The methodology here is also the seed for a future public benchmark
path; that work is tracked separately and not expanded on in this document.

## What the numbers mean

A DeepERC run classifies each check as **PASS**, **WARN**, **FAIL**, or
**UNRESOLVABLE**, and the numbers in this project are always reported against
that vocabulary:

- **FAIL** means the checker holds sourced evidence — a datasheet limit, a
  pin-function table, a §-citation — that the design violates. It is the only
  verdict the precision gate is built around.
- **WARN** is a plausible problem the checker cannot elevate to FAIL with the
  evidence it has.
- **UNRESOLVABLE** is a first-class outcome, not a failure of the tool and not
  a gap we hide. It means the checker could not obtain the evidence a verdict
  would need — no datasheet, no knowledge-base entry, a pin the entry does not
  cover — and says so rather than guessing.
- **Evidence tiers.** Every non-UNRESOLVABLE finding carries how its evidence
  was obtained: `Confirmed — §X.Y` (extracted from a datasheet present locally),
  `Cache-sourced — §X.Y (not locally verified)`, or
  `Cache-sourced — §X.Y (local datasheet differs from cache source)`. An orthogonal `Assumed`
  marker flags any value the checker inferred rather than read.

"Precision gate" means what it says: the measurement is whether FAILs on
known-good boards are real. The one number this document cites for corpus size
is the 126 boards in that gate.

## Why the corpus does not ship

The 126 boards carry a mix of third-party licenses, and the synthetic mutants
are derived from them. Redistributing either set would mean re-licensing other
people's work, so neither is in this repository or its history.

What ships instead is the clone-and-run path: a manifest-verified cache of
datasheet-derived part data under `schematic_checker_poc/datasheets_parsed/`
(what it covers and how it is checked: [LIMITATIONS.md](../LIMITATIONS.md)),
plus the example boards under `examples/`. That is enough to run every checker
on your own boards and on the shipped demos without a single PDF download.
Rebuild tooling for the corpus itself is out of scope for this release.

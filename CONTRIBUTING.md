# Contributing to DeepERC

Thanks for your interest — DeepERC is in early public release and
moving quickly.

## Cadence

One maintainer, part-time. Pull requests are reviewed in batches,
roughly every one to two weeks. If something has sat longer than
that without a word, ping the thread — latency is expected here,
silence is not.

## Most useful contributions

- **Bug reports.** Open a GitHub issue. Where possible include the
  netlist snippet involved and the report output.
- **Suspected false positives or false negatives.** Especially
  valuable — include the board context and the check name from the
  report. A false positive on a real board is worth more than a
  feature request.
- **Test fixtures.** A netlist exhibiting a defect, with the defect
  labeled: which nets, which pins, what the correct wiring would be.
  Fixtures are how coverage claims stay honest. Only submit designs
  you have the right to redistribute, and say where they came from.
  Rails sidecars (`*.net.rails.json`) are scratch inputs, not tracked
  files — if your fixture depends on declared rail voltages, state
  them in the defect description instead of committing a sidecar.
- **Documentation.** Corrections and clarifications welcome directly.

## Please discuss first

Open an issue before starting work on:

- A new check or checker family. Every check is measured against a
  maintainer-side verification corpus before it lands (see
  [docs/CORPUS.md](docs/CORPUS.md) for what that corpus is and why it does not
  ship) — a PR adding a check cannot demonstrate its own precision.
- Anything that changes what an existing check reports on. Verdict
  movement is tracked against a saved baseline; unexplained movement
  blocks a release.
- Changes to caching or extraction behavior.

Small fixes — typos, obvious bugs, doc corrections — go straight to
a PR, no issue needed.

## One hard constraint

**No language-model output may reach a verdict without passing a
deterministic check first.** The LLM extracts datasheet values and
writes explanation prose. It does not decide PASS, FAIL, WARN, or
UNRESOLVABLE. Anything extracted is validated against deterministic
plausibility rules before a check can act on it, and a value that
fails validation produces UNRESOLVABLE rather than a guess.

This is not a stylistic preference. It is the property that makes
the tool's output auditable, and a PR that routes model output into
a verdict — however well-prompted — will be declined.

UNRESOLVABLE is a first-class result, not a failure. "I could not
verify this" is a correct answer and should never be converted into
a PASS or a FAIL to make output look cleaner.

## If your change reads net names

KiCad netlists contain auto-generated `unconnected-(...)` stubs for
open pins. Every existing consumer of net-name meaning filters them
out first through an unconnected-net guard (`_UNCONNECTED_NET_RE` —
per-module copies that the test suite pins to the same pattern). Any
new code that interprets net names — roles, rails, buses — must
apply the same guard before treating a net as a signal. A PR that
skips it will report findings on nets that aren't signals.

## Before you open a PR

Run the test suite:

    make test

The shipped `Makefile` has three targets — `help`, `test`, and
`examples-smoke`. That is deliberate: the private development repo's
other targets all reach corpus data and tooling that don't ship, so
the public file carries only what a clone can actually run. `make test`
expects the `venv/` layout from the README's Install section, installs
its own test dependencies (`requirements-dev.txt`) on first run, runs
the shipped subset of the suite — integration tests and tests that
need external services are excluded by marker — and finishes by
running `examples-smoke`: every fixture under `examples/` gets run
fresh and its findings diffed against the `expected_verdict` block in
its own `provenance.json`. A fixture whose actual output has drifted
from what its sidecar documents fails the gate; this is the mechanism
that keeps the "a skip on a fixture-backed test is a bug" rule below
honest for verdicts, not just for whether the test runs at all.

Skips are normal and environment-conditional: some tests skip when
optional inputs aren't present, and the count varies between
environments and releases, so don't expect a specific number. One
exception: a skip on a test whose fixture ships in `examples/` is a
bug — please report it.

No `make` available? The recipe's raw steps work directly from the
repo root:

    python3 -m venv venv
    venv/bin/pip install -r requirements.txt -r requirements-dev.txt
    cd schematic_checker_poc
    ../venv/bin/python3 -m pytest -q -m "not integration and not gemma_smoke"
    cd ..
    venv/bin/python3 examples_smoke.py

## How pull requests land

The public tree in this repository is generated from a private
development tree at each release, so pull requests are not merged
directly into the source of truth. The flow is:

1. Your commits are cherry-picked into the private tree with your
   authorship preserved, and validated there against the full
   suite and the maintainer-side verification corpus.
2. If validation is clean, your PR is rebase-merged here, so the
   public history stays linear and the commits land under your
   name with Merged credit on the PR.
3. Any new files you add are admitted to the release export
   manifest, so they survive future releases.

The merge happens before the next release's export sync, not
after. By the time a release runs, the public tree already carries
your commits and the sync has nothing left to copy. That is the
intended outcome, not a sign a step was skipped.

One caveat. Changes touching the hashed verdict-affecting source
set (`CHECKER_SOURCE_FILES` in
`schematic_checker_poc/provenance.py`) constitute an announced
measurement-era break: they invalidate cached measurement
provenance and require a full re-measurement cycle. Those are
batched with planned era breaks rather than merged immediately,
even when the change is behaviorally equivalent. If your PR
touches one of those files, expect a hold with an explanation
rather than a fast merge, and a note on the thread when it lands.

## License

By contributing, you agree your contributions are licensed under the
Apache License 2.0.

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

The shipped `Makefile` has exactly two targets — `help` and `test`.
That is deliberate: the private development repo's other targets all
reach corpus data and tooling that don't ship, so the public file
carries only what a clone can actually run. `make test` expects the
`venv/` layout from the README's Install section, installs its own
test dependencies (`requirements-dev.txt`) on first run, and runs
the shipped subset of the suite — integration tests and tests that
need external services are excluded by marker.

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

## License

By contributing, you agree your contributions are licensed under the
Apache License 2.0.

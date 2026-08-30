# TODO-303 2d — wild-shaped constructed fixtures

Card fixture mandate (Phase 2d). Three minimal, hand-authored KiCad-netlist-format
`.net` files, verified by `../test_todo303_wild_shaped_fixtures.py` against the real
production coherence functions (`peripheral_coherence.check_i2c_coherence` /
`check_spi_coherence`) with the real, disk-loaded peripheral KB
(`pipeline.get_peripheral_kb()`/`get_peripheral_routing()`) — same call chain
`step_08d_peripheral_checker.py` uses in production.

- `m6_wild_shaped_swap.net` — I2C SDA/SCL genuinely swapped; both nets named in the
  Direction-2 auto-gen wrapper shape (`Net-(U1-I2C1_SCL{slash}PB6)` /
  `Net-(U1-I2C1_SDA{slash}PB7)`). 2 FAILs expected.
- `m12_wild_shaped_swap.net` — SPI MOSI/MISO genuinely swapped; both nets named in a
  D2-wrapped + Direction-4 tilde-brace shape (`Net-(U1-~{SPI0_MOSI}{slash}PA7)` /
  `Net-(U1-~{SPI0_MISO}{slash}PA6)`). 2 FAILs expected.
- `negative_kb_doubles_correct_i2c.net` — CLAUDE.md KB-evidence-rule FP validation:
  a real KB'd part (STM32F103C8T6) with deliberately generic pin functions (`PB6`/
  `PB7`, not `SCL`/`SDA`) so the role assertion can only come from the KB source,
  correctly wired, both nets wild-wrapped AND Direction-1 suffix-decorated
  (`Net-(U1-I2C1_SCL_3V3{slash}PB6)`). Zero findings expected.

## Scoping decision (documented)

These fixtures are **not** wired into `generate_bad_corpus.py`'s `SEM_SEEDS`/
freeze-manifest/`EXPECTED_CHECKER_OUTCOME` machinery, and do **not** count toward
the recall harness's M6/M12 HIT/MISS accounting. That integration touches the
frozen bad-corpus freeze (376 files, `seed=1234`) and the generator's per-operator
catalog — a larger, riskier change than "add 3 fixtures" warrants as a same-cycle
addition without independent review, so it was deliberately deferred rather than
attempted in a single unreviewed pass (see the 2d report's discovered-work
section). The pytest integration test in this directory's parent is the actual
verification for this phase, using the real production check functions and real
KB rather than a synthetic recall-harness path.

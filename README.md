# DeepERC

A deterministic-first schematic checker for KiCad. It parses a netlist, resolves
each component against cached datasheet extractions and a structured peripheral
knowledge base, and runs a fixed battery of checkers — supply voltage, structural
connectivity, peripheral pin-role coherence and bus capability, pull-up presence/value,
output conflicts — to flag concrete, evidence-backed defects that KiCad's
built-in ERC cannot see. Any LLM-generated text in a report explains a verdict
the deterministic checks already reached; it never asserts one on its own.

(During development this tool was named "schecker" — the earliest upstream issue
reports in [FINDINGS.md](FINDINGS.md) reference that name.)

## How it works

```
BUILD TIME — once, by us, offline. Ships in this repo as data.

  vendor datasheet PDF
        │
        ▼
  extract pin groups — Claude Haiku + pdfplumber   ← the only LLM that
        │                                            produces data a
        ▼                                            check ever reads
  deterministic plausibility gates
        │
        ▼
  <part>_pin_groups.json        kb/ — peripheral role tables
    sha256 of the source PDF      from vendor programming files
    stamped into the entry        and hand-authored pin tables
                                  — no model in this lane
        └─────────────┬─────────────┘
                      ▼
             ships as tracked data

════════════════════════════════════════════════════════════════

RUN TIME — your machine. No key, no PDFs, no network, no LLM.

  your_board.net
        │
        ▼
  parse ──► resolve ──► power rails ──► pin groups from cache
        │   cache first; a local PDF verifies, never required
        ▼
  seven deterministic checkers
        │   signal · supply · structural · peripheral
        │   pull-up value · output conflict · pull-up presence
        ▼
  deterministic plausibility gates, again
        │   a finding resting on an implausible cached value is
        │   downgraded to UNRESOLVABLE, not reported as a defect
        ▼
  ══ VERDICT ══   PASS · FAIL · WARN · UNRESOLVABLE
        │         Nothing above this line is an LLM call.
        ▼
  report — every finding carries an evidence label saying
        │  whether it was verified against a datasheet on your
        │  disk, or taken from the shipped cache
        │
        └──► explanation text, optional. Uses a local model if one
             is running, canned text if not. Never changes a
             verdict, never leaves your machine.

  Step-by-step detail, the eight-stage peripheral checker, the
  run-time gates, and how the evidence label is measured:
  docs/PIPELINE.md
```

## What runs where: three tiers

1. **The shipped example parts — full checks, no keys, no PDFs, no GPU, no
   LLM.** The repo ships pin-group caches for 114 parts, listed with hashes
   in `schematic_checker_poc/datasheets_parsed/CACHE_MANIFEST.json`, including the demo board's two resolved parts
   (AMS1117-3.3, STM32F103C8T6). Every check runs on a fresh clone.
   The console's `Supply evidence` block labels each datasheet-backed
   finding `Cache-sourced — … (not locally verified)`; fetch the two vendor
   PDFs yourself and the same findings are re-labelled `Confirmed — …` (see
   [Verifying the cache locally](#verifying-the-cache-locally)).

2. **Any board, PDF-free — limited support, no keys, no LLM.** The peripheral,
   structural, pull-up-presence, and output-conflict checks run on any KiCad
   netlist from the tracked knowledge base (`kb/`) alone. Datasheet-backed
   checks on parts without a cached extraction report `UNRESOLVABLE` — an
   honest "couldn't verify," never a silent `PASS` and never a fabricated
   `FAIL`.

3. **Your own parts — full checks after one extraction run.** Run the shipped
   extractor against your parts' datasheets with an extraction backend (an
   Anthropic API key, or an experimental fully-local backend — see
   [Extracting your own parts](#extracting-your-own-parts)). Extraction is
   one-time per part and cached; checking itself never calls an LLM.

## Install

Requires Python 3 and git. Tested on Python 3.12 (Ubuntu 24.04); older
versions are untested. On a bare Ubuntu/Debian system:
`sudo apt install python3 python3-venv python3-pip git make`.

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` is the complete runtime manifest for the check path —
nothing else to install. (Tests need `requirements-dev.txt`; see
[Running the tests](#running-the-tests).)

## 30-second demo (after a one-time pip install)

```bash
python3 run_checks.py --board examples/my_stm32_board_i2c_swap/my_stm32_board_i2c_swap.net --skip-confirm
```

The board is a deliberately mutated copy of an open-source STM32 design
(`examples/my_stm32_board_i2c_swap/`, attribution and provenance in that
directory) with its I2C2 SDA/SCL pins swapped. Output on a fresh clone — no
PDFs, no key:

```
WARN: corpus dir not found: <repo>/netlist_corpus — proceeding anyway;
--board runs a single named netlist and does not iterate the corpus.
WARN: datasheets dir not found: <repo>/netlist_corpus/datasheets —
proceeding with 0 datasheets; datasheet-backed checks will use the
shipped cache where available, otherwise UNRESOLVABLE.
[STEP 03] L2 vendor lookup: off (optional — the vendor-lookup extension
isn't installed, or no credentials are configured). Using local
datasheets only.
[STEP 03] WARNING: No datasheet found for '100n'.
[STEP 03] WARNING: No datasheet found for '120R'.
[STEP 03] WARNING: No datasheet found for 'USB_B_Micro'.
[STEP 03] WARNING: No datasheet found for 'Conn_01x04_Pin'.
[STEP 03] WARNING: No datasheet found for 'SW_SPDT'.
[STEP 03] WARNING: No datasheet found for '16MHZ'.
[STEP 03] WARNING: 8 components without MPN fields (22u, 10u, 10n, 1u, 10p,
RED, 1K5, 10K) — resolved as generic passives, skipped.
...

─────────────────────────────────────
  RESULT: has_fail
─────────────────────────────────────
  FAIL:            2
  WARN:            0
  UNRESOLVABLE:    2

  Full report: <repo>/corpus_results/reports/my_stm32_board_i2c_swap.json
─────────────────────────────────────

[STEP 08b] Supply evidence:
  PASS          U2 STM32F103C8T6 VBAT (pin 1) <- +3.3V  —  Cache-sourced —
  supply 3.3V within rated range 1.8V - 3.6V (not locally verified)
  PASS          U2 STM32F103C8T6 VDD (pin 24) <- +3.3V  —  Cache-sourced —
  supply 3.3V within rated range 2.0V - 3.6V (not locally verified)
  PASS          U2 STM32F103C8T6 VDD (pin 36) <- +3.3V  —  Cache-sourced —
  supply 3.3V within rated range 2.0V - 3.6V (not locally verified)
  PASS          U2 STM32F103C8T6 VDD (pin 48) <- +3.3V  —  Cache-sourced —
  supply 3.3V within rated range 2.0V - 3.6V (not locally verified)
  PASS          U2 STM32F103C8T6 VDDA (pin 9) <- +3.3VA  —  Cache-sourced —
  supply 3.3V within rated range 2.4V - 3.6V (not locally verified)

Status: has_fail
  signal           0 PASS  0 WARN  0 FAIL  0 UNRESOLVABLE
  supply           5 PASS  0 WARN  0 FAIL  0 UNRESOLVABLE
  structural       11 PASS  0 WARN  0 FAIL
  peripheral       0 PASS  0 WARN  2 FAIL  2 UNRESOLVABLE
  pullup_value     0 WARN  0 FAIL  0 UNRESOLVABLE
  output_conflict  0 FAIL
  pullup_presence  0 WARN
  (0.0s)

FINDINGS

  [FAIL] peripheral — /I2C2_SDA
    Pin U2.21 (function 'PB10') is an I2C SCL pin but sits on net
    '/I2C2_SDA', whose name implies I2C SDA — SDA/SCL swap (role source:
    kb_possible_roles). (MCU KB — no datasheet required)

  [FAIL] peripheral — /I2C2_SCL
    Pin U2.22 (function 'PB11') is an I2C SDA pin but sits on net
    '/I2C2_SCL', whose name implies I2C SCL — SDA/SCL swap (role source:
    kb_possible_roles). (MCU KB — no datasheet required)

UNRESOLVABLE

  peripheral /I2C2_SCL — Cannot fully verify net '/I2C2_SCL': MPN(s) not in KB:
  ['Conn_01x04_Pin']. (MCU KB — no datasheet required)
    KB sources: VENDOR_XML

  peripheral /I2C2_SDA — Cannot fully verify net '/I2C2_SDA': MPN(s) not in KB:
  ['Conn_01x04_Pin']. (MCU KB — no datasheet required)
    KB sources: VENDOR_XML

  (16 PASS finding(s) not shown — full detail in the JSON report)
```

The 2 FAILs are the injected defect:

- `U2.21` (function `PB10`) is an I2C SCL pin but sits on net `/I2C2_SDA`, whose
  name implies I2C SDA — SDA/SCL swap.
- `U2.22` (function `PB11`) is an I2C SDA pin but sits on net `/I2C2_SCL`, whose
  name implies I2C SCL — SDA/SCL swap.

Both are `ROLE_MISMATCH` findings sourced from the peripheral knowledge base
(`kb_possible_roles`), at high confidence. The five supply pins pass against
their rated ranges from the shipped cache — each line in the `Supply
evidence` block carries `(not locally verified)` because no datasheet is on
disk. The `RESULT` box's `UNRESOLVABLE: 2` is the two I2C nets' connector,
whose MPN isn't in the KB.

**Then fix it.** `examples/my_stm32_board_i2c_fixed/` is the same board with
the I2C2 SDA/SCL crossing corrected — `U2`'s two pins are back on their
correct nets:

```bash
python3 run_checks.py --board examples/my_stm32_board_i2c_fixed/my_stm32_board_i2c_fixed.net --skip-confirm
```

```
─────────────────────────────────────
  RESULT: all_pass
─────────────────────────────────────
  FAIL:            0
  WARN:            0
  UNRESOLVABLE:    2

  Full report: <repo>/corpus_results/reports/my_stm32_board_i2c_fixed.json
─────────────────────────────────────
...

UNRESOLVABLE

  peripheral /I2C2_SCL — Cannot fully verify net '/I2C2_SCL': MPN(s) not in KB:
  ['Conn_01x04_Pin']. (MCU KB — no datasheet required)
    KB sources: VENDOR_XML

  peripheral /I2C2_SDA — Cannot fully verify net '/I2C2_SDA': MPN(s) not in KB:
  ['Conn_01x04_Pin']. (MCU KB — no datasheet required)
    KB sources: VENDOR_XML

  (16 PASS finding(s) not shown — full detail in the JSON report)
```

(From `schematic_checker_poc/`: `main.py --netlist
../examples/my_stm32_board_i2c_fixed/my_stm32_board_i2c_fixed.net
--skip-confirm` prints `[VERDICT] all_pass` and exits `0`.)

The board passes — the injected I2C defect is gone. The two `UNRESOLVABLE`
lines remain because the 4-pin connector has no entry in the peripheral
knowledge base, a gap the swap fix doesn't touch, and DeepERC says so rather
than guessing.

**A second example — an MCU-side SPI swap, no datasheet needed.**
`examples/stm32_spi_swap/` wires an STM32F103C8Tx (KiCad's stock
symbol name, unedited) to an ADS8319 ADC with MOSI and MISO crossed
on the MCU side (PA6/PA7). The MCU's symbol pins carry no SPI names,
so the finding comes from the peripheral knowledge base:

Re-running a board writes `corpus_results/reports/<stem>.json` and
overwrites the previous run's report; pass `--output-dir <dir>` to keep
each state (the report lands at `<dir>/reports/<stem>.json`).

```bash
python3 run_checks.py --board examples/stm32_spi_swap/spi_swap_stm32.net --skip-confirm \
    --output-dir corpus_results/spi_no_rails
```

The corpus/datasheets `WARN:` lines from the 30-second demo above don't
reappear here — that earlier run already created `netlist_corpus/datasheets/`.

Console output, captured from a clean public export (env-stripped, no
credentials):

```
[STEP 03] L2 vendor lookup: off (optional — the vendor-lookup extension
isn't installed, or no credentials are configured). Using local
datasheets only.
[STEP 03] WARNING: No datasheet found for 'ADS8319IBDRCR'.
...

─────────────────────────────────────
  RESULT: has_fail
─────────────────────────────────────
  FAIL:            2
  WARN:            0
  UNRESOLVABLE:    8

  Full report:
  <repo>/corpus_results/spi_no_rails/reports/spi_swap_stm32.json
─────────────────────────────────────

[STEP 08b] Supply evidence:
  UNRESOLVABLE  U1 STM32F103C8Tx VBAT (pin 1) <- VBAT  —  Net voltage
  not confirmed (not locally verified)
  UNRESOLVABLE  U1 STM32F103C8Tx VDD (pin 24) <- 3V3  —  Net voltage
  not confirmed (not locally verified)
  UNRESOLVABLE  U1 STM32F103C8Tx VDD (pin 36) <- 3V3  —  Net voltage
  not confirmed (not locally verified)
  UNRESOLVABLE  U1 STM32F103C8Tx VDD (pin 48) <- 3V3  —  Net voltage
  not confirmed (not locally verified)
  PASS          U1 STM32F103C8Tx VDDA (pin 9) <- VDDA_3V3  —
  Cache-sourced — supply 3.3V within rated range 2.4V - 3.6V (not
  locally verified)

Status: has_fail
  signal           0 PASS  0 WARN  0 FAIL  4 UNRESOLVABLE
  supply           1 PASS  0 WARN  0 FAIL  4 UNRESOLVABLE
  structural       10 PASS  0 WARN  0 FAIL
  peripheral       0 PASS  0 WARN  2 FAIL  0 UNRESOLVABLE
  pullup_value     0 WARN  0 FAIL  0 UNRESOLVABLE
  output_conflict  0 FAIL
  pullup_presence  0 WARN
  (0.0s)

FINDINGS

  [FAIL] peripheral — /MOSI
    Pin U1.16 (function 'PA6') is an SPI MISO pin but sits on net
    '/MOSI', whose name implies SPI MOSI — MOSI/MISO swap (role
    source: kb_possible_roles). (MCU KB — no datasheet required)

  [FAIL] peripheral — /MISO
    Pin U1.17 (function 'PA7') is an SPI MOSI pin but sits on net
    '/MISO', whose name implies SPI MISO — MOSI/MISO swap (role
    source: kb_possible_roles). (MCU KB — no datasheet required)

UNRESOLVABLE

  signal /CSb — Receiver specs unknown — U2 pin specs not extracted
  (signal_score too low or datasheet section not found)
  [Assumed/low-confidence]
    driver=U1  receiver=U2 pin=CONVST
    evidence: Assumed/low-confidence
    confidence: low

  signal /MISO — Receiver specs unknown — U2 pin specs not extracted
  (signal_score too low or datasheet section not found)
  [Assumed/low-confidence]
    driver=U1  receiver=U2 pin=SDO
    evidence: Assumed/low-confidence
    confidence: low

  signal /MOSI — Receiver specs unknown — U2 pin specs not extracted
  (signal_score too low or datasheet section not found)
  [Assumed/low-confidence]
    driver=U1  receiver=U2 pin=SDI
    evidence: Assumed/low-confidence
    confidence: low

  signal /SCLK — Receiver specs unknown — U2 pin specs not extracted
  (signal_score too low or datasheet section not found)
  [Assumed/low-confidence]
    driver=U1  receiver=U2 pin=SCLK
    evidence: Assumed/low-confidence
    confidence: low

  supply U1 VBAT (pin 1) <- VBAT — Net voltage not confirmed (not
  locally verified)
    U1 (STM32F103C8Tx)  pin=VBAT (pin 1)  net=VBAT
    NoneV vs rated 1.8V-3.6V (abs max 3.6V)
    evidence: Net voltage not confirmed (not locally verified)
    confidence: low

  supply U1 VDD (pin 24) <- 3V3 — Net voltage not confirmed (not
  locally verified)
    U1 (STM32F103C8Tx)  pin=VDD (pin 24)  net=3V3
    NoneV vs rated 2.0V-3.6V (abs max 4.0V)
    evidence: Net voltage not confirmed (not locally verified)
    confidence: low

  supply U1 VDD (pin 36) <- 3V3 — Net voltage not confirmed (not
  locally verified)
    U1 (STM32F103C8Tx)  pin=VDD (pin 36)  net=3V3
    NoneV vs rated 2.0V-3.6V (abs max 4.0V)
    evidence: Net voltage not confirmed (not locally verified)
    confidence: low

  supply U1 VDD (pin 48) <- 3V3 — Net voltage not confirmed (not
  locally verified)
    U1 (STM32F103C8Tx)  pin=VDD (pin 48)  net=3V3
    NoneV vs rated 2.0V-3.6V (abs max 4.0V)
    evidence: Net voltage not confirmed (not locally verified)
    confidence: low

  (11 PASS finding(s) not shown — full detail in the JSON report)
```

Tell it what they are — save this as `spi_swap_stm32.net.rails.json`
next to the netlist:

```json
{
  "3V3":  {"voltage": 3.3, "is_rail": true, "is_ground": false},
  "5V":   {"voltage": 5.0, "is_rail": true, "is_ground": false},
  "VBAT": {"voltage": 3.0, "is_rail": true, "is_ground": false}
}
```

```bash
python3 run_checks.py --board examples/stm32_spi_swap/spi_swap_stm32.net --skip-confirm \
    --output-dir corpus_results/spi_with_rails
```

Console output for this state:

```
[STEP 03] L2 vendor lookup: off (optional — the vendor-lookup extension
isn't installed, or no credentials are configured). Using local
datasheets only.
[STEP 03] WARNING: No datasheet found for 'ADS8319IBDRCR'.
...

─────────────────────────────────────
  RESULT: has_fail
─────────────────────────────────────
  FAIL:            2
  WARN:            0
  UNRESOLVABLE:    4

  Full report:
  <repo>/corpus_results/spi_with_rails/reports/spi_swap_stm32.json
─────────────────────────────────────

[STEP 08b] Supply evidence:
  PASS          U1 STM32F103C8Tx VBAT (pin 1) <- VBAT  —
  Cache-sourced — supply 3.0V within rated range 1.8V - 3.6V (not
  locally verified)
  PASS          U1 STM32F103C8Tx VDD (pin 24) <- 3V3  —
  Cache-sourced — supply 3.3V within rated range 2.0V - 3.6V (not
  locally verified)
  PASS          U1 STM32F103C8Tx VDD (pin 36) <- 3V3  —
  Cache-sourced — supply 3.3V within rated range 2.0V - 3.6V (not
  locally verified)
  PASS          U1 STM32F103C8Tx VDD (pin 48) <- 3V3  —
  Cache-sourced — supply 3.3V within rated range 2.0V - 3.6V (not
  locally verified)
  PASS          U1 STM32F103C8Tx VDDA (pin 9) <- VDDA_3V3  —
  Cache-sourced — supply 3.3V within rated range 2.4V - 3.6V (not
  locally verified)

Status: has_fail
  signal           0 PASS  0 WARN  0 FAIL  4 UNRESOLVABLE
  supply           5 PASS  0 WARN  0 FAIL  0 UNRESOLVABLE
  structural       10 PASS  0 WARN  0 FAIL
  peripheral       0 PASS  0 WARN  2 FAIL  0 UNRESOLVABLE
  pullup_value     0 WARN  0 FAIL  0 UNRESOLVABLE
  output_conflict  0 FAIL
  pullup_presence  0 WARN
  (0.0s)

FINDINGS

  [FAIL] peripheral — /MOSI
    Pin U1.16 (function 'PA6') is an SPI MISO pin but sits on net
    '/MOSI', whose name implies SPI MOSI — MOSI/MISO swap (role
    source: kb_possible_roles). (MCU KB — no datasheet required)

  [FAIL] peripheral — /MISO
    Pin U1.17 (function 'PA7') is an SPI MOSI pin but sits on net
    '/MISO', whose name implies SPI MISO — MOSI/MISO swap (role
    source: kb_possible_roles). (MCU KB — no datasheet required)

UNRESOLVABLE

  signal /CSb — Driver voltage unknown — U2 power domain ambiguous (2
  rail pin(s), 2 distinct voltage(s): [3.3, 5.0]) [Cache-sourced —
  Table 36: I/O static characteristics (formula: 0.42*(VDD-2V)+1V)
  (not locally verified)]
    driver=U2  receiver=U1 pin=PA4
    evidence: Cache-sourced — Table 36: I/O static characteristics
    (formula: 0.42*(VDD-2V)+1V) (not locally verified)
    confidence: low

  signal /MISO — Driver voltage unknown — U2 power domain ambiguous
  (2 rail pin(s), 2 distinct voltage(s): [3.3, 5.0]) [Cache-sourced —
  Table 36: I/O static characteristics (formula: 0.42*(VDD-2V)+1V)
  (not locally verified)]
    driver=U2  receiver=U1 pin=PA7
    evidence: Cache-sourced — Table 36: I/O static characteristics
    (formula: 0.42*(VDD-2V)+1V) (not locally verified)
    confidence: low

  signal /MOSI — Driver voltage unknown — U2 power domain ambiguous
  (2 rail pin(s), 2 distinct voltage(s): [3.3, 5.0]) [Cache-sourced —
  Table 36: I/O static characteristics (formula: 0.42*(VDD-2V)+1V)
  (not locally verified)]
    driver=U2  receiver=U1 pin=PA6
    evidence: Cache-sourced — Table 36: I/O static characteristics
    (formula: 0.42*(VDD-2V)+1V) (not locally verified)
    confidence: low

  signal /SCLK — Driver voltage unknown — U2 power domain ambiguous
  (2 rail pin(s), 2 distinct voltage(s): [3.3, 5.0]) [Cache-sourced —
  Table 36: I/O static characteristics (formula: 0.42*(VDD-2V)+1V)
  (not locally verified)]
    driver=U2  receiver=U1 pin=PA5
    evidence: Cache-sourced — Table 36: I/O static characteristics
    (formula: 0.42*(VDD-2V)+1V) (not locally verified)
    confidence: low

  (15 PASS finding(s) not shown — full detail in the JSON report)
```

Declare `VBAT` at 3.7 instead of 3.0 in the rails sidecar and re-run:

```bash
python3 run_checks.py --board examples/stm32_spi_swap/spi_swap_stm32.net --skip-confirm \
    --output-dir corpus_results/spi_bad_vbat
```

Console output for this state:

```
[STEP 03] L2 vendor lookup: off (optional — the vendor-lookup extension
isn't installed, or no credentials are configured). Using local
datasheets only.
[STEP 03] WARNING: No datasheet found for 'ADS8319IBDRCR'.
...

─────────────────────────────────────
  RESULT: has_fail
─────────────────────────────────────
  FAIL:            3
  WARN:            0
  UNRESOLVABLE:    4

  Full report:
  <repo>/corpus_results/spi_bad_vbat/reports/spi_swap_stm32.json
─────────────────────────────────────

[STEP 08b] Supply evidence:
  FAIL          U1 STM32F103C8Tx VBAT (pin 1) <- VBAT  —
  Cache-sourced — actual 3.7V exceeds supply absolute max 3.6V —
  device damage (Table 6: Voltage characteristics) (not locally
  verified)
  PASS          U1 STM32F103C8Tx VDD (pin 24) <- 3V3  —
  Cache-sourced — supply 3.3V within rated range 2.0V - 3.6V (not
  locally verified)
  PASS          U1 STM32F103C8Tx VDD (pin 36) <- 3V3  —
  Cache-sourced — supply 3.3V within rated range 2.0V - 3.6V (not
  locally verified)
  PASS          U1 STM32F103C8Tx VDD (pin 48) <- 3V3  —
  Cache-sourced — supply 3.3V within rated range 2.0V - 3.6V (not
  locally verified)
  PASS          U1 STM32F103C8Tx VDDA (pin 9) <- VDDA_3V3  —
  Cache-sourced — supply 3.3V within rated range 2.4V - 3.6V (not
  locally verified)

Status: has_fail
  signal           0 PASS  0 WARN  0 FAIL  4 UNRESOLVABLE
  supply           4 PASS  0 WARN  1 FAIL  0 UNRESOLVABLE
  structural       10 PASS  0 WARN  0 FAIL
  peripheral       0 PASS  0 WARN  2 FAIL  0 UNRESOLVABLE
  pullup_value     0 WARN  0 FAIL  0 UNRESOLVABLE
  output_conflict  0 FAIL
  pullup_presence  0 WARN
  (4.1s)

FINDINGS

  [FAIL] supply — U1 VBAT (pin 1) <- VBAT
    U1 (STM32F103C8Tx)  pin=VBAT (pin 1)  net=VBAT
    3.7V vs rated 1.8V-3.6V (abs max 3.6V)
    evidence: Cache-sourced — actual 3.7V exceeds supply absolute max
    3.6V — device damage (Table 6: Voltage characteristics) (not
    locally verified)
    confidence: high

  [FAIL] peripheral — /MOSI
    Pin U1.16 (function 'PA6') is an SPI MISO pin but sits on net
    '/MOSI', whose name implies SPI MOSI — MOSI/MISO swap (role
    source: kb_possible_roles). (MCU KB — no datasheet required)

  [FAIL] peripheral — /MISO
    Pin U1.17 (function 'PA7') is an SPI MOSI pin but sits on net
    '/MISO', whose name implies SPI MISO — MOSI/MISO swap (role
    source: kb_possible_roles). (MCU KB — no datasheet required)

UNRESOLVABLE

  signal /CSb — Driver voltage unknown — U2 power domain ambiguous (2
  rail pin(s), 2 distinct voltage(s): [3.3, 5.0]) [Cache-sourced —
  Table 36: I/O static characteristics (formula: 0.42*(VDD-2V)+1V)
  (not locally verified)]
    driver=U2  receiver=U1 pin=PA4
    evidence: Cache-sourced — Table 36: I/O static characteristics
    (formula: 0.42*(VDD-2V)+1V) (not locally verified)
    confidence: low

  signal /MISO — Driver voltage unknown — U2 power domain ambiguous
  (2 rail pin(s), 2 distinct voltage(s): [3.3, 5.0]) [Cache-sourced —
  Table 36: I/O static characteristics (formula: 0.42*(VDD-2V)+1V)
  (not locally verified)]
    driver=U2  receiver=U1 pin=PA7
    evidence: Cache-sourced — Table 36: I/O static characteristics
    (formula: 0.42*(VDD-2V)+1V) (not locally verified)
    confidence: low

  signal /MOSI — Driver voltage unknown — U2 power domain ambiguous
  (2 rail pin(s), 2 distinct voltage(s): [3.3, 5.0]) [Cache-sourced —
  Table 36: I/O static characteristics (formula: 0.42*(VDD-2V)+1V)
  (not locally verified)]
    driver=U2  receiver=U1 pin=PA6
    evidence: Cache-sourced — Table 36: I/O static characteristics
    (formula: 0.42*(VDD-2V)+1V) (not locally verified)
    confidence: low

  signal /SCLK — Driver voltage unknown — U2 power domain ambiguous
  (2 rail pin(s), 2 distinct voltage(s): [3.3, 5.0]) [Cache-sourced —
  Table 36: I/O static characteristics (formula: 0.42*(VDD-2V)+1V)
  (not locally verified)]
    driver=U2  receiver=U1 pin=PA5
    evidence: Cache-sourced — Table 36: I/O static characteristics
    (formula: 0.42*(VDD-2V)+1V) (not locally verified)
    confidence: low

  (14 PASS finding(s) not shown — full detail in the JSON report)
```

That is the point.

## Verifying the cache locally

The repo ships the cached datasheet *extractions* for the demo board's two
resolved parts, never the vendor PDFs. Checks run fully without them.
Placing the PDFs on disk changes nothing about the verdicts; it lets the
checker confirm each finding against the datasheet itself, and the evidence
label on each `Supply evidence` line says which it did:

- `Confirmed — …` — the local PDF byte-matches the one the cache was
  extracted from.
- `Cache-sourced — … (not locally verified)` — no local PDF.
- `Cache-sourced — … (local datasheet differs from cache source)` — a local
  PDF is present but doesn't match; the report also carries a drift warning.
  Vendors re-render PDFs periodically; the AMS1117-3.3 asset on the vendor
  page has already rotated once since the shipped cache was built, so a
  fetched copy will most likely trip the report's drift warning — expected,
  not an error.

To see the labels change, fetch two files:

1. **Create the datasheet directories:**

   ```bash
   mkdir -p netlist_corpus/datasheets/misc netlist_corpus/datasheets/st
   ```

2. **AMS1117-3.3** — product page:
   https://www.evvosemi.com/en/index/productsinfo/id/1089.html
   Download the datasheet from that page and rename the download to
   `netlist_corpus/datasheets/misc/5272_AMS1117-3.3.pdf`. (The product
   page's own download link has rotated to a newer PDF asset since this
   repo's shipped cache was built — vendors re-render datasheets
   periodically; content is unchanged, and the third label above is exactly
   what tells you that honestly.) If you'd rather
   script it, the exact URL that was pinned and unattended-fetchable as of
   this writing is:

   ```bash
   curl -L --max-time 60 -o netlist_corpus/datasheets/misc/5272_AMS1117-3.3.pdf \
       https://evvosemi.com/Uploads/file/20241015/2024101510221244.pdf
   ```

3. **STM32F103C8** — product page:
   https://www.st.com/en/microcontrollers-microprocessors/stm32f103c8.html
   st.com blocks scripted/`curl` fetches of its datasheet PDFs (Akamai CDN
   — the connection resets at the TLS/HTTP2 layer rather than returning a
   clean 4xx, confirmed live: `curl: (92) HTTP/2 stream 1 was not closed
   cleanly: INTERNAL_ERROR`). Open the product page above in a real
   browser, download the datasheet from its documentation section, and
   rename the download to `netlist_corpus/datasheets/st/stm32f103c8.pdf`.

Re-run the demo. Verdicts are unchanged — `FAIL 2 / WARN 0 / UNRESOLVABLE 2`
— and the five `Supply evidence` lines now read `Confirmed — …`:

```
[STEP 08b] Supply checks: 5 PASS  0 WARN  0 FAIL  0 UNRESOLVABLE
...

─────────────────────────────────────
  RESULT: has_fail
─────────────────────────────────────
  FAIL:            2
  WARN:            0
  UNRESOLVABLE:    2

  Full report: <repo>/corpus_results/reports/my_stm32_board_i2c_swap.json
─────────────────────────────────────

[STEP 08b] Supply evidence:
  PASS          U2 STM32F103C8T6 VBAT (pin 1) <- +3.3V  —  Confirmed —
  supply 3.3V within rated range 1.8V - 3.6V
  PASS          U2 STM32F103C8T6 VDD (pin 24) <- +3.3V  —  Confirmed —
  supply 3.3V within rated range 2.0V - 3.6V
  PASS          U2 STM32F103C8T6 VDD (pin 36) <- +3.3V  —  Confirmed —
  supply 3.3V within rated range 2.0V - 3.6V
  PASS          U2 STM32F103C8T6 VDD (pin 48) <- +3.3V  —  Confirmed —
  supply 3.3V within rated range 2.0V - 3.6V
  PASS          U2 STM32F103C8T6 VDDA (pin 9) <- +3.3VA  —  Confirmed —
  supply 3.3V within rated range 2.4V - 3.6V
```

(Structural stays at `11 PASS  0 WARN  0 FAIL` — nothing about the swap
changes. `RESULT` is still `has_fail`: the injected I2C defect is still
there, and is supposed to be — that's the point of the demo.)

## Exit codes and the JSON summary

`run_checks.py --board` exits `0` on any completed run — the RESULT
box / `summary` in the JSON report carries the verdict, not the process
exit code. It exits nonzero only on a pipeline error (parse failure,
crash) that prevented a report from being produced at all.

`main.py` exits `1` when the verdict is `has_fail`, `0` otherwise — this
is the CI-usable gate; check the `[VERDICT]` line or the JSON `summary`
for the full picture in either case.

`main.py` writes its JSON report to `./report.json` in the current working
directory (unlike `run_checks.py --board`, which writes to
`corpus_results/reports/<board-stem>.json`). From `schematic_checker_poc/`:

```bash
python3 main.py --netlist ../examples/my_stm32_board_i2c_swap/my_stm32_board_i2c_swap.net --skip-confirm
```

```
  Full report: ./report.json
─────────────────────────────────────
...
[VERDICT] has_fail
```

Exits `1` — this is the swap board (the same defect as the 30-second demo
above), so `has_fail` is expected.

In the JSON report, `summary.pass`/`warn`/`fail`/`unresolvable` are the
signal-net (`[STEP 08]`) bucket only — the same four keys the box printed
before the other six checkers existed. For a grand total across every
checker (`supply`, `structural`, `peripheral`, `pullup_value`,
`output_conflict`, `pullup_presence` included), read
`summary.total_pass`/`total_warn`/`total_fail`/`total_unresolvable`
instead (schema `poc-1.4`, purely additive — the four un-prefixed keys
keep their original signal-only meaning).

## Extracting your own parts

For boards beyond the shipped examples, uncached parts report `UNRESOLVABLE`
until you extract them once. The extractor reads a part's datasheet PDF and
produces the cached pin-group extraction the checkers consume.

**Default backend (Anthropic API).** Put a key in `.env` at the repo root
(`ANTHROPIC_API_KEY=...`) or export it in your environment. Then:

```bash
cd schematic_checker_poc
python3 main.py \
    --netlist ../netlist_corpus/your_board.net \
    --datasheets-dir ../netlist_corpus/datasheets \
    --skip-confirm
```

`your_board.net` is a placeholder — substitute the path to a netlist
exported from KiCad (`File → Export → Netlist`, KiCad format).

Place the part's datasheet PDF under `netlist_corpus/datasheets/` (any
subdirectory) before running. `main.py` resolves every component on the
board and, for any part without a cached extraction, calls the default
`haiku_pdfplumber` backend to build one — no separate "extract this one
part" command exists; pointing `--netlist` at a board that references the
new part is how a single part gets extracted. The resulting cache lands at
`schematic_checker_poc/datasheets_parsed/<pdf-filename-stem>/`, read by
every subsequent run.

With no key configured, a run touching an uncached part degrades cleanly: the
part reports `UNRESOLVABLE` with the cause spelled out ("no .env found and
ANTHROPIC_API_KEY not set") — never a traceback, never a silent pass.

**Re-extraction.** Extraction is one-time and cached against the source PDF's
hash. If a vendor revises a datasheet, re-run with `--refresh`:

```bash
python3 main.py --netlist ../netlist_corpus/your_board.net \
    --datasheets-dir ../netlist_corpus/datasheets --skip-confirm \
    --refresh STM32F103C8T6
```

`--refresh` takes the datasheet PDF's filename stem (case-insensitive) and
is repeatable — pass it once per part you want to force re-extracted; the
prior cache is never deleted, just renamed to a
`<stem>_pin_groups.json.pre_reextract` sidecar next to the new one.

**Experimental local backend (`gemma_mineru`).** A fully local, zero-key
extraction backend exists for environments where no API use is possible. It is
explicitly lower-quality than the default: it fails extraction cases the
default backend clears, and its output goes through the same deterministic
plausibility gates, so weaker extraction surfaces as more `UNRESOLVABLE`
results, not as wrong verdicts. Treat it as the no-API escape hatch, not an
equal alternative. It shells out to [MinerU](https://github.com/opendatalab/MinerU)
(the `magic-pdf` package) for PDF-to-markdown conversion — not installed by
`requirements.txt` and not bundled with this repo, so opting into this backend
means separately `pip install`ing it yourself; MinerU is licensed AGPL-3.0,
which is a materially different license than the rest of this project's
dependencies, so check that fits your use case before installing it.

## Running the tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
make test
```

## KB authoring

The peripheral knowledge base ships as structured data in `kb/`. The tooling
used to build and extend it is not part of this release; a documented authoring
path is planned. Datasheet-backed coverage grows through the extractor above —
KB authoring is only about the peripheral role/topology tables.

## Limitations

- Coverage is bounded by what's in `kb/` plus your cached extractions. Anything
  outside both is reported `UNRESOLVABLE`, by design — absence of evidence is
  never converted into a verdict.
- This tool complements KiCad's ERC; it does not replace it. It targets defect
  classes ERC cannot express (role/net-name coherence, per-part voltage limits,
  pull-up presence) and assumes you still run ERC for structural rule checking.
- Report explanation text is LLM-generated where a local model is available;
  when it isn't, findings carry a one-line note instead. Verdicts are never
  affected either way.

# Findings

Real electrical defects that DeepERC found in production open-hardware designs. Every entry below was detected by an automated check against the board's exported netlist, manually verified in KiCad against the upstream repository at its current HEAD, and reported to the maintainers with full evidence. Issue links are public; judge the evidence yourself.

A property all three findings share: **each one is invisible to KiCad's ERC.** A crossed net label, a wrong-voltage part on a valid rail — these are electrically legal connections as far as structural rule checking is concerned. Two of the three boards had passed an explicit ERC-cleanup commit with the defect in place; in one case the ERC-cleanup commit is what *introduced* the defect.

| # | Board | Defect | Check | Filed | Status |
|---|-------|--------|-------|-------|--------|
| 1 | Antmicro Jetson AGX Thor baseboard | I²C SDA/SCL swapped on ID EEPROM | I²C role coherence | 2026-07-06 | Open ([issue #1](https://github.com/antmicro/jetson-agx-thor-baseboard/issues/1)) |
| 2 | Antmicro Artix DC-SCM | 1.8 V flash ×4 wired to 3.3 V rail | Supply overvoltage | 2026-07-17 | Open ([issue #6](https://github.com/antmicro/artix-dc-scm/issues/6)) |
| 3 | Tronex TRNXSDR carrier | I²C bus crossed at root sheet (PMIC + clock gen) | I²C role coherence (zero-KB) | 2026-07-19 | Open ([issue #1](https://github.com/acruxcz/TRNXSDR-carrier/issues/1)) |

Status column reflects upstream state as of 2026-09-03.

---

## 1. Jetson AGX Thor baseboard — U54 ID EEPROM, SDA/SCL swapped

**Repo:** [antmicro/jetson-agx-thor-baseboard](https://github.com/antmicro/jetson-agx-thor-baseboard) · **Issue:** [#1](https://github.com/antmicro/jetson-agx-thor-baseboard/issues/1) (the repository's first-ever issue) · **Verified at:** HEAD `d966e9c`, rev 1.1.0

**What the checker found.** On the Peripherals sheet, U54 (AT24CS01 ID EEPROM) has its I²C connections crossed: pin 5 (SDA) is wired to the `I2C_SYS` SCL net and pin 6 (SCL) to the SDA net. The check corroborated across the bus — the other two devices on `I2C_SYS` (a SLB9673 TPM and a PCAL6408A expander) are wired correctly, and the AT24CS01 library symbol itself is correct, which localizes the defect to the wiring at U54 rather than a library error.

**Impact.** The ID EEPROM (0x50) and its serial-number region (0x58) cannot respond; board identification over `I2C_SYS` fails on fabricated rev 1.1.0 hardware.

**Why ERC missed it.** Crossed net labels are electrically valid connections. The defect has been present since the sheet was introduced and survived a later commit dedicated to ERC/DRC cleanup — structural rule checking has no concept of which pin *should* be on which net.

**Upstream response:** open, no maintainer activity (re-verified live 2026-08-22; issue and repo HEAD unchanged since filing).

## 2. Artix DC-SCM — four 1.8 V flash parts on the 3.3 V rail

**Repo:** [antmicro/artix-dc-scm](https://github.com/antmicro/artix-dc-scm) · **Issue:** [#6](https://github.com/antmicro/artix-dc-scm/issues/6) · **Verified at:** HEAD `b9856f78`

**What the checker found.** Four W25Q32JWSSIQ SPI-NOR flash instances (U2–U5) have VCC on the `VCC3V3` rail. The W25Q32**JW** is Winbond's 1.8 V line: operating range 1.7–1.95 V, absolute maximum 2.5 V. The supply-overvoltage check flagged all four instances as FAIL with evidence graded **Confirmed / high** — the voltage limit was extracted from the part's datasheet, not inferred from a heuristic.

**How it got there.** Git history shows the design originally used the W25Q32**JV** (the 3.3 V line). A later commit — titled *"Fix schematic ERC errors. ERC clean"* — changed the part to the JW. The commit that made the schematic ERC-clean is the commit that introduced a real overvoltage.

**Why ERC missed it.** A rail connection is electrically valid to ERC regardless of what the connected part is rated for. Catching this requires knowing, per part, what voltage its supply pin tolerates — which is exactly the datasheet-derived knowledge this tool runs on.

**Upstream response:** open, no maintainer activity (re-verified live 2026-08-22; issue and repo HEAD unchanged since filing).

## 3. TRNXSDR carrier — SMU I²C bus B crossed at the root sheet

**Repo:** [acruxcz/TRNXSDR-carrier](https://github.com/acruxcz/TRNXSDR-carrier) (Tronex s.r.o. Zynq SDR carrier, production V0.1) · **Issue:** [#1](https://github.com/acruxcz/TRNXSDR-carrier/issues/1) (the repository's first-ever issue) · **Verified at:** HEAD `5a3998a`

**What the checker found.** At the root sheet, the SMU microcontroller (STM32G070) drives its SCL onto the SDA pins of both slaves on I²C bus B — U4 (LP87524J-Q1 PMIC) and U13 (5P35023B clock generator) — and vice versa. The board conveniently contains its own control group: bus A uses the same parts, same symbols, and same sub-sheets, wired correctly. That corroboration pins the defect to a single two-wire crossing at the sheet pins.

Per the LP87524 datasheet (SNVSAW2 §5), pin 5 (SCL) is input-only while pin 6 (SDA) is bidirectional — as drawn, U4 cannot ACK, so bus B's I²C is non-functional. The board still boots on the PMIC's OTP defaults, which is consistent with the working captures shown in the project's README: the kind of failure that hides until someone needs runtime PMIC or clock control.

**Notable for the tool:** this finding fired with an empty knowledge base for every part involved — the verdict came purely from pin-function evidence in the netlist plus the vendor datasheet, not from curated part entries.

**Why ERC missed it.** A hierarchical sheet-pin name/function mismatch is not a structural error. The defect has been present since the repository's first commit and survived a production review that fixed a label typo two wires away.

**Upstream response:** open, no maintainer activity (re-verified live 2026-08-22; issue and repo HEAD unchanged since filing).

---

*Note on names: issues #2 and #3 above reference the tool by its pre-release working name, "schecker." Same tool — it was renamed DeepERC for public release.*

# Attribution

The netlist in this directory (`my_stm32_board_i2c_swap.net`) is a **deliberately
mutated copy** of a board schematic exported from a third-party open-source KiCad
project. It is not upstream's file as-shipped — one I2C SDA/SCL pin-role swap was
injected (see `provenance.json` for the exact mutation) to produce a fixture that
`schecker` can flag.

## Original board

- **Project:** STM32-PCB-Design
- **Author:** [Shadaab1904](https://github.com/Shadaab1904)
- **Repository:** https://github.com/Shadaab1904/STM32-PCB-Design
- **Commit:** `72a71d6dba94db2bab8bb0e1f884ef3a1e5a6df1`
- **License:** MIT, `Copyright (c) 2025 Shadaab1904`

The unmodified board (netlist, schematic, and license file) is tracked in this repo
at `netlist_corpus/stm32/community/shadaab1904-STM32-PCB-Design/`, including its
original `LICENSE` file. This directory's fixture is derived from that copy.

## MIT License text (as shipped upstream)

```
MIT License

Copyright (c) 2025 Shadaab1904

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## This directory

This is the same board as `examples/my_stm32_board_i2c_swap/`, with the
injected I2C2 SDA/SCL pin-role crossing corrected — U2 pin 21 (PB10) restored
to net `/I2C2_SCL` and U2 pin 22 (PB11) restored to net `/I2C2_SDA`, the
inverse of that directory's mutation. It exists as the "after" half of the
README's before/after demo (see `provenance.json`'s `derived_from` for the
exact correction).

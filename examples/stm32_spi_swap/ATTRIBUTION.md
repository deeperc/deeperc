# Attribution

Unlike this repo's other `examples/` fixtures — which are deliberately mutated
copies of real, third-party open-source boards — the netlist in this directory
(`spi_swap_stm32.net`) is **original, hand-authored work**. There is no
upstream project to attribute.

It's a minimal, purpose-built two-part design: an STM32F103C8Tx (KiCad's
unedited stock symbol) wired to an ADS8319 SPI ADC, with MOSI and MISO crossed
on the MCU side (PA6/PA7). It exists specifically to demonstrate the case
where an MCU-side SPI swap has to be caught from the peripheral knowledge base
alone — the MCU's symbol pins carry no SPI-specific names, so there's no
net-name shortcut available to the checker. See `provenance.json` for the
exact expected findings.

Licensed under this repository's own license (see `LICENSE` at the repo
root) — no third-party license terms apply.

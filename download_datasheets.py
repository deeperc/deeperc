#!/usr/bin/env python3
"""
download_datasheets.py — Step 3 of the schecker pipeline.

Extracts unique component MPNs from all .kicad_sch files in the corpus,
resolves a direct PDF download URL per part, downloads each datasheet with
per-domain rate limiting and exponential backoff, and writes datasheet_map.json.

Run after download_netlist_corpus.py has populated ./netlist_corpus/.
"""

import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

# ---------------------------------------------------------------------------
# Per-request download timeouts (connect_timeout, read_timeout). Bounds the
# cost of Akamai-class GET tarpits surfaced in the v2_10_9 baseline analysis:
# 8 manufacturer hosts (analog/st/te/eaton/molex) hang ~60s/attempt on GET
# (not a HEAD-only block), so 3 retries + backoff burned ~220s per URL.
# 10s connect bounds the TCP+TLS handshake stall; 30s read bounds a stalled
# body stream while still allowing legitimately slow large-PDF downloads.
# Timeout-class failures fail fast (no in-call retry — see download_with_backoff):
# these hosts hang consistently, not transiently, so the worst case per URL is
# one attempt (~connect 10s + read 30s = ~40s) instead of ~220s. Transient
# blips are still retried at the coarser per-board grain until the L2 negative
# cache marks the MPN failed.
DOWNLOAD_CONNECT_TIMEOUT_SEC = 10
DOWNLOAD_READ_TIMEOUT_SEC = 30

# ---------------------------------------------------------------------------
# Manufacturer patterns — prefix matching (longest match wins)
# ---------------------------------------------------------------------------

MANUFACTURER_PATTERNS: dict[str, dict] = {
    "raspberrypi": {
        "prefixes": ["RP2040", "RP2350"],
        "domain": "datasheets.raspberrypi.com",
        "delay_seconds": 1.0,
    },
    "espressif": {
        "prefixes": ["ESP32", "ESP8266", "ESP-"],
        "domain": "espressif.com",
        "delay_seconds": 1.0,
    },
    "nordic": {
        "prefixes": ["NRF51", "NRF52", "NRF53", "NRF91", "NRF54"],
        "domain": "nordicsemi.com",
        "delay_seconds": 2.0,
    },
    "st": {
        "prefixes": [
            "STM32", "STM8", "SPC5", "STLUX", "STSPIN", "LD1117", "LD33",
            "LDL", "LDK", "STPS", "STTH", "L6", "L9", "VN",
            # ST MEMS sensors — same datasheet URL pattern
            "LIS", "LSM", "L3G", "A3G", "HTS", "LPS",
            # ST ESD protection (ESDA family) — same /resource/en/datasheet/ URL pattern
            "ESDA",
        ],
        "domain": "st.com",
        "delay_seconds": 3.0,
    },
    "ti": {
        "prefixes": [
            "LM", "TPS", "TMS", "MSP430", "CC1", "CC2", "TM4C", "AM",
            "OPA", "INA", "REF", "ADS", "DAC", "LMR", "LMV", "LMC", "LMH",
            "LP", "UCC", "TL", "SN", "CD", "UC", "NE", "SE", "UA",
            "TPD",  # TI ESD protection (TPDxExx family)
        ],
        "domain": "ti.com",
        "delay_seconds": 3.0,
    },
    "microchip": {
        "prefixes": [
            "PIC", "ATMEGA", "ATTINY", "ATSAMD", "MCP", "TC", "MIC",
            "SST", "AT45", "AT24", "AT25", "DSPIC",
        ],
        "domain": "microchip.com",
        "delay_seconds": 2.0,
    },
    "nxp": {
        "prefixes": [
            "LPC", "IMXRT", "MKE", "MKV", "MKW", "MK6", "MK8",
            "MIMXRT", "S32", "MC9", "FXOS", "FXAS",
        ],
        "domain": "nxp.com",
        "delay_seconds": 2.0,
    },
    "analog_devices": {
        "prefixes": [
            "ADXL", "ADF", "ADG", "ADL", "ADM", "ADP", "ADR", "ADT",
            "ADV", "ADA", "ADC", "AD",
            "LTC", "LTM", "LT",
            "MAX0", "MAX1", "MAX2", "MAX3", "MAX4", "MAX5", "MAX6",
            "MAX7", "MAX8", "MAX9", "MAX16", "MAX17", "MAX",
        ],
        "domain": "analog.com",
        "delay_seconds": 2.0,
    },
    "onsemi": {
        "prefixes": [
            "NCP", "NCE", "NCS", "NCV", "FQP", "FQD",
            "MJE", "MJF", "MJ", "2N", "BC", "BAT", "BAS", "MC",
            "FDN", "FDS",  # Fairchild legacy (onsemi acquired)
        ],
        "domain": "onsemi.com",
        "delay_seconds": 2.0,
    },
    "winbond": {
        "prefixes": ["W25Q", "W25X", "W74M"],
        "domain": "winbond.com",
        "delay_seconds": 2.0,
    },
    "ftdi": {
        "prefixes": ["FT2", "FT4", "FT6"],
        "domain": "ftdichip.com",
        "delay_seconds": 2.0,
    },
    "silabs": {
        "prefixes": ["CP21"],
        "domain": "silabs.com",
        "delay_seconds": 2.0,
    },
    "wch": {
        "prefixes": ["CH3", "CH2"],
        "domain": "wch-ic.com",
        "delay_seconds": 2.0,
    },
    "bosch": {
        "prefixes": ["BMP", "BME", "BMA", "BMI", "BMG", "BHI", "BNO"],
        "domain": "bosch-sensortec.com",
        "delay_seconds": 2.0,
    },
    "invensense": {
        "prefixes": ["ICM-", "ICM", "IMU", "ICP"],
        "domain": "invensense.tdk.com",
        "delay_seconds": 2.0,
    },
    "alpha_omega": {
        "prefixes": ["AO3", "AO4", "AO6"],
        "domain": "aosmd.com",
        "delay_seconds": 2.0,
    },
    "diodes_inc": {
        "prefixes": ["AP2", "AP7", "AZ", "DMG"],
        "domain": "diodes.com",
        "delay_seconds": 2.0,
    },
    "nexperia": {
        "prefixes": ["BSS", "BSH", "74AHC", "74LVC", "74HC", "74HCT", "74LS", "74AC"],
        "domain": "nexperia.com",
        "delay_seconds": 2.0,
    },
}

# ---------------------------------------------------------------------------
# Hardcoded datasheet mappings — checked before generic resolver patterns
# ---------------------------------------------------------------------------

# Espressif modules: combined-variant PDFs (two module variants in one file)
ESPRESSIF_MODULE_URLS: dict[str, str] = {
    "ESP-WROOM-32":          "esp32-wroom-32_datasheet_en.pdf",
    "ESP32-WROOM-32":        "esp32-wroom-32_datasheet_en.pdf",
    "ESP32-WROOM-32D":       "esp32-wroom-32d_esp32-wroom-32u_datasheet_en.pdf",
    "ESP32-WROOM-32U":       "esp32-wroom-32d_esp32-wroom-32u_datasheet_en.pdf",
    "ESP32-WROOM-32E":       "esp32-wroom-32e_esp32-wroom-32ue_datasheet_en.pdf",
    "ESP32-WROOM-32E-N4":    "esp32-wroom-32e_esp32-wroom-32ue_datasheet_en.pdf",
    "ESP32-WROOM-32E-N8":    "esp32-wroom-32e_esp32-wroom-32ue_datasheet_en.pdf",
    "ESP32-WROOM-32E-N16":   "esp32-wroom-32e_esp32-wroom-32ue_datasheet_en.pdf",
    "ESP32-WROVER-E":        "esp32-wrover-e_esp32-wrover-ie_datasheet_en.pdf",
    "ESP32-WROVER-IE":       "esp32-wrover-e_esp32-wrover-ie_datasheet_en.pdf",
    "ESP32-S3-MINI-1":       "esp32-s3-mini-1_mini-1u_datasheet_en.pdf",
    "ESP32-S3-MINI-1-N8":    "esp32-s3-mini-1_mini-1u_datasheet_en.pdf",
    "ESP32-C5-WROOM-1":      "esp32-c5-wroom-1_wroom-1u_datasheet_en.pdf",
    "ESP32-C6-WROOM-1":      "esp32-c6-wroom-1_datasheet_en.pdf",
    "ESP32-C6-WROOM-1-N8":   "esp32-c6-wroom-1_datasheet_en.pdf",
}

# STM32: exact MPN → direct URL overrides (checked before family-map prefix matching)
STM32_HARDCODED: dict[str, str] = {
    "STM32F051K8U":  "https://www.st.com/resource/en/datasheet/stm32f051k6.pdf",
    "STM32F303RCT6": "https://www.st.com/resource/en/datasheet/stm32f303cb.pdf",
    "STM32F411":     "https://www.st.com/resource/en/datasheet/stm32f411ce.pdf",
    "STM32F411CCU":  "https://www.st.com/resource/en/datasheet/stm32f411ce.pdf",
    "STM32L031G6U":  "https://www.st.com/resource/en/datasheet/stm32l031f4.pdf",
    "STM32L031G6U6": "https://www.st.com/resource/en/datasheet/stm32l031f4.pdf",
    "STM32L476VET":  "https://www.st.com/resource/en/datasheet/stm32l476rg.pdf",
}

# STM32: family-to-datasheet mapping (one PDF covers multiple pin/memory variants)
STM32_FAMILY_MAP: dict[str, str] = {
    "STM32F411CC":  "stm32f411ce.pdf",  # longest match first for F411CC vs F411
    "STM32F411CE":  "stm32f411ce.pdf",
    "STM32F303RCT6": "stm32f303cb.pdf",
    "STM32F303RC":  "stm32f303cb.pdf",
    "STM32F303RD":  "stm32f303re.pdf",
    "STM32F303RE":  "stm32f303re.pdf",
    "STM32C011J":   "stm32c011j4.pdf",
    "STM32L031G":   "stm32l031f6.pdf",
    "STM32F051K":   "stm32f051k8.pdf",  # K-package (UFQFPN32) family
    "STM32F411":    "stm32f411ce.pdf",
    "STM32F051":    "stm32f051r8.pdf",
    "STM32L031":    "stm32l031f6.pdf",
    "STM32L476":    "stm32l476rg.pdf",
}

# ADI / Linear / Maxim: exact MPN → direct URL overrides
ADI_HARDCODED: dict[str, str] = {
    "AD8009":            "https://www.analog.com/media/en/technical-documentation/data-sheets/AD8009.pdf",
    "AD8051":            "https://www.analog.com/media/en/technical-documentation/data-sheets/AD8051_8052_8054.pdf",
    "LT1129":            "https://www.analog.com/media/en/technical-documentation/data-sheets/lt1129.pdf",
    "LT1373":            "https://www.analog.com/media/en/technical-documentation/data-sheets/lt1373.pdf",
    "LTC4412IS6":        "https://www.analog.com/media/en/technical-documentation/data-sheets/4412fc.pdf",
    "LTC4412IS6#TRMPBF": "https://www.analog.com/media/en/technical-documentation/data-sheets/4412fc.pdf",
    "LTC6957IDD-2":      "https://www.analog.com/media/en/technical-documentation/data-sheets/ltc6957.pdf",
    "MAX16150AUT+T":     "https://www.analog.com/media/en/technical-documentation/data-sheets/MAX16150.pdf",
    "MAX16150AUT":       "https://www.analog.com/media/en/technical-documentation/data-sheets/MAX16150.pdf",
    "MAX17048":          "https://www.analog.com/media/en/technical-documentation/data-sheets/MAX17048-MAX17049.pdf",
    "MAX202":            "https://www.analog.com/media/en/technical-documentation/data-sheets/MAX200-MAX211_MAX213.pdf",
    "MAX3430ESA":        "https://www.analog.com/media/en/technical-documentation/data-sheets/MAX3430.pdf",
}

# onsemi: exact MPN → direct URL overrides (None = discard; not a real part)
# BC-series BJTs are Nexperia parts — do NOT add them here (wrong CDN)
ONSEMI_HARDCODED: dict[str, str | None] = {
    "BC546":      "https://www.onsemi.com/pdf/datasheet/bc546-d.pdf",
    "2N7002":     "https://www.onsemi.com/pdf/datasheet/2n7002-d.pdf",
    "2N7002KT1G": "https://www.onsemi.com/pdf/datasheet/2n7002-d.pdf",
    "BAT_CON":    None,
}

# Nexperia: correct CDN URLs for parts whose CDN blocks bots with 403.
# Kept for reference; actual downloads for these parts go through FORCE_NEXAR → Nexar.
NEXPERIA_HARDCODED: dict[str, str] = {
    "BC237":  "https://assets.nexperia.com/documents/data-sheet/BC237_BC238.pdf",
    "BC307":  "https://assets.nexperia.com/documents/data-sheet/BC307_BC308.pdf",
    "BC337":  "https://assets.nexperia.com/documents/data-sheet/BC337_BC338.pdf",
    "BC848":  "https://assets.nexperia.com/documents/data-sheet/BC848_BC849_BC850.pdf",
    "BSS83P": "https://assets.nexperia.com/documents/data-sheet/BSS83P.pdf",
}

# Parts whose direct CDN URLs are bot-blocked (Nexperia 403s all automated requests).
# For these, skip direct URL resolution entirely and go straight to the Nexar fallback.
FORCE_NEXAR: frozenset[str] = frozenset({"BC237", "BC307", "BC337", "BC848", "BSS83P"})

# Winbond: Mouser-hosted stable copies (Winbond's own CDN embeds non-guessable version strings)
WINBOND_HARDCODED: dict[str, str] = {
    "W25Q128JVS":    "https://www.mouser.com/datasheet/2/949/w25q128jv_revf_03272018_plus-1489608.pdf",
    "W25Q128JVSIQ":  "https://www.mouser.com/datasheet/2/949/w25q128jv_revf_03272018_plus-1489608.pdf",
    "W25Q32JVSS":    "https://www.mouser.com/datasheet/2/949/w25q32jv_revg_03272018_plus-1489560.pdf",
    "W25Q32JVSSIQ":  "https://www.mouser.com/datasheet/2/949/w25q32jv_revg_03272018_plus-1489560.pdf",
    "W25Q32JVZP":    "https://www.mouser.com/datasheet/2/949/w25q32jv_revg_03272018_plus-1489560.pdf",
    "W25Q16JVSS":    "https://www.mouser.com/datasheet/2/949/w25q16jv_spi_revd_08122016-1291814.pdf",
    "W25Q16JVUXIQ":  "https://www.mouser.com/datasheet/2/949/w25q16jv_spi_revd_08122016-1291814.pdf",
    "W25Q80DV_USON": "https://www.mouser.com/datasheet/2/949/w25q80dv_dl_revh_10022015-1294498.pdf",
}

# Microchip: opaque document-number URLs (MPN is not part of the URL)
# Uses new aemDocuments CDN paths (old /downloads/en/DeviceDoc/ returns 301→403)
MICROCHIP_HARDCODED: dict[str, str] = {
    "AT24CM02-SSHD-B": "https://ww1.microchip.com/downloads/aemDocuments/documents/MPD/ProductDocuments/DataSheets/AT24CM02-2-Mbit-I2C-Serial-EEPROM-DS20006197.pdf",
    "AT24CS01-SSHM-B": "https://ww1.microchip.com/downloads/aemDocuments/documents/MPD/ProductDocuments/DataSheets/AT24CS01-2-Kbit-Serial-EEPROM-with-Unique-Serial-Number-20001984.pdf",
    "ATMEGA16U2-MU":   "https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/Atmel-7799-8-bit-AVR-ATmega16U2-32U2_Datasheet.pdf",
    "ATMEGA2560-16AU": "https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/ATmega640-1280-1281-2560-2561-Datasheet-DS40002211A.pdf",
    "ATMEGA328P-MU":   "https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/ATmega48A-PA-88A-PA-168A-PA-328-P-DS-DS40002061B.pdf",
    "ATMEGA328P-PU":   "https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/ATmega48A-PA-88A-PA-168A-PA-328-P-DS-DS40002061B.pdf",
    "ATMEGA32U4-XU":   "https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/Atmel-7766-8-bit-AVR-ATmega16U4-32U4_Datasheet.pdf",
    "ATMEGA32U4-XUMU": "https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/Atmel-7766-8-bit-AVR-ATmega16U4-32U4_Datasheet.pdf",
    "MCP73831":        "https://ww1.microchip.com/downloads/aemDocuments/documents/APID/ProductDocuments/DataSheets/MCP73831-Family-Data-Sheet-DS20001984H.pdf",
    "MCP73831-2-OT":   "https://ww1.microchip.com/downloads/aemDocuments/documents/APID/ProductDocuments/DataSheets/MCP73831-Family-Data-Sheet-DS20001984H.pdf",
    "MCP73832-2-OT":   "https://ww1.microchip.com/downloads/aemDocuments/documents/APID/ProductDocuments/DataSheets/MCP73831-Family-Data-Sheet-DS20001984H.pdf",
    "PIC12C508A":      "https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/41227D.pdf",
    "PIC16F54":        "https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/41375B.pdf",
}

# Reference designator prefixes that indicate passives — skip Value field for these.
# We still include them if they have an explicit MPN property.
PASSIVE_REFDES_PREFIXES = frozenset([
    "R", "C", "L", "D", "TP", "J", "MK", "BT", "SW", "P", "H",
    "ANT", "FID", "G", "E", "F", "LS", "SP", "MP",
])

# Allowlist: Value-field MPNs must start with one of these prefixes to be kept.
# Explicit MPN properties bypass this check (user set them intentionally).
# Ordered longest-first so early entries don't shadow longer matches.
KEEP_PREFIXES: tuple[str, ...] = (
    # MCUs / SoCs
    "STM32", "STM8",
    "ESP32", "ESP8266", "ESP-",
    "ATMEGA", "ATTINY", "ATSAMD",
    "DSPIC",
    "NRF5",
    "RP2040", "RP2350",
    # Logic ICs (longest first to avoid 74H matching 74HC)
    "74AHC", "74AH", "74HCT", "74HC", "74LVC", "74LV", "74ACT", "74AC", "74LS",
    # Sensors — Bosch MEMS
    "BMP", "BME", "BMA", "BMI", "BMG", "BHI", "BNO",
    # Sensors — InvenSense/TDK
    "ICM",
    # Sensors — STMicro MEMS
    "LSM", "LIS", "L3G", "HTS", "LPS",
    # Sensors — generic
    "ADXL", "MPU",
    # ADI/Linear (longer prefixes before shorter)
    "LTC", "LTM", "LT",
    "ADF", "ADG", "ADL", "ADM", "ADP", "ADR", "ADT", "ADV", "ADA", "ADC", "AD",
    "MAX",
    # Microchip MCUs and memory
    "MCP", "PIC", "AT24", "AT25", "AT45",
    # TI (longer before shorter)
    "TPS", "TMS", "INA", "OPA", "DAC", "ADS", "UCC",
    "LMR", "LMV", "LMC", "LMH", "LP",
    "LM", "TL", "SN", "CD",
    # USB bridges
    "CH3", "CH2", "FT2", "FT4", "FT6", "CP21",
    # LDO / power regulators (non-TI/ST)
    "AMS", "AP2", "AP7", "HT7",
    # Flash memory
    "W25", "GD25", "MX25", "IS25", "S25FL",
    # Discretes / MOSFETs
    "BSS", "BSH", "DMG", "FDN", "AO3", "AO4", "AO6", "IRF",
    "2N", "BC",
)

# Values that are never real MPNs (case-insensitive lookup)
PLACEHOLDER_VALUES = frozenset([
    # Generic KiCad placeholders
    "DNP", "TBD", "NA", "N/A", "~", "?", "MOUNT", "LOGO", "HOLE",
    "FIDUCIAL", "MECHANICAL", "TESTPOINT", "TEST", "NC", "NM", "NP",
    "DO NOT POPULATE", "DO NOT INSTALL",
    # SPICE/audio descriptor words
    "BATTERY", "BASS", "TREBLE", "SPEAKER", "MOTOR", "TRANSFORMER",
    "RELAY", "SWITCH", "FUSE", "BUZZER", "ANTENNA",
    # Generic opamp/buffer symbols
    "OPAMP_DUAL", "OPAMP_SINGLE", "OPAMP",
    # KiCad generic symbol / component type names
    "LED", "LED_AK", "LED_RED", "LED_SMALL", "LED/GREEN/0603", "LED/RED/0603",
    "LED/YELLOW/0603", "LED/IR333-A/5MM",
    "CRYSTAL", "CRYSTAL_GND2", "CRYSTAL_GND24", "CRYSTAL_GND24_SMALL",
    "CRYSTAL_GND24_SMALL_EZROUTE", "CRYSTAL_SMALL", "CRYSTAL_SMALL_4P",
    "FERRITEBEAD", "FERRITEBEAD_SMALL", "BEAD", "FB0603", "FB0805",
    "GENERIC_OPAMP",
    "CONNECTOR", "CONN_01X04", "CONN_01X04_PIN",
    "JUMPER", "JUMPER_TRIPLE", "JUMPER_2_OPEN",
    "CON2", "CON3", "CONN_1", "CONN_2", "CONN_3",
    "CONN_2X2", "CONN_4X2", "CONN_8X2", "CONN_13X2", "CONN_30X2",
    "IBIS_DEVICE", "IBIS_DRIVER", "AUDIODRIVER1",
    "AREF", "AREF_SEL", "I2C", "CANBUS", "CHASSIS",
    "CHG", "DBG", "EXTPWR", "FLASH_PWR", "HDMI_+5V",
    "IAM", "IEXP", "IPULSE", "IPWL", "ISFFM", "ISIN", "ITRNOISE", "ITRRANDOM",
    "BTN_SEL", "DEB_EN", "BUSPC", "BUSPCI_5V", "BAT_CON",
    "FLOAT#", "FIDUCIAL_TARGET_C100-200",
    "COMPUTEMODULE5-CM5", "COMPUTEMODULE5-CM5_GPIO",
    "IND0805_WURTH_742792040", "BH16S-OLIMEX_CONNECTORS",
    # Spec/value strings that look like MPNs
    "3.3V_P", "5VREG",
    "8X10K", "9X1K", "5/30PF",
    "8M_HS49S_20PF", "32768_1206_12.5PF",
])

# KiCad power / net names — not components
POWER_SYMBOL_RE = re.compile(
    r"^GND\w*$|^VCC\w*$|^VDD\w*$|^VSS\w*$|"  # GND*, VCC*, VDD*, VSS*
    r"^PWR_FLAG$|^GNDPWR$|^EARTH$|^VREF\w*$",
    re.IGNORECASE,
)

# Generic passive value patterns (10k, 100nF, 1µH, etc.)
PASSIVE_VALUE_RE = re.compile(
    r"^\d+[.,]?\d*\s*[pnuµμmkKMGT]?[ΩΩFHzVAW%Ω]?\s*$"
    r"|^\d+[.,]?\d*\s*[pP][fF]\s*$"
    r"|^\d+[.,]?\d*\s*[uU][hHfF]\s*$"
    r"|^\d+[.,]?\d*\s*[nN][hHfF]\s*$"
    r"|^\d+[.,]?\d*\s*[mM][hH]\s*$"
    r"|^\d+[.,]?\d*\s*[rR]\s*$"
    r"|^\d+[.,]?\d*$"
    r"|^0[Ee]?\s*$"
    r"|^[A-Z]{1,2}$",
    re.IGNORECASE,
)

# Frequency values (crystal oscillators etc.) — not MPNs
FREQUENCY_RE = re.compile(
    r"^\d+[.,]?\d*\s*[kKmMgG]?[hH][zZ]$",
    re.IGNORECASE,
)

# Compiled passive/garbage discard patterns — evaluated in is_generic_value
_PASSIVE_DISCARD_RES: tuple[re.Pattern, ...] = tuple(re.compile(p, re.IGNORECASE) for p in [
    r"^0[24680][0-9][0-9]",          # SMD package codes: 0402, 0603, 0805, 1206, etc.
    r"^[0-9]+[xX][0-9]",             # Arrays like 8x10K, 9x1K
    r"^[0-9]+\.",                     # Values starting digit+decimal (3.3V_P, 12.0MHz)
    r"^[0-9]+/",                      # Values like 5/30pF
    r"^\d+SEPC",                      # Electrolytic cap codes: 16SEPC220MD
    r"^CR[0-9]",                      # Panasonic/Yageo resistors: CR0402-FX-…
    r"^GRM",                          # Murata caps: GRM155R61H104KE14D
    r"^CL[0-9]",                      # Samsung caps: CL05A104KA5NNNC
    r"^ERJ",                          # Panasonic resistors: ERJ-2RKF8873X
    r"^CRCW",                         # Vishay resistors: CRCW080547R0FKEAHP
    r"^CC[0-9]",                      # Generic cap codes: CC0402…, CC1206… (NOT TI CC13xx)
    r"^GC[A-Z]",                      # Murata caps: GCM155L8EH104KE07D
    r"^C[0-9]",                       # Cap codes: C0402C104J4RAC7411, C1005X6S1A105K050BC
    r"^AC[0-9]",                      # Thin-film resistors: AC0402FR-135K1L
    r"^EXB",                          # Panasonic resistor arrays: EXB-18VR000X
    r"^GG[0-9]",                      # Murata inductors: GG0402052R542P
    r"^BLM",                          # Murata ferrite beads: BLM18PG121SN1D
    r"^[0-9]{3,4}[A-Z]{1,3}[0-9]",   # Generic package-first part numbers
    r".*[-_]lz1ddd$",                 # KiCad/Olimex internal suffix
])

# Net label suffixes that indicate signal names, not part numbers
NET_LABEL_SUFFIX_RE = re.compile(
    r"(_P|_N|_SEL|_EN|_PWR|_CON)$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Regex for s-expression property extraction
# ---------------------------------------------------------------------------

PROPERTY_RE = re.compile(
    r'\(property\s+"(Value|MPN|Part Number|Manufacturer Part Number)"\s+"([^"]+)"'
)
REFERENCE_RE = re.compile(r'\(property\s+"Reference"\s+"([^"]+)"')

# ---------------------------------------------------------------------------
# Rate limiter state
# ---------------------------------------------------------------------------

_last_request_time: dict[str, float] = {}


def rate_limited_request(
    method: str,
    url: str,
    delay: float,
    session: requests.Session,
    **kwargs,
) -> requests.Response:
    domain = urlparse(url).netloc
    now = time.monotonic()
    wait = delay - (now - _last_request_time.get(domain, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_request_time[domain] = time.monotonic()  # mark before request so timeouts count
    response = getattr(session, method)(url, **kwargs)
    _last_request_time[domain] = time.monotonic()  # update again on success for precise spacing
    return response


# ---------------------------------------------------------------------------
# Manufacturer detection
# ---------------------------------------------------------------------------

def detect_manufacturer(mpn: str) -> str | None:
    """Return manufacturer key or None if unknown. Longest prefix wins."""
    upper = mpn.upper()

    # CC prefix collision: CC0402, CC0603, CC1206, CC1210, CC2010 are EIA
    # capacitor package-size codes, not TI CC-series wireless chips.
    # Discard here so they don't route to TI and produce wrong URLs.
    if re.match(r"^CC\d{4}", upper):
        return None

    best_mfr = None
    best_len = 0
    for mfr, info in MANUFACTURER_PATTERNS.items():
        for prefix in info["prefixes"]:
            if upper.startswith(prefix.upper()) and len(prefix) > best_len:
                best_mfr = mfr
                best_len = len(prefix)
    return best_mfr


def get_delay(manufacturer: str | None) -> float:
    if manufacturer and manufacturer in MANUFACTURER_PATTERNS:
        return MANUFACTURER_PATTERNS[manufacturer]["delay_seconds"]
    return 1.0  # default for Nexar / unknown


# ---------------------------------------------------------------------------
# URL resolvers
# ---------------------------------------------------------------------------

def resolve_espressif_url(mpn: str) -> list[str]:
    _BASE = "https://www.espressif.com/sites/default/files/documentation/"
    upper = mpn.upper()
    seen: set[str] = set()
    urls: list[str] = []

    def add(u: str) -> None:
        if u not in seen:
            seen.add(u)
            urls.append(u)

    # Hardcoded combined-variant filenames (two module variants in one PDF)
    for key, filename in ESPRESSIF_MODULE_URLS.items():
        if upper == key.upper():
            add(_BASE + filename)
            break

    # Generic pattern candidates
    base = mpn.lower().replace(" ", "-")
    add(f"{_BASE}{base}_datasheet_en.pdf")

    base_us = base.replace("-", "_")
    if base_us != base:
        add(f"{_BASE}{base_us}_datasheet_en.pdf")

    base_nf = re.sub(r"-n\d+$", "", base)
    if base_nf != base:
        add(f"{_BASE}{base_nf}_datasheet_en.pdf")

    parts = base.split("-")
    if len(parts) > 3:
        base_short = "-".join(parts[:3])
        add(f"{_BASE}{base_short}_datasheet_en.pdf")

    return urls


def resolve_st_url(mpn: str) -> list[str]:
    _BASE = "https://www.st.com/resource/en/datasheet/"
    upper = mpn.upper()

    # STM32 exact hardcoded overrides — checked before family-map prefix matching
    if upper in STM32_HARDCODED:
        return [STM32_HARDCODED[upper]]

    # STM32 family mapping: one PDF covers multiple pin/memory variants
    if upper.startswith("STM32"):
        best = max(
            (k for k in STM32_FAMILY_MAP if upper.startswith(k)),
            key=len,
            default=None,
        )
        if best:
            return [_BASE + STM32_FAMILY_MAP[best]]

    base = mpn.lower()
    stripped = re.sub(r'[a-z]{1,4}\d{1,2}$', '', base).rstrip('-')
    urls = [f"{_BASE}{base}.pdf"]
    if stripped != base:
        urls.append(f"{_BASE}{stripped}.pdf")
    return urls


def resolve_ti_url(mpn: str) -> list[str]:
    """FIX 7+: aggressive suffix stripping + bare 74xxx → SN prefix."""
    base = mpn.lower()
    seen: set[str] = set()
    urls: list[str] = []

    def add(stem: str) -> None:
        url = f"https://www.ti.com/lit/ds/symlink/{stem}.pdf"
        if url not in seen:
            seen.add(url)
            urls.append(url)

    add(base)

    # Strip 2+ trailing alpha chars after last digit: LMV358IDGKR → lmv358
    # But first try keeping just the first trailing letter (revision): SN74LV166ANS → sn74lv166a
    s_keep1 = re.sub(r'(?<=[0-9])([a-z])[a-z]{2,}[a-z0-9]*$', r'\1', base)
    if s_keep1 != base:
        add(s_keep1)
    s1 = re.sub(r'(?<=[0-9])[a-z]{2,}[a-z0-9]*$', '', base)
    if s1 != base:
        add(s1)
        s2 = re.sub(r'(?<=[0-9])[a-z]{2,}[a-z0-9]*$', '', s1)
        if s2 != s1:
            add(s2)

    # Strip trailing single-letter+digit (e.g., A1 in INA180A1 → ina180)
    s_a1 = re.sub(r'[a-z]\d$', '', base)
    if s_a1 != base:
        add(s_a1)

    # Strip voltage/grade suffix (-3.3, -33, -1.8): LP2985-3.3 → lp2985
    s_v = re.sub(r'-\d+[\.\d]*$', '', base)
    if s_v != base:
        add(s_v)
        # Also strip trailing alpha after voltage strip: LP3982ILD-3.3 → lp3982
        s_v2 = re.sub(r'(?<=[0-9])[a-z]{2,}[a-z0-9]*$', '', s_v)
        if s_v2 != s_v:
            add(s_v2)

    # Bare 74xxx → also try SN-prefixed TI URL
    if base.startswith("74") and not base.startswith("sn"):
        u = f"https://www.ti.com/lit/ds/symlink/sn{base}.pdf"
        if u not in seen:
            seen.add(u)
            urls.append(u)

    urls.append(f"https://www.ti.com/lit/gpn/{base}")
    return urls


def resolve_microchip_url(mpn: str) -> list[str]:
    upper = mpn.upper()
    if upper in MICROCHIP_HARDCODED:
        url = MICROCHIP_HARDCODED[upper]
        urls = [url]
        # Generic fallback: if a hardcoded URL still uses the old DeviceDoc path,
        # also try the new aemDocuments path with the same filename.
        if "/downloads/en/DeviceDoc/" in url:
            filename = url.rsplit("/", 1)[-1]
            urls.append(
                "https://ww1.microchip.com/downloads/aemDocuments/documents/"
                f"MCU08/ProductDocuments/DataSheets/{filename}"
            )
        return urls
    return [
        f"https://ww1.microchip.com/downloads/en/DeviceDoc/{upper}.pdf",
        f"https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/{upper}.pdf",
    ]


def resolve_nordic_url(mpn: str) -> list[str]:
    base = mpn.upper()
    return [
        f"https://docs.nordicsemi.com/bundle/{base}_PS/resource/{base}_PS.pdf",
    ]


def resolve_raspberrypi_url(mpn: str) -> list[str]:
    base = mpn.lower()
    urls = [f"https://datasheets.raspberrypi.com/{base}/{base}-datasheet.pdf"]
    # RP2040-B2 (chip stepping) and RP2040_Stamp (module) both map to the base RP2040 datasheet
    chip = re.sub(r'[-_][a-z0-9]+$', '', base)
    if chip != base:
        urls.append(f"https://datasheets.raspberrypi.com/{chip}/{chip}-datasheet.pdf")
    return urls


def resolve_analog_devices_url(mpn: str) -> list[str]:
    # Strip ordering suffixes: # (LTC4412IS6#TRMPBF) and + (MAX16150AUT+T)
    base = mpn.split("#")[0].split("+")[0].upper()
    # Check hardcoded dict first on both raw MPN and stripped base
    for key in (mpn.upper(), base):
        if key in ADI_HARDCODED:
            return [ADI_HARDCODED[key]]
    urls = [
        f"https://www.analog.com/media/en/technical-documentation/data-sheets/{base}.pdf",
    ]
    stripped1 = re.sub(r'[_-][A-Z0-9]+$', '', base)
    if stripped1 != base:
        urls.append(
            f"https://www.analog.com/media/en/technical-documentation/data-sheets/{stripped1}.pdf"
        )
        stripped2 = re.sub(r'[_-][A-Z0-9]+$', '', stripped1)
        if stripped2 != stripped1:
            urls.append(
                f"https://www.analog.com/media/en/technical-documentation/data-sheets/{stripped2}.pdf"
            )
    return urls


def resolve_nxp_url(mpn: str) -> list[str]:
    return [
        f"https://www.nxp.com/docs/en/data-sheet/{mpn.upper()}.pdf",
        f"https://cache.nxp.com/docs/en/data-sheet/{mpn.upper()}.pdf",
    ]


def resolve_onsemi_url(mpn: str) -> list[str]:
    base = mpn.upper()
    # Hardcoded exact matches (None = not a real part)
    if base in ONSEMI_HARDCODED:
        url = ONSEMI_HARDCODED[base]
        return [url] if url else []

    candidates = [base]
    # Strip tape-and-reel/lead-free suffixes: T1G, T3G, T4G, T1, T3, T4
    stripped = re.sub(r'T\dG?$', '', base)
    if stripped != base and stripped not in candidates:
        candidates.append(stripped)
        if stripped in ONSEMI_HARDCODED:
            url = ONSEMI_HARDCODED[stripped]
            return [url] if url else []
    # Strip bare lead-free G suffix
    stripped_g = re.sub(r'G$', '', base)
    if stripped_g != base and stripped_g not in candidates:
        candidates.append(stripped_g)
        if stripped_g in ONSEMI_HARDCODED:
            url = ONSEMI_HARDCODED[stripped_g]
            return [url] if url else []
    # 2N7002 CDN uses 2N7002T-D.PDF; try T-suffixed variant for 2N bare transistors
    if re.match(r'^2N\d+$', base):
        candidates.append(base + 'T')

    urls = []
    for c in candidates:
        # Primary: /pdf/datasheet/ path (lowercase filenames)
        urls.append(f"https://www.onsemi.com/pdf/datasheet/{c.lower()}-d.pdf")
        urls.append(f"https://www.onsemi.com/pdf/datasheet/{c.lower()}-fsc-d.pdf")
        # Secondary: /download/data-sheet/pdf/ path
        urls.append(f"https://www.onsemi.com/download/data-sheet/pdf/{c.lower()}-d.pdf")
        urls.append(f"https://www.onsemi.com/download/data-sheet/pdf/{c}-D.PDF")
    urls.append(f"https://www.onsemi.com/pub/Collateral/{base}-D.pdf")
    return urls


def resolve_winbond_url(mpn: str) -> list[str]:
    upper = mpn.upper()
    if upper in WINBOND_HARDCODED:
        return [WINBOND_HARDCODED[upper]]
    # Strip W25Q/W25X prefix: W25Q128JVS → 128jvs
    variant = re.sub(r'^W25[QX]', '', upper).lower()
    # Strip _PACKAGE suffix: 80dv_uson → 80dv
    variant = re.sub(r'_.*$', '', variant)
    seen: set[str] = set()
    urls: list[str] = []

    def add(v: str) -> None:
        u = f"https://www.winbond.com/resource-files/w25q{v}.pdf"
        if u not in seen:
            seen.add(u)
            urls.append(u)

    add(variant)  # 128jvs (full)
    # Keep first trailing alpha after digit, strip the rest: 128jvs → 128j
    v1 = re.sub(r'(?<=[0-9])([a-z])[a-z]{2,}[a-z0-9]*$', r'\1', variant)
    if v1 != variant:
        add(v1)   # 128j
        v2 = re.sub(r'[a-z]+$', '', v1)  # strip all trailing alpha: 128
        if v2 != v1:
            add(v2)
    return urls


def resolve_ftdi_url(mpn: str) -> list[str]:
    """FIX 5: FTDI — two CDN patterns; Nexar is primary."""
    upper = mpn.upper()
    return [
        f"https://ftdichip.com/wp-content/uploads/2023/09/{upper}.pdf",
        f"https://www.ftdichip.com/Documents/DataSheets/{upper}.pdf",
    ]


def resolve_silabs_url(mpn: str) -> list[str]:
    """FIX 5+: Silicon Labs CP21xx — strip ordering code to base."""
    lower = mpn.lower()
    urls = [f"https://www.silabs.com/documents/public/data-sheets/{lower}.pdf"]
    # CP2105-F01-GM → cp2105; CP2102N-A02-GQFN28 → cp2102n
    base = re.sub(r'-[a-z0-9]+-\w+$', '', lower)
    if base == lower:
        base = re.sub(r'-\w+$', '', lower)
    if base != lower:
        urls.append(f"https://www.silabs.com/documents/public/data-sheets/{base}.pdf")
    return urls


def resolve_wch_url(mpn: str) -> list[str]:
    """FIX 5: WCH CH340 series — no predictable direct PDF; use Nexar."""
    return []


def resolve_bosch_url(mpn: str) -> list[str]:
    """FIX 5+: Bosch Sensortec BMP/BME/BMI/BMG/BHI/BNO — try ds001/002/000."""
    lower = mpn.lower().replace("-", "")
    base = f"https://www.bosch-sensortec.com/media/boschsensortec/downloads/datasheets/bst-{lower}-ds"
    return [f"{base}001.pdf", f"{base}002.pdf", f"{base}000.pdf"]


def resolve_invensense_url(mpn: str) -> list[str]:
    """FIX 5: TDK/InvenSense ICM — URL not predictable; use Nexar."""
    return []


def resolve_alpha_omega_url(mpn: str) -> list[str]:
    """FIX 5: Alpha & Omega AO3/AO4/AO6 MOSFETs."""
    upper = mpn.upper()
    return [
        f"https://aosmd.com/res/data_sheets/{upper}.pdf",
        f"https://www.aosmd.com/pdfs/datasheet/{upper}.pdf",
    ]


def resolve_diodes_inc_url(mpn: str) -> list[str]:
    """FIX 5+: Diodes Inc — progressive suffix stripping for ordering codes."""
    upper = mpn.upper()
    candidates = [upper]
    # Strip ordering/voltage suffix after hyphen: AP7361C-33E → AP7361C, DMG1012T-7 → DMG1012T
    s1 = re.sub(r'-[A-Z0-9.]+$', '', upper)
    if s1 != upper and s1 not in candidates:
        candidates.append(s1)
    # Strip trailing variant letter(s) from base: AP2112K → AP2112, AP2161W → AP2161
    base = upper.split('-')[0]
    s2 = re.sub(r'[A-Z]+$', '', base)
    if s2 and s2 not in candidates:
        candidates.append(s2)
    return [f"https://www.diodes.com/assets/Datasheets/{c}.pdf" for c in candidates]


def resolve_nexperia_url(mpn: str) -> list[str]:
    """FIX 5+: 74-series → TI CDN first; BSS/BSH → onsemi CDN first."""
    upper = mpn.upper()
    lower = mpn.lower()
    urls: list[str] = []
    seen: set[str] = set()

    def add(u: str) -> None:
        if u not in seen:
            seen.add(u)
            urls.append(u)

    if upper.startswith("74"):
        # TI CDN is reliably accessible; try sn74xxx with progressive suffix stripping
        bases = [lower]
        b1 = re.sub(r'-[a-z0-9]+$', '', lower)
        if b1 != lower:
            bases.append(b1)
        b2 = re.sub(r'[a-z]{2,5}$', '', bases[-1])
        if b2 != bases[-1]:
            bases.append(b2)
        for b in bases:
            add(f"https://www.ti.com/lit/ds/symlink/sn{b}.pdf")
        # Skip nexperia CDN for 74-series: it blocks automated requests with 403
        return urls
    elif upper.startswith(("BSS", "BSH")):
        # onsemi distributes many BSS/BSH parts (acquired from Fairchild)
        stripped = re.sub(r'T\dG?$', '', upper)
        add(f"https://www.onsemi.com/download/data-sheet/pdf/{upper}-D.PDF")
        if stripped != upper:
            add(f"https://www.onsemi.com/download/data-sheet/pdf/{stripped}-D.PDF")

    # Nexperia CDN as authoritative fallback — filenames are lowercase
    add(f"https://assets.nexperia.com/documents/data-sheet/{lower}.pdf")
    return urls


RESOLVERS = {
    "espressif": resolve_espressif_url,
    "st": resolve_st_url,
    "ti": resolve_ti_url,
    "microchip": resolve_microchip_url,
    "nordic": resolve_nordic_url,
    "raspberrypi": resolve_raspberrypi_url,
    "analog_devices": resolve_analog_devices_url,
    "nxp": resolve_nxp_url,
    "onsemi": resolve_onsemi_url,
    "winbond": resolve_winbond_url,
    "ftdi": resolve_ftdi_url,
    "silabs": resolve_silabs_url,
    "wch": resolve_wch_url,
    "bosch": resolve_bosch_url,
    "invensense": resolve_invensense_url,
    "alpha_omega": resolve_alpha_omega_url,
    "diodes_inc": resolve_diodes_inc_url,
    "nexperia": resolve_nexperia_url,
}


def candidate_urls(mpn: str, manufacturer: str | None) -> list[str]:
    if manufacturer and manufacturer in RESOLVERS:
        return RESOLVERS[manufacturer](mpn)
    return []


# ---------------------------------------------------------------------------
# URL validation (HEAD request)
# ---------------------------------------------------------------------------

def validate_url(
    url: str,
    delay: float,
    session: requests.Session,
    timeout: int = 15,
) -> bool:
    """
    Return True if the URL is worth attempting a full download.

    HEAD responses are interpreted as follows:
    - 200 + PDF content-type → definitely valid
    - 403/405 (method not allowed) → server blocks HEAD; try GET anyway
    - 404/410/400 → definitely invalid, skip
    - timeout → optimistically try GET (analog.com/st.com stall HEAD but serve GET fine)
    - anything else (other 2xx, 3xx, 5xx) → try GET
    """
    try:
        resp = rate_limited_request(
            "head", url, delay, session,
            timeout=timeout,
            allow_redirects=True,
        )
        if resp.status_code in (404, 410, 400, 414):
            return False
        ct = resp.headers.get("Content-Type", "")
        if resp.status_code == 200 and "pdf" in ct.lower():
            return True
        # 403/405 and other statuses → optimistically try the download
        return True
    except requests.Timeout:
        # HEAD timed out — some CDNs (analog.com, st.com) stall HEAD but serve GET fine.
        # Return True and let download_with_backoff's %PDF check catch non-PDF responses.
        return True
    except requests.RequestException:
        # Connection error etc. → skip
        return False


# ---------------------------------------------------------------------------
# Nexar API
# ---------------------------------------------------------------------------

NEXAR_DATASHEET_QUERY = """
query GetDatasheet($mpn: String!) {
  supSearch(q: $mpn, limit: 1) {
    results {
      part {
        mpn
        manufacturer { name }
        documentCollections {
          documents {
            name
            url
            mimeType
          }
        }
      }
    }
  }
}
"""

_nexar_token: str | None = None


def get_nexar_token(client_id: str, client_secret: str) -> str:
    response = requests.post(
        "https://identity.nexar.com/connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def get_nexar_datasheet_url(
    mpn: str,
    client_id: str,
    client_secret: str,
) -> str | None:
    global _nexar_token
    if not _nexar_token:
        _nexar_token = get_nexar_token(client_id, client_secret)

    for attempt in range(2):
        try:
            resp = requests.post(
                "https://api.nexar.com/graphql",
                json={"query": NEXAR_DATASHEET_QUERY, "variables": {"mpn": mpn}},
                headers={"Authorization": f"Bearer {_nexar_token}"},
                timeout=30,
            )
            time.sleep(1.0)

            if resp.status_code == 401 and attempt == 0:
                _nexar_token = get_nexar_token(client_id, client_secret)
                continue

            resp.raise_for_status()
            data = resp.json()
            results = (
                (data.get("data") or {})
                .get("supSearch", {})
                .get("results", [])
            )
            if not results:
                return None
            collections = results[0]["part"].get("documentCollections", [])
            for collection in collections:
                for doc in collection.get("documents", []):
                    url = doc.get("url", "")
                    mime = doc.get("mimeType", "")
                    if mime == "application/pdf" or url.lower().endswith(".pdf"):
                        return url
            return None
        except requests.RequestException:
            if attempt == 0:
                time.sleep(5)
            else:
                return None
    return None


# ---------------------------------------------------------------------------
# Download engine
# ---------------------------------------------------------------------------

def download_with_backoff(
    url: str,
    dest_path: Path,
    delay: float,
    session: requests.Session,
    max_retries: int = 3,
) -> bool:
    """Stream URL to dest_path. Return True on success."""
    for attempt in range(max_retries):
        try:
            resp = rate_limited_request(
                "get", url, delay, session,
                timeout=(DOWNLOAD_CONNECT_TIMEOUT_SEC, DOWNLOAD_READ_TIMEOUT_SEC),
                stream=True,
            )

            if resp.status_code == 200:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = dest_path.with_suffix(".part")
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                # Verify PDF magic bytes
                with open(tmp_path, "rb") as f:
                    magic = f.read(4)
                if magic != b"%PDF":
                    tmp_path.unlink(missing_ok=True)
                    return False
                tmp_path.rename(dest_path)
                return True

            elif resp.status_code in (429, 503):
                wait = (2 ** attempt) * 30
                print(f"  Rate limited ({resp.status_code}), waiting {wait}s…")
                time.sleep(wait)

            elif resp.status_code == 404:
                return False

            else:
                return False

        except requests.Timeout:
            # Akamai-class GET tarpit (connect or read timeout). These hosts
            # hang consistently rather than transiently, so an in-call retry
            # just doubles the cost; fail fast and let the per-board negative
            # cache decide. Bounds each URL at one attempt (~connect 10s +
            # read 30s = ~40s) instead of 3 × 60s + backoff (~220s in v2_10_9).
            # requests.Timeout covers ConnectTimeout and ReadTimeout.
            return False

        except requests.RequestException:
            if attempt < max_retries - 1:
                time.sleep(10 * (attempt + 1))
            else:
                return False

    return False


# ---------------------------------------------------------------------------
# Resume log helpers
# ---------------------------------------------------------------------------

def load_download_log(log_path: Path) -> dict:
    if log_path.exists():
        with open(log_path) as f:
            return json.load(f)
    return {}


def save_download_log(log_path: Path, log: dict) -> None:
    with open(log_path, "w") as f:
        json.dump(log, f, indent=2)


# ---------------------------------------------------------------------------
# MPN extraction
# ---------------------------------------------------------------------------

def is_generic_value(value: str) -> bool:
    """Return True if value should be discarded regardless of context."""
    v = value.strip()
    if not v:
        return True

    # Explicit blocklist (case-insensitive)
    if v.upper() in PLACEHOLDER_VALUES:
        return True

    # KiCad power symbols (GND*, VCC*, VDD*…)
    if POWER_SYMBOL_RE.match(v):
        return True

    # Generic passive values (10k, 100nF, 1µH…)
    if PASSIVE_VALUE_RE.match(v):
        return True

    # Frequency values (16MHz, 32.768kHz…)
    if FREQUENCY_RE.match(v):
        return True

    # Template variables: ${SIM.PARAMS}
    if "${" in v:
        return True

    # Power/voltage net names starting with + or -
    if v.startswith("+") or v.startswith("-"):
        return True

    # Values with whitespace are net names or notes
    if re.search(r"\s", v):
        return True

    # "A or B" multi-option notes fields
    if re.search(r"\bor\b", v, re.IGNORECASE):
        return True

    # Parentheses → symbol/placeholder names
    if "(" in v or ")" in v:
        return True

    # @ → spec strings ("100 @ 100MHz", "1kΩ@100MHz")
    if "@" in v:
        return True

    # SPICE simulation source components
    if "Simulation_SPICE" in v:
        return True

    # Generic family placeholders ending in xx/XX or embedded: HT75xx-1-SOT23
    if v.lower().endswith("xx"):
        return True
    if re.search(r'[A-Z0-9]xx[^x]', v, re.IGNORECASE):
        return True

    # Values ending with bare dash or underscore (symbol names like BAT-, U_)
    if v.endswith("-") or v.endswith("_"):
        return True

    # Placeholder pin-count names: PIC_18_PINS, PIC_8_PINS
    if re.search(r"_\d+_PINS?$", v, re.IGNORECASE):
        return True

    # Short all-caps/underscore strings (< 4 chars) — GND, PWR, VCC etc.
    if re.match(r"^[A-Z_]+$", v) and len(v) < 4:
        return True

    # Values containing only digits, slashes, and percent signs
    if re.match(r"^[\d/%]+$", v):
        return True

    # Net label suffixes: SIGNAL_EN, NODE_PWR, etc.
    if NET_LABEL_SUFFIX_RE.search(v):
        return True

    # Passive / vendor-code patterns compiled above
    for pat in _PASSIVE_DISCARD_RES:
        if pat.match(v):
            return True

    return False


# Prefixes that look like KEEP matches but are actually non-IC parts.
# These override KEEP_PREFIXES (checked first in is_genuine_ic).
_KEEP_OVERRIDE_DISCARD: frozenset[str] = frozenset([
    "LTST",   # Lite-On LEDs — match "LT" ADI prefix but are not ICs
    "ADT2",   # Mini-Circuits RF transformer — not an ADI part (ADT = ADI temp sensors)
])


def is_genuine_ic(value: str) -> bool:
    """
    Return True if value looks like a real IC that needs a datasheet.
    Applied to both Value-field fallbacks and explicit MPN properties.
    """
    upper = value.strip().upper()
    if any(upper.startswith(d) for d in _KEEP_OVERRIDE_DISCARD):
        return False
    return any(upper.startswith(p.upper()) for p in KEEP_PREFIXES)


def normalize_mpn(mpn: str) -> str | None:
    """
    Clean up an MPN before storing and URL resolution.
    Returns None if the MPN is a family placeholder that should be discarded.

    FIX 6 (STM32 wildcards) and general LCSC/suffix stripping.
    """
    # Strip KiCad/Olimex internal suffix: -lz1ddd / _lz1ddd
    mpn = re.sub(r'[-_]lz1ddd$', '', mpn, flags=re.IGNORECASE)

    # Strip LCSC appended part number: TPS2117DRLR_C22399676
    mpn = re.sub(r'_C\d{5,}$', '', mpn)

    # Strip embedded manufacturer prefix: MaxLinear_XR22417-48 → XR22417-48
    mpn = re.sub(r'^[A-Z][A-Za-z]{3,}_(?=[A-Z0-9])', '', mpn)

    # Strip embedded package codes from schematic symbol names
    # e.g. LM317_TO-220, LM7805_TO220, LT1129_QPACK, NE555_DIP8
    mpn = re.sub(r'[_\s]TO-?\d+[A-Z]*$', '', mpn, flags=re.IGNORECASE)
    mpn = re.sub(r'[_\s](?:QPACK|DPAK|D2T|D2PAK)$', '', mpn, flags=re.IGNORECASE)
    mpn = re.sub(r'[_\s](?:DIP|SOIC|SOP|SSOP|TSSOP|QFP|TQFP|LQFP|QFN|DFN|BGA|CSP)-?\d*[A-Z]*$', '', mpn, flags=re.IGNORECASE)

    if not mpn:
        return None

    # STM32-specific normalization
    if mpn.upper().startswith('STM32'):
        # Discard range placeholders: STM32C011J_4-6_Mx
        if re.search(r'_\d+-\d+_', mpn):
            return None
        # Discard wildcard families: STM32F4x1CxUx (lowercase x between digits)
        if re.search(r'\d[a-wx]\d', mpn):  # a-w plus x (family placeholder)
            return None
        # Strip trailing lowercase x chars: STM32F411CCUx → STM32F411CCU
        mpn = re.sub(r'x+$', '', mpn, flags=re.IGNORECASE)
        if not mpn:
            return None

    return mpn


def refdes_prefix(refdes: str) -> str:
    """Extract the alpha prefix from a reference designator (e.g. 'R12' → 'R')."""
    m = re.match(r'^([A-Za-z_]+)', refdes) if refdes else None
    return m.group(1).upper() if m else ""


def extract_mpns_from_schematic(sch_path: Path) -> list[dict]:
    """
    Extract component MPNs from a KiCad s-expression schematic.
    Returns list of dicts: {refdes, value, mpn, source_file}.
    """
    try:
        text = sch_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    # Split into component blocks by finding (symbol ...) at top level.
    # We use a simple approach: find each (property "Reference" ...) and
    # (property "Value" ...) / (property "MPN" ...) near each other.
    # Since properties appear in sequence within a symbol block, we parse
    # the file linearly, tracking the current component's reference.

    results = []
    current_ref = None
    current_values: list[tuple[str, str]] = []  # (prop_name, prop_value)

    # Tokenize property lines
    for line in text.splitlines():
        ref_m = REFERENCE_RE.search(line)
        if ref_m:
            # Flush previous component if we had one
            if current_ref is not None:
                _flush_component(current_ref, current_values, sch_path, results)
            current_ref = ref_m.group(1)
            current_values = []
            continue

        prop_m = PROPERTY_RE.search(line)
        if prop_m and current_ref is not None:
            current_values.append((prop_m.group(1), prop_m.group(2).strip()))

    # Flush last component
    if current_ref is not None:
        _flush_component(current_ref, current_values, sch_path, results)

    return results


def _flush_component(
    ref: str,
    props: list[tuple[str, str]],
    sch_path: Path,
    out: list[dict],
) -> None:
    prefix = refdes_prefix(ref)
    is_passive = prefix in PASSIVE_REFDES_PREFIXES

    mpn_value = None
    fallback_value = None

    for prop_name, prop_value in props:
        if prop_name in ("MPN", "Part Number", "Manufacturer Part Number"):
            # Explicit MPN property: apply both discard rules and allowlist.
            # (Most explicit MPN fields in this corpus are connector/passive
            # part numbers, not ICs — the allowlist filters them out.)
            if not is_generic_value(prop_value) and is_genuine_ic(prop_value):
                mpn_value = prop_value
        elif prop_name == "Value":
            # Value field fallback: apply discard rules AND require allowlist match
            if (
                not is_generic_value(prop_value)
                and not is_passive
                and is_genuine_ic(prop_value)
            ):
                fallback_value = prop_value

    chosen = mpn_value or fallback_value
    if chosen:
        # FIX 6: normalize (strip lz1ddd, LCSC suffix, STM32 wildcards)
        normalized = normalize_mpn(chosen)
        if normalized:
            out.append({
                "refdes": ref,
                "mpn": normalized,
                "source_file": str(sch_path),
            })


def collect_all_mpns(
    corpus_dir: Path,
    manifest_path: Path,
) -> tuple[dict[str, dict], int]:
    """
    Returns:
      mpn_data: {canonical_mpn: {schematics: [str], count: int}}
      total_schematics: int
    """
    # Collect schematic paths from manifest + glob
    sch_paths: set[Path] = set()

    if manifest_path.exists():
        with open(manifest_path) as f:
            manifest = json.load(f)
        for entry in manifest.get("files", []):
            if entry.get("format") == "kicad_sch" and not entry.get("is_export"):
                p = corpus_dir / entry["path"]
                if p.exists():
                    sch_paths.add(p)

    for p in corpus_dir.rglob("*.kicad_sch"):
        sch_paths.add(p)

    total_schematics = len(sch_paths)

    # Extract MPNs
    # mpn_upper → list of (canonical_spelling, source_file)
    all_entries: list[dict] = []
    for sch_path in sorted(sch_paths):
        all_entries.extend(extract_mpns_from_schematic(sch_path))

    # Deduplicate: pick most common capitalisation per upper-cased MPN
    spelling_counter: dict[str, Counter] = defaultdict(Counter)
    schematics_map: dict[str, set] = defaultdict(set)

    for entry in all_entries:
        upper = entry["mpn"].upper()
        spelling_counter[upper][entry["mpn"]] += 1
        rel = str(Path(entry["source_file"]).relative_to(corpus_dir))
        schematics_map[upper].add(rel)

    mpn_data: dict[str, dict] = {}
    for upper, counter in spelling_counter.items():
        canonical = counter.most_common(1)[0][0]
        mpn_data[canonical] = {
            "schematics": sorted(schematics_map[upper]),
            "count": sum(counter.values()),
        }

    return mpn_data, total_schematics


# ---------------------------------------------------------------------------
# Destination path construction
# ---------------------------------------------------------------------------

def dest_pdf_path(mpn: str, manufacturer: str | None, datasheets_dir: Path) -> Path:
    mfr_dir = manufacturer if manufacturer else "other"
    safe_name = re.sub(r'[^\w\-.]', '_', mpn.lower())
    return datasheets_dir / mfr_dir / f"{safe_name}.pdf"


# ---------------------------------------------------------------------------
# LCSC search API fallback
# ---------------------------------------------------------------------------

def _find_pdf_url_in_obj(obj: object, depth: int = 0) -> str | None:
    """Recursively search a JSON object for a pdfUrl string."""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        val = obj.get("pdfUrl") or obj.get("pdf_url") or obj.get("dataManualUrl")
        if isinstance(val, str) and val.lower().endswith(".pdf"):
            return val
        for v in obj.values():
            r = _find_pdf_url_in_obj(v, depth + 1)
            if r:
                return r
    elif isinstance(obj, list):
        for item in obj:
            r = _find_pdf_url_in_obj(item, depth + 1)
            if r:
                return r
    return None


def get_lcsc_datasheet_url(mpn: str, session: requests.Session) -> str | None:
    """Query LCSC search API for a PDF datasheet URL. Returns None if not found."""
    try:
        resp = rate_limited_request(
            "get",
            f"https://wmsc.lcsc.com/ftps/wme/search/global?keyword={mpn}&currentPage=1&pageSize=1",
            1.0,
            session,
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return _find_pdf_url_in_obj(data)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Download datasheets for all component MPNs in the KiCad corpus."
    )
    p.add_argument(
        "--corpus-dir",
        type=Path,
        default=Path("./netlist_corpus"),
        help="Root of the netlist corpus (default: ./netlist_corpus)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print MPNs and resolved URLs; do not download anything.",
    )
    p.add_argument(
        "--mpns",
        type=str,
        default="",
        help="Comma-separated list of specific MPNs to process.",
    )
    p.add_argument(
        "--skip-nexar",
        action="store_true",
        help="Skip the Nexar API fallback even if credentials are set.",
    )
    p.add_argument(
        "--delay-multiplier",
        type=float,
        default=1.0,
        help="Multiply all per-domain delays by this factor.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    corpus_dir: Path = args.corpus_dir.resolve()
    manifest_path = corpus_dir / "manifest.json"

    if not manifest_path.exists():
        print(
            f"ERROR: {manifest_path} does not exist.\n"
            "Run download_netlist_corpus.py first to populate the corpus.",
            file=sys.stderr,
        )
        return 1

    datasheets_dir = corpus_dir / "datasheets"
    log_path = datasheets_dir / "download_log.json"
    map_path = datasheets_dir / "datasheet_map.json"
    failed_path = datasheets_dir / "failed_parts.txt"

    # Nexar credentials
    nexar_client_id = os.environ.get("NEXAR_CLIENT_ID", "")
    nexar_client_secret = os.environ.get("NEXAR_CLIENT_SECRET", "")
    use_nexar = (
        not args.skip_nexar
        and bool(nexar_client_id)
        and bool(nexar_client_secret)
    )

    # ---------------------------------------------------------------------------
    # Phase 1: MPN extraction
    # ---------------------------------------------------------------------------
    print("Scanning schematics for component MPNs…")
    mpn_data, total_schematics = collect_all_mpns(corpus_dir, manifest_path)

    # Apply --mpns filter
    if args.mpns:
        requested = {m.strip().upper() for m in args.mpns.split(",") if m.strip()}
        mpn_data = {
            mpn: v for mpn, v in mpn_data.items()
            if mpn.upper() in requested
        }

    all_mpns = sorted(mpn_data.keys())
    print(f"Found {len(all_mpns)} unique MPNs across {total_schematics} schematics.")

    if not use_nexar and not args.skip_nexar:
        print(
            "NEXAR credentials not set — skipping API fallback. "
            "Set NEXAR_CLIENT_ID and NEXAR_CLIENT_SECRET environment variables to enable."
        )

    # ---------------------------------------------------------------------------
    # Phase 2: URL resolution + dry-run output
    # ---------------------------------------------------------------------------
    if args.dry_run:
        print("\n--- DRY RUN: MPN list and resolved URLs ---\n")
        for mpn in all_mpns:
            mfr = detect_manufacturer(mpn)
            urls = candidate_urls(mpn, mfr)
            url_str = urls[0] if urls else "(no direct URL — Nexar fallback)"
            print(f"  {mpn:40s}  [{mfr or 'unknown':15s}]  {url_str}")
        print(f"\nTotal: {len(all_mpns)} MPNs. No downloads performed.")
        return 0

    # ---------------------------------------------------------------------------
    # Phase 3+: Download
    # ---------------------------------------------------------------------------
    datasheets_dir.mkdir(parents=True, exist_ok=True)

    download_log = load_download_log(log_path)
    session = requests.Session()
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (compatible; schecker-datasheet-bot/1.0; "
        "+https://github.com/user/schecker)"
    )

    stats = {"downloaded": 0, "failed": 0, "skipped": 0, "nexar": 0, "lcsc": 0, "direct": 0}
    failures: list[tuple[str, str]] = []  # (mpn, reason)

    total = len(all_mpns)

    for idx, mpn in enumerate(all_mpns, start=1):
        prefix = f"[{idx}/{total}] {mpn}"

        # Resume: skip already-successful parts
        if download_log.get(mpn, {}).get("status") == "success":
            print(f"{prefix} → already downloaded, skipping")
            stats["skipped"] += 1
            continue

        mfr = detect_manufacturer(mpn)
        delay = get_delay(mfr) * args.delay_multiplier
        dest = dest_pdf_path(mpn, mfr, datasheets_dir)

        # Try direct manufacturer URLs (skip for bot-blocked parts; go straight to Nexar)
        chosen_url: str | None = None
        resolution_method = "direct_url"

        if mpn.upper() not in FORCE_NEXAR:
            for url in candidate_urls(mpn, mfr):
                if validate_url(url, delay, session):
                    chosen_url = url
                    break

        # Nexar fallback
        if chosen_url is None and use_nexar:
            print(f"{prefix} → trying Nexar…", end=" ", flush=True)
            try:
                chosen_url = get_nexar_datasheet_url(
                    mpn, nexar_client_id, nexar_client_secret
                )
            except Exception as exc:
                print(f"Nexar error: {exc}")
                chosen_url = None
            if chosen_url:
                resolution_method = "nexar"
                mfr = mfr or "other"
                dest = dest_pdf_path(mpn, mfr, datasheets_dir)

        # LCSC search API fallback
        if chosen_url is None:
            lcsc_url = get_lcsc_datasheet_url(mpn, session)
            if lcsc_url:
                chosen_url = lcsc_url
                resolution_method = "lcsc"
                mfr = mfr or "other"
                dest = dest_pdf_path(mpn, mfr, datasheets_dir)

        if chosen_url is None:
            reason = "no URL pattern + no Nexar result" if use_nexar else "no direct URL pattern"
            print(f"{prefix} → FAILED ({reason})")
            download_log[mpn] = {"status": "failed", "reason": reason}
            save_download_log(log_path, download_log)
            stats["failed"] += 1
            failures.append((mpn, reason))
            continue

        # Download
        domain = urlparse(chosen_url).netloc
        print(f"{prefix} → {domain}… ", end="", flush=True)
        ok = download_with_backoff(chosen_url, dest, delay, session)

        if ok:
            size_mb = dest.stat().st_size / 1_048_576
            print(f"✓ saved ({size_mb:.1f} MB)")
            download_log[mpn] = {
                "status": "success",
                "local_path": str(dest.relative_to(corpus_dir)),
                "url": chosen_url,
                "resolution_method": resolution_method,
                "manufacturer": mfr or "other",
            }
            stats["downloaded"] += 1
            if resolution_method == "nexar":
                stats["nexar"] += 1
            elif resolution_method == "lcsc":
                stats["lcsc"] += 1
            else:
                stats["direct"] += 1
        else:
            reason = f"download failed from {chosen_url}"
            print(f"✗ failed")
            download_log[mpn] = {"status": "failed", "reason": reason, "url": chosen_url}
            stats["failed"] += 1
            failures.append((mpn, reason))

        save_download_log(log_path, download_log)

    # ---------------------------------------------------------------------------
    # Write datasheet_map.json — always reflects ALL log entries, not just
    # the current run's subset, so --mpns partial runs don't overwrite prior work.
    # ---------------------------------------------------------------------------
    parts_map: dict[str, dict] = {}
    for log_mpn, entry in download_log.items():
        if entry.get("status") == "success":
            parts_map[log_mpn] = {
                "manufacturer": entry.get("manufacturer", "other"),
                "local_path": entry.get("local_path", ""),
                "source_url": entry.get("url", ""),
                "resolution_method": entry.get("resolution_method", "unknown"),
                "file_size_bytes": (
                    (corpus_dir / entry["local_path"]).stat().st_size
                    if entry.get("local_path") and (corpus_dir / entry["local_path"]).exists()
                    else 0
                ),
                "schematics_using_this_part": mpn_data.get(log_mpn, {}).get("schematics", []),
            }

    total_downloaded = stats["downloaded"] + stats["skipped"]
    datasheet_map = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_mpns_found": len(all_mpns),
        "total_downloaded": total_downloaded,
        "total_failed": stats["failed"],
        "parts": parts_map,
    }
    with open(map_path, "w") as f:
        json.dump(datasheet_map, f, indent=2)

    # Write failed_parts.txt
    with open(failed_path, "w") as f:
        for mpn, reason in failures:
            f.write(f"{mpn}: {reason}\n")
        # Also include parts that previously failed (from log) and weren't retried
        for mpn, entry in download_log.items():
            if entry.get("status") == "failed" and mpn not in dict(failures):
                f.write(f"{mpn}: {entry.get('reason', 'unknown')}\n")

    # ---------------------------------------------------------------------------
    # Update manifest.json datasheets section
    # ---------------------------------------------------------------------------
    if manifest_path.exists():
        with open(manifest_path) as f:
            manifest = json.load(f)
        manifest["datasheets"] = {
            "total_mpns": len(all_mpns),
            "downloaded": total_downloaded,
            "failed": stats["failed"],
            "coverage_percent": (
                round(total_downloaded / len(all_mpns) * 100) if all_mpns else 0
            ),
            "datasheet_map": "datasheets/datasheet_map.json",
            "failed_parts": "datasheets/failed_parts.txt",
        }
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)

    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------
    print("\n" + "=" * 60)
    print(f"MPNs found:        {len(all_mpns)}")
    print(f"Downloaded (new):  {stats['downloaded']}  "
          f"(direct: {stats['direct']}, nexar: {stats['nexar']}, lcsc: {stats['lcsc']})")
    print(f"Already cached:    {stats['skipped']}")
    print(f"Failed:            {stats['failed']}")
    if failures:
        print(f"\nFailed parts written to: {failed_path}")
    print(f"Datasheet map:     {map_path}")
    print("=" * 60)

    return 0 if stats["failed"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())

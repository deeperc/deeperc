"""
Peripheral knowledge base (KB) data model and runtime loader.

Phase 1.1.b: KB-anchored I2C peripheral mismatch detection.

Data model classes are defined here and imported by both the checker module
and the test suite. The JSON KB files (kb/vendor/*/*.json) are parsed by
the offline tools in tools/build_kb.py.

NOTE: peripheral_roles.py (Phase 1.1.a) defines Peripheral and Role with
different value names (Role.I2C_DATA vs Signal.I2C_SDA here). These types
are independent and will be reconciled when the checker implementation lands.

Signal.GPIO is used as a catch-all for peripheral signals not represented
in the Signal enum (ADC inputs, UART RTS, etc.). This preserves the
non-fixed-function nature of multi-function pins: a pin with [I2C, SPI]
roles will have both Peripheral.I2C and Peripheral.SPI entries, so it
won't look fixed-function for I2C.

SPI signals (MOSI/MISO/SCK/NSS, TODO-410) ARE tracked, mirroring the I2C
SDA/SCL granularity — see Signal.SPI_MOSI et al. below.
"""
import json
import os
from dataclasses import dataclass
from enum import Enum


class Signal(Enum):
    I2C_SDA  = "I2C_SDA"
    I2C_SCL  = "I2C_SCL"
    UART_TX  = "UART_TX"
    UART_RX  = "UART_RX"
    UART_CTS = "UART_CTS"   # PA0/USART2_CTS
    TIM_CH   = "TIM_CH"     # TIM4_CH1 / TIM4_CH2 share one value (v1 granularity)
    SPI_MOSI = "SPI_MOSI"   # TODO-410
    SPI_MISO = "SPI_MISO"   # TODO-410
    SPI_SCK  = "SPI_SCK"    # TODO-410
    SPI_NSS  = "SPI_NSS"    # TODO-410 (not in SPI_COHERENCE_GROUP — D3 ruling)
    GPIO     = "GPIO"        # also used as catch-all for unmapped signals


class KBSource(Enum):
    VENDOR_XML    = "VENDOR_XML"     # STM32CubeMX XML
    VENDOR_HEADER = "VENDOR_HEADER"  # ESP-IDF IO_MUX header
    MANUAL        = "MANUAL"         # hand-curated entry


class Peripheral(Enum):
    I2C     = "I2C"
    UART    = "UART"
    SPI     = "SPI"
    TIM     = "TIM"
    GPIO    = "GPIO"
    UNKNOWN = "UNKNOWN"   # ADC, CAN, USB, ETH, and other unmapped peripherals


class PeripheralRouting(Enum):
    FIXED   = "fixed"    # IO_MUX-style: specific pins for specific peripherals
    MATRIX  = "matrix"   # GPIO-matrix: any pin can serve any role (e.g. ESP32 I2C)
    UNKNOWN = "unknown"  # no routing information available


@dataclass
class PinRole:
    peripheral: Peripheral
    instance:   str | None   # "I2C1", "I2C2", None for fixed-function sensors
    signal:     Signal
    source:     KBSource


@dataclass
class StrapInfo:
    """Marks a KB pin as a deliberately, correctly NON-PARTICIPATING strap/config
    pin on the bus it is wired to (Todo 240 — e.g. the INA219 A0/A1 I2C-address-
    select pins: high-impedance inputs strapped directly onto SDA/SCL to encode
    16 addresses from 2 pins).

    Consumed at exactly ONE decision, everywhere: ``strap is not None``. Its
    presence EXEMPTS the pin from the two "zero protocol roles on this bus →
    condemn" gates — step_08d Step 4 (PROTOCOL_MISMATCH) and peripheral_consensus
    M14 (``incapable_candidate`` → CAPABILITY_MISMATCH). A strap-marked pin
    NEVER contributes to any CONFIRM/corroboration path (it carries no bus role,
    so it is structurally never a voter); the exemption only silences a false
    condemnation, it never asserts identity.

    This is NOT a pin_id blocklist — the exemption lives on the KB pin entry,
    datasheet-cited (``datasheet_ref``), and is human-gated at KB-authoring time.
    """
    datasheet_ref: str        # REQUIRED, non-empty (validated at load time)
    # RESERVED for Todo 214 (MCU boot-strap tie-state adjudication). Its
    # vocabulary is UNDEFINED in this build. NO code path may read, branch on,
    # or validate its contents beyond accepting None — it is carried verbatim
    # and consumed by nothing. Do not add a consumer here without a new todo.
    legal_states: object | None = None


@dataclass
class PinFunctionEntry:
    mpn:    str
    pin_id: str
    roles:  list             # list[PinRole]
    # Additive, optional (Todo 240). None for every existing KB entry and every
    # existing consumer. When present (StrapInfo), marks a deliberately
    # non-participating strap pin — see StrapInfo above. Consumed only as
    # ``strap is not None`` at the two condemnation gates.
    strap:  object | None = None    # StrapInfo | None


# ── Loader ────────────────────────────────────────────────────────────────────

# JSON string → Peripheral enum. Lowercase keys match the JSON output format.
_PERIPHERAL_MAP: dict[str, Peripheral] = {
    "i2c":   Peripheral.I2C,
    "uart":  Peripheral.UART,
    "usart": Peripheral.UART,  # CubeMX uses "usart"; normalize to UART
    "spi":   Peripheral.SPI,
    "tim":   Peripheral.TIM,
    "gpio":  Peripheral.GPIO,
    # "adc", "dac", "can", "usb", "eth", "rcc", "rtc", "sys", "other", "unknown"
    # → Peripheral.UNKNOWN (handled by the default below)
}

# JSON string → Signal enum. Lowercase keys; None/missing → Signal.GPIO catch-all.
_SIGNAL_MAP: dict[str, Signal] = {
    "sda":      Signal.I2C_SDA,
    "i2c_sda":  Signal.I2C_SDA,
    "scl":      Signal.I2C_SCL,
    "i2c_scl":  Signal.I2C_SCL,
    "tx":       Signal.UART_TX,
    "uart_tx":  Signal.UART_TX,
    "rx":       Signal.UART_RX,
    "uart_rx":  Signal.UART_RX,
    "cts":      Signal.UART_CTS,
    "uart_cts": Signal.UART_CTS,
    "ch":       Signal.TIM_CH,
    "tim_ch":   Signal.TIM_CH,
    "mosi":     Signal.SPI_MOSI,
    "spi_mosi": Signal.SPI_MOSI,
    "miso":     Signal.SPI_MISO,
    "spi_miso": Signal.SPI_MISO,
    "sck":      Signal.SPI_SCK,
    "sclk":     Signal.SPI_SCK,
    "spi_sck":  Signal.SPI_SCK,
    "nss":      Signal.SPI_NSS,
    "cs":       Signal.SPI_NSS,
    "spi_nss":  Signal.SPI_NSS,
    "gpio":     Signal.GPIO,
    # rts, in0, etc. → Signal.GPIO via default
}

_KB_SOURCE_MAP: dict[str, KBSource] = {
    "vendor_xml":    KBSource.VENDOR_XML,
    "vendor_header": KBSource.VENDOR_HEADER,
    "manual":        KBSource.MANUAL,
}


def _role_from_dict(d: dict, default_source: KBSource) -> PinRole:
    peripheral_str = (d.get("peripheral") or "unknown").lower()
    signal_str = (d.get("signal") or "gpio").lower()
    instance = d.get("instance")
    source_str = (d.get("source") or "").lower()
    return PinRole(
        peripheral=_PERIPHERAL_MAP.get(peripheral_str, Peripheral.UNKNOWN),
        instance=instance,
        signal=_SIGNAL_MAP.get(signal_str, Signal.GPIO),
        source=_KB_SOURCE_MAP.get(source_str, default_source),
    )


def _strap_from_dict(d: dict | None):
    """Parse an optional ``"strap"`` pin-dict key into a StrapInfo, or None.

    An ABSENT key (``d is None``) returns None — byte-identical to pre-Todo-240
    behavior for every existing KB entry. When present, ``datasheet_ref`` MUST be
    a non-empty string (a strap exemption is only ever authored with a datasheet
    citation). ``legal_states`` is RESERVED (Todo 214): carried through verbatim,
    never read, validated, or branched on here — only its presence-vs-None is
    meaningful to any current code path, and no current path reads even that.
    """
    if d is None:
        return None
    ref = d.get("datasheet_ref")
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError(
            "strap entry requires a non-empty string 'datasheet_ref' "
            f"(got {ref!r})"
        )
    return StrapInfo(datasheet_ref=ref, legal_states=d.get("legal_states"))


def _entry_from_json(mcu_part: str, pin_dict: dict, default_source: KBSource) -> PinFunctionEntry:
    pin_id = pin_dict["pin_id"]
    roles = [_role_from_dict(r, default_source) for r in pin_dict.get("possible_roles", [])]
    strap = _strap_from_dict(pin_dict.get("strap"))
    return PinFunctionEntry(mpn=mcu_part, pin_id=pin_id, roles=roles, strap=strap)


_ROUTING_MAP: dict[str, PeripheralRouting] = {
    "fixed":   PeripheralRouting.FIXED,
    "matrix":  PeripheralRouting.MATRIX,
    "unknown": PeripheralRouting.UNKNOWN,
}


def load_kb_routing(directory: str) -> dict[str, dict]:
    """Read ``peripheral_routing`` fields from KB JSON files.

    Returns ``{mcu_part: {Peripheral: PeripheralRouting}}``.  Files without a
    ``peripheral_routing`` key are silently skipped; unrecognised routing values
    default to ``PeripheralRouting.UNKNOWN``.
    """
    routing: dict[str, dict] = {}
    for root, _dirs, files in os.walk(directory):
        for fname in files:
            if not fname.endswith(".json"):
                continue
            path = os.path.join(root, fname)
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            pr = data.get("peripheral_routing")
            if not pr or "mcu_part" not in data:
                continue
            mcu_part = data["mcu_part"]
            per_routing: dict = {}
            for periph_str, routing_str in pr.items():
                periph = _PERIPHERAL_MAP.get(periph_str.lower(), Peripheral.UNKNOWN)
                per_routing[periph] = _ROUTING_MAP.get(
                    str(routing_str).lower(), PeripheralRouting.UNKNOWN
                )
            routing[mcu_part] = per_routing
    return routing


def load_peripheral_kb(kb_root_dir: str) -> tuple[dict, dict]:
    """Load both pin function entries and routing metadata in one call.

    Returns:
        (kb, peripheral_routing) — both indexed appropriately for
        check_i2c_peripheral(). Pass both to the checker together.
    """
    return load_kb(kb_root_dir), load_kb_routing(kb_root_dir)


def load_kb(directory: str) -> dict[tuple[str, str], PinFunctionEntry]:
    """Load all KB JSON files from kb/vendor/ subdirectories.

    Walks ``directory`` recursively, loads every ``*.json`` file, and
    returns a dict keyed by ``(mcu_part, pin_id)``.

    JSON files must have the top-level structure:
        { "mcu_part": str, "kb_source": str, "pins": [ { "pin_id": str,
          "possible_roles": [ {...}, ... ] }, ... ] }

    Raises ValueError on malformed JSON (missing required keys).
    """
    kb: dict[tuple[str, str], PinFunctionEntry] = {}
    for root, _dirs, files in os.walk(directory):
        for fname in files:
            if not fname.endswith(".json"):
                continue
            path = os.path.join(root, fname)
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if "mcu_part" not in data:
                raise ValueError(f"{path}: missing required key 'mcu_part'")
            if "pins" not in data:
                raise ValueError(f"{path}: missing required key 'pins'")
            mcu_part = data["mcu_part"]
            source_str = (data.get("kb_source") or "manual").lower()
            default_source = _KB_SOURCE_MAP.get(source_str, KBSource.MANUAL)
            for pin_dict in data["pins"]:
                if "pin_id" not in pin_dict:
                    raise ValueError(f"{path}: pin entry missing 'pin_id'")
                entry = _entry_from_json(mcu_part, pin_dict, default_source)
                kb[(mcu_part, entry.pin_id)] = entry
    return kb

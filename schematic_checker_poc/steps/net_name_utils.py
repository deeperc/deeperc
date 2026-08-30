"""
Shared net name normalization for cross-checker comparisons.

KiCad net names can carry prefixes that should not affect matching:
  - Leading /  — hierarchical sheet path (e.g. /+5V_USB, /GND)
  - Leading +  — positive rail polarity marker (e.g. +3.3V, +5V)
  - Leading -  — negative rail polarity marker (e.g. -12V)

All three checkers (step_08, step_08b, step_08c) use this function when
looking up net names against confirmed_voltages or ground_nets.
"""


def normalize_net_name(name: str) -> str:
    """
    Strip leading /, +, - prefixes and whitespace, then uppercase.

    Examples:
      "/+5V_USB" → "5V_USB"
      "+3.3V"    → "3.3V"
      "/GND"     → "GND"
      "VCC_3V3"  → "VCC_3V3"
      "/-12V"    → "12V"
      "+/3V3"    → "3V3"
    """
    if not name:
        return ""
    cleaned = name.strip()
    while cleaned and cleaned[0] in "/+-":
        cleaned = cleaned[1:]
    return cleaned.upper()

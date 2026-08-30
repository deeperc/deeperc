import logging
import re
from llm.ollama_client import generate_json

logger = logging.getLogger(__name__)

# ── Whole-part group extraction (Option E) ────────────────────────────────────

PART_EXTRACTION_PROMPT = """
You are extracting electrical specifications from a hardware datasheet.
Component: {part_number}

Extract all distinct pin electrical specifications from the DC characteristics
and absolute maximum ratings tables. Pins that share identical electrical specs
should be grouped together.

CRITICAL — numeric fields must be floating-point numbers in Volts:
- VIH_min, VIL_max, VOH_min, VOL_max, absolute_max_voltage
  MUST be numbers like 2.0 or 2.31 or 4.6 — NOT table names or section names.
- VIH_source and abs_max_source ARE the table/section names (strings).
- If a value is given as a formula (e.g. "0.7 × VCC"), compute it using the
  supply_max you found (e.g. 0.7 × 3.3 = 2.31). Write 2.31 in VIH_min, not the formula.
- Use null only when the value is truly absent from the text.

Other rules:
- VIH_min and absolute_max_voltage come from DIFFERENT tables and will almost
  always be different values. VIH_min ≈ 0.7 × VCC; absolute_max_voltage is
  the hard damage limit, typically higher (e.g. 4.0 V or 5.5 V).
- For split-supply devices (VCCA/VCCB), create separate groups for each port.
- Power and ground pins should be their own group with pin_type "power".
- If all signal pins share the same spec (common for logic gates/transceivers),
  one group with applies_to "all signal pins" is sufficient.

For SUPPLY voltage extraction (critical — only for "power" pin_type groups):
- Look in "Recommended Operating Conditions" or "Operating Conditions" table
  for the nominal supply range (e.g. VCC = 2.0V to 3.6V).
- Look in "Absolute Maximum Ratings" for the supply damage threshold
  (e.g. VCC max = 4.6V).
- For split-supply devices (VCCA/VCCB), give separate power groups per supply rail.
- supply_min: minimum recommended operating voltage (e.g. 2.0)
- supply_max: maximum recommended operating voltage (e.g. 3.6)
- supply_abs_max: absolute max supply voltage from abs max ratings (e.g. 4.6)
- supply_rail_name: which rail this group represents (e.g. "VCC", "VCCA",
  "VCCB", "AVDD", "DVDD") — must match the datasheet pin name
For non-power groups, leave all four supply_* fields as null.

Reply ONLY with a JSON object. No explanation, no markdown fences.
{{
  "pin_groups": [
    {{
      "group_name": "descriptive name e.g. standard_io or port_a_inputs",
      "pin_type": "bidirectional | digital_input | digital_output | power | analog",
      "example_pins": ["A1", "A2", "B1"],
      "applies_to": "brief description of which pins this covers",
      "VIH_min": null,
      "VIL_max": null,
      "VOH_min": null,
      "VOL_max": null,
      "absolute_max_voltage": null,
      "supply_min": null,
      "supply_max": null,
      "supply_abs_max": null,
      "supply_rail_name": null,
      "VIH_source": "table/section name where VIH was found",
      "abs_max_source": "table/section name where absolute_max was found"
    }}
  ],
  "default_group": "group_name to use when pin not found in any group"
}}

Datasheet section text:
{section_text}
"""


def _build_section_text(sections: dict, markdown_text: str, max_chars: int = 8000) -> str:
    """Concatenates abs_max + elec_chars + pin_table excerpts up to max_chars."""
    parts = []
    remaining = max_chars
    for key in ("abs_max", "elec_chars", "pin_table"):
        if remaining <= 0:
            break
        heading = sections.get(key)
        text = _extract_section_text(markdown_text, heading)
        if text:
            chunk = text[:remaining]
            parts.append(chunk)
            remaining -= len(chunk)
    return "\n\n".join(parts) if parts else markdown_text[:max_chars]


def extract_part_pin_groups(
    part_number: str,
    sections: dict,
    markdown_text: str,
) -> dict | None:
    """
    Extracts all pin group specs for a part in one LLM call.
    Returns the parsed JSON dict or None if extraction fails.
    """
    section_text = _build_section_text(sections, markdown_text, max_chars=8000)
    prompt = PART_EXTRACTION_PROMPT.format(
        part_number=part_number,
        section_text=section_text,
    )
    result = generate_json(prompt, retries=1, step_hint="04b_extractor")
    if result is None:
        logger.warning(f"[STEP 04b] Pin group extraction failed for {part_number}")
        return None

    groups = result.get("pin_groups", [])
    logger.info(f"[STEP 04b] Extracted {len(groups)} pin group(s) for {part_number}")
    for g in groups:
        logger.info(
            f"  Group '{g.get('group_name')}': "
            f"VIH_min={g.get('VIH_min')}, "
            f"abs_max={g.get('absolute_max_voltage')}, "
            f"covers: {g.get('applies_to')}"
        )
    return result


def resolve_pin_spec_from_groups(
    pin_id: str,
    pin_name: str,
    pin_groups_result: dict | None,
) -> dict | None:
    if not pin_groups_result:
        return None

    groups = pin_groups_result.get("pin_groups", [])
    default_group_name = pin_groups_result.get("default_group")

    # Priority 1: exact match on pin_id or pin_name in example_pins
    for group in groups:
        examples = [str(p).upper() for p in group.get("example_pins", [])]
        if pin_id.upper() in examples or pin_name.upper() in examples:
            return group

    # Priority 2: default_group — but ONLY if it is not a power group
    if default_group_name:
        for group in groups:
            if (group.get("group_name") == default_group_name
                    and group.get("pin_type") != "power"):
                return group

    # Priority 3: first signal group — never return a power group as fallback
    SIGNAL_TYPES = {"bidirectional", "digital_input", "digital_output", "analog"}
    signal_groups = [g for g in groups
                     if g.get("pin_type", "").lower() in SIGNAL_TYPES]
    if signal_groups:
        return signal_groups[0]

    # Priority 4: absolute last resort — any non-power group
    non_power = [g for g in groups if g.get("pin_type") != "power"]
    if non_power:
        return non_power[0]

    return None


_OPEN_DRAIN_RE = re.compile(r"open[\s_-]?drain", re.IGNORECASE)


def _group_is_open_drain(group: dict | None) -> bool:
    """True when the group's metadata documents it as open-drain (e.g. an
    AP22615 FLG fault flag). An open-drain output sinks only — it never sources
    its VCC onto the net — so it must not be trusted as a push-pull driver that
    asserts an overvoltage."""
    if not group:
        return False
    text = f"{group.get('group_name', '')} {group.get('applies_to', '')}"
    return bool(_OPEN_DRAIN_RE.search(text))


def pin_match_meta(pin_id: str, pin_name: str,
                   pin_groups_result: dict | None) -> tuple[bool, bool]:
    """Returns (pin_type_exact, open_drain) for the group this pin resolves to.

    - pin_type_exact: the pin POSITIVELY matched a group's `example_pins` (True),
      vs. fell through to the default/first-signal/last-resort fallback (False).
      A pin whose "output" type came from a fallback (e.g. a ULN2003 base input
      landing on the `default_group` that happens to be the collector-output
      group) is NOT a trustworthy push-pull driver.
    - open_drain: the resolved group is documented open-drain.

    step_08 uses both to gate whether an output-typed driver pin may assert an
    OVERVOLTAGE (only a genuine, positively-matched, non-open-drain push-pull
    output can). Trap-proof for real drivers: 74HC595 QA positively matches its
    digital-output group, so its 5V overdrive is preserved.
    """
    if not pin_groups_result:
        return False, False
    for group in pin_groups_result.get("pin_groups", []):
        examples = [str(p).upper() for p in group.get("example_pins", [])]
        if str(pin_id).upper() in examples or str(pin_name).upper() in examples:
            return True, _group_is_open_drain(group)
    # No exact match — resolves via fallback; report open-drain of the fallback
    # group for completeness, but pin_type_exact is False.
    grp = resolve_pin_spec_from_groups(pin_id, pin_name, pin_groups_result)
    return False, _group_is_open_drain(grp)

SECTION_CHARS = 6000
FALLBACK_CHARS = 6000

_TRAILING_UNIT_RE = re.compile(r"[^\d.+\-eE]+$")

EXTRACTION_PROMPT = """
You are extracting electrical specifications from a hardware datasheet.
Component: {part_number}
Pin: {pin_name} (pin number {pin_id})
This pin may be a digital input, output, or bidirectional signal pin.

This component may be a bidirectional buffer, bus transceiver, or logic gate.
If so, it may have split supply rails (e.g. VCCA and VCCB) and bidirectional I/O pins.

Extract the following values for this pin. Look in BOTH the DC characteristics
table AND the absolute maximum ratings table — they are different tables with
different values.

Values to extract:
1. VIH_min — minimum input voltage recognised as logic HIGH (V)
   Look for: "VIH", "Input HIGH voltage", "High-level input voltage"
2. VIL_max — maximum input voltage recognised as logic LOW (V)
   Look for: "VIL", "Input LOW voltage", "Low-level input voltage"
3. VOH_min — minimum output voltage for logic HIGH (V)
   Look for: "VOH", "Output HIGH voltage", "High-level output voltage"
4. VOL_max — maximum output voltage for logic LOW (V)
   Look for: "VOL", "Output LOW voltage", "Low-level output voltage"
5. absolute_max_voltage — maximum voltage that may EVER be applied to this pin
   without permanent damage (V). Found in "Absolute Maximum Ratings" table only.
   Look for: "VIN", "Input voltage", "Voltage at any pin", "VI"
   NOT the same as VIH. This is usually higher (e.g. 4.6V or 5.5V for 3.3V logic).
6. supply_voltage — the VCC/VCCA/VCCB supply voltage this pin is referenced to (V)
   If split-supply device, use the supply rail for this specific pin's port.
7. pin_type — one of: digital_input, digital_output, bidirectional, power, analog

IMPORTANT rules:
- VIH_min and absolute_max_voltage are DIFFERENT values from DIFFERENT tables.
  If they are the same value, you have read from only one table — check again.
- For bidirectional pins, report I/O specs (both input and output thresholds).
- If the datasheet shows a range like "VIH min = 0.7 x VCC", compute the value
  using the supply_voltage you found. Example: 0.7 x 3.3 = 2.31V.
- For split-supply devices, use the supply voltage for the port this pin belongs to.
- If a value is truly not found, use null.

Reply ONLY with a JSON object. No explanation, no markdown fences.
{{
  "pin_name": "{pin_name}",
  "pin_id": "{pin_id}",
  "VIH_min": null,
  "VIL_max": null,
  "VOH_min": null,
  "VOL_max": null,
  "absolute_max_voltage": null,
  "supply_voltage": null,
  "pin_type": null,
  "VIH_source": "which table/section VIH came from",
  "abs_max_source": "which table/section absolute_max came from",
  "source_snippet": "exact text from datasheet, max 120 chars"
}}

Datasheet section text:
{section_text}
"""


_TOC_LINE_RE = re.compile(r'[\.\s]+\d+\s*$')


def _extract_section_text(markdown_text: str, heading: str | None) -> str:
    if heading is None:
        return markdown_text[:FALLBACK_CHARS]
    # Skip TOC-style occurrences: lines ending with dotleaders + page number (e.g. ". . 37")
    start = 0
    while True:
        idx = markdown_text.find(heading, start)
        if idx == -1:
            break
        eol = markdown_text.find('\n', idx)
        line = markdown_text[idx:eol] if eol != -1 else markdown_text[idx:]
        if not _TOC_LINE_RE.search(line):
            return markdown_text[idx : idx + SECTION_CHARS]
        start = idx + 1
    # No non-TOC occurrence found — fall back to first occurrence
    idx = markdown_text.find(heading)
    if idx == -1:
        return markdown_text[:FALLBACK_CHARS]
    return markdown_text[idx : idx + SECTION_CHARS]


def _call_gemma(
    part_number: str,
    section_text: str,
    pin_name: str,
    pin_id: str,
    pin_type_hint: str = "",   # kept for API compat; not injected into prompt
) -> dict | None:
    prompt = EXTRACTION_PROMPT.format(
        part_number=part_number,
        pin_name=pin_name,
        pin_id=pin_id,
        section_text=section_text,
    )
    return generate_json(prompt, retries=1)


def _merge(results: list[dict]) -> dict:
    merged = {
        "VIH_min": None,
        "VIH_max": None,   # kept for backward compat; populated only by old prompts
        "VIL_max": None,
        "VOH_min": None,
        "VOL_max": None,
        "absolute_max_voltage": None,
        "supply_voltage": None,
        "pin_type": None,
        "VIH_source": None,
        "abs_max_source": None,
        "source_snippet": None,
    }
    for r in results:
        if r is None:
            continue
        for key in merged:
            if merged[key] is None and r.get(key) is not None:
                merged[key] = r[key]
    return merged


def extract_pin_specs(
    part_number: str,
    pin_name: str,
    pin_id: str,
    markdown_text: str,
    section_locations: dict,
) -> dict:
    sections = [
        ("abs_max", section_locations.get("abs_max")),
        ("elec_chars", section_locations.get("elec_chars")),
        ("pin_table", section_locations.get("pin_table")),
    ]

    raw_results = []
    for section_key, heading in sections:
        section_text = _extract_section_text(markdown_text, heading)
        result = _call_gemma(part_number, section_text, pin_name, pin_id)
        if result:
            logger.info(f"[STEP 04b] {section_key} extraction for {pin_name}: {result}")
        raw_results.append(result)

    merged = _merge(raw_results)
    merged["pin_name"] = pin_name
    merged["pin_id"] = pin_id

    # Coerce numeric fields; strip trailing unit text (e.g. "3.6 V" → 3.6)
    for field in ("VIH_min", "VIH_max", "VIL_max", "VOH_min", "VOL_max", "absolute_max_voltage", "supply_voltage"):
        val = merged.get(field)
        if val is not None:
            try:
                merged[field] = float(val)
            except (TypeError, ValueError):
                cleaned = _TRAILING_UNIT_RE.sub("", str(val)).strip()
                try:
                    merged[field] = float(cleaned)
                except (TypeError, ValueError):
                    logger.warning(f"[STEP 04b] Could not convert {field}={val!r} to float, setting null")
                    merged[field] = None

    return merged

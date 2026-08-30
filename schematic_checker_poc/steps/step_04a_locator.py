import logging
import re
from llm.ollama_client import generate_json

logger = logging.getLogger(__name__)

EXCERPT_CHARS = 3000

# Matches lines that are part of a TOC: content followed by dotleaders + page number,
# or content followed by a large whitespace gap + number.
_TOC_LINE_RE = re.compile(
    r'^.*?\.{2,}\s*\d+\s*$'    # "Section heading ..... 12"
    r'|^.*?\s{3,}\d+\s*$',     # "Section heading      12"
    re.MULTILINE,
)


def _strip_toc(markdown_text: str) -> str:
    """Remove TOC blocks from MinerU markdown. A TOC block is ≥3 consecutive TOC-style lines."""
    lines = markdown_text.split('\n')
    result: list[str] = []
    run = 0          # consecutive TOC-line count
    pending: list[str] = []   # buffered lines in current TOC run

    for line in lines:
        if _TOC_LINE_RE.match(line.strip()):
            run += 1
            pending.append(line)
        else:
            if run < 3:
                result.extend(pending)   # short run — not a TOC block, keep it
            # else: drop the pending TOC block
            pending = []
            run = 0
            result.append(line)

    if run < 3:
        result.extend(pending)
    return '\n'.join(result)


def locate_sections(part_number: str, markdown_text: str) -> dict:
    excerpt = _strip_toc(markdown_text)[:EXCERPT_CHARS]
    prompt = (
        f"You are a hardware datasheet parser assistant.\n\n"
        f"Below is the start of a datasheet for {part_number}, converted to\n"
        f"markdown. Your task is to identify which section headings correspond to:\n"
        f"1. The pin description / pin listing table\n"
        f"2. The absolute maximum ratings table\n"
        f"3. The DC electrical characteristics table (VIL, VIH, VOL, VOH, etc.)\n\n"
        f"Reply ONLY with a JSON object. No explanation, no markdown fences.\n"
        f"Format:\n"
        f'{{\n'
        f'  "pin_table": "exact section heading or null",\n'
        f'  "abs_max": "exact section heading or null",\n'
        f'  "elec_chars": "exact section heading or null"\n'
        f"}}\n\n"
        f"Datasheet excerpt:\n{excerpt}"
    )

    result = generate_json(prompt, retries=1, step_hint="04a_locator")
    if result is None:
        logger.warning(f"[STEP 04a] Failed to parse section locations for {part_number}, using full-text fallback")
        return {"pin_table": None, "abs_max": None, "elec_chars": None}

    logger.info(f"[STEP 04a] Located sections for {part_number}: {result}")
    return {
        "pin_table": result.get("pin_table"),
        "abs_max": result.get("abs_max"),
        "elec_chars": result.get("elec_chars"),
    }

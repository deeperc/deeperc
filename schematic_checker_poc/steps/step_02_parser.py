from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PinIR:
    pin_id: str
    pin_name: str
    net: str


@dataclass
class ComponentIR:
    refdes: str
    part_number: str
    value: str
    pins: list[PinIR]
    # Provenance captured from the (comp ...) entry — additive, used only by the
    # eligibility gate to recognize non-IC pseudo-parts (test points, solder
    # jumpers, connectors) by where they came from rather than by their value
    # string. Default "" keeps existing constructors/consumers unaffected.
    footprint: str = ""
    libsource_part: str = ""
    # L2 vendor-lookup placeholder-expansion result (private-tier resolver's
    # placeholder_expansion["concrete"]), threaded back from the resolver.
    # None for every literal MPN and for any placeholder step_03 didn't
    # resolve -- effective_mpn's fallback then makes this field a no-op by
    # construction (kb_identity_gap/REPORT.md Step 3).
    resolved_mpn: str | None = None

    @property
    def mpn(self) -> str:
        return self.part_number

    @property
    def effective_mpn(self) -> str:
        return self.resolved_mpn or self.part_number


@dataclass
class NetIR:
    name: str
    pins: list[tuple[str, str]]


@dataclass
class NetlistIR:
    source_file: str
    components: list[ComponentIR]
    nets: list[NetIR]
    power_nets: list[str] = field(default_factory=list)
    ground_nets: list[str] = field(default_factory=list)
    # (refdes, pin_id) -> KiCad (pintype ...) ETYPE string (e.g. "output",
    # "bidirectional", "open_collector"). Parallel to `nets`/`pins` — additive,
    # populated from the same (nets (net (node ...))) section as pinfunction,
    # never threaded into NetIR.pins or PinIR (keeps their tuple/field shape
    # unchanged for every existing consumer). Empty dict for netlists parsed
    # before this field existed or hand-crafted fixtures without the field.
    pintypes: dict[tuple[str, str], str] = field(default_factory=dict)


# ── S-expression tokenizer ────────────────────────────────────────────────────

def tokenize(text: str) -> list[str]:
    """Splits s-expression text into tokens: '(', ')', and quoted/unquoted atoms."""
    tokens: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in '()':
            tokens.append(c)
            i += 1
        elif c == '"':
            j = i + 1
            while j < n:
                if text[j] == '\\':
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            tokens.append(text[i:j + 1])
            i = j + 1
        elif c in ' \t\n\r':
            i += 1
        else:
            j = i
            while j < n and text[j] not in ' \t\n\r()':
                j += 1
            tokens.append(text[i:j])
            i = j
    return tokens


def parse_sexp(tokens: list[str], pos: int = 0) -> tuple:
    """Recursively parses tokenized s-expression.
    Returns (parsed_structure, next_position).
    Atoms are strings; lists are Python lists.
    """
    if pos >= len(tokens):
        raise ValueError("Unexpected end of tokens")
    if tokens[pos] == '(':
        pos += 1
        result = []
        while pos < len(tokens) and tokens[pos] != ')':
            item, pos = parse_sexp(tokens, pos)
            result.append(item)
        if pos >= len(tokens):
            raise ValueError("Unmatched '(' — missing closing ')'")
        pos += 1  # consume ')'
        return result, pos
    else:
        atom = tokens[pos]
        if atom.startswith('"') and atom.endswith('"'):
            atom = atom[1:-1]
        return atom, pos + 1


# ── Tree helpers ──────────────────────────────────────────────────────────────

def _find(tree: list, key: str) -> list | None:
    """Find first child list whose first element is key."""
    for item in tree:
        if isinstance(item, list) and item and item[0] == key:
            return item
    return None


def _find_all(tree: list, key: str) -> list[list]:
    """Find all child lists whose first element is key."""
    return [item for item in tree if isinstance(item, list) and item and item[0] == key]


def _atom(node: list, key: str) -> str | None:
    """Get the string value of a child like (key "value")."""
    child = _find(node, key)
    if child and len(child) >= 2:
        return str(child[1])
    return None


# ── Component extraction ──────────────────────────────────────────────────────

def _extract_components(tree: list) -> list[ComponentIR]:
    components_node = _find(tree, 'components')
    if not components_node:
        raise ValueError("No (components ...) section found")

    components: list[ComponentIR] = []
    for comp in _find_all(components_node, 'comp'):
        refdes = _atom(comp, 'ref') or ''
        value  = _atom(comp, 'value') or ''

        # MPN: prefer (field (name "MPN") "...") inside (fields ...)
        part_number = value
        fields_node = _find(comp, 'fields')
        if fields_node:
            for f in _find_all(fields_node, 'field'):
                fname = _atom(f, 'name') or ''
                if fname.upper() in ('MPN', 'PART_NUMBER', 'PARTNUMBER'):
                    if len(f) >= 3:
                        part_number = str(f[2])
                    break

        # Pins from (pins (pin (num "1") (name "PA0") ...))
        pins: list[PinIR] = []
        pins_node = _find(comp, 'pins')
        if pins_node:
            for pin in _find_all(pins_node, 'pin'):
                pin_id   = _atom(pin, 'num') or _atom(pin, 'number') or ''
                pin_name = _atom(pin, 'name') or ''
                pins.append(PinIR(pin_id=pin_id, pin_name=pin_name, net=''))

        # Provenance (additive): footprint + libsource part. KiCad exports carry
        # (footprint "Lib:Fp") and (libsource (lib "...") (part "...")). Used only
        # by the eligibility gate's pseudo-part recognizer.
        footprint = _atom(comp, 'footprint') or ''
        libsource_node = _find(comp, 'libsource')
        libsource_part = _atom(libsource_node, 'part') or '' if libsource_node else ''

        components.append(ComponentIR(
            refdes=refdes,
            part_number=part_number,
            value=value,
            pins=pins,
            footprint=footprint,
            libsource_part=libsource_part,
        ))

    return components


# ── Net extraction ────────────────────────────────────────────────────────────

def _extract_nets(tree: list) -> list[NetIR]:
    nets_node = _find(tree, 'nets')
    if not nets_node:
        raise ValueError("No (nets ...) section found")

    nets: list[NetIR] = []
    for net in _find_all(nets_node, 'net'):
        # Works for both formats:
        # Format A: (net (name "VCC") (node (ref "U1") (pin "1")))
        # Format B: (net (code "1") (name "VCC") (node (ref "U1") (pin "1")))
        name     = _atom(net, 'name') or ''
        pin_refs: list[tuple[str, str]] = []
        for node in _find_all(net, 'node'):
            ref = _atom(node, 'ref') or ''
            pin = _atom(node, 'pin') or ''
            if ref and pin:
                pin_refs.append((ref, pin))
        nets.append(NetIR(name=name, pins=pin_refs))

    return nets


def _extract_pinfunctions(tree: list) -> dict[tuple[str, str], str]:
    """Build (refdes, pin_id) → pinfunction mapping from the nets section.

    KiCad modern exports include (pinfunction "VCC_14") on every net node.
    We use this to recover logical pin names (e.g. "VCC") when the components
    section omits them.
    """
    pinfunctions: dict[tuple[str, str], str] = {}
    nets_node = _find(tree, 'nets')
    if not nets_node:
        return pinfunctions
    for net in _find_all(nets_node, 'net'):
        for node in _find_all(net, 'node'):
            ref = _atom(node, 'ref') or ''
            pin = _atom(node, 'pin') or ''
            pf  = _atom(node, 'pinfunction') or ''
            if ref and pin and pf:
                pinfunctions[(ref, pin)] = pf
    return pinfunctions


def _extract_pintypes(tree: list) -> dict[tuple[str, str], str]:
    """Build (refdes, pin_id) → pintype mapping from the nets section.

    KiCad exports include (pintype "output"|"input"|"bidirectional"|"tri_state"|
    "open_collector"|"open_emitter"|"passive"|"power_in"|"power_out"|
    "unspecified"|"no_connect"|"free" ...) on every net node — the ETYPE from
    the symbol's pin definition. Mirrors `_extract_pinfunctions`.
    """
    pintypes: dict[tuple[str, str], str] = {}
    nets_node = _find(tree, 'nets')
    if not nets_node:
        return pintypes
    for net in _find_all(nets_node, 'net'):
        for node in _find_all(net, 'node'):
            ref = _atom(node, 'ref') or ''
            pin = _atom(node, 'pin') or ''
            pt  = _atom(node, 'pintype') or ''
            if ref and pin and pt:
                pintypes[(ref, pin)] = pt
    return pintypes


def _logical_pin_name(pinfunction: str, pin_id: str) -> str:
    """Derive logical pin name from KiCad pinfunction.

    "VCC_14"           → "VCC"
    "UVCC_2"           → "UVCC"
    "(ADC7/TDI)PF7_36" → "PF7"
    "GND_15"           → "GND"
    """
    if not pinfunction:
        return pin_id
    name = re.sub(r'_\d+$', '', pinfunction)        # strip trailing _<digits>
    name = re.sub(r'^\([^)]+\)', '', name).strip()   # strip leading (note)
    return name if name else pin_id


def _backfill_pin_nets(
    components: list[ComponentIR],
    nets: list[NetIR],
    pinfunctions: dict[tuple[str, str], str] | None = None,
) -> None:
    """
    1. Fills in PinIR.net for each pin by looking up net membership.
    2. For components with no pins parsed from the (components ...) section
       (real KiCad exports omit pin listings there), synthesizes PinIR entries
       from the (nets ...) section.  pin_name is derived from the pinfunction
       field when available (e.g. "VCC_14" → "VCC"); falls back to pin_id.
    """
    # (refdes, pin_id) → net_name, built from nets section
    net_lookup: dict[tuple[str, str], str] = {}
    for net in nets:
        for refdes, pin_id in net.pins:
            net_lookup[(refdes, pin_id)] = net.name

    # refdes → [(pin_id, net_name)] for synthesis
    pins_from_nets: dict[str, list[tuple[str, str]]] = {}
    for (refdes, pin_id), net_name in net_lookup.items():
        pins_from_nets.setdefault(refdes, []).append((pin_id, net_name))

    pf = pinfunctions or {}

    for comp in components:
        if comp.pins:
            # Pins were parsed from (components ...) section — just fill in nets
            for pin in comp.pins:
                pin.net = net_lookup.get((comp.refdes, pin.pin_id), '')
        else:
            # No pins in components section (real KiCad export format) —
            # synthesize from nets section, deriving pin_name from pinfunction
            synthesized = pins_from_nets.get(comp.refdes, [])
            comp.pins = [
                PinIR(
                    pin_id=pin_id,
                    pin_name=_logical_pin_name(
                        pf.get((comp.refdes, pin_id), ''), pin_id
                    ),
                    net=net_name,
                )
                for pin_id, net_name in sorted(synthesized, key=lambda x: x[0])
            ]


# ── Main entry point ──────────────────────────────────────────────────────────

def parse_netlist(netlist_path: str | Path) -> NetlistIR:
    """Parses a KiCad .net export in either hand-crafted or real export format."""
    path   = Path(netlist_path)
    suffix = path.suffix.lower()

    if suffix in ('.kicad_sch', '.sch'):
        raise ValueError(
            f"File is a KiCad schematic source ({suffix}), not a netlist export. "
            f"Export to .net first using: "
            f"kicad-cli sch export netlist --format kicadsexpr <file>"
        )

    text = path.read_text(encoding='utf-8', errors='replace')
    if not text.strip():
        raise ValueError(f"Empty file: {netlist_path}")

    tokens = tokenize(text)
    if not tokens:
        raise ValueError(f"No tokens found in: {netlist_path}")

    try:
        tree, _ = parse_sexp(tokens, 0)
    except (ValueError, IndexError) as e:
        raise ValueError(f"Failed to parse s-expression in {netlist_path}: {e}") from e

    if not isinstance(tree, list) or not tree or tree[0] != 'export':
        top = tree[0] if isinstance(tree, list) and tree else str(tree)
        raise ValueError(
            f"Not a KiCad netlist export — top-level element is "
            f"'{top}', expected 'export'"
        )

    try:
        components = _extract_components(tree)
    except ValueError as e:
        raise ValueError(f"Component extraction failed in {netlist_path}: {e}") from e

    try:
        nets = _extract_nets(tree)
    except ValueError as e:
        raise ValueError(f"Net extraction failed in {netlist_path}: {e}") from e

    if not components:
        raise ValueError(f"No components found in {netlist_path}")

    pinfunctions = _extract_pinfunctions(tree)
    _backfill_pin_nets(components, nets, pinfunctions)
    pintypes = _extract_pintypes(tree)

    return NetlistIR(
        source_file=str(netlist_path),
        components=components,
        nets=nets,
        power_nets=[],
        ground_nets=[],
        pintypes=pintypes,
    )


# Backwards-compatible alias — existing callers use parse()
parse = parse_netlist

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import pin_match


@dataclass
class PinIR:
    pin_id: str
    pin_name: str
    net: str
    # Libparts (symbol) pin name for this pin_id, raw ("" when the netlist has no
    # libparts entry for the component's libsource, or the symbol pin is
    # unnamed). Read by the spec-lookup matcher's names(pin) (TODO-488); never
    # replaces pin_name.
    symbol_name: str = ""


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
    libsource_lib: str = ""
    # (libparts (libpart (lib ...) (part ...) (description ...))) text for this
    # component's (libsource_lib, libsource_part); "" when there is no entry or no
    # description (TODO-554; read by part_class.classify_part).
    libsource_description: str = ""
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
class MergedRefdes:
    """TODO-569: a reference designator the netlist merges — two or more (comp)
    records share it, or one of its (ref, pin) nodes sits on two or more (net)
    entries. Detected and disclosed, never separated."""
    records: int                       # (comp) record count; 0 = ref only in net nodes
    sheet_paths: list[str]             # (sheetpath (names ..)) per record, file order
    part_numbers: list[str]            # sorted distinct part_number of its records
    collided_pins: dict[str, list[str]]  # pin_id -> net names (net-entry order), >=2 entries
    test: str                          # "records" | "pins" | "both"


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
    # Components whose (libsource_lib, libsource_part) has no (libparts ...)
    # entry, so their pins carry symbol_name "" (TODO-488).
    libparts_unmatched: int = 0
    # TODO-569: refdes -> MergedRefdes for every MERGED refdes, sorted by ref;
    # empty when the netlist has none.
    merged_refdes: dict[str, MergedRefdes] = field(default_factory=dict)


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

def _read_libpart_descriptions(tree: list) -> dict[tuple[str, str], str]:
    """(libparts (libpart (lib L) (part P) (description D) ...)) -> {(L, P): D}.
    The first libpart entry per (L, P) wins; a missing description reads "".
    (TODO-554)"""
    out: dict[tuple[str, str], str] = {}
    libparts_node = _find(tree, 'libparts')
    if not libparts_node:
        return out
    for lp in _find_all(libparts_node, 'libpart'):
        key = (_atom(lp, 'lib') or '', _atom(lp, 'part') or '')
        if key in out:
            continue
        out[key] = _atom(lp, 'description') or ''
    return out


def _extract_components(
    tree: list, sheet_paths_out: list[str] | None = None,
) -> list[ComponentIR]:
    """sheet_paths_out, when given, is extended with each record's
    (sheetpath (names "..")) in record order ("" when absent) — read only by
    the TODO-569 merged-refdes detector."""
    components_node = _find(tree, 'components')
    if not components_node:
        raise ValueError("No (components ...) section found")

    descriptions = _read_libpart_descriptions(tree)
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
        libsource_lib = _atom(libsource_node, 'lib') or '' if libsource_node else ''

        if sheet_paths_out is not None:
            sheetpath_node = _find(comp, 'sheetpath')
            sheet_paths_out.append(
                (_atom(sheetpath_node, 'names') or '') if sheetpath_node else '')

        components.append(ComponentIR(
            refdes=refdes,
            part_number=part_number,
            value=value,
            pins=pins,
            footprint=footprint,
            libsource_part=libsource_part,
            libsource_lib=libsource_lib,
            libsource_description=descriptions.get((libsource_lib, libsource_part), ''),
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


def _detect_merged_refdes(
    components: list[ComponentIR],
    sheet_paths: list[str],
    nets: list[NetIR],
) -> tuple[list[ComponentIR], dict[str, MergedRefdes]]:
    """TODO-569 (decisions 1, 4, 11). A refdes is MERGED when it has two or more
    (comp) records, OR any (ref, pin) node of it is a node on two or more (net)
    entries (counted by entry position, not by name; a node repeated inside one
    entry does not count). The empty ref is skipped. For a MERGED ref with two or
    more records only the FIRST record (file order) is kept. Returns the kept
    component list (the input list itself when nothing is dropped) and the
    merged map, sorted by ref."""
    records_by_ref: dict[str, list[int]] = {}
    for i, c in enumerate(components):
        records_by_ref.setdefault(c.refdes, []).append(i)

    node_entries: dict[tuple[str, str], list[int]] = {}
    for idx, net in enumerate(nets):
        for ref, pin in net.pins:
            entries = node_entries.setdefault((ref, pin), [])
            if idx not in entries:
                entries.append(idx)
    collided: dict[str, dict[str, list[str]]] = {}
    for (ref, pin), entries in node_entries.items():
        if len(entries) >= 2:
            collided.setdefault(ref, {})[pin] = [nets[i].name for i in sorted(entries)]

    refs = set(records_by_ref) | {ref for ref, _ in node_entries}
    merged: dict[str, MergedRefdes] = {}
    for ref in sorted(refs):
        if not ref:
            continue
        idxs = records_by_ref.get(ref, [])
        by_records = len(idxs) >= 2
        by_pins = ref in collided
        if not (by_records or by_pins):
            continue
        merged[ref] = MergedRefdes(
            records=len(idxs),
            sheet_paths=[sheet_paths[i] if i < len(sheet_paths) else '' for i in idxs],
            part_numbers=sorted({components[i].part_number for i in idxs}),
            collided_pins={p: collided[ref][p] for p in sorted(collided.get(ref, {}))},
            test="both" if by_records and by_pins else ("records" if by_records else "pins"),
        )

    drop = {i for ref, m in merged.items() if m.records >= 2
            for i in records_by_ref[ref][1:]}
    if not drop:
        return components, merged
    return [c for i, c in enumerate(components) if i not in drop], merged


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


def _fill_symbol_names(tree: list, components: list[ComponentIR]) -> int:
    """Sets each pin's symbol_name from the netlist's (libparts ...) section via
    the component's (libsource_lib, libsource_part) and the pin_id. Returns the
    number of components with no libparts entry (their pins keep "")."""
    libparts = pin_match.read_libpart_pins(tree)
    unmatched = 0
    for comp in components:
        names = libparts.get((comp.libsource_lib, comp.libsource_part))
        if names is None:
            unmatched += 1
            continue
        for pin in comp.pins:
            pin.symbol_name = names.get(pin.pin_id, "")
    return unmatched


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

    sheet_paths: list[str] = []
    try:
        components = _extract_components(tree, sheet_paths)
    except ValueError as e:
        raise ValueError(f"Component extraction failed in {netlist_path}: {e}") from e

    try:
        nets = _extract_nets(tree)
    except ValueError as e:
        raise ValueError(f"Net extraction failed in {netlist_path}: {e}") from e

    if not components:
        raise ValueError(f"No components found in {netlist_path}")

    # TODO-569: detect MERGED refdes and keep one record each, before backfill.
    components, merged_refdes = _detect_merged_refdes(components, sheet_paths, nets)

    pinfunctions = _extract_pinfunctions(tree)
    _backfill_pin_nets(components, nets, pinfunctions)
    libparts_unmatched = _fill_symbol_names(tree, components)
    pintypes = _extract_pintypes(tree)

    return NetlistIR(
        source_file=str(netlist_path),
        components=components,
        nets=nets,
        power_nets=[],
        ground_nets=[],
        pintypes=pintypes,
        libparts_unmatched=libparts_unmatched,
        merged_refdes=merged_refdes,
    )


# Backwards-compatible alias — existing callers use parse()
parse = parse_netlist

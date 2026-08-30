"""
TODO-134 — user-supplied confirmed rail map (the V2 "engine" tier).

A declarative sidecar lets a user *declare* rails the deterministic recognizer will
never infer (the topology `Net-(…)` and ambiguous `Spare1`/`/ESP_EN` cases the
TODO-133 widen deliberately left as WARNs). The map is injected as the TOP tier of
`step_06_power.infer_power_nets` (above Tier-1), so it flows through the existing
`confirmed_voltages` / `power_nets` / `ground_nets` plumbing unchanged.

This module only LOADS, VALIDATES and key-MATCHES the map. Injection, precedence and
conflict-surfacing live in step_06_power (next to the inference it shadows).

Schema (`<netlist>.rails.json` sidecar, or --rail-map <path>):
    { "<net-name-or-basename>": { "voltage": <float|null>,
                                  "is_rail":  <bool, default true>,
                                  "is_ground": <bool, default false> } }

Design: investigation/confirmed_rail_map_design.md
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from steps.net_name_utils import normalize_net_name


class RailMapError(ValueError):
    """Malformed rail map — a hard error (a user relying on a typo'd map must hear it)."""


def _basename(norm: str) -> str:
    """Last '/'-segment of an already-normalized name, leading +/- stripped.
    Mirrors the TODO-133 step_08c per-segment reduction so a user key like
    `/CAN bus/CAN12V_prot` or the bare basename `CAN12V_prot` both resolve."""
    seg = norm.rsplit("/", 1)[-1]
    return seg.lstrip("+-")


def _norm_key(key: str) -> str:
    return normalize_net_name(key)


def sidecar_path(netlist_path: str) -> Path:
    """`<netlist>.rails.json` next to the netlist."""
    return Path(str(netlist_path) + ".rails.json")


def load_rail_map(netlist_path: str | None,
                  explicit_path: str | None = None) -> dict | None:
    """Discover + parse + validate a rail map.

    `--rail-map <path>` (explicit_path) wins over the auto-discovered sidecar.
    Returns the validated raw map ({raw_key: decl}) or None when no map exists.
    Raises RailMapError on a malformed/missing-explicit map.
    """
    path: Path | None = None
    if explicit_path:
        path = Path(explicit_path)
        if not path.is_file():
            raise RailMapError(f"--rail-map path not found: {explicit_path}")
    elif netlist_path:
        sc = sidecar_path(netlist_path)
        if sc.is_file():
            path = sc
    if path is None:
        return None

    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise RailMapError(f"rail map {path}: not readable/parseable JSON: {e}") from e

    return _validate(raw, path)


def _validate(raw, path) -> dict:
    if not isinstance(raw, dict):
        raise RailMapError(f"rail map {path}: top level must be an object of net→decl")
    out: dict = {}
    for net, decl in raw.items():
        if not isinstance(net, str) or not net.strip():
            raise RailMapError(f"rail map {path}: net key must be a non-empty string, got {net!r}")
        if not isinstance(decl, dict):
            raise RailMapError(f"rail map {path}: '{net}' value must be an object, got {decl!r}")
        v = decl.get("voltage", None)
        if v is not None and not isinstance(v, (int, float)) or isinstance(v, bool):
            raise RailMapError(f"rail map {path}: '{net}'.voltage must be a number or null, got {v!r}")
        is_rail = decl.get("is_rail", True)
        is_ground = decl.get("is_ground", False)
        if not isinstance(is_rail, bool):
            raise RailMapError(f"rail map {path}: '{net}'.is_rail must be a bool, got {is_rail!r}")
        if not isinstance(is_ground, bool):
            raise RailMapError(f"rail map {path}: '{net}'.is_ground must be a bool, got {is_ground!r}")
        out[net] = {
            "voltage": float(v) if v is not None else None,
            "is_rail": is_rail,
            "is_ground": is_ground,
        }
    return out


def build_index(rail_map: dict) -> dict:
    """Two lookup indices: by normalized full name and by normalized basename.
    Each maps to (decl, original_key) so unmatched keys can be reported."""
    by_norm: dict[str, tuple[dict, str]] = {}
    by_base: dict[str, tuple[dict, str]] = {}
    for key, decl in rail_map.items():
        nk = _norm_key(key)
        by_norm[nk] = (decl, key)
        by_base.setdefault(_basename(nk), (decl, key))
    return {"by_norm": by_norm, "by_base": by_base}


def lookup(index: dict, net_name: str) -> tuple[dict | None, str | None]:
    """Resolve a netlist net name against the map. Full-normalized match wins;
    basename match is the fallback. Returns (decl, matched_key) or (None, None)."""
    if not net_name:
        return None, None
    nn = normalize_net_name(net_name)
    hit = index["by_norm"].get(nn)
    if hit is None:
        hit = index["by_base"].get(_basename(nn))
    if hit is None:
        return None, None
    decl, key = hit
    return decl, key

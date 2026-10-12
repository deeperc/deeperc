"""TODO-531 BUILD-B: majority materialisation and the majority cache writer (steps/extractors.py).

Synthetic draws only: no PDF datasheet (a few fake bytes stand in for the hashed PDF), no network, no key.
Each draw is shaped like extraction_validator.validate output (citations, no quotes).
"""
import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from steps import extractors  # noqa: E402
from steps import step_03_resolver as r3  # noqa: E402

PINS = [{"pin_id": "1", "pin_name": "IN"}]
PK = "1|IN"
# P1 of TODO-531 BUILD-B: the sync regime's fingerprint at 3f9c14e. A change here is a regime change and
# re-keys every cache; this PR must not move it.
SYNC_FINGERPRINT_SHA = "09d71ab37867a6a4e47d848f6325cc6686d034fec752a6737acdcb03006fe1c3"


def cit(quote, match="verbatim", page=1):
    return {"quote": quote, "page": page, "section": None, "match": match}


def grp(name="g", pins=("1|IN",), ptype="digital_input", cits=None, **fields):
    g = {"group_name": name, "pin_type": ptype, "example_pins": list(pins), "applies_to": "inputs"}
    for f in extractors.MAJORITY_FIELDS:
        g[f] = None
    g.update(fields)
    g["citations"] = dict(cits or {})
    for f in extractors.MAJORITY_FIELDS:      # every populated numeric is cited, as the validator does
        if g.get(f) is not None and f not in g["citations"]:
            g["citations"][f] = cit(f"{name} {f} {g[f]}")
    return g


def draw(*groups, default=None):
    return {"pin_groups": list(groups), "default_group": default}


def vih(*vals, **kw):
    """{rep: draw} with one group whose VIH_min is vals[rep-1]."""
    return {r: draw(grp(VIH_min=v, **kw)) for r, v in enumerate(vals, 1)}


def mat(draws, pins=PINS):
    return extractors.materialise_majority(draws, pins=pins)


def g0(cache):
    return cache["pin_groups"][0]


# ── the vote ─────────────────────────────────────────────────────────────────
def test_two_of_three_wins():
    cache, rep = mat(vih(2.0, 2.0, 2.5))
    assert g0(cache)["VIH_min"] == 2.0
    assert rep["base_replicate"] == 1 and rep["counts"]["DISAGREE"] == 0


def test_null_is_a_value():
    cache, rep = mat(vih(2.0, None, None))
    assert g0(cache)["VIH_min"] is None
    assert rep["counts"]["DISAGREE"] == 0 and rep["base_replicate"] == 2


def test_null_majority_over_a_base_value_is_nulled_with_status():
    # 2 / 2 / 2 agreements over VIH_min, VOL_max, VOH_min: draw 1 is the base and is outvoted on VIH_min
    d = {1: draw(grp(VIH_min=2.0, VOL_max=0.4, VOH_min=2.4)),
         2: draw(grp(VIH_min=None, VOL_max=0.5, VOH_min=2.4)),
         3: draw(grp(VIH_min=None, VOL_max=0.4, VOH_min=2.6))}
    cache, rep = mat(d)
    assert rep["base_replicate"] == 1
    g = g0(cache)
    assert g["VIH_min"] is None and g["VOL_max"] == 0.4
    assert g["citations"]["VIH_min"]["status"] == "nulled:majority"
    assert g["citations"]["VIH_min"]["rejected_value"] == 2.0
    assert rep["counts"]["nulled_majority"] == 1


def test_all_distinct_is_none_and_disagree():
    cache, rep = mat(vih(1.0, 2.0, 3.0))
    g = g0(cache)
    assert g["VIH_min"] is None
    assert rep["counts"]["DISAGREE"] == 1 and rep["counts"]["nulled_disagree"] == 1
    assert rep["disagree"] == [{"pin": PK, "field": "VIH_min", "values": {"1": 1.0, "2": 2.0, "3": 3.0}}]
    assert g["citations"]["VIH_min"]["status"] == "nulled:disagree"
    assert g["citations"]["VIH_min"]["rejected_value"] == 1.0
    assert g["citations"]["VIH_min"]["quote"] == "g VIH_min 1.0"


def test_two_draws_must_agree():
    agree, rep = mat({1: draw(grp(VIH_min=2.0)), 3: draw(grp(VIH_min=2.0))})
    assert g0(agree)["VIH_min"] == 2.0 and rep["valid_draws"] == [1, 3]
    split, rep = mat({1: draw(grp(VIH_min=2.0)), 2: draw(grp(VIH_min=2.5))})
    assert g0(split)["VIH_min"] is None and rep["counts"]["DISAGREE"] == 1


@pytest.mark.parametrize("draws", [
    {1: draw(grp(VIH_min=2.0))},
    {1: draw(grp(VIH_min=2.0)), 2: None, 3: None},
    {1: draw(grp(VIH_min=2.0)), 2: {"not": "a draw"}},
])
def test_fewer_than_two_valid_draws(draws):
    cache, rep = mat(draws)
    assert cache is None and rep["reason"] == "fewer_than_two_valid_draws"


# ── group-shaped fill ────────────────────────────────────────────────────────
def test_group_split():
    two = ("1|IN", "2|EN")
    pins = PINS + [{"pin_id": "2", "pin_name": "EN"}]
    merged = draw(grp(pins=two, VIH_min=2.0, VOL_max=0.4))
    split2 = draw(grp("gin", pins=("1|IN",), VIH_min=2.0, VOL_max=0.5),
                  grp("gen", pins=("2|EN",), VIH_min=3.0, VOL_max=0.5))
    split3 = draw(grp("gin", pins=("1|IN",), VIH_min=2.5, VOL_max=0.4),
                  grp("gen", pins=("2|EN",), VIH_min=3.0, VOL_max=0.4))
    cache, rep = mat({1: merged, 2: split2, 3: split3}, pins=pins)
    assert rep["base_replicate"] == 1                      # ties with draw 3, lowest index
    g = g0(cache)
    assert g["VIH_min"] is None and g["VOL_max"] == 0.4    # IN votes 2.0, EN votes 3.0: one group cannot hold both
    assert g["citations"]["VIH_min"]["status"] == "nulled:group_split"
    assert g["citations"]["VIH_min"]["rejected_value"] == 2.0
    assert rep["counts"]["GROUP-SPLIT"] == 1 and rep["counts"]["GROUP-SPLIT_valued"] == 1


def test_group_novote_blanket_and_empty_example_pins():
    def d():
        return draw(grp(VIH_min=2.0),
                    grp("blanket", pins=("All pins",), ptype="power", supply_max=3.6),
                    grp("empty", pins=(), ptype="power", supply_min=1.8))
    cache, rep = mat({1: d(), 2: d(), 3: d()})
    gs = {g["group_name"]: g for g in cache["pin_groups"]}
    assert gs["g"]["VIH_min"] == 2.0
    assert gs["blanket"]["supply_max"] is None and gs["empty"]["supply_min"] is None
    assert gs["blanket"]["citations"]["supply_max"]["status"] == "nulled:group_novote"
    assert gs["blanket"]["citations"]["supply_max"]["rejected_value"] == 3.6
    assert gs["empty"]["citations"]["supply_min"]["rejected_value"] == 1.8
    assert rep["counts"]["GROUP-NOVOTE"] == 16 and rep["counts"]["GROUP-NOVOTE_valued"] == 2
    assert gs["blanket"]["example_pins"] == ["All pins"]   # the base's groups are kept verbatim


# ── Z1 projection precedence ─────────────────────────────────────────────────
ALTS = [{"condition": "VDD = 5 V", "value": 3.5}, {"condition": "VDD < 5 V", "value": None, "expr": "0.7*VDD"}]


def test_z1_numeric_beside_alts_votes_alts_and_is_written_as_alts_only():
    d = {1: draw(grp(VIH_min=2.0, VIH_min_alts=copy.deepcopy(ALTS), VIH_min_expr="0.7*VDD")),
         2: draw(grp(VIH_min_alts=copy.deepcopy(ALTS))),
         3: draw(grp(VIH_min=2.0))}
    cache, rep = mat(d)
    g = g0(cache)
    assert g["VIH_min"] is None and g["VIH_min_alts"] == ALTS and "VIH_min_expr" not in g
    assert rep["counts"]["DISAGREE"] == 0


def test_z1_numeric_beside_expr_votes_rel_and_spellings_agree():
    d = {1: draw(grp(VIH_min=2.0, VIH_min_expr="0.7*VDD")),
         2: draw(grp(VIH_min_expr="VDD x 0.7")),
         3: draw(grp(VIH_min=2.0))}
    cache, rep = mat(d)
    g = g0(cache)
    assert g["VIH_min"] is None and g["VIH_min_expr"] == "0.7*VDD" and "VIH_min_alts" not in g
    assert rep["counts"]["DISAGREE"] == 0


def test_alts_labels_and_order_are_ignored():
    other = [{"condition": "low supply", "value": None, "expr": "VDD*0.7"}, {"condition": "5V", "value": 3.5}]
    d = {1: draw(grp(VIH_min_alts=copy.deepcopy(ALTS))), 2: draw(grp(VIH_min_alts=other)),
         3: draw(grp(VIH_min=2.0))}
    cache, rep = mat(d)
    assert g0(cache)["VIH_min_alts"] == ALTS and rep["counts"]["DISAGREE"] == 0


def test_unparseable_expr_votes_raw_and_is_counted():
    d = {1: draw(grp(VIH_min_expr="see note 3")), 2: draw(grp(VIH_min_expr="see note 3")),
         3: draw(grp(VIH_min_expr="see  note 3"))}
    cache, rep = mat(d)
    assert g0(cache)["VIH_min_expr"] == "see note 3"
    assert rep["counts"]["vote_raw_string"] == 1 and rep["counts"]["DISAGREE"] == 0


def test_value_written_in_exactly_one_form():
    d = {r: draw(grp(VIH_min=2.0, VIH_min_expr="0.7*VDD", VOL_max=0.4, VOL_max_alts=copy.deepcopy(ALTS)))
         for r in (1, 2)}
    d[3] = draw(grp(VIH_min=2.0, VOL_max=0.4))
    g = g0(mat(d)[0])
    assert (g["VIH_min"], g["VIH_min_expr"], g.get("VIH_min_alts")) == (None, "0.7*VDD", None)
    assert (g["VOL_max"], g.get("VOL_max_expr"), g["VOL_max_alts"]) == (None, None, ALTS)
    for f in extractors.MAJORITY_FIELDS:
        forms = [g.get(f) is not None, bool(g.get(f + "_expr")), bool(g.get(f + "_alts"))]
        assert sum(forms) <= 1, f


# ── citations ────────────────────────────────────────────────────────────────
def test_citation_from_the_base():
    d = vih(2.0, 2.0, 2.5)
    d[1]["pin_groups"][0]["citations"]["VIH_min"] = cit("base quote 2.0")
    g = g0(mat(d)[0])
    assert g["citations"]["VIH_min"] == cit("base quote 2.0")
    assert "from_replicate" not in g["citations"]["VIH_min"]


def test_citation_from_another_replicate_when_the_base_lost_the_vote():
    d = {1: draw(grp(VIH_min=2.0, VOL_max=0.4, VOH_min=2.4)),
         2: draw(grp(VIH_min=2.5, VOL_max=0.5, VOH_min=2.4, cits={"VIH_min": cit("r2 VIH 2.5", page=7)})),
         3: draw(grp(VIH_min=2.5, VOL_max=0.4, VOH_min=2.6))}
    cache, rep = mat(d)
    assert rep["base_replicate"] == 1                       # 2 / 2 / 2 agreements: lowest index
    g = g0(cache)
    assert g["VIH_min"] == 2.5
    assert g["citations"]["VIH_min"] == dict(cit("r2 VIH 2.5", page=7), from_replicate=2)
    assert g["VIH_source"] == "p. 7"                         # re-derived by the validator's labeller
    assert rep["counts"]["citation_from_other_replicate"] == 1


# ── the never-a-third-value invariant ────────────────────────────────────────
def test_invariant_holds_and_a_corrupted_fill_raises(monkeypatch):
    d = vih(2.0, 2.0, 2.5)
    mat(copy.deepcopy(d))                                    # holds

    def corrupt(g, f, sg):
        g[f] = 99.0
        return "numeric"
    monkeypatch.setattr(extractors, "_mj_write", corrupt)
    with pytest.raises(extractors.MajorityInvariantError):
        mat(d)


# ── Y2 pin list ──────────────────────────────────────────────────────────────
def test_union_pin_list_vs_an_explicit_one():
    def d():
        return draw(grp(VIH_min=2.0), grp("VCC", pins=("VCC",), ptype="power", supply_max=3.6))
    draws = {1: d(), 2: d(), 3: d()}
    union, rep_u = extractors.materialise_majority(draws)
    expl, rep_e = extractors.materialise_majority(draws, pins=PINS)
    assert rep_u["pin_list"] == {"size": 2, "source": "union_example_pins",
                                 "entries_per_replicate": {"1": 2, "2": 2, "3": 2}}
    assert rep_e["pin_list"]["size"] == 1 and rep_e["pin_list"]["source"] == "explicit"
    assert union["pin_groups"][1]["supply_max"] == 3.6       # the bare entry joins as (VCC, VCC) and votes
    assert expl["pin_groups"][1]["supply_max"] is None       # no gate pin resolves to it: GROUP-NOVOTE
    assert rep_e["counts"]["GROUP-NOVOTE_valued"] == 1


# ── the writer ───────────────────────────────────────────────────────────────
@pytest.fixture
def tmp_pdf(tmp_path):
    p = tmp_path / "FOO123.pdf"
    p.write_bytes(b"%PDF-1.4 fake content for hashing\n" * 100)
    return str(p)


def test_write_majority_cache(tmp_path, tmp_pdf):
    draws = vih(2.0, 2.0, 2.5)
    cache, mrep = mat(draws)
    reports = {r: {"validator_version": "3", "counts": {"citations": r}} for r in draws}
    validation = extractors.majority_validation(reports, mrep, {1: "a" * 64, 2: "a" * 64, 3: "a" * 64})
    dirty = dict(cache, model_input="TEXT", __truncated__=[], extractor_fingerprint={"x": 1},
                 request_sha256="b" * 64, input_regime="whole", usage={}, stop_reason="end_turn")
    ctx = extractors.PartContext(part_number="FOO123", pdf_path=tmp_pdf, input_regime="whole")
    dest = tmp_path / "out" / "foo123_pin_groups.json"
    extractors.write_majority_cache(ctx, dirty, validation=validation, dest_path=str(dest))
    data = json.loads(dest.read_text())
    for k in extractors._MAJORITY_STRIP:
        assert k not in data
    prov = data["provenance"]
    fp = prov["extractor_fingerprint"]["fields"]
    assert fp["draws"] == 3 and fp["aggregate"] == ("majority_2of3_null_on_disagree; citation=base_replicate; "
                                                    "one_entry_alts=plain")              # TODO-531 V1n (3e)
    assert r3.extractor_fingerprint_status(data) == "match"
    assert prov["validation"]["majority"]["base_replicate"] == 1
    assert prov["validation"]["majority"]["draw_counts"] == {"1": {"citations": 1}, "2": {"citations": 2},
                                                             "3": {"citations": 3}}
    assert prov["request_sha256"] == "a" * 64 and prov["service_tier"] == "batch"
    assert prov["input_regime"] == "whole"
    assert data["pin_groups"][0]["VIH_min"] == 2.0


def test_writer_refuses_unvoted_output(tmp_path, tmp_pdf):
    ctx = extractors.PartContext(part_number="FOO123", pdf_path=tmp_pdf)
    with pytest.raises(ValueError):
        extractors.write_majority_cache(ctx, draw(grp(VIH_min=2.0)), validation={"counts": {}},
                                        dest_path=str(tmp_path / "x.json"))
    with pytest.raises(ValueError):
        extractors.write_majority_cache(ctx, None, validation={"majority": {}}, dest_path=str(tmp_path / "x.json"))
    assert not (tmp_path / "x.json").exists()


def test_sync_fingerprint_unchanged():
    impl = extractors.get_extractor("haiku_pdfplumber")
    assert extractors.extractor_fingerprint(impl, draws=1, aggregate="single")["sha256"] == SYNC_FINGERPRINT_SHA


# ── AA1: the supply reading path (pin_match.listing_supply_groups) ──────────
VDDO = ("10|VDDO0", "11|VDDO1")
RAILS = {"VDDO 3.3 V": (3.135, 3.465, 3.8), "VDDO 2.5 V": (2.375, 2.625, 3.8), "VDDO 1.8 V": (1.71, 1.89, 3.8)}


def rails(*names, **override):
    """a draw whose first power group (the one every VDDO pin resolves to) is the 3.3 V rail, followed by the
    named variant rails listing the same pins; override {group name: (min, max, abs)}; plus a digital group and
    its duplicate (a non-power group no pin resolves to)."""
    gs = []
    for n in ("VDDO 3.3 V",) + names:
        lo, hi, ab = override.get(n, RAILS[n])
        gs.append(grp(n, pins=VDDO, ptype="power", supply_min=lo, supply_max=hi, supply_abs_max=ab))
    gs += [grp("IO", pins=("12|IN",), VIH_min=2.0), grp("IO dup", pins=("12|IN",), VIH_min=2.0)]
    return draw(*gs)


def by_name(cache):
    return {g["group_name"]: g for g in cache["pin_groups"]}


def supply(g):
    return tuple(g[f] for f in ("supply_min", "supply_max", "supply_abs_max"))


def test_aa1_variants_in_every_draw_are_kept():
    cache, rep = extractors.materialise_majority({r: rails("VDDO 2.5 V", "VDDO 1.8 V") for r in (1, 2, 3)})
    gs = by_name(cache)
    assert supply(gs["VDDO 2.5 V"]) == RAILS["VDDO 2.5 V"] and supply(gs["VDDO 1.8 V"]) == RAILS["VDDO 1.8 V"]
    assert gs["VDDO 1.8 V"]["citations"]["supply_max"] == cit("VDDO 1.8 V supply_max 1.89")     # the base's
    assert rep["counts"]["SUPPLY-SET-KEPT"] == 6 and rep["counts"]["SUPPLY-SET-NULL"] == 0
    assert [(k["group"], k["pins"]) for k in rep["supply_set"]["kept"]] == [
        ("VDDO 2.5 V", {"10|VDDO0": 3, "11|VDDO1": 3}), ("VDDO 1.8 V", {"10|VDDO0": 3, "11|VDDO1": 3})]
    # the other five fields of a kept group and the whole of the non-power duplicate stay GROUP-NOVOTE
    assert rep["counts"]["GROUP-NOVOTE"] == 2 * 5 + 8 and rep["counts"]["GROUP-NOVOTE_valued"] == 1
    assert gs["IO dup"]["VIH_min"] is None and gs["IO dup"]["citations"]["VIH_min"]["status"] == "nulled:group_novote"


def test_aa1_a_variant_two_of_three_draws_carry_is_kept():
    draws = {1: rails("VDDO 2.5 V", "VDDO 1.8 V"), 2: rails("VDDO 2.5 V", "VDDO 1.8 V"), 3: rails("VDDO 2.5 V")}
    cache, rep = extractors.materialise_majority(draws)
    assert rep["base_replicate"] == 1
    assert supply(by_name(cache)["VDDO 1.8 V"]) == RAILS["VDDO 1.8 V"]
    assert {k["group"]: k["pins"]["10|VDDO0"] for k in rep["supply_set"]["kept"]} == {"VDDO 2.5 V": 3,
                                                                                      "VDDO 1.8 V": 2}


def test_aa1_a_variant_in_the_base_only_is_nulled_supply_set():
    draws = {1: rails("VDDO 2.5 V", "VDDO 1.8 V"), 2: rails("VDDO 2.5 V"),
             3: rails("VDDO 2.5 V", "VDDO 1.8 V", **{"VDDO 1.8 V": (1.71, 1.95, 3.8)})}     # another tuple
    cache, rep = extractors.materialise_majority(draws)
    gs = by_name(cache)
    assert rep["base_replicate"] == 1
    assert supply(gs["VDDO 2.5 V"]) == RAILS["VDDO 2.5 V"]
    assert supply(gs["VDDO 1.8 V"]) == (None, None, None)
    c = gs["VDDO 1.8 V"]["citations"]["supply_max"]
    assert c["status"] == "nulled:supply_set" and c["rejected_value"] == 1.89
    assert rep["counts"]["SUPPLY-SET-NULL"] == 3 and rep["counts"]["SUPPLY-SET-NULL_valued"] == 3
    assert rep["supply_set"]["nulled"] == [{"group": "VDDO 1.8 V", "index": 2, "tuple": [1.71, 1.89, 3.8],
                                            "pins": {"10|VDDO0": 1, "11|VDDO1": 1}}]


def test_aa1_a_non_power_novote_group_stays_nulled():
    cache, rep = extractors.materialise_majority({r: rails() for r in (1, 2, 3)})
    assert rep["supply_set"] == {"kept": [], "nulled": []}
    assert by_name(cache)["IO dup"]["VIH_min"] is None and rep["counts"]["GROUP-NOVOTE"] == 8


def test_aa1_invariant_raises_on_a_kept_unsupported_tuple(monkeypatch):
    draws = {1: rails("VDDO 1.8 V"), 2: rails(), 3: rails()}
    extractors.materialise_majority(copy.deepcopy(draws))                       # nulled: holds
    monkeypatch.setattr(extractors, "_mj_supply_keep", lambda support: True)  # keep it anyway
    with pytest.raises(extractors.MajorityInvariantError, match="kept group 'VDDO 1.8 V'"):
        extractors.materialise_majority(draws)


# ── AA2: the fallback probe in base selection (union pin list only) ─────────
def ena_draws(defaults=("NC", "ENA", "ENA")):
    """tps5430dda-shaped: board pin EN matches no example_pins entry; each draw's fallback is its default_group."""
    def d(default, vih_=1.3):
        return draw(grp("ENA", pins=("5|ENA",), VIH_min=vih_, VIL_max=0.5, absolute_max_voltage=7.0),
                    grp("X", pins=("6|X",), VIH_min=2.4),
                    grp("NC", pins=("NC",), ptype="no_connect"), default=default)
    return {r: d(dg) for r, dg in enumerate(defaults, 1)}


def test_aa2_the_probe_breaks_a_tie_toward_the_draws_whose_fallback_agrees():
    from steps.pin_match import resolve_pin_spec_from_groups
    cache, rep = extractors.materialise_majority(ena_draws())
    assert rep["base_without_probe"] == 1 and rep["base_replicate"] == 2       # tie on every listed pin
    assert rep["probe"]["group_per_draw"] == {"1": "NC", "2": "ENA", "3": "ENA"}
    assert rep["probe"]["agreement"] == {"1": 5, "2": 8, "3": 8} and rep["probe"]["base_changed"] is True
    assert cache["default_group"] == "ENA"
    assert resolve_pin_spec_from_groups("5", "EN", cache)["VIH_min"] == 1.3


def test_aa2_an_explicit_pin_list_has_no_probe():
    cache, rep = extractors.materialise_majority(ena_draws(), pins=[{"pin_id": "5", "pin_name": "ENA"}])
    assert rep["probe"] is None and rep["base_replicate"] == rep["base_without_probe"] == 1
    assert cache["default_group"] == "NC"
    v = extractors._majority_vote(ena_draws(), [{"pin_id": "5", "pin_name": "ENA"}])
    assert v["probe"] is None and v["base"] == v["base_without_probe"] == 1


def test_aa2_the_probe_is_in_no_pin_list_vote_or_disagree():
    draws = ena_draws(("NC", "ENA", "X"))                                      # three fallbacks: all distinct
    cache, rep = extractors.materialise_majority(copy.deepcopy(draws))
    probe = "|".join(extractors._MJ_PROBE)
    assert rep["probe"]["group_per_draw"] == {"1": "NC", "2": "ENA", "3": "X"}
    assert rep["pin_list"]["size"] == 3 and rep["counts"]["DISAGREE"] == 0 and rep["disagree"] == []
    v = extractors._majority_vote(copy.deepcopy(draws))
    assert probe not in v["pks"] and all(pk != probe for pk, _f in v["agg"])
    assert not any(probe in str(k["pins"]) for k in rep["supply_set"]["kept"] + rep["supply_set"]["nulled"])
    named = copy.deepcopy(draws)                          # a draw that lists the probe's name drops it
    named[2]["pin_groups"][1]["example_pins"].append("MJ_FALLBACK_PROBE")
    _c, rep2 = extractors.materialise_majority(named)
    assert rep2["probe"]["dropped"] is True and rep2["probe"]["base_changed"] is False


# ── TODO-531 BUILD-C: V1n (one-entry _alts votes and is written plain) and N2u in the vote ─────────────────
def one(cond, value=None, expr=None):
    x = {"condition": cond, "value": value}
    if expr is not None:
        x["expr"] = expr
    return [x]


def acit(f, quote):
    return {f"{f}_alts[0]": cit(quote)}


def test_v1_one_entry_alts_keys_as_its_plain_value():
    from steps.step_05_validator import parse_rail_expr as P
    a = extractors._mj_value({"VIH_min_alts": one("VDD = 3.3 V", 2.0)}, "VIH_min")
    e = extractors._mj_value({"VIH_min_alts": one("VDD = 3.3 V", expr="0.7*VDD")}, "VIH_min")
    two = extractors._mj_value({"VIH_min_alts": one("a", 2.0) + one("b", 2.5)}, "VIH_min")
    assert extractors._mj_key(a, P) == extractors._mj_key(2.0, P)
    assert extractors._mj_key(e, P) == extractors._mj_key("REL:VDD*0.7", P)
    assert extractors._mj_key(two, P) != extractors._mj_key(2.0, P)
    assert extractors._mj_key(two, P).startswith("('A'")


def test_v1_one_entry_vs_two_entry_alts_are_different_keys():
    from steps.step_05_validator import parse_rail_expr as P
    a1 = extractors._mj_value({"VIH_min_alts": one("x", 2.0)}, "VIH_min")
    a2 = extractors._mj_value({"VIH_min_alts": one("x", 2.0) + one("y", 2.0)}, "VIH_min")
    assert extractors._mj_key(a1, P) != extractors._mj_key(a2, P)


def test_v1_unparseable_one_entry_expr_votes_by_raw_string():
    from steps.step_05_validator import parse_rail_expr as P
    a = extractors._mj_value({"VIH_min_alts": one("x", expr="see note 3")}, "VIH_min")
    assert extractors._mj_key(a, P) == extractors._mj_key("REL:see note 3", P) == repr(("S", "REL:see note 3"))
    assert extractors._mj_votes_raw(a, P)
    d = {r: draw(grp(VIH_min_alts=one(f"c{r}", expr="see note 3"), cits=acit("VIH_min", "note 3"))) for r in (1, 2)}
    d[3] = draw(grp(VIH_min=2.5))
    cache, rep = mat(d)
    assert g0(cache)["VIH_min_expr"] == "see note 3" and "VIH_min_alts" not in g0(cache)
    assert rep["counts"]["vote_raw_string"] == 1 and rep["counts"]["written_plain_from_alts"] == 1


def test_v1n_one_entry_alts_agrees_with_the_numeric_and_the_numeric_replicate_is_written():
    d = {1: draw(grp(VIH_min_alts=one("VDD = 3.3 V", 2.0), cits=acit("VIH_min", "alts 2.0"))),
         2: draw(grp(VIH_min=2.0)),
         3: draw(grp(VIH_min=2.5))}
    cache, rep = mat(d)
    g = g0(cache)
    assert rep["counts"]["DISAGREE"] == 0 and rep["base_replicate"] == 1
    assert g["VIH_min"] == 2.0 and "VIH_min_alts" not in g and "VIH_min_expr" not in g
    assert g["citations"]["VIH_min"]["quote"] == "g VIH_min 2.0"
    assert g["citations"]["VIH_min"]["from_replicate"] == 2 and "from_alts" not in g["citations"]["VIH_min"]
    assert not [k for k in g["citations"] if k.startswith("VIH_min_alts[")]
    assert rep["counts"]["written_plain_from_alts"] == 0


def test_v1n_every_voter_alts_is_written_numeric_with_from_alts():
    d = {1: draw(grp(VIH_min_alts=one("VDD = 3.3 V", 2), cits=acit("VIH_min", "VIH 2 V at 3.3 V"))),
         2: draw(grp(VIH_min_alts=one("3.3 V supply", 2.0), cits=acit("VIH_min", "2.0"))),
         3: draw(grp(VIH_min=2.5))}
    cache, rep = mat(d)
    g = g0(cache)
    assert g["VIH_min"] == 2 and "VIH_min_alts" not in g and "VIH_min_expr" not in g
    assert g["citations"]["VIH_min"] == dict(cit("VIH 2 V at 3.3 V"), from_alts=True)
    assert not [k for k in g["citations"] if k.startswith("VIH_min_alts[")]
    assert rep["counts"]["written_plain_from_alts"] == 1


def test_v1n_three_one_entry_alts_two_exprs_win_and_are_written_as_expr():
    d = {1: draw(grp(VIL_max_alts=one("5 V", 1.5), cits=acit("VIL_max", "1.5"))),
         2: draw(grp(VIL_max_alts=one("any VDD", expr="0.3*VDD"), cits=acit("VIL_max", "0.3 VDD"))),
         3: draw(grp(VIL_max_alts=one("VDD", expr="VDD x 0.3"), cits=acit("VIL_max", "VDD x 0.3")))}
    cache, rep = mat(d)
    g = g0(cache)
    assert rep["base_replicate"] == 2
    assert g["VIL_max"] is None and g["VIL_max_expr"] == "0.3*VDD" and "VIL_max_alts" not in g
    assert g["citations"]["VIL_max_expr"] == dict(cit("0.3 VDD"), from_alts=True)
    assert "VIL_max" not in g["citations"] and not [k for k in g["citations"] if k.startswith("VIL_max_alts[")]


def test_v1n_from_another_replicate_carries_from_replicate_and_from_alts():
    d = {1: draw(grp(VIH_min=2.5, VOL_max=0.5, VOH_min=2.6)),
         2: draw(grp(VIH_min_alts=one("a", 2.0), VOL_max=0.4, VOH_min=2.4, cits=acit("VIH_min", "a 2.0"))),
         3: draw(grp(VIH_min_alts=one("b", 2.0), VOL_max=0.4, VOH_min=2.4, cits=acit("VIH_min", "b 2.0")))}
    cache, rep = mat(d)
    c = g0(cache)["citations"]["VIH_min"]
    assert rep["base_replicate"] == 2 and g0(cache)["VIH_min"] == 2.0
    assert c == dict(cit("a 2.0"), from_alts=True)                    # the base voted: no from_replicate
    d[2], d[1] = d[1], d[2]
    cache, rep = mat(d)
    assert rep["base_replicate"] == 1
    assert g0(cache)["citations"]["VIH_min"] == dict(cit("a 2.0"), from_alts=True)


def test_v1n_multi_entry_alts_are_written_as_today():
    alts = one("a", 2.0) + one("b", expr="0.7*VDD")
    cache, _rep = mat({r: draw(grp(VIH_min_alts=copy.deepcopy(alts))) for r in (1, 2, 3)})
    assert g0(cache)["VIH_min_alts"] == alts and g0(cache)["VIH_min"] is None


def test_v1n_invariant_holds_on_the_materialised_cache():
    d = {1: draw(grp(VIH_min_alts=one("x", 2.0), VIL_max_alts=one("y", expr="0.3*VDD"))),
         2: draw(grp(VIH_min=2.0, VIL_max_expr="VDD*0.3")),
         3: draw(grp(VIH_min_alts=one("z", 2.0), VIL_max=0.9))}
    cache, _rep = mat(d)                                              # raises MajorityInvariantError otherwise
    v = extractors._majority_vote(d, PINS)
    re_proj = extractors._mj_project(cache, v["pins"], v["resolve"])
    for f in ("VIH_min", "VIL_max"):
        got = re_proj[PK][1][f]
        assert got is not None and v["key"](got) == v["key"](v["agg"][(PK, f)])
        assert extractors._mj_one_alt(got) is None                    # written plain


def test_v1n_aggregate_literal_prefers_the_plain_form():
    d = {1: draw(grp(VIH_min_alts=one("x", 2.0))), 2: draw(grp(VIH_min_alts=one("y", 2.0))), 3: draw(grp(VIH_min=2.0))}
    assert extractors._majority_vote(d, PINS)["agg"][(PK, "VIH_min")] == 2.0
    d[3] = draw(grp(VIH_min=2.5))
    assert extractors._majority_vote(d, PINS)["agg"][(PK, "VIH_min")] == 2.0     # converted from the ALTS


def test_identical_plain_draws_are_byte_identical_to_a617030():
    import hashlib
    d = {r: draw(grp("io", pins=("1|IN", "2|EN"), VIH_min=2.0, VIL_max=0.8, VOL_max=0.4, VIH_min_expr=None),
                 grp("pwr", pins=("3|VCC",), ptype="power", supply_min=1.8, supply_max=3.6, supply_abs_max=4.0),
                 default="io") for r in (1, 2, 3)}
    for pins in (None, [{"pin_id": "1", "pin_name": "IN"}, {"pin_id": "3", "pin_name": "VCC"}]):
        cache, _rep = extractors.materialise_majority(copy.deepcopy(d), pins=pins)
        s = json.dumps(cache, sort_keys=True)
        # sha256 of the same call's cache at a617030 (BUILD-B head), measured from that tree
        assert hashlib.sha256(s.encode()).hexdigest() == \
            "25ccb4c0c8a4eddacbe2b114c144157712484130ef87b2fd45d920755b0c8dfd"


def test_n2u_union_pin_list_binds_a_related_name_across_draws():
    vin = dict(pins=("3|VIN",), ptype="power", supply_min=4.5, supply_max=12.0)
    d = {1: draw(grp("in", **vin), grp("io", pins=("1|ADJ",))),
         2: draw(grp("in", **dict(vin, pins=("3|VI",))), grp("io", pins=("1|ADJ",))),
         3: draw(grp("in", **vin), grp("io", pins=("1|ADJ",)))}
    v = extractors._majority_vote(d)
    assert ("3", "VI") in v["pins"] and ("3", "VI", "") in v["part_pins"]
    for r in (1, 2, 3):
        assert v["proj"][r]["3|VI"][0]["group_name"] == "in"         # number_name in draws 1 and 3
        assert v["proj"][r]["3|VIN"][0]["group_name"] == "in"
    assert v["agg"][("3|VI", "supply_max")] == 12.0
    cache, rep = extractors.materialise_majority(d)
    assert next(g for g in cache["pin_groups"] if g["group_name"] == "in")["supply_max"] == 12.0


def test_probe_is_unchanged_by_part_pins():
    from steps.pin_match import match_groups
    d = vih(2.0, 2.0, 2.5)
    pid, pname = extractors._MJ_PROBE
    for r in d:
        assert match_groups(pid, pname, d[r]["pin_groups"]) == ("none", [])
    assert extractors._majority_vote(d)["probe"]["dropped"] is False

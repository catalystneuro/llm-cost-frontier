"""Tests for the update pipeline: payload parsing, history merging, and the
frontier derivations the dashboard renders.

Everything runs offline. Synthetic fixtures are kept small enough to verify by
hand; the tests against the repository's real data check invariants only, so
they keep passing as the data grows.
"""
import datetime as dt
import io
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from llm_cost_frontier.update import (
    CAPABILITIES, FEED_ENTRIES, MIN_PRICE_MOVE, SNAPSHOT_COUNT, TIERS,
    add_months, apply_overrides, build_output, capability_models, cost_changes,
    describe, enclosing_object, extract_models, fetch_payload,
    frontier_advances, join_and, main, merge, parse_object_at, pareto,
    price_timeline, snapshots, split_variant, taken_clause, tier_records,
    tier_summary, write_feed,
)

REPO = Path(__file__).resolve().parents[1]


def model(name, release, iq, cost, open_weights=False, retired=False, caps=None, obs=None, last_seen="2026-09-01"):
    return dict(
        name=name, creator="Lab", release_date=release, intelligence_index=iq,
        cost_per_task=cost, open_weights=open_weights, capabilities=caps or {},
        retired=retired, first_seen=release, last_seen=last_seen,
        observations=obs if obs is not None else [[release, cost, iq]],
    )


# ---- payload parsing ----

def payload_for(objects):
    return "".join(json.dumps(o) for o in objects)


def source_object(slug="test-model", name="Test Model (high)", cost=0.123456, iq=50.06, **extra):
    o = dict(
        slug=slug, name=name, releaseDate="2026-01-05T00:00:00.000Z",
        intelligenceIndex=iq, isOpenWeights=False, deprecated=False,
        creator=dict(name="Lab"),
        intelligenceIndexCostPerTask=dict(cost=dict(total=cost)),
    )
    o.update(extra)
    return o


def test_parse_object_at_handles_nesting_and_strings():
    s = 'x{"a": {"b": "}{"}, "c": [1, 2]}y'
    assert parse_object_at(s, 1) == {"a": {"b": "}{"}, "c": [1, 2]}


def test_enclosing_object_returns_outer_object():
    o = source_object()
    s = payload_for([{"other": 1}, o])
    idx = s.index('"intelligenceIndexCostPerTask"')
    assert enclosing_object(s, idx)["slug"] == "test-model"


def test_fetch_payload_joins_escaped_chunks(monkeypatch):
    html = ('<script>self.__next_f.push([1,"{\\"x\\": "])</script>'
            '<script>self.__next_f.push([1,"42}"])</script>')
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: io.BytesIO(html.encode()))
    assert fetch_payload("https://example.org") == '{"x": 42}'


def test_fetch_payload_rejects_pages_without_chunks(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: io.BytesIO(b"<html></html>"))
    with pytest.raises(RuntimeError):
        fetch_payload("https://example.org")


def test_extract_models_reads_fields_and_capabilities():
    o = source_object(terminalbenchV21=0.789, omniscience=-10.76, agenticIndex=44.36)
    got = extract_models(payload_for([o]))["test-model"]
    assert got["name"] == "Test Model (high)"
    assert got["creator"] == "Lab"
    assert got["release_date"] == "2026-01-05"
    assert got["intelligence_index"] == 50.1
    assert got["cost_per_task"] == 0.123456
    assert got["capabilities"] == {"coding": 78.9, "knowledge": -10.8, "agentic": 44.4}


def test_extract_models_skips_invalid_entries():
    objs = [
        source_object(slug="free-model", cost=0.0),
        dict(source_object(slug="undated"), releaseDate=None),
        source_object(slug="kept"),
    ]
    assert set(extract_models(payload_for(objs))) == {"kept"}


# ---- history merging ----

def live_record(name, release, iq, cost, caps=None):
    return dict(name=name, creator="Lab", release_date=release, intelligence_index=iq,
                cost_per_task=cost, open_weights=False, deprecated=False, capabilities=caps or {})


def test_merge_appends_observation_only_on_change():
    history = {"updated": "2026-09-01", "models": {"m": model("M", "2026-01-01", 50.0, 1.0)}}
    merge(history, {"m": live_record("M", "2026-01-01", 50.0, 1.0)}, "2026-09-02")
    m = history["models"]["m"]
    assert m["observations"] == [["2026-01-01", 1.0, 50.0]]
    assert m["last_seen"] == "2026-09-02"
    merge(history, {"m": live_record("M", "2026-01-01", 50.0, 0.8)}, "2026-09-03")
    m = history["models"]["m"]  # merge rebuilds the record
    assert m["observations"] == [["2026-01-01", 1.0, 50.0], ["2026-09-03", 0.8, 50.0]]


def test_merge_ignores_index_noise_below_threshold():
    history = {"updated": "2026-09-01", "models": {"m": model("M", "2026-01-01", 50.0, 1.0)}}
    merge(history, {"m": live_record("M", "2026-01-01", 50.04, 1.0)}, "2026-09-02")
    assert len(history["models"]["m"]["observations"]) == 1
    merge(history, {"m": live_record("M", "2026-01-01", 50.1, 1.0)}, "2026-09-03")
    assert len(history["models"]["m"]["observations"]) == 2


def test_merge_retires_missing_models_and_keeps_their_data():
    history = {"updated": "2026-09-01", "models": {
        "m": model("M", "2026-01-01", 50.0, 1.0),
        "gone": model("Gone", "2025-06-01", 30.0, 0.5),
    }}
    merge(history, {"m": live_record("M", "2026-01-01", 50.0, 1.0)}, "2026-09-02")
    gone = history["models"]["gone"]
    assert gone["retired"]
    assert gone["observations"] == [["2025-06-01", 0.5, 30.0]]


def test_merge_carries_capabilities_forward_when_absent_from_live():
    history = {"updated": "2026-09-01", "models": {
        "m": model("M", "2026-01-01", 50.0, 1.0, caps={"coding": 80.0}),
    }}
    merge(history, {"m": live_record("M", "2026-01-01", 50.0, 1.0)}, "2026-09-02")
    assert history["models"]["m"]["capabilities"] == {"coding": 80.0}
    merge(history, {"m": live_record("M", "2026-01-01", 50.0, 1.0, caps={"coding": 81.0})}, "2026-09-03")
    assert history["models"]["m"]["capabilities"] == {"coding": 81.0}


def test_merge_seeds_observations_for_legacy_records():
    legacy = model("M", "2026-01-01", 50.0, 1.0)
    del legacy["observations"]
    legacy["last_seen"] = "2026-08-01"
    history = {"updated": "2026-09-01", "models": {"m": legacy}}
    merge(history, {"m": live_record("M", "2026-01-01", 50.0, 1.0)}, "2026-09-02")
    assert history["models"]["m"]["observations"] == [["2026-08-01", 1.0, 50.0]]


def test_apply_overrides_sets_flag_and_tolerates_unknown_slugs(capsys):
    models = {"m": model("M", "2026-01-01", 50.0, 1.0)}
    apply_overrides(models, {"open_weights": {"m": {"value": True}, "ghost": {"value": True}}})
    assert models["m"]["open_weights"] is True
    assert "ghost" in capsys.readouterr().out


# ---- dates and snapshots ----

def test_add_months_clamps_day_and_wraps_year():
    assert add_months(dt.date(2026, 1, 31), 1) == dt.date(2026, 2, 28)
    assert add_months(dt.date(2024, 1, 31), 1) == dt.date(2024, 2, 29)
    assert add_months(dt.date(2026, 11, 15), 3) == dt.date(2027, 2, 15)
    assert add_months(dt.date(2026, 1, 15), -2) == dt.date(2025, 11, 15)


def test_snapshots_end_today_and_step_two_months():
    out = snapshots(dt.date(2026, 9, 4))
    assert len(out) == SNAPSHOT_COUNT
    assert out[-1] == ["2026-09-04", "today"]
    dates = [dt.date.fromisoformat(d) for d, _ in out[:-1]]
    assert dates[-1] == dt.date(2026, 9, 1)
    assert all((b.year - a.year) * 12 + b.month - a.month == 2 for a, b in zip(dates, dates[1:]))


def test_snapshots_on_the_first_use_the_previous_month():
    out = snapshots(dt.date(2026, 9, 1))
    assert out[-2][0] == "2026-08-01"


# ---- price timeline and events ----

def test_cost_changes_applies_price_events_before_the_cut():
    events = [{"slug_prefix": "alpha", "cut_date": "2026-02-15", "multiplier_before": 5.0}]
    m = model("Alpha", "2026-01-01", 40.0, 1.0)
    assert cost_changes("alpha", m, events) == [
        ["2026-01-01", 5.0, "at launch price"],
        ["2026-02-15", 1.0, "price cut (released 2026-01-01)"],
    ]
    late = model("Alpha 2", "2026-03-01", 40.0, 1.0)
    assert cost_changes("alpha-2", late, events) == [["2026-03-01", 1.0, None]]


def test_cost_changes_dates_observed_changes():
    m = model("M", "2026-01-01", 50.0, 0.4,
              obs=[["2026-01-01", 0.5, 50.0], ["2026-06-01", 0.4, 50.0]])
    changes = cost_changes("m", m, [])
    assert [c[:2] for c in changes] == [["2026-01-01", 0.5], ["2026-06-01", 0.4]]
    assert "price change observed" in changes[1][2]


def test_price_timeline_is_sorted_by_date():
    models = {
        "a": model("A", "2026-02-01", 40.0, 1.0),
        "b": model("B", "2026-01-01", 50.0, 2.0),
    }
    dates = [e[0] for e in price_timeline(models, [])]
    assert dates == sorted(dates)


# ---- frontier machinery ----

def test_pareto_keeps_undominated_and_ties():
    state = {"a": (1.0, 40.0), "b": (2.0, 50.0), "dom": (2.5, 45.0), "tie": (1.0, 40.0)}
    assert pareto(state) == {"a", "b", "tie"}


def test_split_variant():
    assert split_variant("GPT-6 Astra (xhigh)") == ("GPT-6 Astra", "xhigh")
    assert split_variant("GLM-5.3-Flash") == ("GLM-5.3-Flash", None)


def test_tier_records_track_the_running_minimum():
    models = {
        "a": model("A", "2026-01-01", 45.0, 1.0),
        "b": model("B", "2026-02-01", 50.0, 2.0),
        "c": model("C", "2026-03-01", 46.0, 0.5),
    }
    recs = tier_records(models, events=[], tiers=[40])["40"]
    assert [(r[0], r[1], r[2]) for r in recs] == [
        ("2026-01-01", 1.0, "A"), ("2026-03-01", 0.5, "C")]


def three_model_history():
    return {
        "alpha": model("Alpha", "2026-01-01", 40.0, 1.0),
        "beta": model("Beta", "2026-02-01", 50.0, 2.0),
        "gamma": model("Gamma", "2026-03-01", 45.0, 0.5),
    }


def test_frontier_advances_for_releases():
    models = three_model_history()
    advances = frontier_advances(models, [], tier_records(models, []))
    by_model = {a["model"]: a for a in advances}
    assert list(by_model) == ["Gamma", "Beta", "Alpha"]  # newest first

    beta = by_model["Beta"]
    assert beta["kind"] == "new model"
    assert beta["ceiling_from"] == 40.0
    assert (beta["owns_from"], beta["owns_to"]) == (40.0, 50.0)

    gamma = by_model["Gamma"]
    assert gamma["taken_from"] == ["Beta", "Alpha"]
    assert gamma["displaced"] == ["Alpha"]
    assert gamma["ceiling_from"] is None
    assert 40 in gamma["records"]


def test_frontier_advances_reports_large_price_drops():
    models = three_model_history()
    models["gamma"]["observations"].append(["2026-06-01", 0.4, 45.0])
    models["gamma"]["cost_per_task"] = 0.4
    advances = frontier_advances(models, [], {})
    cut = [a for a in advances if a["kind"] == "price change"]
    assert len(cut) == 1
    assert cut[0]["model"] == "Gamma"
    assert cut[0]["date"] == "2026-06-01"
    assert cut[0]["previous_cost"] == 0.5


def test_frontier_advances_ignores_price_wiggles_below_threshold():
    models = three_model_history()
    wiggle = 0.5 - MIN_PRICE_MOVE / 2
    models["gamma"]["observations"].append(["2026-06-01", wiggle, 45.0])
    models["gamma"]["cost_per_task"] = wiggle
    advances = frontier_advances(models, [], {})
    assert not [a for a in advances if a["kind"] == "price change"]


def test_frontier_advances_ignores_price_increases():
    models = three_model_history()
    models["gamma"]["observations"].append(["2026-06-01", 0.8, 45.0])
    models["gamma"]["cost_per_task"] = 0.8
    advances = frontier_advances(models, [], {})
    assert not [a for a in advances if a["kind"] == "price change"]


def test_variant_departure_is_not_a_displacement():
    models = {
        "big": model("Big (high)", "2026-01-01", 50.0, 2.0),
        "big-low": model("Big (low)", "2026-01-01", 40.0, 1.0),
        "rival": model("Rival", "2026-02-01", 45.0, 0.5),
    }
    advances = frontier_advances(models, [], {})
    rival = next(a for a in advances if a["model"] == "Rival")
    # Big (low) leaves the frontier, but Big (high) remains, so the base model
    # is not displaced.
    assert rival["displaced"] == []


def test_tier_summary_collapse_and_halving():
    recs = {"40": [["2026-01-01", 1.0, "A", 45.0], ["2026-03-02", 0.25, "C", 46.0]],
            "60": [["2026-01-01", 1.0, "A", 60.0]],
            "70": []}
    out = tier_summary(recs)
    assert out["40"]["collapse"] == 4.0
    assert out["40"]["halving_days"] == 30  # 60 days / log2(4)
    assert out["60"] is None and out["70"] is None


def test_capability_models_substitutes_scores():
    models = {
        "a": model("A", "2026-01-01", 40.0, 1.0, caps={"coding": 80.0}),
        "b": model("B", "2026-02-01", 50.0, 2.0),
    }
    cm = capability_models(models, "coding")
    assert set(cm) == {"a"}
    assert cm["a"]["intelligence_index"] == 80.0
    assert models["a"]["intelligence_index"] == 40.0  # original untouched


# ---- output assembly ----

def test_build_output_shape():
    history = {"updated": "2026-09-04", "models": three_model_history()}
    history["models"]["gamma"]["observations"].append(["2026-06-01", 0.4, 45.0])
    history["models"]["gamma"]["capabilities"] = {"coding": 85.0}
    out = build_output(history, events=[])
    assert out["counts"] == {"total": 3, "live": 3, "retired": 0}
    assert len(out["snapshots"]) == SNAPSHOT_COUNT
    assert [c["key"] for c in out["capabilities"]] == [c["key"] for c in CAPABILITIES]
    names = [r[0] for r in out["models"]]
    assert names == ["Alpha", "Beta", "Gamma"]  # sorted by release date
    for row in out["models"]:
        assert len(row) == 9
        assert len(row[8]) == len(CAPABILITIES)
    alpha, gamma = out["models"][0], out["models"][2]
    assert alpha[7] == 0  # no price changes
    assert [c[:2] for c in gamma[7]] == [["2026-03-01", 0.5], ["2026-06-01", 0.4]]
    assert out["cap_tiers"]["coding"] == [50, 60, 70, 80]
    json.dumps(out)  # everything must be serializable


def test_join_and_and_taken_clause():
    assert join_and(["A"]) == "A"
    assert join_and(["A", "B"]) == "A and B"
    assert join_and(["A", "B", "C"]) == "A, B, and C"
    assert taken_clause(["A"], ["A"]) == ", taking it from A, which left the frontier"
    assert taken_clause(["A", "B"], ["A", "B"]) == ", taking it from A and B, both of which left the frontier"
    assert taken_clause([], ["C"]) == "; C left the frontier"


def advance(**over):
    a = dict(date="2026-06-01", model="Gamma", base="Gamma", variant=None, creator="Lab",
             slug="gamma", intelligence_index=45.0, cost_per_task=0.4, previous_cost=None,
             kind="new model", open_weights=False, owns_from=0.0, owns_to=45.0,
             records=[], taken_from=[], ceiling_from=None, displaced=[])
    a.update(over)
    return a


def test_describe_price_change_and_records():
    text = describe(advance(kind="price change", previous_cost=0.5, records=[40],
                            taken_from=["Beta"], displaced=["Beta"]))
    assert "price moved from $0.500 to $0.40 per task" in text
    assert "taking it from Beta, which left the frontier" in text
    assert "New cost record for index ≥ 40." in text
    assert text.endswith("Proprietary.")


def test_write_feed_is_valid_atom(tmp_path):
    out = dict(updated="2026-09-04", advances=[advance(), advance(date="2026-05-01", slug="beta", model="Beta", base="Beta")])
    feed = tmp_path / "feed.xml"
    write_feed(out, feed, "https://example.org")
    root = ET.parse(feed).getroot()
    ns = "{http://www.w3.org/2005/Atom}"
    entries = root.findall(f"{ns}entry")
    assert len(entries) == 2
    ids = [e.find(f"{ns}id").text for e in entries]
    assert len(set(ids)) == 2
    enclosures = [l for e in entries for l in e.findall(f"{ns}link") if l.get("rel") == "enclosure"]
    assert len(enclosures) == 2


# ---- end to end, offline ----

def test_main_offline_writes_outputs(tmp_path):
    (tmp_path / "history.json").write_text(json.dumps(
        {"updated": "2026-09-04", "models": three_model_history()}))
    (tmp_path / "events.json").write_text("[]")
    rc = main(["--offline",
               "--history", str(tmp_path / "history.json"),
               "--events", str(tmp_path / "events.json"),
               "--overrides", str(tmp_path / "overrides.json"),
               "--out", str(tmp_path / "out.json"),
               "--feed", str(tmp_path / "feed.xml")])
    assert rc == 0
    out = json.loads((tmp_path / "out.json").read_text())
    assert out["counts"]["total"] == 3
    ET.parse(tmp_path / "feed.xml")


# ---- invariants on the real repository data ----

@pytest.mark.skipif(not (REPO / "data/history.json").exists(), reason="repository data not present")
def test_real_history_rebuild_invariants():
    history = json.loads((REPO / "data/history.json").read_text())
    events = json.loads((REPO / "data/price-events.json").read_text())
    out = build_output(history, events)
    assert out["counts"]["total"] == len(out["models"]) >= 50
    names = {r[0] for r in out["models"]}
    dates = [a["date"] for a in out["advances"]]
    assert dates == sorted(dates, reverse=True)
    for a in out["advances"]:
        assert a["model"] in names
        assert a["owns_from"] <= a["owns_to"]
    for records in [out["tier_cost"]] + list(out["cap_tier_cost"].values()):
        for recs in records.values():
            costs = [r[1] for r in recs]
            assert costs == sorted(costs, reverse=True)
            assert all(a[0] <= b[0] for a, b in zip(recs, recs[1:]))
    for key, advs in out["cap_advances"].items():
        for a in advs:
            assert a["model"] in names
    json.dumps(out)

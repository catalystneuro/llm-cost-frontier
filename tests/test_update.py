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
    o = source_object(terminalbenchV21=0.789, omniscience=-10.76, automationBenchPartialScore=0.444)
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
        ["2026-01-01", 5.0, 40.0, None, "at launch price"],
        ["2026-02-15", 1.0, 40.0, None, "price cut (released 2026-01-01)"],
    ]
    late = model("Alpha 2", "2026-03-01", 40.0, 1.0)
    assert cost_changes("alpha-2", late, events) == [["2026-03-01", 1.0, 40.0, None, None]]


def test_cost_changes_dates_observed_changes():
    m = model("M", "2026-01-01", 50.0, 0.4,
              obs=[["2026-01-01", 0.5, 50.0], ["2026-06-01", 0.4, 50.0]])
    changes = cost_changes("m", m, [])
    assert [c[:2] for c in changes] == [["2026-01-01", 0.5], ["2026-06-01", 0.4]]
    assert "price change observed" in changes[1][4]


def test_cost_changes_records_index_moves_without_cost_moves():
    m = model("M", "2026-01-01", 45.0, 0.5,
              obs=[["2026-01-01", 0.5, 50.0], ["2026-09-05", 0.5, 45.0]])
    changes = cost_changes("m", m, [])
    assert [(c[0], c[2]) for c in changes] == [("2026-01-01", 50.0), ("2026-09-05", 45.0)]
    assert changes[1][4] is None  # an index move alone is not a price change


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
    assert out["60"]["collapse"] == 1.0 and out["60"]["halving_days"] is None
    assert out["70"] is None


def test_tier_summary_pools_declines_across_eras():
    # Old era: 1.0 -> 0.25 over 60 days (two halvings). New era: the cost
    # basis resets to 0.6, then falls to 0.3 over 40 days (one halving).
    # The ratio 0.25 -> 0.6 across the boundary must contribute nothing.
    recs = {"40": [["2026-01-01", 1.0, "A", 45.0], ["2026-03-02", 0.25, "C", 46.0],
                   ["2026-09-05", 0.6, "C", 41.0], ["2026-10-15", 0.3, "D", 42.0]]}
    out = tier_summary(recs, eras=ERAS)["40"]
    assert out["first_date"] == "2026-01-01" and out["last_date"] == "2026-10-15"
    assert out["collapse"] == 8.0  # 4x within the old era times 2x within the new
    assert out["halving_days"] == 33  # (60 + 40) days per 3 halvings


def test_tier_summary_ignores_a_cost_rise_at_the_boundary_only():
    # A single new-era record after an old-era decline: the higher new-suite
    # cost is not a regression, and the old decline still sets the estimate.
    recs = {"40": [["2026-01-01", 1.0, "A", 45.0], ["2026-03-02", 0.25, "C", 46.0],
                   ["2026-09-05", 0.6, "C", 41.0]]}
    out = tier_summary(recs, eras=ERAS)["40"]
    assert out["collapse"] == 4.0
    assert out["halving_days"] == 30
    assert out["last_cost"] == 0.6  # the current record is still the new-era one


def test_capability_models_substitutes_scores():
    models = {
        "a": model("A", "2026-01-01", 40.0, 1.0, caps={"coding": 80.0}),
        "b": model("B", "2026-02-01", 50.0, 2.0),
    }
    cm = capability_models(models, "coding")
    assert set(cm) == {"a"}
    assert cm["a"]["intelligence_index"] == 80.0
    assert models["a"]["intelligence_index"] == 40.0  # original untouched


# ---- index eras ----

ERAS = [{"start": "2026-09-05", "note": "index recomposed"}]


def era_history():
    """Two old-era models; one re-scored under the new index, one not."""
    return {
        "stale": model("Stale", "2026-01-01", 55.0, 0.10),
        "fresh": model("Fresh", "2026-02-01", 50.0, 0.20,
                       obs=[["2026-02-01", 0.20, 50.0], ["2026-09-05", 0.30, 42.0]]),
    }


def test_model_era_follows_the_last_observation():
    models = era_history()
    from llm_cost_frontier.update import model_era
    assert model_era(models["stale"], ERAS) == 0
    assert model_era(models["fresh"], ERAS) == 1


def test_tier_records_reset_at_the_era_boundary():
    models = era_history()
    recs = tier_records(models, [], tiers=[40], eras=ERAS)["40"]
    # Old era: Stale set the record at 0.10. New era: only Fresh competes,
    # and its higher new-suite cost is a fresh record, not compared to 0.10.
    assert [(r[0], r[1], r[2]) for r in recs] == [
        ("2026-01-01", 0.10, "Stale"), ("2026-09-05", 0.30, "Fresh")]


def test_stale_models_cannot_hold_new_era_records():
    models = era_history()
    recs = tier_records(models, [], tiers=[50], eras=ERAS)["50"]
    # Stale's 55.0 is an old-era score; Fresh's new score is 42. Nothing
    # reaches new-50, so the tier has no record after the boundary.
    assert [r[2] for r in recs] == ["Stale"]


def test_rebase_observations_produce_no_advances():
    models = era_history()
    advances = frontier_advances(models, [], {}, eras=ERAS)
    assert all(a["date"] < "2026-09-05" for a in advances)


def test_models_released_after_the_boundary_still_advance():
    models = era_history()
    models["newcomer"] = model("Newcomer", "2026-09-06", 45.0, 0.05,
                               obs=[["2026-09-06", 0.05, 45.0]])
    advances = frontier_advances(models, [], {}, eras=ERAS)
    new = [a for a in advances if a["date"] >= "2026-09-05"]
    assert [a["model"] for a in new] == ["Newcomer"]
    # It competes only against re-scored models: Stale's old 55 does not
    # block it from the ceiling.
    assert new[0]["ceiling_from"] == 42.0


def test_post_boundary_price_cut_still_advances():
    models = era_history()
    models["fresh"]["observations"].append(["2026-09-10", 0.25, 42.0])
    advances = frontier_advances(models, [], {}, eras=ERAS)
    cut = [a for a in advances if a["kind"] == "price change"]
    assert [(a["date"], a["previous_cost"]) for a in cut] == [("2026-09-10", 0.30)]


def test_readded_models_do_not_leak_old_scores_into_the_new_era():
    # "Readded" is dropped at the boundary and only re-measured on Sep 7, so
    # between the boundary and its re-measurement it must not sit on the
    # frontier at its old score, and its re-measurement is not an advance.
    models = era_history()
    models["readded"] = model("Readded", "2026-03-01", 60.0, 0.05,
                              obs=[["2026-03-01", 0.05, 60.0], ["2026-09-07", 0.40, 45.0]])
    models["newcomer"] = model("Newcomer", "2026-09-06", 44.0, 0.10,
                               obs=[["2026-09-06", 0.10, 44.0]])
    advances = frontier_advances(models, [], {}, eras=ERAS)
    new = {a["model"]: a for a in advances if a["date"] >= "2026-09-05"}
    assert set(new) == {"Newcomer"}
    # Newcomer pushed the ceiling above Fresh's 42; Readded's old 60 is gone.
    assert new["Newcomer"]["ceiling_from"] == 42.0


def test_mass_move_dates_detects_correlated_shifts():
    from llm_cost_frontier.update import mass_move_dates
    models = {}
    for i in range(10):
        models[f"m{i}"] = model(f"M{i}", "2026-01-01", 30.0 + i, 1.0,
                                obs=[["2026-01-01", 1.0, 30.0 + i], ["2026-06-01", 1.2, 30.0 + i]])
    assert mass_move_dates(models, []) == ["2026-06-01"]


def test_past_era_baseline_settles_after_a_mass_remeasurement():
    from llm_cost_frontier.update import era_snapshots
    # A frozen past era's baseline moves to its settled measurements when a
    # mass re-measurement follows its start within the settling window.
    models = {}
    for i in range(10):
        models[f"m{i}"] = model(f"M{i}", "2026-01-01", 40.0 + i, 1.0,
                                obs=[["2026-01-01", 1.0, 50.0 + i], ["2026-05-01", 1.5, 40.0 + i],
                                     ["2026-05-03", 2.0, 40.0 + i], ["2026-09-05", 2.5, 40.0 + i]])
    eras = [{"start": "2026-05-01"}, {"start": "2026-09-05"}]
    out = era_snapshots(eras, dt.date(2026, 9, 9), models, [])
    assert out[1][0] == ["2026-05-03", "May 3, 2026"]
    assert out[1][-1] == ["2026-09-04", "Sep 4, 2026"]


def test_mass_cost_moves_are_not_price_changes():
    # Ten models re-measured 20% more expensive on one day is a suite change;
    # one model's lone deep cut on another day is a price change.
    models = {}
    for i in range(10):
        models[f"m{i}"] = model(f"M{i}", "2026-01-01", 30.0 + i, 1.0,
                                obs=[["2026-01-01", 1.0, 30.0 + i], ["2026-06-01", 1.2, 30.0 + i]])
    models["m9"]["observations"].append(["2026-07-01", 0.5, 39.0])
    advances = frontier_advances(models, [], {})
    kinds = [(a["date"], a["kind"]) for a in advances if a["kind"] == "price change"]
    assert kinds == [("2026-07-01", "price change")]


def test_rebased_models_scale_history_onto_the_current_basis():
    from llm_cost_frontier.update import rebased_models
    models = era_history()
    # Fresh: old obs 0.20 at score 50 (v-old), current 0.30 at 42. A pre-era
    # price of 0.10 (half the era-end 0.20) rebases to half of today's 0.30.
    models["fresh"]["observations"] = [["2026-02-01", 0.10, 50.0], ["2026-06-01", 0.20, 50.0],
                                       ["2026-09-05", 0.30, 42.0]]
    models["fresh"]["cost_per_task"] = 0.30
    models["fresh"]["intelligence_index"] = 42.0
    out = rebased_models(models, ERAS)
    assert "stale" not in out  # never measured on the current basis
    robs = out["fresh"]["observations"]
    assert robs[0] == ["2026-02-01", 0.15, 42.0]
    assert robs[1] == ["2026-06-01", 0.30, 42.0]
    assert robs[2] == ["2026-09-05", 0.30, 42.0]
    recs = tier_records(out, [], tiers=[40])["40"]
    assert [(r[0], r[1]) for r in recs] == [("2026-02-01", 0.15)]  # continuous, no reset


def test_time_rides_observations_and_big_moves_append():
    history = {"updated": "2026-09-01", "models": {"m": model("M", "2026-01-01", 50.0, 1.0)}}
    live = live_record("M", "2026-01-01", 50.0, 0.8)
    live["time_per_task"] = 120.0
    merge(history, {"m": live}, "2026-09-02")
    m = history["models"]["m"]
    assert m["observations"][-1] == ["2026-09-02", 0.8, 50.0, 120.0]
    assert m["time_per_task"] == 120.0
    # A 25% speedup with unchanged price and score appends its own row.
    live2 = live_record("M", "2026-01-01", 50.0, 0.8)
    live2["time_per_task"] = 90.0
    merge(history, {"m": live2}, "2026-09-03")
    m = history["models"]["m"]
    assert m["observations"][-1] == ["2026-09-03", 0.8, 50.0, 90.0]
    # A small wiggle does not.
    live3 = live_record("M", "2026-01-01", 50.0, 0.8)
    live3["time_per_task"] = 95.0
    merge(history, {"m": live3}, "2026-09-04")
    assert history["models"]["m"]["observations"][-1][0] == "2026-09-03"


def test_time_models_project_onto_the_time_axis():
    from llm_cost_frontier.update import time_models
    models = {
        "a": model("A", "2026-01-01", 45.0, 1.0),
        "b": model("B", "2026-02-01", 50.0, 2.0,
                   obs=[["2026-02-01", 2.0, 50.0], ["2026-06-01", 2.0, 50.0, 300.0]]),
    }
    models["a"]["time_per_task"] = 60.0
    models["b"]["time_per_task"] = 300.0
    tm = time_models(models, [])
    # A has no dated times: its latest time stands across its life.
    assert [o[1] for o in tm["a"]["observations"]] == [60.0]
    # B's dated time is used from its date; earlier rows fall back to it.
    assert [o[1] for o in tm["b"]["observations"]] == [300.0, 300.0]
    recs = tier_records(tm, [], tiers=[40])["40"]
    assert [(r[0], r[1]) for r in recs] == [("2026-01-01", 60.0)]


def test_backfill_time_series_records_times_and_respects_cutoff():
    from llm_cost_frontier.update import backfill_time_series, dated_times
    m = model("M", "2026-07-01", 50.0, 1.0,
              obs=[["2026-07-01", 1.0, 50.0], ["2026-08-15", 0.8, 50.0], ["2026-09-11", 0.8, 45.0, 200.0]])
    history = {"updated": "2026-09-11", "models": {"m": m}}
    series = {
        "2026-07-10": {"m": 100.0},   # no row that day: goes to time_history
        "2026-07-20": {"m": 105.0},   # within 20% of the last record: skipped
        "2026-08-15": {"m": 140.0},   # annotates the existing row
        "2026-09-06": {"m": 90.0},    # at/after the cutoff: never touched
    }
    n = backfill_time_series(history, series, cutoff="2026-09-05")
    m = history["models"]["m"]
    assert n == 2
    # The cost timeline gained no rows; only the annotation and the side record.
    assert [o[0] for o in m["observations"]] == ["2026-07-01", "2026-08-15", "2026-09-11"]
    assert m["observations"][1] == ["2026-08-15", 0.8, 50.0, 140.0]
    assert m["time_history"] == [["2026-07-10", 100.0]]
    assert dated_times(m) == {"2026-07-10": 100.0, "2026-08-15": 140.0, "2026-09-11": 200.0}
    # A second run writes nothing.
    assert backfill_time_series(history, series, cutoff="2026-09-05") == 0


def test_backfill_never_disturbs_the_price_event_timeline():
    from llm_cost_frontier.update import backfill_time_series
    events = [{"slug_prefix": "m", "cut_date": "2026-07-30", "multiplier_before": 5.0}]
    m = model("M", "2026-06-01", 50.0, 1.0, obs=[["2026-08-19", 1.0, 50.0]])
    history = {"updated": "2026-09-11", "models": {"m": m}}
    before = cost_changes("m", m, events)
    n = backfill_time_series(history, {"2026-07-08": {"m": 60.0}}, "2026-09-05")
    assert n == 1
    m = history["models"]["m"]
    assert m["time_history"] == [["2026-07-08", 60.0]]
    # The reconstructed price timeline is identical: the hand-recorded cut
    # keeps its date and the launch price stays backdated to release.
    assert cost_changes("m", m, events) == before


def test_era_time_models_scopes_times_to_one_era():
    from llm_cost_frontier.update import era_time_models
    m = model("M", "2026-02-01", 50.0, 1.0,
              obs=[["2026-02-01", 1.0, 55.0], ["2026-07-10", 1.0, 55.0, 100.0],
                   ["2026-08-15", 0.8, 55.0, 140.0], ["2026-09-11", 0.8, 45.0, 200.0]])
    m["time_history"] = [["2026-07-20", 130.0]]
    out = era_time_models({"m": m}, ERAS, 0)
    assert out["m"]["observations"] == [["2026-07-10", 100.0, 55.0], ["2026-07-20", 130.0, 55.0], ["2026-08-15", 140.0, 55.0]]
    # Era records begin when measuring began, not at the model's release.
    assert out["m"]["release_date"] == "2026-07-10"
    assert out["m"]["intelligence_index"] == 55.0
    # A model with no era measurements is absent.
    assert era_time_models({"m": model("N", "2026-02-01", 50.0, 1.0)}, ERAS, 0) == {}


def test_time_models_ignore_old_era_measurements():
    from llm_cost_frontier.update import time_models
    m = model("M", "2026-02-01", 50.0, 1.0,
              obs=[["2026-02-01", 1.0, 55.0], ["2026-07-10", 1.0, 55.0, 100.0], ["2026-09-12", 0.8, 50.0]])
    m["time_per_task"] = None
    # Only a v4.1 time exists: the model has no time on the current basis.
    assert time_models({"m": m}, ERAS) == {}
    # With a current-era time, the old-era measurement still never leaks in.
    m2 = model("M", "2026-02-01", 50.0, 1.0,
               obs=[["2026-02-01", 1.0, 55.0], ["2026-07-10", 1.0, 55.0, 100.0], ["2026-09-12", 0.8, 50.0, 300.0]])
    m2["time_per_task"] = 300.0
    tm = time_models({"m": m2}, ERAS)
    assert all(o[1] == 300.0 for o in tm["m"]["observations"])


def test_backfilled_rows_are_not_frontier_events():
    a = model("A", "2026-01-01", 50.0, 1.0, obs=[["2026-01-01", 1.0, 50.0], ["2026-02-10", 1.0, 50.0, 90.0]])
    b = model("B", "2026-02-10", 48.0, 2.0)
    models = {"a": a, "b": b}
    records = tier_records(models, [])
    advs = frontier_advances(models, [], records)
    assert all(not (x["model"] == "A" and x["date"] == "2026-02-10") for x in advs)


def test_metric_pages_stay_in_sync_with_the_template():
    from llm_cost_frontier import pages
    for path, content in pages.generate().items():
        assert path.exists(), f"{path} missing; run python -m llm_cost_frontier.pages"
        assert path.read_text() == content, (
            f"{path} is stale; site/index.html changed without re-running "
            "python -m llm_cost_frontier.pages")


def test_capability_feed_speaks_the_metric():
    import tempfile
    from pathlib import Path as P
    from llm_cost_frontier.update import CAPABILITIES, write_feed
    cap = CAPABILITIES[0]
    out = {"updated": "2026-09-11", "advances": [],
           "cap_advances": {cap["key"]: [dict(
               date="2026-09-03", model="M (high)", base="M", variant="high", slug="m-high",
               creator="X", intelligence_index=89.9, cost_per_task=1.72, previous_cost=None,
               kind="new model", open_weights=False, owns_from=80.0, owns_to=89.9,
               records=[80], taken_from=[], ceiling_from=None, displaced=[])]}}
    with tempfile.TemporaryDirectory() as td:
        fp = P(td) / f"feed-{cap['key']}.xml"
        write_feed(out, fp, "https://example.com", cap=cap)
        t = fp.read_text()
    assert f"LLM Frontier: {cap['metric']} advances" in t
    assert f"https://example.com/feed-{cap['key']}.xml" in t
    assert f"{cap['key']}/advance/2026-09-03/m-high" in t
    assert f"{cap['metric']} 89.9%" in t
    assert f"New cost record for {cap['metric']} \u2265 80%" in t


def test_check_live_set_guards():
    from llm_cost_frontier.update import check_live_set
    history = {"models": {f"m{i}": model(f"M{i}", "2026-01-01", 50.0, 1.0) for i in range(100)}}
    live_same = {f"m{i}": live_record(f"M{i}", "2026-01-01", 50.0, 1.0) for i in range(100)}
    check_live_set(live_same, history, [], "2026-09-06")  # no complaint

    with pytest.raises(RuntimeError, match="only 30 live models"):
        check_live_set(dict(list(live_same.items())[:30]), history, [], "2026-09-06")

    with pytest.raises(RuntimeError, match="live set dropped"):
        check_live_set(dict(list(live_same.items())[:60]), history, [], "2026-09-06")

    shifted = {s: live_record(r["name"], "2026-01-01", 45.0, 1.0) for s, r in live_same.items()}
    with pytest.raises(RuntimeError, match="median index shift"):
        check_live_set(shifted, history, [], "2026-09-06")

    # A declared era within a week waives the relative checks.
    check_live_set(shifted, history, ERAS, "2026-09-06")
    check_live_set(dict(list(live_same.items())[:60]), history, ERAS, "2026-09-06")


def test_era_snapshots_split_at_the_boundary():
    from llm_cost_frontier.update import era_snapshots
    out = era_snapshots(ERAS, dt.date(2026, 9, 6))
    assert len(out) == 2
    old, new = out
    # The old era ends the day before the boundary, labeled with its date.
    assert old[-1] == ["2026-09-04", "Sep 4, 2026"]
    assert all(d < "2026-09-05" for d, _ in old)
    # The current era carries the full bi-monthly history: the dashboard
    # reconstructs earlier frontiers on the current basis.
    assert new == snapshots(dt.date(2026, 9, 6))
    # Without eras there is a single list equivalent to snapshots().
    assert era_snapshots([], dt.date(2026, 9, 6)) == [snapshots(dt.date(2026, 9, 6))]


def test_build_output_with_eras():
    history = {"updated": "2026-09-06", "models": era_history()}
    out = build_output(history, events=[], eras=ERAS)
    assert out["eras"] == [["2026-09-05", "index recomposed", "", "", "2026-09-05"]]
    by_name = {r[0]: r for r in out["models"]}
    assert by_name["Stale"][9] == 0 and by_name["Fresh"][9] == 1
    assert all(a["date"] < "2026-09-05" for a in out["advances"])
    s = out["tier_summary"]["40"]
    # First crossed in the old era, current record in the new; the collapse
    # pools within-era declines, which are both flat here.
    assert s["first_date"] == "2026-01-01" and s["last_date"] == "2026-09-05"
    assert s["collapse"] == 1.0 and s["halving_days"] is None


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
        assert len(row) == 13
        assert len(row[8]) == len(CAPABILITIES)
        assert row[9] == 0  # no eras declared
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

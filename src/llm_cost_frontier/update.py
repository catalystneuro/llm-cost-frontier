"""Track the Pareto frontier of LLM intelligence against measured cost per task.

Any model page on artificialanalysis.ai embeds the full comparison dataset for
every currently benchmarked model, including the measured cost per Intelligence
Index task. This module fetches one page, parses that payload, merges it into a
cumulative history, applies known price events, and writes the JSON and Atom
feed that the dashboard at catalystneuro.com/llm-cost-frontier/ renders.

The history keeps every observed (date, cost, index) per model, appending an
observation whenever a value changes. Snapshots and tier records use the cost in
effect on each date, so price changes that happen after a model's release are
dated to when they were observed instead of being back-dated to the release.
Models that leave the live set keep their observations and are marked retired.

Standard library only, so it runs unattended in GitHub Actions.
"""
import argparse
import datetime as dt
import json
import math
import re
import urllib.request
from pathlib import Path

DEFAULT_HISTORY = Path("data/history.json")
DEFAULT_EVENTS = Path("data/price-events.json")
DEFAULT_OVERRIDES = Path("data/overrides.json")
DEFAULT_ERAS = Path("data/eras.json")
DEFAULT_OUTPUT = Path("build/llm-frontier.json")
DEFAULT_FEED = Path("build/feed.xml")
DEFAULT_SITE = "https://catalystneuro.com"
FEED_ENTRIES = 60

# Any model page works; this one is stable and cheap to serve.
SOURCE_PAGE = "https://artificialanalysis.ai/models/gpt-5-6-luna-xhigh"
TIERS = [30, 40, 50, 60]
SNAPSHOT_COUNT = 8
SNAPSHOT_MONTHS = 2
# A price change on a model already on the frontier is only reported as an
# advance when it moved by at least this much, so sub-cent wiggles from
# nightly cost measurement don't flood the advances list and the feed.
MIN_PRICE_MOVE = 0.02
# A date on which the measured cost moved by more than 10% for many models at
# once is a re-measurement of the evaluation suite, not a wave of price
# changes, and produces no price-change advances. September 7, 2026, when 129
# models moved together days after the v4.3 recomposition, is the archetype.
MASS_MOVE_MIN = 6
MASS_MOVE_FRACTION = 0.15
# A new index's measurements settle over its first days (v4.3 was first
# measured on September 5 and revised en masse on September 7). An era's
# baseline snapshot uses the last mass re-measurement within this many days
# of the era's start, so baselines compare against settled values.
SETTLE_DAYS = 7

# Per-capability metrics read from the same payload, each chosen because it
# translates to a class of application better than the aggregate index does.
# "percent" metrics arrive as 0-1 fractions and are stored as 0-100. Metrics
# measured for only a small fraction of models (LiveCodeBench, AIME) are left
# out. The blurb is shown on the dashboard when the capability's tab is active.
CAPABILITIES = [
    dict(key="coding", field="terminalbenchV21", label="Coding", metric="Terminal-Bench 2.1", percent=True,
         blurb="Completion rate on Terminal-Bench 2.1: real software engineering tasks run agentically in a terminal. The axis to watch when picking a model for a coding assistant or an autonomous software agent."),
    # agenticIndex was removed when AA recomposed the index (v4.3, September
    # 2026); AutomationBench-AA is its successor for tool use and multi-step
    # task completion. The key stays "agentic" so the tab and links carry over.
    dict(key="agentic", field="automationBenchPartialScore", label="Agentic Tool Use", metric="AutomationBench-AA", percent=True,
         blurb="Score on AutomationBench-AA, Artificial Analysis's benchmark of tool calling and multi-step task completion, which replaced their Agentic Index in September 2026. Relevant for models that orchestrate tools and workflows rather than answer single prompts."),
    dict(key="longcontext", field="lcr", label="Long Context", metric="AA-LCR", percent=True,
         blurb="Accuracy on Artificial Analysis's long context reasoning suite, which requires answers grounded in roughly 100k tokens of source material. Relevant for document analysis, retrieval pipelines, and codebase-scale prompts."),
    dict(key="instruction", field="ifbench", label="Instruction Following", metric="IFBench", percent=True,
         blurb="Accuracy on IFBench, which checks precise compliance with constraints on the output. Relevant for structured output, templated generation, and any pipeline that parses what the model returns."),
    dict(key="knowledge", field="omniscience", label="Factual Recall", metric="AA Omniscience", percent=False,
         blurb="Artificial Analysis's Omniscience index: factual recall with hallucinated answers penalized, on a scale from -100 to 100, where zero means as many hallucinated answers as correct ones. Relevant for question answering and customer-facing assistants, where a made-up answer is worse than no answer."),
    dict(key="science", field="gpqa", label="Scientific Reasoning", metric="GPQA Diamond", percent=True,
         blurb="Accuracy on GPQA Diamond, graduate-level science questions written to resist lookup. Relevant for research assistance and technical question answering."),
    dict(key="knowledgework", field="gdpvalNormalized", label="Knowledge Work", metric="GDPval-AA", percent=True,
         blurb="Artificial Analysis's automated grading of GDPval deliverables: documents, spreadsheets, slides, and analysis drawn from real occupational tasks. Relevant for office work products beyond chat."),
    dict(key="multimodal", field="mmmuPro", label="Multimodal", metric="MMMU-Pro", percent=True,
         blurb="Accuracy on MMMU-Pro, college-level problems that require reading images, diagrams, and figures. Relevant for applications with visual input."),
]


def fetch_payload(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (catalystneuro.com llm-frontier updater)"})
    html = urllib.request.urlopen(req, timeout=120).read().decode("utf-8")
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html)
    if not chunks:
        raise RuntimeError("no Next.js payload found on page; the site layout may have changed")
    return "".join(json.loads('"' + c + '"') for c in chunks)


def parse_object_at(s: str, start: int) -> dict:
    depth = 0
    in_str = False
    esc = False
    k = start
    while k < len(s):
        ch = s[k]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(s[start : k + 1])
        k += 1
    raise ValueError("unterminated object")


def enclosing_object(s: str, idx: int) -> dict:
    depth = 0
    j = idx
    while j >= 0:
        c = s[j]
        if c == "}":
            depth += 1
        elif c == "{":
            if depth == 0:
                return parse_object_at(s, j)
            depth -= 1
        j -= 1
    raise ValueError("no enclosing object")


def extract_models(payload: str) -> dict:
    models = {}
    for m in re.finditer(r'"intelligenceIndexCostPerTask"', payload):
        o = enclosing_object(payload, m.start())
        cost = ((o.get("intelligenceIndexCostPerTask") or {}).get("cost") or {}).get("total")
        iq = o.get("intelligenceIndex")
        if cost is None or iq is None or not o.get("releaseDate") or not o.get("slug"):
            continue
        if float(cost) <= 0:
            continue  # free or promotional endpoints distort the cost axis
        caps = {}
        for cap in CAPABILITIES:
            v = o.get(cap["field"])
            if v is not None:
                caps[cap["key"]] = round(float(v) * 100, 1) if cap["percent"] else round(float(v), 1)
        models[o["slug"]] = dict(
            name=o["name"],
            creator=(o.get("creator") or {}).get("name") or "",
            release_date=o["releaseDate"][:10],
            intelligence_index=round(float(iq), 1),
            cost_per_task=round(float(cost), 6),
            open_weights=bool(o.get("isOpenWeights")),
            deprecated=bool(o.get("deprecated")),
            capabilities=caps,
        )
    return models


def merge(history: dict, live: dict, today: str) -> dict:
    models = history["models"]
    for slug, rec in live.items():
        prev = models.get(slug, {})
        obs = list(prev.get("observations") or [])
        if not obs and prev.get("cost_per_task") is not None:
            obs = [[prev.get("last_seen", today), prev["cost_per_task"], prev["intelligence_index"]]]
        last = obs[-1] if obs else None
        if last is None or abs(last[1] - rec["cost_per_task"]) > 1e-9 or abs(last[2] - rec["intelligence_index"]) > 0.049:
            obs.append([today, rec["cost_per_task"], rec["intelligence_index"]])
        models[slug] = dict(
            name=rec["name"],
            creator=rec["creator"] or prev.get("creator", ""),
            release_date=rec["release_date"],
            intelligence_index=rec["intelligence_index"],
            cost_per_task=rec["cost_per_task"],
            open_weights=rec["open_weights"],
            capabilities=rec.get("capabilities") or prev.get("capabilities") or {},
            retired=False,
            first_seen=prev.get("first_seen", today),
            last_seen=today,
            observations=obs,
        )
    for slug, rec in models.items():
        if slug not in live:
            rec["retired"] = True
            if not rec.get("observations"):
                rec["observations"] = [[rec.get("last_seen", today), rec["cost_per_task"], rec["intelligence_index"]]]
    history["updated"] = today
    return history


def apply_overrides(models: dict, overrides: dict) -> None:
    """Hand-maintained corrections for fields the upstream data gets wrong.

    Applied to the in-memory models when building the outputs, never to the
    stored history, which stays a faithful record of what the source reports.
    """
    for slug, fix in (overrides.get("open_weights") or {}).items():
        if slug in models:
            models[slug]["open_weights"] = bool(fix["value"])
        else:
            print(f"warning: open_weights override for unknown slug {slug!r}")


def add_months(d: dt.date, months: int) -> dt.date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    day = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return dt.date(y, m, day)


def snapshots(today: dt.date) -> list:
    """Pareto frontier snapshots on the first of every second month, ending with
    the current frontier on the update date."""
    first = today.replace(day=1)
    if first == today:
        first = add_months(first, -1)
    out = []
    for i in range(SNAPSHOT_COUNT - 2, -1, -1):
        d = add_months(first, -SNAPSHOT_MONTHS * i)
        out.append([d.isoformat(), d.strftime("%b %-d, %Y")])
    out.append([today.isoformat(), "today"])
    return out


def era_snapshots(eras: list, today: dt.date, models: dict = None, events: list = None) -> list:
    """Per era, the bi-monthly snapshot dates falling inside it, ending with
    the era's last day (labeled with its date) or with today for the current
    era. The dashboard renders one frontier chart per era from these, since
    index scores are only comparable within an era.

    Each era after the first opens with a baseline snapshot: the day its
    measurement basis settled. That is the era's start, unless a mass
    re-measurement followed within SETTLE_DAYS (a new suite's first values
    are often revised en masse days later), in which case the last such
    re-measurement is the baseline, so the current frontier is compared
    against settled values rather than a basis that no longer exists."""
    starts = [dt.date.fromisoformat(e["start"]) for e in eras or []]
    mass = mass_move_dates(models, events or [], eras) if models else []
    out = []
    for i in range(len(starts) + 1):
        end = today if i == len(starts) else starts[i] - dt.timedelta(days=1)
        first = end.replace(day=1)
        if first == end:
            first = add_months(first, -1)
        snaps = []
        base = None
        if i > 0:
            base = starts[i - 1]
            for d in mass:
                dd = dt.date.fromisoformat(d)
                if starts[i - 1] <= dd <= min(end, starts[i - 1] + dt.timedelta(days=SETTLE_DAYS)):
                    base = dd
            if base < end:
                snaps.append([base.isoformat(), base.strftime("%b %-d, %Y")])
        for k in range(SNAPSHOT_COUNT - 2, -1, -1):
            d = add_months(first, -SNAPSHOT_MONTHS * k)
            if d > end or (base is not None and d <= base):
                continue
            snaps.append([d.isoformat(), d.strftime("%b %-d, %Y")])
        if snaps and snaps[-1][0] == end.isoformat():
            snaps.pop()
        snaps.append([end.isoformat(), "today" if i == len(starts) else end.strftime("%b %-d, %Y")])
        out.append(snaps)
    return out


def era_index(date: str, eras: list) -> int:
    """Number of era boundaries at or before the date; 0 means before the first.

    An era begins when the source recomposes the Intelligence Index (or
    otherwise breaks comparability); eras are declared by hand in eras.json.
    Scores and measured costs are only comparable within one era.
    """
    return sum(1 for e in eras or [] if e["start"] <= date)


def model_era(m: dict, eras: list) -> int:
    """The era a model's current scores belong to: the era of its last
    observation. A model retired before a boundary keeps pre-boundary scores
    forever, so it never competes in later eras."""
    obs = m.get("observations") or []
    last = obs[-1][0] if obs else m.get("last_seen", m["release_date"])
    return era_index(last, eras)


def cost_changes(slug: str, m: dict, events: list) -> list:
    """Dated changes for one model: [[date, cost, iq, note], ...], starting at
    release. An entry is added whenever the cost or the index moves, so the
    index a model had on a given date is recoverable."""
    obs = sorted(m.get("observations") or [[m.get("last_seen", m["release_date"]), m["cost_per_task"], m["intelligence_index"]]])
    first_cost, first_iq = obs[0][1], obs[0][2]
    out = []
    ev = next((e for e in events if slug.startswith(e["slug_prefix"])), None)
    if ev and m["release_date"] < ev["cut_date"]:
        out.append([m["release_date"], first_cost * ev["multiplier_before"], first_iq, "at launch price"])
        out.append([ev["cut_date"], first_cost, first_iq, f"price cut (released {m['release_date']})"])
    else:
        out.append([m["release_date"], first_cost, first_iq, None])
    for date, cost, iq in obs[1:]:
        if abs(cost - out[-1][1]) > 1e-9 or abs(iq - out[-1][2]) > 0.049:
            note = f"price change observed (released {m['release_date']})" if abs(cost - out[-1][1]) > 1e-9 else None
            out.append([date, cost, iq, note])
    return out


def price_timeline(models: dict, events: list) -> list:
    """All dated changes across models as (date, cost, slug, iq, note), with
    the cost and index in effect on each date."""
    out = []
    for slug, m in models.items():
        for date, cost, iq, note in cost_changes(slug, m, events):
            out.append([date, cost, slug, iq, note])
    out.sort(key=lambda e: (e[0], e[1]))
    return out


def tier_records(models: dict, events: list, tiers: list = None, eras: list = None) -> dict:
    """Running cost minimums per tier. The minimum resets at each era boundary,
    since neither the scores nor the measured costs are comparable across one."""
    timeline = price_timeline(models, events)
    starts = [e["start"] for e in eras or []]
    out = {}
    for t in TIERS if tiers is None else tiers:
        best = math.inf
        pending = list(starts)
        recs = []
        for date, cost, slug, iq, note in timeline:
            while pending and date >= pending[0]:
                best = math.inf
                pending.pop(0)
            if iq >= t and cost < best:
                best = cost
                recs.append([date, round(cost, 6), models[slug]["name"], iq] + ([note] if note else []))
        out[str(t)] = recs
    return out


def split_variant(name: str):
    """'GPT-5.6 Luna (xhigh)' -> ('GPT-5.6 Luna', 'xhigh'); names without a suffix return variant None."""
    m = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", name)
    if not m:
        return name, None
    return m.group(1), m.group(2)


def pareto(state: dict) -> set:
    """Slugs on the Pareto frontier of (max index, min cost) for the given {slug: (cost, iq)}."""
    out = set()
    items = list(state.items())
    for slug, (cost, iq) in items:
        dominated = any(o_iq >= iq and o_cost <= cost and (o_iq > iq or o_cost < cost) for o_slug, (o_cost, o_iq) in items if o_slug != slug)
        if not dominated:
            out.add(slug)
    return out


def mass_move_dates(models: dict, events: list, eras: list = None) -> list:
    """Dates on which the measured cost moved by more than 10% for many models
    at once: at least MASS_MOVE_MIN of them and MASS_MOVE_FRACTION of the
    models measured at that point. These are re-measurements of the evaluation
    suite, not waves of price changes."""
    timeline = price_timeline(models, events)
    by_date = {}
    for date, cost, slug, iq, note in timeline:
        by_date.setdefault(date, []).append((cost, slug))
    state = {}
    cur_era = 0
    out = []
    for date in sorted(by_date):
        ev_era = era_index(date, eras)
        if ev_era > cur_era:
            state = {}
            cur_era = ev_era
        movers = sum(1 for cost, slug in by_date[date]
                     if slug in state and state[slug] > 0 and abs(cost - state[slug]) / state[slug] > 0.10)
        if movers >= max(MASS_MOVE_MIN, MASS_MOVE_FRACTION * len(state)):
            out.append(date)
        for cost, slug in by_date[date]:
            state[slug] = cost
    return out


def frontier_advances(models: dict, events: list, records: dict, eras: list = None) -> list:
    """Dates on which the Pareto frontier changed, newest first.

    At an era boundary, models whose scores were never re-measured under the
    new index are dropped from the frontier, and each surviving model's first
    post-boundary observation is treated as a re-measurement, not an advance:
    it moves the frontier because the yardstick changed, which is not news
    about the model.
    """
    timeline = price_timeline(models, events)
    mass_days = set(mass_move_dates(models, events, eras))
    record_keys = {(r[0], r[2]): t for t, recs in records.items() for r in recs}
    state = {}
    current = set()
    advances = []
    by_date = {}
    for date, cost, slug, iq, note in timeline:
        by_date.setdefault(date, []).append((cost, slug, iq, note))
    cur_era = 0
    seen_era = {}
    for date in sorted(by_date):
        ev_era = era_index(date, eras)
        if ev_era > cur_era:
            # Everything measured so far predates the boundary, so nothing in
            # the state is comparable in the new era. Models re-enter at their
            # first in-era observation (a rebase), which also covers models the
            # source drops at the boundary and re-measures days later.
            state = {}
            current = set()
            cur_era = ev_era
        changed = {}
        state_before = dict(state)
        base_of = lambda o: split_variant(models[o]["name"])[0]
        mass = date in mass_days
        for cost, slug, iq, note in by_date[date]:
            prev = state.get(slug)
            state[slug] = (cost, iq)
            rebase = ev_era > 0 and seen_era.get(slug, -1) < ev_era and models[slug]["release_date"] < (eras or [])[ev_era - 1]["start"]
            seen_era[slug] = ev_era
            if rebase or (mass and prev is not None):
                continue
            changed[slug] = ("price change" if note and ("cut" in note or "change" in note) else "new model", prev[0] if prev else None)
        prev_front = current
        new_front = pareto(state)
        entered = [s for s in new_front if s in changed and (s not in current or (changed[s][0] == "price change" and changed[s][1] is not None and changed[s][1] - state[s][0] >= MIN_PRICE_MOVE))]
        # A model "leaves the frontier" only when none of its reasoning variants remains on it.
        remaining_bases = {base_of(o) for o in new_front}
        left_bases = {}
        for o in current - new_front:
            b = base_of(o)
            if b not in remaining_bases:
                left_bases.setdefault(b, []).append(o)
        # Attribute each departed base model to the entering model just above its highest variant in index.
        entered_sorted = sorted(entered, key=lambda s: state[s][1])
        attribution = {}
        for b, variants in left_bases.items():
            top_iq = max(state[o][1] for o in variants)
            above = [e for e in entered_sorted if state[e][1] >= top_iq]
            owner = above[0] if above else (entered_sorted[-1] if entered_sorted else None)
            if owner:
                attribution.setdefault(owner, []).append(b)
        for slug in sorted(entered, key=lambda s: -state[s][1]):
            cost, iq = state[slug]
            kind, prev_cost = changed[slug]
            tiers = [t for (d, name), t in record_keys.items() if d == date and name == models[slug]["name"]]
            # index range this model now owns: from its index down to the next frontier model below it
            below = [state[o][1] for o in new_front if state[o][1] < iq]
            lower = max(below) if below else 0.0
            # Which models covered the gained range before today. On the previous frontier, a target
            # index t was served by the member with the smallest index >= t; collect those for the range
            # (prev_lower, iq], excluding this model's own variants.
            my_base = base_of(slug)
            prev_cover = []
            prev_members = sorted([o for o in prev_front if base_of(o) != my_base], key=lambda o: -state[o][1])
            prev_own_range_lower = None
            if slug in prev_front:
                pb = [state[o][1] for o in prev_front if state[o][1] < state_before.get(slug, (None, iq))[1]]
                prev_own_range_lower = max(pb) if pb else 0.0
            gained_lower = lower
            gained_upper = iq if prev_own_range_lower is None else prev_own_range_lower
            for o in prev_members:
                o_iq = state[o][1]
                o_below = [state[q][1] for q in prev_front if state[q][1] < o_iq]
                o_lower = max(o_below) if o_below else 0.0
                if o_iq > gained_lower and o_lower < gained_upper:
                    b = base_of(o)
                    if b not in prev_cover:
                        prev_cover.append(b)
            taken_from = prev_cover
            prev_ceiling = max((state[o][1] for o in prev_front), default=None)
            ceiling_from = round(prev_ceiling, 1) if prev_ceiling is not None and iq > prev_ceiling else None
            base, variant = split_variant(models[slug]["name"])
            advances.append(dict(
                date=date, model=models[slug]["name"], base=base, variant=variant, creator=models[slug]["creator"], slug=slug,
                intelligence_index=iq, cost_per_task=round(cost, 6), previous_cost=round(prev_cost, 6) if prev_cost else None,
                kind=kind, open_weights=models[slug]["open_weights"],
                owns_from=round(lower, 1), owns_to=round(iq, 1),
                records=sorted(int(t) for t in tiers),
                taken_from=taken_from,
                ceiling_from=ceiling_from,
                displaced=sorted(attribution.get(slug, [])),
            ))
        current = new_front
    advances.sort(key=lambda a: (a["date"], a["intelligence_index"]), reverse=True)
    return advances


def capability_models(models: dict, key: str) -> dict:
    """The models measured on one capability, with the capability score standing
    in for the intelligence index so the frontier machinery applies unchanged.
    Observations are rewritten with the constant capability score, since only
    the index is versioned in the history; the cost history is kept."""
    out = {}
    for slug, m in models.items():
        v = (m.get("capabilities") or {}).get(key)
        if v is None:
            continue
        mm = dict(m)
        mm["intelligence_index"] = v
        mm["observations"] = [[d, c, v] for d, c, _iq in (m.get("observations") or [])]
        out[slug] = mm
    return out


def tier_summary(records: dict, eras: list = None) -> dict:
    """Collapse and halving time per tier, using the full record history.

    A cost ratio is only ever taken within one era, since a recomposition
    changes the cost basis; each era contributes its own decline (in log2)
    and its own span of days, and the contributions are pooled. The collapse
    is the product of the within-era ratios, and the halving time is the
    pooled days per pooled halving, so older eras keep informing the estimate
    without a cost ever being compared across a boundary."""
    out = {}
    for t, recs in records.items():
        if not recs:
            out[t] = None
            continue
        first, last = recs[0], recs[-1]
        groups = {}
        for r in recs:
            groups.setdefault(era_index(r[0], eras), []).append(r)
        days = 0
        log_drop = 0.0
        for g in groups.values():
            days += (dt.date.fromisoformat(g[-1][0]) - dt.date.fromisoformat(g[0][0])).days
            log_drop += math.log2(g[0][1] / g[-1][1])
        out[t] = dict(
            first_date=first[0], first_model=first[2], first_cost=first[1],
            last_date=last[0], last_model=last[2], last_cost=last[1],
            collapse=round(2 ** log_drop, 1),
            halving_days=round(days / log_drop) if log_drop > 0 and days else None,
        )
    return out


def build_output(history: dict, events: list, overrides: dict | None = None, eras: list | None = None) -> dict:
    today = dt.date.fromisoformat(history["updated"])
    models = history["models"]
    if overrides:
        apply_overrides(models, overrides)
    current_era = len(eras or [])
    rows = []
    for slug, m in sorted(models.items(), key=lambda kv: (kv[1]["release_date"], kv[1]["name"])):
        changes = [[d, round(c, 6), iq] for d, c, iq, _n in cost_changes(slug, m, events)]
        caps = m.get("capabilities") or {}
        row = [m["name"], m["creator"], m["release_date"], m["intelligence_index"], m["cost_per_task"], int(m["retired"]), int(m["open_weights"]),
               changes if len(changes) > 1 else 0,
               [caps.get(c["key"]) for c in CAPABILITIES],
               model_era(m, eras)]
        rows.append(row)
    records = tier_records(models, events, eras=eras)
    advances = frontier_advances(models, events, records, eras=eras)
    # Per-capability tiers are derived from each metric's range: the top four
    # multiples of ten at or below the highest score among models whose
    # measurements are current-era.
    cap_tiers, cap_tier_cost, cap_tier_summary, cap_advances = {}, {}, {}, {}
    for c in CAPABILITIES:
        cm = capability_models(models, c["key"])
        current = [m for m in cm.values() if model_era(m, eras) == current_era]
        if not current:
            continue
        hi = int(max(m["intelligence_index"] for m in current) // 10) * 10
        tiers = [t for t in (hi - 30, hi - 20, hi - 10, hi) if t > 0]
        recs = tier_records(cm, events, tiers, eras=eras)
        cap_tiers[c["key"]] = tiers
        cap_tier_cost[c["key"]] = recs
        cap_tier_summary[c["key"]] = tier_summary(recs, eras=eras)
        cap_advances[c["key"]] = frontier_advances(cm, events, recs, eras=eras)
    return dict(
        advances=advances,
        cap_advances=cap_advances,
        cap_tiers=cap_tiers,
        cap_tier_cost=cap_tier_cost,
        cap_tier_summary=cap_tier_summary,
        eras=[[e["start"], e.get("note", ""), e.get("label", ""), e.get("label_before", "")] for e in eras or []],
        era_snapshots=era_snapshots(eras, today, models, events),
        updated=history["updated"],
        source="Artificial Analysis (artificialanalysis.ai), measured cost per Intelligence Index task",
        snapshots=snapshots(today),
        tiers=TIERS,
        capabilities=[{k: c[k] for k in ("key", "label", "metric", "percent", "blurb")} for c in CAPABILITIES],
        models=rows,
        tier_cost=records,
        tier_summary=tier_summary(records, eras=eras),
        price_events=events,
        counts=dict(total=len(rows), live=sum(1 for m in models.values() if not m["retired"]), retired=sum(1 for m in models.values() if m["retired"])),
    )


def join_and(items: list) -> str:
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def taken_clause(taken: list, departed: list) -> str:
    """', taking it from A and B, both of which left the frontier' style clause."""
    out = ""
    if taken:
        out += ", taking it from " + join_and(taken)
        gone = [t for t in taken if t in departed]
        if gone and len(gone) == len(taken):
            out += ", which left the frontier" if len(taken) == 1 else (", both of which left the frontier" if len(taken) == 2 else ", all of which left the frontier")
        elif gone:
            out += "; " + join_and(gone) + (" left the frontier" if len(gone) > 1 else " left the frontier")
    extra = [d for d in departed if d not in taken]
    if extra:
        out += ("; " if out else "; ") + join_and(extra) + " left the frontier"
    return out


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def xml_escape(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def describe(a: dict) -> str:
    cost = f"${a['cost_per_task']:.4f}" if a["cost_per_task"] < 0.01 else f"${a['cost_per_task']:.3f}" if a["cost_per_task"] < 0.1 else f"${a['cost_per_task']:.2f}"
    if f"{a['owns_to']:.1f}" == f"{a['owns_from']:.1f}":
        span = f"index {a['owns_to']:.1f}"
    else:
        span = f"index {a['owns_from']:.1f} to {a['owns_to']:.1f}"
    if a["kind"] == "price change" and a["previous_cost"]:
        parts = [f"{a['model']}: price moved from ${a['previous_cost']:.3f} to {cost} per task; now the cheapest way to reach {span}."]
    elif a.get("ceiling_from") is not None:
        parts = [f"{a['model']}: pushed the intelligence ceiling from {a['ceiling_from']:.1f} to {a['owns_to']:.1f}, at {cost} per task."]
    else:
        parts = [f"{a['model']}: now the cheapest way to reach {span} at {cost} per task."]
    parts[0] = parts[0][:-1] + taken_clause(a.get("taken_from") or [], a.get("displaced") or []) + "."
    if a["records"]:
        parts.append("New cost record for " + join_and([f"index \u2265 {t}" for t in a["records"]]) + ".")
    parts.append("Open weights." if a["open_weights"] else "Proprietary.")
    return " ".join(parts)


def write_feed(out: dict, feed_path: Path, site: str) -> None:
    entries = out["advances"][:FEED_ENTRIES]
    updated = out["updated"] + "T06:00:00Z"
    lines = ['<?xml version="1.0" encoding="utf-8"?>', '<feed xmlns="http://www.w3.org/2005/Atom">',
             "  <title>LLM Cost Frontier: frontier advances</title>",
             f'  <link href="{site}/llm-cost-frontier/" />',
             f'  <link rel="self" href="{site}/llm-cost-frontier/feed.xml" />',
             f"  <id>{site}/llm-cost-frontier/feed.xml</id>",
             f"  <updated>{updated}</updated>",
             "  <author><name>CatalystNeuro</name></author>",
             "  <subtitle>Each entry is a date on which a model became the cheapest way to reach some level of the Artificial Analysis Intelligence Index, through a release or a price change.</subtitle>"]
    for a in entries:
        title = f"{a['date']}: {a['model']} ({'price change' if a['kind'] == 'price change' else 'new model'}, index {a['intelligence_index']:.1f})"
        # The advance's social card, rendered per (date, base model) group.
        card = f"{site}/llm-cost-frontier/images/advances/{a['date']}-{slugify(a['base'])}.png"
        lines += ["  <entry>", f"    <title>{xml_escape(title)}</title>",
                  f'    <link href="{site}/llm-cost-frontier/#advances" />',
                  f'    <link rel="enclosure" type="image/png" href="{card}" />',
                  f"    <id>{site}/llm-cost-frontier/advance/{a['date']}/{a['slug']}</id>",
                  f"    <updated>{a['date']}T00:00:00Z</updated>",
                  f"    <summary>{xml_escape(describe(a))}</summary>", "  </entry>"]
    lines.append("</feed>")
    feed_path.parent.mkdir(parents=True, exist_ok=True)
    feed_path.write_text("\n".join(lines) + "\n")


def check_live_set(live: dict, history: dict, eras: list, today: str) -> None:
    """Refuse suspicious fetches. A hard floor guards against page breakage;
    a relative floor and a median index shift check guard against the source
    recomposing its index, which must be declared as an era by hand before
    the updater will merge it."""
    if len(live) < 50:
        raise RuntimeError(f"only {len(live)} live models parsed; refusing to update")
    recent_era = any(abs((dt.date.fromisoformat(today) - dt.date.fromisoformat(e["start"])).days) <= 7 for e in eras)
    if recent_era:
        return
    prev_live = sum(1 for m in history["models"].values() if not m.get("retired"))
    if prev_live and len(live) < 0.7 * prev_live:
        raise RuntimeError(
            f"live set dropped from {prev_live} to {len(live)} models; if the source recomposed "
            f"its index, declare an era in data/eras.json before updating")
    shifts = sorted(abs(rec["intelligence_index"] - history["models"][s]["intelligence_index"])
                    for s, rec in live.items() if s in history["models"])
    if shifts and shifts[len(shifts) // 2] > 2.0:
        raise RuntimeError(
            f"median index shift is {shifts[len(shifts) // 2]:.1f} points across {len(shifts)} models; "
            f"if the source recomposed its index, declare an era in data/eras.json before updating")


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="llm-cost-frontier", description=__doc__.splitlines()[0])
    p.add_argument("--history", type=Path, default=DEFAULT_HISTORY, help="cumulative per-model history (read and rewritten)")
    p.add_argument("--events", type=Path, default=DEFAULT_EVENTS, help="hand-maintained price events")
    p.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES, help="hand-maintained corrections to upstream fields")
    p.add_argument("--eras", type=Path, default=DEFAULT_ERAS, help="hand-declared index era boundaries")
    p.add_argument("--out", type=Path, default=DEFAULT_OUTPUT, help="dashboard JSON to write")
    p.add_argument("--feed", type=Path, default=DEFAULT_FEED, help="Atom feed to write")
    p.add_argument("--site", default=DEFAULT_SITE, help="base URL used for links in the feed")
    p.add_argument("--source", default=SOURCE_PAGE, help="Artificial Analysis model page to read the dataset from")
    p.add_argument("--offline", action="store_true", help="rebuild the outputs from the stored history without fetching")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    history = json.loads(args.history.read_text())
    events = json.loads(args.events.read_text())
    overrides = json.loads(args.overrides.read_text()) if args.overrides.exists() else {}
    eras = json.loads(args.eras.read_text()) if args.eras.exists() else []
    if not args.offline:
        today = dt.date.today().isoformat()
        payload = fetch_payload(args.source)
        live = extract_models(payload)
        check_live_set(live, history, eras, today)
        history = merge(history, live, today)
        args.history.write_text(json.dumps(history, indent=1, sort_keys=True) + "\n")
        print(f"merged {len(live)} live models; history now {len(history['models'])} models")
    out = build_output(history, events, overrides, eras)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, separators=(",", ":")) + "\n")
    write_feed(out, args.feed, args.site)
    print(f"wrote {args.out} and {args.feed} ({out['counts']}) as of {out['updated']}")
    for t, s in out["tier_summary"].items():
        if s:
            print(f"  index >= {t}: {s['collapse']}x from {s['first_date']} to {s['last_date']}, halving ~{s['halving_days']} d")
    return 0

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

# Per-capability metrics read from the same payload, each chosen because it
# translates to a class of application better than the aggregate index does.
# "percent" metrics arrive as 0-1 fractions and are stored as 0-100. Metrics
# measured for only a small fraction of models (LiveCodeBench, AIME) are left
# out. The blurb is shown on the dashboard when the capability's tab is active.
CAPABILITIES = [
    dict(key="coding", field="terminalbenchV21", label="Coding", metric="Terminal-Bench 2.1", percent=True,
         blurb="Completion rate on Terminal-Bench 2.1: real software engineering tasks run agentically in a terminal. The axis to watch when picking a model for a coding assistant or an autonomous software agent."),
    dict(key="agentic", field="agenticIndex", label="Agentic Tool Use", metric="AA Agentic Index", percent=False,
         blurb="Artificial Analysis's Agentic Index, a composite of tool calling and multi-step task completion evaluations. Relevant for models that orchestrate tools and workflows rather than answer single prompts."),
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


def cost_changes(slug: str, m: dict, events: list) -> list:
    """Dated cost changes for one model: [[date, cost, note], ...], starting at release."""
    obs = sorted(m.get("observations") or [[m.get("last_seen", m["release_date"]), m["cost_per_task"], m["intelligence_index"]]])
    first_cost = obs[0][1]
    out = []
    ev = next((e for e in events if slug.startswith(e["slug_prefix"])), None)
    if ev and m["release_date"] < ev["cut_date"]:
        out.append([m["release_date"], first_cost * ev["multiplier_before"], "at launch price"])
        out.append([ev["cut_date"], first_cost, f"price cut (released {m['release_date']})"])
    else:
        out.append([m["release_date"], first_cost, None])
    for date, cost, _iq in obs[1:]:
        if abs(cost - out[-1][1]) > 1e-9:
            out.append([date, cost, f"price change observed (released {m['release_date']})"])
    return out


def price_timeline(models: dict, events: list) -> list:
    """All dated cost changes across models as (date, cost, slug, iq, note)."""
    out = []
    for slug, m in models.items():
        for date, cost, note in cost_changes(slug, m, events):
            out.append([date, cost, slug, m["intelligence_index"], note])
    out.sort(key=lambda e: (e[0], e[1]))
    return out


def tier_records(models: dict, events: list, tiers: list = None) -> dict:
    timeline = price_timeline(models, events)
    out = {}
    for t in TIERS if tiers is None else tiers:
        best = math.inf
        recs = []
        for date, cost, slug, iq, note in timeline:
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


def frontier_advances(models: dict, events: list, records: dict) -> list:
    """Dates on which the Pareto frontier changed, newest first."""
    timeline = price_timeline(models, events)
    record_keys = {(r[0], r[2]): t for t, recs in records.items() for r in recs}
    state = {}
    current = set()
    advances = []
    by_date = {}
    for date, cost, slug, iq, note in timeline:
        by_date.setdefault(date, []).append((cost, slug, iq, note))
    for date in sorted(by_date):
        changed = {}
        state_before = dict(state)
        base_of = lambda o: split_variant(models[o]["name"])[0]
        for cost, slug, iq, note in by_date[date]:
            prev = state.get(slug)
            state[slug] = (cost, iq)
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
    in for the intelligence index so the frontier machinery applies unchanged."""
    out = {}
    for slug, m in models.items():
        v = (m.get("capabilities") or {}).get(key)
        if v is None:
            continue
        mm = dict(m)
        mm["intelligence_index"] = v
        out[slug] = mm
    return out


def tier_summary(records: dict) -> dict:
    out = {}
    for t, recs in records.items():
        if len(recs) < 2:
            out[t] = None
            continue
        first, last = recs[0], recs[-1]
        days = (dt.date.fromisoformat(last[0]) - dt.date.fromisoformat(first[0])).days
        ratio = first[1] / last[1]
        out[t] = dict(
            first_date=first[0], first_model=first[2], first_cost=first[1],
            last_date=last[0], last_model=last[2], last_cost=last[1],
            collapse=round(ratio, 1),
            halving_days=round(days / math.log2(ratio)) if ratio > 1 else None,
        )
    return out


def build_output(history: dict, events: list, overrides: dict | None = None) -> dict:
    today = dt.date.fromisoformat(history["updated"])
    models = history["models"]
    if overrides:
        apply_overrides(models, overrides)
    rows = []
    for slug, m in sorted(models.items(), key=lambda kv: (kv[1]["release_date"], kv[1]["name"])):
        changes = [[d, round(c, 6)] for d, c, _n in cost_changes(slug, m, events)]
        caps = m.get("capabilities") or {}
        row = [m["name"], m["creator"], m["release_date"], m["intelligence_index"], m["cost_per_task"], int(m["retired"]), int(m["open_weights"]),
               changes if len(changes) > 1 else 0,
               [caps.get(c["key"]) for c in CAPABILITIES]]
        rows.append(row)
    records = tier_records(models, events)
    advances = frontier_advances(models, events, records)
    # Per-capability tiers are derived from each metric's range: the top four
    # multiples of ten at or below the highest measured score.
    cap_tiers, cap_tier_cost, cap_tier_summary, cap_advances = {}, {}, {}, {}
    for c in CAPABILITIES:
        cm = capability_models(models, c["key"])
        if not cm:
            continue
        hi = int(max(m["intelligence_index"] for m in cm.values()) // 10) * 10
        tiers = [t for t in (hi - 30, hi - 20, hi - 10, hi) if t > 0]
        recs = tier_records(cm, events, tiers)
        cap_tiers[c["key"]] = tiers
        cap_tier_cost[c["key"]] = recs
        cap_tier_summary[c["key"]] = tier_summary(recs)
        cap_advances[c["key"]] = frontier_advances(cm, events, recs)
    return dict(
        advances=advances,
        cap_advances=cap_advances,
        cap_tiers=cap_tiers,
        cap_tier_cost=cap_tier_cost,
        cap_tier_summary=cap_tier_summary,
        updated=history["updated"],
        source="Artificial Analysis (artificialanalysis.ai), measured cost per Intelligence Index task",
        snapshots=snapshots(today),
        tiers=TIERS,
        capabilities=[{k: c[k] for k in ("key", "label", "metric", "percent", "blurb")} for c in CAPABILITIES],
        models=rows,
        tier_cost=records,
        tier_summary=tier_summary(records),
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


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="llm-cost-frontier", description=__doc__.splitlines()[0])
    p.add_argument("--history", type=Path, default=DEFAULT_HISTORY, help="cumulative per-model history (read and rewritten)")
    p.add_argument("--events", type=Path, default=DEFAULT_EVENTS, help="hand-maintained price events")
    p.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES, help="hand-maintained corrections to upstream fields")
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
    if not args.offline:
        today = dt.date.today().isoformat()
        payload = fetch_payload(args.source)
        live = extract_models(payload)
        if len(live) < 50:
            raise RuntimeError(f"only {len(live)} live models parsed; refusing to update")
        history = merge(history, live, today)
        args.history.write_text(json.dumps(history, indent=1, sort_keys=True) + "\n")
        print(f"merged {len(live)} live models; history now {len(history['models'])} models")
    out = build_output(history, events, overrides)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, separators=(",", ":")) + "\n")
    write_feed(out, args.feed, args.site)
    print(f"wrote {args.out} and {args.feed} ({out['counts']}) as of {out['updated']}")
    for t, s in out["tier_summary"].items():
        if s:
            print(f"  index >= {t}: {s['collapse']}x from {s['first_date']} to {s['last_date']}, halving ~{s['halving_days']} d")
    return 0

"""Render social card images for frontier advances and the current frontier.

Each advance gets a 1200x630 PNG showing the scatter of models as they stood on
the advance date, the Pareto frontier before and after, and the advancing model
highlighted. Because the state is reconstructed as of the advance date, a card
never changes once rendered, so existing files are skipped and only new
advances cost anything on the nightly run. A separate card shows the current
frontier, for use as the dashboard page's social preview image.

Unlike the updater, this module needs matplotlib (pip install matplotlib, or
install the package with the [render] extra).
"""
import argparse
import datetime as dt
import json
from pathlib import Path

from .update import (
    DEFAULT_EVENTS,
    DEFAULT_HISTORY,
    DEFAULT_OVERRIDES,
    apply_overrides,
    join_and,
    pareto,
    price_timeline,
)

DEFAULT_IMAGES = Path("build/images")

# Palette shared with the dashboard at catalystneuro.com/llm-cost-frontier/.
C = dict(
    surface="#ffffff", grid="#ecf1f8", axis="#dfe6f1",
    ink="#101642", ink2="#55607a", muted="#68718b", deemph="#c2cbdc",
    old="#9aa4bb", blue="#2a78d6", accent="#eb6834",
)
W_PX, H_PX, DPI = 1200, 630, 100


def fmt_cost(c: float) -> str:
    return f"${c:.4f}" if c < 0.01 else f"${c:.3f}" if c < 0.1 else f"${c:.2f}"


def long_date(iso: str) -> str:
    d = dt.date.fromisoformat(iso)
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def card_summary(a: dict) -> str:
    """A shorter counterpart of update.describe, sized for two lines on the card."""
    cost = fmt_cost(a["cost_per_task"])
    if f"{a['owns_to']:.1f}" == f"{a['owns_from']:.1f}":
        span = f"index {a['owns_to']:.1f}"
    else:
        span = f"index {a['owns_from']:.1f} to {a['owns_to']:.1f}"
    if a["kind"] == "price change" and a["previous_cost"]:
        s = f"Price moved from {fmt_cost(a['previous_cost'])} to {cost} per task; now the cheapest way to reach {span}."
    elif a.get("ceiling_from") is not None:
        s = f"Pushed the intelligence ceiling from {a['ceiling_from']:.1f} to {a['owns_to']:.1f}, at {cost} per task."
    else:
        s = f"Now the cheapest way to reach {span} at {cost} per task."
    if a["records"]:
        s += " New cost record for " + join_and([f"index ≥ {t}" for t in a["records"]]) + "."
    if a["open_weights"]:
        s += " Open weights."
    return s


def state_at(timeline: list, date: str, before: bool = False) -> dict:
    """{slug: (cost, iq)} using the last cost change on or before the date
    (strictly before it when before=True)."""
    state = {}
    for d, cost, slug, iq, _note in timeline:
        if d < date or (d == date and not before):
            state[slug] = (cost, iq)
    return state


def frontier_steps(state: dict, front: set, x_right: float) -> tuple:
    """Staircase (xs, ys) through the frontier members, extended to the right edge."""
    pts = sorted((state[s] for s in front), key=lambda p: p[1])
    xs, ys = [pts[0][0]], [pts[0][1]]
    for cost, iq in pts[1:]:
        xs += [cost, cost]
        ys += [ys[-1], iq]
    xs.append(x_right)
    ys.append(ys[-1])
    return xs, ys


def new_figure(kicker: str, title: str, summary_lines: list):
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(W_PX / DPI, H_PX / DPI), dpi=DPI, facecolor=C["surface"])
    fig.text(0.048, 0.945, kicker.upper(), fontsize=12.5, color=C["muted"], va="top")
    fig.text(0.048, 0.895, title, fontsize=25, color=C["ink"], va="top", fontweight="bold")
    for i, line in enumerate(summary_lines[:2]):
        fig.text(0.048, 0.820 - 0.042 * i, line, fontsize=13.5, color=C["ink2"], va="top")
    fig.text(0.048, 0.028, "Data: Artificial Analysis · measured cost per Intelligence Index task",
             fontsize=11.5, color=C["muted"], va="bottom")
    fig.text(0.952, 0.028, "catalystneuro.com/llm-cost-frontier",
             fontsize=12.5, color=C["ink2"], va="bottom", ha="right", fontweight="bold")
    top = 0.720 if len(summary_lines) < 2 else 0.685
    ax = fig.add_axes([0.058, 0.135, 0.894, top - 0.135])
    return fig, ax


def draw_chart(ax, state: dict, models: dict, front: set, front_before: set = None, highlight: str = None):
    import matplotlib.ticker as mticker

    costs = [c for c, _iq in state.values()]
    iqs = [iq for _c, iq in state.values()]
    xlo, xhi = min(costs) * 0.66, max(costs) * 1.5
    yhi = max(66, ((int(max(iqs)) + 3) // 10 + 1) * 10)

    ax.set_xscale("log")
    ax.set_xlim(xlo, xhi)
    ax.set_ylim(0, yhi)
    ax.set_facecolor(C["surface"])
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(C["axis"])
    ax.grid(True, which="major", color=C["grid"], linewidth=1)
    ax.set_axisbelow(True)
    ax.tick_params(colors=C["muted"], labelsize=11, length=0)
    decades = []
    d = 0.0001
    while d <= xhi:
        if d >= xlo:
            decades.append(d)
        d *= 10
    if not decades:  # a very narrow early range can contain no power of ten
        decades = [min(costs)]
    ax.xaxis.set_major_locator(mticker.FixedLocator(decades))
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(
        lambda v, _p: f"${v:.0f}" if v >= 1 else f"${v:.2f}" if v >= 0.01 else f"${v:.4g}"))
    ax.xaxis.set_minor_locator(mticker.NullLocator())
    ax.yaxis.set_major_locator(mticker.MultipleLocator(10))
    ax.set_xlabel("Cost per task (log)", fontsize=12, color=C["ink2"])
    ax.set_ylabel("Intelligence Index", fontsize=12, color=C["ink2"])

    def dots(slugs, color, size, z):
        filled = [state[s] for s in slugs if not models[s]["open_weights"]]
        hollow = [state[s] for s in slugs if models[s]["open_weights"]]
        if filled:
            ax.scatter(*zip(*filled), s=size, color=color, edgecolors=C["surface"], linewidths=1, zorder=z)
        if hollow:
            ax.scatter(*zip(*hollow), s=size, facecolors=C["surface"], edgecolors=color, linewidths=1.6, zorder=z)

    dots([s for s in state if s not in front and s != highlight], C["deemph"], 26, 2)

    if front_before:
        xs, ys = frontier_steps(state, front_before, xhi)
        ax.plot(xs, ys, color=C["old"], linewidth=1.8, linestyle=(0, (5, 4)), zorder=3)
    xs, ys = frontier_steps(state, front, xhi)
    ax.plot(xs, ys, color=C["blue"], linewidth=2.6, solid_joinstyle="round", zorder=4)
    dots([s for s in front if s != highlight], C["blue"], 42, 5)
    return xlo, xhi


def draw_highlight(ax, a: dict, state: dict, xlo: float, xhi: float):
    import math

    cost, iq = state[a["slug"]]
    ax.axhspan(a["owns_from"], a["owns_to"], color=C["accent"], alpha=0.06, zorder=1)
    if a["kind"] == "price change" and a["previous_cost"]:
        ax.scatter([a["previous_cost"]], [iq], s=70, facecolors="none",
                   edgecolors=C["accent"], linewidths=1.6, linestyle="--", zorder=6)
        ax.annotate("", xy=(cost, iq), xytext=(a["previous_cost"], iq),
                    arrowprops=dict(arrowstyle="->", color=C["accent"], linewidth=1.6,
                                    linestyle="--", shrinkA=8, shrinkB=8), zorder=6)
    if a["open_weights"]:
        ax.scatter([cost], [iq], s=120, facecolors=C["surface"], edgecolors=C["accent"], linewidths=2.6, zorder=7)
    else:
        ax.scatter([cost], [iq], s=120, color=C["accent"], edgecolors=C["surface"], linewidths=1.6, zorder=7)
    frac = (math.log10(cost) - math.log10(xlo)) / (math.log10(xhi) - math.log10(xlo))
    on_right = frac > 0.62
    ax.annotate(a["model"], xy=(cost, iq), xytext=(-14 if on_right else 14, 10),
                textcoords="offset points", ha="right" if on_right else "left",
                fontsize=12.5, color=C["accent"], fontweight="bold", zorder=7)


def add_legend(ax, price_change: bool):
    from matplotlib.lines import Line2D

    handles = [
        Line2D([], [], color=C["blue"], linewidth=2.6, label="frontier after"),
        Line2D([], [], color=C["old"], linewidth=1.8, linestyle=(0, (5, 4)), label="frontier before"),
        Line2D([], [], marker="o", color="none", markerfacecolor=C["accent"],
               markeredgecolor=C["surface"], markersize=9, label="this advance"),
        Line2D([], [], marker="o", color="none", markerfacecolor=C["surface"],
               markeredgecolor=C["ink2"], markeredgewidth=1.6, markersize=8, label="open weights"),
    ]
    if price_change:
        handles.insert(3, Line2D([], [], marker="o", color="none", markerfacecolor="none",
                                 markeredgecolor=C["accent"], markeredgewidth=1.6, markersize=9,
                                 label="previous price"))
    ax.legend(handles=handles, loc="lower right", frameon=False, fontsize=10.5,
              labelcolor=C["ink2"], handletextpad=0.5, borderaxespad=0.2)


def save(fig, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=DPI, facecolor=C["surface"], metadata={"Software": "llm-cost-frontier"})
    import matplotlib.pyplot as plt

    plt.close(fig)


def wrap(text: str, width: int = 108) -> list:
    import textwrap

    return textwrap.wrap(text, width=width)


def render_advance(a: dict, models: dict, timeline: list, path: Path):
    state = state_at(timeline, a["date"])
    front = pareto(state)
    front_before = pareto(state_at(timeline, a["date"], before=True))
    kicker = f"Frontier advance · {long_date(a['date'])} · {a['kind']} · {a['creator']}"
    fig, ax = new_figure(kicker, a["model"], wrap(card_summary(a)))
    xlo, xhi = draw_chart(ax, state, models, front, front_before, highlight=a["slug"])
    draw_highlight(ax, a, state, xlo, xhi)
    add_legend(ax, price_change=a["kind"] == "price change" and bool(a["previous_cost"]))
    save(fig, path)


def render_current(out: dict, models: dict, timeline: list, path: Path):
    state = state_at(timeline, out["updated"])
    front = pareto(state)
    live = sum(1 for m in models.values() if not m["retired"])
    summary = (f"The cheapest way to reach each level of the Artificial Analysis "
               f"Intelligence Index, across {live} live models.")
    s50 = (out.get("tier_summary") or {}).get("50")
    if s50 and s50.get("halving_days"):
        first = dt.date.fromisoformat(s50["first_date"])
        summary += (f" Index ≥ 50 cost has fallen {s50['collapse']:g}x since "
                    f"{first.strftime('%B %Y')}, halving about every {s50['halving_days']} days.")
    fig, ax = new_figure(f"Updated {long_date(out['updated'])}", "The LLM Cost Frontier", wrap(summary))
    draw_chart(ax, state, models, front)
    save(fig, path)


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="llm-cost-frontier-render", description=__doc__.splitlines()[0])
    p.add_argument("--history", type=Path, default=DEFAULT_HISTORY, help="cumulative per-model history to read")
    p.add_argument("--events", type=Path, default=DEFAULT_EVENTS, help="hand-maintained price events")
    p.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES, help="hand-maintained corrections to upstream fields")
    p.add_argument("--out", type=Path, default=DEFAULT_IMAGES, help="directory to write images into")
    p.add_argument("--force", action="store_true", help="re-render advance cards that already exist")
    return p.parse_args(argv)


def main(argv=None):
    try:
        import matplotlib
    except ImportError:
        raise SystemExit("rendering needs matplotlib: pip install matplotlib")
    matplotlib.use("Agg")
    # Dollar amounts in the card text would otherwise be parsed as TeX math.
    matplotlib.rcParams["text.parse_math"] = False

    args = parse_args(argv)
    from .update import build_output

    history = json.loads(args.history.read_text())
    events = json.loads(args.events.read_text())
    overrides = json.loads(args.overrides.read_text()) if args.overrides.exists() else {}
    out = build_output(history, events, overrides)
    models = history["models"]
    timeline = price_timeline(models, events)

    rendered = skipped = 0
    for a in out["advances"]:
        path = args.out / "advances" / f"{a['date']}-{a['slug']}.png"
        if path.exists() and not args.force:
            skipped += 1
            continue
        render_advance(a, models, timeline, path)
        rendered += 1
    render_current(out, models, timeline, args.out / "frontier-card.png")
    print(f"rendered {rendered} advance cards ({skipped} already existed) and frontier-card.png in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

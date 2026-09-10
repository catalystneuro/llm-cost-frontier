# LLM Cost Frontier

This repository tracks how cheaply a given level of large language model capability can be bought, and how that price changes over time. Four times a day it reads Artificial Analysis's measured cost per Intelligence Index task for every model they benchmark, merges the result into a cumulative history, and rebuilds two artifacts: the JSON that the dashboard at [llm-cost-frontier.catalystneuro.com](https://llm-cost-frontier.catalystneuro.com/) renders, and an Atom feed of frontier advances.

The background, the method, and the argument for why the cheap end of the range matters are in the post [What Happens When the Cost of Intelligence Drops 100x](https://catalystneuro.com/blog/cost-of-intelligence-drops-100x/).

## What It Produces

| File | Contents |
|---|---|
| `data/history.json` | Cumulative per-model record: release date, creator, open weights flag, retired flag, and every observed `(date, cost, index)` |
| `data/price-events.json` | Hand-maintained price changes from before nightly observation began |
| `data/overrides.json` | Hand-maintained corrections to upstream fields, currently the open weights flag; applied to the outputs, never to the history |
| `data/eras.json` | Hand-declared index era boundaries: dates on which the source recomposed the Intelligence Index, breaking comparability of scores and measured costs |
| `build/llm-frontier.json` | What the dashboard renders: model rows, frontier snapshots, tier records, halving times, and frontier advances |
| `build/feed.xml` | Atom feed of the last 60 frontier advances |
| `build/images/advances/` | A 1200x630 social card per advancing model per date, reasoning levels grouped, named `{date}-{base model}.png`; capability advances get theirs under `advances/{capability}/` |
| `build/images/frontier-card.png` | A social card of the current frontier, for the dashboard page's link preview |

The dashboard itself lives in `site/` and is served from this repository via GitHub Pages at [llm-cost-frontier.catalystneuro.com](https://llm-cost-frontier.catalystneuro.com/): a deploy workflow copies `site/` and the `build/` artifacts into the Pages tree on every push that touches them, and the update workflow redeploys after each data commit. Every same-repository pull request gets a live preview of the site at `/previews/pr-<number>/`, linked by a sticky comment on the PR and removed when it closes. The old catalystneuro.com/llm-cost-frontier URLs redirect here.

## Method

Any model page on artificialanalysis.ai embeds the full comparison dataset for every model they currently benchmark, including `intelligenceIndexCostPerTask`, the average billed cost to run one task from their evaluation suite with the input, reasoning, and answer tokens the run actually used. The updater fetches one page, parses that payload, and merges it into the history.

Beyond the aggregate Intelligence Index, the updater also records a set of per-capability scores from the same payload, each chosen because it maps onto a class of application better than the aggregate does: Terminal-Bench 2.1 (agentic coding), the Agentic Index (tool use), AA-LCR (long context), IFBench (instruction following), Omniscience (factual recall with hallucinations penalized), GPQA Diamond (scientific reasoning), GDPval-AA (office work products), and MMMU-Pro (multimodal input). The dashboard renders these as tabs that switch the vertical axis of the frontier chart. Capability scores are stored as their latest measured values; only prices are tracked over time. Metrics measured for only a small fraction of models (LiveCodeBench, AIME) are left out.

Three properties follow from keeping a history instead of a snapshot:

- **Models that leave keep their data.** When Artificial Analysis retires a model from live benchmarking, its observations stay and it is marked retired, so the record only grows.
- **Prices are dated.** An observation is appended whenever a model's cost or index changes, so a price cut is dated to when it was observed instead of being back-dated to the model's release. Frontier snapshots and tier records use the price in effect on each date.
- **Advances are derived, not curated.** A frontier advance is any date on which the Pareto frontier of (higher index, lower cost) changed, whether through a release or a price change. Each one records the index range the model took over, the models it took that range from, any tier cost record it set, and whether it pushed the intelligence ceiling.

Observation began on August 19, 2026. Before that date the only value available is a model's price at first observation, indexed by its release date, except for the price events recorded by hand in `data/price-events.json`. Earlier cuts that are not recorded make older points look cheaper than they were, which understates the collapse and dates it too early.

When the source recomposes the Intelligence Index, as it did on September 5, 2026, scores and measured costs before and after the change are not comparable. Such a change is declared by hand as an era in `data/eras.json`. Observations store the index alongside the cost, so every derivation uses the index in effect on each date: frontiers before a boundary keep the old scores, records reset at the boundary, a model's first re-measurement under a new index is not reported as an advance, and models never re-scored under the current index compete only in the eras they were measured in. Collapse and halving figures pool the within-era declines, so the full history keeps informing them without a cost ever being compared across a boundary. A date on which the measured cost moved by more than 10% for a large share of models at once is treated as a re-measurement of the evaluation suite rather than a wave of price changes, and produces no advances. The updater refuses to merge a fetch whose live set shrank by more than 30% or whose median index shift exceeds 2 points unless an era within a week of the run date has been declared, so the next recomposition stops the pipeline for a human decision instead of merging silently.

Free and promotional endpoints with a measured cost of zero are excluded, since they distort the cost axis.

## Usage

The updater is standard library only, so it needs no dependencies to run:

```bash
PYTHONPATH=src python -m llm_cost_frontier            # fetch, merge, rebuild
PYTHONPATH=src python -m llm_cost_frontier --offline  # rebuild from the stored history
```

Or install it and use the console script:

```bash
pip install git+https://github.com/catalystneuro/llm-cost-frontier
llm-cost-frontier --help
```

Paths and the feed's base URL are options, so the tool can write wherever you want:

```bash
llm-cost-frontier --history data/history.json --out build/llm-frontier.json \
                  --feed build/feed.xml --site https://example.org
```

A run refuses to rewrite the history if it parses fewer than 50 live models, which guards against a change in the source page's layout quietly emptying the dataset.

The test suite runs offline. The unit tests need only pytest and cover the payload parsing, the history merge, the frontier derivations, and the output assembly, checking invariants against the repository's real data so they keep passing as the data grows. A second suite drives the assembled site in a headless browser (`pip install playwright && playwright install chromium`), asserting a clean console, working tab and era switching, and phone-width behavior including rotation; it is skipped when playwright is absent. `.github/workflows/test.yml` runs both on every pull request and push to main.

```bash
pip install pytest
pytest
```

## Advance Cards

Each frontier advance also gets a 1200x630 PNG suitable for social sharing: the scatter of every model as it stood on the advance date, the frontier before and after, and the advancing model highlighted, with the region it took from the previous frontier shaded. Reasoning levels of the same base model that advance on the same date share one card, so a release that lands three variants on the frontier produces one image, not three. The shaded region and the dashed line show the frontier as it would stand without that model's change, so on a date with several advancing models each card credits only its own model. Because the state is reconstructed as of the advance date, a card never changes once rendered, so the renderer skips existing files and only new advances cost anything on the nightly run. This is the one part of the pipeline that needs a dependency:

```bash
pip install matplotlib
PYTHONPATH=src python -m llm_cost_frontier.render           # render new cards
PYTHONPATH=src python -m llm_cost_frontier.render --force   # re-render everything
```

## Schedule

`.github/workflows/update.yml` runs the updater four times a day, every six hours starting at 00:00 UTC, commits `data/history.json` and the build artifacts when they change, and then redeploys the site. The workflow can also be run by hand from the Actions tab.

## Caveats

The Intelligence Index is one aggregate of nine evaluations, so two models with the same score may behave differently on a particular task. Cost per task is measured on a reasoning heavy evaluation suite with long prompts; a chat workload with short prompts would scale differently across models. Prices are what buyers pay, which says nothing about what inference costs the provider.

## License

BSD 3-Clause. Data is derived from [Artificial Analysis](https://artificialanalysis.ai/), whose terms govern its use.

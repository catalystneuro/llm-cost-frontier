# LLM Cost Frontier

This repository tracks how cheaply a given level of large language model capability can be bought, and how that price changes over time. Every night it reads Artificial Analysis's measured cost per Intelligence Index task for every model they benchmark, merges the result into a cumulative history, and rebuilds two artifacts: the JSON that the dashboard at [catalystneuro.com/llm-cost-frontier](https://catalystneuro.com/llm-cost-frontier/) renders, and an Atom feed of frontier advances.

The background, the method, and the argument for why the cheap end of the range matters are in the post [What Happens When the Cost of Intelligence Drops 100x](https://catalystneuro.com/blog/cost-of-intelligence-drops-100x/).

## What It Produces

| File | Contents |
|---|---|
| `data/history.json` | Cumulative per-model record: release date, creator, open weights flag, retired flag, and every observed `(date, cost, index)` |
| `data/price-events.json` | Hand-maintained price changes from before nightly observation began |
| `build/llm-frontier.json` | What the dashboard renders: model rows, frontier snapshots, tier records, halving times, and frontier advances |
| `build/feed.xml` | Atom feed of the last 60 frontier advances |

The website syncs the two files in `build/` an hour after this repository updates them, so the dashboard and the feed are served from catalystneuro.com, not from GitHub.

## Method

Any model page on artificialanalysis.ai embeds the full comparison dataset for every model they currently benchmark, including `intelligenceIndexCostPerTask`, the average billed cost to run one task from their evaluation suite with the input, reasoning, and answer tokens the run actually used. The updater fetches one page, parses that payload, and merges it into the history.

Three properties follow from keeping a history instead of a snapshot:

- **Models that leave keep their data.** When Artificial Analysis retires a model from live benchmarking, its observations stay and it is marked retired, so the record only grows.
- **Prices are dated.** An observation is appended whenever a model's cost or index changes, so a price cut is dated to when it was observed instead of being back-dated to the model's release. Frontier snapshots and tier records use the price in effect on each date.
- **Advances are derived, not curated.** A frontier advance is any date on which the Pareto frontier of (higher index, lower cost) changed, whether through a release or a price change. Each one records the index range the model took over, the models it took that range from, any tier cost record it set, and whether it pushed the intelligence ceiling.

Observation began on August 19, 2026. Before that date the only value available is a model's price at first observation, indexed by its release date, except for the price events recorded by hand in `data/price-events.json`. Earlier cuts that are not recorded make older points look cheaper than they were, which understates the collapse and dates it too early.

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

## Schedule

`.github/workflows/update.yml` runs the updater every night at 06:00 UTC and commits `data/history.json` and the two build artifacts when they change. It can also be triggered by hand from the Actions tab.

## Caveats

The Intelligence Index is one aggregate of nine evaluations, so two models with the same score may behave differently on a particular task. Cost per task is measured on a reasoning heavy evaluation suite with long prompts; a chat workload with short prompts would scale differently across models. Prices are what buyers pay, which says nothing about what inference costs the provider.

## License

BSD 3-Clause. Data is derived from [Artificial Analysis](https://artificialanalysis.ai/), whose terms govern its use.

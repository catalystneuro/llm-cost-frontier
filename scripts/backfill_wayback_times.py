"""Backfill v4.1-era time-per-task measurements from Wayback Machine snapshots.

Artificial Analysis began publishing intelligenceIndexTimePerTask around
July 2026, but this project only started recording it on September 11, 2026,
under the v4.3 index. Archived copies of AA model pages embed the full
comparison payload, so the July-September v4.1 measurements are recoverable.

This script queries the Wayback CDX index for AA model-page captures in a
date window, fetches one usable capture per day, extracts every model's
time per task with the updater's own parser, saves the harvested series to
data/wayback_times.json (so re-runs never refetch a finished day), and
merges it into data/history.json via backfill_time_series, which is
idempotent and never touches current-era rows.

Usage: PYTHONPATH=src python scripts/backfill_wayback_times.py [--from 2026-07-01] [--to 2026-09-04]
"""
import argparse
import gzip
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

from llm_cost_frontier.update import backfill_time_series, extract_models, flight_payload

REPO = Path(__file__).resolve().parents[1]
CDX = ("https://web.archive.org/cdx/search/cdx?url=artificialanalysis.ai/models/"
       "&matchType=prefix&from={f}&to={t}&filter=statuscode:200&limit=8000")
PAGE = "https://web.archive.org/web/{ts}id_/{url}"
MIN_MODELS = 60  # a complete payload carries the whole comparison set


def fetch(url, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": "llm-frontier backfill (catalystneuro.com)"})
    raw = urllib.request.urlopen(req, timeout=timeout).read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")


def candidates(day_rows):
    """Prefer clean model pages; skip partial RSC responses and subpages."""
    def clean(url):
        return "?" not in url and re.match(r"^https://artificialanalysis\.ai/models/[a-z0-9.-]+$", url)
    rows = [r for r in day_rows if clean(r[1])]
    return rows or []


def harvest_day(day_rows, tries=3):
    for ts, url in candidates(day_rows)[:tries]:
        try:
            models = extract_models(flight_payload(fetch(PAGE.format(ts=ts, url=url))))
        except Exception as e:
            print(f"    {ts} {url}: {e}")
            time.sleep(2)
            continue
        if len(models) < MIN_MODELS:
            print(f"    {ts}: only {len(models)} models parsed; likely truncated, trying next")
            time.sleep(2)
            continue
        times = {slug: m["time_per_task"] for slug, m in models.items() if m.get("time_per_task")}
        return times  # may be empty: the metric did not exist yet that day
    return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="2026-07-01")
    ap.add_argument("--to", dest="end", default="2026-09-04")
    args = ap.parse_args(argv)

    store_path = REPO / "data/wayback_times.json"
    store = json.loads(store_path.read_text()) if store_path.exists() else {}

    cdx = fetch(CDX.format(f=args.start.replace("-", ""), t=args.end.replace("-", "")))
    by_day = {}
    for line in cdx.splitlines():
        p = line.split(" ")
        if len(p) >= 3:
            by_day.setdefault(p[1][:8], []).append((p[1], p[2]))
    print(f"{len(by_day)} capture days between {args.start} and {args.end}")

    for day in sorted(by_day):
        date = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        if date in store:
            continue
        print(f"  {date}: {len(by_day[day])} captures")
        times = harvest_day(by_day[day])
        if times is None:
            print(f"    no usable capture")
            continue
        store[date] = times
        print(f"    {len(times)} models with time per task")
        store_path.write_text(json.dumps(store, indent=0, sort_keys=True) + "\n")
        time.sleep(2)

    eras = json.loads((REPO / "data/eras.json").read_text())
    cutoff = eras[0]["start"]
    hist_path = REPO / "data/history.json"
    history = json.loads(hist_path.read_text())
    n = backfill_time_series(history, store, cutoff)
    hist_path.write_text(json.dumps(history, indent=1, sort_keys=True) + "\n")
    print(f"wrote {n} time observations into history (cutoff {cutoff})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

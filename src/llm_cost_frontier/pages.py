"""Generate the per-metric pages from the dashboard's index page.

Each capability gets a real page at /<key>/ with its own title, meta
description, canonical URL, social card, and Atom feed link, so a
metric-specific frontier can be shared, indexed, and subscribed to
directly. The pages are the root index.html with the head rewritten,
asset paths lifted one level, and data attributes that tell the
dashboard script which metric the page is about and where the site
root is.

The generated files are committed; tests/test_update.py checks they
match what this module produces, so editing site/index.html without
re-running `python -m llm_cost_frontier.pages` fails the suite.
"""
import sys
from pathlib import Path

from .update import CAPABILITIES, DEFAULT_SITE

SITE_DIR = Path(__file__).resolve().parents[2] / "site"


def metric_page(template: str, cap: dict) -> str:
    key, label = cap["key"], cap["label"]
    s = template
    swaps = [
        ("<title>LLM Frontier</title>", f"<title>{label} · LLM Frontier</title>"),
        ('<meta name="description" content="The cheapest way to reach each level of LLM capability, and how fast that cost is falling. Refreshed four times a day from Artificial Analysis measurements.">',
         f'<meta name="description" content="The cheapest way to reach each level of {cap["metric"]}, and how fast that cost is falling. Refreshed four times a day from Artificial Analysis measurements.">'),
        ('<meta property="og:title" content="LLM Frontier">',
         f'<meta property="og:title" content="{label} · LLM Frontier">'),
        ('<meta property="og:image" content="https://llm-frontier.catalystneuro.com/images/frontier-card.png">',
         f'<meta property="og:image" content="{DEFAULT_SITE}/images/{key}-card.png">'),
        ('<link rel="canonical" href="https://llm-frontier.catalystneuro.com/">',
         f'<link rel="canonical" href="{DEFAULT_SITE}/{key}/">'),
        ('<link rel="icon" type="image/svg+xml" href="favicon.svg">',
         '<link rel="icon" type="image/svg+xml" href="../favicon.svg">'),
        ('<link rel="alternate" type="application/atom+xml" title="Frontier advances" href="feed.xml">',
         f'<link rel="alternate" type="application/atom+xml" title="{cap["metric"]} advances" href="../feed-{key}.xml">'),
        ('<link rel="stylesheet" href="style.css">', '<link rel="stylesheet" href="../style.css">'),
        ("<body>", f'<body data-cap="{key}" data-root="../">'),
        ('<script src="js/llm-frontier-dashboard.js" defer></script>',
         '<script src="../js/llm-frontier-dashboard.js" defer></script>'),
    ]
    for old, new in swaps:
        assert old in s, f"template drifted: {old[:60]}"
        s = s.replace(old, new, 1)
    # Every root-relative link in the shared chrome lifts one level.
    for old, new in [
        ('class="site-title"><a href="./"', 'class="site-title"><a href="../"'),
        ('<a href="./" aria-current="page">Dashboard</a>', '<a href="../">Dashboard</a>'),
        ('href="cn-logo.png"', 'href="../cn-logo.png"'),
        ('src="cn-logo.png"', 'src="../cn-logo.png"'),
        ('href="methodology/"', 'href="../methodology/"'),
        ('href="feed.xml"', f'href="../feed-{key}.xml"'),
    ]:
        s = s.replace(old, new)
    return s


def generate() -> dict:
    template = (SITE_DIR / "index.html").read_text()
    return {SITE_DIR / c["key"] / "index.html": metric_page(template, c) for c in CAPABILITIES}


def main(argv=None):
    for path, content in generate().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        print(f"wrote {path.relative_to(SITE_DIR.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

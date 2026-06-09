"""
06_nyt.py
─────────
Pull kimchi-related articles from the New York Times Article Search API.

Source   : NYT Article Search API v2
Docs     : https://developer.nytimes.com/docs/articlesearch-product/1/overview
Coverage : 1851–present (realistically 1990s onward for kimchi mentions)
Key      : Free — register at https://developer.nytimes.com/

What you get
────────────
- Headline, abstract, snippet, section, publication date, URL
- NO full body text (NYT does not provide this via free API)
- Article volume over time as a cultural signal
- Abstracts for lightweight framing analysis

Rate limits & Colab safety
───────────────────────────
Free tier: 10 requests/minute, 500 requests/day.
Colab sessions time out — this script is designed to survive that:

  - Every page fetched is appended to the output CSV immediately
  - Progress is tracked in a checkpoint file (data/nyt_checkpoint.json)
    recording the last completed year and page
  - Run the script again (same command, no flags needed) and it automatically
    resumes from the checkpoint — no data is lost or re-fetched
  - If the 500/day limit is hit mid-run, just re-run tomorrow

Chunking strategy
─────────────────
NYT caps results at 10 per page, 100 pages per query = 1000 results max
per query window. Chunking by year keeps each window small enough that
1000 results is never hit (kimchi has at most ~100 articles/year).

Output
──────
data/nyt_kimchi_articles.csv    ← all articles collected so far (appended live)
data/nyt_checkpoint.json        ← progress tracker (year + page)
"""

import os
import json
import time
import requests
import pandas as pd
from datetime import date
from pathlib import Path
from utils import log, save_csv, DATA_DIR

# ── config ────────────────────────────────────────────────────────────────────

NYT_KEY    = os.getenv("NYT_KEY")

if not NYT_KEY:
    raise ValueError("NYT_KEY environment variable not set")

BASE_URL   = "https://api.nytimes.com/svc/search/v2/articlesearch.json"
QUERY      = "kimchi"
START_YEAR = 1990
END_YEAR   = date.today().year
SLEEP_SEC  = 6.1       # 10 req/min limit → one every 6s

OUT_CSV    = DATA_DIR / "nyt/nyt_kimchi_articles.csv"
CHECKPOINT = DATA_DIR / "nyt/nyt_checkpoint.json"


# ── checkpoint helpers ────────────────────────────────────────────────────────

def load_checkpoint() -> dict:
    """Return checkpoint dict: {year, page, total_fetched}. None if no checkpoint."""
    if CHECKPOINT.exists():
        with open(CHECKPOINT) as f:
            cp = json.load(f)
        log.info(f"Checkpoint found: resuming from year {cp['year']}, "
                 f"page {cp['page']} ({cp['total_fetched']} articles already saved)")
        return cp
    return {"year": START_YEAR, "page": 0, "total_fetched": 0}


def save_checkpoint(year: int, page: int, total_fetched: int):
    with open(CHECKPOINT, "w") as f:
        json.dump({"year": year, "page": page,
                   "total_fetched": total_fetched}, f)


# ── append to CSV ─────────────────────────────────────────────────────────────

def append_to_csv(rows: list[dict]):
    """Append rows to the output CSV, writing header only if file is new."""
    if not rows:
        return
    df = pd.DataFrame(rows)
    write_header = not OUT_CSV.exists()
    df.to_csv(OUT_CSV, mode="a", header=write_header,
              index=False, encoding="utf-8-sig")


# ── parse one page of hits ────────────────────────────────────────────────────

def parse_hits(hits: list[dict]) -> list[dict]:
    rows = []
    for h in hits:
        headline = (h.get("headline") or {})
        date_str = (h.get("pub_date") or "")[:10]
        rows.append({
            "date":     date_str,
            "year":     date_str[:4],
            "headline": headline.get("main", ""),
            "abstract": h.get("abstract", ""),
            "snippet":  h.get("snippet", ""),
            "section":  h.get("section_name", ""),
            "url":      h.get("web_url", ""),
        })
    return rows


# ── fetch ─────────────────────────────────────────────────────────────────────

def fetch_all():
    cp = load_checkpoint()
    current_year = cp["year"]
    start_page   = cp["page"]
    total_fetched = cp["total_fetched"]

    years = list(range(START_YEAR, END_YEAR + 1))
    # skip years fully completed before the checkpoint year
    years = [y for y in years if y >= current_year]

    for year in years:
        # for the checkpoint year, resume at the saved page
        # for subsequent years, always start at page 0
        first_page = start_page if year == current_year else 0
        page = first_page

        log.info(f"\nYear {year} — starting at page {page}")

        while True:
            params = {
                "q":          QUERY,
                "begin_date": f"{year}0101",
                "end_date":   f"{year}1231",
                "sort":       "oldest",
                "page":       page,
            }
            headers = {"X-Api-Key": NYT_KEY}

            try:
                r = requests.get(BASE_URL, params=params,
                                 headers=headers, timeout=30)

                if r.status_code == 429:
                    log.warning("Rate limited (429) — waiting 60s then retrying.")
                    save_checkpoint(year, page, total_fetched)
                    time.sleep(60)
                    continue

                r.raise_for_status()
                body = r.json().get("response", {})

            except Exception as e:
                log.warning(f"  [{year}] page {page} error: {e} — saving checkpoint.")
                save_checkpoint(year, page, total_fetched)
                return

            hits = body.get("docs", [])
            if not hits:
                break

            rows = parse_hits(hits)
            append_to_csv(rows)           # write to disk immediately
            total_fetched += len(rows)

            meta = body.get("meta", {})
            total_hits = meta.get("hits", 0)
            log.info(f"  [{year}] page {page}: {len(hits)} articles saved "
                     f"(year total: ~{total_hits}, all-time: {total_fetched})")

            # save checkpoint after every page
            save_checkpoint(year, page, total_fetched)

            if len(hits) < 10 or page >= 99:
                break

            page += 1
            time.sleep(SLEEP_SEC)

        # year complete — advance checkpoint to next year, page 0
        next_year = year + 1
        save_checkpoint(next_year, 0, total_fetched)
        log.info(f"  Year {year} complete.")
        time.sleep(SLEEP_SEC)

    # all done — clean up checkpoint and deduplicate output
    log.info("\nAll years complete. Deduplicating output CSV…")
    if OUT_CSV.exists():
        df = pd.read_csv(OUT_CSV, dtype=str)
        before = len(df)
        df = df.drop_duplicates(subset=["url"]).sort_values("date")
        df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
        log.info(f"Deduplicated: {before} → {len(df)} rows")

        by_year = df.groupby("year").size().rename("article_count")
        print(f"\nTotal articles: {len(df)}")
        print(f"\nArticles per year:\n{by_year.to_string()}")

    if CHECKPOINT.exists():
        CHECKPOINT.unlink()
        log.info("Checkpoint file removed (run complete).")


# ── main ──────────────────────────────────────────────────────────────────────

def main():

    if OUT_CSV.exists() and not CHECKPOINT.exists():
        # output exists but no checkpoint = previous run completed cleanly
        log.info(f"Output file already exists and no checkpoint found.")
        log.info(f"Delete {OUT_CSV} to start fresh, or delete specific years "
                 f"from the CSV and set START_YEAR accordingly.")
        df = pd.read_csv(OUT_CSV, dtype=str)
        print(f"Current file: {len(df)} articles, "
              f"years {df['year'].min()}–{df['year'].max()}")
        return

    log.info(f"NYT Article Search — query: '{QUERY}', "
             f"{START_YEAR}–{END_YEAR}")
    log.info(f"Output: {OUT_CSV}")
    log.info(f"Every page is saved immediately — safe to interrupt and resume.")
    fetch_all()


if __name__ == "__main__":
    main()

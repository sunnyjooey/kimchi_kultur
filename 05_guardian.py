"""
05_guardian.py
──────────────
Pull kimchi-related articles from The Guardian Open API.

Source   : The Guardian Open Platform
Docs     : https://open-platform.theguardian.com/documentation/
Coverage : 1999–present
Key      : Free, register at https://open-platform.theguardian.com/access/

What you get
────────────
- Article metadata: date, headline, section, tags, URL
- Full body text (requires `show-fields=body` — included here)
- Article counts by year as a cultural signal
- Body text for downstream LLM framing analysis
  (what narrative frames appear: health? cuisine? K-culture? fermentation?)

Setup
─────
1. Go to https://open-platform.theguardian.com/access/
2. Register for a developer key (free, instant)
3. Set env var:  export GUARDIAN_KEY="your-key-here"
   or paste it into GUARDIAN_KEY below.
   Note: without a key you can still use the "test" key but it's rate-limited.

Rate limits
───────────
Free developer key: 12 calls/second, 5000 calls/day — generous for this task.
This script makes ~1 call per page of results; total calls = (total articles / 200).

Output
──────
data/guardian_kimchi_articles.csv
  Columns: date, year, headline, section, tags, url, body_text
"""

import os
import time
import requests
import pandas as pd
from utils import log, save_csv, DATA_DIR

# ── config ────────────────────────────────────────────────────────────────────

GUARDIAN_KEY = os.getenv("GUARDIAN_KEY")

if not GUARDIAN_KEY:
    raise ValueError("GUARDIAN_KEY environment variable not set")

BASE_URL   = "https://content.guardianapis.com/search"
QUERY      = "kimchi"
PAGE_SIZE  = 200      # max allowed
SLEEP_SEC  = 0.5      # between pages — well within rate limits
FROM_DATE  = "1999-01-01"


# ── fetch ─────────────────────────────────────────────────────────────────────

def fetch_all() -> list[dict]:

    key = GUARDIAN_KEY

    params = {
        "q":             QUERY,
        "from-date":     FROM_DATE,
        "page-size":     PAGE_SIZE,
        "order-by":      "oldest",
        "show-fields":   "headline,bodyText,wordcount",
        "show-tags":     "keyword",
        "api-key":       key,
        "page":          1,
    }

    all_articles = []
    page = 1

    while True:
        params["page"] = page
        try:
            r = requests.get(BASE_URL, params=params, timeout=30)
            r.raise_for_status()
            body = r.json().get("response", {})
        except Exception as e:
            log.error(f"Page {page} failed: {e}")
            break

        status = body.get("status", "")
        if status != "ok":
            log.error(f"API returned status: {status}")
            break

        results = body.get("results", [])
        if not results:
            break

        all_articles.extend(results)
        total_pages = body.get("pages", 1)
        log.info(f"Page {page}/{total_pages}: {len(results)} articles "
                 f"(total so far: {len(all_articles)})")

        if page >= total_pages:
            break

        page += 1
        time.sleep(SLEEP_SEC)

    return all_articles


# ── parse ─────────────────────────────────────────────────────────────────────

def parse(articles: list[dict]) -> pd.DataFrame:
    rows = []
    for a in articles:
        fields = a.get("fields", {})
        tags   = a.get("tags", [])
        rows.append({
            "date":       a.get("webPublicationDate", "")[:10],
            "year":       a.get("webPublicationDate", "")[:4],
            "headline":   fields.get("headline", a.get("webTitle", "")),
            "section":    a.get("sectionName", ""),
            "tags":       "|".join(t.get("webTitle", "") for t in tags),
            "url":        a.get("webUrl", ""),
            "word_count": fields.get("wordcount", ""),
            "body_text":  fields.get("bodyText", ""),
        })
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.sort_values("date")
    return df.reset_index(drop=True)


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    log.info(f"Fetching Guardian articles matching '{QUERY}' from {FROM_DATE}…")
    articles = fetch_all()

    if not articles:
        log.error("No articles returned.")
        return

    df = parse(articles)
    save_csv(df, "guardian_kimchi_articles.csv", "Guardian kimchi articles")

    # summary
    by_year = df.groupby("year").size().rename("article_count")
    print(f"\nTotal articles: {len(df)}")
    print(f"\nArticles per year:\n{by_year.to_string()}")

    print(f"\nTop sections:\n{df['section'].value_counts().head(10).to_string()}")


if __name__ == "__main__":
    main()

"""
07_wikipedia.py
───────────────
Pull monthly Wikipedia pageview data for "Kimchi" articles across languages.

Source   : Wikimedia REST API (pageviews)
Docs     : https://wikitech.wikimedia.org/wiki/Analytics/AQS/Pageviews
Coverage : July 2015–present
Key      : None required — completely free and open

What you get
────────────
- Monthly view counts for the "Kimchi" Wikipedia article
- Across multiple language editions (configurable)
- A clean cultural-interest proxy that is:
    * Absolute (not relative like Google Trends)
    * Multilingual (can compare interest by language/region)
    * Stable (no rate-limit headaches, no unofficial wrapper)

Interpretation
──────────────
Pageviews ≠ search interest, but they're a strong proxy for people
actively looking up information about kimchi. Spikes often correspond to
media coverage events. The multilingual breakdown lets you track interest
in Korea (ko), Japan (ja), the US/UK (en), Europe (de, fr, nl), etc.

Languages tracked
─────────────────
en  English          ko  Korean          ja  Japanese
de  German           fr  French          nl  Dutch
zh  Chinese          es  Spanish         pt  Portuguese
id  Indonesian       th  Thai            vi  Vietnamese

Output
──────
data/wikipedia_kimchi_pageviews.csv
  Columns: year_month, date, language, article, views

data/wikipedia_kimchi_wide.csv
  Wide format: year_month as rows, language codes as columns
"""

import time
import requests
import pandas as pd
from utils import log, save_csv, DATA_DIR

# ── config ────────────────────────────────────────────────────────────────────

# Wikipedia article name for "kimchi" in each language
ARTICLES = {
    "en": "Kimchi",
    "ko": "김치",
    "ja": "キムチ",
    "de": "Kimchi",
    "fr": "Kimchi",
    "nl": "Kimchi",
    "zh": "泡菜",        # Note: zh uses 泡菜 which also covers other pickles
    "es": "Kimchi",
    "pt": "Kimchi",
    "id": "Kimchi",
    "th": "กิมจิ",
    "vi": "Kim chi",
}

# Pageviews API only goes back to 2015-07
START_YYYYMM = "2015-07"
SLEEP_SEC    = 0.5      # between requests — API is generous but be polite

BASE_URL = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article"
HEADERS  = {
    "User-Agent": "kimchi-export-research/1.0 (academic; contact via github)",
}


# ── fetch one article ─────────────────────────────────────────────────────────

def fetch_article(lang: str, article: str,
                  start: str, end: str) -> list[dict]:
    """
    Fetch monthly pageviews for one article.
    start/end format: YYYYMMDD (e.g. "20150701", "20241201")
    """
    # URL-encode the article name (spaces → underscores, special chars encoded)
    import urllib.parse
    article_enc = urllib.parse.quote(article.replace(" ", "_"), safe="")

    url = (f"{BASE_URL}/{lang}.wikipedia/all-access/all-agents"
           f"/{article_enc}/monthly/{start}/{end}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code == 404:
            log.warning(f"  [{lang}] article not found: {article}")
            return []
        r.raise_for_status()
        items = r.json().get("items", [])
        log.info(f"  [{lang}] {article}: {len(items)} months")
        return items
    except Exception as e:
        log.warning(f"  [{lang}] {article}: {e}")
        return []


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    from datetime import date
    today = date.today()
    # end on the last complete month
    if today.day < 2:
        end_dt = date(today.year, today.month, 1) - pd.DateOffset(months=1)
    else:
        end_dt = date(today.year, today.month, 1)

    start_str = START_YYYYMM.replace("-", "") + "01"   # e.g. "20150701"
    end_str   = end_dt.strftime("%Y%m") + "01"

    log.info(f"Fetching Wikipedia pageviews: {START_YYYYMM} → "
             f"{end_dt.strftime('%Y-%m')}")
    log.info(f"Languages: {list(ARTICLES.keys())}")

    all_rows = []

    for lang, article in ARTICLES.items():
        log.info(f"\n{lang}: {article}")
        items = fetch_article(lang, article, start_str, end_str)

        for item in items:
            ts = item.get("timestamp", "")   # format: "2015070100"
            all_rows.append({
                "year_month": ts[:6],         # "201507"
                "date":       f"{ts[:4]}-{ts[4:6]}-01",
                "language":   lang,
                "article":    article,
                "views":      item.get("views", 0),
            })

        time.sleep(SLEEP_SEC)

    if not all_rows:
        log.error("No data collected.")
        return

    df = pd.DataFrame(all_rows)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.sort_values(["language", "date"]).reset_index(drop=True)

    save_csv(df, "wikipedia_kimchi_pageviews.csv",
             "Wikipedia kimchi pageviews (long)")

    # wide format — easier for visualisation
    wide = (df.pivot_table(index="year_month", columns="language",
                           values="views", aggfunc="sum")
              .reset_index())
    save_csv(wide, "wikipedia_kimchi_wide.csv",
             "Wikipedia kimchi pageviews (wide)")

    # summary
    print(f"\nTotal months × languages: {len(df)}")
    print(f"\nAverage monthly views by language:")
    avg = (df.groupby("language")["views"].mean()
             .sort_values(ascending=False)
             .round(0).astype(int))
    print(avg.to_string())

    print(f"\nPeak month per language:")
    peaks = (df.loc[df.groupby("language")["views"].idxmax()]
               [["language", "year_month", "views"]]
               .set_index("language"))
    print(peaks.to_string())


if __name__ == "__main__":
    main()

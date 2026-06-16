"""
09_semantic_scholar.py
───────────────────────
Pull kimchi-related research papers from the Semantic Scholar Graph API.

Source   : Semantic Scholar Academic Graph API
Docs     : https://api.semanticscholar.org/api-docs/
Key      : Optional — free, raises rate limit. Register at
           https://www.semanticscholar.org/product/api#api-key

What you get
────────────
- Richer metadata than PubMed: citation counts, influential citation
  counts, fields of study, open-access PDF links
- Citation velocity — a "research momentum" signal complementary to
  PubMed's raw paper-volume signal
- Same underlying papers as PubMed in most cases, but also covers
  non-biomedical venues (food science, agriculture, anthropology journals
  that PubMed doesn't index)

Rate limits
───────────
Unauthenticated : 100 requests / 5 minutes (~1 req/3s)
With free key    : 1 request/second sustained, higher burst
Set env var SEMANTIC_SCHOLAR_KEY to use a key.

This script paginates through the /paper/search endpoint (max 100 results
per page) and respects rate limits with a sleep + 429 backoff.

Output
──────
data/semantic_scholar_kimchi_papers.csv
  Columns: paper_id, year, title, abstract, venue, citation_count,
           influential_citation_count, fields_of_study, is_open_access,
           open_access_pdf_url, doi, pmid
"""

import os
import time
import requests
import pandas as pd
from utils import log, save_csv, DATA_DIR

# ── config ────────────────────────────────────────────────────────────────────

S2_KEY = os.getenv("SEMANTIC_SCHOLAR_KEY", "")

BASE_URL  = "https://api.semanticscholar.org/graph/v1/paper/search"
QUERY     = "kimchi"
PAGE_SIZE = 100        # max allowed by /paper/search
SLEEP_SEC = 6.0 if not S2_KEY else 1.1   # unauthenticated tier is a SHARED pool
                                          # across all users worldwide without a
                                          # key — 100/5min nominal limit is often
                                          # already consumed by others. Slower
                                          # pacing reduces 403/429 hits.
MAX_RETRIES = 5

FIELDS = ",".join([
    "title", "year", "abstract", "venue", "citationCount",
    "influentialCitationCount", "fieldsOfStudy", "isOpenAccess",
    "openAccessPdf", "externalIds", "publicationDate",
])


# ── fetch ─────────────────────────────────────────────────────────────────────

def fetch_page(offset: int) -> dict:
    """Fetch one page of results, with 429 retry/backoff."""
    params = {
        "query":  QUERY,
        "fields": FIELDS,
        "limit":  PAGE_SIZE,
        "offset": offset,
    }
    headers = {}
    if S2_KEY:
        headers["x-api-key"] = S2_KEY

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(BASE_URL, params=params, headers=headers, timeout=30)

            if r.status_code == 429:
                wait = 10 * attempt
                log.warning(f"  Rate limited (429). Waiting {wait}s "
                           f"(attempt {attempt}/{MAX_RETRIES})…")
                time.sleep(wait)
                continue

            if r.status_code != 200:
                # Surface the actual error instead of failing silently.
                # 403 is common on the shared unauthenticated tier when
                # traffic from other users has exhausted the pool.
                log.error(f"  HTTP {r.status_code} on offset {offset}: "
                         f"{r.text[:300]}")
                if r.status_code in (401, 403) and attempt < MAX_RETRIES:
                    wait = 15 * attempt
                    log.warning(f"  Retrying in {wait}s "
                               f"(attempt {attempt}/{MAX_RETRIES})…")
                    time.sleep(wait)
                    continue
                return {}

            return r.json()

        except requests.exceptions.RequestException as e:
            log.warning(f"  Request exception on offset {offset}: {e}")
            time.sleep(5)

    log.error(f"  Offset {offset}: exhausted retries.")
    return {}


def fetch_all() -> list[dict]:
    all_papers = []
    offset = 0
    total = None

    while True:
        log.info(f"Fetching offset {offset}…")
        body = fetch_page(offset)
        if not body:
            break

        if total is None:
            total = body.get("total", 0)
            log.info(f"Total matches: {total}")

        papers = body.get("data", [])
        if not papers:
            break

        all_papers.extend(papers)
        log.info(f"  Retrieved {len(papers)} papers "
                 f"(total so far: {len(all_papers)}/{total})")

        offset += len(papers)
        if offset >= total or "next" not in body:
            break

        time.sleep(SLEEP_SEC)

    return all_papers


# ── parse ─────────────────────────────────────────────────────────────────────

def parse(papers: list[dict]) -> pd.DataFrame:
    rows = []
    for p in papers:
        external_ids = p.get("externalIds") or {}
        open_access  = p.get("openAccessPdf") or {}
        fields_of_study = p.get("fieldsOfStudy") or []

        rows.append({
            "paper_id":                    p.get("paperId", ""),
            "year":                        p.get("year"),
            "publication_date":            p.get("publicationDate", ""),
            "title":                       p.get("title", ""),
            "abstract":                    p.get("abstract", "") or "",
            "venue":                       p.get("venue", "") or "",
            "citation_count":              p.get("citationCount", 0),
            "influential_citation_count":  p.get("influentialCitationCount", 0),
            "fields_of_study":             "|".join(fields_of_study),
            "is_open_access":              p.get("isOpenAccess", False),
            "open_access_pdf_url":         open_access.get("url", ""),
            "doi":                         external_ids.get("DOI", ""),
            "pmid":                        external_ids.get("PubMed", ""),
        })

    df = pd.DataFrame(rows)
    df["year"] = pd.to_numeric(df["year"], errors="coerce")
    return df.sort_values("year")


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    log.info(f"Semantic Scholar — query: '{QUERY}'")
    if not S2_KEY:
        log.warning(
            "No API key set. The unauthenticated tier is a SHARED pool across "
            "ALL users worldwide without a key, so it's often already exhausted "
            "by other traffic — this commonly shows up as 403s with no clear "
            "cause. A free key removes this and gives you a dedicated 1 req/s. "
            "Register at: https://www.semanticscholar.org/product/api#api-key "
            "then: export SEMANTIC_SCHOLAR_KEY='your-key-here'"
        )
    log.info(f"Rate limit: {'keyed (~1 req/s)' if S2_KEY else 'unauthenticated (shared pool)'}")

    papers = fetch_all()
    if not papers:
        log.error("No papers retrieved.")
        return

    df = parse(papers)
    save_csv(df, "semantic_scholar_kimchi_papers.csv", "Semantic Scholar kimchi papers")

    # summary
    print(f"\nTotal papers: {len(df)}")
    by_year = df.groupby("year").size().rename("paper_count")
    print(f"\nPapers per year:\n{by_year.to_string()}")

    print(f"\nTop 10 most-cited papers:")
    top_cited = df.nlargest(10, "citation_count")[
        ["title", "year", "citation_count", "venue"]
    ]
    print(top_cited.to_string(index=False))

    print(f"\nFields of study distribution:")
    all_fields = df["fields_of_study"].str.split("|").explode()
    all_fields = all_fields[all_fields != ""]
    print(all_fields.value_counts().head(10).to_string())


if __name__ == "__main__":
    main()

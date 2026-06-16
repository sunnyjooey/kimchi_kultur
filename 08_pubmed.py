"""
08_pubmed.py
────────────
Pull kimchi-related biomedical research papers from PubMed via NCBI's
E-utilities API.

Source   : NCBI E-utilities (esearch + efetch)
Docs     : https://www.ncbi.nlm.nih.gov/books/NBK25501/
Coverage : 35M+ biomedical papers, full history
Key      : None required — optional free key raises rate limit

What you get
────────────
- Paper counts over time (research momentum signal)
- Titles, abstracts, MeSH terms, publication dates, journal names
- Abstracts for LLM framing analysis: is kimchi framed as a probiotic?
  anti-cancer? immunity-boosting? fermentation-science topic?

Rate limits
───────────
Without a key : 3 requests/second
With free key  : 10 requests/second
Get a free key at: https://www.ncbi.nlm.nih.gov/account/settings/
(API Key Management section, after creating an NCBI account)

This script defaults to the unauthenticated 3 req/s limit unless
PUBMED_KEY is set.

How this works
───────────────
Two-step E-utilities flow:
  1. esearch — search by keyword, get back a list of PMIDs (paper IDs)
  2. efetch  — fetch full records (title/abstract/MeSH/journal/date) for
               those PMIDs in batches of up to 200

Output
──────
data/pubmed_kimchi_papers.csv
  Columns: pmid, year, date, title, journal, abstract, mesh_terms
"""

import os
import time
import requests
import xml.etree.ElementTree as ET
import pandas as pd
from utils import log, save_csv, DATA_DIR

# ── config ────────────────────────────────────────────────────────────────────

PUBMED_KEY = os.getenv("PUBMED_KEY", "")   # optional — raises rate limit to 10/s

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

QUERY        = "kimchi"
RETMAX_SEARCH = 10000     # max UIDs per esearch call
FETCH_BATCH   = 200       # PMIDs per efetch call
SLEEP_SEC     = 0.34 if not PUBMED_KEY else 0.11   # 3/s vs 10/s


# ── step 1: search for PMIDs ────────────────────────────────────────────────

def search_pmids() -> list[str]:
    """Search PubMed for the query term, return all matching PMIDs."""
    params = {
        "db":      "pubmed",
        "term":    QUERY,
        "retmax":  RETMAX_SEARCH,
        "retmode": "json",
    }
    if PUBMED_KEY:
        params["api_key"] = PUBMED_KEY

    log.info(f"Searching PubMed for '{QUERY}'…")
    r = requests.get(ESEARCH_URL, params=params, timeout=30)
    r.raise_for_status()
    body = r.json().get("esearchresult", {})

    count = int(body.get("count", 0))
    pmids = body.get("idlist", [])
    log.info(f"Found {count} total matches; retrieved {len(pmids)} PMIDs "
             f"(retmax={RETMAX_SEARCH})")

    if count > RETMAX_SEARCH:
        log.warning(f"Total hits ({count}) exceed retmax ({RETMAX_SEARCH}) — "
                    f"some results were not retrieved. Consider raising RETMAX_SEARCH.")

    return pmids


# ── step 2: fetch full records ──────────────────────────────────────────────

def fetch_records(pmids: list[str]) -> list[dict]:
    """Fetch full records for a list of PMIDs, in batches."""
    all_rows = []
    n_batches = (len(pmids) + FETCH_BATCH - 1) // FETCH_BATCH

    for i in range(0, len(pmids), FETCH_BATCH):
        batch = pmids[i:i + FETCH_BATCH]
        batch_num = i // FETCH_BATCH + 1

        params = {
            "db":      "pubmed",
            "id":      ",".join(batch),
            "retmode": "xml",
        }
        if PUBMED_KEY:
            params["api_key"] = PUBMED_KEY

        try:
            r = requests.get(EFETCH_URL, params=params, timeout=30)
            r.raise_for_status()
            rows = parse_efetch_xml(r.content)
            all_rows.extend(rows)
            log.info(f"  Batch {batch_num}/{n_batches}: {len(rows)} records "
                     f"(total: {len(all_rows)})")
        except Exception as e:
            log.warning(f"  Batch {batch_num}/{n_batches} failed: {e}")

        time.sleep(SLEEP_SEC)

    return all_rows


def parse_efetch_xml(xml_content: bytes) -> list[dict]:
    """Parse a PubmedArticleSet XML response into row dicts."""
    root = ET.fromstring(xml_content)
    rows = []

    for article in root.findall(".//PubmedArticle"):
        pmid = article.findtext(".//PMID", default="")

        title = article.findtext(".//ArticleTitle", default="")

        # abstract may have multiple <AbstractText> sections (e.g. Background/Methods/...)
        abstract_parts = [
            (el.text or "") for el in article.findall(".//AbstractText")
        ]
        abstract = " ".join(p.strip() for p in abstract_parts if p).strip()

        journal = article.findtext(".//Journal/Title", default="")

        # publication date — prefer PubDate Year, fall back to MedlineDate
        year = article.findtext(".//Journal/JournalIssue/PubDate/Year", default="")
        medline_date = article.findtext(".//Journal/JournalIssue/PubDate/MedlineDate", default="")
        if not year and medline_date:
            year = medline_date[:4]   # e.g. "2015 Jan-Feb" → "2015"

        month = article.findtext(".//Journal/JournalIssue/PubDate/Month", default="")
        day   = article.findtext(".//Journal/JournalIssue/PubDate/Day", default="")
        date_str = "-".join(p for p in [year, month, day] if p)

        mesh_terms = [
            el.text for el in article.findall(".//MeshHeading/DescriptorName")
            if el.text
        ]

        rows.append({
            "pmid":       pmid,
            "year":       year,
            "date":       date_str,
            "title":      title,
            "journal":    journal,
            "abstract":   abstract,
            "mesh_terms": "|".join(mesh_terms),
        })

    return rows


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    log.info(f"PubMed E-utilities — query: '{QUERY}'")
    log.info(f"Rate limit: {'10/s (key set)' if PUBMED_KEY else '3/s (no key — set PUBMED_KEY to raise this)'}")

    pmids = search_pmids()
    if not pmids:
        log.error("No PMIDs found.")
        return

    rows = fetch_records(pmids)
    if not rows:
        log.error("No records retrieved.")
        return

    df = pd.DataFrame(rows)
    df["year"] = pd.to_numeric(df["year"], errors="coerce")
    df = df.sort_values("year")

    save_csv(df, "pubmed_kimchi_papers.csv", "PubMed kimchi papers")

    # summary
    print(f"\nTotal papers: {len(df)}")
    by_year = df.groupby("year").size().rename("paper_count")
    print(f"\nPapers per year:\n{by_year.to_string()}")

    # most common MeSH terms — gives a sense of framing
    all_mesh = df["mesh_terms"].str.split("|").explode()
    all_mesh = all_mesh[all_mesh != ""]
    print(f"\nTop 15 MeSH terms (framing signal):")
    print(all_mesh.value_counts().head(15).to_string())


if __name__ == "__main__":
    main()

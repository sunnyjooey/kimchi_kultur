"""
04_google_trends.py
───────────────────
Pull Google Trends data for "kimchi" across key markets.

Library  : pytrends (unofficial wrapper — no API key needed)
Term     : "kimchi"
Geo      : global + per country breakdown
Timeframe: 2004-01-01 to present

No API key needed. The library uses the public Google Trends interface.

Rate limits & 429 handling
───────────────────────────
Google doesn't publish rate limits, but the unofficial API triggers 429s if
you query too fast. This script handles them properly:

  - Exponential backoff with jitter on every 429 (up to MAX_RETRIES attempts)
  - After the first 429 the per-request sleep is permanently increased
  - Checkpoint saves: each completed section is written to disk immediately,
    so a 429 that kills the run doesn't lose earlier work
  - Resume flag: --resume skips any section whose output file already exists
  - If all retries are exhausted on a single request, that section is skipped
    and the rest of the run continues

If you keep hitting 429s: wait 10–15 minutes before retrying, or use --resume
to pick up where you left off.

Outputs
───────
data/trends_global_monthly.csv      — worldwide monthly interest (0–100 index)
data/trends_by_country_monthly.csv  — per-country monthly (wide format)
data/trends_by_country_snapshot.csv — interest by country, 5yr snapshot
data/trends_related_queries.csv     — top & rising related queries

Interpretation note
───────────────────
Google Trends returns a *relative* index (0–100), not absolute search volume.
100 = peak interest in the period; 50 = half the peak. Values are comparable
within one query's time-range but NOT across different calls with different
time-ranges. The global monthly series uses a single call so values ARE
comparable across time.
"""

import time
import random
import argparse
import pandas as pd
from pytrends.request import TrendReq
from pytrends.exceptions import TooManyRequestsError

from utils import log, save_csv, DATA_DIR

# ── config ────────────────────────────────────────────────────────────────────
KEYWORD                = "kimchi"
SLEEP_BASE             = 4.0    # seconds between requests (bumped up after first 429)
SLEEP_JITTER           = 1.5    # ± random jitter added to every sleep
MAX_RETRIES            = 6      # attempts per request before giving up
BACKOFF_BASE           = 60     # first retry wait in seconds (doubles each time)
BACKOFF_MAX            = 600    # cap at 10 minutes

# Countries to pull individual time series for (ISO2 codes)
TARGET_COUNTRIES = ["US", "JP", "NL", "DE", "GB", "AU", "CA", "FR", "SG", "KR"]

# ── state ─────────────────────────────────────────────────────────────────────
_sleep_current = SLEEP_BASE   # may be increased dynamically after 429s


def _sleep(base: float | None = None):
    """Sleep with jitter. Uses global _sleep_current if base not given."""
    t = (base if base is not None else _sleep_current) + random.uniform(0, SLEEP_JITTER)
    log.debug(f"  sleeping {t:.1f}s…")
    time.sleep(t)


def _backoff_sleep(attempt: int):
    """Exponential backoff after a 429. Also increases the global base sleep."""
    global _sleep_current
    wait = min(BACKOFF_BASE * (2 ** (attempt - 1)), BACKOFF_MAX)
    wait += random.uniform(0, wait * 0.2)   # ±20% jitter
    log.warning(f"  Rate limited (429). Waiting {wait:.0f}s before retry {attempt}/{MAX_RETRIES}…")
    # permanently increase per-request sleep to reduce future 429s
    _sleep_current = min(_sleep_current * 1.5, 30.0)
    log.info(f"  Base sleep increased to {_sleep_current:.1f}s for remainder of run.")
    time.sleep(wait)


# ── pytrends session ──────────────────────────────────────────────────────────

def build_pytrends() -> TrendReq:
    return TrendReq(
        hl="en-US",
        tz=0,
        timeout=(10, 60),
        retries=0,          # we handle retries ourselves
        backoff_factor=0,
    )


# ── generic retry wrapper ──────────────────────────────────────────────────────

def _with_retry(fn, label: str):
    """
    Call fn() with retry/backoff on TooManyRequestsError or similar 429s.
    Returns the result or None if all retries exhausted.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = fn()
            _sleep()   # polite delay after every successful call
            return result
        except TooManyRequestsError:
            if attempt == MAX_RETRIES:
                log.error(f"  {label}: exhausted {MAX_RETRIES} retries. Skipping.")
                return None
            _backoff_sleep(attempt)
        except Exception as e:
            err = str(e)
            # pytrends sometimes wraps 429 in a generic exception
            if "429" in err or "too many" in err.lower() or "rate" in err.lower():
                if attempt == MAX_RETRIES:
                    log.error(f"  {label}: exhausted {MAX_RETRIES} retries. Skipping.")
                    return None
                _backoff_sleep(attempt)
            else:
                log.warning(f"  {label}: unexpected error — {e}")
                return None
    return None


# ── fetch functions ────────────────────────────────────────────────────────────

def fetch_global_monthly(pt: TrendReq) -> pd.DataFrame:
    """Monthly global interest since 2004."""
    log.info("Fetching global monthly trend (2004–present)…")

    def _call():
        pt.build_payload(
            kw_list=  [KEYWORD],
            timeframe="2004-01-01 2025-12-31",
            geo=      "",
            gprop=    "",
        )
        return pt.interest_over_time()

    df = _with_retry(_call, "global monthly")
    if df is None or df.empty:
        log.warning("Global monthly: empty or failed.")
        return pd.DataFrame()

    df = df.drop(columns=["isPartial"], errors="ignore")
    df.index.name = "date"
    df = df.reset_index()
    df.columns = ["date", "interest"]
    df["geo"] = "GLOBAL"
    log.info(f"Global monthly: {len(df)} rows, {df['date'].min()} → {df['date'].max()}")
    return df


def fetch_country_series(pt: TrendReq, country_code: str) -> pd.DataFrame:
    """Monthly trend for a single country."""
    log.info(f"  Fetching country series: {country_code}…")

    def _call():
        pt.build_payload(
            kw_list=  [KEYWORD],
            timeframe="2004-01-01 2025-12-31",
            geo=      country_code,
            gprop=    "",
        )
        return pt.interest_over_time()

    df = _with_retry(_call, f"country {country_code}")
    if df is None or df.empty:
        log.warning(f"  {country_code}: no data returned (skipped).")
        return pd.DataFrame()

    df = df.drop(columns=["isPartial"], errors="ignore")
    df.index.name = "date"
    df = df.reset_index()
    df.columns = ["date", "interest"]
    df["geo"] = country_code
    return df


def fetch_by_country_snapshot(pt: TrendReq) -> pd.DataFrame:
    """Interest by country (5yr snapshot)."""
    log.info("Fetching interest-by-country snapshot (5yr)…")

    def _call():
        pt.build_payload(
            kw_list=  [KEYWORD],
            timeframe="today 5-y",
            geo=      "",
            gprop=    "",
        )
        return pt.interest_by_region(resolution="COUNTRY", inc_low_vol=True)

    df = _with_retry(_call, "country snapshot")
    if df is None or df.empty:
        log.warning("Country snapshot: empty or failed.")
        return pd.DataFrame()

    df = df.reset_index()
    df.columns = ["country", "interest"]
    df = df[df["interest"] > 0].sort_values("interest", ascending=False)
    log.info(f"Country snapshot: {len(df)} countries")
    return df


def fetch_related_queries(pt: TrendReq) -> pd.DataFrame:
    """Top & rising related queries."""
    log.info("Fetching related queries…")

    def _call():
        pt.build_payload(
            kw_list=  [KEYWORD],
            timeframe="today 5-y",
            geo=      "",
            gprop=    "",
        )
        return pt.related_queries()

    related = _with_retry(_call, "related queries")
    if not related:
        log.warning("Related queries: empty or failed.")
        return pd.DataFrame()

    frames = []
    for kw, data in related.items():
        for qtype in ["top", "rising"]:
            if data.get(qtype) is not None and not data[qtype].empty:
                sub = data[qtype].copy()
                sub["keyword"]    = kw
                sub["query_type"] = qtype
                frames.append(sub)

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Pull Google Trends data for kimchi")
    parser.add_argument(
        "--resume", action="store_true",
        help="Skip any section whose output CSV already exists in data/"
    )
    args = parser.parse_args()

    def already_done(filename: str) -> bool:
        if not args.resume:
            return False
        path = DATA_DIR / filename
        if path.exists():
            log.info(f"  --resume: {filename} already exists, skipping.")
            return True
        return False

    pt = build_pytrends()

    # ── 1. global monthly ──────────────────────────────────────────────────────
    global_df = pd.DataFrame()
    fname_global = "trends_global_monthly.csv"
    if already_done(fname_global):
        global_df = pd.read_csv(DATA_DIR / fname_global)
    else:
        global_df = fetch_global_monthly(pt)
        if not global_df.empty:
            save_csv(global_df, fname_global, "Google Trends global monthly")
            peaks = global_df.nlargest(5, "interest")[["date", "interest"]]
            print(f"\nGlobal trend peaks:\n{peaks.to_string(index=False)}")
        else:
            log.warning("Global monthly fetch failed or returned nothing.")

    # ── 2. per-country monthly series ─────────────────────────────────────────
    fname_countries = "trends_by_country_monthly.csv"
    if already_done(fname_countries):
        log.info("  Per-country monthly: using existing file.")
    else:
        country_frames = [global_df] if not global_df.empty else []
        failed = []

        for code in TARGET_COUNTRIES:
            df = fetch_country_series(pt, code)
            if not df.empty:
                country_frames.append(df)
            else:
                failed.append(code)

        if failed:
            log.warning(f"  Countries with no data / failed: {failed}")

        if len(country_frames) > 1:
            all_countries = pd.concat(country_frames, ignore_index=True)
            wide = all_countries.pivot_table(
                index="date", columns="geo", values="interest"
            ).reset_index()
            save_csv(wide, fname_countries, "Google Trends per-country monthly (wide)")
        elif not country_frames:
            log.warning("No country series data collected.")

    # ── 3. country snapshot ────────────────────────────────────────────────────
    fname_snapshot = "trends_by_country_snapshot.csv"
    if already_done(fname_snapshot):
        log.info("  Country snapshot: using existing file.")
    else:
        snapshot = fetch_by_country_snapshot(pt)
        if not snapshot.empty:
            save_csv(snapshot, fname_snapshot, "Google Trends country snapshot (5yr)")
            print(f"\nTop 10 countries by search interest:\n{snapshot.head(10).to_string(index=False)}")
        else:
            log.warning("Country snapshot failed or returned nothing.")

    # ── 4. related queries ─────────────────────────────────────────────────────
    fname_related = "trends_related_queries.csv"
    if already_done(fname_related):
        log.info("  Related queries: using existing file.")
    else:
        related = fetch_related_queries(pt)
        if not related.empty:
            save_csv(related, fname_related, "Google Trends related queries")
            top = related[related["query_type"] == "top"].head(15)
            print(f"\nTop related queries:\n{top[['query','value']].to_string(index=False)}")
        else:
            log.warning("Related queries failed or returned nothing.")

    log.info("Google Trends pull complete.")


if __name__ == "__main__":
    main()

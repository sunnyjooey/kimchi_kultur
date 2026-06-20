"""
11_youtube.py
─────────────
Pull YouTube video volume + engagement mentioning "kimchi" as a social-interest
signal, to complement Stream 2 (Guardian/NYT media) and Google Trends.

Source   : YouTube Data API v3 (developers.google.com/youtube/v3)
Method   : search.list, one call per calendar year (publishedAfter/Before),
           order=date, fully paginated within each year before moving on.
           Then videos.list to attach view/like/comment counts per video.

REVISION NOTE 2 — quota errors can arrive as HTTP 429, not just 403
─────────────────────────────────────────────────────────────────────
In practice, YouTube returned the daily search quota error as HTTP 429
("rateLimitExceeded" reason) rather than 403. The original revision only
checked for status 403, so a 429 quota error fell through into the
generic "partial_error" branch — exactly the kind of silent mislabeling
this script is supposed to prevent. Fixed by checking the actual error
reason in the response body (via _is_quota_error) rather than trusting
a single status code, since Google is inconsistent about which of the
two it returns for quota exhaustion.

REVISION NOTE 1 — fixes silent partial-year truncation
──────────────────────────────────────────────────────
An earlier version of this script could silently return a partial year's
worth of results if a 403/quota error or other HTTP error interrupted
pagination mid-year, with no way to tell a "complete" year from a
"stopped early" one apart by looking at the output. This version fixes
that by:
  1. Tracking an explicit per-year status: "complete", "capped" (hit the
     500-result ceiling), "partial_error" (stopped early on a non-quota
     HTTP error), or "not_attempted" (quota ran out before reaching it).
  2. Raising immediately on a 403 and DISCARDING that year's partial items
     rather than keeping them — a year is either fully fetched or absent,
     never half-counted.
  3. Checkpointing progress to data/youtube_year_status.csv after every
     year, so re-running the script after a quota reset automatically
     resumes from where it left off instead of re-fetching completed
     years (which would waste quota for no reason).
  4. Writing the status + a human-readable note into
     youtube_kimchi_yearly_volume.csv directly, so any downstream chart
     or analysis can filter out unreliable years instead of trusting
     video_count blindly.

Run with --fresh to ignore the checkpoint and re-fetch every year from
scratch (e.g. if you suspect the prior pull was corrupted some other way).

IMPORTANT — quota design
─────────────────────────
search.list and videos.insert each have their OWN daily cap of 100 calls,
separate from the general 10,000-unit/day pool. videos.list (statistics) is
cheap — 1 unit per call — and shares the general pool, not the search cap.

This script is deliberately designed to use at most ~1-2 search.list calls
per year (≈20-40 total for a 2007-2025 pull), well under the 100/day search
cap. If you widen the keyword list or shorten the per-call window, recompute
your search.list budget before running — going over silently fails with a
403 until the next midnight Pacific Time reset.

YouTube also caps any single query at roughly 500 total results before
relevance degrades and pagination stops being reliable — splitting by year
keeps each window's result count well under that ceiling for a niche term
like "kimchi", but if a particular year returns close to 500 ("capped"
status), treat that year's count as a floor, not a precise total.

Setup
─────
1. Go to https://console.cloud.google.com
2. Create a project (or select an existing one)
3. APIs & Services → Library → search "YouTube Data API v3" → Enable
4. APIs & Services → Credentials → Create Credentials → API key
5. Copy the key. No OAuth needed for search/read — an API key is sufficient.
6. Set env var:  export YOUTUBE_API_KEY="your-key-here"
   or paste it below into YOUTUBE_API_KEY.

No approval wait, no credit card required for the free quota tier.

Output
──────
data/youtube_kimchi_videos.csv
  Columns: video_id, published_at, year, title, description, channel_title,
           view_count, like_count, comment_count, duration_iso, url

data/youtube_kimchi_yearly_volume.csv
  Columns: year, video_count, total_views, total_likes, total_comments,
           status, note
  status is "complete", "capped", "partial_error", or "not_attempted" —
  ALWAYS check this column before plotting video_count as a trend.

data/youtube_year_status.csv
  Checkpoint file — {year, status, note}. Safe to delete to force a full
  re-fetch of all years on the next run (same effect as --fresh).
"""

import os
import time
from datetime import datetime

import pandas as pd
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from utils import log, save_csv, DATA_DIR

# ── config ──────────────────────────────────────────────────────────────────
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "YOUR_KEY_HERE")

KEYWORD     = "kimchi"
START_YEAR  = 2007     # matches Comtrade window for easy cross-stream alignment
END_YEAR    = 2025      # exclude 2026 — partial year, same convention as other streams

SLEEP_SEC       = 0.3   # courtesy delay between calls
MAX_PER_YEAR    = 500   # YouTube's practical ceiling per query — see module docstring
PAGE_SIZE       = 50    # max allowed by API


def build_client():
    if YOUTUBE_API_KEY == "YOUR_KEY_HERE":
        raise ValueError(
            "Set your YouTube Data API key:\n"
            "  export YOUTUBE_API_KEY='your-key-here'\n"
            "or edit YOUTUBE_API_KEY in this file.\n\n"
            "Get a key at: https://console.cloud.google.com "
            "(enable 'YouTube Data API v3', then create an API key credential)."
        )
    return build("youtube", "v3", developerKey=YOUTUBE_API_KEY)


class QuotaExceededError(Exception):
    """Raised when search.list reports quota exhaustion (HTTP 403 or 429 —
    Google uses both depending on which limit was hit). Signals the caller
    to stop the whole run rather than silently moving to the next year."""
    pass


def _is_quota_error(e: HttpError) -> bool:
    """
    Google's API returns quota-exceeded errors as EITHER 403 or 429
    depending on which limit was hit (daily quota vs. per-minute rate),
    so checking status code alone is unreliable — confirmed in practice
    when a 'Search Queries per day' quota error came back as 429, not 403.

    This checks the actual error reason/content from the response body,
    which is more dependable than the status code.
    """
    status = getattr(e.resp, "status", None)
    if status not in (403, 429):
        return False

    quota_signals = ("quotaexceeded", "ratelimitexceeded", "userratelimitexceeded",
                      "quota exceeded", "rate limit")
    try:
        error_text = e.content.decode("utf-8", errors="ignore").lower()
    except Exception:
        error_text = str(e).lower()

    return any(signal in error_text for signal in quota_signals)


def search_year(youtube, year: int) -> dict:
    """
    Walk every page for one calendar year before returning. Returns a dict:
      {
        "items": [...],          # video metadata collected (snippet only)
        "status": "complete" | "partial_error" | "capped",
        "pages_fetched": int,
        "note": str,              # human-readable explanation of status
      }

    status meanings:
      complete      — full year retrieved, nextPageToken exhausted normally,
                       totalResults stayed under MAX_PER_YEAR
      capped        — hit MAX_PER_YEAR; YouTube's reliable ceiling reached,
                       so this is a floor, not an exact count
      partial_error — pagination was interrupted by a non-quota HTTP error
                       partway through the year; DO NOT treat len(items) as
                       a real count for this year — re-run it specifically
    """
    published_after  = f"{year}-01-01T00:00:00Z"
    published_before = f"{year + 1}-01-01T00:00:00Z"

    items = []
    page_token = None
    pages_fetched = 0
    status = "complete"
    note = ""

    while True:
        try:
            request = youtube.search().list(
                part            = "snippet",
                q               = KEYWORD,
                type            = "video",
                order           = "date",
                publishedAfter  = published_after,
                publishedBefore = published_before,
                maxResults      = PAGE_SIZE,
                pageToken       = page_token,
            )
            response = request.execute()
        except HttpError as e:
            if _is_quota_error(e):
                # Quota exhausted — stop the ENTIRE run, not just this year.
                # Returning partial results here would silently corrupt the
                # year's count, which is exactly the bug this revision fixes.
                # Note: Google returns either 403 OR 429 for quota errors
                # depending on which limit was hit, so we check the error
                # reason/content rather than trusting a single status code.
                log.error(
                    f"  Quota exceeded on year {year} (page {pages_fetched + 1}): {e}\n"
                    f"  search.list daily cap (100/day) likely exhausted."
                )
                raise QuotaExceededError(
                    f"Quota exceeded while fetching year {year}, "
                    f"page {pages_fetched + 1}. {len(items)} partial items "
                    f"collected for this year are DISCARDED — re-run after "
                    f"the next midnight Pacific Time reset to get a clean "
                    f"year. Already-completed years are unaffected if you "
                    f"used --resume (see checkpoint file)."
                ) from e

            # Non-quota error mid-pagination (e.g. 500, network blip).
            # This year is now suspect — mark it rather than pretend it's done.
            status = "partial_error"
            note = f"Stopped at page {pages_fetched + 1} due to: {e}"
            log.warning(f"  Year {year}: {note}")
            break

        pages_fetched += 1

        for item in response.get("items", []):
            items.append({
                "video_id":      item["id"]["videoId"],
                "published_at":  item["snippet"]["publishedAt"],
                "title":         item["snippet"]["title"],
                "description":   item["snippet"]["description"],
                "channel_title": item["snippet"]["channelTitle"],
            })

        page_token = response.get("nextPageToken")
        total_results = response.get("pageInfo", {}).get("totalResults", 0)

        if len(items) >= MAX_PER_YEAR:
            status = "capped"
            note = (f"Hit the {MAX_PER_YEAR}-result ceiling "
                    f"(totalResults reported as {total_results}) — "
                    f"treat this year's count as a floor, not exact.")
            log.warning(f"  Year {year}: {note}")
            break

        if not page_token:
            # Normal completion — every page walked, no errors.
            break

        time.sleep(SLEEP_SEC)

    log.info(f"  Year {year}: {len(items)} videos, status={status} "
             f"({pages_fetched} search.list call(s))")

    return {
        "items": items,
        "status": status,
        "pages_fetched": pages_fetched,
        "note": note,
    }


def attach_statistics(youtube, videos: list[dict]) -> list[dict]:
    """
    Batch video IDs into videos.list calls (50 IDs per call, 1 quota unit
    each) to attach view/like/comment counts and duration.
    """
    if not videos:
        return videos

    by_id = {v["video_id"]: v for v in videos}
    ids = list(by_id.keys())

    for i in range(0, len(ids), 50):
        batch_ids = ids[i:i + 50]
        try:
            response = youtube.videos().list(
                part = "statistics,contentDetails",
                id   = ",".join(batch_ids),
            ).execute()
        except HttpError as e:
            log.warning(f"  videos.list batch failed: {e}")
            continue

        for item in response.get("items", []):
            vid = item["id"]
            stats = item.get("statistics", {})
            content = item.get("contentDetails", {})
            by_id[vid]["view_count"]    = int(stats.get("viewCount", 0))
            by_id[vid]["like_count"]    = int(stats.get("likeCount", 0)) if "likeCount" in stats else None
            by_id[vid]["comment_count"] = int(stats.get("commentCount", 0)) if "commentCount" in stats else None
            by_id[vid]["duration_iso"]  = content.get("duration")

        time.sleep(SLEEP_SEC)

    return list(by_id.values())


# ── checkpoint helpers ──────────────────────────────────────────────────────
# Tracks which years are done so a quota cutoff doesn't force re-fetching
# years that already completed cleanly.

CHECKPOINT_FILE = "youtube_year_status.csv"


def load_checkpoint() -> dict:
    """Returns {year: status} for years already attempted in a prior run."""
    path = DATA_DIR / CHECKPOINT_FILE
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    return dict(zip(df["year"], df["status"]))


def save_checkpoint(year_status: dict, year_notes: dict):
    df = pd.DataFrame([
        {"year": y, "status": s, "note": year_notes.get(y, "")}
        for y, s in sorted(year_status.items())
    ])
    df.to_csv(DATA_DIR / CHECKPOINT_FILE, index=False, encoding="utf-8-sig")


def fetch_all(resume: bool = True) -> tuple[pd.DataFrame, dict, dict]:
    youtube = build_client()
    years = list(range(START_YEAR, END_YEAR + 1))

    prior_status = load_checkpoint() if resume else {}
    already_done = {
        y for y, s in prior_status.items()
        if s in ("complete", "capped") and y in years
    }
    if already_done:
        log.info(f"Resuming: {len(already_done)} year(s) already completed "
                  f"in a prior run will be skipped: {sorted(already_done)}")

    years_to_fetch = [y for y in years if y not in already_done]
    log.info(f"Fetching YouTube data for {len(years_to_fetch)} year(s): "
             f"{years_to_fetch}")
    log.info(f"Estimated search.list calls: ~{len(years_to_fetch)}–"
              f"{len(years_to_fetch)*2} (well under the 100/day cap)")

    all_videos = []
    # IMPORTANT: only seed year_status with years that are genuinely
    # already_done (complete/capped). Seeding it with the FULL prior_status
    # dict was the source of a real bug — a year about to be re-fetched
    # would inherit its OLD status (e.g. a stale "partial_error" from a
    # previous broken run) and keep that stale value if the re-fetch
    # raised QuotaExceededError before reaching the assignment below,
    # because the exception handler's "if year not in year_status" check
    # would see it as already present and skip correcting it.
    year_status = {y: s for y, s in prior_status.items() if y in already_done}
    year_notes  = {}

    # If resuming, we still need the previously-collected videos for
    # completed years — but this script writes the raw video CSV fresh
    # each run, so completed years must be re-loaded from the prior raw
    # output rather than re-fetched (re-fetching would burn quota for no
    # reason). Load them if available.
    if already_done:
        prior_csv = DATA_DIR / "youtube_kimchi_videos.csv"
        if prior_csv.exists():
            prior_df = pd.read_csv(prior_csv)
            prior_df = prior_df[prior_df["year"].isin(already_done)]
            all_videos.extend(prior_df.to_dict("records"))
            log.info(f"  Reloaded {len(prior_df)} videos from prior raw "
                     f"output for completed years.")
        else:
            log.warning("  No prior raw CSV found to reload completed "
                        "years from — those years will show 0 videos "
                        "unless re-fetched. Consider running with "
                        "resume=False.")

    try:
        for year in years_to_fetch:
            result = search_year(youtube, year)
            all_videos.extend(result["items"])
            year_status[year] = result["status"]
            year_notes[year]  = result["note"]
            # checkpoint after every year so a later quota error doesn't
            # lose progress on years already completed in THIS run
            save_checkpoint(year_status, year_notes)
    except QuotaExceededError as e:
        log.error(f"\nStopping run early: {e}")
        # any year in years_to_fetch not yet in year_status was never
        # reached this run — record that explicitly so it's visible in
        # the output rather than just absent
        for year in years_to_fetch:
            if year not in year_status:
                year_status[year] = "not_attempted"
                year_notes[year]  = "Quota ran out before this year was reached."
        save_checkpoint(year_status, year_notes)
        log.info(f"Checkpoint saved — re-run the script later to pick up "
                 f"remaining years automatically.")
        # fall through and save whatever was collected so far, clearly
        # flagged via year_status, rather than losing it entirely

    log.info(f"Total videos in hand: {len(all_videos)} — attaching statistics…")
    all_videos = attach_statistics(youtube, all_videos)

    df = pd.DataFrame(all_videos)
    if not df.empty:
        # format='mixed' is required here because resumed runs combine two
        # different timestamp string formats in the same column: videos
        # reloaded from a prior CSV write come back as
        # "2022-12-31 19:42:35+00:00" (pandas' own round-trip format),
        # while freshly-fetched videos from this run are still raw
        # YouTube API strings like "2022-12-31T19:42:35Z". Without
        # format='mixed', pandas locks onto whichever format the first
        # row uses and raises a ValueError the moment it hits a row in
        # the other format — which is exactly what happened here, right
        # at the boundary between reloaded and freshly-fetched rows.
        df["published_at"] = pd.to_datetime(df["published_at"], utc=True, format="mixed")
        df["year"] = df["published_at"].dt.year
        df["url"] = "https://youtube.com/watch?v=" + df["video_id"]
        df = df.sort_values("published_at").reset_index(drop=True)

    return df, year_status, year_notes


def build_yearly_volume(df: pd.DataFrame, year_status: dict, year_notes: dict) -> pd.DataFrame:
    """Aggregate to year-level counts and attach the reliability status —
    the plotting-ready file, with no silent ambiguity about which years
    are trustworthy."""
    if df.empty:
        yearly = pd.DataFrame(columns=[
            "year", "video_count", "total_views", "total_likes",
            "total_comments", "status", "note"
        ])
    else:
        yearly = (
            df.groupby("year")
            .agg(
                video_count    = ("video_id", "count"),
                total_views    = ("view_count", "sum"),
                total_likes    = ("like_count", "sum"),
                total_comments = ("comment_count", "sum"),
            )
            .reset_index()
        )

    # years that were attempted but produced zero rows (e.g. quota hit on
    # page 1) still need a row so the gap is visible, not silently absent
    all_attempted_years = sorted(year_status.keys())
    existing_years = yearly["year"].tolist()
    full_index = sorted(set(all_attempted_years) | set(existing_years))
    yearly = yearly.set_index("year").reindex(full_index).reset_index()
    yearly[["video_count", "total_views", "total_likes", "total_comments"]] = (
        yearly[["video_count", "total_views", "total_likes", "total_comments"]].fillna(0)
    )

    yearly["status"] = yearly["year"].map(year_status).fillna("not_attempted")
    yearly["note"]   = yearly["year"].map(year_notes).fillna("")

    return yearly


def main():
    import sys
    fresh = "--fresh" in sys.argv  # ignore checkpoint, re-fetch everything

    df, year_status, year_notes = fetch_all(resume=not fresh)

    if df.empty and not year_status:
        log.warning("No data fetched — check your API key and quota status.")
        return

    save_csv(df, "youtube_kimchi_videos.csv", "YouTube kimchi videos (raw)")

    yearly = build_yearly_volume(df, year_status, year_notes)
    save_csv(yearly, "youtube_kimchi_yearly_volume.csv",
             "YouTube kimchi yearly volume (aggregated, status-flagged)")

    print(f"\nTotal videos collected: {len(df):,}")
    print(f"\nYearly volume with reliability status:")
    print(yearly[["year", "video_count", "status"]].to_string(index=False))

    unreliable = yearly[yearly["status"].isin(["capped", "partial_error", "not_attempted"])]
    if not unreliable.empty:
        print(f"\n⚠ {len(unreliable)} year(s) are NOT safe to plot as exact "
              f"counts — see the 'status' and 'note' columns:")
        print(unreliable[["year", "video_count", "status", "note"]].to_string(index=False))
        print(
            "\n'capped'        → hit the 500-result ceiling; true count is higher\n"
            "'partial_error' → pagination stopped early due to a non-quota error;\n"
            "                  re-run with --fresh, or delete that year's checkpoint\n"
            "                  row in data/youtube_year_status.csv to retry it\n"
            "'not_attempted' → quota ran out before this year was reached;\n"
            "                  just run the script again to pick up from here"
        )

    if not df.empty:
        print(f"\nTop 10 videos by view count:")
        top10 = df.nlargest(10, "view_count")[["title", "channel_title", "year", "view_count"]]
        print(top10.to_string(index=False))


if __name__ == "__main__":
    main()

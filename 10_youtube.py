"""
11_youtube.py
─────────────
Pull YouTube video volume + engagement mentioning "kimchi" as a social-interest
signal, to complement Stream 2 (Guardian/NYT media) and Google Trends.

Source   : YouTube Data API v3 (developers.google.com/youtube/v3)
Method   : search.list, one call per calendar year (publishedAfter/Before),
           order=date, paginated only if a year has >50 results.
           Then videos.list to attach view/like/comment counts per video.

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
like "kimchi", but if a particular year returns close to 500, treat that
year's count as a floor, not a precise total, and consider splitting it by
month for a one-off re-run.

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
  Columns: year, video_count, total_views, total_likes, total_comments
  (aggregated — this is the file you'll plot alongside trade/media/academic/trends)
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


def search_year(youtube, year: int) -> list[dict]:
    """
    One search.list call (plus pagination only if needed) for videos
    published in the given calendar year. Returns list of video metadata
    dicts (snippet only — no statistics yet).
    """
    published_after  = f"{year}-01-01T00:00:00Z"
    published_before = f"{year + 1}-01-01T00:00:00Z"

    items = []
    page_token = None
    pages_fetched = 0

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
            if e.resp.status == 403:
                log.error(
                    f"  Quota exceeded or forbidden on year {year}: {e}\n"
                    f"  search.list daily cap (100/day) may be exhausted — "
                    f"stopping early. Re-run tomorrow after PT midnight reset."
                )
                raise
            log.warning(f"  HTTP error on year {year}: {e}")
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

        if not page_token or len(items) >= MAX_PER_YEAR:
            if total_results > MAX_PER_YEAR:
                log.warning(
                    f"  Year {year}: totalResults={total_results} exceeds "
                    f"the ~{MAX_PER_YEAR} reliable ceiling — treat this "
                    f"year's count as a floor, not exact."
                )
            break

        time.sleep(SLEEP_SEC)

    log.info(f"  Year {year}: {len(items)} videos found ({pages_fetched} search.list call(s))")
    return items


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


def fetch_all() -> pd.DataFrame:
    youtube = build_client()
    years = list(range(START_YEAR, END_YEAR + 1))
    log.info(f"Fetching YouTube data for {len(years)} years ({START_YEAR}–{END_YEAR})")
    log.info(f"Estimated search.list calls: ~{len(years)}–{len(years)*2} "
              f"(well under the 100/day cap)")

    all_videos = []
    for year in years:
        videos = search_year(youtube, year)
        all_videos.extend(videos)

    log.info(f"Total videos found: {len(all_videos)} — attaching statistics…")
    all_videos = attach_statistics(youtube, all_videos)

    df = pd.DataFrame(all_videos)
    if df.empty:
        return df

    df["published_at"] = pd.to_datetime(df["published_at"], utc=True)
    df["year"] = df["published_at"].dt.year
    df["url"] = "https://youtube.com/watch?v=" + df["video_id"]
    df = df.sort_values("published_at").reset_index(drop=True)

    return df


def build_yearly_volume(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate to year-level counts — the plotting-ready file."""
    yearly = (
        df.groupby("year")
        .agg(
            video_count   = ("video_id", "count"),
            total_views   = ("view_count", "sum"),
            total_likes   = ("like_count", "sum"),
            total_comments= ("comment_count", "sum"),
        )
        .reset_index()
    )
    return yearly


def main():
    df = fetch_all()

    if df.empty:
        log.warning("No data fetched — check your API key and quota status.")
        return

    save_csv(df, "youtube_kimchi_videos.csv", "YouTube kimchi videos (raw)")

    yearly = build_yearly_volume(df)
    save_csv(yearly, "youtube_kimchi_yearly_volume.csv", "YouTube kimchi yearly volume (aggregated)")

    print(f"\nTotal videos collected: {len(df):,}")
    print(f"\nVideos per year:")
    print(yearly.to_string(index=False))

    print(f"\nTop 10 videos by view count:")
    top10 = df.nlargest(10, "view_count")[["title", "channel_title", "year", "view_count"]]
    print(top10.to_string(index=False))


if __name__ == "__main__":
    main()

"""
03_kati_download.py
───────────────────
Download kimchi export statistics from the official aT (한국농수산식품유통공사)
dataset published on data.go.kr / api.odcloud.kr.

Dataset  : 한국농수산식품유통공사_품목별 연도별 수출실적
Provider : Korea Agro-Fisheries & Food Trade Corporation (aT / KATI)
API key  : set KATI_KEY env var, or it falls back to the hardcoded dev key below

What this API provides
──────────────────────
Annual export totals by PRODUCT (품명), not by destination country.
Each endpoint is a static annual snapshot comparing two consecutive years:

  Endpoint A  →  2021 vs 2022  (_20221231)
  Endpoint B  →  2022 vs 2023  (_20231231)
  Endpoint C  →  2023 vs 2024  (_20241231)

Fields per row:
  구분                  category (식품 = food, 농산물 = agricultural product, …)
  품명                  product name (Korean) — e.g. 김치
  {YY}년 금액(달러)      export value USD for year YY
  {YY}년 중량(키로그램)   export weight KG for year YY
  전년대비 금액(퍼센트)   YoY change in value %
  전년대비 중량(퍼센트)   YoY change in weight %

Kimchi filtering
──────────────────
The API returns all food products; this script fetches everything and
filters to rows where 품명 contains "김치". Inspect the combined CSV to
verify no kimchi variants are being missed.

Limitations
───────────
- Annual totals only (no monthly, no by-country breakdown)
- Only 2021–2024 covered by the three current endpoints
- For historical data (2010–2020) or country-level breakdown, use
  01_comtrade.py (annual, by country, back to 2000) or
  02_korea_customs.py (monthly, by country, back to 2010)

Output
──────
data/kati_raw_YYYY.csv          ← raw full-product file per snapshot year
data/kati_kimchi_combined.csv   ← kimchi rows only, all snapshots stacked
"""

import os
import time
import requests
import pandas as pd
from pathlib import Path
from utils import log, save_csv, DATA_DIR
import os


# get key
KATI_KEY = os.getenv("KATI_KEY")
if not KATI_KEY:
    raise ValueError("KATI_KEY environment variable not set")

BASE_URL   = "https://api.odcloud.kr/api"
PER_PAGE   = 1000   # fetch up to 1000 rows per page (well above total product count)
SLEEP_SEC  = 1.0

RAW_DIR = DATA_DIR / "kati_raw"
RAW_DIR.mkdir(exist_ok=True)

# The three static annual-snapshot endpoints.
# Each compares two consecutive years; the label is the LATER year.
ENDPOINTS = [
    {
        "label":   "2022",
        "path":    "/15120376/v1/uddi:8028c1dd-02f5-4bd8-ae0c-99d68d9a5c33",
        "year_a":  "21",   # earlier year suffix in field names
        "year_b":  "22",   # later year suffix
    },
    {
        "label":   "2023",
        "path":    "/15120376/v1/uddi:2863b0ea-61a5-42d4-a0ff-e94a7b2ad762",
        "year_a":  "22",
        "year_b":  "23",
    },
    {
        "label":   "2024",
        "path":    "/15120376/v1/uddi:833d3aa8-6724-444a-8c3b-b4334f2a66d3",
        "year_a":  "23",
        "year_b":  "24",
    },
]

KIMCHI_KEYWORDS = ["김치"]   # extend if needed, e.g. ["김치", "깍두기"]


# ── fetch ─────────────────────────────────────────────────────────────────────

def fetch_endpoint(ep: dict) -> pd.DataFrame:
    """Fetch all pages for one annual endpoint. Returns combined DataFrame."""
    url = BASE_URL + ep["path"]
    params = {
        "serviceKey": KATI_KEY,
        "page":       1,
        "perPage":    PER_PAGE,
        "returnType": "JSON",
    }

    all_rows = []
    page = 1

    while True:
        params["page"] = page
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            body = r.json()
        except Exception as e:
            log.error(f"  [{ep['label']}] page {page} failed: {e}")
            break

        rows = body.get("data", [])
        if not rows:
            break

        all_rows.extend(rows)
        log.info(f"  [{ep['label']}] page {page}: {len(rows)} rows "
                 f"(total so far: {len(all_rows)} / {body.get('totalCount', '?')})")

        total = body.get("totalCount", 0)
        if len(all_rows) >= total:
            break

        page += 1
        time.sleep(0.2)

    if not all_rows:
        log.warning(f"  [{ep['label']}] no data returned")
        return pd.DataFrame()

    return pd.DataFrame(all_rows)


# ── normalise ─────────────────────────────────────────────────────────────────

def normalise(df: pd.DataFrame, ep: dict) -> pd.DataFrame:
    """
    Rename Korean field names to consistent English, add year columns.

    The API has used at least two different column naming conventions:

    Schema A (2022/2023 snapshots):
      구분, 품명,
      21년 금액(달러), 21년 중량(키로그램),
      22년 금액(달러), 22년 중량(키로그램),
      전년대비 금액(퍼센트), 전년대비 중량(퍼센트)

    Schema B (2024 snapshot — different names, no 품명):
      category (already in English),
      23년수출금액, 23년수출중량, 23년증감률,
      24년수출금액, 24년수출중량, 24년증감률,
      금액증감률, 물량증감률
      (no product name column present)
    """
    ya, yb = ep["year_a"], ep["year_b"]

    # Build rename map, then apply. Schema B maps several fields to the same
    # target name, which would produce duplicate columns and break .str access.
    # Instead, map Schema B YoY fields to unique temp names and coalesce after.
    rename = {
        # Schema A
        "구분":                       "category",
        "품명":                       "product_name_kr",
        f"{ya}년 금액(달러)":          "value_usd_prev",
        f"{ya}년 중량(키로그램)":       "weight_kg_prev",
        f"{yb}년 금액(달러)":          "value_usd",
        f"{yb}년 중량(키로그램)":       "weight_kg",
        "전년대비 금액(퍼센트)":         "yoy_value_pct",
        "전년대비 중량(퍼센트)":         "yoy_weight_pct",
        # Schema B — unique temp names to avoid duplicate columns after rename
        f"{ya}년수출금액":              "value_usd_prev",
        f"{ya}년수출중량":              "weight_kg_prev",
        f"{ya}년증감률":               "_b_yoy_prev_tmp",
        f"{yb}년수출금액":              "value_usd",
        f"{yb}년수출중량":              "weight_kg",
        f"{yb}년증감률":               "_b_yoy_weight_tmp",
        "금액증감률":                   "_b_yoy_value_tmp",
        "물량증감률":                   "_b_yoy_weight2_tmp",
    }

    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    # Coalesce Schema B temp columns into canonical names (only if not already set)
    for canon, tmps in [
        ("yoy_value_pct",  ["_b_yoy_value_tmp",   "_b_yoy_prev_tmp"]),
        ("yoy_weight_pct", ["_b_yoy_weight2_tmp",  "_b_yoy_weight_tmp"]),
    ]:
        if canon not in df.columns:
            for tmp in tmps:
                if tmp in df.columns:
                    df[canon] = df[tmp]
                    break
    df = df.drop(columns=[c for c in df.columns if c.endswith("_tmp")], errors="ignore")

    # Schema B has no product name column — log a warning so the user knows
    if "product_name_kr" not in df.columns:
        log.warning(
            f"  [{ep['label']}] no product name column found in this snapshot. "
            f"Columns present: {list(df.columns)}. "
            f"Kimchi filtering will be skipped for this endpoint; "
            f"all rows will be included. Check data/kati_raw/kati_raw_{ep['label']}.csv "
            f"to identify the correct product name field and add it to the rename map."
        )
        df["product_name_kr"] = None

    # full 4-digit years for clarity
    df["year"]      = int("20" + yb)
    df["year_prev"] = int("20" + ya)

    # numeric cleanup
    for col in ["value_usd", "weight_kg", "value_usd_prev", "weight_kg_prev",
                "yoy_value_pct", "yoy_weight_pct"]:
        if col in df.columns:
            df[col] = (df[col].astype(str)
                              .str.replace(",", "", regex=False)
                              .pipe(pd.to_numeric, errors="coerce"))

    return df


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    log.info("Fetching aT/KATI annual export data from api.odcloud.kr …")
    log.info(f"API key: {KATI_KEY[:8]}…{KATI_KEY[-4:]}")

    raw_frames     = []
    kimchi_frames  = []

    for ep in ENDPOINTS:
        log.info(f"\nEndpoint: {ep['label']} snapshot")
        df_raw = fetch_endpoint(ep)

        if df_raw.empty:
            continue

        # save full raw file (all products)
        raw_path = RAW_DIR / f"kati_raw_{ep['label']}.csv"
        df_raw.to_csv(raw_path, index=False, encoding="utf-8-sig")
        log.info(f"  Saved raw: {raw_path.name} ({len(df_raw)} rows)")

        # normalise
        df = normalise(df_raw, ep)

        # filter to kimchi — skip if no product name column available
        if df["product_name_kr"].isna().all():
            log.warning(f"  [{ep['label']}] skipping kimchi filter (no product name column); "
                        f"including all {len(df)} rows with product_name_kr=None")
            df_kimchi = df.copy()
        else:
            mask = df["product_name_kr"].str.contains("|".join(KIMCHI_KEYWORDS), na=False)
            df_kimchi = df[mask].copy()
            log.info(f"  Kimchi rows: {len(df_kimchi)} of {len(df)} total products")

        if df_kimchi.empty:
            log.warning(f"  No kimchi rows found in {ep['label']} snapshot — "
                        f"check product names in data/kati_raw/kati_raw_{ep['label']}.csv")
        else:
            kimchi_frames.append(df_kimchi)

        time.sleep(SLEEP_SEC)

    # combine kimchi rows across all snapshots
    if not kimchi_frames:
        log.error("No kimchi data found across any endpoint.")
        log.info("Tip: open the raw CSVs in data/kati_raw/ and search for kimchi "
                 "product names to update KIMCHI_KEYWORDS.")
        return

    combined = pd.concat(kimchi_frames, ignore_index=True)
    # deduplicate: same product + year may appear in overlapping snapshots
    combined = combined.drop_duplicates(subset=["product_name_kr", "year"])
    combined = combined.sort_values(["year", "product_name_kr"])

    save_csv(combined, "kati_kimchi_combined.csv", "KATI kimchi annual export")

    # summary
    print("\n── Kimchi export summary ──────────────────────────────")
    cols = ["year", "product_name_kr", "value_usd", "weight_kg",
            "yoy_value_pct", "yoy_weight_pct"]
    show = [c for c in cols if c in combined.columns]
    print(combined[show].to_string(index=False))

    print("\n── Annual totals (all kimchi products combined) ───────")
    totals = (
        combined.groupby("year")[["value_usd", "weight_kg"]]
        .sum()
        .assign(value_usd_m=lambda d: (d["value_usd"] / 1e6).round(2),
                weight_kg_t=lambda d: (d["weight_kg"] / 1e3).round(1))
        [["value_usd_m", "weight_kg_t"]]
        .rename(columns={"value_usd_m": "value_USD_million",
                         "weight_kg_t": "weight_tonnes"})
    )
    print(totals.to_string())


if __name__ == "__main__":
    main()

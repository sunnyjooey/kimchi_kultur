"""
02_korea_customs.py
───────────────────
Pull monthly Korea export statistics by HS code × destination country
from the Korea Customs Service open API on data.go.kr.

Dataset  : 관세청_품목별 국가별 수출입실적(GW)
ID       : 15100475
Page     : https://www.data.go.kr/data/15100475/openapi.do
Endpoint : https://apis.data.go.kr/1220000/nitemtrade/getNitemtradeList
Format   : XML

How to apply
────────────
1. Go to https://www.data.go.kr/data/15100475/openapi.do
2. Click 활용신청 — auto-approved instantly (개발계정)
3. Go to 마이페이지 → 인증키 발급현황
4. Copy the 일반 인증키
5. Set env var:  export CUSTOMS_KEY="your_key_here"
   or paste it into CUSTOMS_KEY below.

Note: use the plain (디코딩) key — this gateway accepts it unencoded.
If you get 400 errors, try the URL-encoded (인코딩) version instead.

Request parameters
──────────────────
  serviceKey   your API key
  strtYymm     start year-month  YYYYMM  (required)
  endYymm      end year-month    YYYYMM  (required, max 1-year range per call)
  hsSgn        HS code           e.g. 200599
  cntyCd       country code      optional; omit for all countries
  numOfRows    page size         default 10, we use 1000
  pageNo       page number       1-based

Response fields
───────────────
  year              period (YYYYMM)
  statCdCntnKor1    country name (Korean)
  statCd            country code
  statKor           product name (Korean)
  hsCd              HS code
  expWgt            export weight (kg)
  expDlr            export value (USD)
  impWgt            import weight (kg)
  impDlr            import value (USD)
  balPayments       trade balance (USD)

Output
──────
data/korea_customs_monthly.csv
  Columns: year_month, country_code, country_name_kr, product_name_kr,
           hs_code, export_weight_kg, export_value_usd,
           import_weight_kg, import_value_usd, trade_balance_usd
"""

import os
import time
import xml.etree.ElementTree as ET
import requests
import pandas as pd
from pathlib import Path
from datetime import date
from dateutil.relativedelta import relativedelta
from utils import log, save_csv, DATA_DIR
import os


# get key
COMTRADE_KEY = os.getenv("CUSTOMS_KEY")
if not COMTRADE_KEY:
    raise ValueError("CUSTOMS_KEY environment variable not set")

BASE_URL    = "https://apis.data.go.kr/1220000/nitemtrade/getNitemtradeList"
HS_CODE     = "200599"       # kimchi — "other prepared/preserved vegetables" (6-digit prefix)
                             # Full 10-digit HSK: 2005991000
                             # NOTE: 200520 is potatoes — do not use that
START_YM    = "201001"       # YYYYMM
# END_YM auto-set to last complete month

NUM_OF_ROWS = 1000           # max per page
SLEEP_SEC   = 1.0            # between yearly chunks
MAX_CHUNK_MONTHS = 12        # API allows max 1-year range per call


# ── date helpers ──────────────────────────────────────────────────────────────

def last_complete_month() -> str:
    today = date.today()
    anchor = today - relativedelta(months=2 if today.day < 16 else 1)
    return anchor.strftime("%Y%m")


def yearly_chunks(start_ym: str, end_ym: str) -> list[tuple[str, str]]:
    """
    Split [start_ym, end_ym] into 12-month chunks.
    Returns list of (chunk_start, chunk_end) YYYYMM tuples.
    """
    start = date(int(start_ym[:4]), int(start_ym[4:]), 1)
    end   = date(int(end_ym[:4]),   int(end_ym[4:]),   1)

    chunks = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + relativedelta(months=MAX_CHUNK_MONTHS - 1), end)
        chunks.append((cur.strftime("%Y%m"), chunk_end.strftime("%Y%m")))
        cur = cur + relativedelta(months=MAX_CHUNK_MONTHS)
    return chunks


# ── fetch ─────────────────────────────────────────────────────────────────────

def fetch_chunk(strt_ym: str, end_ym: str) -> list[dict]:
    """
    Fetch all pages for one time chunk. Returns list of row dicts.
    """
    params = {
        "serviceKey": CUSTOMS_KEY,
        "strtYymm":   strt_ym,
        "endYymm":    end_ym,
        "hsSgn":      HS_CODE,
        "numOfRows":  NUM_OF_ROWS,
        "pageNo":     1,
    }

    all_rows = []
    page = 1

    while True:
        params["pageNo"] = page
        try:
            r = requests.get(BASE_URL, params=params, timeout=30)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as e:
            log.warning(f"  [{strt_ym}–{end_ym}] page {page}: {e}")
            break

        result_code = root.findtext("header/resultCode") or ""
        if result_code not in ("00", ""):
            log.warning(f"  API error {result_code}: "
                        f"{root.findtext('header/resultMsg', '')}")
            break

        item_nodes = root.findall("body/items/item")
        if not item_nodes:
            break

        item_list = [{child.tag: child.text for child in node}
                     for node in item_nodes]
        all_rows.extend(item_list)

        total_count = int(root.findtext("body/totalCount") or 0)
        log.info(f"  [{strt_ym}–{end_ym}] page {page}: "
                 f"{len(item_list)} rows (total: {len(all_rows)}/{total_count})")

        if len(all_rows) >= total_count or len(item_list) < NUM_OF_ROWS:
            break

        page += 1
        time.sleep(0.2)

    return all_rows


# ── clean ─────────────────────────────────────────────────────────────────────

def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Rename API field names to English and coerce numeric columns."""
    rename = {
        "year":             "year_month",
        "statCd":           "country_code",
        "statCdCntnKor1":   "country_name_kr",
        "statKor":          "product_name_kr",
        "hsCd":             "hs_code",
        "expWgt":           "export_weight_kg",
        "expDlr":           "export_value_usd",
        "impWgt":           "import_weight_kg",
        "impDlr":           "import_value_usd",
        "balPayments":      "trade_balance_usd",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    for col in ["export_weight_kg", "export_value_usd",
                "import_weight_kg", "import_value_usd", "trade_balance_usd"]:
        if col in df.columns:
            df[col] = (df[col].astype(str)
                              .str.replace(",", "", regex=False)
                              .pipe(pd.to_numeric, errors="coerce"))

    df = df.sort_values(["year_month", "export_value_usd"],
                        ascending=[True, False])
    return df.reset_index(drop=True)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    if CUSTOMS_KEY == "YOUR_KEY_HERE":
        print("""
┌──────────────────────────────────────────────────────────────┐
│  API KEY REQUIRED                                            │
│                                                              │
│  Apply at: https://www.data.go.kr/data/15100475/openapi.do  │
│  Then:  export CUSTOMS_KEY="your_key_here"                   │
│  or paste it into CUSTOMS_KEY at the top of this file.      │
└──────────────────────────────────────────────────────────────┘
""")
        return

    end_ym = last_complete_month()
    chunks = yearly_chunks(START_YM, end_ym)

    log.info(f"Fetching HS {HS_CODE} monthly exports by country: "
             f"{START_YM} → {end_ym} ({len(chunks)} yearly chunks)")

    all_rows = []
    for i, (strt, end) in enumerate(chunks, 1):
        log.info(f"\nChunk {i}/{len(chunks)}: {strt} → {end}")
        rows = fetch_chunk(strt, end)
        if rows:
            all_rows.extend(rows)
            log.info(f"  Chunk complete: {len(rows)} rows")
        else:
            log.warning(f"  No data returned for {strt}–{end}")
        if i < len(chunks):
            time.sleep(SLEEP_SEC)

    if not all_rows:
        log.error("No data collected. Check your API key and try again.")
        return

    df = clean(pd.DataFrame(all_rows))
    save_csv(df, "korea_customs_monthly.csv",
             f"Korea Customs monthly HS {HS_CODE} exports by country")

    # ── summary ────────────────────────────────────────────────────────────────
    df["year"] = df["year_month"].str[:4]

    by_year = (
        df.groupby("year")["export_value_usd"]
        .sum().div(1e6).round(2)
        .rename("export_usd_m")
    )
    print(f"\nAnnual export totals for HS {HS_CODE} (USD million):")
    print(by_year.to_string())

    latest_year = df["year"].max()
    top5 = (
        df[df["year"] == latest_year]
        .groupby("country_name_kr")["export_value_usd"]
        .sum().nlargest(5).div(1e6).round(2)
        .rename("export_usd_m")
    )
    print(f"\nTop 5 destinations in {latest_year} (USD million):")
    print(top5.to_string())


if __name__ == "__main__":
    main()

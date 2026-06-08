"""
01_comtrade.py
──────────────
Pull Korea kimchi export data from UN Comtrade API v1.

Source   : UN Comtrade (comtradeplus.un.org)
HS code  : 200599 — "other prepared/preserved vegetables, not frozen"
           Full 10-digit HSK: 2005991000
           NOTE: 200520 = potatoes. 200599 is the correct kimchi subheading.
Reporter : Korea (code 410)
Flow     : Exports (X)
Frequency: Annual
Years    : 2000–2024 (adjustable via START_YEAR / END_YEAR)

Setup
─────
1. Register free at https://comtradeplus.un.org
2. Subscribe to "comtrade - v1" (free tier)
3. Copy your primary API key
4. Set env var:  export COMTRADE_KEY="your-key-here"
   or paste it directly into COMTRADE_KEY below.

Free tier limits
────────────────
- 500 API calls/day
- Up to 100,000 records per call
- This script makes one call per year chunk → ~3 calls total, well within limits.

Output
──────
data/comtrade_hs200599_exports.csv
  Columns: year, partner_code, partner, qty_kg, value_usd,
           flow, cmd_code, cmd_desc
"""

import os
import time
import pandas as pd
import comtradeapicall
from utils import log, save_csv, DATA_DIR
from google.colab import userdata
import os

os.environ['COMTRADE_KEY'] = userdata.get('COMTRADE_KEY')

HS_CODE      = "200599"      # kimchi — "other prepared/preserved vegetables, not frozen"
                             # Full 10-digit HSK: 2005991000
                             # NOTE: 200520 = potatoes — do not use
REPORTER     = "410"         # Korea
FLOW         = "X"           # exports
FREQ         = "A"           # annual
START_YEAR   = 2000
END_YEAR     = 2024

# Comtrade annual API: max 12-year window per call
CHUNK_YEARS  = 12


def year_chunks(start: int, end: int, size: int):
    """Yield comma-separated year strings in chunks of `size`."""
    years = list(range(start, end + 1))
    for i in range(0, len(years), size):
        yield ",".join(str(y) for y in years[i:i + size])


def fetch_comtrade() -> pd.DataFrame:
    if COMTRADE_KEY == "YOUR_KEY_HERE":
        raise ValueError(
            "Set your Comtrade API key:\n"
            "  export COMTRADE_KEY='your-key-here'\n"
            "or edit COMTRADE_KEY in this file."
        )

    frames = []
    chunks = list(year_chunks(START_YEAR, END_YEAR, CHUNK_YEARS))
    log.info(f"Fetching {len(chunks)} chunk(s) from Comtrade ...")

    for i, period_str in enumerate(chunks, 1):
        log.info(f"  Chunk {i}/{len(chunks)}: years {period_str[:9]}…")
        df = comtradeapicall.getFinalData(
            subscription_key = COMTRADE_KEY,
            typeCode         = "C",
            freqCode         = FREQ,
            clCode           = "HS",
            period           = period_str,
            reporterCode     = REPORTER,
            cmdCode          = HS_CODE,
            flowCode         = FLOW,
            partnerCode      = None,    # all partner countries
            partner2Code     = None,
            customsCode      = None,
            motCode          = None,
            maxRecords       = 100_000,
            format_output    = "JSON",
            aggregateBy      = None,
            breakdownMode    = "classic",
            countOnly        = None,
            includeDesc      = True,
        )

        if df is None or df.empty:
            log.warning(f"  No data returned for chunk {i}")
            continue

        frames.append(df)

        # be kind to the API between chunks
        if i < len(chunks):
            time.sleep(1.5)

    if not frames:
        raise RuntimeError("No data returned from Comtrade — check your key and try again.")

    raw = pd.concat(frames, ignore_index=True)
    log.info(f"Raw Comtrade rows: {len(raw):,}, columns: {list(raw.columns)}")
    return raw


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Select and rename the columns we actually need."""

    # Comtrade v1 returns camelCase column names; map to snake_case
    col_map = {
        "period":          "year",
        "reporterCode":    "reporter_code",
        "reporterDesc":    "reporter",
        "partnerCode":     "partner_code",
        "partnerDesc":     "partner",
        "cmdCode":         "hs_code",
        "cmdDesc":         "hs_desc",
        "flowCode":        "flow",
        "primaryValue":    "value_usd",    # FOB USD
        "netWgt":          "qty_kg",       # net weight kg
        "qty":             "qty_units",    # supplementary quantity (if reported)
        "qtyUnitCode":     "qty_unit_code",
    }

    keep = [c for c in col_map if c in df.columns]
    out  = df[keep].rename(columns={c: col_map[c] for c in keep})

    # numeric cleanup
    for col in ["value_usd", "qty_kg", "qty_units"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    out["year"] = pd.to_numeric(out["year"], errors="coerce").astype("Int64")

    # drop world aggregate row (partner_code == 0) to keep only bilateral rows
    if "partner_code" in out.columns:
        out = out[out["partner_code"].astype(str) != "0"]

    out = out.sort_values(["year", "value_usd"], ascending=[True, False])
    return out.reset_index(drop=True)


def main():
    raw    = fetch_comtrade()
    clean_ = clean(raw)
    save_csv(clean_, "comtrade_hs200599_exports.csv", "Comtrade HS200599 Korea exports")

    # quick sanity check
    by_year = (
        clean_.groupby("year")["value_usd"]
        .sum()
        .div(1e6)
        .round(2)
        .rename("total_export_usd_m")
    )
    print("\nAnnual totals (USD million):")
    print(by_year.to_string())

    top5_2023 = (
        clean_[clean_["year"] == 2023]
        .nlargest(5, "value_usd")[["partner", "value_usd", "qty_kg"]]
        .assign(value_usd=lambda d: d["value_usd"].div(1e6).round(2))
        .assign(qty_kg=lambda d: d["qty_kg"].div(1e6).round(3))
        .rename(columns={"value_usd": "usd_m", "qty_kg": "tonnes_000"})
    )
    print("\nTop 5 destinations (2023):")
    print(top5_2023.to_string(index=False))


if __name__ == "__main__":
    main()

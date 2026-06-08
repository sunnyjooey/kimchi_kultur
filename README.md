# Stream 1 — Trade & Market Data

Three sources, ordered by recommended priority:

## Sources

### 1. UN Comtrade  (`01_comtrade.py`)
- **What**: Korea (reporter 410) exports of HS 200520 (preserved vegetables incl. kimchi), by destination country, annual 2000–present
- **Key**: Register free at https://comtradeplus.un.org → sign up → subscribe to "comtrade - v1 (free)" → copy primary key
- **Caveat**: HS 200520 = "other prepared/preserved vegetables, not frozen" — includes kimchi and other Korean pickled items, not kimchi-only. Most Korea HS 200520 exports ARE kimchi (per KATI), but note the caveat in your writeup.
- **Output**: `data/comtrade_hs200520_exports.csv`

### 2. Korea Customs Service open API  (`02_korea_customs.py`)
- **What**: Korea-side HS code export data by country, monthly — more granular than Comtrade, Korea-only reporter, faster update cycle
- **Key**: Register at https://www.data.go.kr (공공데이터포털) → search "관세청 국가별 수출입실적" → 활용신청 → copy service key (URL-encoded)
- **Output**: `data/korea_customs_exports.csv`

### 3. KATI file data  (`03_kati_download.py`)
- **What**: aT/KATI annual kimchi-specific export stats (품목별 연도별 수출실적) — this is kimchi-explicit, not just HS code
- **Access**: Direct file download from data.go.kr — no API key needed, just download and parse
- **URL**: https://www.data.go.kr/data/15120376/fileData.do
- **Output**: `data/kati_annual_exports.csv`

### 4. Google Trends  (`04_google_trends.py`)
- **What**: "kimchi" search interest index (0–100) by country/region, 2004–present
- **Key**: None needed — unofficial pytrends wrapper
- **Output**: `data/google_trends_kimchi.csv`

## Running order
Run 03 first (no key needed, validates your setup).
Then 04 (no key needed).
Then 01 and 02 after you have keys.

## Directory structure
stream1/
  01_comtrade.py
  02_korea_customs.py
  03_kati_download.py
  04_google_trends.py
  utils.py
  data/           ← created on first run
  README.md

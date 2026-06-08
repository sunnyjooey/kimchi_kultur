"""
Shared utilities for kimchi project stream 1 data pulls.
"""

import os
import time
import logging
from pathlib import Path

import pandas as pd
import requests

# ── paths ──────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# ── logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── HTTP helper ────────────────────────────────────────────────────────────
def get_json(url: str, params: dict = None, retries: int = 3,
             backoff: float = 2.0) -> dict:
    """GET request with retry/backoff. Returns parsed JSON."""
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            log.warning(f"Attempt {attempt}/{retries} failed: {e}")
            if attempt < retries:
                time.sleep(backoff ** attempt)
    raise RuntimeError(f"All {retries} attempts failed for {url}")


# ── save helper ────────────────────────────────────────────────────────────
def save_csv(df: pd.DataFrame, filename: str, desc: str = "") -> Path:
    """Save DataFrame to data/ with logging."""
    path = DATA_DIR / filename
    df.to_csv(path, index=False, encoding="utf-8-sig")   # utf-8-sig for Excel compat with Korean
    log.info(f"Saved {desc or filename}: {len(df):,} rows → {path}")
    return path

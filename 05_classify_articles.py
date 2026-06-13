"""
05_classify_articles.py
───────────────────────
LLM-based relevance and framing classifier for kimchi media articles.

Classifies each article on two dimensions:
  1. kimchi_centrality : primary | secondary | incidental
  2. korea_subject      : Y | N

Works on both Guardian (has full body_text) and NYT (abstracts only).
Uses different context fields per source to give the model the best signal.

Input files
───────────
data/guardian_articles.csv   — cols: date, year, headline, section, tags,
                               url, word_count, body_text
data/nyt_articles.csv        — cols: date, year, headline, abstract,
                               snippet, section, url

Output files
────────────
data/guardian_classified.csv
data/nyt_classified.csv

Each output adds columns:
  kimchi_centrality, korea_subject, classifier_confidence,
  classifier_reasoning, classifier_model, classified_at

Setup
─────
Set env var:  export ANTHROPIC_KEY="your-key"
Or paste it below into ANTHROPIC_KEY.

Cost note
─────────
Uses claude-haiku-3-5 by default — fast and cheap (~$0.001 per article).
For higher accuracy on ambiguous cases, switch MODEL to claude-sonnet-4-6.

Batching
────────
Processes in batches with a short sleep between calls.
For large datasets (1000+ articles), consider Anthropic's Batch API instead
— it's 50% cheaper and has no rate limit concerns. See:
https://docs.claude.com/en/docs/build-with-claude/batch-processing
"""

import os
import sys
import json
import time
import logging
import argparse
from datetime import datetime
from pathlib import Path

import anthropic
import pandas as pd

from utils import log, save_csv, DATA_DIR

# ── config ──────────────────────────────────────────────────────────────────
ANTHROPIC_KEY = os.getenv("ANTHROPIC_KEY")
MODEL         = "claude-haiku-4-5"    # swap to claude-sonnet-4-6 for harder cases
SLEEP_SEC     = 0.3                   # polite delay between API calls
MAX_RETRIES   = 3

VALID_CENTRALITY  = {"primary", "secondary", "incidental"}
VALID_KOREA       = {"Y", "N"}

# ── prompts ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a media analyst classifying news and editorial articles
that mention kimchi. Your job is to assess two things about each article.

CLASSIFICATIONS:

1. kimchi_centrality
   primary    — kimchi is the main subject; the article is explicitly about kimchi
                (a recipe, a deep-dive on fermentation, a kimchi market report)
   secondary  — kimchi plays a meaningful supporting role; it illustrates a point,
                anchors a section, or is one of a small number of focal foods
   incidental — kimchi appears briefly or in passing; it could be removed without
                changing the article's argument or focus

2. korea_subject
   Y — the article is substantially about Korea (country, culture, society,
       people, politics, economy) — kimchi appears as part of that Korean context
   N — the article is not primarily about Korea; kimchi appears in a non-Korea
       or globalised context (e.g. a Western restaurant review, a fermentation
       health piece, a recipe from a non-Korean chef, a cultural reference)

Return ONLY a JSON object with exactly these keys:
{
  "kimchi_centrality": "primary" | "secondary" | "incidental",
  "korea_subject": "Y" | "N",
  "confidence": "high" | "medium" | "low",
  "reasoning": "one sentence explaining your classification"
}

No preamble, no markdown fences, no extra keys. JSON only."""


def make_guardian_prompt(row: pd.Series) -> str:
    """Guardian has full body text — use headline + tags + first ~400 words."""
    body_preview = str(row.get("body_text", ""))[:1500].strip()
    tags = str(row.get("tags", "")).replace("|", ", ")
    return f"""SOURCE: The Guardian
DATE: {row.get('date', 'unknown')}
SECTION: {row.get('section', 'unknown')}
TAGS: {tags}
HEADLINE: {row.get('headline', '')}
WORD COUNT: {row.get('word_count', 'unknown')}

ARTICLE EXCERPT (first ~400 words):
{body_preview}

Classify this article."""


def make_nyt_prompt(row: pd.Series) -> str:
    """NYT has abstract only — use headline + abstract + section."""
    abstract = str(row.get("abstract", "")).strip()
    snippet  = str(row.get("snippet", "")).strip()
    # use snippet if it adds information beyond the abstract
    extra = f"\nSNIPPET: {snippet}" if snippet and snippet != abstract else ""
    return f"""SOURCE: New York Times
DATE: {row.get('date', 'unknown')}
SECTION: {row.get('section', 'unknown')}
HEADLINE: {row.get('headline', '')}
ABSTRACT: {abstract}{extra}

Note: only the abstract/headline is available for this source — no full text.
Classify this article."""


# ── classifier ───────────────────────────────────────────────────────────────

def classify_one(client: anthropic.Anthropic, prompt: str) -> dict:
    """Call the API and return parsed classification dict."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            msg = client.messages.create(
                model      = MODEL,
                max_tokens = 256,
                system     = SYSTEM_PROMPT,
                messages   = [{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()
            # strip accidental markdown fences
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            parsed = json.loads(raw)

            # validate values
            if parsed.get("kimchi_centrality") not in VALID_CENTRALITY:
                raise ValueError(f"Bad centrality: {parsed.get('kimchi_centrality')}")
            if parsed.get("korea_subject") not in VALID_KOREA:
                raise ValueError(f"Bad korea_subject: {parsed.get('korea_subject')}")

            return parsed

        except (json.JSONDecodeError, ValueError) as e:
            log.warning(f"  Parse error attempt {attempt}/{MAX_RETRIES}: {e}")
            if attempt == MAX_RETRIES:
                return {
                    "kimchi_centrality": "incidental",
                    "korea_subject": "N",
                    "confidence": "low",
                    "reasoning": f"Classification failed after {MAX_RETRIES} attempts: {e}",
                }
            time.sleep(1.5 ** attempt)

        except anthropic.RateLimitError:
            wait = 10 * attempt
            log.warning(f"  Rate limit — waiting {wait}s")
            time.sleep(wait)

        except anthropic.APIError as e:
            log.warning(f"  API error attempt {attempt}: {e}")
            if attempt == MAX_RETRIES:
                raise
            time.sleep(2 ** attempt)


def classify_df(
    df: pd.DataFrame,
    prompt_fn,
    client: anthropic.Anthropic,
    resume_from: int = 0,
) -> pd.DataFrame:
    """
    Classify all rows in df using prompt_fn to build each prompt.
    resume_from: row index to restart from (for interrupted runs).
    """
    results = []
    total = len(df)

    for i, (_, row) in enumerate(df.iterrows()):
        if i < resume_from:
            results.append(None)
            continue

        prompt = prompt_fn(row)

        if i % 10 == 0:
            log.info(f"  [{i+1}/{total}] classifying: {str(row.get('headline',''))[:60]}…")

        result = classify_one(client, prompt)
        result["classifier_model"]  = MODEL
        result["classified_at"]     = datetime.utcnow().isoformat()
        results.append(result)

        time.sleep(SLEEP_SEC)

    # unpack results into columns
    out = df.copy()
    valid_results = [r for r in results if r is not None]

    for col in ["kimchi_centrality", "korea_subject",
                "confidence", "reasoning", "classifier_model", "classified_at"]:
        out[col] = [r.get(col) if r else None for r in results]

    return out


# ── checkpoint helpers ────────────────────────────────────────────────────────

def save_checkpoint(df: pd.DataFrame, path: Path):
    df.to_csv(path, index=False, encoding="utf-8-sig")
    log.info(f"Checkpoint saved → {path}")


def load_checkpoint(path: Path) -> tuple[pd.DataFrame | None, int]:
    """Load existing checkpoint. Returns (df, next_row_to_process)."""
    if not path.exists():
        return None, 0
    df = pd.read_csv(path)
    classified = df["kimchi_centrality"].notna().sum()
    log.info(f"Resuming from checkpoint: {classified}/{len(df)} already done")
    return df, classified


# ── main ──────────────────────────────────────────────────────────────────────

def run_source(
    client:     anthropic.Anthropic,
    input_path: Path,
    output_path: Path,
    prompt_fn,
    label: str,
    checkpoint_every: int = 25,
):
    log.info(f"\n{'='*60}")
    log.info(f"Classifying {label}")
    log.info(f"Input:  {input_path}")
    log.info(f"Output: {output_path}")

    if not input_path.exists():
        log.warning(f"Input file not found: {input_path} — skipping")
        return

    df = pd.read_csv(input_path)
    log.info(f"Loaded {len(df)} rows")

    # check for existing checkpoint
    existing, resume_from = load_checkpoint(output_path)
    if existing is not None and resume_from >= len(df):
        log.info("All rows already classified — nothing to do")
        return
    if existing is not None:
        df = existing   # use checkpoint df (already has partial results)

    # add output columns if missing
    for col in ["kimchi_centrality", "korea_subject", 
                "confidence", "reasoning", "classifier_model", "classified_at"]:
        if col not in df.columns:
            df[col] = None

    total = len(df)
    for i, (idx, row) in enumerate(df.iterrows()):
        if i < resume_from:
            continue

        if i % 10 == 0:
            log.info(f"  [{i+1}/{total}] {str(row.get('headline',''))[:70]}…")

        prompt = prompt_fn(row)
        result = classify_one(client, prompt)

        df.at[idx, "kimchi_centrality"]  = result.get("kimchi_centrality")
        df.at[idx, "korea_subject"]      = result.get("korea_subject")
        df.at[idx, "confidence"]         = result.get("confidence")
        df.at[idx, "reasoning"]          = result.get("reasoning")
        df.at[idx, "classifier_model"]   = MODEL
        df.at[idx, "classified_at"]      = datetime.utcnow().isoformat()

        # periodic checkpoint save
        if (i + 1) % checkpoint_every == 0:
            save_checkpoint(df, output_path)

        time.sleep(SLEEP_SEC)

    # final save
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    log.info(f"Done — {total} rows saved → {output_path}")

    # summary
    print(f"\n── {label} classification summary ──")
    print(df["kimchi_centrality"].value_counts().to_string())
    print("\nKorea subject:")
    print(df["korea_subject"].value_counts().to_string())
    print("\nArticle type:")
    print("\nConfidence:")
    print(df["confidence"].value_counts().to_string())


def main():
    parser = argparse.ArgumentParser(description="Classify kimchi articles")
    parser.add_argument(
        "--source", choices=["guardian", "nyt", "both"], default="both",
        help="Which source to classify (default: both)"
    )
    parser.add_argument(
        "--guardian-in",  default="guardian_articles.csv",
        help="Guardian input filename (inside data/)"
    )
    parser.add_argument(
        "--nyt-in",       default="nyt_articles.csv",
        help="NYT input filename (inside data/)"
    )
    parser.add_argument(
        "--guardian-out", default="guardian_classified.csv",
        help="Guardian output filename (inside data/)"
    )
    parser.add_argument(
        "--nyt-out",      default="nyt_classified.csv",
        help="NYT output filename (inside data/)"
    )
    parser.add_argument(
        "--checkpoint-every", type=int, default=25,
        help="Save checkpoint every N articles (default: 25)"
    )
    args = parser.parse_args()

    key = ANTHROPIC_KEY or os.getenv("ANTHROPIC_KEY")
    if not key:
        raise ValueError(
            "Set your Anthropic API key:\n"
            "  export ANTHROPIC_KEY='your-key'\n"
            "or edit ANTHROPIC_KEY at the top of this file."
        )

    client = anthropic.Anthropic(api_key=key)
    log.info(f"Using model: {MODEL}")

    if args.source in ("guardian", "both"):
        run_source(
            client      = client,
            input_path  = DATA_DIR / args.guardian_in,
            output_path = DATA_DIR / args.guardian_out,
            prompt_fn   = make_guardian_prompt,
            label       = "Guardian",
            checkpoint_every = args.checkpoint_every,
        )

    if args.source in ("nyt", "both"):
        run_source(
            client      = client,
            input_path  = DATA_DIR / args.nyt_in,
            output_path = DATA_DIR / args.nyt_out,
            prompt_fn   = make_nyt_prompt,
            label       = "NYT",
            checkpoint_every = args.checkpoint_every,
        )

    log.info("\nAll done.")


if __name__ == "__main__":
    main()

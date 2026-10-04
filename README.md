# Reddit Data Scraper (Arctic-Style Search)

This project is a configurable Python tool that uses the Arctic Shift API to let you pick subreddit(s) and keyword queries, then export post and comment metadata.

It includes both:

- a CLI collector
- a local web UI (Streamlit)

## Features

- Choose any subreddit(s) at runtime
- Search by any keyword query
- Export post metadata to JSONL and CSV
- Optionally export comments for matched posts
- Supports sort and time filters

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

```powershell
pip install -r requirements.txt
```

3. Optional: copy `.env.example` to `.env` if you want to override the Arctic Shift base URL.

## Quick Start

Example: collect posts for a keyword query from two subreddits.

```powershell
python src/reddit_scalper/main.py --subreddit dlsu proftopick --query "alex eala" --limit 100 --sort desc --after 2024-01-01
```

Example: collect posts and comments from a specific subreddit.

```powershell
python src/reddit_scalper/main.py --subreddit worldnews --query reddit --limit 150 --include-comments --comment-limit 30
```

## Web UI (Recommended)

Launch the local interface:

```powershell
streamlit run app.py
```

In the UI, you can:

- set subreddits as a comma-separated list (example: `dlsu,philippines`)
- enter any keyword query
- choose sort order and optional after/before dates
- include comments with per-post limit
- download results as CSV or JSONL

## Output

Generated files are written to `data/` by default:

- `{prefix}_posts_{timestamp}.jsonl`
- `{prefix}_posts_{timestamp}.csv`
- `{prefix}_comments_{timestamp}.jsonl` (if comments enabled)
- `{prefix}_comments_{timestamp}.csv` (if comments enabled)

## Notes

- Arctic Shift search accepts `sort=asc|desc`, `after`, and `before` filters.
- Keep requests moderate to stay within API policies and rate limits.
- For research reproducibility, store your command parameters alongside exported files.

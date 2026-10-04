import argparse
import csv
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

try:
    from .arctic_shift import ArcticShiftClient
except ImportError:  # pragma: no cover - script execution fallback
    from arctic_shift import ArcticShiftClient

VALID_SORTS = {"asc", "desc"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Arctic Shift-backed Reddit search collector. "
            "Pick subreddit(s) and a keyword query, then export metadata."
        )
    )
    parser.add_argument(
        "--subreddit",
        nargs="+",
        required=True,
        help="One or more subreddits. Example: dlsu proftopick philippines",
    )
    parser.add_argument(
        "--query",
        required=True,
        help='Keyword query. Example: "alex eala" OR "asian games"',
    )
    parser.add_argument("--limit", type=int, default=100, help="Max posts to collect.")
    parser.add_argument(
        "--sort",
        default="desc",
        choices=sorted(VALID_SORTS),
        help="Sort order by created_utc.",
    )
    parser.add_argument(
        "--after",
        default="",
        help="Optional UTC datetime or date, e.g. 2024-01-01 or 2024-01-01T00:00:00",
    )
    parser.add_argument(
        "--before",
        default="",
        help="Optional UTC datetime or date, e.g. 2024-12-31 or 2024-12-31T23:59:59",
    )
    parser.add_argument(
        "--include-comments",
        action="store_true",
        help="Collect comments for each matched post.",
    )
    parser.add_argument(
        "--comment-limit",
        type=int,
        default=20,
        help="Max comments per post when --include-comments is set.",
    )
    parser.add_argument(
        "--output-dir",
        default="data",
        help="Directory for output files.",
    )
    parser.add_argument(
        "--output-prefix",
        default="reddit_export",
        help="Prefix for output file names.",
    )
    parser.add_argument(
        "--base-url",
        default="https://arctic-shift.photon-reddit.com",
        help="Arctic Shift API base URL.",
    )
    return parser.parse_args()


def parse_datetime(value: str | None) -> str | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def ensure_output_dir(path: str) -> Path:
    output_dir = Path(path)
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    client = ArcticShiftClient(base_url=args.base_url)
    after = parse_datetime(args.after)
    before = parse_datetime(args.before)

    posts = client.search_posts(
        query=args.query,
        subreddits=args.subreddit,
        limit=args.limit,
        sort=args.sort,
        after=after,
        before=before,
    )
    post_dicts = [asdict(post) for post in posts]

    output_dir = ensure_output_dir(args.output_dir)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    posts_jsonl = output_dir / f"{args.output_prefix}_posts_{timestamp}.jsonl"
    posts_csv = output_dir / f"{args.output_prefix}_posts_{timestamp}.csv"

    write_jsonl(posts_jsonl, post_dicts)
    write_csv(posts_csv, post_dicts)

    print(f"Collected posts: {len(post_dicts)}")
    print(f"Posts JSONL: {posts_jsonl}")
    print(f"Posts CSV: {posts_csv}")

    if args.include_comments:
        comments = []
        for post in posts:
            comments.extend(
                client.fetch_comments_for_post(
                    post.id,
                    query=args.query,
                    limit=args.comment_limit,
                )
            )
        comment_dicts = [asdict(comment) for comment in comments]

        comments_jsonl = output_dir / f"{args.output_prefix}_comments_{timestamp}.jsonl"
        comments_csv = output_dir / f"{args.output_prefix}_comments_{timestamp}.csv"

        write_jsonl(comments_jsonl, comment_dicts)
        write_csv(comments_csv, comment_dicts)

        print(f"Collected comments: {len(comment_dicts)}")
        print(f"Comments JSONL: {comments_jsonl}")
        print(f"Comments CSV: {comments_csv}")


if __name__ == "__main__":
    main()

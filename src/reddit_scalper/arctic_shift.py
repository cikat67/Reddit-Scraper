import os
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Callable, Iterable, List, Optional

import requests

try:
    from .models import CommentRecord, PostRecord
except ImportError:  # pragma: no cover - script execution fallback
    from models import CommentRecord, PostRecord

DEFAULT_BASE_URL = os.getenv("ARCTIC_SHIFT_BASE_URL", "https://arctic-shift.photon-reddit.com")

class ArcticShiftError(RuntimeError):
    def __init__(self, message: str, partial_records: list[PostRecord] | None = None) -> None:
        super().__init__(message)
        self.partial_records = partial_records or []


class ArcticShiftClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: int = 60, request_pause: float = 4.0, page_size: int = 100) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.request_pause = request_pause
        self.page_size = max(1, page_size)

    def _request(self, path: str, params: dict[str, str | int | bool | None], retries: int = 3) -> dict:
        url = f"{self.base_url}{path}"
        clean_params = {key: value for key, value in params.items() if value not in (None, "")}

        for attempt in range(retries + 1):
            response = requests.get(url, params=clean_params, timeout=self.timeout)

            payload: dict | None = None
            try:
                payload = response.json()
            except ValueError:
                payload = None

            if response.ok:
                if isinstance(payload, dict) and payload.get("error"):
                    raise ArcticShiftError(str(payload["error"]))
                return payload or {}

            error_message = None
            if isinstance(payload, dict):
                error_message = payload.get("error")

            if response.status_code == 422 and error_message and "Timeout" in str(error_message):
                if attempt < retries:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise ArcticShiftError(str(error_message))

            if error_message:
                raise ArcticShiftError(str(error_message))

            response.raise_for_status()

        raise ArcticShiftError("Arctic Shift request failed")

    @staticmethod
    def _to_utc_iso(value: int | float | str | None) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                return value
        return datetime.fromtimestamp(int(float(value)), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _to_timestamp(value: str | None) -> Optional[str]:
        if not value:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _shift_timestamp(value: int | float | str | None, seconds: int) -> str | None:
        if value is None:
            return None
        try:
            numeric_value = int(float(value))
        except (TypeError, ValueError):
            return None
        return datetime.fromtimestamp(numeric_value + seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def search_posts(
        self,
        *,
        query: str,
        subreddits: List[str],
        limit: int,
        sort: str = "desc",
        after: str | None = None,
        before: str | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
        post_callback: Callable[[PostRecord], bool | None] | None = None,
    ) -> List[PostRecord]:
        subreddit_values = [item.strip() for item in subreddits if item.strip() and item.strip().lower() != "all"]
        if not subreddit_values:
            raise ArcticShiftError("Arctic Shift keyword search requires at least one subreddit.")

        search_targets = subreddit_values
        fetched_at = datetime.now(timezone.utc).isoformat()
        records: list[PostRecord] = []
        seen_ids: set[str] = set()
        parsed_count = 0

        try:
            for subreddit in search_targets:
                target_after = after
                target_before = before
                repeated_cursor_hits = 0
                last_cursor_key: tuple[str | None, str | None] | None = None

                if progress_callback:
                    progress_callback(
                        {
                            "subreddit": subreddit,
                            "parsed": parsed_count,
                            "found": len(records),
                            "limit": limit,
                            "items_in_page": 0,
                        }
                    )

                while len(records) < limit:
                    cursor_key = (target_after, target_before)
                    if cursor_key == last_cursor_key:
                        repeated_cursor_hits += 1
                    else:
                        repeated_cursor_hits = 0
                    last_cursor_key = cursor_key

                    # Prevent endless loops on unstable/inclusive cursors.
                    if repeated_cursor_hits >= 3:
                        break

                    request_limit = min(self.page_size, limit - len(records))
                    payload = self._request(
                        "/api/posts/search",
                        {
                            "query": query,
                            "subreddit": subreddit,
                            "after": target_after,
                            "before": target_before,
                            "limit": request_limit,
                            "sort": sort,
                            "md2html": "true",
                            "meta-app": "reddit-scraper",
                        },
                    )
                    items = payload.get("data", []) or []
                    if not items:
                        break

                    page_timestamps: list[int] = []
                    unique_before = len(records)
                    for item in items:
                        parsed_count += 1
                        if progress_callback:
                            progress_callback(
                                {
                                    "subreddit": subreddit,
                                    "parsed": parsed_count,
                                    "found": len(records),
                                    "limit": limit,
                                    "items_in_page": len(items),
                                }
                            )
                        created_utc = item.get("created_utc") or 0
                        try:
                            page_timestamps.append(int(float(created_utc)))
                        except (TypeError, ValueError):
                            continue

                        post_id = str(item.get("id", ""))
                        if not post_id or post_id in seen_ids:
                            continue
                        seen_ids.add(post_id)
                        subreddit_name = item.get("subreddit") or item.get("subreddit_name_prefixed") or ""
                        permalink = item.get("permalink") or ""
                        if permalink and not permalink.startswith("http"):
                            permalink = f"https://reddit.com{permalink}"
                        post_record = PostRecord(
                            id=post_id,
                            subreddit=subreddit_name,
                            title=item.get("title") or "",
                            selftext=item.get("selftext") or "",
                            author=item.get("author") or "[deleted]",
                            score=int(item.get("score") or 0),
                            upvote_ratio=float(item.get("upvote_ratio") or 0.0),
                            num_comments=int(item.get("num_comments") or 0),
                            created_utc=int(float(created_utc)),
                            created_iso=self._to_utc_iso(created_utc),
                            permalink=permalink,
                            url=item.get("url") or "",
                            domain=item.get("domain") or "",
                            over_18=bool(item.get("over_18", False)),
                            spoiler=bool(item.get("spoiler", False)),
                            locked=bool(item.get("locked", False)),
                            stickied=bool(item.get("stickied", False)),
                            link_flair_text=item.get("link_flair_text") or "",
                            removed_by=str(item.get("removed_by") or ""),
                            removed_by_category=str(item.get("removed_by_category") or ""),
                            query=query,
                            fetched_at=fetched_at,
                        )
                        records.append(post_record)

                        if post_callback is not None:
                            should_continue = post_callback(post_record)
                            if should_continue is False:
                                return records

                        if progress_callback:
                            progress_callback(
                                {
                                    "subreddit": subreddit,
                                    "parsed": parsed_count,
                                    "found": len(records),
                                    "limit": limit,
                                    "items_in_page": len(items),
                                }
                            )

                    if len(items) < request_limit:
                        break

                    next_after = target_after
                    next_before = target_before
                    if page_timestamps:
                        if sort == "desc":
                            next_before = self._to_utc_iso(min(page_timestamps))
                        else:
                            next_after = self._to_utc_iso(max(page_timestamps))

                    # If we got a full page but made no unique progress and cursor
                    # would not move, stop to avoid hard loops on the same page.
                    if len(records) == unique_before and (next_after, next_before) == (target_after, target_before):
                        break

                    target_after = next_after
                    target_before = next_before

                    if len(records) < limit and self.request_pause > 0:
                        time.sleep(self.request_pause)

                if len(records) >= limit:
                    break
        except ArcticShiftError as exc:
            if not getattr(exc, "partial_records", None):
                exc.partial_records = list(records)
            raise

        records.sort(key=lambda record: record.created_utc, reverse=(sort == "desc"))
        return records[:limit]

    def _flatten_comment_tree(self, items: list[dict], query: str, fetched_at: str) -> list[CommentRecord]:
        comments: list[CommentRecord] = []
        stack = list(items)
        while stack:
            item = stack.pop(0)
            kind = item.get("kind")
            data = item.get("data", {})
            if kind != "t1":
                continue
            comment_id = str(data.get("id", ""))
            if not comment_id:
                continue
            permalink = data.get("permalink") or ""
            if permalink and not permalink.startswith("http"):
                permalink = f"https://reddit.com{permalink}"
            comments.append(
                CommentRecord(
                    id=comment_id,
                    post_id=str(data.get("link_id") or "").removeprefix("t3_"),
                    parent_id=str(data.get("parent_id") or ""),
                    subreddit=str(data.get("subreddit") or ""),
                    author=str(data.get("author") or "[deleted]"),
                    body=str(data.get("body") or ""),
                    score=int(data.get("score") or 0),
                    created_utc=int(float(data.get("created_utc") or 0)),
                    created_iso=self._to_utc_iso(data.get("created_utc") or 0),
                    permalink=permalink,
                    query=query,
                    fetched_at=fetched_at,
                )
            )
            replies = data.get("replies")
            if isinstance(replies, dict):
                children = replies.get("data", {}).get("children", [])
                if children:
                    stack.extend(children)
        return comments

    def fetch_comments_for_post(self, post_id: str, *, query: str, limit: int = 20) -> list[CommentRecord]:
        fetched_at = datetime.now(timezone.utc).isoformat()
        payload = self._request(
            "/api/comments/tree",
            {
                "link_id": f"t3_{post_id}",
                "limit": limit,
                "start_breadth": 4,
                "start_depth": 4,
                "md2html": "true",
                "meta-app": "reddit-scraper",
            },
        )
        data = payload.get("data", []) or []
        comments = self._flatten_comment_tree(list(data), query=query, fetched_at=fetched_at)
        return comments[:limit]

    def search_subreddits(self, prefix: str, limit: int = 10) -> list[dict]:
        prefix = prefix.strip()
        if not prefix:
            return []

        payload = self._request(
            "/api/subreddits/search",
            {
                "subreddit_prefix": prefix,
                "limit": min(max(1, limit), 10),
                "sort": "desc",
                "sort_type": "subscribers",
            },
        )
        results: list[dict] = []
        for item in payload.get("data", []) or []:
            display_name = item.get("display_name") or item.get("subreddit") or item.get("name")
            if not display_name:
                continue
            results.append(
                {
                    "display_name": str(display_name),
                    "subscribers": item.get("subscribers"),
                    "over18": item.get("over18"),
                    "title": item.get("title") or item.get("public_description") or "",
                }
            )
        return results[:10]
from dataclasses import asdict
from datetime import datetime, timezone
from io import BytesIO
import json
import time

import pandas as pd
import streamlit as st

from src.reddit_scalper.arctic_shift import ArcticShiftClient


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


def to_jsonl_bytes(records: list[dict]) -> bytes:
    buffer = BytesIO()
    for row in records:
        buffer.write((json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))
    return buffer.getvalue()


def to_utc_iso(value: str) -> str | None:
    if not value.strip():
        return None
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def timestamp_to_utc_iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def merge_record_lists(existing: list[dict], new: list[dict], key: str = "id") -> list[dict]:
    merged = list(existing)
    seen_ids = {str(item.get(key) or "") for item in merged if str(item.get(key) or "").strip()}
    for item in new:
        item_id = str(item.get(key) or "").strip()
        if item_id and item_id in seen_ids:
            continue
        if item_id:
            seen_ids.add(item_id)
        merged.append(item)
    return merged


def resume_bounds_from_posts(posts: list) -> tuple[str | None, str | None]:
    if not posts:
        return None, None

    created_values = [int(getattr(post, "created_utc", 0) or 0) for post in posts if int(getattr(post, "created_utc", 0) or 0) > 0]
    if not created_values:
        return None, None

    newest_created = max(created_values)
    oldest_created = min(created_values)
    # Keep boundary timestamps inclusive to avoid skipping posts that share
    # the same second at page edges; dedupe handles overlaps across reruns.
    return timestamp_to_utc_iso(newest_created), timestamp_to_utc_iso(oldest_created)


def build_retry_status(mode_label: str | None, attempt_number: int, max_tries: int) -> str:
    if not mode_label:
        return "Error mode: Off"
    return f"Error mode: {mode_label} ({attempt_number}/{max_tries})"


def current_retry_status(mode_label: str | None, retry_count: int, max_tries: int) -> str:
    if not mode_label:
        return "Error mode: Off"
    attempt_number = min(retry_count + 1, max_tries)
    return build_retry_status(mode_label, attempt_number, max_tries)


def init_field_group(group_key: str, next_id_key: str) -> None:
    if group_key not in st.session_state:
        st.session_state[group_key] = [0]
    if next_id_key not in st.session_state:
        st.session_state[next_id_key] = 1


def add_field(group_key: str, next_id_key: str, default_value: str = "") -> int:
    field_id = int(st.session_state[next_id_key])
    st.session_state[next_id_key] = field_id + 1
    st.session_state[group_key].append(field_id)
    st.session_state[f"{group_key}_{field_id}"] = default_value
    return field_id


def remove_field(group_key: str, field_id: int) -> None:
    st.session_state[group_key] = [value for value in st.session_state[group_key] if value != field_id]
    st.session_state.pop(f"{group_key}_{field_id}", None)


def get_field_values(group_key: str) -> list[str]:
    values = []
    for field_id in st.session_state.get(group_key, []):
        value = str(st.session_state.get(f"{group_key}_{field_id}", "")).strip()
        if value:
            values.append(value)
    return values


def append_value_to_group(group_key: str, next_id_key: str, value: str) -> None:
    value = value.strip()
    if not value:
        return
    existing_values = get_field_values(group_key)
    if value in existing_values:
        return

    for field_id in st.session_state.get(group_key, []):
        field_key = f"{group_key}_{field_id}"
        if not str(st.session_state.get(field_key, "")).strip():
            st.session_state[field_key] = value
            return

    new_field_id = add_field(group_key, next_id_key, value)
    st.session_state[f"{group_key}_{new_field_id}"] = value


def set_group_value(group_key: str, field_id: int, value: str) -> None:
    st.session_state[f"{group_key}_{field_id}"] = value.strip()


def apply_subreddit_suggestion(group_key: str, field_id: int, select_key: str) -> None:
    selected_label = str(st.session_state.get(select_key, "")).strip()
    if not selected_label or selected_label == "Choose a match...":
        return
    selected_name = selected_label.removeprefix("r/").split(" (")[0]
    set_group_value(group_key, field_id, selected_name)


def build_keyword_query(keywords: list[str], mode: str) -> str:
    formatted_terms = []
    for term in keywords:
        cleaned = term.strip()
        if not cleaned:
            continue
        if " " in cleaned and not (cleaned.startswith('"') and cleaned.endswith('"')):
            cleaned = f'"{cleaned}"'
        formatted_terms.append(cleaned)
    if not formatted_terms:
        return ""
    if mode == "all":
        return " ".join(formatted_terms)
    return " OR ".join(formatted_terms)


def build_search_terms(keywords: list[str]) -> list[str]:
    terms = []
    for keyword in keywords:
        term = build_keyword_query([keyword], "all")
        if term:
            terms.append(term)
    return terms


def render_input_group(
    title: str,
    group_key: str,
    next_id_key: str,
    placeholder: str,
    add_button_label: str,
    base_url: str | None = None,
    autocomplete: bool = False,
) -> None:
    st.markdown(f"**{title}**")
    field_ids = list(st.session_state.get(group_key, []))
    for field_id in field_ids:
        row_columns = st.columns([0.86, 0.14])
        field_key = f"{group_key}_{field_id}"
        row_columns[0].text_input(
            label=f"{title} {field_id + 1}",
            key=field_key,
            placeholder=placeholder,
            label_visibility="collapsed",
        )
        if len(field_ids) > 1:
            if row_columns[1].button("−", key=f"remove_{group_key}_{field_id}", width="stretch"):
                remove_field(group_key, field_id)
                st.rerun()

        if autocomplete and base_url and st.session_state.get(field_key, "").strip():
            prefix = str(st.session_state.get(field_key, "")).strip()
            if len(prefix) >= 1:
                try:
                    suggestions = get_subreddit_suggestions(base_url, prefix)
                except Exception as exc:
                    st.caption(f"Subreddit lookup failed: {exc}")
                    suggestions = []

                if suggestions:
                    suggestion_labels = [
                        (
                            f"r/{item['display_name']}"
                            + (
                                f" ({int(item['subscribers']):,})"
                                if item.get("subscribers") not in (None, "")
                                else ""
                            )
                        )
                        for item in suggestions
                    ]
                    suggestion_key = f"{field_key}_suggestions"
                    selected_label = st.selectbox(
                        label=f"{title} suggestions {field_id + 1}",
                        options=["Choose a match..."] + suggestion_labels,
                        index=0,
                        key=suggestion_key,
                        on_change=apply_subreddit_suggestion,
                        args=(group_key, field_id, suggestion_key),
                        label_visibility="collapsed",
                    )

    if st.button(add_button_label, key=f"add_{group_key}", width="content"):
        add_field(group_key, next_id_key)
        st.rerun()


def render_post_cards(post_records: list[dict]) -> None:
    st.subheader("Posts")
    for index, post in enumerate(post_records, start=1):
        title = str(post.get("title") or "[No title]").strip()
        permalink = str(post.get("permalink") or "").strip()
        subreddit = str(post.get("subreddit") or "").strip()
        author = str(post.get("author") or "[deleted]").strip()
        created_iso = str(post.get("created_iso") or "").strip()
        score = post.get("score", 0)
        num_comments = post.get("num_comments", 0)
        flair = str(post.get("link_flair_text") or "").strip()
        removed_by = str(post.get("removed_by") or "").strip()
        removed_by_category = str(post.get("removed_by_category") or "").strip()
        url = str(post.get("url") or "").strip()
        preview_text = str(post.get("selftext") or "").strip()
        if len(preview_text) > 420:
            preview_text = preview_text[:420].rstrip() + "..."

        with st.container(border=True):
            header_cols = st.columns([0.82, 0.18])
            with header_cols[0]:
                if permalink:
                    st.markdown(f"### [{title}]({permalink})")
                else:
                    st.markdown(f"### {title}")
                meta_bits = []
                if subreddit:
                    meta_bits.append(f"r/{subreddit}")
                if author:
                    meta_bits.append(f"u/{author}")
                if created_iso:
                    meta_bits.append(created_iso)
                st.caption(" • ".join(meta_bits) if meta_bits else f"Post #{index}")
            with header_cols[1]:
                st.metric("Score", score)
                st.caption(f"Comments: {num_comments}")

            if flair or removed_by or removed_by_category:
                tag_parts = []
                if flair:
                    tag_parts.append(f"Flair: {flair}")
                if removed_by or removed_by_category:
                    removed_label = removed_by_category or removed_by
                    tag_parts.append(f"Removed: {removed_label}")
                st.caption(" • ".join(tag_parts))

            if preview_text:
                st.write(preview_text)
            else:
                st.caption("No selftext available.")

            if str(post.get("selftext") or "").strip():
                with st.expander("More"):
                    st.write(str(post.get("selftext") or "").strip())

            link_cols = st.columns(2)
            if url:
                link_cols[0].markdown(f"[Open URL]({url})")
            if permalink:
                link_cols[1].markdown(f"[Open Reddit thread]({permalink})")

            with st.expander("Show full metadata"):
                st.json(post)


@st.cache_data(ttl=120)
def get_subreddit_suggestions(base_url: str, prefix: str) -> list[dict]:
    client = ArcticShiftClient(base_url=base_url)
    return client.search_subreddits(prefix=prefix, limit=10)


def main() -> None:
    st.set_page_config(page_title="Reddit Data Scraper", page_icon="📊", layout="wide")

    st.title("Reddit Data Scraper")
    st.caption("Arctic Shift-backed search for Reddit posts and comments")

    init_field_group("subreddit_fields", "subreddit_next_id")
    init_field_group("keyword_fields", "keyword_next_id")

    if "search_state" not in st.session_state:
        st.session_state["search_state"] = None
    if "search_notice" not in st.session_state:
        st.session_state["search_notice"] = ""
    if "search_auto_rerun_pending" not in st.session_state:
        st.session_state["search_auto_rerun_pending"] = False
    if "search_auto_rerun_count" not in st.session_state:
        st.session_state["search_auto_rerun_count"] = 0
    if "search_continue_resume_active" not in st.session_state:
        st.session_state["search_continue_resume_active"] = False
    if "search_resume_after" not in st.session_state:
        st.session_state["search_resume_after"] = None
    if "search_resume_before" not in st.session_state:
        st.session_state["search_resume_before"] = None
    if "search_run_status" not in st.session_state:
        st.session_state["search_run_status"] = "Waiting to start a search."
    if "search_retry_status" not in st.session_state:
        st.session_state["search_retry_status"] = "Error mode: Off"
    if "search_retry_mode_label" not in st.session_state:
        st.session_state["search_retry_mode_label"] = None
    if "posts_page_index" not in st.session_state:
        st.session_state["posts_page_index"] = 0
    if "search_posts_found_total" not in st.session_state:
        st.session_state["search_posts_found_total"] = 0
    if "search_posts_stored_total" not in st.session_state:
        st.session_state["search_posts_stored_total"] = 0
    if "search_empty_pass_streak" not in st.session_state:
        st.session_state["search_empty_pass_streak"] = 0
    if "search_timeout_pass_streak" not in st.session_state:
        st.session_state["search_timeout_pass_streak"] = 0
    if "search_stop_requested" not in st.session_state:
        st.session_state["search_stop_requested"] = False

    with st.sidebar:
        st.header("Search Settings")

        def enable_retry_from_scratch() -> None:
            if st.session_state.get("retry_from_scratch"):
                st.session_state["continue_from_left_off"] = False

        def enable_continue_from_left_off() -> None:
            if st.session_state.get("continue_from_left_off"):
                st.session_state["retry_from_scratch"] = False

        base_url = st.text_input(
            "Arctic Shift API Base URL",
            value="https://arctic-shift.photon-reddit.com",
            help="Leave as-is to use the public Arctic Shift endpoint.",
        )
        render_input_group(
            title="Subreddits",
            group_key="subreddit_fields",
            next_id_key="subreddit_next_id",
            placeholder="Enter a subreddit name",
            add_button_label="Add another subreddit",
            base_url=base_url,
            autocomplete=True,
        )

        render_input_group(
            title="Keyword/s",
            group_key="keyword_fields",
            next_id_key="keyword_next_id",
            placeholder="Enter a keyword or phrase",
            add_button_label="Add another keyword",
        )
        keyword_mode = st.radio(
            "Keyword match mode",
            options=["any", "all"],
            format_func=lambda value: "Match any keyword" if value == "any" else "Match all keywords",
            horizontal=True,
            index=0,
        )
        st.caption("Use 'Match all keywords' for a narrower search when you want every keyword to be required.")

        after = st.text_input(
            "After (UTC)",
            value="",
            help="Optional UTC date or datetime, e.g. 2024-01-01 or 2024-01-01T00:00:00",
        )
        before = st.text_input(
            "Before (UTC)",
            value="",
            help="Optional UTC date or datetime, e.g. 2024-12-31 or 2024-12-31T23:59:59",
        )
        limit = st.number_input("Total posts stored", min_value=1, max_value=100000, value=50, step=10)
        search_rate = st.slider(
            "Searches per second",
            min_value=0.1,
            max_value=5.0,
            value=0.25,
            step=0.05,
            help="Lower values slow the app down more and reduce timeout risk.",
        )
        request_pause = 1.0 / float(search_rate)
        delay_bar = st.progress(min(float(search_rate) / 2.0, 1.0))
        st.caption(f"Current pacing: {float(search_rate):.2f} searches/sec ({request_pause:.1f}s between requests)")
        sort = st.selectbox("Sort", options=["desc", "asc"], index=0)
        exclude_removed = st.checkbox("Exclude Removed Posts", value=True)
        include_comments = st.checkbox("Include Comments", value=False)
        st.caption("In case of error")
        retry_from_scratch = False
        continue_from_left_off = st.checkbox(
            "Continue from last stop",
            value=False,
            key="continue_from_left_off",
            on_change=enable_continue_from_left_off,
            help="Resume from the last collected posts instead of starting over.",
        )
        run_until_done = st.checkbox(
            "Run until done",
            value=False,
            key="run_until_done",
            disabled=not continue_from_left_off,
            help="Keep rerunning automatically until the kept-post target is met or no new kept matches are found.",
        )
        retry_max_tries = st.number_input(
            "Auto rerun max tries",
            min_value=1,
            max_value=100,
            value=2,
            step=1,
            disabled=run_until_done or not continue_from_left_off,
            help="Caps the number of automatic retries so the app does not loop forever.",
        )
        retry_pause_seconds = st.slider(
            "Time between retries (sec)",
            min_value=1,
            max_value=20,
            value=3,
            step=1,
            help="How long to wait before an automatic rerun/retry starts.",
        )
        if run_until_done and not continue_from_left_off:
            st.session_state["run_until_done"] = False
            run_until_done = False
        run_until_done_enabled = bool(run_until_done and continue_from_left_off)

        selected_retry_mode = None
        if continue_from_left_off:
            selected_retry_mode = "Continue from last stop"

        if selected_retry_mode:
            st.session_state["search_retry_mode_label"] = selected_retry_mode
        else:
            st.session_state["search_retry_mode_label"] = None

        if run_until_done_enabled:
            st.session_state["search_retry_status"] = "Run until done: Active"
        elif selected_retry_mode:
            st.session_state["search_retry_status"] = build_retry_status(
                st.session_state["search_retry_mode_label"],
                min(int(st.session_state.get("search_auto_rerun_count", 0)) + 1, int(retry_max_tries)),
                int(retry_max_tries),
            )
        else:
            st.session_state["search_retry_status"] = "Error mode: Off"
        st.caption(st.session_state["search_retry_status"])
        comment_limit = st.number_input(
            "Comment Limit per Post",
            min_value=1,
            max_value=200,
            value=20,
            step=5,
            disabled=not include_comments,
        )
        run_search_clicked = st.button("Run Search", type="primary", width="stretch")
        run_search_auto = bool(st.session_state.get("search_auto_rerun_pending", False))
        run_search = run_search_clicked or run_search_auto
        if run_search_clicked:
            st.session_state["search_auto_rerun_count"] = 0
            st.session_state["search_empty_pass_streak"] = 0
            st.session_state["search_timeout_pass_streak"] = 0
            st.session_state["search_stop_requested"] = False
            st.session_state["search_retry_mode_label"] = selected_retry_mode
            if not selected_retry_mode:
                st.session_state["search_posts_found_total"] = 0
                st.session_state["search_posts_stored_total"] = 0
        if st.button("Stop Search", width="stretch"):
            st.session_state["search_stop_requested"] = True
            st.session_state["search_auto_rerun_pending"] = False
            st.session_state["search_notice"] = "Stop requested. Finishing current pass and saving collected posts."
            st.session_state["search_run_status"] = "Stopping search..."
            st.rerun()
        if st.button("Reset Results", width="stretch"):
            st.session_state["search_state"] = None
            st.session_state["search_notice"] = ""
            st.session_state["search_auto_rerun_pending"] = False
            st.session_state["search_auto_rerun_count"] = 0
            st.session_state["search_continue_resume_active"] = False
            st.session_state["search_resume_after"] = None
            st.session_state["search_resume_before"] = None
            st.session_state["search_run_status"] = "Waiting to start a search."
            st.session_state["search_retry_mode_label"] = None
            st.session_state["search_retry_status"] = "Error mode: Off"
            st.session_state["posts_page_index"] = 0
            st.session_state["search_posts_found_total"] = 0
            st.session_state["search_posts_stored_total"] = 0
            st.session_state["search_empty_pass_streak"] = 0
            st.session_state["search_timeout_pass_streak"] = 0
            st.session_state["search_stop_requested"] = False
            st.rerun()

        progress_panel = st.container(border=True)
        progress_panel.markdown("**Live progress**")
        retry_status_placeholder = progress_panel.empty()
        progress_subreddit = progress_panel.empty()
        status_placeholder = progress_panel.empty()
        notice_placeholder = progress_panel.empty()
        found_placeholder = progress_panel.empty()
        total_placeholder = progress_panel.empty()
        progress_bar = progress_panel.progress(0.0)
        if run_until_done_enabled:
            st.session_state["search_retry_status"] = (
                f"Run until done: Pass {int(st.session_state.get('search_auto_rerun_count', 0)) + 1}"
            )
        else:
            st.session_state["search_retry_status"] = build_retry_status(
                st.session_state.get("search_retry_mode_label"),
                min(int(st.session_state.get("search_auto_rerun_count", 0)) + 1, int(retry_max_tries)),
                int(retry_max_tries),
            )
        retry_status_placeholder.caption(st.session_state["search_retry_status"])
        progress_subreddit.markdown(st.session_state.get("search_run_status", "Waiting to start a search."))
        status_placeholder.info(st.session_state.get("search_run_status", "Waiting to start a search."))
        found_placeholder.metric("Posts found", int(st.session_state.get("search_posts_found_total", 0)))
        total_placeholder.metric("Posts kept", int(st.session_state.get("search_posts_stored_total", 0)))
        if st.session_state.get("search_notice"):
            notice_placeholder.warning(st.session_state["search_notice"])

        if run_search_auto:
            st.session_state["search_auto_rerun_pending"] = False

    st.info("This Arctic Shift mode does not require Reddit API credentials or a .env file.")

    if run_search:
        subreddits = get_field_values("subreddit_fields")
        if not subreddits:
            st.error("Add at least one subreddit.")
            return

        keyword_values = get_field_values("keyword_fields")
        query = build_keyword_query(keyword_values, keyword_mode)
        if not query:
            st.error("Add at least one keyword.")
            return

        st.session_state["search_notice"] = ""
        st.session_state["search_run_status"] = "Starting search..."
        if run_until_done_enabled:
            st.session_state["search_retry_status"] = (
                f"Run until done: Pass {int(st.session_state.get('search_auto_rerun_count', 0)) + 1}"
            )
        else:
            st.session_state["search_retry_status"] = build_retry_status(
                st.session_state.get("search_retry_mode_label"),
                min(int(st.session_state.get("search_auto_rerun_count", 0)) + 1, int(retry_max_tries)),
                int(retry_max_tries),
            )

        search_terms = build_search_terms(keyword_values)
        continuation_active = bool(continue_from_left_off and st.session_state.get("search_continue_resume_active"))
        retry_resume_active = bool(st.session_state.get("search_auto_rerun_count", 0)) > 0
        persistent_run_active = continuation_active or retry_resume_active
        base_found_total = int(st.session_state.get("search_posts_found_total", 0)) if persistent_run_active else 0
        progress_state = {"parsed": base_found_total, "stored": 0, "subreddit": "", "term": "", "limit": int(limit)}
        search_error_message = ""
        request_pause_value = float(request_pause)
        effective_after = st.session_state.get("search_resume_after") if continuation_active else after
        effective_before = st.session_state.get("search_resume_before") if continuation_active else before
        base_search_state = st.session_state.get("search_state") if persistent_run_active else None
        base_post_records = list(base_search_state.get("post_records", [])) if base_search_state else []
        base_comment_records = list(base_search_state.get("comment_records", [])) if base_search_state else []
        live_stored_posts = list(base_post_records)
        progress_state["stored"] = len(live_stored_posts)
        st.session_state["search_posts_found_total"] = base_found_total
        st.session_state["search_posts_stored_total"] = len(live_stored_posts)
        seen_post_ids: set[str] = {
            str(post.get("id") or "").strip() for post in base_post_records if str(post.get("id") or "").strip()
        }

        def should_store_post(post: object) -> bool:
            if not exclude_removed:
                return True
            removed_by = str(getattr(post, "removed_by", "") or "").strip()
            removed_by_category = str(getattr(post, "removed_by_category", "") or "").strip()
            title = str(getattr(post, "title", "") or "").strip().lower()
            selftext = str(getattr(post, "selftext", "") or "").strip().lower()
            return not (
                removed_by
                or removed_by_category
                or title in {"[removed]", "[deleted]"}
                or selftext in {"[removed]", "[deleted]"}
            )

        def add_post_if_new(post: object) -> bool:
            if len(live_stored_posts) >= int(limit):
                return False
            post_id = str(getattr(post, "id", "") or "").strip()
            if not post_id or post_id in seen_post_ids:
                return False
            seen_post_ids.add(post_id)
            all_posts.append(post)
            if should_store_post(post):
                live_stored_posts.append(post)
                progress_state["stored"] = len(live_stored_posts)
                st.session_state["search_posts_stored_total"] = progress_state["stored"]
                total_placeholder.metric("Posts kept", progress_state["stored"])
                progress_bar.progress(min(len(live_stored_posts) / max(1, progress_state["limit"]), 1.0))
            return True

        def on_post_accepted(post: object) -> bool:
            if st.session_state.get("search_stop_requested"):
                return False
            add_post_if_new(post)
            # Stop only when the kept-post target is reached.
            return len(live_stored_posts) < int(limit)

        def make_progress_callback(term: str, parsed_offset: int):
            def update_progress(payload: dict[str, object]) -> None:
                progress_state["parsed"] = parsed_offset + int(payload.get("parsed", 0))
                progress_state["stored"] = len(live_stored_posts)
                progress_state["subreddit"] = str(payload.get("subreddit", progress_state["subreddit"]))
                progress_state["term"] = term
                st.session_state["search_posts_found_total"] = progress_state["parsed"]

                if progress_state["subreddit"]:
                    run_status = f"Searching: {progress_state['term']} in r/{progress_state['subreddit']}"
                elif progress_state["term"]:
                    run_status = f"Searching: {progress_state['term']}"
                else:
                    run_status = "Searching..."
                st.session_state["search_run_status"] = run_status
                if run_until_done_enabled:
                    st.session_state["search_retry_status"] = (
                        f"Run until done: Pass {int(st.session_state.get('search_auto_rerun_count', 0)) + 1}"
                    )
                else:
                    st.session_state["search_retry_status"] = build_retry_status(
                        st.session_state.get("search_retry_mode_label"),
                        min(int(st.session_state.get("search_auto_rerun_count", 0)) + 1, int(retry_max_tries)),
                        int(retry_max_tries),
                    )

                progress_subreddit.markdown(run_status)
                status_placeholder.info(run_status)
                retry_status_placeholder.caption(st.session_state.get("search_retry_status", "Error mode: Off"))

                found_placeholder.metric("Posts found", progress_state["parsed"])
                total_placeholder.metric("Posts kept", progress_state["stored"])
                progress_bar.progress(min(len(live_stored_posts) / max(1, progress_state["limit"]), 1.0))

            return update_progress

        def run_search_for_term(term: str, *, parsed_offset: int) -> tuple[list, int]:
            remaining = max(0, int(limit) - len(live_stored_posts))
            if remaining <= 0:
                return [], 0

            posts = client.search_posts(
                query=term,
                subreddits=subreddits,
                limit=remaining,
                sort=sort,
                after=effective_after,
                before=effective_before,
                progress_callback=make_progress_callback(term, parsed_offset),
                post_callback=on_post_accepted,
            )
            return posts, len(posts)

        all_posts: list = []
        client = ArcticShiftClient(base_url=base_url, request_pause=request_pause_value)
        auto_retry_enabled = bool(retry_from_scratch or continue_from_left_off)

        with st.spinner("Collecting posts from Arctic Shift..."):
            try:
                if keyword_mode == "any" and len(search_terms) > 1:
                    for term in search_terms:
                        if len(live_stored_posts) >= int(limit):
                            break
                        try:
                            run_search_for_term(term, parsed_offset=progress_state["parsed"])
                        except Exception as exc:
                            partial_posts = list(getattr(exc, "partial_records", []) or [])
                            for post in partial_posts:
                                if add_post_if_new(post) and len(live_stored_posts) >= int(limit):
                                    break
                            search_error_message = str(exc)
                            st.session_state["search_run_status"] = (
                                "Retrying from scratch..." if retry_from_scratch else "Continuing from last stop..."
                            )
                            st.session_state["search_retry_status"] = build_retry_status(
                                st.session_state.get("search_retry_mode_label"),
                                min(int(st.session_state.get("search_auto_rerun_count", 0)) + 1, int(retry_max_tries)),
                                int(retry_max_tries),
                            )
                            if not auto_retry_enabled:
                                st.exception(exc)
                                return
                            break
                else:
                    try:
                        all_posts = client.search_posts(
                            query=query.strip(),
                            subreddits=subreddits,
                            limit=int(limit),
                            sort=sort,
                            after=effective_after,
                            before=effective_before,
                            progress_callback=make_progress_callback(query.strip(), progress_state["parsed"]),
                            post_callback=on_post_accepted,
                        )
                    except Exception as exc:
                        partial_posts = list(getattr(exc, "partial_records", []) or [])
                        if partial_posts:
                            for post in partial_posts:
                                add_post_if_new(post)
                            search_error_message = str(exc)
                        elif auto_retry_enabled:
                            search_error_message = str(exc)
                            st.session_state["search_run_status"] = (
                                "Retrying from scratch..." if retry_from_scratch else "Continuing from last stop..."
                            )
                            st.session_state["search_retry_status"] = build_retry_status(
                                st.session_state.get("search_retry_mode_label"),
                                min(int(st.session_state.get("search_auto_rerun_count", 0)) + 1, int(retry_max_tries)),
                                int(retry_max_tries),
                            )
                        else:
                            st.exception(exc)
                            return
            except Exception as exc:
                partial_posts = list(getattr(exc, "partial_records", []) or [])
                if partial_posts:
                    for post in partial_posts:
                        add_post_if_new(post)
                    search_error_message = str(exc)
                elif auto_retry_enabled:
                    search_error_message = str(exc)
                    st.session_state["search_run_status"] = (
                        "Retrying from scratch..." if retry_from_scratch else "Continuing from last stop..."
                    )
                    st.session_state["search_retry_status"] = build_retry_status(
                        st.session_state.get("search_retry_mode_label"),
                        min(int(st.session_state.get("search_auto_rerun_count", 0)) + 1, int(retry_max_tries)),
                        int(retry_max_tries),
                    )
                else:
                    st.exception(exc)
                    return

        post_records = [asdict(post) for post in all_posts]
        if base_post_records:
            post_records = merge_record_lists(base_post_records, post_records)
        if exclude_removed:
            post_records = [
                post
                for post in post_records
                if not (
                    str(post.get("removed_by") or "").strip()
                    or str(post.get("removed_by_category") or "").strip()
                    or str(post.get("title") or "").strip().lower() == "[removed]"
                    or str(post.get("selftext") or "").strip().lower() == "[removed]"
                    or str(post.get("selftext") or "").strip().lower() == "[deleted]"
                )
            ]
        posts_df = pd.DataFrame(post_records)
        total_placeholder.metric("Posts kept", len(post_records))
        total_stored_count = len(post_records)
        st.session_state["search_posts_found_total"] = max(int(st.session_state.get("search_posts_found_total", 0)), progress_state["parsed"])
        st.session_state["search_posts_stored_total"] = total_stored_count

        if search_error_message:
            st.session_state["search_notice"] = (
                f"Search was rate limited or timed out: {search_error_message}. Showing the posts collected so far."
            )

        comment_records: list[dict] = []
        comments_df = pd.DataFrame()

        current_search_state = {
            "post_records": post_records,
            "posts_df": posts_df,
            "comment_records": comment_records,
            "comments_df": comments_df,
            "include_comments": include_comments,
            "query": query,
        }

        search_terminal_reason = "success"
        if search_error_message:
            search_terminal_reason = "timeout"
        elif len(live_stored_posts) >= int(limit):
            search_terminal_reason = "limit"
        elif not all_posts:
            search_terminal_reason = "empty"

        run_until_done_active = run_until_done_enabled
        stop_requested = bool(st.session_state.get("search_stop_requested"))

        if run_until_done_active and len(live_stored_posts) < int(limit) and not stop_requested:
            if all_posts:
                st.session_state["search_empty_pass_streak"] = 0
                st.session_state["search_timeout_pass_streak"] = 0
                current_retry_count = int(st.session_state.get("search_auto_rerun_count", 0))
                st.session_state["search_auto_rerun_count"] = current_retry_count + 1
                st.session_state["search_auto_rerun_pending"] = True

                resume_after, resume_before = resume_bounds_from_posts(all_posts)
                if sort == "desc":
                    st.session_state["search_resume_after"] = None
                    st.session_state["search_resume_before"] = resume_before
                else:
                    st.session_state["search_resume_after"] = resume_after
                    st.session_state["search_resume_before"] = None

                st.session_state["search_continue_resume_active"] = True
                st.session_state["search_run_status"] = f"Continuing scan ({current_retry_count + 1})..."
                st.session_state["search_retry_status"] = f"Run until done: Pass {current_retry_count + 1}"
                st.session_state["search_notice"] = "Continuing until target is reached or no new kept matches are found..."
                st.session_state["search_state"] = current_search_state
                time.sleep(int(retry_pause_seconds))
                st.rerun()
            else:
                if search_terminal_reason == "timeout":
                    st.session_state["search_timeout_pass_streak"] = int(st.session_state.get("search_timeout_pass_streak", 0)) + 1
                    st.session_state["search_empty_pass_streak"] = 0
                    current_retry_count = int(st.session_state.get("search_auto_rerun_count", 0))
                    st.session_state["search_auto_rerun_count"] = current_retry_count + 1
                    st.session_state["search_auto_rerun_pending"] = True
                    st.session_state["search_run_status"] = f"Recovering from timeout ({int(st.session_state.get('search_timeout_pass_streak', 0))})..."
                    st.session_state["search_retry_status"] = f"Run until done: Pass {current_retry_count + 1}"
                    st.session_state["search_notice"] = "Timeout pass returned no posts; continuing retries until target or true exhaustion."
                    st.session_state["search_state"] = current_search_state
                    time.sleep(int(retry_pause_seconds))
                    st.rerun()
                else:
                    st.session_state["search_timeout_pass_streak"] = 0
                    st.session_state["search_empty_pass_streak"] = int(st.session_state.get("search_empty_pass_streak", 0)) + 1
                    st.session_state["search_notice"] = "Run-until-done stopped: no more matching posts were returned for the current bounds."
        elif run_until_done_active and stop_requested:
            st.session_state["search_notice"] = "Search stopped by user. Showing collected posts so far."
            st.session_state["search_stop_requested"] = False

        if (not run_until_done_active) and (retry_from_scratch or continue_from_left_off) and search_terminal_reason == "timeout":
            current_retry_count = int(st.session_state.get("search_auto_rerun_count", 0))
            max_retry_count = int(retry_max_tries)
            if current_retry_count < max_retry_count - 1 and len(live_stored_posts) < int(limit):
                st.session_state["search_auto_rerun_count"] = current_retry_count + 1
                st.session_state["search_auto_rerun_pending"] = True

                if continue_from_left_off:
                    resume_after, resume_before = resume_bounds_from_posts(all_posts)
                    if sort == "desc":
                        st.session_state["search_resume_after"] = None
                        st.session_state["search_resume_before"] = resume_before
                    else:
                        st.session_state["search_resume_after"] = resume_after
                        st.session_state["search_resume_before"] = None
                    st.session_state["search_continue_resume_active"] = True
                    st.session_state["search_run_status"] = (
                        f"Continuing from last stop ({current_retry_count + 1}/{max_retry_count})..."
                    )
                    st.session_state["search_retry_status"] = build_retry_status(
                        st.session_state.get("search_retry_mode_label"),
                        current_retry_count + 1,
                        max_retry_count,
                    )
                    st.session_state["search_notice"] = "Continuing from where the search left off..."
                else:
                    st.session_state["search_run_status"] = (
                        f"Retrying from scratch ({current_retry_count + 1}/{max_retry_count})..."
                    )
                    st.session_state["search_retry_status"] = build_retry_status(
                        st.session_state.get("search_retry_mode_label"),
                        current_retry_count + 1,
                        max_retry_count,
                    )
                    st.session_state["search_notice"] = "Retrying the search from the start..."
                st.session_state["search_state"] = current_search_state
                time.sleep(int(retry_pause_seconds))
                st.rerun()
            st.session_state["search_auto_rerun_count"] = min(current_retry_count, max_retry_count - 1)
            st.session_state["search_retry_status"] = build_retry_status(
                st.session_state.get("search_retry_mode_label"),
                min(current_retry_count + 1, max_retry_count),
                max_retry_count,
            )
            if not continue_from_left_off:
                st.session_state["search_continue_resume_active"] = False
                st.session_state["search_resume_after"] = None
                st.session_state["search_resume_before"] = None
        if include_comments and post_records:
            with st.spinner("Collecting comments..."):
                try:
                    comments = []
                    for index, post in enumerate(all_posts, start=1):
                        comments.extend(
                            client.fetch_comments_for_post(
                                post.id,
                                query=query.strip(),
                                limit=int(comment_limit),
                            )
                        )
                        if index < len(all_posts):
                            time.sleep(request_pause_value)
                except Exception as exc:
                    st.exception(exc)
                    return

            comment_records = [asdict(comment) for comment in comments]
            if base_comment_records:
                comment_records = merge_record_lists(base_comment_records, comment_records)
            comments_df = pd.DataFrame(comment_records)

        st.session_state["search_state"] = current_search_state
        st.session_state["search_run_status"] = f"Collected {len(post_records)} total posts."
        if run_until_done_enabled:
            st.session_state["search_retry_status"] = (
                f"Run until done: Pass {int(st.session_state.get('search_auto_rerun_count', 0)) + 1}"
            )
        else:
            st.session_state["search_retry_status"] = build_retry_status(
                st.session_state.get("search_retry_mode_label"),
                min(int(st.session_state.get("search_auto_rerun_count", 0)) + 1, int(retry_max_tries)),
                int(retry_max_tries),
            )
        st.session_state["posts_page_index"] = 0
        if not (retry_from_scratch or continue_from_left_off):
            st.session_state["search_continue_resume_active"] = False
            st.session_state["search_resume_after"] = None
            st.session_state["search_resume_before"] = None

    search_state = st.session_state.get("search_state")
    if search_state:
        post_records = search_state.get("post_records", [])
        posts_df = search_state.get("posts_df", pd.DataFrame())
        comment_records = search_state.get("comment_records", [])
        comments_df = search_state.get("comments_df", pd.DataFrame())
        include_comments = bool(search_state.get("include_comments", False))
        posts_page_size = 5
        total_post_pages = max(1, (len(post_records) + posts_page_size - 1) // posts_page_size)
        st.session_state["posts_page_index"] = min(max(int(st.session_state.get("posts_page_index", 0)), 0), total_post_pages - 1)
        posts_page_index = int(st.session_state.get("posts_page_index", 0))
        page_start = posts_page_index * posts_page_size
        page_end = min(page_start + posts_page_size, len(post_records))
        page_records = post_records[page_start:page_end]

        def render_post_page_nav(prefix: str) -> None:
            nav_cols = st.columns([0.18, 0.64, 0.18])
            with nav_cols[0]:
                if st.button(f"Previous##{prefix}", width="stretch", disabled=posts_page_index <= 0):
                    st.session_state["posts_page_index"] = max(posts_page_index - 1, 0)
                    st.rerun()
            with nav_cols[1]:
                st.caption(
                    f"Page {posts_page_index + 1} of {total_post_pages} • Showing posts {page_start + 1}-{page_end} of {len(post_records)}"
                )
            with nav_cols[2]:
                if st.button(f"Next##{prefix}", width="stretch", disabled=posts_page_index >= total_post_pages - 1):
                    st.session_state["posts_page_index"] = min(posts_page_index + 1, total_post_pages - 1)
                    st.rerun()

        st.success(f"Collected {len(post_records)} posts.")

        if posts_df.empty:
            st.warning("No posts matched your query.")
        else:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            posts_csv_name = f"reddit_posts_{timestamp}.csv"
            posts_jsonl_name = f"reddit_posts_{timestamp}.jsonl"

            download_cols = st.columns(2)
            with download_cols[0]:
                st.download_button(
                    "Download Posts CSV",
                    data=to_csv_bytes(posts_df),
                    file_name=posts_csv_name,
                    mime="text/csv",
                    width="stretch",
                )
            with download_cols[1]:
                st.download_button(
                    "Download Posts JSONL",
                    data=to_jsonl_bytes(post_records),
                    file_name=posts_jsonl_name,
                    mime="application/json",
                    width="stretch",
                )

            render_post_page_nav("top")

            render_post_cards(page_records)

            render_post_page_nav("bottom")

            with st.expander("Show table view"):
                st.dataframe(posts_df, width="stretch")

            if include_comments:
                st.subheader("Comments")
                st.write(f"Collected {len(comment_records)} comments.")

                if not comments_df.empty:
                    comments_csv_name = f"reddit_comments_{timestamp}.csv"
                    comments_jsonl_name = f"reddit_comments_{timestamp}.jsonl"

                    comment_download_cols = st.columns(2)
                    with comment_download_cols[0]:
                        st.download_button(
                            "Download Comments CSV",
                            data=to_csv_bytes(comments_df),
                            file_name=comments_csv_name,
                            mime="text/csv",
                            width="stretch",
                        )
                    with comment_download_cols[1]:
                        st.download_button(
                            "Download Comments JSONL",
                            data=to_jsonl_bytes(comment_records),
                            file_name=comments_jsonl_name,
                            mime="application/json",
                            width="stretch",
                        )

                    st.dataframe(comments_df, width="stretch")
                else:
                    st.warning("No comments found for the matched posts.")


if __name__ == "__main__":
    main()

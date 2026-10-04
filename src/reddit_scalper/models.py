from dataclasses import dataclass


@dataclass
class PostRecord:
    id: str
    subreddit: str
    title: str
    selftext: str
    author: str
    score: int
    upvote_ratio: float
    num_comments: int
    created_utc: int
    created_iso: str
    permalink: str
    url: str
    domain: str
    over_18: bool
    spoiler: bool
    locked: bool
    stickied: bool
    link_flair_text: str
    removed_by: str
    removed_by_category: str
    query: str
    fetched_at: str


@dataclass
class CommentRecord:
    id: str
    post_id: str
    parent_id: str
    subreddit: str
    author: str
    body: str
    score: int
    created_utc: int
    created_iso: str
    permalink: str
    query: str
    fetched_at: str
"""
ingest.py — Forward-only ingest of new posts and comments from r/datascience
into the Dimension Labs Universal tracker.

Reddit data source: public .json endpoints (no API key, no OAuth).
Tracker: POST https://tracker.dimensionlabs.io/track with query params
  platform=universal, v=11.1.0-rest, type=<incoming|outgoing>, apiKey=<key>
and a JSON body of shape:
  { text, userId, sessionId, conversationId, platformJson }

Locked event-type mapping:
  - posts                                 -> outgoing
  - comments where author != post.author  -> incoming
  - comments where author == post.author  -> outgoing  (self-reply)

Skip rule: author in {"[deleted]", "[removed]", None} -> no event sent
(but the fullname is still recorded so we never re-evaluate it).

State (state.json) records the most recent post/comment fullnames seen
so the run is forward-only across invocations. On first run the state is
seeded from the current top-of-listing items and nothing is sent.

Usage:
  python ingest.py --dry-run    # print events that would be sent
  python ingest.py              # send events to the tracker
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SUBREDDIT = "datascience"
USER_AGENT = "dl-datascience-json-ingest/0.1 (+https://github.com/jasondashbot/dl-datascience-json-ingest)"
REDDIT_BASE = "https://www.reddit.com"
NEW_POSTS_URL = f"{REDDIT_BASE}/r/{SUBREDDIT}/new.json"
NEW_COMMENTS_URL = f"{REDDIT_BASE}/r/{SUBREDDIT}/comments.json"
POST_THREAD_URL = f"{REDDIT_BASE}/comments/{{post_id}}.json"

TRACKER_URL = "https://tracker.dimensionlabs.io/track"
TRACKER_PLATFORM = "universal"
TRACKER_VERSION = "11.1.0-rest"

SKIP_AUTHORS = {"[deleted]", "[removed]", None, ""}

REQUEST_DELAY_SECONDS = 1.1  # be polite to Reddit
HTTP_TIMEOUT = 20

STATE_PATH = Path(__file__).resolve().parent / "state.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def hash_user(author: str) -> str:
    """SHA-256 of the author username, hex-encoded, truncated to 16 chars."""
    return hashlib.sha256(author.encode("utf-8")).hexdigest()[:16]


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {"seen_posts": [], "seen_comments": []}


def save_state(state: dict) -> None:
    # Cap state lists to avoid unbounded growth.
    state["seen_posts"] = state["seen_posts"][-2000:]
    state["seen_comments"] = state["seen_comments"][-5000:]
    STATE_PATH.write_text(json.dumps(state, indent=2))


def reddit_get(url: str, *, params: dict | None = None) -> dict | list:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    resp = requests.get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT)
    if resp.status_code == 429:
        # Back off once on rate limit.
        time.sleep(5)
        resp = requests.get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def fetch_new_posts(limit: int = 100) -> list[dict]:
    data = reddit_get(NEW_POSTS_URL, params={"limit": limit, "raw_json": 1})
    return [c["data"] for c in data["data"]["children"] if c.get("kind") == "t3"]


def fetch_new_comments(limit: int = 100) -> list[dict]:
    data = reddit_get(NEW_COMMENTS_URL, params={"limit": limit, "raw_json": 1})
    return [c["data"] for c in data["data"]["children"] if c.get("kind") == "t1"]


def fetch_post_author(post_id: str) -> str | None:
    """Resolve the author of a post by id (e.g. '1abcd2'), used when a comment's
    parent post was not seen in the current new.json batch."""
    url = POST_THREAD_URL.format(post_id=post_id)
    payload = reddit_get(url, params={"limit": 1, "raw_json": 1})
    try:
        post = payload[0]["data"]["children"][0]["data"]
        return post.get("author")
    except (KeyError, IndexError, TypeError):
        return None


def is_skippable_author(author: str | None) -> bool:
    return author in SKIP_AUTHORS


# ---------------------------------------------------------------------------
# Event construction
# ---------------------------------------------------------------------------

def build_post_event(post: dict) -> dict:
    title = post.get("title") or ""
    selftext = post.get("selftext") or ""
    text = title + "\n\n" + selftext
    fullname = post["name"]  # t3_xxx
    return {
        "type": "outgoing",
        "body": {
            "text": text,
            "userId": hash_user(post["author"]),
            "sessionId": fullname,
            "conversationId": fullname,
            "platformJson": {
                "kind": "t3",
                "fullname": fullname,
                "id": post.get("id"),
                "subreddit": post.get("subreddit"),
                "permalink": post.get("permalink"),
                "url": post.get("url"),
                "created_utc": post.get("created_utc"),
                "is_self": post.get("is_self"),
                "title": title,
                "author": post.get("author"),
            },
        },
    }


def build_comment_event(comment: dict, post_author: str | None) -> dict:
    body_text = comment.get("body") or ""
    parent_post_fullname = comment.get("link_id")  # t3_xxx
    comment_author = comment.get("author")
    is_self_reply = bool(post_author) and comment_author == post_author
    event_type = "outgoing" if is_self_reply else "incoming"
    return {
        "type": event_type,
        "body": {
            "text": body_text,
            "userId": hash_user(comment_author),
            "sessionId": parent_post_fullname,
            "conversationId": parent_post_fullname,
            "platformJson": {
                "kind": "t1",
                "fullname": comment.get("name"),
                "id": comment.get("id"),
                "subreddit": comment.get("subreddit"),
                "permalink": comment.get("permalink"),
                "link_id": parent_post_fullname,
                "parent_id": comment.get("parent_id"),
                "created_utc": comment.get("created_utc"),
                "author": comment_author,
                "post_author": post_author,
                "is_self_reply": is_self_reply,
            },
        },
    }


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------

def post_to_tracker(api_key: str, event_type: str, body: dict, *, dry_run: bool) -> None:
    params = {
        "platform": TRACKER_PLATFORM,
        "v": TRACKER_VERSION,
        "type": event_type,
        "apiKey": api_key,
    }
    if dry_run:
        print(json.dumps({"params": {**params, "apiKey": "<redacted>"}, "body": body}, indent=2))
        return
    resp = requests.post(TRACKER_URL, params=params, json=body, timeout=HTTP_TIMEOUT)
    if not resp.ok:
        print(f"[tracker] {resp.status_code} {resp.text[:200]}", file=sys.stderr)
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run(dry_run: bool) -> None:
    api_key = os.environ.get("DIMENSION_LABS_API_KEY", "")
    if not dry_run and not api_key:
        print("DIMENSION_LABS_API_KEY is not set. Set it or use --dry-run.", file=sys.stderr)
        sys.exit(2)

    state = load_state()
    seen_posts: set[str] = set(state["seen_posts"])
    seen_comments: set[str] = set(state["seen_comments"])
    first_run = not seen_posts and not seen_comments

    posts = fetch_new_posts(limit=100)
    time.sleep(REQUEST_DELAY_SECONDS)
    comments = fetch_new_comments(limit=100)

    # Build a lookup from post fullname -> author for comments that reference
    # posts in the current new.json batch.
    post_author_by_fullname: dict[str, str | None] = {p["name"]: p.get("author") for p in posts}

    if first_run:
        print(f"[seed] First run: recording {len(posts)} posts and {len(comments)} comments without sending.")
        for p in posts:
            seen_posts.add(p["name"])
        for c in comments:
            seen_comments.add(c["name"])
        state["seen_posts"] = sorted(seen_posts)
        state["seen_comments"] = sorted(seen_comments)
        save_state(state)
        return

    new_posts = [p for p in posts if p["name"] not in seen_posts]
    new_comments = [c for c in comments if c["name"] not in seen_comments]

    # Process oldest-first so sessionId/conversationId for the post lands before its comments.
    new_posts.sort(key=lambda p: p.get("created_utc", 0))
    new_comments.sort(key=lambda c: c.get("created_utc", 0))

    print(f"[ingest] new posts: {len(new_posts)}, new comments: {len(new_comments)}")

    for post in new_posts:
        author = post.get("author")
        fullname = post["name"]
        if is_skippable_author(author):
            print(f"[skip] post {fullname} author={author!r}")
        else:
            event = build_post_event(post)
            post_to_tracker(api_key, event["type"], event["body"], dry_run=dry_run)
        seen_posts.add(fullname)

    for comment in new_comments:
        author = comment.get("author")
        fullname = comment["name"]
        parent_post_fullname = comment.get("link_id")
        if is_skippable_author(author):
            print(f"[skip] comment {fullname} author={author!r}")
            seen_comments.add(fullname)
            continue

        # Resolve post author for self-reply check.
        post_author = post_author_by_fullname.get(parent_post_fullname)
        if post_author is None and parent_post_fullname and parent_post_fullname.startswith("t3_"):
            time.sleep(REQUEST_DELAY_SECONDS)
            post_author = fetch_post_author(parent_post_fullname.split("_", 1)[1])
            post_author_by_fullname[parent_post_fullname] = post_author

        event = build_comment_event(comment, post_author)
        post_to_tracker(api_key, event["type"], event["body"], dry_run=dry_run)
        seen_comments.add(fullname)

    state["seen_posts"] = sorted(seen_posts)
    state["seen_comments"] = sorted(seen_comments)
    save_state(state)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest r/datascience -> Dimension Labs Universal tracker")
    parser.add_argument("--dry-run", action="store_true", help="Print mapped events instead of POSTing")
    args = parser.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()

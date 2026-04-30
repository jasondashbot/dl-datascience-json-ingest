# dl-datascience-json-ingest

Forward-only ingest of new posts and comments from **r/datascience** into the **Dimension Labs Universal tracker**, using Reddit's public `.json` endpoints (no API key required).

## What it does

This project polls Reddit's public JSON endpoints for new submissions and comments in `r/datascience`, normalizes each item into a Dimension Labs tracking event, and POSTs it to the Universal tracker. It is forward-only: a small `state.json` file records the most recent post/comment fullnames seen so historical items are not re-ingested.

## Scope (v1)

- Subreddit: `r/datascience` only.
- Forward-only. On first run, the state file is seeded from the current top-of-listing items and nothing is sent.
- No backfill, no edits, no deletes.

## Endpoint contract

All events are sent via:

```
POST https://tracker.dimensionlabs.io/track
  ?platform=universal
  &v=11.1.0-rest
  &type=<incoming|outgoing>
  &apiKey=<DIMENSION_LABS_API_KEY>
```

JSON body shape:

```json
{
  "text": "...",
  "userId": "<sha256(author)[:16]>",
  "sessionId": "t3_xxxxxx",
  "conversationId": "t3_xxxxxx",
  "platformJson": { "...": "..." }
}
```

- `sessionId` and `conversationId` are both the Reddit fullname of the post the item belongs to (the `t3_xxx` id).
- `userId` is the SHA-256 hash of the Reddit author's username, hex-encoded and truncated to the first 16 hex characters.
- `platformJson` carries the raw Reddit fields useful for downstream debugging (kind, fullname, permalink, created_utc, subreddit, etc.).

## Event-type mapping (locked)

| Item | `type` |
|---|---|
| Post (submission) | `outgoing` |
| Comment where `comment.author != post.author` | `incoming` |
| Comment where `comment.author == post.author` (self-reply) | `outgoing` |

## `text` field

- Posts: `title + "\n\n" + selftext` (selftext may be empty for link posts; the trailing blank stays).
- Comments: `body`.

## Skip rules

An item is skipped (no event sent) when its `author` is any of:

- `"[deleted]"`
- `"[removed]"`
- `null` / missing

The item's fullname is still recorded in state so we never re-evaluate it.

## Reddit endpoints used

- `https://www.reddit.com/r/datascience/new.json?limit=100` — newest submissions.
- `https://www.reddit.com/r/datascience/comments.json?limit=100` — newest comments across the subreddit.
- `https://www.reddit.com/comments/<post_id>.json` — fallback used to resolve the post author when a comment's parent post hasn't been seen yet (needed for the self-reply mapping).

Reddit's public JSON endpoints work by appending `.json` to any Reddit URL. No OAuth, no API key. Be polite: send a descriptive `User-Agent` and keep request rate at roughly 1 req/sec.

## Run

```bash
pip install -r requirements.txt
cp .env.example .env  # then edit DIMENSION_LABS_API_KEY
python ingest.py --dry-run   # print mapped events without POSTing
python ingest.py             # send events to the tracker
```

State is persisted in `state.json` next to the script. Delete it to re-seed.

## Layout

- `ingest.py` — main script.
- `requirements.txt` — Python deps (`requests`).
- `.env.example` — environment variable template.
- `state.json` — created at runtime; tracks last-seen fullnames.
# dl-datascience-json-ingest
Forward-only ingest of new posts and comments from r/datascience into Dimension Labs' Universal tracker.

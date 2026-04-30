# dl-datascience-json-ingest

Forward-only ingest of new posts and comments from **r/datascience** into the **Dimension Labs Universal tracker**, using Reddit's public `.json` endpoints (no Reddit API key required).

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

- Posts: `title + "\n\n" + selftext`. `selftext` is always a string on Reddit's JSON, so we use `post.get("selftext") or ""` defensively. **Note:** `selftext` is **not** guaranteed empty for link posts (`is_self == False`). Reddit lets a self-style body coexist with a URL, so a link post can carry meaningful body text. Downstream consumers should not use `is_self` as a proxy for "has body text"; trust the `text` field.
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

## Field assumptions (verified)

These were confirmed empirically against a live 100-post / 100-comment sample from `r/datascience`:

- `post["name"]` is the `t3_xxx` fullname directly. No reconstruction from `kind` + `id` is needed.
- `comment["link_id"]` is already a `t3_` fullname. Stripping the `t3_` prefix yields the bare post id used by `/comments/<id>.json`.
- `selftext` is always present and always a string. It is **not** guaranteed empty when `is_self == False` (4 of 16 link posts in the sample carried non-empty body text alongside their URL).
- The `User-Agent` shown in `ingest.py` does not trigger 429s at ~1 req/sec from a residential IP.

## Where this needs to run

**A residential / non-datacenter IP.** Reddit returns `403 Blocked` for unauthenticated `.json` requests originating from cloud / datacenter IP ranges (AWS, Azure, GCP, GitHub-hosted Actions runners, etc.). The OAuth "script app" path that would let CI authenticate is **deprecated for new Reddit accounts**, so for v1 the only viable execution environment is a host on a residential connection: a desktop, laptop, Pi, or home server.

The repo ships a launchd plist under `deploy/` for running on an always-on macOS host.

## Run locally (ad-hoc / dry-run)

```bash
pip install -r requirements.txt
cp .env.example .env  # then edit DIMENSION_LABS_API_KEY
python ingest.py --dry-run   # print mapped events without POSTing
python ingest.py             # send events to the tracker
```

State is persisted in `state.json` next to the script. Delete it to re-seed.

## Scheduling: macOS launchd (recommended)

`deploy/com.dl-datascience-json-ingest.plist` defines a launchd job that runs `python ingest.py` every 15 minutes.

Before installing, edit the plist (locally; do not commit your API key) and replace the placeholders:

| Placeholder | What to put there |
|---|---|
| `__PYTHON__` | Absolute path to a Python with `requests` installed, e.g. `/usr/local/bin/python3` or `/Users/you/.../venv/bin/python` |
| `__REPO__` | Absolute path to your local clone, e.g. `/Users/you/code/dl-datascience-json-ingest` |
| `__API_KEY__` | Your Dimension Labs Universal tracker API key |
| `__LOG_DIR__` | Log directory, e.g. `/Users/you/Library/Logs/dl-datascience-json-ingest` |

Install:

```bash
mkdir -p "$HOME/Library/Logs/dl-datascience-json-ingest"
cp deploy/com.dl-datascience-json-ingest.plist "$HOME/Library/LaunchAgents/"
# edit the copied file to fill placeholders
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.dl-datascience-json-ingest.plist"
launchctl enable gui/$(id -u)/com.dl-datascience-json-ingest
```

Trigger an immediate run (useful for the seed run):

```bash
launchctl kickstart -k gui/$(id -u)/com.dl-datascience-json-ingest
```

Inspect:

```bash
launchctl print gui/$(id -u)/com.dl-datascience-json-ingest
tail -f "$HOME/Library/Logs/dl-datascience-json-ingest/ingest.log"
```

Uninstall:

```bash
launchctl bootout gui/$(id -u)/com.dl-datascience-json-ingest
rm "$HOME/Library/LaunchAgents/com.dl-datascience-json-ingest.plist"
```

Notes:

- launchd may delay slightly under load; treat the cadence as best-effort 15 min.
- The plist sets `RunAtLoad=true`, so the job fires once on login as well as on the interval.
- The desktop must be awake at the moment the interval fires; sleep / shutdown means missed runs (next interval picks up automatically).

## Why not GitHub Actions?

This project originally shipped a GitHub Actions cron workflow. We removed it because:

1. **Reddit blocks anonymous `.json` from datacenter IPs.** GitHub-hosted runners run on Azure ranges that Reddit walls off with `403 Blocked`. Confirmed in a live workflow run.
2. **Reddit's OAuth "script app" flow is deprecated for new accounts.** The standard mitigation (register a script app, do client-credentials OAuth, hit `oauth.reddit.com`) is no longer available to register, closing the CI auth path for new users.

Self-hosting a GitHub Actions runner on a residential machine would also work, but for a single-script ingest the launchd / cron route is operationally simpler.

## Layout

- `ingest.py` — main script.
- `requirements.txt` — Python deps (`requests`).
- `.env.example` — environment variable template.
- `deploy/com.dl-datascience-json-ingest.plist` — launchd job for macOS scheduling.
- `state.json` — created at runtime; tracks last-seen fullnames. Stored next to the script and ignored by git.
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

- Posts: `title + "\n\n" + selftext`. `selftext` is always a string on Reddit's JSON, so we use `post.get("selftext") or ""` defensively. **Note:** `selftext` is **not** guaranteed empty for link posts (`is_self == False`). Reddit lets a self-style body coexist with a URL, so a link post can carry meaningful body text. Downstream consumers should not use `is_self` as a proxy for "has body text"; trust the `text` field.
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

## Field assumptions (verified)

These were confirmed empirically against a live 100-post / 100-comment sample from `r/datascience`:

- `post["name"]` is the `t3_xxx` fullname directly. No reconstruction from `kind` + `id` is needed.
- `comment["link_id"]` is already a `t3_` fullname. Stripping the `t3_` prefix yields the bare post id used by `/comments/<id>.json`.
- `selftext` is always present and always a string. It is **not** guaranteed empty when `is_self == False` (4 of 16 link posts in the sample carried non-empty body text alongside their URL).
- The `User-Agent` shown in `ingest.py` does not trigger 429s at ~1 req/sec without authentication.

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env  # then edit DIMENSION_LABS_API_KEY
python ingest.py --dry-run   # print mapped events without POSTing
python ingest.py             # send events to the tracker
```

State is persisted in `state.json` next to the script. Delete it to re-seed.

## Scheduling on GitHub Actions

A workflow at `.github/workflows/ingest.yml` runs the ingest on a 15-minute cron, plus on-demand via the **Run workflow** button (`workflow_dispatch`).

Setup:

1. **Add the API key as a repo secret.**
   In the repo, go to **Settings → Secrets and variables → Actions → New repository secret** and create:
   - Name: `DIMENSION_LABS_API_KEY`
   - Value: your Dimension Labs Universal tracker API key.
2. **Confirm Actions is enabled** for the repo (Settings → Actions → General → Actions permissions).
3. **Trigger a manual run** to verify: **Actions** tab → **ingest** workflow → **Run workflow** → **Run workflow**.
4. After it succeeds, the cron will run every 15 minutes automatically.

If the secret is missing, the workflow falls back to `--dry-run` and emits an Actions warning so it doesn't silently no-op.

### State persistence

`state.json` is **not** kept on `main`. The workflow stores it on a dedicated orphan branch named `state`, which is created automatically on the first run. Every subsequent run:

1. Fetches `state` and restores `state.json` into the working tree.
2. Runs `ingest.py` (which updates `state.json` in place).
3. Commits the new `state.json` back to the `state` branch as `github-actions[bot]`.

This keeps `main`'s history clean while preserving an audit trail of state changes on the `state` branch. To inspect or reset state, browse or rewrite that branch directly.

### Concurrency

The workflow declares `concurrency: { group: ingest, cancel-in-progress: false }`, so overlapping schedules queue rather than race on `state.json`.

### Adjusting the cadence

Edit the `cron` line in `.github/workflows/ingest.yml`. GitHub-hosted cron can drift several minutes under load; for tighter real-time guarantees, run the script on your own host instead.

## Layout

- `ingest.py` — main script.
- `requirements.txt` — Python deps (`requests`).
- `.env.example` — environment variable template.
- `.github/workflows/ingest.yml` — scheduled GitHub Actions runner.
- `state.json` — created at runtime; tracks last-seen fullnames. On Actions, persisted to the `state` branch.
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

- Posts: `title + "\n\n" + selftext`. `selftext` is always a string on Reddit's JSON, so we use `post.get("selftext") or ""` defensively. **Note:** `selftext` is **not** guaranteed empty for link posts (`is_self == False`). Reddit lets a self-style body coexist with a URL, so a link post can carry meaningful body text. Downstream consumers should not use `is_self` as a proxy for "has body text"; trust the `text` field.
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

## Field assumptions (verified)

These were confirmed empirically against a live 100-post / 100-comment sample from `r/datascience`:

- `post["name"]` is the `t3_xxx` fullname directly. No reconstruction from `kind` + `id` is needed.
- `comment["link_id"]` is already a `t3_` fullname. Stripping the `t3_` prefix yields the bare post id used by `/comments/<id>.json`.
- `selftext` is always present and always a string. It is **not** guaranteed empty when `is_self == False` (4 of 16 link posts in the sample carried non-empty body text alongside their URL).
- The `User-Agent` shown in `ingest.py` does not trigger 429s at ~1 req/sec without authentication.

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

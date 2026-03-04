# Agent Reference

Technical reference for AI agents and developers working on this codebase.

## Architecture

- Single Python script (`sync_meetups.py`) syncs Meetup Pro Network events to Discourse topics
- Stateless — uses Discourse `external_id` for state tracking, no local database
- Runs in a container with a config volume at `/config/`
- Two modes: `--staging` (sandbox category, `staging-` prefix) and `--production` (production category, bare ID)

## Development

- Python 3.12+
- Dependencies: `pip install -r requirements.txt`
- Dev tools: `pip install black ruff mypy isort pytest`
- Config: `pyproject.toml` (line length 160, black profile)

### Running tests

```bash
pytest tests/ -v
```

### Code quality

```bash
black .
ruff check .
mypy .
isort .
```

### Running locally

```bash
cp config.yml.example config.yml
# Edit config.yml with credentials
python sync_meetups.py --config config.yml --staging --validate-config
python sync_meetups.py --config config.yml --staging
```

### Container

```bash
podman build --tag meetup-sync .
podman run --rm -v /srv/docker-config/meetup:/config:ro meetup-sync --staging
```

### Manual testing against live APIs

- Use `--staging` to write to the sandbox category
- Use `--validate-config` to verify credentials without syncing
- Use `--dump-events` to inspect raw Meetup API data
- Use `--log-level DEBUG` for verbose output
- Staging topics get `TESTING:` title prefix and `staging-{id}` external IDs

## Meetup GraphQL API

### Authentication

- JWT OAuth2 flow using RS256-signed JWTs
  - Private key read from PEM file (`meetup_private_key_path`)
  - Token endpoint: `https://secure.meetup.com/oauth2/access`
  - Token lifetime comes from the API `expires_in` field (3600s fallback default), refreshed with 60-second buffer
  - Token endpoint retries once on failure before exiting

### `eventsSearch` query

- Endpoint: `https://api.meetup.com/gql-ext`
- Fields used: `id`, `title`, `dateTime`, `duration`, `eventUrl`, `description`, `eventType`, `group { id name urlname timezone }`, `venue { name address city country }`, `networkEvent { id groupCount }`
- `status` filter type is `[String!]` (array of strings)
  - Inline string literals like `status: "UPCOMING"` work via GraphQL coercion
  - Variables must use array type: `$status: [String!]`
  - Unquoted enum form `status: UPCOMING` returns `ValidationError`
- Rate limit: 500 points per 60 seconds
- GraphQL queries retry up to 3 times on server errors (500+) with exponential backoff, and on HTTP 429 with a 60-second wait

### Data types and gotchas

- `duration` — ISO 8601 string (e.g. `"PT2H"`, `"PT1H30M"`, `"PT0S"`), not milliseconds. `parse_duration()` handles both formats
- `eventType` — known values: `ONLINE`, `PHYSICAL`. Online detection falls back to `venue_name.lower() == "online event"`
- `group` field — can be `null` (key present, value null). Use `node.get("group") or {}`, not `node.get("group", {})`
- `description` — Markdown with `\n` line breaks, `**bold**`, `[text](url)` links, `* item` lists. No HTML tags. Passed through to Discourse as-is
  - May contain unicode: emojis, international characters (`München`), smart quotes, em dashes

### Network events

- Created via Meetup's Network Event Scheduler
- Produce a separate event record per group, each with a **different** `event.id` but sharing the same `networkEvent.id` (a stable UUID)
- `networkEvent` field: nullable. When present provides `id` (UUID), `groupCount`, `rsvpCount`, `title`, `status`
- There is no `proNetwork.networkEvents` query — network events are only discoverable via the `networkEvent` field on individual `Event` objects
- **Currently skipped during sync** — network events are filtered out in `run_sync()` because Discourse does not support setting `external_id` on existing topics via the API

### Event deduplication

- Two dedup keys: `event.id` (standard) and `networkEvent.id` (network events with different IDs per group)
- Virtual group (`ansible-virtual-meetups`) is preferred when duplicates exist
- `external_id` uses `networkEvent.id` when present (stable UUID), falls back to `event.id` for standalone events

## Discourse API

### Endpoints used

- `GET /t/external_id/{id}.json` — look up topic by external ID
  - Requires `include_raw=1` query param to get Markdown source in `post_stream.posts[].raw`
  - Without it, only `cooked` (rendered HTML) is returned, causing false change detection
- `POST /posts.json` — create topic with `external_id`
  - `external_id` is set here at creation time. It is the **only** way to set it
  - Body: `{"title": ..., "raw": ..., "category": ..., "tags": ["meetup"], "external_id": ...}`
- `PUT /t/-/{topic_id}.json` — update topic title
- `PUT /posts/{post_id}.json` — update post body
  - Body: `{"post": {"raw": ..., "edit_reason": ...}}`

### `external_id` behaviour

- Available since Discourse 2.9.0-beta2
- **Immutable after creation** — `PUT /t/{id}.json` with `external_id` returns HTTP 200 but silently ignores the field
- Clearing requires Rails console: `Topic.find(id).update!(external_id: nil)`
- Production uses bare event IDs (e.g. `"313523351"` or `"07ef9599-..."`)
- Staging uses `staging-{id}` prefix to prevent namespace collisions

### Rate limiting

- Read and write methods retry once on HTTP 429, sleeping for `Retry-After` header value (default 10s)
- 1-second delay between events in the sync loop

### Error handling

- HTTP 422 on topic creation: logged with response body (common causes: duplicate `external_id`, duplicate title)
- Staging title prefix `TESTING:` avoids title collisions with production topics

### Update ordering

- Body must be updated **before** title — Discourse validates `[event]` BBCode times on title update. Stale end times cause HTTP 422 `"An event can't end before it starts."`

### BBCode

- `[event]` tag uses `minimal="true"` to hide Discourse RSVP buttons (RSVPs managed on Meetup)
- Double quotes in `event.title` and `event.event_url` are replaced with single quotes before interpolation into BBCode attributes to prevent attribute injection
- All topics tagged with `meetup`

## Configuration

- Single YAML file loaded via `yaml.safe_load()`
- See `config.yml.example` for full structure
- `--validate-config` checks:
  - Private key file exists and is valid RSA
  - Meetup token endpoint reachable and access token obtainable
  - Pro Network exists and has groups
  - Discourse URL reachable, API credentials valid, categories exist
  - `external_id` endpoint functional (expects 404 for nonexistent ID)
  - `lookahead_days` and `lookback_days` are non-negative integers
- **Cannot verify Discourse write permissions** — validation is read-only

## Container operation

- No persistent state needed (unlike the R version which required `/srv/docker-pins/`)
- Config volume only: `/config/` containing `config.yml` and `meetup_private_key.pem`
- Entrypoint: `python sync_meetups.py`
- Default CMD: `--config /config/config.yml`

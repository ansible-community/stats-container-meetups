# Project Plan

## Email Reporting

Reimplement `send_email.R` and `meetup_report.Rmd` functionality.

### Data collection

New `MeetupClient` methods needed:

- **`fetch_past_events(days)`** — query `eventsSearch` with
  `status: "PAST"` and date filter. Returns events from the last N days.
  Need two periods (e.g. last 30 days and 30-60 days) for trend comparison.
- **RSVP counts** — the `Event` type has no direct `rsvpCount` field.
  Options:
  - `rsvps(first: 0) { totalCount }` on each event (need to introspect
    `RsvpConnection` to confirm `totalCount` exists)
  - `NetworkEvent.rsvpCount` is available but only for network events
  - May need to introspect `RsvpConnection` type before implementing

### Report sections

| Section | Data source | Computation |
| --- | --- | --- |
| Top 15 groups by event count | Past events (90 days) | Group by `group.urlname`, count |
| RSVP trends | Past events (60 days) | RSVP totals per period, compare 0-30 vs 30-60 days |
| Activity summary with trend arrows | Past events (60 days) | Current vs previous period counts |
| Upcoming events table | `fetch_events()` (existing) | Filter to next 90 days |
| Recent events table | Past events (30 days) | Sort by date descending |
| Geographic totals | Past events (90 days) | Group by `venue.country` and `venue.city` |

### Email generation

- **HTML template** — Jinja2 template with inline CSS for email client compatibility
- **Charts** — matplotlib for bar charts and trend lines, embedded as base64 `<img>` tags
- **SMTP** — Python stdlib `smtplib` + `email.mime`, Gmail SMTP with app password (port 587, STARTTLS)

### CLI

- `--send-report` — generate and send the email report (can combine with `--dry-run` to preview without sending)

### New dependencies

- `jinja2` — HTML templating
- `matplotlib` — chart generation (~50MB, consider `plotly` if container image size matters)

### Open questions

- Does `RsvpConnection` have a `totalCount` field, or do we need to paginate through all RSVPs to count them?
- Should the report run as a separate container invocation (`--send-report`) or after every sync?
- What time period defines "recent" and "upcoming" for the tables?
- Are the trend arrows comparing month-over-month or week-over-week?

## Post-Event DM

Send a Discourse DM to user `gundalow` when an event completes:

- Ask for slides to be shared via the Forum Post
- Ask for Forum IDs for organizers and speakers for badge awards
- Use `POST /posts.json` with `archetype: "private_message"`, `target_recipients: "gundalow"`

## Network Event Sync

Network events are currently skipped during sync because Discourse does not
support setting `external_id` on existing topics via the API. To enable
network event sync:

- Ensure all network event topics are created by the sync script (not manually)
- Remove the network event filter in `run_sync()`
- Network event deduplication logic is already implemented in `deduplicate_events()`

## GitHub Actions for CI

Add GitHub Actions workflows for:

- Linting: `black --check`, `ruff check`, `isort --check`
- Type checking: `mypy`
- Tests: `pytest` with coverage reporting
- Markdown linting: `markdownlint`
- Triggered on push and pull requests

## Production Hardening

- **Exit code masks sync errors.** `main()` returns 0 even when individual
  events fail to sync. Consider returning 1 when `summary.errors > 0`.
- **Discourse write failure retry.** Non-429 write failures (e.g. transient
  500s) are not retried. Adding a single retry with backoff would reduce
  unnecessary error noise.

## Future Improvements

- **Fuzzy dedup for manually duplicated events.** Manually duplicated events
  (copied via "Duplicate event") have different IDs and no `networkEvent` link.
  Detecting these would require title/date similarity matching.
- **`--dump-events` for past events.** Currently `--dump-events` only fetches
  `UPCOMING` events. Supporting `--dump-events --status PAST` would aid
  debugging and historical analysis.
- **Recorded API response tests.** Use a VCR-style library (e.g. `vcrpy` or
  `responses`) to record live API responses and replay them in tests, closing
  the integration test gap without requiring live credentials.

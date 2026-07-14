# Meetup-to-Discourse Sync

Syncs Ansible community meetup events from the Meetup Pro Network to the
[Ansible Forum](https://forum.ansible.com) (Discourse).

## Features

- **Dynamic group discovery** from the Meetup Pro Network (no hardcoded group list)
- **Stateless operation** using Discourse `external_id` (no local state files needed)
- **Event deduplication** by event ID and network event ID, preferring the configured virtual group
- **JWT OAuth2 authentication** for unattended container operation
- **Single YAML config file** for all settings
- **Config validation** (`--validate-config`) to verify all credentials and endpoints
- **Schema introspection** tool (`introspect_schema.py`) for Meetup API discovery

## Quick Start

### 1. Configure

```bash
cp config.yml.example config.yml
# Edit config.yml with your Meetup and Discourse credentials
```

### 2. Run Locally

```bash
pip install -r requirements.txt
python sync_meetups.py --config config.yml --staging --validate-config
python sync_meetups.py --config config.yml --staging
```

### 3. Run in Container

```bash
podman build --tag meetup-sync .

# Staging run
podman run --rm -v /srv/docker-config/meetup:/config:ro meetup-sync --staging

# Production run with debug logging
podman run --rm -v /srv/docker-config/meetup:/config:ro \
    meetup-sync --production --log-level DEBUG
```

## Config Volume Layout

```text
/srv/docker-config/meetup/
├── config.yml
└── meetup_private_key.pem
```

## Staging and Production Modes

The tool requires exactly one of `--staging` or `--production` to prevent
`external_id` collisions between test and live runs.

| | Staging (`--staging`) | Production (`--production`) |
| --- | --- | --- |
| Category | `category_staging` | `category_production` |
| External ID | `staging-{id}` | `{id}` (bare) |
| Title prefix | `TESTING:` | (none) |

## CLI Options

| Option | Default | Description |
| --- | --- | --- |
| `--config` | `/config/config.yml` | Path to YAML config file |
| `--staging` | - | Use staging category with `staging-` prefix and `TESTING:` title prefix |
| `--production` | - | Use production category with bare external_id |
| `--validate-config` | off | Validate all config settings and credentials, then exit |
| `--dump-events` | off | Fetch and display all event fields from Meetup API, then exit |
| `--log-level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |

## How Stateless Sync Works

This tool requires no database or local state files. It uses the Discourse
[`external_id` feature](https://meta.discourse.org/t/what-is-the-new-external-id-feature-for-topics-used-for/218203)
(added in Discourse 2.9.0-beta2) to link Meetup events to forum topics.

On every sync run, the tool:

1. Fetches upcoming events from the Meetup GraphQL API
2. Deduplicates events by event ID and network event ID
3. For each event, calls `GET /t/external_id/{id}.json` on Discourse
4. If **not found** (HTTP 404) — creates a new topic with that `external_id`
5. If **found** (HTTP 200) — compares the current title and body, updates only if changed

This means the tool can be run repeatedly (idempotent), from any machine,
without worrying about state synchronization.

## Authentication

This tool uses JWT OAuth2 for Meetup API access. See the
[Meetup GraphQL authentication docs](https://www.meetup.com/graphql/authentication/)
for setup instructions. You need:

1. A Meetup OAuth consumer with a signing key
2. An RSA private key (PEM format)
3. Your Meetup member ID

## Schema Introspection

`introspect_schema.py` queries the Meetup GraphQL API schema to discover
types and fields. It reuses the authentication infrastructure from
`sync_meetups.py`.

```bash
# Introspect default types (ProNetwork, NetworkEvent, Event, etc.)
python introspect_schema.py --config config.yml

# Also run live queries to see network event data
python introspect_schema.py --config config.yml --all
```

## Development

```bash
pip install -r requirements.txt
pip install black ruff mypy isort pytest

# Run tests
pytest tests/ -v

# Code quality
black . && ruff check . && mypy . && isort .
```

See [AGENTS.md](AGENTS.md) for detailed API reference, data types, and
development notes.

## License

GNU General Public License v3 — see [LICENSE](LICENSE).

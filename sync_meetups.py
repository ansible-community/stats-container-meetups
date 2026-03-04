#!/usr/bin/env python3
"""Sync Ansible community meetup events from Meetup Pro Network to Discourse."""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import requests
import yaml

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Config:
    """Application configuration loaded from YAML."""

    meetup_client_key: str
    meetup_signing_key_id: str
    meetup_private_key_path: str
    meetup_member_id: str
    meetup_pro_network: str
    meetup_api_url: str
    meetup_token_url: str
    discourse_url: str
    discourse_category_staging: int
    discourse_category_production: int
    discourse_api_key: str
    discourse_api_user: str
    lookahead_days: int = 90
    lookback_days: int = 1
    virtual_group: str = "ansible-virtual-meetups"

    @classmethod
    def from_yaml(cls, path: str) -> Config:
        """Load and validate configuration from a YAML file."""
        config_path = Path(path)
        if not config_path.exists():
            log.error("Config file not found: %s", path)
            sys.exit(1)

        with open(config_path) as f:
            raw = yaml.safe_load(f)

        missing: list[str] = []
        events = raw.get("events") or {}

        field_map = {
            "meetup_client_key": ("meetup", "client_key"),
            "meetup_signing_key_id": ("meetup", "signing_key_id"),
            "meetup_private_key_path": ("meetup", "private_key_path"),
            "meetup_member_id": ("meetup", "member_id"),
            "meetup_pro_network": ("meetup", "pro_network"),
            "meetup_api_url": ("meetup", "api_url"),
            "meetup_token_url": ("meetup", "token_url"),
            "discourse_url": ("discourse", "url"),
            "discourse_category_staging": ("discourse", "category_staging"),
            "discourse_category_production": ("discourse", "category_production"),
            "discourse_api_key": ("discourse", "api_key"),
            "discourse_api_user": ("discourse", "api_user"),
        }

        kwargs: dict[str, Any] = {}
        for param, (section, key) in field_map.items():
            section_data = raw.get(section) or {}
            value = section_data.get(key)
            if value is None:
                missing.append(f"{section}.{key}")
            else:
                kwargs[param] = value

        if missing:
            log.error("Missing required config fields: %s", ", ".join(missing))
            sys.exit(1)

        kwargs["lookahead_days"] = events.get("lookahead_days", 90)
        kwargs["lookback_days"] = events.get("lookback_days", 1)
        kwargs["virtual_group"] = events.get("virtual_group", "ansible-virtual-meetups")

        return cls(**kwargs)


@dataclass
class MeetupGroup:
    """A meetup group from the Pro Network."""

    id: str
    name: str
    urlname: str


@dataclass
class MeetupEvent:
    """A meetup event from the Pro Network."""

    id: str
    title: str
    date_time: str
    duration: str | None
    event_url: str
    description: str
    group_name: str
    group_urlname: str
    event_type: str | None
    event_timezone: str | None
    venue_name: str | None
    venue_address: str | None
    venue_city: str | None
    venue_country: str | None
    network_event_id: str | None = None
    network_event_group_count: int | None = None


@dataclass
class SyncResult:
    """Result of syncing a single event to Discourse."""

    event_id: str
    action: str  # created, updated, skipped, error
    topic_url: str | None = None
    error: str | None = None


@dataclass
class SyncSummary:
    """Summary of sync operation."""

    groups_found: int = 0
    events_fetched: int = 0
    events_after_dedup: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    results: list[SyncResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Meetup Client
# ---------------------------------------------------------------------------


class MeetupClient:
    """Client for the Meetup GraphQL API with JWT OAuth2 authentication."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._session = requests.Session()
        self._access_token: str | None = None
        self._token_expires_at: float = 0
        self._private_key = self._load_private_key()

    def _load_private_key(self) -> str:
        """Load RSA private key from PEM file."""
        key_path = Path(self._config.meetup_private_key_path)
        if not key_path.exists():
            log.error("Private key file not found: %s", key_path)
            sys.exit(1)
        return key_path.read_text()

    def _generate_jwt(self) -> str:
        """Create a signed JWT for Meetup API authentication."""
        now = time.time()
        payload = {
            "sub": self._config.meetup_member_id,
            "iss": self._config.meetup_client_key,
            "aud": "api.meetup.com",
            "exp": int(now) + 120,
        }
        headers = {
            "kid": self._config.meetup_signing_key_id,
            "typ": "JWT",
            "alg": "RS256",
        }
        return jwt.encode(payload, self._private_key, algorithm="RS256", headers=headers)

    def _ensure_token(self) -> None:
        """Obtain or refresh the access token."""
        if self._access_token and time.time() < self._token_expires_at - 60:
            return

        signed_jwt = self._generate_jwt()
        resp = self._session.post(
            self._config.meetup_token_url,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": signed_jwt,
            },
        )

        if resp.status_code != 200:
            log.error("Meetup auth failed (HTTP %d): %s", resp.status_code, resp.text)
            # Retry once
            resp = self._session.post(
                self._config.meetup_token_url,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": self._generate_jwt(),
                },
            )
            if resp.status_code != 200:
                log.error("Meetup auth retry failed (HTTP %d): %s", resp.status_code, resp.text)
                sys.exit(1)

        token_data = resp.json()
        if "access_token" not in token_data:
            log.error("Token response missing 'access_token': %s", list(token_data.keys()))
            sys.exit(1)
        self._access_token = token_data["access_token"]
        self._token_expires_at = time.time() + token_data.get("expires_in", 3600)
        log.debug("Meetup access token obtained, expires in %ds", token_data.get("expires_in", 3600))

    def _graphql(self, query: str, variables: dict[str, Any] | None = None, retries: int = 3) -> dict[str, Any]:
        """Execute a GraphQL query with authentication and retry logic."""
        self._ensure_token()

        attempt = 0
        while attempt < retries:
            resp = self._session.post(
                self._config.meetup_api_url,
                json={"query": query, "variables": variables or {}},
                headers={"Authorization": f"Bearer {self._access_token}"},
            )

            if resp.status_code == 200:
                data: dict[str, Any] = resp.json()
                if "errors" in data:
                    log.warning("GraphQL errors: %s", data["errors"])
                return data

            if resp.status_code >= 500:
                wait = 2**attempt
                log.warning("GraphQL server error (HTTP %d), retrying in %ds...", resp.status_code, wait)
                time.sleep(wait)
                attempt += 1
                continue

            if resp.status_code == 429:
                wait = 60
                log.warning("GraphQL rate limited, waiting %ds...", wait)
                time.sleep(wait)
                attempt += 1
                continue

            log.error("GraphQL request failed (HTTP %d): %s", resp.status_code, resp.text)
            break

        log.error("GraphQL request failed after %d attempts", retries)
        return {}

    def fetch_groups(self) -> list[MeetupGroup]:
        """Fetch all groups from the Pro Network with pagination."""
        query = """
        query ($urlname: ID!, $cursor: String) {
          proNetwork(urlname: $urlname) {
            groupsSearch(input: { first: 200, after: $cursor, filter: {} }) {
              totalCount
              pageInfo { endCursor hasNextPage }
              edges { node { id name urlname } }
            }
          }
        }
        """
        groups: list[MeetupGroup] = []
        cursor: str | None = None

        while True:
            variables: dict[str, str | None] = {"urlname": self._config.meetup_pro_network, "cursor": cursor}
            data = self._graphql(query, variables)

            pro_network = data.get("data", {}).get("proNetwork")
            if not pro_network:
                log.error("No proNetwork data in response")
                break

            search = pro_network.get("groupsSearch", {})
            edges = search.get("edges", [])

            for edge in edges:
                node = edge.get("node", {})
                groups.append(MeetupGroup(id=node["id"], name=node["name"], urlname=node["urlname"]))

            page_info = search.get("pageInfo", {})
            if page_info.get("hasNextPage"):
                cursor = page_info["endCursor"]
            else:
                break

        log.info("Discovered %d groups from Pro Network '%s'", len(groups), self._config.meetup_pro_network)
        return groups

    def fetch_events(self) -> list[MeetupEvent]:
        """Fetch upcoming events from the Pro Network with pagination."""
        query = """
        query ($urlname: ID!, $cursor: String) {
          proNetwork(urlname: $urlname) {
            eventsSearch(input: { first: 200, after: $cursor, filter: { status: "UPCOMING" } }) {
              totalCount
              pageInfo { endCursor hasNextPage }
              edges { node {
                id title dateTime duration eventUrl description
                eventType
                group { id name urlname timezone }
                venue { name address city country }
                networkEvent { id groupCount }
              } }
            }
          }
        }
        """
        events: list[MeetupEvent] = []
        cursor: str | None = None

        while True:
            variables: dict[str, str | None] = {"urlname": self._config.meetup_pro_network, "cursor": cursor}
            data = self._graphql(query, variables)

            pro_network = data.get("data", {}).get("proNetwork")
            if not pro_network:
                log.error("No proNetwork data in response")
                break

            search = pro_network.get("eventsSearch", {})
            edges = search.get("edges", [])

            for edge in edges:
                node = edge.get("node", {})
                group = node.get("group") or {}
                venue = node.get("venue") or {}
                net_event = node.get("networkEvent") or {}

                events.append(
                    MeetupEvent(
                        id=node["id"],
                        title=node["title"],
                        date_time=node["dateTime"],
                        duration=node.get("duration"),
                        event_url=node["eventUrl"],
                        description=node.get("description", ""),
                        group_name=group.get("name", ""),
                        group_urlname=group.get("urlname", ""),
                        event_type=node.get("eventType"),
                        event_timezone=group.get("timezone"),
                        venue_name=venue.get("name"),
                        venue_address=venue.get("address"),
                        venue_city=venue.get("city"),
                        venue_country=venue.get("country"),
                        network_event_id=net_event.get("id"),
                        network_event_group_count=net_event.get("groupCount"),
                    )
                )

            page_info = search.get("pageInfo", {})
            if page_info.get("hasNextPage"):
                cursor = page_info["endCursor"]
            else:
                break

        log.info("Fetched %d events from Pro Network", len(events))
        return events

    def fetch_events_detailed(self) -> list[dict[str, Any]]:
        """Fetch upcoming events with all available fields for inspection."""
        query = """
        query ($urlname: ID!, $cursor: String) {
          proNetwork(urlname: $urlname) {
            eventsSearch(input: { first: 200, after: $cursor, filter: { status: "UPCOMING" } }) {
              totalCount
              pageInfo { endCursor hasNextPage }
              edges { node {
                id title dateTime endTime duration eventUrl description
                status eventType
                maxTickets guestsAllowed numberOfAllowedGuests
                group { id name urlname timezone }
                venue { name address city state country lat }
                eventHosts { name }
                featuredEventPhoto { baseUrl }
                series { description }
                rsvpSettings { rsvpOpenTime rsvpCloseTime }
                feeSettings { amount currency }
                networkEvent { id title groupCount rsvpCount status }
              } }
            }
          }
        }
        """
        nodes: list[dict[str, Any]] = []
        cursor: str | None = None

        while True:
            variables: dict[str, str | None] = {"urlname": self._config.meetup_pro_network, "cursor": cursor}
            data = self._graphql(query, variables)

            pro_network = data.get("data", {}).get("proNetwork")
            if not pro_network:
                log.error("No proNetwork data in response")
                break

            search = pro_network.get("eventsSearch", {})
            for edge in search.get("edges", []):
                nodes.append(edge.get("node", {}))

            page_info = search.get("pageInfo", {})
            if page_info.get("hasNextPage"):
                cursor = page_info["endCursor"]
            else:
                break

        log.info("Fetched %d detailed events from Pro Network", len(nodes))
        return nodes


# ---------------------------------------------------------------------------
# Discourse Client
# ---------------------------------------------------------------------------


class DiscourseClient:
    """Client for the Discourse API."""

    def __init__(self, config: Config, category: int) -> None:
        self._config = config
        self._category = category
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Api-Key": config.discourse_api_key,
                "Api-Username": config.discourse_api_user,
            }
        )
        self._base_url = config.discourse_url.rstrip("/")

    def lookup_topic(self, external_id: str) -> dict[str, Any] | None:
        """Look up a topic by external_id. Returns topic data or None if not found."""
        url = f"{self._base_url}/t/external_id/{external_id}.json"
        for _attempt in range(2):
            resp = self._session.get(url, params={"include_raw": "1"})
            if not self._handle_rate_limit(resp):
                break

        if resp.status_code == 200:
            result: dict[str, Any] = resp.json()
            return result
        if resp.status_code == 404:
            return None

        log.warning("Discourse lookup unexpected status %d for %s", resp.status_code, external_id)
        return None

    def _handle_rate_limit(self, resp: requests.Response) -> bool:
        """Wait if rate limited by Discourse. Returns True if rate limited (caller should retry)."""
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 10))
            log.warning("Discourse rate limited, waiting %ds...", retry_after)
            time.sleep(retry_after)
            return True
        return False

    def create_topic(self, title: str, raw: str, external_id: str) -> dict[str, Any] | None:
        """Create a new Discourse topic. Returns response data or None on failure."""
        for _attempt in range(2):
            resp = self._session.post(
                f"{self._base_url}/posts.json",
                json={
                    "title": title,
                    "raw": raw,
                    "category": self._category,
                    "tags": ["meetup"],
                    "external_id": external_id,
                },
            )
            if not self._handle_rate_limit(resp):
                break

        if resp.status_code == 422:
            log.warning("Discourse 422 creating topic: %s — %s", external_id, resp.text)
            return None

        if resp.status_code not in (200, 201):
            log.error("Discourse create failed (HTTP %d): %s", resp.status_code, resp.text)
            return None

        result: dict[str, Any] = resp.json()
        return result

    def update_topic_title(self, topic_id: int, title: str) -> bool:
        """Update a topic's title."""
        for _attempt in range(2):
            resp = self._session.put(
                f"{self._base_url}/t/-/{topic_id}.json",
                json={"title": title, "category": self._category},
            )
            if not self._handle_rate_limit(resp):
                break

        if resp.status_code != 200:
            log.error("Discourse title update failed (HTTP %d): %s", resp.status_code, resp.text)
            return False
        return True

    def update_post_body(self, post_id: int, raw: str) -> bool:
        """Update a post's body."""
        for _attempt in range(2):
            resp = self._session.put(
                f"{self._base_url}/posts/{post_id}.json",
                json={"post": {"raw": raw, "edit_reason": "Meetup event updated"}},
            )
            if not self._handle_rate_limit(resp):
                break

        if resp.status_code != 200:
            log.error("Discourse body update failed (HTTP %d): %s", resp.status_code, resp.text)
            return False
        return True


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def build_post_title(group_urlname: str, event_title: str, date_time: str, prefix: str = "") -> str:
    """Build the Discourse topic title from event data."""
    dt = parse_event_datetime(date_time)
    date_str = dt.strftime("%Y-%m-%d")
    return f"{prefix}{group_urlname}: {event_title} [{date_str}]"


def build_post_body(event: MeetupEvent) -> str:
    """Build the Discourse post body with [event] BBCode."""
    dt = parse_event_datetime(event.date_time)
    start_iso = event.date_time

    end_iso = start_iso
    duration_td = parse_duration(event.duration)
    if duration_td:
        end_dt = dt + duration_td
        end_iso = end_dt.isoformat(timespec="milliseconds")

    tz_attr = f' timezone="{event.event_timezone}"' if event.event_timezone else ""

    safe_title = event.title.replace('"', "'")
    safe_url = event.event_url.replace('"', "'")

    body_parts = [
        f'[event start="{start_iso}" end="{end_iso}"{tz_attr} status="public" minimal="true"' f' name="{safe_title}" url="{safe_url}"]',
        "[/event]",
        "",
        f"## {event.title}",
        "",
    ]

    description_md = event.description.strip()
    if description_md:
        body_parts.append(description_md)
        body_parts.append("")

    if event.event_type == "ONLINE" or (event.venue_name and event.venue_name.lower() == "online event"):
        body_parts.append("**Venue:** Online Event")
    elif event.venue_name:
        venue_parts = [event.venue_name]
        if event.venue_address:
            venue_parts.append(event.venue_address)
        if event.venue_city:
            venue_parts.append(event.venue_city)
        if event.venue_country:
            venue_parts.append(event.venue_country)
        body_parts.append(f"**Venue:** {', '.join(venue_parts)}")

    body_parts.append("")
    body_parts.append(f"**RSVP on Meetup:** [View event on Meetup]({event.event_url})")

    return "\n".join(body_parts)


def parse_duration(duration: str | None) -> timedelta | None:
    """Parse an ISO 8601 duration or milliseconds string into a timedelta."""
    if not duration:
        return None
    match = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$", duration)
    if match:
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        seconds = float(match.group(3) or 0)
        return timedelta(hours=hours, minutes=minutes, seconds=seconds)
    try:
        ms = int(duration)
        if ms > 0:
            return timedelta(milliseconds=ms)
    except ValueError:
        pass
    return None


def parse_event_datetime(date_time_str: str) -> datetime:
    """Parse an ISO 8601 datetime string from the Meetup API."""
    try:
        return datetime.fromisoformat(date_time_str)
    except ValueError:
        return datetime.strptime(date_time_str, "%Y-%m-%dT%H:%M:%S.%f%z")


def filter_events(events: list[MeetupEvent], lookback_days: int, lookahead_days: int) -> list[MeetupEvent]:
    """Filter events by date range."""
    now = datetime.now(UTC)
    start = now - timedelta(days=lookback_days)
    end = now + timedelta(days=lookahead_days)

    filtered = []
    for event in events:
        dt = parse_event_datetime(event.date_time)
        dt_utc = dt.astimezone(UTC)
        if start <= dt_utc <= end:
            filtered.append(event)

    log.info("Filtered to %d events (from %d) within date range", len(filtered), len(events))
    return filtered


def deduplicate_events(events: list[MeetupEvent], virtual_group: str) -> list[MeetupEvent]:
    """Deduplicate events by event ID and network event ID, preferring virtual_group.

    Network events (created via Meetup's Network Event Scheduler) produce a
    separate event record per group, each with a different event ID but sharing
    the same ``network_event_id``.  This function deduplicates both cases:

    1. Same ``event.id`` across groups (standard dedup).
    2. Different ``event.id`` but same ``network_event_id`` (network event dedup).

    In both cases the copy from ``virtual_group`` is preferred when present.
    """
    by_id: dict[str, MeetupEvent] = {}
    by_network: dict[str, MeetupEvent] = {}

    for event in events:
        if event.id not in by_id or event.group_urlname == virtual_group:
            by_id[event.id] = event
        if event.network_event_id and (event.network_event_id not in by_network or event.group_urlname == virtual_group):
            by_network[event.network_event_id] = event

    result = list(by_network.values())
    for event in by_id.values():
        if not event.network_event_id:
            result.append(event)

    deduped = len(events) - len(result)
    if deduped:
        log.info("Deduplicated %d events (from %d to %d)", deduped, len(events), len(result))
    return result


def dump_events_table(events: list[dict[str, Any]]) -> None:
    """Print a table of all event fields for inspection."""
    if not events:
        log.info("No events to display")
        return

    rows: list[dict[str, str]] = []
    for node in events:
        group = node.get("group") or {}
        venue = node.get("venue") or {}
        hosts = node.get("eventHosts") or []
        photo = node.get("featuredEventPhoto") or {}
        fee = node.get("feeSettings") or {}
        rsvp_settings = node.get("rsvpSettings") or {}
        series = node.get("series") or {}
        net_event = node.get("networkEvent") or {}

        rows.append(
            {
                "id": node.get("id", ""),
                "group": group.get("urlname", ""),
                "title": node.get("title", ""),
                "status": node.get("status", ""),
                "event_type": node.get("eventType", ""),
                "date_time": node.get("dateTime", ""),
                "end_time": node.get("endTime", ""),
                "duration": node.get("duration", ""),
                "timezone": group.get("timezone", ""),
                "max_tickets": str(node.get("maxTickets", "")),
                "guests_allowed": str(node.get("guestsAllowed", "")),
                "max_guests": str(node.get("numberOfAllowedGuests", "")),
                "venue_name": venue.get("name", ""),
                "venue_city": venue.get("city", ""),
                "venue_country": venue.get("country", ""),
                "venue_lat": str(venue.get("lat", "")),
                "hosts": ", ".join(h.get("name", "") for h in hosts),
                "photo_url": photo.get("baseUrl", ""),
                "fee": f"{fee.get('amount', '')}{fee.get('currency', '')}" if fee.get("amount") else "",
                "rsvp_open": rsvp_settings.get("rsvpOpenTime", ""),
                "rsvp_close": rsvp_settings.get("rsvpCloseTime", ""),
                "series": series.get("description", ""),
                "network_event_id": net_event.get("id", ""),
                "network_event_title": net_event.get("title", ""),
                "network_event_groups": str(net_event.get("groupCount", "")),
                "network_event_rsvps": str(net_event.get("rsvpCount", "")),
                "event_url": node.get("eventUrl", ""),
                "description": (node.get("description", "") or "")[:80],
            }
        )

    fields = list(rows[0].keys())
    label_width = max(len(f) for f in fields)

    for i, row in enumerate(rows):
        if i > 0:
            print()
        print(f"--- Event {i + 1}/{len(rows)} ---")
        for col in fields:
            print(f"  {col.ljust(label_width)}  {row.get(col, '')}")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def validate_config(config: Config) -> bool:
    """Validate all config settings and credentials, logging PASS/FAIL per check.

    Returns True if all checks pass, False otherwise.
    """
    all_passed = True
    meetup_token: str | None = None

    # Check 1: Private key file exists
    key_path = Path(config.meetup_private_key_path)
    if key_path.exists():
        log.info("[PASS] Private key file exists: %s", key_path)
    else:
        log.error("[FAIL] Private key file exists: file not found: %s", key_path)
        all_passed = False
        log.warning("[SKIP] Private key is valid RSA (depends on: private key file exists)")
        log.warning("[SKIP] Meetup token endpoint reachable (depends on: private key is valid RSA)")
        log.warning("[SKIP] Meetup access token obtained (depends on: token endpoint reachable)")
        log.warning("[SKIP] Meetup Pro Network exists (depends on: access token obtained)")
        # Continue with Discourse checks (independent of Meetup)
        _validate_discourse_checks(config, all_passed=False)
        _validate_event_settings(config, all_passed=False)
        return False

    # Check 2: Private key is valid RSA
    private_key = key_path.read_text()
    try:
        jwt.encode({"test": True}, private_key, algorithm="RS256")
        log.info("[PASS] Private key is valid RSA")
    except (ValueError, jwt.exceptions.InvalidKeyError) as exc:
        log.error("[FAIL] Private key is valid RSA: %s", exc)
        all_passed = False
        log.warning("[SKIP] Meetup token endpoint reachable (depends on: private key is valid RSA)")
        log.warning("[SKIP] Meetup access token obtained (depends on: token endpoint reachable)")
        log.warning("[SKIP] Meetup Pro Network exists (depends on: access token obtained)")
        all_passed = _validate_discourse_checks(config, all_passed=all_passed)
        all_passed = _validate_event_settings(config, all_passed=all_passed)
        return all_passed

    # Check 3 & 4: Meetup token endpoint reachable and access token obtained
    try:
        now = time.time()
        payload = {
            "sub": config.meetup_member_id,
            "iss": config.meetup_client_key,
            "aud": "api.meetup.com",
            "exp": int(now) + 120,
        }
        headers = {"kid": config.meetup_signing_key_id, "typ": "JWT", "alg": "RS256"}
        signed_jwt = jwt.encode(payload, private_key, algorithm="RS256", headers=headers)

        session = requests.Session()
        resp = session.post(
            config.meetup_token_url,
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": signed_jwt},
        )

        if resp.status_code == 200:
            log.info("[PASS] Meetup token endpoint reachable")
            token_data = resp.json()
            if "access_token" in token_data:
                meetup_token = token_data["access_token"]
                log.info("[PASS] Meetup access token obtained")
            else:
                log.error("[FAIL] Meetup access token obtained: 'access_token' not in response")
                all_passed = False
                log.warning("[SKIP] Meetup Pro Network exists (depends on: access token obtained)")
        else:
            log.error("[FAIL] Meetup token endpoint reachable: HTTP %d: %s", resp.status_code, resp.text[:200])
            all_passed = False
            log.warning("[SKIP] Meetup access token obtained (depends on: token endpoint reachable)")
            log.warning("[SKIP] Meetup Pro Network exists (depends on: access token obtained)")
    except requests.RequestException as exc:
        log.error("[FAIL] Meetup token endpoint reachable: %s", exc)
        all_passed = False
        log.warning("[SKIP] Meetup access token obtained (depends on: token endpoint reachable)")
        log.warning("[SKIP] Meetup Pro Network exists (depends on: access token obtained)")

    # Check 5: Meetup Pro Network exists
    if meetup_token:
        try:
            query = """
            query ($urlname: ID!) {
              proNetwork(urlname: $urlname) {
                groupsSearch(input: { first: 1, filter: {} }) {
                  totalCount
                }
              }
            }
            """
            session = requests.Session()
            resp = session.post(
                config.meetup_api_url,
                json={"query": query, "variables": {"urlname": config.meetup_pro_network}},
                headers={"Authorization": f"Bearer {meetup_token}"},
            )
            if resp.status_code == 200:
                data = resp.json()
                pro_network = data.get("data", {}).get("proNetwork")
                if pro_network:
                    total = pro_network.get("groupsSearch", {}).get("totalCount", 0)
                    if total > 0:
                        log.info("[PASS] Meetup Pro Network exists (%d groups)", total)
                    else:
                        log.error("[FAIL] Meetup Pro Network exists: 0 groups found for '%s'", config.meetup_pro_network)
                        all_passed = False
                else:
                    log.error("[FAIL] Meetup Pro Network exists: proNetwork '%s' not found in response", config.meetup_pro_network)
                    all_passed = False
            else:
                log.error("[FAIL] Meetup Pro Network exists: HTTP %d", resp.status_code)
                all_passed = False
        except requests.RequestException as exc:
            log.error("[FAIL] Meetup Pro Network exists: %s", exc)
            all_passed = False

    # Checks 6-9: Discourse
    all_passed = _validate_discourse_checks(config, all_passed=all_passed)

    # Checks 10-11: Event settings
    all_passed = _validate_event_settings(config, all_passed=all_passed)

    return all_passed


def _validate_discourse_checks(config: Config, *, all_passed: bool) -> bool:
    """Run Discourse validation checks (checks 6-9). Returns updated all_passed."""
    base_url = config.discourse_url.rstrip("/")

    # Check 6: Discourse URL reachable (no credentials, public endpoint)
    session = requests.Session()
    try:
        resp = session.get(f"{base_url}/site.json")
        if resp.status_code == 200:
            log.info("[PASS] Discourse URL reachable")
        else:
            log.error("[FAIL] Discourse URL reachable: HTTP %d", resp.status_code)
            all_passed = False
            log.warning("[SKIP] Discourse API credentials and category (depends on: Discourse URL reachable)")
            log.warning("[SKIP] Discourse external_id endpoint (depends on: Discourse URL reachable)")
            return all_passed
    except requests.RequestException as exc:
        log.error("[FAIL] Discourse URL reachable: %s", exc)
        all_passed = False
        log.warning("[SKIP] Discourse API credentials and category (depends on: Discourse URL reachable)")
        log.warning("[SKIP] Discourse external_id endpoint (depends on: Discourse URL reachable)")
        return all_passed

    # Check 7+8: Discourse API credentials valid AND categories exist
    # Uses the category endpoint to validate both at once: a 403 means bad
    # credentials, a 404 means valid credentials but wrong category ID, and
    # 200 means both are valid.
    api_headers = {"Api-Key": config.discourse_api_key, "Api-Username": config.discourse_api_user}
    credentials_valid = False

    # First check credentials with the staging category
    try:
        resp = session.get(f"{base_url}/c/{config.discourse_category_staging}/show.json", headers=api_headers)
        if resp.status_code == 200:
            log.info("[PASS] Discourse API credentials valid")
            log.info("[PASS] Discourse staging category exists (ID: %d)", config.discourse_category_staging)
            credentials_valid = True
        elif resp.status_code == 403:
            log.error("[FAIL] Discourse API credentials valid: HTTP 403 (invalid API key or user)")
            all_passed = False
        elif resp.status_code == 404:
            log.info("[PASS] Discourse API credentials valid")
            log.error("[FAIL] Discourse staging category exists: category ID %d not found", config.discourse_category_staging)
            all_passed = False
            credentials_valid = True
        else:
            log.error(
                "[FAIL] Discourse API credentials or staging category: HTTP %d for category ID %d",
                resp.status_code,
                config.discourse_category_staging,
            )
            all_passed = False
    except requests.RequestException as exc:
        log.error("[FAIL] Discourse API credentials or staging category: %s", exc)
        all_passed = False

    # Check production category (only if credentials are valid)
    if credentials_valid:
        try:
            resp = session.get(f"{base_url}/c/{config.discourse_category_production}/show.json", headers=api_headers)
            if resp.status_code == 200:
                log.info("[PASS] Discourse production category exists (ID: %d)", config.discourse_category_production)
            elif resp.status_code == 404:
                log.error("[FAIL] Discourse production category exists: category ID %d not found", config.discourse_category_production)
                all_passed = False
            else:
                log.error(
                    "[FAIL] Discourse production category: HTTP %d for category ID %d",
                    resp.status_code,
                    config.discourse_category_production,
                )
                all_passed = False
        except requests.RequestException as exc:
            log.error("[FAIL] Discourse production category: %s", exc)
            all_passed = False

    if not credentials_valid:
        log.warning("[SKIP] Discourse external_id endpoint (depends on: Discourse API credentials valid)")
        return all_passed

    # Check 9: Discourse external_id endpoint
    try:
        resp = session.get(f"{base_url}/t/external_id/validate-test.json", headers=api_headers)
        if resp.status_code == 404:
            log.info("[PASS] Discourse external_id endpoint (404 = endpoint exists, topic not found)")
        elif resp.status_code == 403:
            log.error("[FAIL] Discourse external_id endpoint: HTTP 403 (insufficient permissions)")
            all_passed = False
        else:
            log.error("[FAIL] Discourse external_id endpoint: unexpected HTTP %d (expected 404)", resp.status_code)
            all_passed = False
    except requests.RequestException as exc:
        log.error("[FAIL] Discourse external_id endpoint: %s", exc)
        all_passed = False

    return all_passed


def _validate_event_settings(config: Config, *, all_passed: bool) -> bool:
    """Run event settings validation checks (checks 10-11). Returns updated all_passed."""
    # Check 10: lookahead_days valid
    if isinstance(config.lookahead_days, int) and config.lookahead_days > 0:
        log.info("[PASS] events.lookahead_days valid (%d)", config.lookahead_days)
    else:
        log.error("[FAIL] events.lookahead_days valid: must be a positive integer, got %r", config.lookahead_days)
        all_passed = False

    # Check 11: lookback_days valid
    if isinstance(config.lookback_days, int) and config.lookback_days >= 0:
        log.info("[PASS] events.lookback_days valid (%d)", config.lookback_days)
    else:
        log.error("[FAIL] events.lookback_days valid: must be a non-negative integer, got %r", config.lookback_days)
        all_passed = False

    return all_passed


def sync_event(event: MeetupEvent, discourse: DiscourseClient, external_id_prefix: str = "", title_prefix: str = "") -> SyncResult:
    """Sync a single event to Discourse."""
    # Network events have a different event.id per group but share a stable
    # network_event_id.  Use that when available so the external_id is
    # deterministic regardless of which group copy wins deduplication.
    event_key = event.network_event_id if event.network_event_id else event.id
    external_id = f"{external_id_prefix}-{event_key}" if external_id_prefix else str(event_key)
    title = build_post_title(event.group_urlname, event.title, event.date_time, prefix=title_prefix)
    body = build_post_body(event)

    existing = discourse.lookup_topic(external_id)

    if existing is None:
        result = discourse.create_topic(title, body, external_id)
        if result is None:
            return SyncResult(event_id=event.id, action="error", error="Failed to create topic")
        new_topic_id = result.get("topic_id", 0)
        topic_url = f"{discourse._base_url}/t/{new_topic_id}" if new_topic_id else None
        log.info("[NEW] %s (topic %d) %s", external_id, new_topic_id, title)
        return SyncResult(event_id=event.id, action="created", topic_url=topic_url)

    topic_id: int = existing.get("id", 0)
    post_stream = existing.get("post_stream", {})
    posts = post_stream.get("posts", [])
    first_post_id: int | None = posts[0].get("id") if posts else None
    existing_title = existing.get("title", "")
    existing_body = posts[0].get("raw", "") if posts else ""

    # Discourse auto-capitalizes the first letter of titles, so compare
    # case-insensitively to avoid an update loop on groups like "ansible-munchen".
    title_changed = title.casefold() != existing_title.casefold()
    body_changed = body != existing_body

    if not title_changed and not body_changed:
        log.info("[NO CHANGE] %s (topic %d) %s", external_id, topic_id, title)
        return SyncResult(event_id=event.id, action="skipped", topic_url=f"{discourse._base_url}/t/{topic_id}")

    changes = []
    if title_changed:
        changes.append("title")
    if body_changed:
        changes.append("body")

    update_ok = True
    if body_changed and first_post_id is not None and not discourse.update_post_body(first_post_id, body):
        update_ok = False
    if title_changed and not discourse.update_topic_title(topic_id, title):
        update_ok = False

    if update_ok:
        log.info("[UPDATED %s] %s (topic %d) %s", "+".join(changes), external_id, topic_id, title)
        return SyncResult(event_id=event.id, action="updated", topic_url=f"{discourse._base_url}/t/{topic_id}")

    log.error("[UPDATE FAILED %s] %s (topic %d) %s", "+".join(changes), external_id, topic_id, title)
    return SyncResult(event_id=event.id, action="error", error="Update failed")


def run_sync(config: Config, category: int, external_id_prefix: str = "", title_prefix: str = "") -> SyncSummary:
    """Run the full sync workflow."""
    summary = SyncSummary()

    meetup = MeetupClient(config)
    discourse = DiscourseClient(config, category=category)

    groups = meetup.fetch_groups()
    summary.groups_found = len(groups)

    events = meetup.fetch_events()
    summary.events_fetched = len(events)

    events = filter_events(events, config.lookback_days, config.lookahead_days)
    events = deduplicate_events(events, config.virtual_group)

    network_count = sum(1 for e in events if e.network_event_id)
    if network_count:
        events = [e for e in events if not e.network_event_id]
        log.info("Skipped %d network events (not yet supported)", network_count)

    summary.events_after_dedup = len(events)

    for event in events:
        try:
            result = sync_event(event, discourse, external_id_prefix=external_id_prefix, title_prefix=title_prefix)
        except requests.RequestException:
            log.exception("Failed to sync event %s", event.id)
            result = SyncResult(event_id=event.id, action="error", error="Unexpected error")

        summary.results.append(result)
        if result.action == "created":
            summary.created += 1
        elif result.action == "updated":
            summary.updated += 1
        elif result.action == "skipped":
            summary.skipped += 1
        elif result.action == "error":
            summary.errors += 1

        # Rate limiting: ~1 request per second
        time.sleep(1)

    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Sync Meetup events to Discourse")
    parser.add_argument("--config", default="/config/config.yml", help="Path to YAML config file (default: /config/config.yml)")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staging", action="store_true", help="Use staging category with staging- prefix and TESTING: title prefix")
    mode.add_argument("--production", action="store_true", help="Use production category with bare external_id")
    parser.add_argument("--validate-config", action="store_true", help="Validate all config settings and credentials, then exit")
    parser.add_argument("--dump-events", action="store_true", help="Fetch and display all event fields from Meetup API, then exit")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging level (default: INFO)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Main entry point."""
    args = parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    config = Config.from_yaml(args.config)

    if args.production:
        category = config.discourse_category_production
        external_id_prefix = ""
        title_prefix = ""
        mode_label = "PRODUCTION"
    else:
        category = config.discourse_category_staging
        external_id_prefix = "staging"
        title_prefix = "TESTING: "
        mode_label = "STAGING"

    if args.validate_config:
        log.info("Validating configuration...")
        valid = validate_config(config)
        if not valid:
            log.error("Configuration validation failed")
            return 1
        log.info("All configuration checks passed")
        return 0

    if args.dump_events:
        meetup = MeetupClient(config)
        events = meetup.fetch_events_detailed()
        dump_events_table(events)
        return 0

    log.info("Mode: %s (category=%d, prefix=%s)", mode_label, category, external_id_prefix or "(none)")

    summary = run_sync(config, category=category, external_id_prefix=external_id_prefix, title_prefix=title_prefix)

    log.info(
        "Sync complete: groups=%d, events_fetched=%d, after_dedup=%d, created=%d, updated=%d, skipped=%d, errors=%d",
        summary.groups_found,
        summary.events_fetched,
        summary.events_after_dedup,
        summary.created,
        summary.updated,
        summary.skipped,
        summary.errors,
    )

    if summary.events_fetched == 0 and summary.groups_found == 0:
        log.error("Critical failure: no groups or events found")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

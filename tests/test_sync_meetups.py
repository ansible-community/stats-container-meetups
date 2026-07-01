"""Unit tests for sync_meetups."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import requests

from sync_meetups import (
    Config,
    DiscourseClient,
    MeetupClient,
    MeetupEvent,
    SyncResult,
    build_post_body,
    build_post_title,
    deduplicate_events,
    dump_events_table,
    filter_events,
    parse_args,
    parse_duration,
    parse_event_datetime,
    run_sync,
    sync_event,
    validate_config,
)

# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestConfig:
    def test_from_yaml_valid(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yml"
        key_file = tmp_path / "key.pem"
        key_file.write_text("fake-key")
        config_file.write_text(f"""
meetup:
  client_key: "ck"
  signing_key_id: "ski"
  private_key_path: "{key_file}"
  member_id: "123"
  pro_network: "ansible"
  api_url: "https://api.meetup.com/gql-ext"
  token_url: "https://secure.meetup.com/oauth2/access"
discourse:
  url: "https://forum.example.com"
  category_staging: 42
  category_production: 5
  api_key: "dk"
  api_user: "Bot"
events:
  lookahead_days: 60
  lookback_days: 2
  virtual_group: "my-virtual"
""")
        config = Config.from_yaml(str(config_file))
        assert config.meetup_client_key == "ck"
        assert config.discourse_category_staging == 42
        assert config.discourse_category_production == 5
        assert config.lookahead_days == 60
        assert config.lookback_days == 2
        assert config.virtual_group == "my-virtual"

    def test_from_yaml_missing_fields(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yml"
        config_file.write_text("meetup:\n  client_key: 'ck'\n")
        with pytest.raises(SystemExit):
            Config.from_yaml(str(config_file))

    def test_from_yaml_file_not_found(self) -> None:
        with pytest.raises(SystemExit):
            Config.from_yaml("/nonexistent/config.yml")

    def test_from_yaml_none_sections(self, tmp_path: Path) -> None:
        """YAML sections with no value (None) should not crash with AttributeError."""
        config_file = tmp_path / "config.yml"
        key_file = tmp_path / "key.pem"
        key_file.write_text("fake-key")
        config_file.write_text(f"""
meetup:
  client_key: "ck"
  signing_key_id: "ski"
  private_key_path: "{key_file}"
  member_id: "123"
  pro_network: "ansible"
  api_url: "https://api.meetup.com/gql-ext"
  token_url: "https://secure.meetup.com/oauth2/access"
discourse:
  url: "https://forum.example.com"
  category_staging: 42
  category_production: 5
  api_key: "dk"
  api_user: "Bot"
events:
""")
        config = Config.from_yaml(str(config_file))
        assert config.lookahead_days == 90
        assert config.lookback_days == 1
        assert config.virtual_group == "ansible-virtual-meetups"

    def test_defaults(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yml"
        key_file = tmp_path / "key.pem"
        key_file.write_text("fake-key")
        config_file.write_text(f"""
meetup:
  client_key: "ck"
  signing_key_id: "ski"
  private_key_path: "{key_file}"
  member_id: "123"
  pro_network: "ansible"
  api_url: "https://api.meetup.com/gql-ext"
  token_url: "https://secure.meetup.com/oauth2/access"
discourse:
  url: "https://forum.example.com"
  category_staging: 42
  category_production: 5
  api_key: "dk"
  api_user: "Bot"
""")
        config = Config.from_yaml(str(config_file))
        assert config.lookahead_days == 90
        assert config.lookback_days == 1
        assert config.virtual_group == "ansible-virtual-meetups"


# ---------------------------------------------------------------------------
# Post builder tests
# ---------------------------------------------------------------------------


class TestBuildPostTitle:
    def test_basic_title(self, sample_event: MeetupEvent) -> None:
        title = build_post_title(sample_event.group_urlname, sample_event.title, sample_event.date_time)
        assert title == "ansible-london: Getting Started with Ansible Automation [2026-03-15]"

    def test_different_date(self) -> None:
        title = build_post_title("ansible-nyc", "Test Event", "2026-12-25T10:00:00.000+00:00")
        assert title == "ansible-nyc: Test Event [2026-12-25]"

    def test_emoji_in_title(self) -> None:
        title = build_post_title("manchester-beer-and-pubs", "Saturday social \U0001f60a\U0001f37a\U0001f60a", "2026-03-01T14:00:00.000+00:00")
        assert title == "manchester-beer-and-pubs: Saturday social \U0001f60a\U0001f37a\U0001f60a [2026-03-01]"

    def test_unicode_in_title(self) -> None:
        title = build_post_title("ansible-munchen", "M\u00fcnchen \u2014 Gis\u00e8le Tennant", "2026-03-01T18:00:00.000+00:00")
        assert title == "ansible-munchen: M\u00fcnchen \u2014 Gis\u00e8le Tennant [2026-03-01]"


class TestBuildPostBody:
    def test_physical_event(self, sample_event: MeetupEvent) -> None:
        body = build_post_body(sample_event)
        assert '[event start="2026-03-15T18:00:00.000+00:00"' in body
        assert 'timezone="Europe/London"' in body
        assert 'status="public"' in body
        assert 'minimal="true"' in body
        assert "[/event]" in body
        assert "## Getting Started with Ansible Automation" in body
        assert "**Venue:** WeWork Moorgate" in body
        assert "**RSVP on Meetup:**" in body

    def test_online_event(self, online_event: MeetupEvent) -> None:
        body = build_post_body(online_event)
        assert "**Venue:** Online Event" in body

    def test_emoji_title_in_heading_and_bbcode(self) -> None:
        event = MeetupEvent(
            id="1",
            title="Saturday social \U0001f60a\U0001f37a\U0001f60a",
            date_time="2026-03-01T14:00:00.000+00:00",
            duration="PT4H",
            event_url="https://example.com",
            description="Come and join us for a few drinks.",
            group_name="Manchester Beer",
            group_urlname="manchester-beer-and-pubs",
            event_type="PHYSICAL",
            event_timezone="Europe/London",
            venue_name="The Waterhouse",
            venue_address=None,
            venue_city="Manchester",
            venue_country="gb",
        )
        body = build_post_body(event)
        assert "## Saturday social \U0001f60a\U0001f37a\U0001f60a" in body
        assert 'name="Saturday social \U0001f60a\U0001f37a\U0001f60a"' in body

    def test_title_with_hash_and_ampersand(self) -> None:
        event = MeetupEvent(
            id="1",
            title="ACP60 #628 - Tempering & Disruptions (Conflict)",
            date_time="2026-03-12T18:00:00.000+00:00",
            duration="PT2H",
            event_url="https://example.com",
            description="Event description",
            group_name="Test Group",
            group_urlname="test-group",
            event_type="PHYSICAL",
            event_timezone="UTC",
            venue_name=None,
            venue_address=None,
            venue_city=None,
            venue_country=None,
        )
        body = build_post_body(event)
        assert "## ACP60 #628 - Tempering & Disruptions (Conflict)" in body
        assert 'name="ACP60 #628 - Tempering & Disruptions (Conflict)"' in body

    def test_description_newlines_preserved(self) -> None:
        event = MeetupEvent(
            id="1",
            title="Test",
            date_time="2026-03-12T18:00:00.000+00:00",
            duration=None,
            event_url="https://example.com",
            description="**Speaker 1**\n**Topic: Ansible**\n\nAbout the speaker.\nMore details here.",
            group_name="Test Group",
            group_urlname="test-group",
            event_type="PHYSICAL",
            event_timezone="UTC",
            venue_name=None,
            venue_address=None,
            venue_city=None,
            venue_country=None,
        )
        body = build_post_body(event)
        assert "**Speaker 1**\n**Topic: Ansible**" in body
        assert "\n\nAbout the speaker." in body

    def test_event_bbcode_has_newline(self, sample_event: MeetupEvent) -> None:
        body = build_post_body(sample_event)
        lines = body.split("\n")
        event_start_idx = next(i for i, line in enumerate(lines) if line.startswith("[event"))
        event_end_idx = next(i for i, line in enumerate(lines) if line == "[/event]")
        assert event_end_idx == event_start_idx + 1

    def test_iso_duration_end_time(self) -> None:
        event = MeetupEvent(
            id="1",
            title="Test",
            date_time="2026-03-12T18:00:00.000+00:00",
            duration="PT2H",
            event_url="https://example.com",
            description="",
            group_name="Test Group",
            group_urlname="test-group",
            event_type="PHYSICAL",
            event_timezone="UTC",
            venue_name=None,
            venue_address=None,
            venue_city=None,
            venue_country=None,
        )
        body = build_post_body(event)
        assert 'end="2026-03-12T20:00:00.000+00:00"' in body

    def test_zero_duration_end_equals_start(self) -> None:
        event = MeetupEvent(
            id="1",
            title="Test",
            date_time="2026-03-12T18:00:00.000+00:00",
            duration="PT0S",
            event_url="https://example.com",
            description="",
            group_name="Test Group",
            group_urlname="test-group",
            event_type="PHYSICAL",
            event_timezone="UTC",
            venue_name=None,
            venue_address=None,
            venue_city=None,
            venue_country=None,
        )
        body = build_post_body(event)
        assert 'start="2026-03-12T18:00:00.000+00:00" end="2026-03-12T18:00:00.000+00:00"' in body

    def test_none_duration_end_equals_start(self) -> None:
        event = MeetupEvent(
            id="1",
            title="Test",
            date_time="2026-03-12T18:00:00.000+00:00",
            duration=None,
            event_url="https://example.com",
            description="",
            group_name="Test Group",
            group_urlname="test-group",
            event_type="PHYSICAL",
            event_timezone="UTC",
            venue_name=None,
            venue_address=None,
            venue_city=None,
            venue_country=None,
        )
        body = build_post_body(event)
        assert 'start="2026-03-12T18:00:00.000+00:00" end="2026-03-12T18:00:00.000+00:00"' in body

    def test_iso_duration_end_time_non_utc_offset(self) -> None:
        """Non-UTC offset (+05:30) should produce correct end time via isoformat()."""
        event = MeetupEvent(
            id="1",
            title="Test",
            date_time="2026-03-12T18:00:00.000+05:30",
            duration="PT2H",
            event_url="https://example.com",
            description="",
            group_name="Test Group",
            group_urlname="test-group",
            event_type="PHYSICAL",
            event_timezone="Asia/Kolkata",
            venue_name=None,
            venue_address=None,
            venue_city=None,
            venue_country=None,
        )
        body = build_post_body(event)
        assert 'end="2026-03-12T20:00:00.000+05:30"' in body

    def test_no_venue(self) -> None:
        event = MeetupEvent(
            id="1",
            title="Test",
            date_time="2026-01-01T10:00:00.000+00:00",
            duration=None,
            event_url="https://example.com",
            description="",
            group_name="Test Group",
            group_urlname="test-group",
            event_type="PHYSICAL",
            event_timezone="UTC",
            venue_name=None,
            venue_address=None,
            venue_city=None,
            venue_country=None,
        )
        body = build_post_body(event)
        assert "**Venue:**" not in body


# ---------------------------------------------------------------------------
# Parse duration tests
# ---------------------------------------------------------------------------


class TestParseDuration:
    def test_hours(self) -> None:
        assert parse_duration("PT2H") == timedelta(hours=2)

    def test_minutes(self) -> None:
        assert parse_duration("PT30M") == timedelta(minutes=30)

    def test_hours_and_minutes(self) -> None:
        assert parse_duration("PT1H30M") == timedelta(hours=1, minutes=30)

    def test_zero(self) -> None:
        assert parse_duration("PT0S") == timedelta(0)

    def test_milliseconds(self) -> None:
        assert parse_duration("7200000") == timedelta(milliseconds=7200000)

    def test_none(self) -> None:
        assert parse_duration(None) is None

    def test_empty_string(self) -> None:
        assert parse_duration("") is None

    def test_invalid_string(self) -> None:
        assert parse_duration("not-a-duration") is None

    def test_zero_ms(self) -> None:
        assert parse_duration("0") is None

    def test_negative_ms(self) -> None:
        assert parse_duration("-1000") is None


# ---------------------------------------------------------------------------
# Parse datetime tests
# ---------------------------------------------------------------------------


class TestParseEventDatetime:
    def test_iso_format(self) -> None:
        dt = parse_event_datetime("2026-03-15T18:00:00.000+00:00")
        assert dt.year == 2026
        assert dt.month == 3
        assert dt.day == 15
        assert dt.hour == 18

    def test_with_offset(self) -> None:
        dt = parse_event_datetime("2026-03-15T18:00:00.000+05:30")
        offset = dt.utcoffset()
        assert offset is not None
        assert offset.total_seconds() == 5.5 * 3600


# ---------------------------------------------------------------------------
# Filter events tests
# ---------------------------------------------------------------------------


class TestFilterEvents:
    def test_filter_within_range(self, sample_event: MeetupEvent) -> None:
        now = datetime.now(UTC)
        event_in_range = MeetupEvent(
            id="1",
            title="Soon",
            date_time=(now + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S.000+00:00"),
            duration=None,
            event_url="https://example.com",
            description="",
            group_name="Test",
            group_urlname="test",
            event_type=None,
            event_timezone="UTC",
            venue_name=None,
            venue_address=None,
            venue_city=None,
            venue_country=None,
        )
        event_too_far = MeetupEvent(
            id="2",
            title="Far",
            date_time=(now + timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%S.000+00:00"),
            duration=None,
            event_url="https://example.com",
            description="",
            group_name="Test",
            group_urlname="test",
            event_type=None,
            event_timezone="UTC",
            venue_name=None,
            venue_address=None,
            venue_city=None,
            venue_country=None,
        )
        result = filter_events([event_in_range, event_too_far], lookback_days=1, lookahead_days=90)
        assert len(result) == 1
        assert result[0].id == "1"

    def test_filter_lookback(self) -> None:
        now = datetime.now(UTC)
        yesterday = MeetupEvent(
            id="1",
            title="Yesterday",
            date_time=(now - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%S.000+00:00"),
            duration=None,
            event_url="https://example.com",
            description="",
            group_name="Test",
            group_urlname="test",
            event_type=None,
            event_timezone="UTC",
            venue_name=None,
            venue_address=None,
            venue_city=None,
            venue_country=None,
        )
        result = filter_events([yesterday], lookback_days=1, lookahead_days=90)
        assert len(result) == 1

    def test_filter_too_old(self) -> None:
        now = datetime.now(UTC)
        old_event = MeetupEvent(
            id="1",
            title="Old",
            date_time=(now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000+00:00"),
            duration=None,
            event_url="https://example.com",
            description="",
            group_name="Test",
            group_urlname="test",
            event_type=None,
            event_timezone="UTC",
            venue_name=None,
            venue_address=None,
            venue_city=None,
            venue_country=None,
        )
        result = filter_events([old_event], lookback_days=1, lookahead_days=90)
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Deduplicate events tests
# ---------------------------------------------------------------------------


class TestDeduplicateEvents:
    def test_no_duplicates(self, sample_event: MeetupEvent, online_event: MeetupEvent) -> None:
        result = deduplicate_events([sample_event, online_event], "ansible-virtual-meetups")
        assert len(result) == 2

    def test_duplicate_prefers_virtual(self) -> None:
        event1 = MeetupEvent(
            id="1",
            title="Test",
            date_time="2026-01-01T10:00:00.000+00:00",
            duration=None,
            event_url="https://example.com",
            description="",
            group_name="Group A",
            group_urlname="group-a",
            event_type=None,
            event_timezone="UTC",
            venue_name=None,
            venue_address=None,
            venue_city=None,
            venue_country=None,
        )
        event2 = MeetupEvent(
            id="1",
            title="Test",
            date_time="2026-01-01T10:00:00.000+00:00",
            duration=None,
            event_url="https://example.com",
            description="",
            group_name="Virtual",
            group_urlname="ansible-virtual-meetups",
            event_type=None,
            event_timezone="UTC",
            venue_name=None,
            venue_address=None,
            venue_city=None,
            venue_country=None,
        )
        result = deduplicate_events([event1, event2], "ansible-virtual-meetups")
        assert len(result) == 1
        assert result[0].group_urlname == "ansible-virtual-meetups"

    def test_duplicate_virtual_first_keeps_virtual(self) -> None:
        """Virtual group appears first in input, non-virtual second — should keep virtual."""
        event_virtual = MeetupEvent(
            id="1",
            title="Test",
            date_time="2026-01-01T10:00:00.000+00:00",
            duration=None,
            event_url="https://example.com",
            description="",
            group_name="Virtual",
            group_urlname="ansible-virtual-meetups",
            event_type=None,
            event_timezone="UTC",
            venue_name=None,
            venue_address=None,
            venue_city=None,
            venue_country=None,
        )
        event_physical = MeetupEvent(
            id="1",
            title="Test",
            date_time="2026-01-01T10:00:00.000+00:00",
            duration=None,
            event_url="https://example.com",
            description="",
            group_name="Group A",
            group_urlname="group-a",
            event_type=None,
            event_timezone="UTC",
            venue_name=None,
            venue_address=None,
            venue_city=None,
            venue_country=None,
        )
        result = deduplicate_events([event_virtual, event_physical], "ansible-virtual-meetups")
        assert len(result) == 1
        assert result[0].group_urlname == "ansible-virtual-meetups"

    def test_duplicate_keeps_first_if_no_virtual(self) -> None:
        event1 = MeetupEvent(
            id="1",
            title="Test",
            date_time="2026-01-01T10:00:00.000+00:00",
            duration=None,
            event_url="https://example.com",
            description="",
            group_name="Group A",
            group_urlname="group-a",
            event_type=None,
            event_timezone="UTC",
            venue_name=None,
            venue_address=None,
            venue_city=None,
            venue_country=None,
        )
        event2 = MeetupEvent(
            id="1",
            title="Test",
            date_time="2026-01-01T10:00:00.000+00:00",
            duration=None,
            event_url="https://example.com",
            description="",
            group_name="Group B",
            group_urlname="group-b",
            event_type=None,
            event_timezone="UTC",
            venue_name=None,
            venue_address=None,
            venue_city=None,
            venue_country=None,
        )
        result = deduplicate_events([event1, event2], "ansible-virtual-meetups")
        assert len(result) == 1
        assert result[0].group_urlname == "group-a"

    def test_network_event_dedup_different_ids(self) -> None:
        """Network events have different IDs per group but same network_event_id."""
        event_london = MeetupEvent(
            id="310386047",
            title="Ansible Virtual Meetup: October 2025",
            date_time="2025-10-02T18:00:00+01:00",
            duration=None,
            event_url="https://www.meetup.com/ansible-london/events/310386047/",
            description="",
            group_name="Ansible London",
            group_urlname="Ansible-London",
            event_type="ONLINE",
            event_timezone="Europe/London",
            venue_name=None,
            venue_address=None,
            venue_city=None,
            venue_country=None,
            network_event_id="07ef9599-aab4-4ce9-abd9-df76e6acc60b",
            network_event_group_count=35,
        )
        event_virtual = MeetupEvent(
            id="310386067",
            title="Ansible Virtual Meetup: October 2025",
            date_time="2025-10-02T13:00:00-04:00",
            duration=None,
            event_url="https://www.meetup.com/ansible-virtual-meetups/events/310386067/",
            description="",
            group_name="Ansible Virtual Meetups",
            group_urlname="ansible-virtual-meetups",
            event_type="ONLINE",
            event_timezone="US/Eastern",
            venue_name=None,
            venue_address=None,
            venue_city=None,
            venue_country=None,
            network_event_id="07ef9599-aab4-4ce9-abd9-df76e6acc60b",
            network_event_group_count=35,
        )
        event_atlanta = MeetupEvent(
            id="310386066",
            title="Ansible Virtual Meetup: October 2025",
            date_time="2025-10-02T13:00:00-04:00",
            duration=None,
            event_url="https://www.meetup.com/ansible-atlanta/events/310386066/",
            description="",
            group_name="Ansible Atlanta",
            group_urlname="Ansible-Atlanta",
            event_type="ONLINE",
            event_timezone="US/Eastern",
            venue_name=None,
            venue_address=None,
            venue_city=None,
            venue_country=None,
            network_event_id="07ef9599-aab4-4ce9-abd9-df76e6acc60b",
            network_event_group_count=35,
        )
        result = deduplicate_events([event_london, event_virtual, event_atlanta], "ansible-virtual-meetups")
        assert len(result) == 1
        assert result[0].group_urlname == "ansible-virtual-meetups"
        assert result[0].id == "310386067"

    def test_network_event_dedup_keeps_first_without_virtual(self) -> None:
        """Without virtual_group present, keeps first network event copy."""
        event1 = MeetupEvent(
            id="100",
            title="Network Event",
            date_time="2026-01-01T10:00:00+00:00",
            duration=None,
            event_url="https://example.com/100",
            description="",
            group_name="Group A",
            group_urlname="group-a",
            event_type="ONLINE",
            event_timezone="UTC",
            venue_name=None,
            venue_address=None,
            venue_city=None,
            venue_country=None,
            network_event_id="net-001",
            network_event_group_count=3,
        )
        event2 = MeetupEvent(
            id="101",
            title="Network Event",
            date_time="2026-01-01T10:00:00+00:00",
            duration=None,
            event_url="https://example.com/101",
            description="",
            group_name="Group B",
            group_urlname="group-b",
            event_type="ONLINE",
            event_timezone="UTC",
            venue_name=None,
            venue_address=None,
            venue_city=None,
            venue_country=None,
            network_event_id="net-001",
            network_event_group_count=3,
        )
        result = deduplicate_events([event1, event2], "ansible-virtual-meetups")
        assert len(result) == 1
        assert result[0].id == "100"
        assert result[0].group_urlname == "group-a"

    def test_network_event_mixed_with_standalone(self) -> None:
        """Network events are deduped while standalone events pass through."""
        standalone = MeetupEvent(
            id="200",
            title="Standalone Event",
            date_time="2026-01-01T10:00:00+00:00",
            duration=None,
            event_url="https://example.com/200",
            description="",
            group_name="Group C",
            group_urlname="group-c",
            event_type="PHYSICAL",
            event_timezone="UTC",
            venue_name=None,
            venue_address=None,
            venue_city=None,
            venue_country=None,
        )
        net1 = MeetupEvent(
            id="300",
            title="Network Event",
            date_time="2026-01-01T10:00:00+00:00",
            duration=None,
            event_url="https://example.com/300",
            description="",
            group_name="Group A",
            group_urlname="group-a",
            event_type="ONLINE",
            event_timezone="UTC",
            venue_name=None,
            venue_address=None,
            venue_city=None,
            venue_country=None,
            network_event_id="net-002",
            network_event_group_count=2,
        )
        net2 = MeetupEvent(
            id="301",
            title="Network Event",
            date_time="2026-01-01T10:00:00+00:00",
            duration=None,
            event_url="https://example.com/301",
            description="",
            group_name="Group B",
            group_urlname="group-b",
            event_type="ONLINE",
            event_timezone="UTC",
            venue_name=None,
            venue_address=None,
            venue_city=None,
            venue_country=None,
            network_event_id="net-002",
            network_event_group_count=2,
        )
        result = deduplicate_events([standalone, net1, net2], "ansible-virtual-meetups")
        assert len(result) == 2
        result_ids = {e.id for e in result}
        assert "200" in result_ids
        assert "300" in result_ids


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_staging_flag(self) -> None:
        args = parse_args(["--staging"])
        assert args.staging is True
        assert args.production is False

    def test_production_flag(self) -> None:
        args = parse_args(["--production"])
        assert args.production is True
        assert args.staging is False

    def test_no_mode_fails(self) -> None:
        with pytest.raises(SystemExit):
            parse_args([])

    def test_both_modes_fails(self) -> None:
        with pytest.raises(SystemExit):
            parse_args(["--staging", "--production"])

    def test_custom_args(self) -> None:
        args = parse_args(["--staging", "--config", "/tmp/test.yml", "--log-level", "DEBUG"])
        assert args.config == "/tmp/test.yml"
        assert args.log_level == "DEBUG"

    def test_validate_config_flag(self) -> None:
        args = parse_args(["--staging", "--validate-config"])
        assert args.validate_config is True

    def test_dump_events_flag(self) -> None:
        args = parse_args(["--staging", "--dump-events"])
        assert args.dump_events is True


# ---------------------------------------------------------------------------
# Dump events tests
# ---------------------------------------------------------------------------


class TestDumpEventsTable:
    def test_empty_events(self, capsys: pytest.CaptureFixture[str]) -> None:
        dump_events_table([])
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_table_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        events = [
            {
                "id": "123",
                "title": "Test Event",
                "dateTime": "2026-03-15T18:00:00.000+00:00",
                "endTime": "2026-03-15T20:00:00.000+00:00",
                "duration": "PT2H",
                "eventUrl": "https://example.com",
                "description": "A test event description",
                "status": "UPCOMING",
                "eventType": "PHYSICAL",
                "maxTickets": 100,
                "guestsAllowed": True,
                "numberOfAllowedGuests": 1,
                "group": {"id": "1", "name": "Test Group", "urlname": "test-group", "timezone": "UTC"},
                "venue": {"name": "Office", "address": "123 Main St", "city": "London", "country": "GB", "lat": 51.5},
                "eventHosts": [{"name": "Alice"}, {"name": "Bob"}],
                "featuredEventPhoto": {"baseUrl": "https://photos.example.com/1.jpg"},
                "series": None,
                "rsvpSettings": None,
                "feeSettings": None,
            }
        ]
        dump_events_table(events)
        captured = capsys.readouterr()
        assert "test-group" in captured.out
        assert "Test Event" in captured.out
        assert "100" in captured.out
        assert "Alice, Bob" in captured.out
        assert "London" in captured.out

    def test_table_with_unicode(self, capsys: pytest.CaptureFixture[str]) -> None:
        events = [
            {
                "id": "456",
                "title": "M\u00fcnchen \U0001f37a",
                "dateTime": "2026-03-26T18:00:00+01:00",
                "group": {"urlname": "ansible-munchen", "timezone": "Europe/Berlin"},
                "venue": {"city": "M\u00fcnchen"},
            }
        ]
        dump_events_table(events)
        captured = capsys.readouterr()
        assert "M\u00fcnchen" in captured.out
        assert "\U0001f37a" in captured.out


# ---------------------------------------------------------------------------
# Sync event tests
# ---------------------------------------------------------------------------


class TestSyncEvent:
    def test_create_new_topic(self, sample_event: MeetupEvent, sample_config: Config) -> None:
        discourse = DiscourseClient(sample_config, category=42)
        with patch.object(discourse, "lookup_topic", return_value=None), patch.object(discourse, "create_topic", return_value={"topic_id": 42}) as mock_create:
            result = sync_event(sample_event, discourse)

        assert result.action == "created"
        mock_create.assert_called_once()

    def test_skip_unchanged(self, sample_event: MeetupEvent, sample_config: Config) -> None:
        title = build_post_title(sample_event.group_urlname, sample_event.title, sample_event.date_time)
        body = build_post_body(sample_event)

        discourse = DiscourseClient(sample_config, category=42)
        with patch.object(
            discourse,
            "lookup_topic",
            return_value={
                "id": 42,
                "title": title,
                "post_stream": {"posts": [{"id": 100, "raw": body}]},
            },
        ):
            result = sync_event(sample_event, discourse)

        assert result.action == "skipped"

    def test_skip_title_case_difference(self, sample_event: MeetupEvent, sample_config: Config) -> None:
        """Discourse auto-capitalizes titles; case-only difference should not trigger update."""
        title = build_post_title(sample_event.group_urlname, sample_event.title, sample_event.date_time)
        capitalized_title = title[0].upper() + title[1:]
        body = build_post_body(sample_event)

        discourse = DiscourseClient(sample_config, category=42)
        with patch.object(
            discourse,
            "lookup_topic",
            return_value={
                "id": 42,
                "title": capitalized_title,
                "post_stream": {"posts": [{"id": 100, "raw": body}]},
            },
        ):
            result = sync_event(sample_event, discourse)

        assert result.action == "skipped"

    def test_update_changed_title(self, sample_event: MeetupEvent, sample_config: Config) -> None:
        body = build_post_body(sample_event)

        discourse = DiscourseClient(sample_config, category=42)
        with (
            patch.object(
                discourse,
                "lookup_topic",
                return_value={
                    "id": 42,
                    "title": "Old Title",
                    "post_stream": {"posts": [{"id": 100, "raw": body}]},
                },
            ),
            patch.object(discourse, "update_topic_title", return_value=True) as mock_update,
        ):
            result = sync_event(sample_event, discourse)

        assert result.action == "updated"
        mock_update.assert_called_once()

    def test_body_updated_before_title(self, sample_event: MeetupEvent, sample_config: Config) -> None:
        """Body must be updated before title so Discourse validates against corrected event times."""
        call_order: list[str] = []

        def _track_body(*a: object) -> bool:
            call_order.append("body")
            return True

        def _track_title(*a: object) -> bool:
            call_order.append("title")
            return True

        discourse = DiscourseClient(sample_config, category=42)
        with (
            patch.object(
                discourse,
                "lookup_topic",
                return_value={
                    "id": 42,
                    "title": "Old Title",
                    "post_stream": {"posts": [{"id": 100, "raw": "old body"}]},
                },
            ),
            patch.object(discourse, "update_post_body", side_effect=_track_body) as mock_body,
            patch.object(discourse, "update_topic_title", side_effect=_track_title) as mock_title,
        ):
            result = sync_event(sample_event, discourse)

        assert result.action == "updated"
        mock_body.assert_called_once()
        mock_title.assert_called_once()
        assert call_order == ["body", "title"]

    def test_create_failure(self, sample_event: MeetupEvent, sample_config: Config) -> None:
        discourse = DiscourseClient(sample_config, category=42)
        with patch.object(discourse, "lookup_topic", return_value=None), patch.object(discourse, "create_topic", return_value=None):
            result = sync_event(sample_event, discourse)

        assert result.action == "error"

    def test_update_with_malformed_post_data(self, sample_event: MeetupEvent, sample_config: Config) -> None:
        """Post missing 'id' field should report error, not silently skip body update."""
        discourse = DiscourseClient(sample_config, category=42)
        with (
            patch.object(
                discourse,
                "lookup_topic",
                return_value={
                    "id": 42,
                    "title": "Old Title",
                    "post_stream": {"posts": [{"raw": "old body"}]},
                },
            ),
            patch.object(discourse, "update_topic_title", return_value=True) as mock_title,
            patch.object(discourse, "update_post_body", return_value=True) as mock_body,
        ):
            result = sync_event(sample_event, discourse)

        assert result.action == "error"
        mock_body.assert_not_called()

    def test_network_event_uses_network_id(self, sample_config: Config) -> None:
        """Network events use network_event_id for external_id, not event.id."""
        event = MeetupEvent(
            id="310386067",
            title="Ansible Virtual Meetup",
            date_time="2026-03-01T18:00:00.000+00:00",
            duration="PT2H",
            event_url="https://example.com",
            description="",
            group_name="Virtual",
            group_urlname="ansible-virtual-meetups",
            event_type="ONLINE",
            event_timezone="UTC",
            venue_name=None,
            venue_address=None,
            venue_city=None,
            venue_country=None,
            network_event_id="07ef9599-aab4-4ce9-abd9-df76e6acc60b",
            network_event_group_count=35,
        )
        discourse = DiscourseClient(sample_config, category=42)
        with patch.object(discourse, "lookup_topic", return_value=None), patch.object(discourse, "create_topic", return_value={"topic_id": 99}) as mock_create:
            result = sync_event(event, discourse)

        assert result.action == "created"
        call_args = mock_create.call_args
        assert call_args[0][2] == "07ef9599-aab4-4ce9-abd9-df76e6acc60b"

    def test_standalone_event_uses_event_id(self, sample_event: MeetupEvent, sample_config: Config) -> None:
        """Standalone events (no network_event_id) use event.id for external_id."""
        discourse = DiscourseClient(sample_config, category=42)
        with patch.object(discourse, "lookup_topic", return_value=None), patch.object(discourse, "create_topic", return_value={"topic_id": 99}) as mock_create:
            result = sync_event(sample_event, discourse)

        assert result.action == "created"
        call_args = mock_create.call_args
        assert call_args[0][2] == sample_event.id

    def test_bare_external_id_production(self, sample_event: MeetupEvent, sample_config: Config) -> None:
        """Production mode (empty prefix) produces bare event ID as external_id."""
        discourse = DiscourseClient(sample_config, category=42)
        with patch.object(discourse, "lookup_topic", return_value=None), patch.object(discourse, "create_topic", return_value={"topic_id": 99}) as mock_create:
            result = sync_event(sample_event, discourse, external_id_prefix="")

        assert result.action == "created"
        call_args = mock_create.call_args
        assert call_args[0][2] == "123456789"

    def test_staging_external_id_prefix(self, sample_event: MeetupEvent, sample_config: Config) -> None:
        """Staging mode uses 'staging-{id}' as external_id."""
        discourse = DiscourseClient(sample_config, category=42)
        with patch.object(discourse, "lookup_topic", return_value=None), patch.object(discourse, "create_topic", return_value={"topic_id": 99}) as mock_create:
            result = sync_event(sample_event, discourse, external_id_prefix="staging")

        assert result.action == "created"
        call_args = mock_create.call_args
        assert call_args[0][2] == "staging-123456789"

    def test_update_failure_reports_error(self, sample_event: MeetupEvent, sample_config: Config) -> None:
        """Failed update_post_body returns action='error'."""
        discourse = DiscourseClient(sample_config, category=42)
        with (
            patch.object(
                discourse,
                "lookup_topic",
                return_value={
                    "id": 42,
                    "title": "Old Title",
                    "post_stream": {"posts": [{"id": 100, "raw": "old body"}]},
                },
            ),
            patch.object(discourse, "update_post_body", return_value=False),
            patch.object(discourse, "update_topic_title", return_value=True),
        ):
            result = sync_event(sample_event, discourse)

        assert result.action == "error"
        assert result.error == "Update failed"

    def test_title_update_failure_reports_error(self, sample_event: MeetupEvent, sample_config: Config) -> None:
        """Failed update_topic_title returns action='error'."""
        body = build_post_body(sample_event)
        discourse = DiscourseClient(sample_config, category=42)
        with (
            patch.object(
                discourse,
                "lookup_topic",
                return_value={
                    "id": 42,
                    "title": "Old Title",
                    "post_stream": {"posts": [{"id": 100, "raw": body}]},
                },
            ),
            patch.object(discourse, "update_topic_title", return_value=False),
        ):
            result = sync_event(sample_event, discourse)

        assert result.action == "error"
        assert result.error == "Update failed"


# ---------------------------------------------------------------------------
# Discourse client tests
# ---------------------------------------------------------------------------


class TestDiscourseClient:
    def test_create_topic_accepts_201(self, sample_config: Config) -> None:
        """Discourse POST /posts.json returns 201 on success."""
        discourse = DiscourseClient(sample_config, category=42)
        mock_resp = MockResponse(status_code=201, json_data={"topic_id": 99})
        with patch.object(discourse._session, "post", return_value=mock_resp):
            result = discourse.create_topic("Test Title", "Test body", "ext-1")

        assert result is not None
        assert result["topic_id"] == 99

    def test_create_topic_rejects_500(self, sample_config: Config) -> None:
        """Discourse POST returning 500 should return None."""
        discourse = DiscourseClient(sample_config, category=42)
        mock_resp = MockResponse(status_code=500, text="Internal Server Error")
        with patch.object(discourse._session, "post", return_value=mock_resp):
            result = discourse.create_topic("Test Title", "Test body", "ext-1")

        assert result is None

    def test_create_topic_retries_after_429(self, sample_config: Config) -> None:
        """429 response triggers sleep then retry."""
        discourse = DiscourseClient(sample_config, category=42)
        rate_limit_resp = MockResponse(status_code=429, headers={"Retry-After": "1"})
        success_resp = MockResponse(status_code=201, json_data={"topic_id": 99})
        with (
            patch.object(discourse._session, "post", side_effect=[rate_limit_resp, success_resp]) as mock_post,
            patch("sync_meetups.time.sleep") as mock_sleep,
        ):
            result = discourse.create_topic("Test Title", "Test body", "ext-1")

        assert result is not None
        assert result["topic_id"] == 99
        assert mock_post.call_count == 2
        mock_sleep.assert_called_once_with(1)

    def test_update_topic_title_retries_after_429(self, sample_config: Config) -> None:
        """update_topic_title retries after 429."""
        discourse = DiscourseClient(sample_config, category=42)
        rate_limit_resp = MockResponse(status_code=429, headers={"Retry-After": "2"})
        success_resp = MockResponse(status_code=200)
        with (
            patch.object(discourse._session, "put", side_effect=[rate_limit_resp, success_resp]) as mock_put,
            patch("sync_meetups.time.sleep") as mock_sleep,
        ):
            result = discourse.update_topic_title(42, "New Title")

        assert result is True
        assert mock_put.call_count == 2
        mock_sleep.assert_called_once_with(2)

    def test_update_post_body_retries_after_429(self, sample_config: Config) -> None:
        """update_post_body retries after 429."""
        discourse = DiscourseClient(sample_config, category=42)
        rate_limit_resp = MockResponse(status_code=429, headers={"Retry-After": "3"})
        success_resp = MockResponse(status_code=200)
        with (
            patch.object(discourse._session, "put", side_effect=[rate_limit_resp, success_resp]) as mock_put,
            patch("sync_meetups.time.sleep") as mock_sleep,
        ):
            result = discourse.update_post_body(100, "New body")

        assert result is True
        assert mock_put.call_count == 2
        mock_sleep.assert_called_once_with(3)


class MockResponse:
    """Minimal mock for requests.Response."""

    def __init__(self, status_code: int = 200, json_data: dict[str, Any] | None = None, text: str = "", headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text
        self.headers: dict[str, str] = headers or {}

    def json(self) -> dict[str, Any]:
        return self._json_data


# ---------------------------------------------------------------------------
# Validate config tests
# ---------------------------------------------------------------------------


def _make_config_with_key(tmp_path: Path, key_content: str = "fake-key") -> Config:
    """Create a Config with a private key file."""
    key_file = tmp_path / "test_key.pem"
    key_file.write_text(key_content)
    return Config(
        meetup_client_key="test_client_key",
        meetup_signing_key_id="test_signing_key_id",
        meetup_private_key_path=str(key_file),
        meetup_member_id="12345",
        meetup_pro_network="ansible",
        meetup_api_url="https://api.meetup.com/gql-ext",
        meetup_token_url="https://secure.meetup.com/oauth2/access",
        discourse_url="https://forum.example.com",
        discourse_category_staging=42,
        discourse_category_production=5,
        discourse_api_key="test_api_key",
        discourse_api_user="TestBot",
    )


# RSA private key for testing (2048-bit, not used for real auth)
_TEST_RSA_KEY = """-----BEGIN RSA PRIVATE KEY-----
MIIEowIBAAKCAQEAhDumhL+iZn9+OiazSPWvtK0VVU6OC93UCmuFJRL7//f104Ju
va0tUv8SFZPs3+TohnIdGm5RcI0jgqBW5fr77Kn0Ask+uKy0LGNGwELZamKCjte4
ElTDSl5tTY2mYNrPhVg8X/WWn9tEeUS6S2GhAVsX19zcwQbm1lVkNzFCd1pw9N98
5yueyJyyLd/ok5XjJDyyz21wkLrbqfHIw0hyQ1rHNMTnA0ZBiXx/OYDg0bdBwHnZ
3DEJQN08ETL08pwZSF1GWV9DJey/s/54k01KBQ3UEgcwyUEKCTKM0JZ+K/1wucqU
3qzYrT57i7jG3AvinVBcqJwazJVFJ47+8NteRwIDAQABAoIBAA+b2vQgitL+ia/j
kZYzKiJxq+r98taDwNvaBUVzEDwO0P2+j8PkBU2evku9wmBLbQcxwS21h/d5MY/h
zWAoWER/a0ZI6xZxjHMQ5PEc8v0T08V4wUmop8THkK9u4Qzdx1E+MSJCox0LjPGj
oznytEassgvRDl3aqrTyL3o0XlwoLWxVgyTgSKpl0Zr/Tp+xkhMp/ZWYNouLrLZ4
VrrVh5fQTOqDu5LlMYBUn9oc+AD5FyMco8poSjVC/W8gyKIWfHwUiT0voUJxE60t
BKEpRuXwp6WBRDD1dlnR9HQJ+JULEwqdserWRZxUcwK5MvE+F6kS9kwKrhQjKc7m
GZic14ECgYEAupqoHTL1ycIrX+0VFLV3eXNf7CPe+D9R2tqOfXyrq8urE7Wr7FV0
3fWC91oNQPbf0mBSkKV3/AOh//P1k//GSgBGBAHquTGWbad5oSRjfcVVzb/GLU4+
pbDe1LJAQqCna7iwaGaDvoQPK8Ub9KF7+Uvm+c27Sg8lzbtc/KaVszkCgYEAtWit
/jOzIj1LERnYik3UwfTD3bdHHnKxiEi8EsgxNkYvJcT3a8RLjTk7ca1ByLE94mWW
gvUyrXxU0zVjxfuZwwTQZgYnBchQSzk8gj+YQdFWrn1gMkPjtJV4kx8u5hEr7HXT
mpUCs1tqomajhFDECNlmOumrzEdSadpUJL+aHX8CgYA3D6+Pfhv8fqjh00knJSyt
z5d8TFFcmwKCO39UE9dsB9rhI/go8kZbwDf22MGUa8Q7hWSXfdvbpw7EQa4zD4Pp
Dg+a2x3xq2ohzQscu2oIEJRy86V8dNwTdA8sX7SKdHEyXfrfs3AoZTs8xRqsooG8
W+M5zrT282VKQYD8pAMEMQKBgDlypvcDRE0pf+YweySBNUkezBAghEMeKx5vei+w
efUoELIzR+82wH4+i5aaOWTmzCQv65QZNi0+XFZuZ+RAoxbhJWXJuP3Zy6OmwoS0
wvDE7GBhj98bJLcBRqfAjkeJVJGTVqlzWuGVp5U6T7oNIadzwS4S5bbRN0YSP+dL
TfDdAoGBAIo7meV+0+3QP1VjaDIK1wnf8fWnADfz+dtHjlxyXxsdJ++5OfUu3IL7
P2KIE/fElaVD3aAvx8EqeK/6U9DLhEwJLo4nsmyGLcUwOUyayag89mw0UD4bR0l/
u3FlsqNvXMfp1mq6J8C28JBu7pDth7gnZjbHoWUkVX9+yqfB5z//
-----END RSA PRIVATE KEY-----"""


class TestValidateConfig:
    def test_all_checks_pass(self, tmp_path: Path) -> None:
        """All checks pass with mocked API responses."""
        config = _make_config_with_key(tmp_path, _TEST_RSA_KEY)

        token_resp = MockResponse(200, json_data={"access_token": "tok123", "expires_in": 3600})
        pro_network_resp = MockResponse(
            200,
            json_data={"data": {"proNetwork": {"groupsSearch": {"totalCount": 5}}}},
        )
        site_resp = MockResponse(200)
        staging_category_resp = MockResponse(200)
        production_category_resp = MockResponse(200)
        external_id_resp = MockResponse(404)

        responses_post = [token_resp, pro_network_resp]
        responses_get = [site_resp, staging_category_resp, production_category_resp, external_id_resp]
        post_idx = {"i": 0}
        get_idx = {"i": 0}

        def mock_post(self: Any, url: str, **kwargs: Any) -> MockResponse:
            idx = post_idx["i"]
            post_idx["i"] += 1
            return responses_post[idx]

        def mock_get(self: Any, url: str, **kwargs: Any) -> MockResponse:
            idx = get_idx["i"]
            get_idx["i"] += 1
            return responses_get[idx]

        with patch.object(requests.Session, "post", mock_post), patch.object(requests.Session, "get", mock_get):
            result = validate_config(config)

        assert result is True

    def test_private_key_missing(self, tmp_path: Path) -> None:
        """Private key file missing fails check 1 and skips checks 2-5."""
        config = Config(
            meetup_client_key="ck",
            meetup_signing_key_id="ski",
            meetup_private_key_path=str(tmp_path / "nonexistent.pem"),
            meetup_member_id="123",
            meetup_pro_network="ansible",
            meetup_api_url="https://api.meetup.com/gql-ext",
            meetup_token_url="https://secure.meetup.com/oauth2/access",
            discourse_url="https://forum.example.com",
            discourse_category_staging=42,
            discourse_category_production=5,
            discourse_api_key="dk",
            discourse_api_user="Bot",
        )

        site_resp = MockResponse(200)
        staging_category_resp = MockResponse(200)
        production_category_resp = MockResponse(200)
        external_id_resp = MockResponse(404)
        responses = [site_resp, staging_category_resp, production_category_resp, external_id_resp]
        call_idx = {"i": 0}

        def mock_get(self: Any, url: str, **kwargs: Any) -> MockResponse:
            idx = call_idx["i"]
            call_idx["i"] += 1
            return responses[idx]

        with patch.object(requests.Session, "get", mock_get):
            result = validate_config(config)

        assert result is False

    def test_invalid_rsa_key(self, tmp_path: Path) -> None:
        """Invalid RSA key fails check 2 and skips checks 3-5."""
        config = _make_config_with_key(tmp_path, "not-a-valid-rsa-key")

        site_resp = MockResponse(200)
        staging_category_resp = MockResponse(200)
        production_category_resp = MockResponse(200)
        external_id_resp = MockResponse(404)
        responses = [site_resp, staging_category_resp, production_category_resp, external_id_resp]
        call_idx = {"i": 0}

        def mock_get(self: Any, url: str, **kwargs: Any) -> MockResponse:
            idx = call_idx["i"]
            call_idx["i"] += 1
            return responses[idx]

        with patch.object(requests.Session, "get", mock_get):
            result = validate_config(config)

        assert result is False

    def test_meetup_auth_failure(self, tmp_path: Path) -> None:
        """Meetup auth failure (401) fails check 3."""
        config = _make_config_with_key(tmp_path, _TEST_RSA_KEY)

        token_resp = MockResponse(401, text="Unauthorized")
        site_resp = MockResponse(200)
        staging_category_resp = MockResponse(200)
        production_category_resp = MockResponse(200)
        external_id_resp = MockResponse(404)

        responses_post = [token_resp]
        responses_get = [site_resp, staging_category_resp, production_category_resp, external_id_resp]
        post_idx = {"i": 0}
        get_idx = {"i": 0}

        def mock_post(self: Any, url: str, **kwargs: Any) -> MockResponse:
            idx = post_idx["i"]
            post_idx["i"] += 1
            return responses_post[idx]

        def mock_get(self: Any, url: str, **kwargs: Any) -> MockResponse:
            idx = get_idx["i"]
            get_idx["i"] += 1
            return responses_get[idx]

        with patch.object(requests.Session, "post", mock_post), patch.object(requests.Session, "get", mock_get):
            result = validate_config(config)

        assert result is False

    def test_discourse_403(self, tmp_path: Path) -> None:
        """Discourse 403 on category endpoint means invalid credentials."""
        config = _make_config_with_key(tmp_path, _TEST_RSA_KEY)

        token_resp = MockResponse(200, json_data={"access_token": "tok123", "expires_in": 3600})
        pro_network_resp = MockResponse(
            200,
            json_data={"data": {"proNetwork": {"groupsSearch": {"totalCount": 5}}}},
        )
        site_resp = MockResponse(200)
        category_resp = MockResponse(403)

        responses_post = [token_resp, pro_network_resp]
        responses_get = [site_resp, category_resp]
        post_idx = {"i": 0}
        get_idx = {"i": 0}

        def mock_post(self: Any, url: str, **kwargs: Any) -> MockResponse:
            idx = post_idx["i"]
            post_idx["i"] += 1
            return responses_post[idx]

        def mock_get(self: Any, url: str, **kwargs: Any) -> MockResponse:
            idx = get_idx["i"]
            get_idx["i"] += 1
            return responses_get[idx]

        with patch.object(requests.Session, "post", mock_post), patch.object(requests.Session, "get", mock_get):
            result = validate_config(config)

        assert result is False

    def test_category_not_found(self, tmp_path: Path) -> None:
        """Staging category not found (404) fails but credentials are valid."""
        config = _make_config_with_key(tmp_path, _TEST_RSA_KEY)

        token_resp = MockResponse(200, json_data={"access_token": "tok123", "expires_in": 3600})
        pro_network_resp = MockResponse(
            200,
            json_data={"data": {"proNetwork": {"groupsSearch": {"totalCount": 5}}}},
        )
        site_resp = MockResponse(200)
        staging_category_resp = MockResponse(404)
        production_category_resp = MockResponse(200)
        external_id_resp = MockResponse(404)

        responses_post = [token_resp, pro_network_resp]
        responses_get = [site_resp, staging_category_resp, production_category_resp, external_id_resp]
        post_idx = {"i": 0}
        get_idx = {"i": 0}

        def mock_post(self: Any, url: str, **kwargs: Any) -> MockResponse:
            idx = post_idx["i"]
            post_idx["i"] += 1
            return responses_post[idx]

        def mock_get(self: Any, url: str, **kwargs: Any) -> MockResponse:
            idx = get_idx["i"]
            get_idx["i"] += 1
            return responses_get[idx]

        with patch.object(requests.Session, "post", mock_post), patch.object(requests.Session, "get", mock_get):
            result = validate_config(config)

        assert result is False

    def test_invalid_lookahead_days(self, tmp_path: Path) -> None:
        """Invalid lookahead_days fails check 10."""
        config = _make_config_with_key(tmp_path, _TEST_RSA_KEY)
        config.lookahead_days = -1  # type: ignore[assignment]

        token_resp = MockResponse(200, json_data={"access_token": "tok123", "expires_in": 3600})
        pro_network_resp = MockResponse(
            200,
            json_data={"data": {"proNetwork": {"groupsSearch": {"totalCount": 5}}}},
        )
        site_resp = MockResponse(200)
        staging_category_resp = MockResponse(200)
        production_category_resp = MockResponse(200)
        external_id_resp = MockResponse(404)

        responses_post = [token_resp, pro_network_resp]
        responses_get = [site_resp, staging_category_resp, production_category_resp, external_id_resp]
        post_idx = {"i": 0}
        get_idx = {"i": 0}

        def mock_post(self: Any, url: str, **kwargs: Any) -> MockResponse:
            idx = post_idx["i"]
            post_idx["i"] += 1
            return responses_post[idx]

        def mock_get(self: Any, url: str, **kwargs: Any) -> MockResponse:
            idx = get_idx["i"]
            get_idx["i"] += 1
            return responses_get[idx]

        with patch.object(requests.Session, "post", mock_post), patch.object(requests.Session, "get", mock_get):
            result = validate_config(config)

        assert result is False

    def test_invalid_lookback_days(self, tmp_path: Path) -> None:
        """Invalid lookback_days fails check 11."""
        config = _make_config_with_key(tmp_path, _TEST_RSA_KEY)
        config.lookback_days = -5  # type: ignore[assignment]

        token_resp = MockResponse(200, json_data={"access_token": "tok123", "expires_in": 3600})
        pro_network_resp = MockResponse(
            200,
            json_data={"data": {"proNetwork": {"groupsSearch": {"totalCount": 5}}}},
        )
        site_resp = MockResponse(200)
        staging_category_resp = MockResponse(200)
        production_category_resp = MockResponse(200)
        external_id_resp = MockResponse(404)

        responses_post = [token_resp, pro_network_resp]
        responses_get = [site_resp, staging_category_resp, production_category_resp, external_id_resp]
        post_idx = {"i": 0}
        get_idx = {"i": 0}

        def mock_post(self: Any, url: str, **kwargs: Any) -> MockResponse:
            idx = post_idx["i"]
            post_idx["i"] += 1
            return responses_post[idx]

        def mock_get(self: Any, url: str, **kwargs: Any) -> MockResponse:
            idx = get_idx["i"]
            get_idx["i"] += 1
            return responses_get[idx]

        with patch.object(requests.Session, "post", mock_post), patch.object(requests.Session, "get", mock_get):
            result = validate_config(config)

        assert result is False


# ---------------------------------------------------------------------------
# GraphQL retry tests
# ---------------------------------------------------------------------------


class TestGraphQLRetry:
    def test_retries_on_500_with_backoff(self, sample_config: Config) -> None:
        client = MeetupClient(sample_config)
        client._access_token = "fake-token"
        client._token_expires_at = 9999999999.0

        responses = [
            MockResponse(500, text="server error"),
            MockResponse(500, text="server error"),
            MockResponse(200, json_data={"data": {"result": True}}),
        ]
        call_count = 0

        def mock_post(*args: Any, **kwargs: Any) -> MockResponse:
            nonlocal call_count
            resp = responses[call_count]
            call_count += 1
            return resp

        with patch.object(client._session, "post", side_effect=mock_post), patch("sync_meetups.time.sleep") as mock_sleep:
            result = client._graphql("query { test }")

        assert result == {"data": {"result": True}}
        assert call_count == 3
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(1)
        mock_sleep.assert_any_call(2)

    def test_retries_on_429_with_60s_wait(self, sample_config: Config) -> None:
        client = MeetupClient(sample_config)
        client._access_token = "fake-token"
        client._token_expires_at = 9999999999.0

        responses = [
            MockResponse(429, text="rate limited"),
            MockResponse(200, json_data={"data": {"ok": True}}),
        ]
        call_count = 0

        def mock_post(*args: Any, **kwargs: Any) -> MockResponse:
            nonlocal call_count
            resp = responses[call_count]
            call_count += 1
            return resp

        with patch.object(client._session, "post", side_effect=mock_post), patch("sync_meetups.time.sleep") as mock_sleep:
            result = client._graphql("query { test }")

        assert result == {"data": {"ok": True}}
        mock_sleep.assert_called_once_with(60)

    def test_returns_empty_dict_on_exhaustion(self, sample_config: Config) -> None:
        client = MeetupClient(sample_config)
        client._access_token = "fake-token"
        client._token_expires_at = 9999999999.0

        def mock_post(*args: Any, **kwargs: Any) -> MockResponse:
            return MockResponse(500, text="server error")

        with patch.object(client._session, "post", mock_post), patch("sync_meetups.time.sleep"):
            result = client._graphql("query { test }", retries=3)

        assert result == {}

    def test_breaks_on_4xx_non_429(self, sample_config: Config) -> None:
        client = MeetupClient(sample_config)
        client._access_token = "fake-token"
        client._token_expires_at = 9999999999.0

        call_count = 0

        def mock_post(*args: Any, **kwargs: Any) -> MockResponse:
            nonlocal call_count
            call_count += 1
            return MockResponse(403, text="forbidden")

        with patch.object(client._session, "post", mock_post), patch("sync_meetups.time.sleep") as mock_sleep:
            result = client._graphql("query { test }")

        assert result == {}
        assert call_count == 1
        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# run_sync tests
# ---------------------------------------------------------------------------


class TestRunSync:
    def test_skips_network_events(self, sample_config: Config) -> None:
        standalone_event = MeetupEvent(
            id="100", title="Standalone", date_time=datetime.now(UTC).isoformat(),
            duration="PT2H", event_url="https://meetup.com/e/100", description="desc",
            group_name="test", group_urlname="test-group", event_type="PHYSICAL",
            event_timezone="UTC", venue_name=None, venue_address=None,
            venue_city=None, venue_country=None,
        )
        network_event = MeetupEvent(
            id="200", title="Network", date_time=datetime.now(UTC).isoformat(),
            duration="PT2H", event_url="https://meetup.com/e/200", description="desc",
            group_name="test", group_urlname="test-group", event_type="PHYSICAL",
            event_timezone="UTC", venue_name=None, venue_address=None,
            venue_city=None, venue_country=None, network_event_id="net-uuid-1",
        )

        with patch.object(MeetupClient, "__init__", return_value=None), \
             patch.object(MeetupClient, "fetch_groups", return_value=[]), \
             patch.object(MeetupClient, "fetch_events", return_value=[standalone_event, network_event]), \
             patch.object(DiscourseClient, "__init__", return_value=None), \
             patch("sync_meetups.sync_event", return_value=SyncResult(event_id="100", action="created")) as mock_sync, \
             patch("sync_meetups.time.sleep"):
            summary = run_sync(sample_config, category=42)

        assert summary.events_fetched == 2
        assert summary.events_after_dedup == 1
        assert summary.created == 1
        mock_sync.assert_called_once()
        synced_event = mock_sync.call_args[0][0]
        assert synced_event.id == "100"

    def test_catches_request_exception(self, sample_config: Config) -> None:
        event = MeetupEvent(
            id="100", title="Test", date_time=datetime.now(UTC).isoformat(),
            duration="PT2H", event_url="https://meetup.com/e/100", description="desc",
            group_name="test", group_urlname="test-group", event_type="PHYSICAL",
            event_timezone="UTC", venue_name=None, venue_address=None,
            venue_city=None, venue_country=None,
        )

        with patch.object(MeetupClient, "__init__", return_value=None), \
             patch.object(MeetupClient, "fetch_groups", return_value=[]), \
             patch.object(MeetupClient, "fetch_events", return_value=[event]), \
             patch.object(DiscourseClient, "__init__", return_value=None), \
             patch("sync_meetups.sync_event", side_effect=requests.RequestException("connection error")), \
             patch("sync_meetups.time.sleep"):
            summary = run_sync(sample_config, category=42)

        assert summary.errors == 1
        assert summary.results[0].action == "error"
        assert summary.results[0].error == "Unexpected error"


# ---------------------------------------------------------------------------
# Edge case tests for real-world data scenarios
# ---------------------------------------------------------------------------


class TestBuildPostBodySanitization:
    """Verify that user-generated content from Meetup API produces valid output."""

    def test_title_with_double_quotes_in_bbcode(self) -> None:
        """Meetup organizer puts double quotes in the event title."""
        event = MeetupEvent(
            id="1", title='Ansible "Best Practices" Workshop',
            date_time="2026-03-15T18:00:00.000+00:00", duration="PT2H",
            event_url="https://meetup.com/e/1", description="A workshop.",
            group_name="Test", group_urlname="test", event_type="PHYSICAL",
            event_timezone="UTC", venue_name=None, venue_address=None,
            venue_city=None, venue_country=None,
        )
        body = build_post_body(event)
        assert """name="Ansible 'Best Practices' Workshop\"""" in body
        assert '## Ansible "Best Practices" Workshop' in body

    def test_url_with_parentheses_in_markdown_link(self) -> None:
        """Event URL with closing paren doesn't break Markdown link."""
        event = MeetupEvent(
            id="1", title="Test",
            date_time="2026-03-15T18:00:00.000+00:00", duration="PT2H",
            event_url="https://meetup.com/events/123_(special)/",
            description="Desc", group_name="Test", group_urlname="test",
            event_type="PHYSICAL", event_timezone="UTC",
            venue_name=None, venue_address=None, venue_city=None, venue_country=None,
        )
        body = build_post_body(event)
        assert "[View event on Meetup](https://meetup.com/events/123_(special%29/)" in body

    def test_url_with_double_quotes_in_bbcode(self) -> None:
        """Event URL with double quotes doesn't break BBCode attributes."""
        event = MeetupEvent(
            id="1", title="Test",
            date_time="2026-03-15T18:00:00.000+00:00", duration="PT2H",
            event_url='https://meetup.com/e/1?ref="foo"',
            description="Desc", group_name="Test", group_urlname="test",
            event_type="PHYSICAL", event_timezone="UTC",
            venue_name=None, venue_address=None, venue_city=None, venue_country=None,
        )
        body = build_post_body(event)
        assert 'url="https://meetup.com/e/1?ref=\'foo\'"' in body

    def test_timezone_with_double_quotes(self) -> None:
        """Timezone containing quotes doesn't break BBCode timezone attribute."""
        event = MeetupEvent(
            id="1", title="Test",
            date_time="2026-03-15T18:00:00.000+00:00", duration="PT2H",
            event_url="https://meetup.com/e/1", description="Desc",
            group_name="Test", group_urlname="test", event_type="PHYSICAL",
            event_timezone='Europe/London" injected="true',
            venue_name=None, venue_address=None, venue_city=None, venue_country=None,
        )
        body = build_post_body(event)
        assert 'timezone="Europe/London\' injected=\'true"' in body
        assert 'injected="true"' not in body

    def test_minimal_event_no_description_no_venue_no_timezone(self) -> None:
        """Bare-minimum event from API: empty description, no venue, no timezone."""
        event = MeetupEvent(
            id="1", title="Minimal",
            date_time="2026-03-15T18:00:00.000+00:00", duration=None,
            event_url="https://meetup.com/e/1", description="",
            group_name="Test", group_urlname="test", event_type=None,
            event_timezone=None, venue_name=None, venue_address=None,
            venue_city=None, venue_country=None,
        )
        body = build_post_body(event)
        assert "## Minimal" in body
        assert "timezone=" not in body
        assert "**Venue:**" not in body
        assert "**RSVP on Meetup:**" in body


class TestDiscourseRateLimitEdgeCases:
    """Verify rate-limit handling with real-world edge cases."""

    def test_retry_after_date_string_does_not_crash(self, sample_config: Config) -> None:
        """Discourse sends HTTP-date format Retry-After header."""
        discourse = DiscourseClient(sample_config, category=42)

        responses = [
            MockResponse(429, headers={"Retry-After": "Thu, 01 Jan 2026 00:00:00 GMT"}),
            MockResponse(200, json_data={"id": 42, "post_stream": {"posts": [{"id": 1, "raw": "body"}]}}),
        ]
        call_count = 0

        def mock_get(*args: Any, **kwargs: Any) -> MockResponse:
            nonlocal call_count
            resp = responses[call_count]
            call_count += 1
            return resp

        with patch.object(discourse._session, "get", side_effect=mock_get), patch("sync_meetups.time.sleep") as mock_sleep:
            result = discourse.lookup_topic("test-id")

        assert result is not None
        mock_sleep.assert_called_once_with(10)

    def test_lookup_topic_raises_on_persistent_429(self, sample_config: Config) -> None:
        """lookup_topic raises when both attempts are rate-limited."""
        discourse = DiscourseClient(sample_config, category=42)

        def mock_get(*args: Any, **kwargs: Any) -> MockResponse:
            return MockResponse(429, headers={"Retry-After": "5"})

        with patch.object(discourse._session, "get", side_effect=mock_get), \
             patch("sync_meetups.time.sleep"), \
             pytest.raises(requests.RequestException, match="status 429"):
            discourse.lookup_topic("test-id")


class TestRunSyncErrorPropagation:
    """Verify that lookup failures propagate correctly through sync."""

    def test_lookup_failure_counts_as_error_not_create(self, sample_config: Config) -> None:
        """When lookup_topic raises (e.g. rate limited), sync records an error
        instead of attempting to create a duplicate topic."""
        event = MeetupEvent(
            id="100", title="Test", date_time=datetime.now(UTC).isoformat(),
            duration="PT2H", event_url="https://meetup.com/e/100", description="desc",
            group_name="test", group_urlname="test-group", event_type="PHYSICAL",
            event_timezone="UTC", venue_name=None, venue_address=None,
            venue_city=None, venue_country=None,
        )

        with patch.object(MeetupClient, "__init__", return_value=None), \
             patch.object(MeetupClient, "fetch_groups", return_value=[]), \
             patch.object(MeetupClient, "fetch_events", return_value=[event]), \
             patch.object(DiscourseClient, "__init__", return_value=None), \
             patch.object(DiscourseClient, "lookup_topic", side_effect=requests.RequestException("429 rate limited")), \
             patch.object(DiscourseClient, "create_topic") as mock_create, \
             patch("sync_meetups.time.sleep"):
            summary = run_sync(sample_config, category=42)

        assert summary.errors == 1
        assert summary.created == 0
        mock_create.assert_not_called()

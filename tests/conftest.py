"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from sync_meetups import Config, MeetupEvent


@pytest.fixture
def sample_config(tmp_path: Path) -> Config:
    """Return a Config with test values."""
    key_file = tmp_path / "test_key.pem"
    key_file.write_text("fake-key")
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


@pytest.fixture
def sample_event() -> MeetupEvent:
    """Return a sample MeetupEvent."""
    return MeetupEvent(
        id="123456789",
        title="Getting Started with Ansible Automation",
        date_time="2026-03-15T18:00:00.000+00:00",
        duration="7200000",
        event_url="https://www.meetup.com/ansible-london/events/123456789/",
        description="Join us for an evening of **Ansible automation**!\n\nWe'll cover the basics and more.",
        group_name="Ansible London",
        group_urlname="ansible-london",
        event_type="PHYSICAL",
        event_timezone="Europe/London",
        venue_name="WeWork Moorgate",
        venue_address="1 Fore Street Avenue",
        venue_city="London",
        venue_country="GB",
    )


@pytest.fixture
def online_event() -> MeetupEvent:
    """Return a sample online MeetupEvent."""
    return MeetupEvent(
        id="987654321",
        title="Virtual Ansible Workshop",
        date_time="2026-04-01T17:00:00.000+00:00",
        duration="3600000",
        event_url="https://www.meetup.com/ansible-virtual-meetups/events/987654321/",
        description="An online workshop about **Ansible**.\n\nJoin us from anywhere!",
        group_name="Ansible Virtual Meetups",
        group_urlname="ansible-virtual-meetups",
        event_type="ONLINE",
        event_timezone="UTC",
        venue_name=None,
        venue_address=None,
        venue_city=None,
        venue_country=None,
    )

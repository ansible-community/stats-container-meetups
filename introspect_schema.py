#!/usr/bin/env python3
"""Introspect the Meetup GraphQL API schema to discover network event types.

Reuses Config and MeetupClient from sync_meetups.py for authentication.
Runs introspection queries against ProNetwork, NetworkEvent, Event, and
NetworkEventsFilter types to understand how network events are represented.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from sync_meetups import Config, MeetupClient

log = logging.getLogger(__name__)


def introspect_type(client: MeetupClient, type_name: str) -> dict[str, Any]:
    """Run a __type introspection query for the given type name."""
    query = """
    query IntrospectType($name: String!) {
      __type(name: $name) {
        name
        kind
        description
        fields {
          name
          description
          type {
            name
            kind
            ofType {
              name
              kind
              ofType {
                name
                kind
              }
            }
          }
          args {
            name
            type {
              name
              kind
              ofType {
                name
                kind
              }
            }
          }
        }
        inputFields {
          name
          description
          type {
            name
            kind
            ofType {
              name
              kind
            }
          }
        }
        enumValues {
          name
          description
        }
      }
    }
    """
    return client._graphql(query, {"name": type_name})


def format_type_ref(type_info: dict[str, Any] | None) -> str:
    """Format a GraphQL type reference into a readable string."""
    if not type_info:
        return "Unknown"

    kind = type_info.get("kind", "")
    name = type_info.get("name")

    if name:
        return str(name)

    of_type = type_info.get("ofType")
    if kind == "NON_NULL" and of_type:
        return f"{format_type_ref(of_type)}!"
    if kind == "LIST" and of_type:
        return f"[{format_type_ref(of_type)}]"

    return kind or "Unknown"


def print_type_result(type_name: str, result: dict[str, Any]) -> None:
    """Print introspection result in a readable format."""
    print(f"\n{'=' * 70}")
    print(f"Type: {type_name}")
    print("=" * 70)

    if "errors" in result:
        print(f"  GraphQL errors: {json.dumps(result['errors'], indent=2)}")

    data = result.get("data", {})
    type_info = data.get("__type")

    if not type_info:
        print(f"  Type '{type_name}' not found in schema")
        return

    print(f"  Kind: {type_info.get('kind', 'N/A')}")
    if type_info.get("description"):
        print(f"  Description: {type_info['description']}")

    # Print fields (for OBJECT types)
    fields = type_info.get("fields") or []
    if fields:
        print(f"\n  Fields ({len(fields)}):")
        for field in sorted(fields, key=lambda f: f["name"]):
            type_str = format_type_ref(field.get("type"))
            desc = f" - {field['description']}" if field.get("description") else ""
            args = field.get("args") or []
            args_str = ""
            if args:
                arg_parts = [f"{a['name']}: {format_type_ref(a.get('type'))}" for a in args]
                args_str = f"({', '.join(arg_parts)})"
            print(f"    {field['name']}{args_str}: {type_str}{desc}")

    # Print input fields (for INPUT_OBJECT types)
    input_fields = type_info.get("inputFields") or []
    if input_fields:
        print(f"\n  Input Fields ({len(input_fields)}):")
        for field in sorted(input_fields, key=lambda f: f["name"]):
            type_str = format_type_ref(field.get("type"))
            desc = f" - {field['description']}" if field.get("description") else ""
            print(f"    {field['name']}: {type_str}{desc}")

    # Print enum values (for ENUM types)
    enum_values = type_info.get("enumValues") or []
    if enum_values:
        print(f"\n  Enum Values ({len(enum_values)}):")
        for val in enum_values:
            desc = f" - {val['description']}" if val.get("description") else ""
            print(f"    {val['name']}{desc}")


def run_network_events_query(client: MeetupClient, pro_network: str) -> dict[str, Any]:
    """Try querying proNetwork.networkEvents to see if it exists."""
    query = """
    query ($urlname: ID!) {
      proNetwork(urlname: $urlname) {
        networkEvents(input: { first: 1 }) {
          totalCount
          pageInfo { endCursor hasNextPage }
          edges {
            node {
              id
              title
              dateTime
              eventUrl
              description
              status
              eventType
              group { id name urlname }
            }
          }
        }
      }
    }
    """
    return client._graphql(query, {"urlname": pro_network})


def run_events_search_network_fields(client: MeetupClient, pro_network: str, status: str = "UPCOMING") -> dict[str, Any]:
    """Query eventsSearch with networkEvent fields to check dedup behavior."""
    query = """
    query ($urlname: ID!, $status: [String!]) {
      proNetwork(urlname: $urlname) {
        eventsSearch(input: { first: 200, filter: { status: $status } }) {
          totalCount
          edges {
            node {
              id
              title
              dateTime
              eventUrl
              group { id name urlname }
              networkEvent {
                id
                title
                groupCount
                rsvpCount
                status
                isAnnounced
              }
            }
          }
        }
      }
    }
    """
    return client._graphql(query, {"urlname": pro_network, "status": [status]})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Introspect Meetup GraphQL API schema for network event types")
    parser.add_argument("--config", default="/config/config.yml", help="Path to config YAML file")
    parser.add_argument("--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument(
        "--types",
        nargs="*",
        default=None,
        help="Specific types to introspect (default: ProNetwork, NetworkEvent, Event, NetworkEventsFilter, EventStatus)",
    )
    parser.add_argument("--query-network-events", action="store_true", help="Also try the proNetwork.networkEvents query")
    parser.add_argument("--query-events-search", action="store_true", help="Also run eventsSearch and show sample results")
    parser.add_argument("--all", action="store_true", help="Run all introspection queries and live queries")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run schema introspection queries."""
    args = parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")

    config = Config.from_yaml(args.config)
    client = MeetupClient(config)

    default_types = [
        "ProNetwork",
        "NetworkEvent",
        "NetworkEventsFilter",
        "Event",
        "EventStatus",
        "ProNetworkEventsSearchInput",
        "ProNetworkEventsSearchFilter",
        "ProNetworkEventsSearchConnection",
        "ProNetworkEventsSearchConnectionEdge",
    ]
    types_to_query = args.types if args.types is not None else default_types

    print("Meetup GraphQL Schema Introspection")
    print(f"Pro Network: {config.meetup_pro_network}")
    print(f"Types to introspect: {', '.join(types_to_query)}")

    for type_name in types_to_query:
        result = introspect_type(client, type_name)
        print_type_result(type_name, result)

    if args.query_network_events or args.all:
        print(f"\n{'=' * 70}")
        print("Live Query: proNetwork.networkEvents")
        print("=" * 70)
        result = run_network_events_query(client, config.meetup_pro_network)
        if "errors" in result:
            print(f"  Errors: {json.dumps(result['errors'], indent=2)}")
        data = result.get("data", {})
        network = data.get("proNetwork", {})
        network_events = network.get("networkEvents")
        if network_events:
            print(f"  Total count: {network_events.get('totalCount', 'N/A')}")
            edges = network_events.get("edges", [])
            for edge in edges:
                node = edge.get("node", {})
                print(f"  Event: {node.get('id')} - {node.get('title')}")
                group = node.get("group") or {}
                print(f"    Group: {group.get('urlname', 'N/A')}")
                print(f"    URL: {node.get('eventUrl', 'N/A')}")
        else:
            print("  networkEvents field not available or returned null")
            print(f"  Full response: {json.dumps(data, indent=2)}")

    if args.query_events_search or args.all:
        for status in ["UPCOMING", "PAST"]:
            print(f"\n{'=' * 70}")
            print(f"Live Query: proNetwork.eventsSearch (status={status}, with networkEvent)")
            print("=" * 70)
            result = run_events_search_network_fields(client, config.meetup_pro_network, status=status)
            if "errors" in result:
                print(f"  Errors: {json.dumps(result['errors'], indent=2)}")
            data = result.get("data", {})
            network = data.get("proNetwork", {})
            events_search = network.get("eventsSearch", {})
            total = events_search.get("totalCount", "N/A")
            edges = events_search.get("edges", [])
            print(f"  Total count: {total}")
            print(f"  Events returned: {len(edges)}")

            # Track duplicate event IDs to verify API dedup behavior
            seen_ids: dict[str, list[str]] = {}
            network_event_count = 0

            for edge in edges:
                node = edge.get("node", {})
                event_id = node.get("id", "N/A")
                group = node.get("group") or {}
                group_urlname = group.get("urlname", "N/A")
                net_event = node.get("networkEvent")

                seen_ids.setdefault(event_id, []).append(group_urlname)

                if net_event:
                    network_event_count += 1
                    print(f"\n  Event: {event_id} - {node.get('title')}")
                    print(f"    Date: {node.get('dateTime', 'N/A')}")
                    print(f"    Group: {group_urlname}")
                    print(f"    URL: {node.get('eventUrl', 'N/A')}")
                    print("    ** NETWORK EVENT **")
                    print(f"    Network Event ID: {net_event.get('id')}")
                    print(f"    Group Count: {net_event.get('groupCount')}")
                    print(f"    RSVP Count: {net_event.get('rsvpCount')}")
                    print(f"    Status: {net_event.get('status')}")
                    print(f"    Announced: {net_event.get('isAnnounced')}")

            # Summary
            print(f"\n  --- Summary ({status}) ---")
            print(f"  Total events: {len(edges)}")
            print(f"  Network events: {network_event_count}")
            print(f"  Non-network events: {len(edges) - network_event_count}")

            duplicates = {eid: groups for eid, groups in seen_ids.items() if len(groups) > 1}
            if duplicates:
                print(f"\n  DUPLICATE IDs found ({len(duplicates)}):")
                for eid, groups in duplicates.items():
                    print(f"    Event {eid} appears {len(groups)} times: {', '.join(groups)}")
            else:
                print("  No duplicate event IDs — API returns deduplicated results")

    print(f"\n{'=' * 70}")
    print("Done")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())

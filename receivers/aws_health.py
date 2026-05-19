#!/usr/bin/env python3
"""
receivers/aws_health.py — AWS Health receiver for PowerTrace.

Pulls EC2 events from the AWS Health API and converts them to InfraEvent
objects that correlate.py can consume. Output is written to
sample_data/events.json in the format correlate.py expects.

Usage:
    python receivers/aws_health.py
    python receivers/aws_health.py --region us-east-1 --days 3 --output my_events.json
    python receivers/aws_health.py --dry-run   # prints events without writing file
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [aws_health] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("powertrace.aws_health")

# ── Event type mapping ─────────────────────────────────────────────────────────
#
# AWS Health event type codes use a structured naming convention:
#   AWS_{SERVICE}_{CATEGORY}_{DESCRIPTION}
# We map them to the PowerTrace EventType literals that correlate.py understands.
# Unknown codes fall back to "host_degradation" — the broadest physical event type.

_AWS_TYPE_MAP: dict[str, str] = {
    # Hardware/host events
    "AWS_EC2_HARDWARE_PERFORMANCE_DEGRADATION":  "host_degradation",
    "AWS_EC2_INSTANCE_HARDWARE_MAINTENANCE":     "host_degradation",
    "AWS_EC2_HOST_MAINTENANCE_SCHEDULED":        "host_degradation",
    "AWS_EC2_INSTANCE_RETIREMENT_SCHEDULED":     "host_degradation",
    "AWS_EC2_INSTANCE_STOP_SCHEDULED":           "host_degradation",
    "AWS_EC2_INSTANCE_REBOOT_SCHEDULED":         "host_degradation",
    # Network events map to host_degradation — no network-specific type yet
    "AWS_EC2_NETWORK_CONNECTIVITY_ISSUE":        "host_degradation",
    "AWS_EC2_OPERATIONAL_ISSUE":                 "host_degradation",
    # Power / thermal events (surfaced indirectly by AWS)
    "AWS_EC2_POWER_ISSUE":                       "host_degradation",
    "AWS_EC2_COOLING_ISSUE":                     "host_degradation",
}

_AWS_TYPE_FALLBACK = "host_degradation"

# AWS Health statusCode → PowerTrace severity.
# "upcoming" events are low severity — they haven't happened yet.
# "open" events are actively affecting the instance (high/critical).
# "closed" events have resolved (medium — included for historical completeness).

_AWS_STATUS_SEVERITY: dict[str, str] = {
    "open":     "critical",
    "upcoming": "low",
    "closed":   "medium",
}

_AWS_STATUS_SEVERITY_FALLBACK = "high"


# ── Core logic ─────────────────────────────────────────────────────────────────

def _map_event_type(aws_type_code: str) -> str:
    return _AWS_TYPE_MAP.get(aws_type_code.upper(), _AWS_TYPE_FALLBACK)


def _map_severity(aws_status_code: str) -> str:
    return _AWS_STATUS_SEVERITY.get(aws_status_code.lower(), _AWS_STATUS_SEVERITY_FALLBACK)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def fetch_aws_health_events(
    region: str,
    lookback_days: int,
) -> list[dict]:
    """
    Calls describe_events to list EC2 events in the last `lookback_days` days,
    then describe_event_details for the full description of each event.

    AWS Health API is only available in us-east-1 regardless of which region's
    events you're querying. The `regions` filter in the request controls which
    AWS region's events are returned.

    Returns a list of raw dicts combining event summary + detail.
    Returns [] on any API error — callers should check the logs.
    """
    # AWS Health is a global service — endpoint must be us-east-1
    try:
        client = boto3.client("health", region_name="us-east-1")
    except Exception as exc:
        log.error("Failed to create boto3 Health client: %s", exc)
        return []

    start_time = _utc_now() - timedelta(days=lookback_days)

    # ── Step 1: List events ────────────────────────────────────────────────────

    events: list[dict] = []
    paginator = client.get_paginator("describe_events")

    try:
        pages = paginator.paginate(
            filter={
                "services": ["EC2"],
                "regions": [region],
                "startTimes": [{"from": start_time}],
            }
        )
        for page in pages:
            events.extend(page.get("events", []))
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        msg  = exc.response["Error"]["Message"]
        log.error("AWS Health describe_events failed (%s): %s", code, msg)
        return []
    except BotoCoreError as exc:
        log.error("AWS Health describe_events connection error: %s", exc)
        return []

    if not events:
        log.info("No EC2 events found in %s for the last %d day(s).", region, lookback_days)
        return []

    log.info("Found %d event(s) from AWS Health, fetching details...", len(events))

    # ── Step 2: Fetch event details (descriptions) ────────────────────────────
    # describe_event_details accepts up to 10 ARNs per call.

    arns = [e["arn"] for e in events if "arn" in e]
    details_by_arn: dict[str, dict] = {}

    for i in range(0, len(arns), 10):
        batch = arns[i : i + 10]
        try:
            resp = client.describe_event_details(eventArns=batch)
            for detail in resp.get("successfulSet", []):
                arn = detail["event"]["arn"]
                details_by_arn[arn] = detail
            for failure in resp.get("failedSet", []):
                log.warning(
                    "describe_event_details failed for ARN %s: %s",
                    failure.get("arn"),
                    failure.get("errorMessage"),
                )
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            msg  = exc.response["Error"]["Message"]
            log.warning(
                "describe_event_details batch %d–%d failed (%s): %s — "
                "using summary data only for this batch.",
                i, i + len(batch), code, msg,
            )
        except BotoCoreError as exc:
            log.warning(
                "describe_event_details connection error for batch %d–%d: %s — "
                "using summary data only.",
                i, i + len(batch), exc,
            )

    # Merge summary + detail for each event
    merged: list[dict] = []
    for event in events:
        arn = event.get("arn", "")
        detail = details_by_arn.get(arn, {})
        merged.append({"summary": event, "detail": detail})

    return merged


def _extract_affected_instances(detail: dict) -> list[str]:
    """
    Returns affected instance IDs from event detail entities.
    AWS populates these in describe_event_details → entityList.
    Falls back to an empty list if unavailable.
    """
    entities = detail.get("eventDescription", {})
    # describe_event_details doesn't return entities directly — those come from
    # describe_affected_entities. We use the structured detail when available.
    # For now, return empty so callers use a fallback device_id.
    return []


def convert_to_infra_events(
    raw_events: list[dict],
    region: str,
    fallback_device_id: str = "aws-instance-unknown",
) -> list[dict]:
    """
    Converts raw AWS Health API response dicts to the InfraEvent dict format
    that correlate.py parses. Returns a list of plain dicts (not Pydantic objects)
    so they serialize cleanly to JSON.

    Each dict matches the InfraEvent schema:
        id, timestamp, source, type, severity, device_id, raw_message, metadata

    Events that cannot be mapped are logged and skipped.
    """
    results: list[dict] = []

    for raw in raw_events:
        summary = raw.get("summary", {})
        detail  = raw.get("detail", {})

        arn           = summary.get("arn", "")
        type_code     = summary.get("eventTypeCode", "")
        status_code   = summary.get("statusCode", "")
        service       = summary.get("service", "")
        event_region  = summary.get("region", region)
        az            = summary.get("availabilityZone", "")
        start_time    = summary.get("startTime")  # datetime object from boto3

        # AWS boto3 returns datetime objects with tzinfo; convert to ISO string
        if isinstance(start_time, datetime):
            if start_time.tzinfo is None:
                # boto3 occasionally returns naive datetimes for older events
                start_time = start_time.replace(tzinfo=timezone.utc)
            timestamp_str = start_time.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        elif isinstance(start_time, str):
            timestamp_str = start_time
        else:
            log.warning("Event ARN %r has no startTime — skipping.", arn)
            continue

        # Description: prefer the latestDescription from event detail
        description = (
            detail.get("eventDescription", {}).get("latestDescription")
            or f"{service} {type_code} ({status_code})"
        )

        # Map AWS type → PowerTrace type
        powertrace_type = _map_event_type(type_code)
        if powertrace_type == _AWS_TYPE_FALLBACK and type_code not in _AWS_TYPE_MAP:
            log.debug("Unknown event type code %r — mapped to %r.", type_code, _AWS_TYPE_FALLBACK)

        # Map AWS status → PowerTrace severity
        powertrace_severity = _map_severity(status_code)

        # device_id: use ARN as a stable identifier scoped to the event.
        # Callers that have describe_affected_entities output can enrich this
        # with the actual instance ID before writing to events.json.
        device_id = arn if arn else fallback_device_id

        # Retrieve instance type from event metadata if available
        # (present in some event types via eventScopeCode + metadata)
        instance_type = summary.get("eventScopeCode", "")

        event_dict = {
            "id":          arn or f"aws_health_{len(results)}",
            "timestamp":   timestamp_str,
            "source":      "aws_health",
            "type":        powertrace_type,
            "severity":    powertrace_severity,
            "device_id":   device_id,
            "raw_message": description,
            "metadata": {
                "region":         event_region,
                "availability_zone": az,
                "event_type_code": type_code,
                "status_code":     status_code,
                "instance_type":   instance_type,
            },
        }

        results.append(event_dict)
        log.debug("Converted event %r → type=%r severity=%r", arn, powertrace_type, powertrace_severity)

    return results


def fetch_affected_instances(
    arns: list[str],
) -> dict[str, list[str]]:
    """
    Calls describe_affected_entities to get the EC2 instance IDs affected by
    each event ARN. Returns {arn: [instance_id, ...]} mapping.

    Requires the health:DescribeAffectedEntities permission. Logs and returns {}
    on failure so the caller can fall back to the ARN as device_id.
    """
    if not arns:
        return {}

    try:
        client = boto3.client("health", region_name="us-east-1")
    except Exception as exc:
        log.warning("Failed to create boto3 client for affected entities: %s", exc)
        return {}

    arn_to_instances: dict[str, list[str]] = {}

    for i in range(0, len(arns), 10):
        batch = arns[i : i + 10]
        try:
            paginator = client.get_paginator("describe_affected_entities")
            pages = paginator.paginate(filter={"eventArns": batch})
            for page in pages:
                for entity in page.get("entities", []):
                    event_arn    = entity.get("eventArn", "")
                    instance_id  = entity.get("entityValue", "")
                    if event_arn and instance_id:
                        arn_to_instances.setdefault(event_arn, []).append(instance_id)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            msg  = exc.response["Error"]["Message"]
            log.warning(
                "describe_affected_entities failed for batch %d–%d (%s): %s — "
                "device_id will use ARN as fallback.",
                i, i + len(batch), code, msg,
            )
        except BotoCoreError as exc:
            log.warning(
                "describe_affected_entities connection error batch %d–%d: %s",
                i, i + len(batch), exc,
            )

    return arn_to_instances


def enrich_device_ids(
    events: list[dict],
    arn_to_instances: dict[str, list[str]],
) -> list[dict]:
    """
    Replaces the ARN-based device_id with the actual EC2 instance ID when
    describe_affected_entities returned a match.

    If an event ARN maps to multiple instances, the event is duplicated once
    per instance — each instance gets its own InfraEvent row so topology
    resolution in correlate.py works correctly.
    """
    enriched: list[dict] = []

    for evt in events:
        arn       = evt["id"]
        instances = arn_to_instances.get(arn)

        if not instances:
            # No entity data — keep ARN as device_id
            enriched.append(evt)
            continue

        for idx, instance_id in enumerate(instances):
            copy = dict(evt)
            copy["device_id"] = instance_id
            # Keep id unique when an event spans multiple instances
            if idx > 0:
                copy["id"] = f"{arn}#{instance_id}"
            enriched.append(copy)

    return enriched


def _read_json_safe(path: Path, empty: dict) -> dict:
    """Reads a JSON file; returns `empty` if the file doesn't exist or is corrupt."""
    if not path.exists():
        return empty
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            log.warning("%s is not a JSON object — treating as empty.", path)
            return empty
        return data
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read %s (%s) — treating as empty.", path, exc)
        return empty


def merge_events_file(new_events: list[dict], output_path: Path) -> int:
    """
    Merges new_events into the existing events file, deduplicating by event id.
    Returns the number of new events actually added (skipping duplicates).
    Creates the file if it doesn't exist.
    """
    existing = _read_json_safe(output_path, {"events": []})
    existing_list: list[dict] = existing.get("events") or []

    seen_ids: set[str] = {e["id"] for e in existing_list if isinstance(e, dict) and "id" in e}
    added = 0

    for evt in new_events:
        if evt["id"] not in seen_ids:
            existing_list.append(evt)
            seen_ids.add(evt["id"])
            added += 1
        else:
            log.debug("Skipping duplicate event id %r.", evt["id"])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump({"events": existing_list}, f, indent=2, default=str)

    log.info(
        "Events file: %d new event(s) added, %d total in %s.",
        added, len(existing_list), output_path,
    )
    return added


def generate_mock_events(instance_id: str) -> list[dict]:
    """
    Returns two realistic AWS Health events for testing without AWS credentials.

    Timestamps are fixed to 2024-01-15T14:31:55Z and 14:31:58Z so they fall
    within the 60-second lookback window of the sample_data trace anomaly
    (first anomalous metric: 14:32:11Z). This makes the mock events appear
    in correlate.py's causal chain when run against the sample data.
    """
    return [
        {
            "id": (
                "arn:aws:health:us-east-1::event/EC2"
                "/AWS_EC2_HARDWARE_PERFORMANCE_DEGRADATION/mock-hw-001"
            ),
            "timestamp": "2024-01-15T14:31:55.000Z",
            "source": "aws_health",
            "type": "host_degradation",
            "severity": "critical",
            "device_id": instance_id,
            "raw_message": (
                "We detected degraded hardware performance for your Amazon EC2 instance. "
                "This event may result in increased latency or instance unavailability."
            ),
            "metadata": {
                "region": "us-east-1",
                "availability_zone": "us-east-1a",
                "event_type_code": "AWS_EC2_HARDWARE_PERFORMANCE_DEGRADATION",
                "status_code": "open",
                "instance_type": "p4d.24xlarge",
            },
        },
        {
            "id": (
                "arn:aws:health:us-east-1::event/EC2"
                "/AWS_EC2_INSTANCE_STOP_SCHEDULED/mock-stop-001"
            ),
            "timestamp": "2024-01-15T14:31:58.000Z",
            "source": "aws_health",
            "type": "host_degradation",
            "severity": "low",
            "device_id": instance_id,
            "raw_message": (
                "Your Amazon EC2 instance has been scheduled for a stop due to "
                "underlying host maintenance. AWS will attempt to start your instance "
                "on a different host after the maintenance window."
            ),
            "metadata": {
                "region": "us-east-1",
                "availability_zone": "us-east-1a",
                "event_type_code": "AWS_EC2_INSTANCE_STOP_SCHEDULED",
                "status_code": "upcoming",
                "instance_type": "p4d.24xlarge",
            },
        },
    ]


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    _default_output = str(Path(__file__).parent.parent / "sample_data" / "events.json")

    parser = argparse.ArgumentParser(
        description=(
            "PowerTrace AWS Health receiver — fetches EC2 health events and "
            "merges them into the InfraEvent JSON file for correlate.py."
        )
    )
    parser.add_argument(
        "--region", default="us-east-1",
        help="AWS region to query for EC2 events (default: us-east-1).",
    )
    parser.add_argument(
        "--days", type=int, default=7,
        help="Look back this many days for events (default: 7).",
    )
    parser.add_argument(
        "--instance", default="i-0abc123def456", metavar="INSTANCE_ID",
        help=(
            "EC2 instance ID used as device_id in mock mode (default: i-0abc123def456, "
            "which matches the sample_data topology)."
        ),
    )
    parser.add_argument(
        "--output", default=_default_output,
        help=f"Events JSON file to merge into (default: {_default_output}).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print events to stdout instead of writing to file.",
    )
    parser.add_argument(
        "--mock", action="store_true",
        help=(
            "Generate two synthetic AWS Health events instead of calling AWS. "
            "Events are timestamped to match the sample_data anomaly window "
            "so correlate.py will include them in the causal chain."
        ),
    )
    parser.add_argument(
        "--skip-entities", action="store_true",
        help=(
            "Skip the describe_affected_entities call. "
            "device_id will be the event ARN rather than the instance ID. "
            "Use if your IAM role lacks health:DescribeAffectedEntities."
        ),
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Log verbosity (default: INFO).",
    )
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log_level)

    # ── Fetch or generate ──────────────────────────────────────────────────────

    if args.mock:
        log.info("Running in mock mode — no AWS calls will be made.")
        events = generate_mock_events(args.instance)
        log.info("Generated %d mock event(s) for instance %s.", len(events), args.instance)
    else:
        log.info(
            "Fetching AWS Health EC2 events — region: %s, lookback: %d day(s).",
            args.region, args.days,
        )
        raw = fetch_aws_health_events(region=args.region, lookback_days=args.days)

        if not raw:
            log.info("No events returned.")
            events = []
        else:
            events = convert_to_infra_events(raw, region=args.region)

            if events and not args.skip_entities:
                arns = [e["id"] for e in events]
                arn_to_instances = fetch_affected_instances(arns)
                if arn_to_instances:
                    events = enrich_device_ids(events, arn_to_instances)
                    log.info(
                        "Enriched device_id for %d ARN(s) with instance IDs.",
                        len(arn_to_instances),
                    )
                else:
                    log.info(
                        "No affected-entity data — device_id will be the event ARN."
                    )

        log.info("Converted %d event(s) total.", len(events))

    # ── Output ─────────────────────────────────────────────────────────────────

    output_path = Path(args.output)

    if args.dry_run:
        print(json.dumps({"events": events}, indent=2, default=str))
    else:
        n_added = merge_events_file(events, output_path)
        print(
            f"Done. {n_added} new event(s) merged into {output_path}.\n"
            f"Run: python correlate.py --events {output_path} "
            f"--traces sample_data/traces.json --topology sample_data/topology.json"
        )


if __name__ == "__main__":
    main()

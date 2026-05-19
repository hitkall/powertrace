#!/usr/bin/env python3
"""
receivers/cloudwatch.py — CloudWatch receiver for PowerTrace.

Polls EC2 instance metrics from CloudWatch, converts them to ServiceMetric and
InfraEvent objects, and merges the results into sample_data/traces.json and
sample_data/events.json respectively.

ServiceMetric note: p50/p95/p99_latency_ms are set to 0.0 because CloudWatch
does not expose application-level latency. correlate.py guards p99 anomaly
detection with `p99_base > 0`, so 0.0 silently disables that signal while
leaving error_rate and throughput anomaly detection fully active.

throughput_rps represents NetworkIn bytes-per-second — not actual request rate.
It serves as an activity proxy: a sharp drop signals the instance went quiet.

Usage:
    python receivers/cloudwatch.py --instance i-0abc123 --region us-east-1
    python receivers/cloudwatch.py --instance i-0abc123 --lookback 60 --dry-run
    python receivers/cloudwatch.py --mock   # generates fake data, no AWS needed
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [cloudwatch] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("powertrace.cloudwatch")

# ── Constants ──────────────────────────────────────────────────────────────────

_PERIOD_SECONDS = 60
_EC2_NAMESPACE = "AWS/EC2"

# CPU threshold and run-length required to emit a cpu_power_cap event.
# Three consecutive 60-second datapoints above this level is a sustained
# condition worth flagging, not a momentary spike.
_CPU_HIGH_THRESHOLD = 95.0
_CPU_CONSECUTIVE_REQUIRED = 3

# Default file paths relative to the project root (parent of receivers/).
_PROJECT_ROOT = Path(__file__).parent.parent
_DEFAULT_EVENTS_PATH = _PROJECT_ROOT / "sample_data" / "events.json"
_DEFAULT_TRACES_PATH = _PROJECT_ROOT / "sample_data" / "traces.json"

# Metrics to fetch and how to aggregate each over a 60-second period.
# "statistic" must be one of: Average, Minimum, Maximum, Sum, SampleCount.
_METRICS: list[dict] = [
    {"name": "CPUUtilization",          "statistic": "Average", "unit": "Percent"},
    {"name": "StatusCheckFailed",        "statistic": "Maximum", "unit": "Count"},
    {"name": "StatusCheckFailed_System", "statistic": "Maximum", "unit": "Count"},
    {"name": "StatusCheckFailed_Instance","statistic": "Maximum","unit": "Count"},
    {"name": "NetworkIn",               "statistic": "Sum",     "unit": "Bytes"},
    {"name": "NetworkOut",              "statistic": "Sum",     "unit": "Bytes"},
]


# ── Timestamp helper ───────────────────────────────────────────────────────────

def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _epoch(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


# ── Layer 1: Fetch raw CloudWatch datapoints ───────────────────────────────────

def fetch_cloudwatch_metrics(
    instance_id: str,
    region: str,
    lookback_minutes: int,
) -> dict[str, list[dict]]:
    """
    Calls GetMetricStatistics for each metric in _METRICS and returns a dict:
        {metric_name: [{"timestamp": datetime, "value": float}, ...]}

    Datapoints are sorted chronologically. Metrics that return no data (not
    emitted by this instance type) are stored as empty lists — not errors.
    Any API error for a single metric is logged and skipped; other metrics
    continue to be fetched.
    """
    try:
        client = boto3.client("cloudwatch", region_name=region)
    except Exception as exc:
        log.error("Failed to create boto3 CloudWatch client: %s", exc)
        return {}

    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=lookback_minutes)

    results: dict[str, list[dict]] = {}

    for metric_cfg in _METRICS:
        metric_name = metric_cfg["name"]
        statistic   = metric_cfg["statistic"]

        try:
            resp = client.get_metric_statistics(
                Namespace=_EC2_NAMESPACE,
                MetricName=metric_name,
                Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                StartTime=start_time,
                EndTime=end_time,
                Period=_PERIOD_SECONDS,
                Statistics=[statistic],
            )
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            msg  = exc.response["Error"]["Message"]
            # InvalidParameterCombination is common for metrics not supported
            # on certain instance types — treat as "no data" not a hard error.
            if code in ("InvalidParameterCombination", "InvalidParameterValue"):
                log.info(
                    "Metric %s not available for %s (%s) — skipping.",
                    metric_name, instance_id, code,
                )
            else:
                log.warning(
                    "GetMetricStatistics failed for %s on %s (%s): %s — skipping.",
                    metric_name, instance_id, code, msg,
                )
            results[metric_name] = []
            continue
        except BotoCoreError as exc:
            log.warning(
                "Connection error fetching %s for %s: %s — skipping.",
                metric_name, instance_id, exc,
            )
            results[metric_name] = []
            continue

        datapoints = resp.get("Datapoints", [])

        if not datapoints:
            log.info("No datapoints for metric %s on %s.", metric_name, instance_id)
            results[metric_name] = []
            continue

        parsed: list[dict] = []
        for dp in datapoints:
            ts = dp.get("Timestamp")
            if ts is None:
                continue
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            value = dp.get(statistic, 0.0)
            parsed.append({"timestamp": ts, "value": float(value)})

        results[metric_name] = sorted(parsed, key=lambda p: p["timestamp"])
        log.info(
            "Fetched %d datapoint(s) for %s on %s.",
            len(results[metric_name]), metric_name, instance_id,
        )

    return results


# ── Layer 2a: Convert metric timeseries → ServiceMetric dicts ─────────────────

def datapoints_to_service_metrics(
    metric_data: dict[str, list[dict]],
    instance_id: str,
    service_name: str,
) -> list[dict]:
    """
    Aligns all metric timeseries on their timestamps and produces one
    ServiceMetric dict per unique timestamp.

    p50/p95/p99_latency_ms are 0.0 — CloudWatch does not provide
    application-level latency. See module docstring for details.

    throughput_rps = NetworkIn bytes / 60 seconds (bytes-per-second proxy).
    error_rate_percent = 1.0 if StatusCheckFailed > 0, else 0.0.
    """
    # Collect all timestamps across all available metrics
    all_timestamps: set[datetime] = set()
    for points in metric_data.values():
        for pt in points:
            all_timestamps.add(pt["timestamp"])

    if not all_timestamps:
        return []

    # Build per-metric lookup for O(1) access per timestamp
    def _lookup(name: str) -> dict[datetime, float]:
        return {pt["timestamp"]: pt["value"] for pt in metric_data.get(name, [])}

    cpu_by_ts          = _lookup("CPUUtilization")
    status_by_ts       = _lookup("StatusCheckFailed")
    net_in_by_ts       = _lookup("NetworkIn")

    results: list[dict] = []
    for ts in sorted(all_timestamps):
        status_failed = status_by_ts.get(ts, 0.0)
        net_in_bytes  = net_in_by_ts.get(ts, 0.0)

        results.append({
            "service_name":      service_name,
            "timestamp":         _iso(ts),
            # CloudWatch has no application latency — 0.0 disables p99 anomaly
            # detection in correlate.py without breaking Pydantic validation.
            "p50_latency_ms":    0.0,
            "p95_latency_ms":    0.0,
            "p99_latency_ms":    0.0,
            "error_rate_percent": 1.0 if status_failed > 0 else 0.0,
            # NetworkIn bytes/second used as activity proxy, not true RPS.
            "throughput_rps":    round(net_in_bytes / _PERIOD_SECONDS, 4),
            "node_id":           instance_id,
        })

    log.info(
        "Produced %d ServiceMetric rows for service %r on %s.",
        len(results), service_name, instance_id,
    )
    return results


# ── Layer 2b: Detect InfraEvents from metric anomalies ─────────────────────────

def detect_events_from_metrics(
    metric_data: dict[str, list[dict]],
    instance_id: str,
) -> list[dict]:
    """
    Scans the fetched metric timeseries and emits InfraEvent dicts for:

    1. StatusCheckFailed > 0       → host_degradation / critical
    2. StatusCheckFailed_System > 0 → host_degradation / critical
       (system-level failures indicate underlying hardware problems)
    3. CPUUtilization > 95% for 3+ consecutive 60s datapoints
       → cpu_power_cap / medium (emitted once per sustained episode)

    Event IDs are deterministic (instance + metric + epoch) so re-running the
    receiver for overlapping windows doesn't duplicate events in the output file.
    """
    events: list[dict] = []

    # ── Status check failures ──────────────────────────────────────────────────

    for ts, value in [
        (pt["timestamp"], pt["value"])
        for pt in metric_data.get("StatusCheckFailed", [])
    ]:
        if value <= 0:
            continue
        events.append({
            "id":          f"cwm_{instance_id}_status_failed_{_epoch(ts)}",
            "timestamp":   _iso(ts),
            "source":      "aws_health",
            "type":        "host_degradation",
            "severity":    "critical",
            "device_id":   instance_id,
            "raw_message": f"EC2 status check failed on {instance_id}",
            "metadata": {
                "metric":      "StatusCheckFailed",
                "value":       value,
                "instance_id": instance_id,
            },
        })

    for ts, value in [
        (pt["timestamp"], pt["value"])
        for pt in metric_data.get("StatusCheckFailed_System", [])
    ]:
        if value <= 0:
            continue
        events.append({
            "id":          f"cwm_{instance_id}_status_system_{_epoch(ts)}",
            "timestamp":   _iso(ts),
            "source":      "aws_health",
            "type":        "host_degradation",
            "severity":    "critical",
            "device_id":   instance_id,
            "raw_message": (
                f"EC2 system status check failed on {instance_id}. "
                "System-level failures indicate a problem with the underlying "
                "AWS infrastructure hosting this instance — not the instance itself."
            ),
            "metadata": {
                "metric":       "StatusCheckFailed_System",
                "value":        value,
                "instance_id":  instance_id,
                "check_scope":  "system",
                "note":         (
                    "System status check failure indicates underlying hardware "
                    "or network infrastructure issue outside the instance."
                ),
            },
        })

    # ── CPU sustained high (3 consecutive > 95%) ──────────────────────────────

    cpu_points = sorted(
        ((pt["timestamp"], pt["value"]) for pt in metric_data.get("CPUUtilization", [])),
        key=lambda x: x[0],
    )

    consecutive = 0
    in_episode  = False  # True once we've emitted for the current high-CPU run

    for ts, value in cpu_points:
        if value > _CPU_HIGH_THRESHOLD:
            if not in_episode:
                consecutive += 1
                if consecutive >= _CPU_CONSECUTIVE_REQUIRED:
                    events.append({
                        "id":          f"cwm_{instance_id}_cpu_cap_{_epoch(ts)}",
                        "timestamp":   _iso(ts),
                        "source":      "aws_health",
                        "type":        "cpu_power_cap",
                        "severity":    "medium",
                        "device_id":   instance_id,
                        "raw_message": (
                            f"CPU utilization on {instance_id} exceeded "
                            f"{_CPU_HIGH_THRESHOLD:.0f}% for "
                            f"{_CPU_CONSECUTIVE_REQUIRED} consecutive "
                            f"{_PERIOD_SECONDS}s periods "
                            f"(current: {value:.1f}%)"
                        ),
                        "metadata": {
                            "metric":              "CPUUtilization",
                            "value_percent":       value,
                            "threshold_percent":   _CPU_HIGH_THRESHOLD,
                            "consecutive_periods": _CPU_CONSECUTIVE_REQUIRED,
                            "period_seconds":      _PERIOD_SECONDS,
                            "instance_id":         instance_id,
                        },
                    })
                    in_episode = True
        else:
            # CPU dropped below threshold — reset for the next potential episode
            consecutive = 0
            in_episode  = False

    if events:
        log.info(
            "Detected %d InfraEvent(s) from metric anomalies on %s.",
            len(events), instance_id,
        )
    else:
        log.info("No metric anomalies detected on %s.", instance_id)

    return events


# ── Layer 3: Merge into existing JSON files ────────────────────────────────────

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


def merge_events_file(new_events: list[dict], path: Path) -> int:
    """
    Appends new_events into the events file at path, deduplicating by event id.
    Returns the number of new events actually added (skipping duplicates).
    """
    existing = _read_json_safe(path, {"events": []})
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

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump({"events": existing_list}, f, indent=2, default=str)

    log.info(
        "Events file: %d new event(s) added, %d total in %s.",
        added, len(existing_list), path,
    )
    return added


def merge_traces_file(new_metrics: list[dict], path: Path) -> int:
    """
    Appends new_metrics into the traces file at path, deduplicating by
    (service_name, timestamp). Returns the number of new rows added.
    """
    existing = _read_json_safe(path, {"service_metrics": [], "spans": []})
    existing_metrics: list[dict] = existing.get("service_metrics") or []
    existing_spans: list[dict]   = existing.get("spans") or []

    # Build a dedup key from the two fields that uniquely identify a datapoint
    def _key(m: dict) -> tuple[str, str]:
        return (m.get("service_name", ""), m.get("timestamp", ""))

    seen_keys: set[tuple] = {_key(m) for m in existing_metrics if isinstance(m, dict)}
    added = 0

    for metric in new_metrics:
        k = _key(metric)
        if k not in seen_keys:
            existing_metrics.append(metric)
            seen_keys.add(k)
            added += 1
        else:
            log.debug("Skipping duplicate metric at %s for %s.", k[1], k[0])

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(
            {"service_metrics": existing_metrics, "spans": existing_spans},
            f, indent=2, default=str,
        )

    log.info(
        "Traces file: %d new metric row(s) added, %d total in %s.",
        added, len(existing_metrics), path,
    )
    return added


# ── Mock data generator ────────────────────────────────────────────────────────

def generate_mock_data(
    instance_id: str,
    base_time: Optional[datetime] = None,
) -> dict[str, list[dict]]:
    """
    Generates 10 realistic CloudWatch datapoints without real AWS access.
    Scenario: normal operation → 3+ consecutive high-CPU minutes with a
    system status check failure mid-spike → gradual recovery.

    base_time anchors the mock timeseries. Defaults to 2024-01-15T14:23:00Z
    so the CPU spike and status-check failure land within the 60-second lookback
    window of the sample_data trace anomaly (first anomalous metric: 14:32:11Z).

    With the default base:
      T+0 … T+8 = normal / spike / recovery
      T+9 = 14:32:00Z  →  the 3rd-consecutive CPU period and status failure
            land at T+7 = 14:30:00Z, still within the --lag 60 default ... but
            the mock scenario with base=14:23:00 puts the status failure at
            14:30:00Z, 131 seconds before the anomaly.  Pass
            base_time=datetime(2024,1,15,14,31,0,tzinfo=timezone.utc) to tighten
            the window for a single-receiver test.
    """
    if base_time is None:
        # Default: anchor so the spike (T+3 through T+5) falls inside the
        # sample_data anomaly window (14:32:01–14:33:48).
        # T+3 = 14:31:50, T+5 = 14:32:10 — just before the first trace anomaly.
        base_time = datetime(2024, 1, 15, 14, 28, 50, tzinfo=timezone.utc)

    base = base_time.replace(second=0, microsecond=0)
    timestamps = [base + timedelta(minutes=i) for i in range(10)]

    # (CPUUtil%, StatusFailed, StatusSystem, NetworkIn_bytes, NetworkOut_bytes)
    scenario = [
        (45.2, 0, 0, 524_288,   180_000),  # T+0  normal
        (48.7, 0, 0, 491_520,   165_000),  # T+1  normal
        (51.3, 0, 0, 540_672,   195_000),  # T+2  normal
        (96.1, 0, 0, 786_432,   210_000),  # T+3  CPU spike starts
        (97.8, 0, 0, 819_200,   220_000),  # T+4  still high
        (98.5, 1, 1, 131_072,    40_000),  # T+5  3rd consecutive → cpu_power_cap event
                                           #       StatusCheckFailed + System → 2 events
        (97.2, 1, 0, 114_688,    35_000),  # T+6  still degraded (status still failing)
        (62.4, 0, 0, 376_832,   130_000),  # T+7  recovering
        (49.1, 0, 0, 458_752,   158_000),  # T+8  recovered
        (46.8, 0, 0, 475_136,   162_000),  # T+9  stable
    ]

    cpu_points, status_points, status_sys_points = [], [], []
    status_inst_points, net_in_points, net_out_points = [], [], []

    for ts, cpu, status, status_sys, net_in, net_out in zip(timestamps, *zip(*scenario)):
        cpu_points.append(        {"timestamp": ts, "value": cpu})
        status_points.append(     {"timestamp": ts, "value": float(status)})
        status_sys_points.append( {"timestamp": ts, "value": float(status_sys)})
        status_inst_points.append({"timestamp": ts, "value": 0.0})  # not triggered
        net_in_points.append(     {"timestamp": ts, "value": float(net_in)})
        net_out_points.append(    {"timestamp": ts, "value": float(net_out)})

    log.info("Generated 10 mock datapoints for instance %s.", instance_id)

    return {
        "CPUUtilization":           cpu_points,
        "StatusCheckFailed":         status_points,
        "StatusCheckFailed_System":  status_sys_points,
        "StatusCheckFailed_Instance":status_inst_points,
        "NetworkIn":                 net_in_points,
        "NetworkOut":                net_out_points,
    }


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "PowerTrace CloudWatch receiver — polls EC2 metrics and writes "
            "ServiceMetric + InfraEvent JSON for correlate.py."
        )
    )
    parser.add_argument(
        "--instance", default=None, metavar="INSTANCE_ID",
        help=(
            "EC2 instance ID to poll (e.g. i-0abc123def456). "
            "Required unless --mock is set."
        ),
    )
    parser.add_argument(
        "--region", default="us-east-1",
        help="AWS region of the instance (default: us-east-1).",
    )
    parser.add_argument(
        "--lookback", type=int, default=120, metavar="MINUTES",
        help="How many minutes of metrics to fetch (default: 120).",
    )
    parser.add_argument(
        "--service-name", default=None, metavar="NAME",
        help=(
            "Service name to tag ServiceMetric rows with. "
            "Falls back to SERVICE_NAME env var, then 'unknown-service'."
        ),
    )
    parser.add_argument(
        "--events-out", default=str(_DEFAULT_EVENTS_PATH), metavar="PATH",
        help=f"Events JSON file to merge into (default: {_DEFAULT_EVENTS_PATH}).",
    )
    parser.add_argument(
        "--traces-out", default=str(_DEFAULT_TRACES_PATH), metavar="PATH",
        help=f"Traces JSON file to merge into (default: {_DEFAULT_TRACES_PATH}).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print output JSON to stdout instead of writing files.",
    )
    parser.add_argument(
        "--mock", action="store_true",
        help=(
            "Generate synthetic data instead of calling AWS. "
            "Uses --instance value if given, otherwise 'i-mock000000000'."
        ),
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
    )
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log_level)

    # Resolve instance ID.
    # With no arguments at all, default to mock mode so the receiver is
    # immediately useful without AWS credentials.
    instance_id: str
    if args.instance:
        instance_id = args.instance
        if not args.mock and not args.dry_run:
            pass  # real AWS call — instance_id is required and present
    else:
        # No --instance given: auto-enable mock mode
        if not args.mock:
            log.info(
                "No --instance given — running in mock mode. "
                "Pass --instance INSTANCE_ID to poll real CloudWatch metrics."
            )
            args.mock = True
        instance_id = "i-0abc123def456"
        log.info(
            "Using default mock instance %s (matches sample_data topology).",
            instance_id,
        )

    # Resolve service name: CLI → env var → default
    service_name = (
        args.service_name
        or os.environ.get("SERVICE_NAME")
        or "unknown-service"
    )
    log.info("Tagging ServiceMetric rows as service %r.", service_name)

    # ── Fetch or generate metric data ──────────────────────────────────────────

    if args.mock:
        log.info("Running in mock mode — no AWS calls will be made.")
        metric_data = generate_mock_data(instance_id)
    else:
        log.info(
            "Fetching CloudWatch metrics for %s in %s (lookback: %d min).",
            instance_id, args.region, args.lookback,
        )
        metric_data = fetch_cloudwatch_metrics(
            instance_id=instance_id,
            region=args.region,
            lookback_minutes=args.lookback,
        )

    if not any(metric_data.values()):
        log.warning(
            "No metric data returned for %s — nothing to write.", instance_id
        )
        sys.exit(0)

    # ── Convert to output types ────────────────────────────────────────────────

    service_metrics = datapoints_to_service_metrics(metric_data, instance_id, service_name)
    infra_events    = detect_events_from_metrics(metric_data, instance_id)

    # ── Emit ───────────────────────────────────────────────────────────────────

    if args.dry_run:
        print("=== ServiceMetrics ===")
        print(json.dumps({"service_metrics": service_metrics}, indent=2, default=str))
        print("\n=== InfraEvents ===")
        print(json.dumps({"events": infra_events}, indent=2, default=str))
        return

    events_path = Path(args.events_out)
    traces_path = Path(args.traces_out)

    n_events  = merge_events_file(infra_events,    events_path)
    n_metrics = merge_traces_file(service_metrics, traces_path)

    print(
        f"Done. {n_events} new event(s) → {events_path}, "
        f"{n_metrics} new metric row(s) → {traces_path}.\n"
        f"Run: python correlate.py "
        f"--events {events_path} "
        f"--traces {traces_path} "
        f"--topology sample_data/topology.json"
    )


if __name__ == "__main__":
    main()

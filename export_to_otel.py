#!/usr/bin/env python3
"""
export_to_otel.py — Push PowerTrace data into the local OTel stack.

Flow:
  1. Load sample_data/events.json + sample_data/traces.json
  2. Run correlation engine (same as `main.py simulate`) to get incidents
  3. Shift all 2024-01-15 timestamps → "now - 45 min" so data appears
     in Grafana's "last 3 hours" window
  4. POST OTLP JSON (no protobuf) to localhost:4318:
       /v1/metrics  — all ServiceMetric fields as Gauge data points
       /v1/traces   — each incident as a root span; causal events as child spans
       /v1/logs     — each InfraEvent as a log record
  5. Push Grafana native annotations for each InfraEvent (coloured by severity)
  6. Print a summary of everything sent

Usage:
  python export_to_otel.py [--otel-endpoint URL] [--grafana-url URL]
                            [--grafana-user USER] [--grafana-pass PASS]
                            [--dry-run]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("export_to_otel")

# ── Constants ──────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).parent
_EVENTS_PATH   = _ROOT / "sample_data" / "events.json"
_TRACES_PATH   = _ROOT / "sample_data" / "traces.json"
_TOPOLOGY_PATH = _ROOT / "sample_data" / "topology.json"

_DEFAULT_OTEL     = "http://localhost:4318"
_DEFAULT_GRAFANA  = "http://localhost:3000"
_DEFAULT_GF_USER  = "admin"
_DEFAULT_GF_PASS  = "powertrace"

# Incident start in the sample data (UTC)
_SAMPLE_INCIDENT_START = datetime(2024, 1, 15, 14, 32, 1, tzinfo=timezone.utc)
# How far back from "now" we want the incident to appear in Grafana
_TARGET_LAG_SECONDS = 45 * 60  # 45 minutes

_SEVERITY_COLORS = {
    "critical": "red",
    "high":     "orange",
    "medium":   "yellow",
    "low":      "blue",
}


# ── Module loader (mirrors main.py) ────────────────────────────────────────────
def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Timestamp helpers ──────────────────────────────────────────────────────────
def _compute_shift() -> float:
    """
    Return the number of seconds to ADD to every sample timestamp so the
    incident lands at (now - _TARGET_LAG_SECONDS).
    """
    now = datetime.now(tz=timezone.utc).timestamp()
    target_start = now - _TARGET_LAG_SECONDS
    return target_start - _SAMPLE_INCIDENT_START.timestamp()


def _shift_ts(iso_str: str, shift: float) -> datetime:
    """Parse an ISO-8601 string, apply shift, return aware datetime."""
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return datetime.fromtimestamp(dt.timestamp() + shift, tz=timezone.utc)


def _to_unix_nano(dt: datetime) -> str:
    """Return Unix nanoseconds as a string (OTLP JSON requires string for int64)."""
    return str(int(dt.timestamp() * 1_000_000_000))


def _new_trace_id() -> str:
    return uuid.uuid4().hex  # 32 hex chars (128-bit)


def _new_span_id() -> str:
    return uuid.uuid4().hex[:16]  # 16 hex chars


# ── HTTP helpers ───────────────────────────────────────────────────────────────
def _post_otlp(endpoint: str, path: str, payload: dict, dry_run: bool) -> bool:
    url = endpoint.rstrip("/") + path
    if dry_run:
        log.info("[DRY-RUN] Would POST %d bytes to %s", len(json.dumps(payload)), url)
        return True
    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code not in (200, 202):
            log.warning("POST %s → HTTP %d: %s", url, resp.status_code, resp.text[:200])
            return False
        return True
    except requests.exceptions.ConnectionError:
        log.error("Cannot reach OTel Collector at %s — is the stack running? (docker compose up -d)", url)
        return False
    except Exception as exc:
        log.error("POST %s failed: %s", url, exc)
        return False


# ── OTLP payload builders ──────────────────────────────────────────────────────
def _build_metrics_payload(metrics: list[dict], shift: float) -> dict:
    """
    Build a single OTLP JSON /v1/metrics payload for all ServiceMetric records.

    Each record contributes one data point per numeric field.
    The metric name follows the pattern: powertrace_<field>.
    """
    # Group data points by metric name
    gauge_map: dict[str, list[dict]] = {
        "powertrace_p99_latency_ms":         [],
        "powertrace_error_rate_percent":      [],
        "powertrace_throughput_rps":          [],
        "powertrace_cpu_utilization_percent": [],
        "powertrace_status_check_failed":     [],
        "powertrace_network_in_bps":          [],
        "powertrace_network_out_bps":         [],
    }

    field_map = {
        "p99_latency_ms":         "powertrace_p99_latency_ms",
        "error_rate_percent":     "powertrace_error_rate_percent",
        "throughput_rps":         "powertrace_throughput_rps",
        "cpu_utilization_percent":"powertrace_cpu_utilization_percent",
        "status_check_failed":    "powertrace_status_check_failed",
        "network_in_bps":         "powertrace_network_in_bps",
        "network_out_bps":        "powertrace_network_out_bps",
    }

    for m in metrics:
        shifted_dt = _shift_ts(m["timestamp"], shift)
        ts_nano = _to_unix_nano(shifted_dt)

        base_attrs = [
            {"key": "service_name",  "value": {"stringValue": m.get("service_name", "unknown")}},
            {"key": "instance_id",   "value": {"stringValue": m.get("instance_id", "unknown")}},
        ]

        for field, metric_name in field_map.items():
            val = m.get(field)
            if val is None:
                continue
            try:
                fval = float(val)
            except (TypeError, ValueError):
                continue

            gauge_map.setdefault(metric_name, []).append({
                "timeUnixNano": ts_nano,
                "asDouble": fval,
                "attributes": base_attrs,
            })

    scope_metrics = []
    for metric_name, data_points in gauge_map.items():
        if not data_points:
            continue
        scope_metrics.append({
            "name": metric_name,
            "gauge": {"dataPoints": data_points},
        })

    return {
        "resourceMetrics": [{
            "resource": {
                "attributes": [
                    {"key": "service.name",    "value": {"stringValue": "powertrace"}},
                    {"key": "service.version", "value": {"stringValue": "1.0.0"}},
                ]
            },
            "scopeMetrics": [{
                "scope": {"name": "powertrace.export_to_otel"},
                "metrics": scope_metrics,
            }],
        }]
    }


def _build_correlation_metrics_payload(incidents: list, shift: float) -> dict:
    """
    Build OTLP metrics payload for the correlation summary gauges:
      powertrace_incidents_total
      powertrace_correlation_confidence
      powertrace_mttc_seconds
    """
    now_nano = _to_unix_nano(datetime.now(tz=timezone.utc))

    total_incidents = len(incidents)
    avg_confidence  = (
        sum(inc.get("confidence", 0) for inc in incidents) / total_incidents
        if total_incidents else 0.0
    )
    # MTTC = average seconds from first causal event to first anomaly
    mttc_values = []
    for inc in incidents:
        chain = inc.get("causal_chain", [])
        anomaly_start = inc.get("anomaly_window", {}).get("start")
        if chain and anomaly_start:
            try:
                first_event_ts = _shift_ts(chain[0]["timestamp"], shift).timestamp()
                anomaly_ts     = _shift_ts(anomaly_start, shift).timestamp()
                mttc_values.append(abs(anomaly_ts - first_event_ts))
            except Exception:
                pass
    avg_mttc = sum(mttc_values) / len(mttc_values) if mttc_values else 0.0

    metrics = [
        {"name": "powertrace_incidents_total",         "value": float(total_incidents)},
        {"name": "powertrace_correlation_confidence",  "value": avg_confidence},
        {"name": "powertrace_mttc_seconds",            "value": avg_mttc},
    ]

    scope_metrics = []
    for m in metrics:
        scope_metrics.append({
            "name": m["name"],
            "gauge": {
                "dataPoints": [{
                    "timeUnixNano": now_nano,
                    "asDouble": m["value"],
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "powertrace"}}
                    ],
                }]
            },
        })

    return {
        "resourceMetrics": [{
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "powertrace"}},
                ]
            },
            "scopeMetrics": [{
                "scope": {"name": "powertrace.correlation_summary"},
                "metrics": scope_metrics,
            }],
        }]
    }


def _build_traces_payload(incidents: list, shift: float) -> dict:
    """
    Build OTLP JSON /v1/traces payload.

    Each incident → one root span (duration = anomaly window).
    Each causal chain event → one child span under the root.
    """
    resource_spans = []

    for inc in incidents:
        trace_id  = _new_trace_id()
        root_span_id = _new_span_id()

        anomaly = inc.get("anomaly_window", {})
        a_start = anomaly.get("start")
        a_end   = anomaly.get("end")

        if a_start:
            start_dt = _shift_ts(a_start, shift)
            end_dt   = _shift_ts(a_end, shift) if a_end else datetime.now(tz=timezone.utc)
        else:
            start_dt = datetime.now(tz=timezone.utc)
            end_dt   = start_dt

        service = inc.get("service", "unknown")
        confidence = inc.get("confidence", 0.0)

        root_span = {
            "traceId": trace_id,
            "spanId":  root_span_id,
            "name":    f"incident/{service}",
            "kind":    2,  # SPAN_KIND_SERVER
            "startTimeUnixNano": _to_unix_nano(start_dt),
            "endTimeUnixNano":   _to_unix_nano(end_dt),
            "attributes": [
                {"key": "service.name",         "value": {"stringValue": service}},
                {"key": "correlation.confidence","value": {"doubleValue": confidence}},
                {"key": "incident.p99_peak_ms",  "value": {"doubleValue": inc.get("impact", {}).get("p99_peak_ms", 0.0)}},
            ],
            "status": {"code": 2},  # STATUS_CODE_ERROR
        }

        child_spans = []
        for evt in inc.get("causal_chain", []):
            evt_dt    = _shift_ts(evt["timestamp"], shift)
            child_end = datetime.fromtimestamp(evt_dt.timestamp() + 1.0, tz=timezone.utc)
            child_spans.append({
                "traceId":   trace_id,
                "spanId":    _new_span_id(),
                "parentSpanId": root_span_id,
                "name":      f"event/{evt.get('event_type', 'unknown')}",
                "kind":      3,  # SPAN_KIND_CLIENT
                "startTimeUnixNano": _to_unix_nano(evt_dt),
                "endTimeUnixNano":   _to_unix_nano(child_end),
                "attributes": [
                    {"key": "device_id",   "value": {"stringValue": evt.get("device_id", "")}},
                    {"key": "severity",    "value": {"stringValue": evt.get("severity", "")}},
                    {"key": "source",      "value": {"stringValue": evt.get("source", "")}},
                    {"key": "event_type",  "value": {"stringValue": evt.get("event_type", "")}},
                    {"key": "confidence",  "value": {"doubleValue": evt.get("confidence", 0.0)}},
                ],
                "status": {"code": 2},
            })

        spans = [root_span] + child_spans
        resource_spans.append({
            "resource": {
                "attributes": [
                    {"key": "service.name",    "value": {"stringValue": "powertrace-correlator"}},
                    {"key": "service.version", "value": {"stringValue": "1.0.0"}},
                ]
            },
            "scopeSpans": [{
                "scope": {"name": "powertrace.correlation"},
                "spans": spans,
            }],
        })

    return {"resourceSpans": resource_spans}


def _build_logs_payload(events: list, shift: float) -> dict:
    """
    Build OTLP JSON /v1/logs payload — one log record per InfraEvent.
    """
    log_records = []

    severity_number_map = {
        "critical": 21,   # FATAL
        "high":     17,   # ERROR
        "medium":   13,   # WARN
        "low":       9,   # INFO
    }

    for evt in events:
        ts_dt = _shift_ts(evt["timestamp"], shift)
        sev   = evt.get("severity", "low")

        log_records.append({
            "timeUnixNano":         _to_unix_nano(ts_dt),
            "observedTimeUnixNano": _to_unix_nano(datetime.now(tz=timezone.utc)),
            "severityNumber":       severity_number_map.get(sev, 9),
            "severityText":         sev.upper(),
            "body": {
                "stringValue": f"[{evt.get('event_type', 'unknown')}] {evt.get('device_id', '')} — {evt.get('description', '')}"
            },
            "attributes": [
                {"key": "event.id",      "value": {"stringValue": evt.get("id", "")}},
                {"key": "device_id",     "value": {"stringValue": evt.get("device_id", "")}},
                {"key": "source",        "value": {"stringValue": evt.get("source", "")}},
                {"key": "severity",      "value": {"stringValue": sev}},
                {"key": "event_type",    "value": {"stringValue": evt.get("event_type", "")}},
                {"key": "region",        "value": {"stringValue": evt.get("region", "")}},
            ],
        })

    return {
        "resourceLogs": [{
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "powertrace"}},
                ]
            },
            "scopeLogs": [{
                "scope": {"name": "powertrace.infra_events"},
                "logRecords": log_records,
            }],
        }]
    }


# ── Grafana annotation pusher ──────────────────────────────────────────────────
def _push_grafana_annotations(
    events: list,
    shift: float,
    grafana_url: str,
    gf_user: str,
    gf_pass: str,
    dry_run: bool,
) -> int:
    """
    Push one annotation per InfraEvent to Grafana's native annotation store.
    Returns the count of successfully pushed annotations.
    """
    pushed = 0
    base_url = grafana_url.rstrip("/")
    url = f"{base_url}/api/annotations"
    auth = (gf_user, gf_pass)

    for evt in events:
        shifted_dt = _shift_ts(evt["timestamp"], shift)
        ts_ms = int(shifted_dt.timestamp() * 1000)

        sev   = evt.get("severity", "low")
        color = _SEVERITY_COLORS.get(sev, "blue")
        tags  = [f"severity:{sev}", "powertrace", evt.get("event_type") or evt.get("type", "unknown")]

        text  = (
            f"<b>[{sev.upper()}]</b> {evt.get('event_type', '?')} "
            f"on <code>{evt.get('device_id', '?')}</code><br>"
            f"source: {evt.get('source', '?')}"
        )

        payload = {
            "time":     ts_ms,
            "timeEnd":  ts_ms,
            "tags":     tags,
            "text":     text,
        }

        if dry_run:
            log.info("[DRY-RUN] Would push annotation: %s", tags)
            pushed += 1
            continue

        try:
            resp = requests.post(url, json=payload, auth=auth, timeout=10)
            if resp.status_code == 200:
                pushed += 1
            else:
                log.warning("Annotation push HTTP %d: %s", resp.status_code, resp.text[:100])
        except requests.exceptions.ConnectionError:
            log.error("Cannot reach Grafana at %s — is the stack running?", grafana_url)
            break
        except Exception as exc:
            log.error("Annotation push failed: %s", exc)

    return pushed


# ── Correlation runner ─────────────────────────────────────────────────────────
def _run_correlation() -> tuple[list, list, list]:
    """
    Load events/traces/topology, run correlate.py, return
    (events_list, metrics_list, incidents_list).

    incidents_list is a list of dicts with keys:
      service, confidence, anomaly_window, causal_chain, impact
    """
    correlate = _load_module("correlate", _ROOT / "correlate.py")

    # Load raw JSON
    def _rj(p):
        with open(p) as f:
            return json.load(f)

    raw = _rj(_EVENTS_PATH)
    events_list   = raw.get("events", raw) if isinstance(raw, dict) else raw
    traces_raw    = _rj(_TRACES_PATH)
    # traces.json may use "service_metrics" (correlate.py schema) or "metrics"
    metrics_list  = (
        traces_raw.get("service_metrics")
        or traces_raw.get("metrics")
        or (traces_raw if isinstance(traces_raw, list) else [])
    )
    topology_raw  = _rj(_TOPOLOGY_PATH)

    # Parse through correlate.py's own parsers (same path as main())
    parsed_events   = correlate.parse_events({"events": events_list})
    parsed_traces   = correlate.parse_traces(traces_raw)  # keeps service_metrics key
    topology_maps   = correlate.parse_topology(topology_raw)
    topology_index  = correlate.build_topology_index(topology_maps)

    anomaly_windows, _skip = correlate.detect_anomaly_windows(
        parsed_traces.service_metrics,
        baseline_lookback=600.0,
    )

    incidents = []
    for window in anomaly_windows:
        incident = correlate.build_causal_chain(
            parsed_events,
            window,
            topology_index,
            max_lag=60.0,
            correlation_window=5.0,
            min_confidence=0.0,    # include everything for export
            gpu_rate=None,
        )
        if incident is None:
            continue

        # Serialise CausalChainEvent objects to plain dicts
        chain_dicts = []
        for ce in incident.causal_chain:
            chain_dicts.append({
                "timestamp":  ce.timestamp.isoformat(),
                "event_type": ce.type,
                "device_id":  ce.device_id,
                "severity":   ce.severity or "low",
                "source":     ce.source,
                "confidence": ce.confidence or 0.0,
            })

        impact_dict = incident.impact.model_dump() if incident.impact else {}

        # anomaly_window dict has keys: service, window_start, window_end, ...
        incidents.append({
            "service":    window["service"],
            "confidence": incident.overall_confidence,
            "anomaly_window": {
                "start": window["window_start"].isoformat(),
                "end":   window["window_end"].isoformat(),
            },
            "causal_chain": chain_dicts,
            "impact": impact_dict,
        })

    return events_list, metrics_list, incidents


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Export PowerTrace correlation data to OTel Collector + Grafana annotations"
    )
    ap.add_argument("--otel-endpoint", default=_DEFAULT_OTEL,
                    help=f"OTel Collector HTTP endpoint (default: {_DEFAULT_OTEL})")
    ap.add_argument("--grafana-url",  default=_DEFAULT_GRAFANA,
                    help=f"Grafana base URL (default: {_DEFAULT_GRAFANA})")
    ap.add_argument("--grafana-user", default=_DEFAULT_GF_USER)
    ap.add_argument("--grafana-pass", default=_DEFAULT_GF_PASS)
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be sent without actually sending")
    ap.add_argument("--log-level", default="INFO",
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = ap.parse_args()

    logging.getLogger().setLevel(args.log_level)

    # ── Step 1: Run correlation ────────────────────────────────────────────────
    log.info("Running correlation engine…")
    try:
        events_list, metrics_list, incidents = _run_correlation()
    except FileNotFoundError as exc:
        log.error("%s", exc)
        log.error("Run `python main.py simulate` first to generate sample data.")
        sys.exit(1)

    log.info("  %d events | %d metric points | %d incident(s) found",
             len(events_list), len(metrics_list), len(incidents))

    # ── Step 2: Compute timestamp shift ───────────────────────────────────────
    shift = _compute_shift()
    shifted_start = datetime.fromtimestamp(
        _SAMPLE_INCIDENT_START.timestamp() + shift, tz=timezone.utc
    )
    log.info("  Timestamp shift: %+.0fs  (incident appears at %s)",
             shift, shifted_start.strftime("%H:%M:%S UTC"))

    counters = {
        "metric_points": 0,
        "correlation_metrics": 0,
        "traces": 0,
        "log_records": 0,
        "annotations": 0,
        "errors": 0,
    }

    # ── Step 3: Push metrics ───────────────────────────────────────────────────
    log.info("Sending metrics to OTel Collector…")
    metrics_payload = _build_metrics_payload(metrics_list, shift)
    n_points = sum(
        len(m["gauge"]["dataPoints"])
        for rm in metrics_payload["resourceMetrics"]
        for sm in rm["scopeMetrics"]
        for m in sm["metrics"]
    )
    if _post_otlp(args.otel_endpoint, "/v1/metrics", metrics_payload, args.dry_run):
        counters["metric_points"] += n_points
        log.info("  ✓ %d metric data points sent", n_points)
    else:
        counters["errors"] += 1

    # Correlation summary metrics
    corr_payload = _build_correlation_metrics_payload(incidents, shift)
    if _post_otlp(args.otel_endpoint, "/v1/metrics", corr_payload, args.dry_run):
        counters["correlation_metrics"] += 3
        log.info("  ✓ Correlation summary metrics sent (incidents, confidence, mttc)")
    else:
        counters["errors"] += 1

    # ── Step 4: Push traces ────────────────────────────────────────────────────
    if incidents:
        log.info("Sending traces to OTel Collector…")
        traces_payload = _build_traces_payload(incidents, shift)
        total_spans = sum(
            len(ss["spans"])
            for rs in traces_payload["resourceSpans"]
            for ss in rs["scopeSpans"]
        )
        if _post_otlp(args.otel_endpoint, "/v1/traces", traces_payload, args.dry_run):
            counters["traces"] += total_spans
            log.info("  ✓ %d spans sent (%d incident(s))", total_spans, len(incidents))
        else:
            counters["errors"] += 1
    else:
        log.info("No incidents detected — skipping trace export")

    # ── Step 5: Push logs ──────────────────────────────────────────────────────
    log.info("Sending log records to OTel Collector…")
    logs_payload = _build_logs_payload(events_list, shift)
    n_logs = sum(
        len(sl["logRecords"])
        for rl in logs_payload["resourceLogs"]
        for sl in rl["scopeLogs"]
    )
    if _post_otlp(args.otel_endpoint, "/v1/logs", logs_payload, args.dry_run):
        counters["log_records"] += n_logs
        log.info("  ✓ %d log records sent", n_logs)
    else:
        counters["errors"] += 1

    # ── Step 6: Push Grafana annotations ──────────────────────────────────────
    log.info("Pushing Grafana annotations…")
    pushed = _push_grafana_annotations(
        events_list, shift,
        args.grafana_url, args.grafana_user, args.grafana_pass,
        args.dry_run,
    )
    counters["annotations"] = pushed
    log.info("  ✓ %d annotation(s) pushed", pushed)

    # ── Summary ────────────────────────────────────────────────────────────────
    print()
    print("─" * 52)
    print("  PowerTrace → OTel export complete")
    print("─" * 52)
    print(f"  Metric data points  : {counters['metric_points']}")
    print(f"  Correlation metrics : {counters['correlation_metrics']}")
    print(f"  Trace spans         : {counters['traces']}")
    print(f"  Log records         : {counters['log_records']}")
    print(f"  Grafana annotations : {counters['annotations']}")
    if counters["errors"]:
        print(f"  Errors              : {counters['errors']}  ← check stack is running")
    print("─" * 52)
    print(f"  Grafana   → {args.grafana_url}  (admin / powertrace)")
    print(f"  Prometheus→ http://localhost:9090")
    print()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
PowerTrace correlate.py — correlates physical infrastructure events with OTel trace degradations.
"""

import argparse
import json
import sys
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple, Literal

from pydantic import BaseModel, field_validator, ValidationError

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s", stream=sys.stderr)
log = logging.getLogger("powertrace")

# ── Constants ──────────────────────────────────────────────────────────────────

SEVERITY_RANK: Dict[str, int] = {"critical": 4, "high": 3, "medium": 2, "low": 1}
PHYSICAL_SOURCES = {"pdu_snmp", "redfish", "poe_snmp", "ups"}

# Case 8: tie-breaking rule — physical-layer sources sort before software sources at equal
# timestamps. Documented here so it's traceable when causal chains are reviewed.
_SOURCE_PRIORITY: Dict[str, int] = {src: 0 for src in PHYSICAL_SOURCES}

# Case 7: lag threshold above which indirect causality is flagged
LONG_LAG_SECONDS = 30.0

# Human-readable display names for terminal output (type field → label)
EVENT_TYPE_DISPLAY: Dict[str, str] = {
    "host_degradation":   "Host degradation",
    "thermal_throttle":   "GPU thermal throttle",
    "power_cap_applied":  "GPU power cap applied",
    "voltage_sag":        "PDU voltage sag",
    "psu_failover":       "PSU failover",
    "cpu_power_cap":      "CPU power cap applied",
    "port_power_exceeded": "PoE port power exceeded",
    "ups_transfer":       "UPS battery transfer",
    "breaker_trip":       "PDU circuit breaker trip",
    "trace_degradation":  "Trace degradation",
}


# ── Case 1: ISO 8601 with timezone enforcement ─────────────────────────────────

def _parse_tz_timestamp(v: object, field_name: str = "timestamp") -> datetime:
    """
    Parses an ISO 8601 string that MUST include timezone information.
    Rejects:
      - Non-string, non-datetime inputs
      - Strings that are not valid ISO 8601
      - Valid ISO 8601 strings with no timezone offset (naive datetimes)
    Raises ValueError with a clear, actionable message on any rejection.
    """
    if isinstance(v, datetime):
        if v.tzinfo is None:
            raise ValueError(
                f"{field_name} datetime object has no timezone. "
                f"Provide a timezone-aware datetime."
            )
        return v

    if not isinstance(v, str):
        raise ValueError(
            f"{field_name} must be an ISO 8601 string, got {type(v).__name__}: {v!r}"
        )

    normalized = v.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        raise ValueError(
            f"Invalid {field_name} {v!r}: not a valid ISO 8601 datetime. "
            f"Expected format: '2024-01-15T14:32:01Z' or '2024-01-15T14:32:01+00:00'."
        )

    if dt.tzinfo is None:
        raise ValueError(
            f"Timestamp {v!r} has no timezone. "
            f"ISO 8601 with timezone is required "
            f"(e.g. '2024-01-15T14:32:01Z' or '2024-01-15T14:32:01+05:30'). "
            f"Naive timestamps are rejected to prevent silent clock-skew errors."
        )

    return dt


# ── Pydantic Models ────────────────────────────────────────────────────────────

EventSource = Literal[
    "aws_health", "azure_resource_health", "gcp_instance_health",
    "nvml", "rapl", "pdu_snmp", "redfish", "poe_snmp", "ups",
]

EventType = Literal[
    "host_degradation", "thermal_throttle", "power_cap_applied",
    "voltage_sag", "psu_failover", "cpu_power_cap",
    "port_power_exceeded", "ups_transfer", "breaker_trip",
]

EventSeverity = Literal["critical", "high", "medium", "low"]


class InfraEvent(BaseModel):
    id: str
    timestamp: datetime
    source: EventSource
    type: EventType
    severity: EventSeverity
    device_id: str
    raw_message: str
    # Case 9: accept any dict content, never require specific keys; default {} handles
    # missing or null metadata without failing the whole record.
    metadata: dict = {}

    @field_validator("timestamp", mode="before")
    @classmethod
    def parse_timestamp(cls, v):
        return _parse_tz_timestamp(v, "timestamp")


class EventsFile(BaseModel):
    events: List[InfraEvent]


class ServiceMetric(BaseModel):
    service_name: str
    timestamp: datetime
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    error_rate_percent: float
    throughput_rps: float
    node_id: Optional[str] = None

    @field_validator("timestamp", mode="before")
    @classmethod
    def parse_timestamp(cls, v):
        return _parse_tz_timestamp(v, "timestamp")


class Span(BaseModel):
    trace_id: str
    span_id: str
    service_name: str
    operation: str
    start_time: datetime
    duration_ms: float
    status: str
    attributes: dict = {}

    @field_validator("start_time", mode="before")
    @classmethod
    def parse_start_time(cls, v):
        return _parse_tz_timestamp(v, "start_time")


class TracesFile(BaseModel):
    service_metrics: List[ServiceMetric]
    spans: List[Span] = []


class TopologyMapping(BaseModel):
    physical_device_id: str
    rack_id: Optional[str] = None
    cloud_instance_id: Optional[str] = None
    kubernetes_node: Optional[str] = None
    gpus: Optional[List[str]] = None
    services: Optional[List[str]] = None
    feeds: Optional[List[str]] = None
    parent_device_id: Optional[str] = None
    asset_tag: Optional[str] = None
    ip_address: Optional[str] = None
    gpu_index: Optional[int] = None
    gpu_uuid: Optional[str] = None


class CausalChainEvent(BaseModel):
    event_id: str
    timestamp: datetime
    source: str
    type: str
    severity: Optional[str] = None       # from original event; omitted from JSON output
    device_id: str
    caused_by: Optional[str] = None
    confidence: Optional[float] = None
    lag_seconds: Optional[float] = None
    metadata: Optional[dict] = None      # for rich terminal display
    # Case 7: flagged when lag from previous event or from anomaly start exceeds LONG_LAG_SECONDS
    indirect_causality: bool = False


class ImpactSummary(BaseModel):
    p99_baseline_ms: float
    p99_peak_ms: float           # worst measured point; used in JSON output
    p99_change_percent: float    # (peak − baseline) / baseline × 100
    p99_first_anomaly_ms: float  # value at first detected anomaly; used in terminal display
    p99_first_change_percent: float
    error_rate_baseline_percent: float
    error_rate_peak_percent: float
    error_rate_first_anomaly_percent: float  # at first anomaly; terminal display
    throughput_baseline_rps: float
    throughput_peak_rps: float
    throughput_first_anomaly_rps: float      # at first anomaly; terminal display
    throughput_first_change_percent: float
    # Case 10: null when --gpu-rate not provided; never computed by guessing
    estimated_cost_impact_usd: Optional[float] = None


class Incident(BaseModel):
    id: str
    window_start: datetime
    window_end: datetime
    duration_seconds: float
    overall_confidence: float
    affected_services: List[str]
    causal_chain: List[CausalChainEvent]
    impact: ImpactSummary
    warnings: List[str] = []
    unmapped_devices: List[str] = []
    # Case 4: always emitted; consumers use this flag rather than confidence being hidden
    low_confidence: bool = False


class CorrelationReport(BaseModel):
    generated_at: datetime
    incidents: List[Incident]
    total_events_processed: int
    total_incidents_detected: int
    total_unmapped_devices: int
    total_warnings: int


# ── Layer 1: Input parsing and validation ──────────────────────────────────────

def load_json_file(path: str) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        # Case 2: clear message, no stack trace
        print(f"ERROR: File not found: {path}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in {path}: {e}", file=sys.stderr)
        sys.exit(1)

    # Case 2: reject files that are valid JSON but wrong shape (e.g. a bare array)
    if not isinstance(data, dict):
        print(
            f"ERROR: {path} must be a JSON object, got {type(data).__name__}. "
            f"Expected a top-level object with keys like "
            f"\"events\", \"service_metrics\", or \"mappings\".",
            file=sys.stderr,
        )
        sys.exit(1)

    return data


def parse_events(data: dict) -> List[InfraEvent]:
    # Use `or []` so that an explicit null value ("events": null) is treated as empty
    raw_list = data.get("events") or []
    parsed = []
    for raw in raw_list:
        try:
            parsed.append(InfraEvent.model_validate(raw))
        except ValidationError as e:
            log.warning("Skipping malformed event record: %s | raw: %s", e, raw)
        except Exception as e:
            log.warning("Skipping event record (unexpected error): %s | raw: %s", e, raw)
    return parsed


def parse_traces(data: dict) -> TracesFile:
    metrics: List[ServiceMetric] = []
    for raw in (data.get("service_metrics") or []):
        try:
            metrics.append(ServiceMetric.model_validate(raw))
        except ValidationError as e:
            log.warning("Skipping malformed service_metric: %s | raw: %s", e, raw)
        except Exception as e:
            log.warning("Skipping service_metric (unexpected error): %s | raw: %s", e, raw)

    spans: List[Span] = []
    for raw in (data.get("spans") or []):
        try:
            spans.append(Span.model_validate(raw))
        except ValidationError as e:
            log.warning("Skipping malformed span: %s | raw: %s", e, raw)
        except Exception as e:
            log.warning("Skipping span (unexpected error): %s | raw: %s", e, raw)

    return TracesFile(service_metrics=metrics, spans=spans)


def parse_topology(data: dict) -> List[TopologyMapping]:
    parsed = []
    for raw in (data.get("mappings") or []):
        try:
            parsed.append(TopologyMapping.model_validate(raw))
        except ValidationError as e:
            log.warning("Skipping malformed topology mapping: %s | raw: %s", e, raw)
        except Exception as e:
            log.warning("Skipping topology mapping (unexpected error): %s | raw: %s", e, raw)
    return parsed


# ── Layer 2: Topology index and baseline calculator ────────────────────────────

def build_topology_index(mappings: List[TopologyMapping]) -> Dict[str, List[str]]:
    """
    Builds device_id → [service_names] index. Resolves transitive chains:
    PDU feeds servers → servers run services → PDU also covers those services.
    """
    device_services: Dict[str, List[str]] = {}
    device_feeds: Dict[str, List[str]] = {}

    for m in mappings:
        did = m.physical_device_id
        svcs = list(m.services or [])

        device_services.setdefault(did, []).extend(svcs)

        if m.cloud_instance_id:
            device_services.setdefault(m.cloud_instance_id, []).extend(svcs)

        if m.gpus:
            for gpu in m.gpus:
                device_services.setdefault(gpu, []).extend(svcs)

        if m.feeds:
            device_feeds[did] = list(m.feeds)

        if m.parent_device_id:
            device_services.setdefault(m.parent_device_id, []).extend(svcs)

    # Propagate feed relationships transitively (up to 3 hops)
    for _ in range(3):
        for device, fed_devices in device_feeds.items():
            existing = set(device_services.get(device, []))
            for fed in fed_devices:
                for svc in device_services.get(fed, []):
                    if svc not in existing:
                        device_services.setdefault(device, []).append(svc)
                        existing.add(svc)

    return {k: list(set(v)) for k, v in device_services.items()}


def calculate_baseline(
    metrics: List[ServiceMetric],
    service_name: str,
    window_start: datetime,
    lookback_seconds: float,
) -> Optional[Dict]:
    """
    Rolling average baseline from data BEFORE window_start.

    Returns:
      None                        — no pre-window data at all
      {"insufficient": True, ...} — exactly 1 pre-window point; not enough for comparison
      {"p99": ..., ...}           — valid baseline from ≥2 points
    """
    start_ts = window_start.timestamp()
    cutoff_ts = start_ts - lookback_seconds

    pre_window = [
        m for m in metrics
        if m.service_name == service_name
        and cutoff_ts <= m.timestamp.timestamp() < start_ts
    ]

    n = len(pre_window)

    if n == 0:
        return None

    # Case 5: a single data point cannot establish a reliable baseline
    if n == 1:
        return {"insufficient": True, "n": 1}

    # n >= 2 guaranteed; no division-by-zero risk
    return {
        "insufficient": False,
        "p99": sum(m.p99_latency_ms for m in pre_window) / n,
        "error_rate": sum(m.error_rate_percent for m in pre_window) / n,
        "throughput": sum(m.throughput_rps for m in pre_window) / n,
        "n": n,
    }


# ── Layer 3: Anomaly detector ──────────────────────────────────────────────────

def detect_anomaly_windows(
    metrics: List[ServiceMetric],
    baseline_lookback: float,
    gap_tolerance_seconds: float = 30.0,
) -> Tuple[List[Dict], List[str]]:
    """
    Detects windows where P99 or error_rate exceeds 2x baseline.
    Returns (windows, skip_warnings).
    skip_warnings lists services that were skipped due to insufficient data (case 5).

    Uses a LOCKED baseline per service (computed once from the first clean pre-window
    period) to prevent anomaly data from contaminating the baseline during recovery.
    """
    by_service: Dict[str, List[ServiceMetric]] = {}
    for m in metrics:
        by_service.setdefault(m.service_name, []).append(m)

    windows: List[Dict] = []
    skip_warnings: List[str] = []

    for service, svc_metrics in by_service.items():
        svc_sorted = sorted(svc_metrics, key=lambda m: m.timestamp)

        # Case 5: service with only 1 total data point — flag and skip entirely
        if len(svc_sorted) < 2:
            skip_warnings.append(
                f"Service {service!r} has only {len(svc_sorted)} data point(s) — "
                f"need at least 2 to establish a baseline. "
                f"Skipping anomaly detection for this service."
            )
            continue

        # Lock the baseline at the first metric point that has ≥2 pre-window data points.
        # This prevents recovering anomaly points from contaminating the baseline, which
        # would cause the anomaly window to close prematurely.
        locked_baseline = None
        baseline_compare_from_ts = None
        single_point_flagged = False

        for m in svc_sorted:
            baseline = calculate_baseline(svc_sorted, service, m.timestamp, baseline_lookback)
            if baseline is None:
                continue
            # Case 5: single pre-window point — warn once to stderr only, keep looking
            if baseline.get("insufficient"):
                if not single_point_flagged:
                    log.warning(
                        "Service %r: only 1 pre-window data point available for baseline "
                        "at %s — skipping that comparison. "
                        "Add more historical data for reliable detection.",
                        service, m.timestamp.isoformat()
                    )
                    single_point_flagged = True
                continue
            locked_baseline = baseline
            baseline_compare_from_ts = m.timestamp.timestamp()
            break  # Baseline is locked; don't recalculate for later points

        if locked_baseline is None:
            continue

        p99_base = locked_baseline["p99"]
        err_base = locked_baseline["error_rate"]

        anomaly_points: List[Dict] = []
        for m in svc_sorted:
            if m.timestamp.timestamp() < baseline_compare_from_ts:
                continue  # skip points that precede the first usable baseline window

            p99_anomaly = p99_base > 0 and m.p99_latency_ms > 2 * p99_base
            # When baseline error rate is near zero, flag any rate above 1%
            err_anomaly = (
                m.error_rate_percent > 2 * err_base if err_base > 0
                else m.error_rate_percent > 1.0
            )

            if p99_anomaly or err_anomaly:
                anomaly_points.append({"metric": m, "baseline": locked_baseline})

        if not anomaly_points:
            continue

        # Group consecutive anomalous points into contiguous windows
        groups: List[List[Dict]] = [[anomaly_points[0]]]
        for ap in anomaly_points[1:]:
            prev_ts = groups[-1][-1]["metric"].timestamp.timestamp()
            curr_ts = ap["metric"].timestamp.timestamp()
            if curr_ts - prev_ts <= gap_tolerance_seconds:
                groups[-1].append(ap)
            else:
                groups.append([ap])

        for group in groups:
            windows.append({
                "service": service,
                "window_start": group[0]["metric"].timestamp,
                "window_end": group[-1]["metric"].timestamp,
                "anomaly_points": group,
                "baseline": locked_baseline,
            })

    return windows, skip_warnings


# ── Layer 4: Causal chain builder ──────────────────────────────────────────────

def _source_priority(source: str) -> int:
    """
    Case 8: secondary sort key for events at identical timestamps.
    Physical-layer sources (PDU, Redfish, UPS, PoE) sort before software sources.
    Returns 0 for physical, 1 for software.
    """
    return _SOURCE_PRIORITY.get(source, 1)


def _edge_confidence(
    evt: InfraEvent,
    prev_evt: Optional[InfraEvent],
    service: str,
    topology_index: Dict[str, List[str]],
    anomaly_start_ts: float,
    max_lag: float,
) -> Tuple[float, bool]:
    """
    Scores a single edge in the causal chain using an additive penalty model.
    Returns (score, indirect_causality).

    Base score: 0.90.  Only penalties reduce it — no upward rewards.
    indirect_causality is True when lag > LONG_LAG_SECONDS (case 7).
    """
    score = 0.90
    indirect_causality = False

    # Topology: penalty for devices not linked to this service
    svc_list = topology_index.get(evt.device_id, [])
    if service not in svc_list and svc_list:
        score -= 0.08   # device exists in topology but for a different service
    elif not svc_list:
        score -= 0.20   # case 3: no topology entry at all

    # Severity: small penalty for lower-severity events
    sev = SEVERITY_RANK.get(evt.severity, 1)
    if sev == 1:    # low
        score -= 0.05
    elif sev == 2:  # medium
        score -= 0.02

    # Case 7: lag from anomaly window start > 30s — significant penalty + flag
    lag_from_anomaly = anomaly_start_ts - evt.timestamp.timestamp()
    if lag_from_anomaly > LONG_LAG_SECONDS:
        score -= 0.25
        indirect_causality = True
    elif lag_from_anomaly > 0 and max_lag > 0:
        # Graduated penalty: up to -0.06 at the far end of the lookback window
        score -= (lag_from_anomaly / max_lag) * 0.06

    # Temporal proximity to the previous event in the chain
    if prev_evt is not None:
        inter_gap = abs(evt.timestamp.timestamp() - prev_evt.timestamp.timestamp())
        if inter_gap > LONG_LAG_SECONDS:
            # Case 7: long gap between consecutive events → indirect causality
            score -= 0.25
            indirect_causality = True
        elif inter_gap > 10:
            score -= 0.05
        elif inter_gap > 30:
            score -= 0.10
        # ≤ 10s: no penalty (events are closely coupled)

    return max(0.30, min(round(score, 4), 0.99)), indirect_causality


def build_causal_chain(
    events: List[InfraEvent],
    anomaly_window: Dict,
    topology_index: Dict[str, List[str]],
    max_lag: float,
    correlation_window: float,
    min_confidence: float,
    gpu_rate: Optional[float],
) -> Optional[Incident]:
    """
    For one anomaly window: look back max_lag seconds, rank events, score the
    causal chain, and return an Incident (or None if no candidate events found).
    """
    service = anomaly_window["service"]
    anomaly_start: datetime = anomaly_window["window_start"]
    anomaly_end: datetime = anomaly_window["window_end"]
    anomaly_start_ts = anomaly_start.timestamp()

    lookback_cutoff = anomaly_start_ts - max_lag

    candidate_events = [
        e for e in events
        if lookback_cutoff <= e.timestamp.timestamp() <= anomaly_start_ts + correlation_window
    ]

    if not candidate_events:
        return None

    related_events: List[InfraEvent] = []
    unmapped_device_ids: List[str] = []
    warnings: List[str] = []

    for evt in candidate_events:
        svc_list = topology_index.get(evt.device_id, [])
        if service in svc_list:
            related_events.append(evt)
        elif not svc_list:
            # Case 3: no topology entry at all — flag it, include with scoring penalty
            unmapped_device_ids.append(evt.device_id)
            warnings.append(
                f"Device {evt.device_id!r} has no topology mapping — "
                f"included in causal chain with reduced confidence."
            )
            related_events.append(evt)
        # devices that map to other services are filtered out (not relevant to this incident)

    if not related_events:
        related_events = candidate_events

    # Case 8: sort chronologically; break equal-timestamp ties with physical-layer-first
    related_events.sort(key=lambda e: (e.timestamp, _source_priority(e.source)))

    # Score each edge
    edge_scores: List[float] = []
    indirect_flags: List[bool] = []
    prev: Optional[InfraEvent] = None
    for evt in related_events:
        score, indirect = _edge_confidence(
            evt, prev, service, topology_index, anomaly_start_ts, max_lag
        )
        edge_scores.append(score)
        indirect_flags.append(indirect)
        prev = evt

    overall_confidence = round(
        sum(edge_scores) / len(edge_scores) if edge_scores else 0.0, 2
    )

    # Build CausalChainEvent list
    chain: List[CausalChainEvent] = []
    prev_id: Optional[str] = None

    for i, (evt, edge_score, indirect) in enumerate(
        zip(related_events, edge_scores, indirect_flags)
    ):
        if i == 0:
            lag_to_prev = round(anomaly_start_ts - evt.timestamp.timestamp(), 1)
        else:
            lag_to_prev = round(
                evt.timestamp.timestamp() - related_events[i - 1].timestamp.timestamp(), 1
            )

        # Case 7: warn specifically on long inter-event gap
        if indirect and i > 0 and lag_to_prev > LONG_LAG_SECONDS:
            warnings.append(
                f"Long lag ({lag_to_prev:.0f}s) between {related_events[i-1].id!r} → "
                f"{evt.id!r}: possible indirect causality."
            )

        chain.append(CausalChainEvent(
            event_id=evt.id,
            timestamp=evt.timestamp,
            source=evt.source,
            type=evt.type,
            severity=evt.severity,
            device_id=evt.device_id,
            caused_by=prev_id,
            confidence=edge_score if i > 0 else None,
            lag_seconds=lag_to_prev,
            metadata=evt.metadata,
            indirect_causality=indirect,
        ))
        prev_id = evt.id

    # Append trace degradation as final chain entry
    anomaly_points = anomaly_window["anomaly_points"]
    first_anomaly: ServiceMetric = anomaly_points[0]["metric"]
    trace_lag = round(first_anomaly.timestamp.timestamp() - anomaly_start_ts, 1)

    # Case 7: long lag from last infra event to trace impact
    indirect_trace = trace_lag > LONG_LAG_SECONDS
    if indirect_trace:
        warnings.append(
            f"Correlation lag to trace degradation is {trace_lag:.0f}s — "
            f"possible indirect causality."
        )

    chain.append(CausalChainEvent(
        event_id=f"trace_impact_{service}",
        timestamp=first_anomaly.timestamp,
        source="otel_traces",
        type="trace_degradation",
        severity=None,
        device_id=service,
        caused_by=prev_id,
        confidence=round(overall_confidence, 2),
        lag_seconds=trace_lag,
        metadata=None,
        indirect_causality=indirect_trace,
    ))

    # Impact metrics
    baseline = anomaly_window["baseline"]
    peak_metric: ServiceMetric = max(
        anomaly_points, key=lambda ap: ap["metric"].p99_latency_ms
    )["metric"]

    p99_base = baseline["p99"]
    err_base = baseline["error_rate"]
    tput_base = baseline["throughput"]

    # Guard division-by-zero on percentage calculations
    p99_change = (
        round((peak_metric.p99_latency_ms - p99_base) / p99_base * 100, 1)
        if p99_base > 0 else 0.0
    )
    tput_change = (
        round((peak_metric.throughput_rps - tput_base) / tput_base * 100, 1)
        if tput_base > 0 else 0.0
    )

    # First-anomaly point values (used in terminal display; show when it broke, not worst)
    p99_first_change = (
        round((first_anomaly.p99_latency_ms - p99_base) / p99_base * 100, 1)
        if p99_base > 0 else 0.0
    )
    tput_first_change = (
        round((first_anomaly.throughput_rps - tput_base) / tput_base * 100, 1)
        if tput_base > 0 else 0.0
    )

    # Window start = first infra event in chain (not first anomalous trace metric)
    incident_start = chain[0].timestamp if chain else anomaly_start
    duration = round(anomaly_end.timestamp() - incident_start.timestamp(), 1)

    # Case 10: cost only when caller passed --gpu-rate; null otherwise, never guessed
    cost_usd: Optional[float] = (
        round((duration / 3600.0) * gpu_rate, 2) if gpu_rate is not None else None
    )

    impact = ImpactSummary(
        p99_baseline_ms=round(p99_base, 1),
        p99_peak_ms=peak_metric.p99_latency_ms,
        p99_change_percent=p99_change,
        p99_first_anomaly_ms=first_anomaly.p99_latency_ms,
        p99_first_change_percent=p99_first_change,
        error_rate_baseline_percent=round(err_base, 2),
        error_rate_peak_percent=peak_metric.error_rate_percent,
        error_rate_first_anomaly_percent=first_anomaly.error_rate_percent,
        throughput_baseline_rps=round(tput_base, 1),
        throughput_peak_rps=peak_metric.throughput_rps,
        throughput_first_anomaly_rps=first_anomaly.throughput_rps,
        throughput_first_change_percent=tput_first_change,
        estimated_cost_impact_usd=cost_usd,
    )

    return Incident(
        id=f"inc_{abs(hash(service)) % 1000:03d}",
        window_start=incident_start,
        window_end=anomaly_end,
        duration_seconds=duration,
        overall_confidence=overall_confidence,
        affected_services=[service],
        causal_chain=chain,
        impact=impact,
        warnings=warnings,
        unmapped_devices=list(set(unmapped_device_ids)),
        # Case 4: always emit; mark clearly rather than hiding
        low_confidence=overall_confidence < min_confidence,
    )


# ── Output renderers ───────────────────────────────────────────────────────────

def _fmt_ts(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S")


def _severity_label(severity: Optional[str]) -> str:
    if severity is None:
        return "[IMPACT]  "
    return f"[{severity.upper()}]".ljust(10)


def render_timeline(report: CorrelationReport, min_confidence: float) -> str:
    lines: List[str] = []
    sep = "═" * 62

    lines += [
        "PowerTrace Correlation Report",
        sep,
        f"Generated: {report.generated_at.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        sep,
        "",
    ]

    if not report.incidents:
        lines += ["No incidents detected above confidence threshold.", "", sep]
        return "\n".join(lines)

    for inc in report.incidents:
        svc = ", ".join(inc.affected_services)
        conf_pct = int(inc.overall_confidence * 100)

        # Header line matches README format: "INCIDENT DETECTED — service (confidence: X%)"
        header = f"INCIDENT DETECTED — {svc} (confidence: {conf_pct}%)"
        # Case 4: always shown; append low-confidence flag inline
        if inc.low_confidence:
            header += f"  [LOW CONFIDENCE — below {int(min_confidence * 100)}% threshold]"
        lines += [
            header,
            f"Window: {_fmt_ts(inc.window_start)} – {_fmt_ts(inc.window_end)}"
            f" ({int(inc.duration_seconds)} seconds)",
            "",
        ]

        # Build a lookup so "caused by" lines can show device_id instead of event_id
        event_to_device: Dict[str, str] = {
            ce.event_id: ce.device_id for ce in inc.causal_chain
        }

        lines += ["CAUSAL CHAIN"]
        for ce in inc.causal_chain:
            sev_str = _severity_label(ce.severity)
            display_name = EVENT_TYPE_DISPLAY.get(ce.type, ce.type)
            lines.append(f"  {_fmt_ts(ce.timestamp)}  {sev_str} {display_name:<28} {ce.device_id}")

            if ce.source == "otel_traces":
                imp = inc.impact
                lines += [
                    f"              P99: {imp.p99_baseline_ms:,.0f}ms → "
                    f"{imp.p99_first_anomaly_ms:,.0f}ms ({imp.p99_first_change_percent:+.0f}%)",
                    f"              Error rate: {imp.error_rate_baseline_percent}% → "
                    f"{imp.error_rate_first_anomaly_percent}%",
                    f"              Throughput: {imp.throughput_baseline_rps:,.0f} rps → "
                    f"{imp.throughput_first_anomaly_rps:,.0f} rps"
                    f" ({imp.throughput_first_change_percent:.0f}%)",
                ]
            # Case 7: indirect causality flag for non-trace events
            if ce.indirect_causality and ce.source != "otel_traces":
                lines.append("              ↳ [possible indirect causality]")

        lines.append("")

        # Compact IMPACT section matching README format
        imp = inc.impact
        root = inc.causal_chain[0] if inc.causal_chain else None
        root_desc = (
            f"{EVENT_TYPE_DISPLAY.get(root.type, root.type)} on {root.device_id}"
            if root else "unknown"
        )
        lines += ["IMPACT"]
        lines.append(f"  Duration: {int(inc.duration_seconds)} seconds")
        if imp.estimated_cost_impact_usd is not None:
            lines.append(f"  Est. cost: ${imp.estimated_cost_impact_usd:,.0f}")
        else:
            # Case 10: explicit null with guidance
            lines.append(f"  Est. cost: null  (pass --gpu-rate USD_PER_HR to compute)")
        lines.append(f"  Root cause: {root_desc}")
        lines.append("")

        if inc.unmapped_devices:
            lines += ["UNMAPPED DEVICES"]
            lines += [f"  {d}" for d in inc.unmapped_devices]
            lines.append("")

        if inc.warnings:
            lines += ["WARNINGS"]
            lines += [f"  {w}" for w in inc.warnings]
            lines.append("")

    lines.append(sep)
    return "\n".join(lines)


def render_json(report: CorrelationReport) -> str:
    def _dt(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    incidents_out = []
    for inc in report.incidents:
        incidents_out.append({
            "id": inc.id,
            "window_start": _dt(inc.window_start),
            "window_end": _dt(inc.window_end),
            "duration_seconds": inc.duration_seconds,
            "overall_confidence": inc.overall_confidence,
            "low_confidence": inc.low_confidence,
            "affected_services": inc.affected_services,
            "causal_chain": [
                {
                    "event_id": ce.event_id,
                    "timestamp": _dt(ce.timestamp),
                    "source": ce.source,
                    "type": ce.type,
                    "device_id": ce.device_id,
                    "caused_by": ce.caused_by,
                    "confidence": ce.confidence,
                    "lag_seconds": ce.lag_seconds,
                    # Case 7: included in JSON so consumers can filter on it
                    "indirect_causality": ce.indirect_causality,
                }
                for ce in inc.causal_chain
            ],
            # Case 10: model_dump includes None values, so estimated_cost_impact_usd
            # is always present in JSON — as null when --gpu-rate was not provided
            "impact": inc.impact.model_dump(),
            "warnings": inc.warnings,
            "unmapped_devices": inc.unmapped_devices,
        })

    return json.dumps(
        {
            "report": {
                "generated_at": report.generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "incidents": incidents_out,
                "summary": {
                    "total_events_processed": report.total_events_processed,
                    "total_incidents_detected": report.total_incidents_detected,
                    "total_unmapped_devices": report.total_unmapped_devices,
                    "total_warnings": report.total_warnings,
                },
            }
        },
        indent=2,
    )


def render_markdown(report: CorrelationReport, min_confidence: float) -> str:
    lines: List[str] = [
        "# PowerTrace Correlation Report",
        f"\n**Generated:** `{report.generated_at.strftime('%Y-%m-%dT%H:%M:%SZ')}`\n",
    ]

    if not report.incidents:
        lines.append("_No incidents detected above confidence threshold._")
        return "\n".join(lines)

    for inc in report.incidents:
        lines += [
            f"## Incident: {', '.join(inc.affected_services)}",
            f"\n- **Window:** {_fmt_ts(inc.window_start)} – {_fmt_ts(inc.window_end)}"
            f" ({int(inc.duration_seconds)}s)",
            f"- **Confidence:** {int(inc.overall_confidence * 100)}%",
        ]
        # Case 4: always show; mark clearly
        if inc.low_confidence:
            lines.append(
                f"- **⚠ LOW CONFIDENCE** ({int(inc.overall_confidence * 100)}% is below "
                f"the {int(min_confidence * 100)}% threshold — shown for visibility)"
            )

        lines += [
            "\n### Causal Chain\n",
            "| Time | Severity | Type | Device | Confidence | Lag | Indirect? |",
            "|------|----------|------|--------|------------|-----|-----------|",
        ]
        for ce in inc.causal_chain:
            conf_str = f"{int(ce.confidence * 100)}%" if ce.confidence is not None else "—"
            lag_str = f"{ce.lag_seconds:.1f}s" if ce.lag_seconds is not None else "—"
            sev_str = (ce.severity or "IMPACT").upper()
            # Case 7: surface indirect_causality flag in table
            indirect_str = "⚠ yes" if ce.indirect_causality else "no"
            lines.append(
                f"| {_fmt_ts(ce.timestamp)} | {sev_str} | {ce.type} | "
                f"`{ce.device_id}` | {conf_str} | {lag_str} | {indirect_str} |"
            )

        imp = inc.impact
        lines += [
            "\n### Impact\n",
            f"- **P99:** {imp.p99_baseline_ms:.0f}ms → {imp.p99_peak_ms:.0f}ms "
            f"({imp.p99_change_percent:+.0f}%)",
            f"- **Error rate:** {imp.error_rate_baseline_percent}% → "
            f"{imp.error_rate_peak_percent}%",
            f"- **Throughput:** {imp.throughput_baseline_rps:.0f} → "
            f"{imp.throughput_peak_rps:.0f} rps ({imp.throughput_first_change_percent:.0f}%)",
        ]
        # Case 10: explicit null vs computed value
        if imp.estimated_cost_impact_usd is not None:
            lines.append(f"- **Estimated cost impact:** ${imp.estimated_cost_impact_usd:.2f}")
        else:
            lines.append(
                f"- **Estimated cost impact:** `null` "
                f"(pass `--gpu-rate USD_PER_HR` to compute)"
            )

        if inc.warnings:
            lines += ["\n### Warnings\n"] + [f"- ⚠ {w}" for w in inc.warnings]

        if inc.unmapped_devices:
            lines += ["\n### Unmapped Devices\n"] + [f"- `{d}`" for d in inc.unmapped_devices]

        lines.append("")

    lines.append(
        f"\n---\n**Summary:** {report.total_incidents_detected} incident(s) | "
        f"{report.total_events_processed} events processed | "
        f"{report.total_unmapped_devices} unmapped device(s) | "
        f"{report.total_warnings} warning(s)"
    )
    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="PowerTrace — correlate physical infra events with OTel trace degradations"
    )
    parser.add_argument("--events", required=True, help="Path to events JSON file")
    parser.add_argument("--traces", required=True, help="Path to traces JSON file")
    parser.add_argument("--topology", required=True, help="Path to topology JSON file")
    parser.add_argument("--window", type=float, default=5.0, metavar="SEC",
                        help="Correlation time window in seconds (default: 5)")
    parser.add_argument("--lag", type=float, default=60.0, metavar="SEC",
                        help="Max correlation lag in seconds (default: 60)")
    parser.add_argument("--baseline", type=float, default=600.0, metavar="SEC",
                        help="Baseline lookback window in seconds (default: 600)")
    parser.add_argument("--confidence", type=float, default=0.6, metavar="FLOAT",
                        help="Min confidence threshold 0–1 (default: 0.6)")
    parser.add_argument("--output", choices=["timeline", "json", "markdown"], default="timeline",
                        help="Output format (default: timeline)")
    # Case 10: optional; output is null when omitted, not guessed
    parser.add_argument("--gpu-rate", type=float, default=None, metavar="USD_PER_HR",
                        help="GPU instance hourly rate for cost estimation (e.g. 28.50). "
                             "Omit to get null in estimated_cost_impact_usd.")
    args = parser.parse_args()

    # ── Layer 1: Parse and validate ────────────────────────────────────────────

    events_raw = load_json_file(args.events)
    traces_raw = load_json_file(args.traces)
    topology_raw = load_json_file(args.topology)

    events = parse_events(events_raw)
    traces = parse_traces(traces_raw)
    topology_mappings = parse_topology(topology_raw)

    # Case 2: clear, helpful exit messages — no Python tracebacks
    if not events:
        print(
            "ERROR: No valid events found in events file.\n"
            "  Expected format: {\"events\": [{\"id\": \"...\", \"timestamp\": \"...Z\", ...}]}\n"
            "  Ensure the file is non-empty, records are well-formed, and timestamps "
            "include a timezone (e.g. '2024-01-15T14:32:01Z').",
            file=sys.stderr,
        )
        sys.exit(1)

    if not traces.service_metrics:
        print(
            "ERROR: No valid service_metrics found in traces file.\n"
            "  Expected format: {\"service_metrics\": [{\"service_name\": \"...\", "
            "\"timestamp\": \"...Z\", ...}]}\n"
            "  Ensure the file is non-empty and records are well-formed.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not topology_mappings:
        print(
            "ERROR: No valid topology mappings found in topology file.\n"
            "  Expected format: {\"mappings\": [{\"physical_device_id\": \"...\", ...}]}\n"
            "  Cannot resolve device-to-service links without at least one mapping.",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── Layer 2: Topology index ────────────────────────────────────────────────

    topology_index = build_topology_index(topology_mappings)

    # Case 3: scan ALL events globally for unmapped devices (not just those in incident windows)
    globally_unmapped = {e.device_id for e in events if e.device_id not in topology_index}

    # ── Layer 3: Detect anomaly windows ───────────────────────────────────────

    anomaly_windows, skip_warnings = detect_anomaly_windows(traces.service_metrics, args.baseline)

    for w in skip_warnings:
        log.warning(w)

    if not anomaly_windows:
        report = CorrelationReport(
            generated_at=datetime.now(timezone.utc),
            incidents=[],
            total_events_processed=len(events),
            total_incidents_detected=0,
            total_unmapped_devices=len(globally_unmapped),
            total_warnings=len(skip_warnings),
        )
        _emit(report, args.output, args.confidence)
        return

    # ── Layer 4: Causal chains ─────────────────────────────────────────────────

    global_warnings: List[str] = list(skip_warnings)

    # Case 6: flag overlapping anomaly windows per service; never merge automatically
    for i, w1 in enumerate(anomaly_windows):
        for w2 in anomaly_windows[i + 1:]:
            if w1["service"] != w2["service"]:
                continue
            s1, e1 = w1["window_start"].timestamp(), w1["window_end"].timestamp()
            s2, e2 = w2["window_start"].timestamp(), w2["window_end"].timestamp()
            if s1 <= e2 and s2 <= e1:
                global_warnings.append(
                    f"Overlapping anomaly windows for {w1['service']!r}: "
                    f"{_fmt_ts(w1['window_start'])}–{_fmt_ts(w1['window_end'])} and "
                    f"{_fmt_ts(w2['window_start'])}–{_fmt_ts(w2['window_end'])}. "
                    f"Not merged — review each window independently."
                )

    incidents: List[Incident] = []
    for window in anomaly_windows:
        incident = build_causal_chain(
            events=events,
            anomaly_window=window,
            topology_index=topology_index,
            max_lag=args.lag,
            correlation_window=args.window,
            min_confidence=args.confidence,
            gpu_rate=args.gpu_rate,
        )
        if incident is None:
            continue
        incident.warnings = global_warnings + incident.warnings
        incidents.append(incident)

    # Case 6: also flag overlapping windows across different services (after incidents built)
    for i, inc1 in enumerate(incidents):
        for inc2 in incidents[i + 1:]:
            s1, e1 = inc1.window_start.timestamp(), inc1.window_end.timestamp()
            s2, e2 = inc2.window_start.timestamp(), inc2.window_end.timestamp()
            if s1 <= e2 and s2 <= e1:
                msg = (
                    f"Incidents for {inc1.affected_services[0]!r} and "
                    f"{inc2.affected_services[0]!r} overlap in time "
                    f"({_fmt_ts(max(inc1.window_start, inc2.window_start))}–"
                    f"{_fmt_ts(min(inc1.window_end, inc2.window_end))}). "
                    f"Not merged — may share a common root cause."
                )
                global_warnings.append(msg)
                inc1.warnings.append(msg)
                inc2.warnings.append(msg)

    all_unmapped: set = set(globally_unmapped)
    all_warnings: List[str] = list(global_warnings)
    for inc in incidents:
        all_unmapped.update(inc.unmapped_devices)
        all_warnings.extend(inc.warnings)

    report = CorrelationReport(
        generated_at=datetime.now(timezone.utc),
        incidents=incidents,
        total_events_processed=len(events),
        total_incidents_detected=len(incidents),
        total_unmapped_devices=len(all_unmapped),
        total_warnings=len(set(all_warnings)),
    )

    _emit(report, args.output, args.confidence)


def _emit(report: CorrelationReport, fmt: str, min_confidence: float) -> None:
    if fmt == "timeline":
        print(render_timeline(report, min_confidence))
    elif fmt == "json":
        print(render_json(report))
    elif fmt == "markdown":
        print(render_markdown(report, min_confidence))


if __name__ == "__main__":
    main()

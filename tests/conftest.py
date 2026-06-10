"""Shared fixtures for PowerTrace tests."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure repo root is on sys.path so imports work without installation
sys.path.insert(0, str(Path(__file__).parent.parent))


def ts(offset_seconds: int = 0, base: str = "2024-01-15T14:00:00") -> str:
    """Return an ISO-8601 UTC timestamp, optionally offset by seconds."""
    dt = datetime.fromisoformat(base).replace(tzinfo=timezone.utc)
    dt = dt + timedelta(seconds=offset_seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def make_event(
    id: str = "e1",
    device_id: str = "dev1",
    offset: int = 0,
    type: str = "voltage_sag",
    severity: str = "critical",
    source: str = "pdu_snmp",
) -> dict:
    return {
        "id": id,
        "timestamp": ts(offset),
        "source": source,
        "type": type,
        "severity": severity,
        "device_id": device_id,
        "raw_message": f"{type} on {device_id}",
    }


def make_metric(
    service: str = "api",
    offset: int = 0,
    p99: float = 100.0,
    error_rate: float = 0.1,
    throughput: float = 100.0,
) -> dict:
    return {
        "service_name": service,
        "timestamp": ts(offset),
        "p50_latency_ms": p99 * 0.5,
        "p95_latency_ms": p99 * 0.9,
        "p99_latency_ms": p99,
        "error_rate_percent": error_rate,
        "throughput_rps": throughput,
    }

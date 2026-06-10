# PowerTrace — Correlation Engine Design

This document explains how the PowerTrace correlation engine works: what it takes as input, how it detects anomalies, how it builds causal chains, and what its current limitations are.

---

## Input schemas

PowerTrace takes three JSON files as input.

### events.json — Infrastructure events

```json
{
  "events": [
    {
      "id": "evt_001",
      "timestamp": "2024-01-15T14:32:01.000Z",
      "source": "pdu_snmp",
      "type": "voltage_sag",
      "severity": "critical",
      "device_id": "PDU-B-rack-14",
      "raw_message": "PDU-B input voltage below threshold",
      "metadata": { "voltage_volts": 108.2 }
    }
  ]
}
```

**Required fields:** `id`, `timestamp` (ISO-8601 with timezone), `source`, `type`, `severity`, `device_id`, `raw_message`.

**Valid sources:** `aws_health`, `pdu_snmp`, `redfish`, `nvml`, `rapl`, `poe_snmp`, `ups`, `azure_resource_health`, `gcp_instance_health`.

**Valid event types:** `host_degradation`, `thermal_throttle`, `power_cap_applied`, `voltage_sag`, `psu_failover`, `cpu_power_cap`, `port_power_exceeded`, `ups_transfer`, `breaker_trip`.

**Timezone enforcement:** All timestamps must include timezone info. Naive timestamps (e.g., `2024-01-15T14:32:01` without `Z` or `+HH:MM`) are rejected with a clear error. This prevents silent clock-skew bugs.

---

### traces.json — Application trace metrics

```json
{
  "service_metrics": [
    {
      "service_name": "llama-inference-api",
      "timestamp": "2024-01-15T14:32:11.000Z",
      "p50_latency_ms": 890,
      "p95_latency_ms": 1650,
      "p99_latency_ms": 2100,
      "error_rate_percent": 4.3,
      "throughput_rps": 61,
      "node_id": "i-0abc123def456"
    }
  ]
}
```

These are typically derived from OTel trace data aggregated per service per scrape interval. They do not have to come from OTel directly — any source that provides time-bucketed latency and error metrics works.

---

### topology.json — Device-to-service mappings

```json
{
  "mappings": [
    {
      "physical_device_id": "PDU-B-rack-14",
      "feeds": ["server-rack-14-node-3"]
    },
    {
      "physical_device_id": "server-rack-14-node-3",
      "cloud_instance_id": "i-0abc123def456",
      "gpus": ["GPU-0"],
      "services": ["llama-inference-api"]
    }
  ]
}
```

The topology file is the critical link between physical devices and application services. Without accurate topology, correlation produces incorrect or low-confidence causal chains.

---

## Topology index

`build_topology_index()` converts the topology mappings into a flat `device_id → [service_names]` lookup. It handles:

- **Direct mappings**: `physical_device_id` → `services`
- **Cloud instance alias**: `cloud_instance_id` maps to the same services as its host
- **GPU inheritance**: each GPU ID inherits its parent server's services
- **Feed propagation**: if a PDU feeds a server, the PDU is associated with all services the server runs. This is resolved transitively up to 3 hops (PDU → server → GPU → service).

The 3-hop limit prevents runaway propagation in large topologies while covering typical GPU cluster layouts.

---

## Anomaly detection

`detect_anomaly_windows()` identifies time windows where a service's performance has significantly degraded. The algorithm:

1. Groups metrics by service name.
2. Skips services with fewer than 2 total data points (emits a warning).
3. **Locks the baseline** at the first metric point that has ≥2 pre-window data points in the lookback window. The baseline is the average P99 latency, error rate, and throughput across those pre-window points.
4. Marks a metric point as anomalous when either:
   - `p99_latency_ms > 2 × baseline_p99` (doubling threshold)
   - `error_rate_percent > 2 × baseline_error_rate` (or `> 1.0%` when baseline is near zero)
5. Groups consecutive anomalous points into contiguous windows (gap tolerance: 30s).

**Why a locked baseline?** A rolling baseline would be contaminated by the anomaly itself as recovery begins, causing the window to close prematurely. Locking prevents this.

**Threshold choice:** 2× is intentionally conservative. Infrastructure-caused degradation on GPU workloads typically produces 3–10× latency spikes, so the 2× threshold catches real incidents while avoiding false positives from normal variance.

---

## Causal chain builder

`build_causal_chain()` takes one anomaly window and builds a ranked list of infrastructure events that likely caused it.

### Candidate selection

Events are candidate causal events when they fall within the lookback window: `[anomaly_start − max_lag, anomaly_start + correlation_window]`. The default lag is 60 seconds.

### Topology filtering

Events are filtered by topology:
- Events whose `device_id` maps to the affected service are included.
- Events whose `device_id` maps to a *different* service are excluded.
- Events with *no topology entry* are included but marked as unmapped and penalized in scoring.

If no topology-matched events are found, all candidate events are included (fallback mode).

### Confidence scoring

Each event in the chain is scored using an additive penalty model. Base score: **0.90**.

Penalties applied:
| Condition | Penalty |
|-----------|---------|
| Device not in topology | −0.20 |
| Device maps to different service | −0.08 |
| Severity = low | −0.05 |
| Severity = medium | −0.02 |
| Lag from anomaly start > 30s | −0.25 (indirect_causality = True) |
| Lag from anomaly start, graduated | up to −0.06 |
| Inter-event gap > 30s | −0.25 (indirect_causality = True) |
| Inter-event gap 10–30s | −0.10 |

Score is clamped to [0.30, 0.99].

The **overall incident confidence** is the average of all edge scores. Incidents below the `--confidence` threshold (default: 0.6) are still emitted but marked `low_confidence: true`.

### Physical-layer priority

At identical timestamps, events from physical sources (`pdu_snmp`, `redfish`, `poe_snmp`, `ups`) sort before software sources. This reflects the causal direction: power events precede compute events.

### Cost estimation

Cost impact is computed as `duration_seconds / 3600 × gpu_rate_usd` when `--gpu-rate` is provided. When omitted, `estimated_cost_impact_usd` is `null` in all output formats. The engine never estimates GPU costs automatically — pricing varies too much across on-demand, reserved, spot, and owned hardware.

---

## Output formats

| Format | Description |
|--------|-------------|
| `timeline` (default) | Human-readable ASCII timeline, best for terminal review |
| `markdown` | Markdown tables, suitable for incident reports and GitHub issues |
| `json` | Machine-readable, written to `powertrace_report.json` |

See `examples/sample_report.md` and `examples/sample_report.json` for example output from the sample data.

---

## Known limitations

**Clock skew.** Physical devices may have drifted clocks (up to several seconds). The confidence scoring accounts for this through the lag tolerance window, but cannot eliminate false negatives when drift is large. Use NTP-synchronized devices.

**Topology accuracy.** Correlations are only as accurate as the topology file. A stale or wrong topology produces incorrect causal chains with no error — the engine can't know if the mappings are outdated.

**Sampling gaps.** If OTel traces are sampled at low rates, short infrastructure events may fall between sampled spans. For GPU-intensive services, increase trace sampling to at least 1 sample/second for critical paths.

**Baseline drift.** Services with naturally variable latency (e.g., batch inference with variable request complexity) may produce false positive anomaly windows if the baseline window captures high-variance behavior. Use a longer `--baseline` window.

**2× threshold.** The 2× P99 threshold is a heuristic. Services with naturally high P99 variability may need a higher multiplier. The threshold is not currently configurable per-service.

**Cloud physical layer.** For cloud deployments, PowerTrace cannot see below the hypervisor. AWS Health Events are the closest available physical signal, but they are coarse-grained (per-instance, not per-GPU) and may lag real hardware events by minutes.

**No streaming mode.** The current engine is batch-only. It processes all events and metrics from the input files at once. Streaming/incremental correlation is not yet implemented.

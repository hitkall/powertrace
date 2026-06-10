# PowerTrace and OpenTelemetry — Current Positioning

This document explains the relationship between PowerTrace and the OpenTelemetry ecosystem, what "OTel-aligned" means in this context, and what would be required to become a native OTel Collector component.

---

## What PowerTrace is today

PowerTrace is **OTel-aligned**. Specifically:

- It **exports via OTLP** (OpenTelemetry Line Protocol) over HTTP to any OTel Collector endpoint
- It **emits standard OTel signals**: metrics (Gauge data points), traces (root + child spans), and logs (log records) — all in the standard OTLP JSON format
- It **uses OTel semantic conventions** where they exist: `service.name`, `service.version`, span kinds, status codes
- The included observability stack uses **official OTel Collector Contrib** as the telemetry pipeline

What it is **not** yet:

- It is not a **native OTel Collector receiver** — there is no Go component that plugs into the collector pipeline
- It is not a **native OTel Collector processor** — correlation runs as a standalone Python process, not inside the collector
- It does not implement the **OTel Collector component interfaces** (`component.Receiver`, `component.Processor`, etc.)

The framing "OTel-native" in early versions of this project was imprecise. The current framing is: **OTel-aligned / OTLP-exporting / Collector-compatible**.

---

## How the current architecture works

```
AWS Health API ─────► receivers/aws_health.py ─────►
CloudWatch API ──────► receivers/cloudwatch.py ──────► correlate.py ──► export_to_otel.py ──► OTel Collector ──► Grafana/Tempo/Prometheus
sample_data/*.json ──►                                                                         (OTLP HTTP)
```

The Python process is responsible for:
1. Collecting signals (from AWS APIs or sample data)
2. Running the correlation engine
3. Pushing OTLP payloads to the collector over HTTP

The OTel Collector is used as a **telemetry pipeline** — it receives, batches, and routes signals to Prometheus, Tempo, and debug exporters. The collector is not doing any correlation; that happens in Python before the data reaches the collector.

---

## What a native OTel Collector architecture would look like

A proper OTel Collector integration would have two components:

### 1. A Receiver (Go)

A receiver is how the OTel Collector ingests data from external sources. For PowerTrace, this would be:

- **`awshealthreceiver`**: polls the AWS Health Events API and emits log records or metric data points into the collector pipeline
- **`cloudwatchreceiver`**: polls CloudWatch metrics for EC2 instances (this likely overlaps with the existing `awscloudwatchreceiver` in otelcol-contrib; a PR to extend it would be the right path)
- **`nvmlreceiver`** (future): polls NVIDIA GPU metrics via NVML from within the instance
- **`redfishreceiver`** (future): polls Redfish BMC endpoints via HTTP

Each receiver would implement `component.StartFunc`, `component.ShutdownFunc`, and produce `pdata.Logs` or `pdata.Metrics` output.

### 2. A Connector or Processor (Go)

The correlation logic would become an OTel Connector — a component that receives one signal type and emits another. Specifically:

- **Input**: infrastructure event logs + application trace metrics
- **Output**: correlation incident as a structured log record or span with causal chain attributes

A Connector is the right abstraction because it joins two different signal pipelines (logs from infrastructure receivers, metrics from application sources) and emits a new signal (the incident).

```yaml
# Hypothetical otelcol-contrib config
receivers:
  awshealth:
    region: us-east-1
    lookback_days: 7
  otlp:  # application traces/metrics from instrumented apps

connectors:
  powertrace_correlator:
    topology: /etc/powertrace/topology.json
    max_lag: 60s
    confidence_threshold: 0.6

exporters:
  otlp/tempo:
    endpoint: tempo:4317

service:
  pipelines:
    logs/infra:
      receivers: [awshealth]
      exporters: [powertrace_correlator]
    traces/app:
      receivers: [otlp]
      exporters: [powertrace_correlator]
    traces/incidents:
      receivers: [powertrace_correlator]
      exporters: [otlp/tempo]
```

---

## What it would take to get there

The path from the current Python POC to a native OTel Collector component:

1. **Rewrite receivers in Go** — implementing the `component.Receiver` interface. This is primarily boilerplate; the API call logic is straightforward.

2. **Rewrite the correlation engine in Go** — the Python engine in `correlate.py` would need to be ported. This is the most substantial work. Alternatively, the correlation logic could remain in Python and be called as a sidecar, but that's not idiomatic for the collector ecosystem.

3. **Define semantic conventions** — file a proposal with the OTel Semantic Conventions working group for physical infrastructure attributes (`pdu.device_id`, `host.power_state`, `gpu.thermal_state`, etc.). Without standardized attribute names, the data is not interoperable.

4. **Submit to otelcol-contrib** — receivers and connectors for general use would need to go through the opentelemetry-collector-contrib contribution process: implementation, tests, docs, a maintainer sponsor.

---

## Why start as a Python POC

The Python POC exists to:
1. Validate the correlation logic before committing to a Go rewrite
2. Demonstrate the problem and solution to the OTel community for feedback
3. Serve as a working reference implementation while Go components are built

The OTLP export format (JSON over HTTP) is intentionally simple — it works with any OTel Collector endpoint without protocol buffers or gRPC, which makes the POC easy to run locally and easy to evaluate.

---

## OTel community engagement

If you are an OTel contributor or maintainer working on receiver development, physical infrastructure observability, or semantic conventions for hardware signals — feedback on the architecture above would be valuable.

Relevant upstream issues and discussions:
- [opentelemetry-collector-contrib](https://github.com/open-telemetry/opentelemetry-collector-contrib) — for receiver contributions
- [opentelemetry-specification](https://github.com/open-telemetry/opentelemetry-specification) — for semantic convention proposals

The gap this project addresses — connecting physical infrastructure events to application traces — is not specific to AWS or GPU workloads. It applies to any environment where hardware events (power, thermal, network) causally affect application performance and those signals are not currently in the OTel pipeline.

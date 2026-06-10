# PowerTrace

**Connecting infrastructure signals to application observability — so you know exactly why your AI workload broke.**

[![CI](https://github.com/hitkall/powertrace/actions/workflows/ci.yml/badge.svg)](https://github.com/hitkall/powertrace/actions/workflows/ci.yml)

---

## What PowerTrace Is

PowerTrace is an **OTel-aligned correlation engine** that connects infrastructure events (AWS Health, CloudWatch anomalies, PDU power events, GPU thermals) to application trace degradation — producing a unified causal timeline with confidence scoring.

It exports results via OTLP to any standard OTel Collector endpoint, and includes a local demo stack with Grafana, Prometheus, and Tempo.

---

## Current Status

### Working Today
- Python correlation CLI (`main.py simulate` / `run` / `correlate`)
- Sample incident simulation — runs from a clean clone with no AWS credentials or Docker
- Infrastructure event + trace degradation correlation
- Topology-aware causal chain ranking with confidence scoring
- AWS Health receiver prototype (boto3)
- CloudWatch receiver prototype (boto3)
- OTLP export path into a local Grafana / Prometheus / Tempo / OTel Collector stack
- 63 unit tests covering the correlation engine, CLI, and export layer
- GitHub Actions CI on Python 3.11 and 3.12

### Prototype
- AWS Health event mapping and affected-entity enrichment (`receivers/aws_health.py`)
- CloudWatch infrastructure signal ingestion (`receivers/cloudwatch.py`)
- Local Grafana observability demo with provisioned dashboards and annotations

### Planned
- Native OpenTelemetry Collector receiver / processor (Go) — see [docs/OTEL_POSITIONING.md](docs/OTEL_POSITIONING.md)
- NVML / DCGM live GPU receiver
- SNMP / Redfish on-prem receiver
- RAPL CPU power cap receiver
- Azure Resource Health / GCP Instance Health receivers
- Production validation on real GPU clusters

---

## 60-Second Demo

No AWS credentials or Docker required:

```bash
git clone https://github.com/hitkall/powertrace
cd powertrace
pip3 install -r requirements.txt

# correlation engine on bundled sample data
python3 main.py simulate
python3 main.py simulate --output json
python3 main.py correlate \
  --events   sample_data/events.json \
  --traces   sample_data/traces.json \
  --topology sample_data/topology.json

# preview OTLP payloads without a running backend
python3 export_to_otel.py --dry-run
```

Sample output from `main.py simulate`:

```
INCIDENT DETECTED — llama-inference-api (confidence: 89%)
Window: 14:32:01 – 14:33:48 (107 seconds)

CAUSAL CHAIN
  14:32:01  [CRITICAL] PDU voltage sag          PDU-B-rack-14
  14:32:02  [CRITICAL] PSU failover             server-rack-14-node-3
  14:32:03  [MEDIUM]   CPU power cap applied    server-rack-14-node-3
  14:32:04  [HIGH]     GPU thermal throttle     GPU-0
  14:32:07  [HIGH]     GPU power cap applied    GPU-0
  14:32:11  [IMPACT]   Trace degradation        llama-inference-api
              P99: 530ms → 2,100ms (+296%)
              Error rate: 0.1% → 4.3%
              Throughput: 140 rps → 61 rps (-56%)

IMPACT
  Duration: 107 seconds
  Est. cost: null  (pass --gpu-rate USD_PER_HR to compute)
  Root cause: PDU voltage sag on PDU-B-rack-14
```

**With the Grafana stack:**

```bash
docker compose up -d   # Grafana, Prometheus, Tempo, OTel Collector
python3 main.py simulate
python3 export_to_otel.py
```

→ Grafana: http://localhost:3000  (admin / powertrace)
→ Prometheus: http://localhost:9090

See [docs/DEMO.md](docs/DEMO.md) for a full walkthrough including the live AWS data path.

---

## The Problem

When an AI training job slows down or crashes, two separate teams start investigating simultaneously.

The **infrastructure team** is looking at CloudWatch, AWS Health Events, or physical power dashboards. The **SRE / platform team** is looking at APM traces, error rates, and GPU metrics. Neither team can see what the other sees.

A power fluctuation that caused a GPU to thermal throttle, which caused a training job to lose a checkpoint, gets discovered in a post-mortem — not in real time.

For teams running large GPU clusters on AWS, this is expensive. A single interrupted training run on a p4d.24xlarge costs over $30/hour while stopped; multiply that by the time it takes to diagnose the root cause manually. PowerTrace shows the causality in seconds.

---

## Architecture

```
Signal Sources                    PowerTrace                  Observability Backend
──────────────                    ──────────                  ────────────────────
AWS Health Events ─────────────►
CloudWatch ───────────────────►  Python Correlation ───────► OTel Collector (OTLP)
sample_data/*.json ────────────►  Engine (correlate.py)       │
                                       │                       ├── Grafana / Tempo
                                  Topology Map                 ├── Prometheus
                             (device → service)                └── Any OTLP backend
```

**Key architectural notes:**
- The correlation engine is Python. There is no Go component yet.
- The OTel Collector is used as a telemetry pipeline, not for correlation.
- `export_to_otel.py` posts OTLP JSON (no protobuf) directly to `localhost:4318`.
- For the path to a native OTel Collector component, see [docs/OTEL_POSITIONING.md](docs/OTEL_POSITIONING.md).

---

## Quickstart

```bash
git clone https://github.com/hitkall/powertrace
cd powertrace
pip install -r requirements.txt

# Run the demo against bundled sample data
python3 main.py simulate

# With GPU cost estimate
python3 main.py simulate --gpu-rate 32.77

# Against your own files
python3 main.py correlate \
  --events   sample_data/events.json \
  --traces   sample_data/traces.json \
  --topology sample_data/topology.json

# Poll a live EC2 instance (requires AWS credentials + IAM policy)
python3 main.py run \
  --instance i-0abc123def456 \
  --region   us-east-1 \
  --service  llama-inference-api
```

---

## Configuration

```bash
python3 main.py correlate \
  --events     events.json \
  --traces     traces.json \
  --topology   topology.json \
  --window     5 \
  --lag        60 \
  --baseline   600 \
  --confidence 0.6 \
  --output     timeline \
  --gpu-rate   3.50
```

| Flag | Default | Description |
|------|---------|-------------|
| `--window` | `5` | Correlation time window in seconds |
| `--lag` | `60` | Max causal lag: how far before an anomaly to look for events |
| `--baseline` | `600` | Lookback window for baseline locking, in seconds |
| `--confidence` | `0.6` | Minimum confidence to report an incident |
| `--output` | `timeline` | Output format: `timeline`, `json`, or `markdown` |
| `--gpu-rate` | `null` | GPU hourly cost in USD — omit to leave cost estimate as `null` |

`--gpu-rate` is intentionally optional. Without it, `Est. cost` is `null` rather than a guessed number. GPU pricing varies widely across on-demand, reserved, spot, and owned hardware; PowerTrace requires you to supply the rate explicitly.

---

## Testing with Live AWS Data

See [docs/DEMO.md](docs/DEMO.md#option-c-live-aws-data-requires-aws-account) for prerequisites and step-by-step setup.

The IAM policies required are in `receivers/iam_policy_aws_health.json` and `receivers/iam_policy_cloudwatch.json`.

> **Note:** AWS Health API requires Business or Enterprise Support plan. Use `python3 main.py simulate` for a full demo without credentials.

---

## How the Correlation Works

1. Parses and validates all three input files using Pydantic (strict timezone enforcement on all timestamps)
2. Builds a topology index mapping physical device IDs to logical service names (resolves transitive PDU → server → service chains)
3. Locks a baseline per service from the first clean pre-window period (prevents anomaly data from contaminating the baseline)
4. Detects anomaly windows where P99 latency or error rate exceeds 2× baseline
5. For each anomaly window, looks back up to `--lag` seconds for preceding infrastructure events
6. Filters events by topology — only events whose device maps to the affected service are included
7. Scores each event using an additive confidence penalty model (topology match, severity, temporal proximity, source layer)
8. Emits incidents above the confidence threshold with full causal chains, impact metrics, and optional cost estimate

See [docs/DESIGN.md](docs/DESIGN.md) for a detailed walkthrough of the algorithm.

---

## Supported Signal Sources

| Source | Protocol / API | Status |
|--------|---------------|--------|
| AWS Health Events | AWS Health API (boto3) | Prototype receiver |
| CloudWatch | CloudWatch API (boto3) | Prototype receiver |
| PDU power events | SNMP | Planned |
| Server BMC | Redfish HTTP | Planned |
| GPU telemetry | NVML | Planned |
| CPU power capping | RAPL | Planned |
| Azure Resource Health | Azure Resource Health API | Planned |
| GCP Instance Health | GCP Instance Health API | Planned |

---

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt

make test          # run pytest
make lint          # run ruff
make simulate      # python3 main.py simulate
make dry-run-export  # python3 export_to_otel.py --dry-run
make demo-up       # docker compose + data export
make demo-down     # docker compose down
```

CI runs on Python 3.11 and 3.12 on every push and pull request. See [.github/workflows/ci.yml](.github/workflows/ci.yml).

---

## Known Limitations

- **Timestamp skew:** Physical devices may have clock drift. The lag tolerance window accounts for this but cannot eliminate it.
- **Topology accuracy:** Correlations are only as accurate as your topology file. Stale mappings produce incorrect causal chains silently.
- **Sampling gaps:** Short infrastructure events may fall between sampled trace spans at low sampling rates.
- **Cloud physical layer:** For cloud deployments, PowerTrace cannot see below the hypervisor. AWS Health Events are coarse-grained and may lag real hardware events.
- **Batch-only:** No streaming or incremental mode. The engine processes all input data in one pass.

---

## Roadmap

**Done (v0.1.0)**
- Correlation engine with Pydantic validation, topology resolution, confidence scoring
- AWS Health Events receiver
- CloudWatch metrics receiver
- OTLP export (metrics, traces, logs) with Grafana annotations
- Docker Compose demo stack (Grafana, Prometheus, Tempo, OTel Collector)
- Test suite (63 tests), CI (GitHub Actions), ruff linting

**Prototype / Active Development**
- Improve AWS receiver topology enrichment from `describe_affected_entities`
- NVML receiver for live GPU telemetry

**Planned**
- OTel Collector receiver/connector in Go (see [docs/OTEL_POSITIONING.md](docs/OTEL_POSITIONING.md))
- Redfish BMC receiver
- PDU SNMP receiver
- Proposed OTel semantic conventions for physical infrastructure attributes
- Streaming/incremental correlation mode

---

## Related Work

- [Cardinality Detector](https://github.com/hitkall/cardinality-detector) — sibling project: OTel metric cardinality CLI
- [OpenTelemetry Collector Contrib](https://github.com/open-telemetry/opentelemetry-collector-contrib) — upstream for future receiver contributions
- [DMTF Redfish Specification](https://www.dmtf.org/standards/redfish) — server BMC API standard

---

## Design Partners

If you run AI infrastructure — on AWS GPU instances, in a colo, or on-prem — and want to discuss the architecture, test this on real workloads, or collaborate on the Go receiver:

→ [LinkedIn](https://linkedin.com/in/hiteshkalluru) | kalluruhitesh3@gmail.com

---

## License

Apache 2.0

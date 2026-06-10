# PowerTrace

**Connecting infrastructure signals to application observability — so you know exactly why your AI workload broke.**

[![CI](https://github.com/hitkall/powertrace/actions/workflows/ci.yml/badge.svg)](https://github.com/hitkall/powertrace/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![OpenTelemetry](https://img.shields.io/badge/OpenTelemetry-aligned-blueviolet.svg)](docs/OTEL_POSITIONING.md)

---

## What PowerTrace Is

PowerTrace is an OTel-aligned correlation engine that connects infrastructure events and application trace degradation into a causal timeline.

Today, PowerTrace runs as a Python correlation engine with an OTLP export path into a local Grafana / Prometheus / Tempo / OpenTelemetry Collector stack. It is not yet a native OpenTelemetry Collector receiver, processor, or connector.

## Current Status

### Working Today

- Python correlation CLI
- Sample incident simulation
- Infrastructure event and trace degradation correlation
- Topology-aware causal chain ranking
- Confidence scoring
- AWS Health receiver prototype
- CloudWatch receiver prototype
- OTLP export into a local Grafana / Prometheus / Tempo / OpenTelemetry Collector stack

### Prototype

- AWS Health event mapping and affected-entity enrichment
- CloudWatch infrastructure signal ingestion
- Local Grafana observability demo
- Sample report generation

### Planned

- Native OpenTelemetry Collector receiver / processor / connector
- NVML / DCGM live GPU receiver
- SNMP / Redfish receiver
- RAPL support
- Azure / GCP health event receivers
- Production validation on real GPU clusters

---

## Why This Matters

When an AI training job slows down or crashes, two separate teams start investigating simultaneously. The **infrastructure team** looks at CloudWatch, AWS Health Events, or physical power dashboards. The **SRE / platform team** looks at APM traces, error rates, and GPU metrics. Neither team can see what the other sees.

A power fluctuation that caused a GPU to thermal throttle, which caused a training job to lose a checkpoint, gets discovered in a post-mortem — not in real time. For teams running large GPU clusters, this is expensive: a single interrupted training run on a p4d.24xlarge costs over $30/hour while stopped, multiplied by the time it takes to diagnose the root cause manually. PowerTrace shows the causality in seconds.

---

## 60-Second Demo

Works from a clean clone. No AWS credentials required; Docker only for the Grafana stack.

```bash
git clone https://github.com/hitkall/powertrace
cd powertrace

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# Correlation engine on bundled sample data
python main.py simulate
python main.py simulate --output json
python main.py correlate --events sample_data/events.json --traces sample_data/traces.json --topology sample_data/topology.json

# Preview OTLP payloads without a running backend
python export_to_otel.py --dry-run
```

Sample output from `python main.py simulate`:

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

**With the Grafana stack (Docker required):**

```bash
docker compose up -d        # Grafana, Prometheus, Tempo, OTel Collector
python export_to_otel.py    # push metrics, traces, logs, and annotations
docker compose down         # tear down when finished
```

| Service | URL | Credentials |
|---------|-----|-------------|
| Grafana dashboard | http://localhost:3000/d/powertrace-main | admin / powertrace |
| Grafana home | http://localhost:3000 | admin / powertrace |
| Prometheus | http://localhost:9090 | — |

The provisioned **PowerTrace — Infrastructure Correlation Dashboard** shows P99 latency and error-rate time series with the incident spike, Grafana annotations marking each infrastructure event at its exact timestamp, and the correlated incident trace in Tempo.

See [docs/DEMO.md](docs/DEMO.md) for the full walkthrough, including the live AWS data path.

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

## Configuration

```bash
python main.py correlate \
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

> **Note:** AWS Health API requires Business or Enterprise Support plan. Use `python main.py simulate` for a full demo without credentials.

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

## Signal Sources

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

## Documentation & Examples

| Resource | Description |
|----------|-------------|
| [docs/DEMO.md](docs/DEMO.md) | Full demo walkthrough: CLI-only, Docker stack, and live AWS paths |
| [docs/DESIGN.md](docs/DESIGN.md) | Correlation engine internals: schemas, scoring model, limitations |
| [docs/OTEL_POSITIONING.md](docs/OTEL_POSITIONING.md) | Honest OTel positioning and the path to a native Collector component |
| [examples/sample_report.md](examples/sample_report.md) | Markdown incident report generated from the sample data |
| [examples/sample_report.json](examples/sample_report.json) | JSON incident report generated from the sample data |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development setup and contribution guidelines |

---

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt

make test            # pytest (63 tests)
make lint            # ruff check .
make simulate        # python main.py simulate
make dry-run-export  # python export_to_otel.py --dry-run
make demo-up         # docker compose stack + data export
make demo-down       # docker compose down
make clean           # remove caches
```

CI runs on Python 3.11 and 3.12 on every push and pull request: lint, tests, and smoke runs of the simulate and export commands. See [.github/workflows/ci.yml](.github/workflows/ci.yml).

---

## Known Limitations

- **Timestamp skew:** Physical devices may have clock drift. The lag tolerance window accounts for this but cannot eliminate it.
- **Topology accuracy:** Correlations are only as accurate as your topology file. Stale mappings produce incorrect causal chains silently.
- **Sampling gaps:** Short infrastructure events may fall between sampled trace spans at low sampling rates.
- **Cloud physical layer:** For cloud deployments, PowerTrace cannot see below the hypervisor. AWS Health Events are coarse-grained and may lag real hardware events.
- **Batch-only:** No streaming or incremental mode. The engine processes all input data in one pass.
- **No production validation yet:** The correlation model has been validated on simulated incidents, not on a real GPU cluster.

---

## Roadmap

**Done (v0.1.0)**
- Correlation engine with Pydantic validation, topology resolution, confidence scoring
- AWS Health Events receiver prototype
- CloudWatch metrics receiver prototype
- OTLP export (metrics, traces, logs) with Grafana annotations
- Docker Compose demo stack (Grafana, Prometheus, Tempo, OTel Collector)
- Test suite (63 tests), CI (GitHub Actions), ruff linting

**Prototype / Active Development**
- Improve AWS receiver topology enrichment from `describe_affected_entities`
- Harden the local Grafana demo and sample report generation

**Planned**
- OTel Collector receiver / processor / connector in Go (see [docs/OTEL_POSITIONING.md](docs/OTEL_POSITIONING.md))
- NVML / DCGM live GPU receiver
- SNMP / Redfish receiver
- RAPL support
- Azure / GCP health event receivers
- Proposed OTel semantic conventions for physical infrastructure attributes
- Streaming / incremental correlation mode
- Production validation on real GPU clusters

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

[Apache 2.0](LICENSE)

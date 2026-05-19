# PowerTrace

**Connecting physical infrastructure signals to application observability — so you know exactly why your AI workload broke.**

---

## The Problem

When an AI training job slows down or crashes, two separate teams start investigating simultaneously.

The **infrastructure team** is staring at power dashboards — PDU feeds, server thermals, UPS status. The **platform/SRE team** is staring at APM dashboards — traces, error rates, GPU utilization. Neither team can see what the other sees.

The result: engineers spend hours manually reconstructing what happened after the damage is already done. A power fluctuation that caused a GPU to thermal throttle, which caused a training job to lose a checkpoint, gets discovered in a post-mortem — not in real time.

**For AI companies running large GPU clusters, this is not a minor inconvenience. A single interrupted training run can cost $50,000 or more.**

---

## What PowerTrace Does

PowerTrace is an OpenTelemetry-native correlation engine that connects physical infrastructure signals to your application observability stack.

It produces a unified causal timeline like this:

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
  Est. cost: $847
  Root cause: PDU-B voltage sag on rack-14
```

This is the correlation that currently takes hours to reconstruct manually.

---

## Why This Doesn't Exist Yet

The DCIM (data center infrastructure management) world and the observability world have always been separate industries with separate buyers, separate tools, and separate vocabularies.

The OTel ecosystem has receivers for Kafka, Redis, MySQL, and dozens of software systems. It has nothing for physical power infrastructure.

AI changed the urgency. Traditional cloud workloads have stable, predictable power profiles. AI training jobs do not — they are massive, spiky, and catastrophically expensive to lose mid-run.

---

## Who This Is For

### On-Prem and Colocation AI Teams
You run GPU clusters in your own facility or a colo. You have physical access to PDUs, server BMCs, and network infrastructure. PowerTrace ingests those signals directly via SNMP and Redfish and connects them to your existing OTel traces.

### Cloud-Based AI Companies
You're on AWS, GCP, or Azure. You have no visibility into the physical data center — the CSP owns that. But you're not completely blind.

**Signals accessible from inside your cloud instance:**
- **NVML** — per-GPU power draw, thermal throttle state, power cap events. Accessible from inside any cloud GPU instance (p4d, A100 on GCP) without physical access.
- **RAPL** — Intel's CPU power capping interface, accessible via `/sys/class/powercap/` on many cloud instances.
- **AWS Health Events API** — Amazon surfaces underlying hardware events that affect your instance: host degradation, scheduled retirement, network connectivity issues. These are physical events in disguise.
- **Azure Resource Health / GCP Instance Health** — equivalent signals for other CSPs.

**The gap for cloud customers:** these signals exist but nobody is ingesting them into OTel and correlating them with traces automatically. PowerTrace does.

### Colocation Providers
When a power event causes a tenant's workload to degrade, you spend hours proving the cause was or wasn't on your end. PowerTrace gives you the evidence in real time.

---

## Architecture

```
Signal Sources                    PowerTrace                  Your Backend
──────────────                    ──────────                  ────────────
PDU SNMP          ─────────────►
Redfish (BMC)     ─────────────►  Correlation    ──────────►  Grafana
NVML (GPU)        ─────────────►  Engine         ──────────►  Datadog
RAPL (CPU)        ─────────────►                 ──────────►  Honeycomb
AWS Health Events ─────────────►                 ──────────►  Any OTel backend
                                       │
                              Topology Map
                         (device → service mapping)
```

PowerTrace takes three inputs:

| Input | What it contains |
|---|---|
| `events.json` | Infrastructure events (GPU throttle, PDU voltage, AWS Health, etc.) |
| `traces.json` | OTel trace metrics and spans (P99, error rate, throughput) |
| `topology.json` | Mapping of physical devices to logical services |

And produces a ranked causal timeline with confidence scores.

---

## Supported Signal Sources

| Source | Protocol | On-Prem | Cloud |
|---|---|---|---|
| PDU power events | SNMP | ✓ | — |
| Server BMC | Redfish | ✓ | — |
| PoE switch | SNMP | ✓ | — |
| UPS | SNMP | ✓ | — |
| GPU telemetry | NVML | ✓ | ✓ |
| CPU power capping | RAPL | ✓ | ✓ |
| AWS Health Events | HTTP API | — | ✓ |
| Azure Resource Health | HTTP API | — | ✓ |
| GCP Instance Health | HTTP API | — | ✓ |

---

## Quickstart

```bash
git clone https://github.com/hitkall/powertrace
cd powertrace
pip install -r requirements.txt

python correlate.py \
  --events sample_data/events.json \
  --traces sample_data/traces.json \
  --topology sample_data/topology.json
```

Sample data is included so you can run a full correlation demo without any real infrastructure.

---

## Configuration

```bash
python correlate.py \
  --events events.json \
  --traces traces.json \
  --topology topology.json \
  --window 5 \        # correlation time window in seconds (default: 5)
  --lag 60 \          # max correlation lag in seconds (default: 60)
  --baseline 600 \    # baseline lookback window in seconds (default: 600)
  --confidence 0.6 \  # minimum confidence threshold to report (default: 0.6)
  --output timeline   # output format: timeline | json | markdown
```

---

## How the Correlation Works

1. Parses and validates all three input files using Pydantic
2. Builds a topology index mapping physical device IDs to logical service names
3. Calculates a rolling baseline per service (P99, error rate, throughput)
4. Detects anomaly windows where metrics deviate more than 2x from baseline
5. For each anomaly window, looks back up to `--lag` seconds for preceding infrastructure events
6. Filters events to those whose device maps to the affected service via the topology index
7. Ranks events by severity, time proximity, and physical layer priority
8. Scores confidence: penalizes time gaps, rewards topology matches and severity alignment
9. Emits incidents above the confidence threshold with full causal chains and impact metrics

---

## Known Limitations

- **Timestamp skew:** Physical devices may have clock drift up to several seconds. Confidence scores account for this but cannot eliminate it. Use NTP-synchronized devices where possible.
- **Topology accuracy:** Correlations are only as accurate as your topology map. Stale mappings produce incorrect causal chains.
- **Sampling gaps:** If OTel traces are sampled at low rates, short power events may fall between sampled spans. Increase trace sampling rate for GPU-heavy services.
- **Baseline drift:** Services with naturally high or variable latency require a longer baseline window to produce reliable anomaly detection.
- **Cloud physical layer:** For cloud deployments, PowerTrace cannot see below the hypervisor. CSP-surfaced health events are the closest available signal.

---

## Roadmap

- [x] Correlation CLI with simulated inputs
- [ ] NVML receiver (pynvml — Python)
- [ ] AWS Health Events receiver (boto3 — Python)
- [ ] OTel Collector receiver for PDU SNMP (Go)
- [ ] OTel Collector receiver for Redfish (Go)
- [ ] Grafana dashboard template (unified physical + trace timeline)
- [ ] Predictive alerting (power budget trending before threshold breach)
- [ ] Proposed OTel semantic convention for physical infrastructure attributes

---

## Design Partners

If you run AI infrastructure — on-prem, in a colo, or on cloud GPU instances — and you want early access or want to collaborate, reach out.

Specifically looking for teams who have experienced unexplained training job degradation and want to understand whether physical infrastructure was the cause.

→ [LinkedIn](https://linkedin.com/in/hiteshkalluru) | kalluruhitesh3@gmail.com

---

## Contributing

PowerTrace is in early development. Contributions welcome — especially:
- Additional signal source receivers
- Topology auto-discovery integrations
- OTel semantic convention proposals for physical infrastructure

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup instructions.

---

## Related Work

- [Cardinality Exploder Detector](https://github.com/hitkall/cardinality-detector) — sibling project for OTel metric cardinality management
- [OpenTelemetry Collector Contrib](https://github.com/open-telemetry/opentelemetry-collector-contrib) — upstream for future receiver contributions
- [POWER-ETHERNET-MIB (RFC 3621)](https://datatracker.ietf.org/doc/html/rfc3621) — PoE SNMP MIB reference
- [DMTF Redfish Specification](https://www.dmtf.org/standards/redfish) — server BMC API standard

---

## License

Apache 2.0

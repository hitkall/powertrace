# PowerTrace — Local Demo Walkthrough

This page walks you through running the full PowerTrace demo locally, from the CLI correlation engine through the Grafana dashboard.

---

## Option A: CLI only (no Docker required)

Run the correlation engine against the included sample data:

```bash
git clone https://github.com/hitkall/powertrace
cd powertrace
pip install -r requirements.txt

python3 main.py simulate
```

This runs the full correlation pipeline on a pre-built incident scenario and prints a timeline report to stdout.

**What the sample scenario shows:**
A PDU voltage sag on `rack-14` triggers a PSU failover, CPU power cap, and GPU thermal throttle on `server-rack-14-node-3`. Ten seconds later, `llama-inference-api` P99 latency spikes from 530ms to 2,100ms (+296%) and error rate jumps from 0.1% to 4.3%. The engine traces causality from physical layer to application trace in one report.

**Other output formats:**

```bash
# Markdown (for copy-paste into incident reports)
python3 main.py simulate --output markdown

# JSON (for piping into other tools)
python3 main.py simulate --output json
cat powertrace_report.json | python3 -m json.tool

# With GPU cost estimate (pass hourly on-demand rate)
python3 main.py simulate --gpu-rate 32.77   # p4d.24xlarge on-demand
```

---

## Option B: Full observability stack (Docker required)

Runs Grafana, Prometheus, Tempo, and the OTel Collector. Seeded with the sample incident.

```bash
# Start everything
./start.sh
```

Or step by step:

```bash
# 1. Start the stack
docker compose up -d

# 2. Wait ~20s for services to be ready, then run the correlation
python3 main.py simulate

# 3. Export data to OTel Collector + Grafana
python3 export_to_otel.py
```

**Access the stack:**

| Service    | URL                                      | Credentials        |
|------------|------------------------------------------|--------------------|
| Grafana    | http://localhost:3000                    | admin / powertrace |
| Prometheus | http://localhost:9090                    | —                  |
| Dashboard  | http://localhost:3000/d/powertrace-main  | admin / powertrace |

**What you'll see in Grafana:**
- P99 latency and error rate time series with the incident spike visible
- Grafana annotations marking each infrastructure event (PDU, PSU, GPU) at the exact timestamps they occurred
- Trace data in Tempo showing the correlated incident span and causal chain child spans

**Stop the stack:**

```bash
./stop.sh
# or
docker compose down
```

---

## Option C: Live AWS data (requires AWS account)

See [README.md](../README.md#testing-with-live-aws-data) for prerequisite steps (AWS credentials, IAM policy, EC2 instance ID).

```bash
python3 main.py run \
  --instance i-YOUR_INSTANCE_ID \
  --region us-east-1 \
  --service your-service-name \
  --lookback 120
```

---

## Correlation command reference

```bash
python3 main.py correlate \
  --events   sample_data/events.json \
  --traces   sample_data/traces.json \
  --topology sample_data/topology.json \
  --window   5       # correlation time window in seconds
  --lag      60      # max causal lag in seconds
  --baseline 600     # baseline lookback window in seconds
  --confidence 0.6   # minimum confidence to report
  --output   timeline | json | markdown
  --gpu-rate 11.57   # optional: GPU cost in USD/hr for cost estimate
```

---

## Troubleshooting

**`python3: command not found`** — Use `python` if `python3` is not aliased, or `python3.11`.

**WARNING about "1 pre-window data point"** — This is informational, not an error. It means the algorithm skipped one baseline comparison because there wasn't enough historical data in the lookback window for that specific metric point. The correlation still runs correctly.

**`SubscriptionRequiredException` from AWS Health API** — AWS Health requires Business or Enterprise Support plan. Use `python3 main.py simulate` for a full demo without credentials.

**Docker Compose services not ready** — If `export_to_otel.py` reports connection errors, wait a bit longer after `docker compose up -d`. Services can take up to 30s to be fully ready.

# Contributing to PowerTrace

Thanks for your interest in contributing. PowerTrace is an early-stage project and contributions of all sizes are welcome.

## Development setup

```bash
git clone https://github.com/hitkall/powertrace
cd powertrace

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

Python 3.11+ is what CI tests against. The engine currently also runs on 3.9+, which keeps the demo runnable on stock macOS `python3`.

## Running tests

```bash
make test        # or: pytest
```

All 63 tests run offline — no AWS credentials, Docker, or network access required. Please keep it that way: new tests must not depend on live services.

## Linting

```bash
make lint        # or: ruff check .
```

Ruff configuration lives in `pyproject.toml`. CI fails on lint errors, so run this before pushing.

## Running the demo

```bash
python main.py simulate                  # CLI demo, no dependencies
python export_to_otel.py --dry-run       # preview OTLP payloads
docker compose up -d                     # full Grafana/Prometheus/Tempo stack
python export_to_otel.py                 # push data to the stack
docker compose down
```

See [docs/DEMO.md](docs/DEMO.md) for the full walkthrough.

## Reporting bugs

Open a GitHub issue with:

- The command you ran and its full output
- Your Python version (`python --version`)
- For correlation bugs: a minimal `events.json` / `traces.json` / `topology.json` that reproduces the problem (scrubbed of any real infrastructure identifiers)

## What contributions are welcome

- **Bug fixes** in the correlation engine, CLI, or export path
- **Test coverage** for edge cases (clock skew, sparse metrics, deep topologies)
- **Receiver prototypes** for the planned sources (NVML, SNMP, Redfish, RAPL, Azure, GCP) — open an issue first to discuss the interface
- **Docs improvements** — especially anything that confused you on first read
- **OTel expertise** — feedback on [docs/OTEL_POSITIONING.md](docs/OTEL_POSITIONING.md) and the path to a native Collector component

## Truth-in-docs policy

Planned receivers and features must **not** be documented as implemented until they are merged and tested. The README's Current Status section (Working Today / Prototype / Planned) is the source of truth — if your PR adds a capability, move it to the right section in the same PR. No fake benchmarks, no fake production claims.

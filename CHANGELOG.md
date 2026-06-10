# Changelog

## Unreleased

- Clarified OTel-aligned positioning.
- Added tests, CI, docs, and example outputs.
- Added local demo instructions.
- Fixed unreachable branch in causal-chain confidence scoring.
- Fixed OTLP log export reading the wrong event field names.
- Replaced mutable Pydantic defaults with `Field(default_factory=...)`.
- Added LICENSE, CONTRIBUTING, SECURITY, and editor configuration.

## 0.1.0

- Initial release: correlation engine with Pydantic validation, topology
  resolution, anomaly detection, and confidence scoring.
- AWS Health Events and CloudWatch receiver prototypes (boto3).
- OTLP export path (metrics, traces, logs) with Grafana annotations.
- Docker Compose demo stack: Grafana, Prometheus, Tempo, OTel Collector.

# Security Policy

## Reporting a vulnerability

If you find a security issue in PowerTrace, please email **kalluruhitesh3@gmail.com** rather than opening a public GitHub issue. Include a description of the issue and steps to reproduce. You should receive a response within a few days.

## Scope notes

PowerTrace is a local CLI tool. It does not run a network service, but two areas deserve care:

- `export_to_otel.py` sends HTTP requests to a configurable OTLP endpoint and a Grafana instance. Pointing it at an untrusted endpoint sends your correlation data there.
- The AWS receivers (`receivers/aws_health.py`, `receivers/cloudwatch.py`) use boto3 with your AWS credentials and make read-only API calls.

## Do not commit credentials

Never commit AWS access keys, session tokens, or `~/.aws` contents to this repository — including in issues, test fixtures, or example files. The `.gitignore` excludes `.env`, but credentials can leak through many paths; check `git diff` before committing.

## Do not upload production telemetry

Do not attach real production telemetry, customer data, or internal infrastructure identifiers (instance IDs, hostnames, topology files from real environments) to issues or pull requests. Reproduce bugs with the bundled `sample_data/` files or scrubbed synthetic fixtures.

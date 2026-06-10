"""Tests for main.py CLI behavior."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
PYTHON = sys.executable


def run(*args, **kwargs):
    """Run a subprocess and return CompletedProcess. Captures stdout+stderr."""
    return subprocess.run(
        [PYTHON, str(ROOT / "main.py"), *args],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        **kwargs,
    )


class TestSimulateCommand:
    def test_simulate_exits_zero(self):
        result = run("simulate")
        assert result.returncode == 0, result.stderr

    def test_simulate_outputs_incident_report(self):
        result = run("simulate")
        assert "INCIDENT DETECTED" in result.stdout

    def test_simulate_json_writes_valid_json(self):
        result = run("simulate", "--output", "json")
        # When --output json, main.py writes to powertrace_report.json in cwd
        report_file = ROOT / "powertrace_report.json"
        assert result.returncode == 0, result.stderr
        assert report_file.exists()
        parsed = json.loads(report_file.read_text())
        assert "report" in parsed
        assert "incidents" in parsed["report"]

    def test_simulate_markdown_output(self):
        result = run("simulate", "--output", "markdown")
        assert result.returncode == 0, result.stderr
        assert "## Incident" in result.stdout

    def test_simulate_with_gpu_rate(self):
        result = run("simulate", "--gpu-rate", "10.0")
        assert result.returncode == 0, result.stderr
        assert "Est. cost:" in result.stdout
        assert "null" not in result.stdout.split("Est. cost:")[1].split("\n")[0]

    def test_simulate_missing_events_file_fails_clearly(self):
        result = run(
            "simulate",
            "--events", "nonexistent_events.json",
            "--traces", str(ROOT / "sample_data" / "traces.json"),
            "--topology", str(ROOT / "sample_data" / "topology.json"),
        )
        assert result.returncode != 0
        assert "nonexistent_events.json" in result.stdout + result.stderr

    def test_simulate_missing_traces_file_fails_clearly(self):
        result = run(
            "simulate",
            "--events", str(ROOT / "sample_data" / "events.json"),
            "--traces", "nonexistent_traces.json",
            "--topology", str(ROOT / "sample_data" / "topology.json"),
        )
        assert result.returncode != 0

    def test_simulate_missing_topology_file_fails_clearly(self):
        result = run(
            "simulate",
            "--events", str(ROOT / "sample_data" / "events.json"),
            "--traces", str(ROOT / "sample_data" / "traces.json"),
            "--topology", "nonexistent_topology.json",
        )
        assert result.returncode != 0


class TestCorrelateCommand:
    def test_correlate_with_sample_data(self):
        result = run(
            "correlate",
            "--events",   str(ROOT / "sample_data" / "events.json"),
            "--traces",   str(ROOT / "sample_data" / "traces.json"),
            "--topology", str(ROOT / "sample_data" / "topology.json"),
        )
        assert result.returncode == 0, result.stderr
        assert "INCIDENT DETECTED" in result.stdout

    def test_correlate_json_output_valid(self):
        result = run(
            "correlate",
            "--events",   str(ROOT / "sample_data" / "events.json"),
            "--traces",   str(ROOT / "sample_data" / "traces.json"),
            "--topology", str(ROOT / "sample_data" / "topology.json"),
            "--output",   "json",
        )
        assert result.returncode == 0, result.stderr
        # JSON is written to a file; ensure exit was clean
        report_file = ROOT / "powertrace_report.json"
        if report_file.exists():
            parsed = json.loads(report_file.read_text())
            assert "report" in parsed

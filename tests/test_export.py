"""Tests for export_to_otel.py payload builders and dry-run behavior."""
import importlib.util
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
PYTHON = sys.executable


def _load_export():
    spec = importlib.util.spec_from_file_location("export_to_otel", ROOT / "export_to_otel.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestDryRun:
    def test_dry_run_exits_zero_without_live_collector(self):
        result = subprocess.run(
            [PYTHON, str(ROOT / "export_to_otel.py"), "--dry-run"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        assert result.returncode == 0, result.stderr

    def test_dry_run_does_not_attempt_http_connection(self):
        result = subprocess.run(
            [PYTHON, str(ROOT / "export_to_otel.py"), "--dry-run",
             "--otel-endpoint", "http://127.0.0.1:19999"],  # nothing on this port
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        assert result.returncode == 0, result.stderr
        assert "DRY-RUN" in result.stderr

    def test_dry_run_reports_correct_counts(self):
        result = subprocess.run(
            [PYTHON, str(ROOT / "export_to_otel.py"), "--dry-run"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert "Metric data points" in combined
        assert "Log records" in combined


class TestMetricsPayloadBuilder:
    def setup_method(self):
        self.mod = _load_export()

    def test_returns_resource_metrics_key(self):
        metrics = [{
            "service_name": "api",
            "timestamp": "2024-01-15T14:32:00.000Z",
            "p99_latency_ms": 100.0,
            "error_rate_percent": 0.1,
            "throughput_rps": 100.0,
        }]
        payload = self.mod._build_metrics_payload(metrics, shift=0)
        assert "resourceMetrics" in payload
        assert len(payload["resourceMetrics"]) == 1

    def test_data_points_have_time_unix_nano(self):
        metrics = [{
            "service_name": "api",
            "timestamp": "2024-01-15T14:32:00.000Z",
            "p99_latency_ms": 100.0,
            "error_rate_percent": 0.1,
            "throughput_rps": 100.0,
        }]
        payload = self.mod._build_metrics_payload(metrics, shift=0)
        for rm in payload["resourceMetrics"]:
            for sm in rm["scopeMetrics"]:
                for metric in sm["metrics"]:
                    for dp in metric["gauge"]["dataPoints"]:
                        assert "timeUnixNano" in dp
                        assert isinstance(dp["timeUnixNano"], str)

    def test_skips_none_values(self):
        metrics = [{
            "service_name": "api",
            "timestamp": "2024-01-15T14:32:00.000Z",
            "p99_latency_ms": None,
            "error_rate_percent": 0.1,
            "throughput_rps": 100.0,
        }]
        payload = self.mod._build_metrics_payload(metrics, shift=0)
        metric_names = [
            m["name"]
            for rm in payload["resourceMetrics"]
            for sm in rm["scopeMetrics"]
            for m in sm["metrics"]
        ]
        assert "powertrace_p99_latency_ms" not in metric_names


class TestLogsPayloadBuilder:
    def setup_method(self):
        self.mod = _load_export()

    def _sample_event(self):
        return {
            "id": "evt_001",
            "timestamp": "2024-01-15T14:32:01.000Z",
            "source": "pdu_snmp",
            "type": "voltage_sag",
            "severity": "critical",
            "device_id": "PDU-01",
            "raw_message": "PDU-B input voltage below threshold",
        }

    def test_returns_resource_logs_key(self):
        payload = self.mod._build_logs_payload([self._sample_event()], shift=0)
        assert "resourceLogs" in payload

    def test_log_body_uses_type_field(self):
        payload = self.mod._build_logs_payload([self._sample_event()], shift=0)
        body = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["body"]["stringValue"]
        assert "voltage_sag" in body
        assert "[unknown]" not in body

    def test_log_body_uses_raw_message_field(self):
        payload = self.mod._build_logs_payload([self._sample_event()], shift=0)
        body = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["body"]["stringValue"]
        assert "PDU-B input voltage below threshold" in body

    def test_severity_number_critical_is_21(self):
        payload = self.mod._build_logs_payload([self._sample_event()], shift=0)
        sev_num = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["severityNumber"]
        assert sev_num == 21  # FATAL in OTel severity scale


class TestTracesPayloadBuilder:
    def setup_method(self):
        self.mod = _load_export()

    def _sample_incident(self):
        return {
            "service": "api",
            "confidence": 0.89,
            "anomaly_window": {
                "start": "2024-01-15T14:32:01.000Z",
                "end":   "2024-01-15T14:33:48.000Z",
            },
            "causal_chain": [{
                "timestamp":  "2024-01-15T14:32:01.000Z",
                "event_type": "voltage_sag",
                "device_id":  "PDU-01",
                "severity":   "critical",
                "source":     "pdu_snmp",
                "confidence": 0.0,
            }],
            "impact": {},
        }

    def test_returns_resource_spans_key(self):
        payload = self.mod._build_traces_payload([self._sample_incident()], shift=0)
        assert "resourceSpans" in payload

    def test_span_names_use_event_type_from_chain(self):
        payload = self.mod._build_traces_payload([self._sample_incident()], shift=0)
        spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
        child_names = [s["name"] for s in spans if "parentSpanId" in s]
        assert any("voltage_sag" in n for n in child_names)


class TestTimestampShift:
    def setup_method(self):
        self.mod = _load_export()

    def test_shifted_timestamp_is_timezone_aware(self):
        dt = self.mod._shift_ts("2024-01-15T14:32:01.000Z", shift=3600)
        assert dt.tzinfo is not None

    def test_shift_adds_correct_seconds(self):
        dt = self.mod._shift_ts("2024-01-15T14:32:01.000Z", shift=3600)
        expected = datetime(2024, 1, 15, 15, 32, 1, tzinfo=timezone.utc)
        assert abs((dt - expected).total_seconds()) < 1

    def test_unix_nano_is_string(self):
        dt = datetime(2024, 1, 15, 14, 32, 1, tzinfo=timezone.utc)
        nano = self.mod._to_unix_nano(dt)
        assert isinstance(nano, str)
        assert len(nano) > 10

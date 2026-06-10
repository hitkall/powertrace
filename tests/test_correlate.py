"""Tests for the PowerTrace correlation engine (correlate.py)."""
import json
from datetime import datetime, timezone

import pytest

from correlate import (
    CorrelationReport,
    _parse_tz_timestamp,
    build_causal_chain,
    build_topology_index,
    detect_anomaly_windows,
    parse_events,
    parse_topology,
    parse_traces,
    render_json,
    render_markdown,
    render_timeline,
)
from tests.conftest import make_event, make_metric

# ── Case 1: Timestamp validation ───────────────────────────────────────────────

class TestTimestampValidation:
    def test_naive_string_rejected(self):
        with pytest.raises(ValueError, match="no timezone"):
            _parse_tz_timestamp("2024-01-15T14:32:01")

    def test_z_suffix_accepted(self):
        dt = _parse_tz_timestamp("2024-01-15T14:32:01Z")
        assert dt.tzinfo is not None

    def test_offset_accepted(self):
        dt = _parse_tz_timestamp("2024-01-15T14:32:01+05:30")
        assert dt.tzinfo is not None

    def test_non_string_rejected(self):
        with pytest.raises(ValueError):
            _parse_tz_timestamp(12345)

    def test_invalid_format_rejected(self):
        with pytest.raises(ValueError, match="not a valid ISO 8601"):
            _parse_tz_timestamp("not-a-date")

    def test_naive_datetime_object_rejected(self):
        naive = datetime(2024, 1, 15, 14, 32, 1)
        with pytest.raises(ValueError, match="no timezone"):
            _parse_tz_timestamp(naive)

    def test_aware_datetime_object_accepted(self):
        aware = datetime(2024, 1, 15, 14, 32, 1, tzinfo=timezone.utc)
        dt = _parse_tz_timestamp(aware)
        assert dt.tzinfo is not None


# ── Case 2: Input shape validation ─────────────────────────────────────────────

class TestInputShapeValidation:
    def test_parse_events_wrong_key(self):
        result = parse_events({"wrong_key": []})
        assert result == []

    def test_parse_events_null_value(self):
        result = parse_events({"events": None})
        assert result == []

    def test_parse_events_skips_malformed_record(self):
        data = {
            "events": [
                make_event(),  # valid
                {"id": "bad"},  # missing required fields
            ]
        }
        result = parse_events(data)
        assert len(result) == 1
        assert result[0].id == "e1"

    def test_parse_traces_wrong_key(self):
        result = parse_traces({"wrong_key": []})
        assert result.service_metrics == []

    def test_parse_topology_wrong_key(self):
        result = parse_topology({"wrong_key": []})
        assert result == []


# ── Topology index ──────────────────────────────────────────────────────────────

class TestTopologyIndex:
    def test_direct_device_to_service(self):
        topology_raw = {"mappings": [
            {"physical_device_id": "server1", "services": ["api"]},
        ]}
        maps = parse_topology(topology_raw)
        idx = build_topology_index(maps)
        assert "api" in idx["server1"]

    def test_cloud_instance_id_aliased(self):
        topology_raw = {"mappings": [
            {"physical_device_id": "server1", "cloud_instance_id": "i-abc", "services": ["api"]},
        ]}
        maps = parse_topology(topology_raw)
        idx = build_topology_index(maps)
        assert "api" in idx.get("i-abc", [])

    def test_transitive_pdu_feeds_server_feeds_service(self):
        topology_raw = {"mappings": [
            {"physical_device_id": "pdu1", "feeds": ["server1"]},
            {"physical_device_id": "server1", "services": ["api"]},
        ]}
        maps = parse_topology(topology_raw)
        idx = build_topology_index(maps)
        assert "api" in idx["pdu1"]

    def test_transitive_two_hops(self):
        topology_raw = {"mappings": [
            {"physical_device_id": "pdu1", "feeds": ["server1"]},
            {"physical_device_id": "server1", "feeds": ["gpu1"]},
            {"physical_device_id": "gpu1", "services": ["training-job"]},
        ]}
        maps = parse_topology(topology_raw)
        idx = build_topology_index(maps)
        assert "training-job" in idx["pdu1"]

    def test_gpu_device_inherits_server_services(self):
        topology_raw = {"mappings": [
            {"physical_device_id": "server1", "gpus": ["gpu0"], "services": ["api"]},
        ]}
        maps = parse_topology(topology_raw)
        idx = build_topology_index(maps)
        assert "api" in idx.get("gpu0", [])

    def test_unmapped_device_not_in_index(self):
        topology_raw = {"mappings": [
            {"physical_device_id": "server1", "services": ["api"]},
        ]}
        maps = parse_topology(topology_raw)
        idx = build_topology_index(maps)
        assert "unmapped-device" not in idx


# ── Anomaly detection ───────────────────────────────────────────────────────────

class TestAnomalyDetection:
    def _run(self, metrics_list, lookback=600.0):
        traces = parse_traces({"service_metrics": metrics_list})
        return detect_anomaly_windows(traces.service_metrics, lookback)

    def test_single_data_point_skipped_with_warning(self):
        windows, warnings = self._run([make_metric(p99=100.0)])
        assert windows == []
        assert any("only 1 data point" in w for w in warnings)

    def test_two_total_points_both_normal_no_anomaly(self):
        metrics = [make_metric(offset=0, p99=100.0), make_metric(offset=700, p99=100.0)]
        windows, _ = self._run(metrics)
        assert windows == []

    def test_p99_exceeds_2x_baseline_detected(self):
        # 4 baseline points, then anomaly >2x
        metrics = [make_metric(offset=i * 60, p99=100.0) for i in range(4)]
        metrics.append(make_metric(offset=3 * 60 + 700, p99=350.0))
        windows, _ = self._run(metrics)
        assert len(windows) == 1
        assert windows[0]["service"] == "api"

    def test_error_rate_exceeds_2x_baseline_detected(self):
        # baseline error_rate=1.0, then 3.0 (>2x)
        metrics = [make_metric(offset=i * 60, p99=100.0, error_rate=1.0) for i in range(4)]
        metrics.append(make_metric(offset=3 * 60 + 700, p99=100.0, error_rate=3.5))
        windows, _ = self._run(metrics)
        assert len(windows) == 1

    def test_near_zero_error_rate_baseline_uses_1pct_floor(self):
        # baseline error_rate=0.0, any >1% is anomalous
        metrics = [make_metric(offset=i * 60, p99=100.0, error_rate=0.0) for i in range(4)]
        metrics.append(make_metric(offset=3 * 60 + 700, p99=100.0, error_rate=2.0))
        windows, _ = self._run(metrics)
        assert len(windows) == 1

    def test_below_threshold_no_anomaly(self):
        metrics = [make_metric(offset=i * 60, p99=100.0) for i in range(4)]
        metrics.append(make_metric(offset=3 * 60 + 700, p99=150.0))  # 1.5x, below 2x
        windows, _ = self._run(metrics)
        assert windows == []

    def test_multiple_anomalous_points_grouped_into_one_window(self):
        metrics = [make_metric(offset=i * 60, p99=100.0) for i in range(4)]
        base = 3 * 60 + 700
        for j in range(3):
            metrics.append(make_metric(offset=base + j * 10, p99=350.0))
        windows, _ = self._run(metrics)
        assert len(windows) == 1


# ── Causal chain builder ────────────────────────────────────────────────────────

class TestCausalChain:
    def _setup(self, topology_raw=None, event_device="dev1", event_offset=0):
        if topology_raw is None:
            topology_raw = {"mappings": [
                {"physical_device_id": "dev1", "services": ["api"]},
            ]}
        events = parse_events({"events": [make_event(device_id=event_device, offset=event_offset)]})
        topology_maps = parse_topology(topology_raw)
        topology_idx = build_topology_index(topology_maps)

        # 4 baseline + 1 anomaly
        metrics_raw = [make_metric(offset=i * 60, p99=100.0) for i in range(4)]
        metrics_raw.append(make_metric(offset=3 * 60 + 700, p99=350.0))
        traces = parse_traces({"service_metrics": metrics_raw})
        windows, _ = detect_anomaly_windows(traces.service_metrics, baseline_lookback=600.0)
        return events, topology_idx, windows

    def test_returns_incident_when_event_precedes_anomaly(self):
        # event at offset 0, anomaly window starts at offset 3*60+700=880
        events, topo, windows = self._setup(event_offset=870)
        inc = build_causal_chain(
            events=events,
            anomaly_window=windows[0],
            topology_index=topo,
            max_lag=60.0,
            correlation_window=5.0,
            min_confidence=0.0,
            gpu_rate=None,
        )
        assert inc is not None
        assert "api" in inc.affected_services

    def test_returns_none_when_no_candidate_events(self):
        events, topo, windows = self._setup(event_offset=0)  # event at 14:00:00, anomaly at ~14:14:40
        # event is way before the anomaly window (>60s lag), no candidates
        inc = build_causal_chain(
            events=events,
            anomaly_window=windows[0],
            topology_index=topo,
            max_lag=60.0,
            correlation_window=5.0,
            min_confidence=0.0,
            gpu_rate=None,
        )
        assert inc is None

    def test_unmapped_device_included_with_warning(self):
        events = parse_events({"events": [
            make_event(device_id="unmapped-dev", offset=870)
        ]})
        topology_raw = {"mappings": [
            {"physical_device_id": "server1", "services": ["api"]},
        ]}
        topology_maps = parse_topology(topology_raw)
        topology_idx = build_topology_index(topology_maps)

        metrics_raw = [make_metric(offset=i * 60, p99=100.0) for i in range(4)]
        metrics_raw.append(make_metric(offset=880, p99=350.0))
        traces = parse_traces({"service_metrics": metrics_raw})
        windows, _ = detect_anomaly_windows(traces.service_metrics, baseline_lookback=600.0)

        inc = build_causal_chain(
            events=events,
            anomaly_window=windows[0],
            topology_index=topology_idx,
            max_lag=60.0,
            correlation_window=5.0,
            min_confidence=0.0,
            gpu_rate=None,
        )
        assert inc is not None
        assert "unmapped-dev" in inc.unmapped_devices
        assert any("no topology mapping" in w for w in inc.warnings)

    def test_long_lag_event_flagged_as_indirect(self):
        # event 55s before anomaly window (> LONG_LAG_SECONDS=30)
        events = parse_events({"events": [make_event(device_id="dev1", offset=825)]})
        topology_raw = {"mappings": [{"physical_device_id": "dev1", "services": ["api"]}]}
        topology_maps = parse_topology(topology_raw)
        topology_idx = build_topology_index(topology_maps)

        metrics_raw = [make_metric(offset=i * 60, p99=100.0) for i in range(4)]
        metrics_raw.append(make_metric(offset=880, p99=350.0))
        traces = parse_traces({"service_metrics": metrics_raw})
        windows, _ = detect_anomaly_windows(traces.service_metrics, baseline_lookback=600.0)

        inc = build_causal_chain(
            events=events,
            anomaly_window=windows[0],
            topology_index=topology_idx,
            max_lag=60.0,
            correlation_window=5.0,
            min_confidence=0.0,
            gpu_rate=None,
        )
        assert inc is not None
        chain_events = [ce for ce in inc.causal_chain if ce.source != "otel_traces"]
        assert any(ce.indirect_causality for ce in chain_events)

    def test_cost_null_without_gpu_rate(self):
        events, topo, windows = self._setup(event_offset=870)
        inc = build_causal_chain(
            events=events,
            anomaly_window=windows[0],
            topology_index=topo,
            max_lag=60.0,
            correlation_window=5.0,
            min_confidence=0.0,
            gpu_rate=None,
        )
        assert inc is not None
        assert inc.impact.estimated_cost_impact_usd is None

    def test_cost_computed_with_gpu_rate(self):
        events, topo, windows = self._setup(event_offset=870)
        inc = build_causal_chain(
            events=events,
            anomaly_window=windows[0],
            topology_index=topo,
            max_lag=60.0,
            correlation_window=5.0,
            min_confidence=0.0,
            gpu_rate=10.0,
        )
        assert inc is not None
        assert inc.impact.estimated_cost_impact_usd is not None
        assert inc.impact.estimated_cost_impact_usd > 0

    def test_low_confidence_flag_set_below_threshold(self):
        events, topo, windows = self._setup(event_offset=870)
        inc = build_causal_chain(
            events=events,
            anomaly_window=windows[0],
            topology_index=topo,
            max_lag=60.0,
            correlation_window=5.0,
            min_confidence=0.99,  # very high threshold
            gpu_rate=None,
        )
        assert inc is not None
        assert inc.low_confidence is True

    def test_low_confidence_false_above_threshold(self):
        events, topo, windows = self._setup(event_offset=870)
        inc = build_causal_chain(
            events=events,
            anomaly_window=windows[0],
            topology_index=topo,
            max_lag=60.0,
            correlation_window=5.0,
            min_confidence=0.0,
            gpu_rate=None,
        )
        assert inc is not None
        assert inc.low_confidence is False


# ── Output renderers ────────────────────────────────────────────────────────────

class TestRenderers:
    def _make_report(self):
        """Build a minimal CorrelationReport from the sample data."""
        import importlib.util
        from pathlib import Path
        root = Path(__file__).parent.parent
        spec = importlib.util.spec_from_file_location("main_mod", root / "main.py")
        main_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(main_mod)
        report, _, _ = main_mod.run_correlation_engine(
            events_path=root / "sample_data" / "events.json",
            traces_path=root / "sample_data" / "traces.json",
            topology_path=root / "sample_data" / "topology.json",
        )
        return report

    def test_timeline_renders_without_error(self):
        report = self._make_report()
        output = render_timeline(report, min_confidence=0.6)
        assert "INCIDENT DETECTED" in output
        assert "CAUSAL CHAIN" in output
        assert "IMPACT" in output

    def test_markdown_renders_without_error(self):
        report = self._make_report()
        output = render_markdown(report, min_confidence=0.6)
        assert "## Incident" in output
        assert "### Causal Chain" in output
        assert "### Impact" in output

    def test_json_renders_valid_json(self):
        report = self._make_report()
        output = render_json(report)
        parsed = json.loads(output)
        assert "report" in parsed
        assert "incidents" in parsed["report"]
        assert "summary" in parsed["report"]

    def test_json_cost_null_without_gpu_rate(self):
        report = self._make_report()
        output = render_json(report)
        parsed = json.loads(output)
        for inc in parsed["report"]["incidents"]:
            assert inc["impact"]["estimated_cost_impact_usd"] is None

    def test_no_incidents_renders_cleanly(self):
        report = CorrelationReport(
            generated_at=datetime.now(timezone.utc),
            incidents=[],
            total_events_processed=0,
            total_incidents_detected=0,
            total_unmapped_devices=0,
            total_warnings=0,
        )
        assert "No incidents" in render_timeline(report, 0.6)
        assert "_No incidents" in render_markdown(report, 0.6)
        parsed = json.loads(render_json(report))
        assert parsed["report"]["incidents"] == []

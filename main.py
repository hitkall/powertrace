#!/usr/bin/env python3
"""
main.py — PowerTrace single entry point.

Commands:
  run        Poll AWS Health + CloudWatch for a real EC2 instance, then correlate.
  simulate   Run the correlation engine on sample_data without any AWS calls.
  correlate  Run the correlation engine on explicit event/trace files.

Examples:
  python main.py run --instance i-0abc123def456 --service llama-inference-api
  python main.py simulate
  python main.py correlate --events events.json --traces traces.json \\
    --topology sample_data/topology.json
"""

import argparse
import importlib.util
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Rich ───────────────────────────────────────────────────────────────────────

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    _RICH = True
except ImportError:
    _RICH = False

console = Console() if _RICH else None

VERSION = "0.1.0"
GITHUB  = "github.com/hitkall/powertrace"

# ── Project paths ──────────────────────────────────────────────────────────────

_ROOT            = Path(__file__).parent
_SAMPLE_EVENTS   = _ROOT / "sample_data" / "events.json"
_SAMPLE_TRACES   = _ROOT / "sample_data" / "traces.json"
_SAMPLE_TOPOLOGY = _ROOT / "sample_data" / "topology.json"


# ── Module loader ──────────────────────────────────────────────────────────────

def _load_module(name: str, path: Path):
    """
    Loads a Python file as a module by path without requiring it to be on
    sys.path. Safe to call multiple times — returns cached module on repeat.
    """
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Eagerly load project modules.
# correlate.py runs logging.basicConfig(level=WARNING) on import, which
# silences the receivers' INFO-level logs — intentional in the main.py context
# since we print our own status lines instead.
_correlate   = _load_module("correlate",   _ROOT / "correlate.py")
_aws_health  = _load_module("aws_health",  _ROOT / "receivers" / "aws_health.py")
_cloudwatch  = _load_module("cloudwatch",  _ROOT / "receivers" / "cloudwatch.py")


# ── Console helpers ────────────────────────────────────────────────────────────

def _print(msg: str = "", **kwargs) -> None:
    if _RICH:
        console.print(msg, **kwargs)
    else:
        # Strip Rich markup tags for fallback plain output
        import re
        plain = re.sub(r"\[/?[^\]]+\]", "", msg)
        print(plain, **{k: v for k, v in kwargs.items() if k in ("end", "file")})


def _rule(title: str = "") -> None:
    if _RICH:
        console.rule(title)
    else:
        print(f"{'─' * 20} {title} {'─' * 20}" if title else "─" * 62)


# ── Banner ─────────────────────────────────────────────────────────────────────

def print_banner() -> None:
    if _RICH:
        t = Text()
        t.append("⚡ PowerTrace ", style="bold yellow")
        t.append(f"v{VERSION}\n", style="bold white")
        t.append("Connecting power signals to OTel traces\n", style="dim")
        t.append(GITHUB, style="dim cyan underline")
        console.print(Panel(t, expand=False, border_style="yellow"))
        console.print()
    else:
        print(f"PowerTrace v{VERSION} — {GITHUB}")
        print()


# ── JSON loader (raises instead of sys.exit) ───────────────────────────────────

def _load_json(path: Path) -> dict:
    """
    Loads and validates a JSON file. Raises FileNotFoundError or ValueError
    instead of calling sys.exit() so callers can handle errors gracefully.
    """
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    try:
        with open(path) as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"{path} must be a JSON object, got {type(data).__name__}. "
            f"Expected a top-level object with keys like \"events\", "
            f"\"service_metrics\", or \"mappings\"."
        )
    return data


# ── AWS credential check ───────────────────────────────────────────────────────

def _has_aws_credentials() -> bool:
    """Returns True if boto3 can find any AWS credentials in the environment."""
    try:
        import boto3
        creds = boto3.session.Session().get_credentials()
        return creds is not None
    except Exception:
        return False


# ── Correlation engine (library wrapper) ───────────────────────────────────────

def run_correlation_engine(
    events_path: Path,
    traces_path: Path,
    topology_path: Path,
    output_format: str = "timeline",
    window: float = 5.0,
    lag: float = 60.0,
    baseline: float = 600.0,
    confidence: float = 0.6,
    gpu_rate: Optional[float] = None,
) -> tuple:
    """
    Loads the three input files, runs the full correlation pipeline, and
    returns (report, rendered_output_str, stats_dict).

    Raises FileNotFoundError or ValueError if inputs are missing or malformed.
    Does NOT call sys.exit() — callers decide how to handle errors.
    """
    c = _correlate

    # Load inputs (raises on error — no sys.exit here)
    events_raw   = _load_json(events_path)
    traces_raw   = _load_json(traces_path)
    topology_raw = _load_json(topology_path)

    # Parse (skips malformed records with log.warning, does not exit)
    events        = c.parse_events(events_raw)
    traces        = c.parse_traces(traces_raw)
    topology_maps = c.parse_topology(topology_raw)

    stats = {
        "events_in_file":  len(events),
        "metrics_in_file": len(traces.service_metrics),
        "incidents":       0,
    }

    missing = []
    if not events:               missing.append("events")
    if not traces.service_metrics: missing.append("service metrics")
    if not topology_maps:        missing.append("topology mappings")
    if missing:
        raise ValueError(f"No valid data found in: {', '.join(missing)}")

    # Build topology index
    topology_index = c.build_topology_index(topology_maps)
    globally_unmapped = {e.device_id for e in events if e.device_id not in topology_index}

    # Detect anomaly windows (locked-baseline approach, case 5 handled inside)
    anomaly_windows, skip_warnings = c.detect_anomaly_windows(
        traces.service_metrics, baseline
    )

    if not anomaly_windows:
        report = c.CorrelationReport(
            generated_at=datetime.now(timezone.utc),
            incidents=[],
            total_events_processed=len(events),
            total_incidents_detected=0,
            total_unmapped_devices=len(globally_unmapped),
            total_warnings=len(skip_warnings),
        )
        return report, _render(c, report, output_format, confidence), stats

    global_warnings = list(skip_warnings)

    # Case 6: same-service overlapping windows
    for i, w1 in enumerate(anomaly_windows):
        for w2 in anomaly_windows[i + 1:]:
            if w1["service"] != w2["service"]:
                continue
            s1, e1 = w1["window_start"].timestamp(), w1["window_end"].timestamp()
            s2, e2 = w2["window_start"].timestamp(), w2["window_end"].timestamp()
            if s1 <= e2 and s2 <= e1:
                global_warnings.append(
                    f"Overlapping anomaly windows for {w1['service']!r} — "
                    f"review each independently."
                )

    # Build causal chains
    incidents = []
    for window_dict in anomaly_windows:
        inc = c.build_causal_chain(
            events=events,
            anomaly_window=window_dict,
            topology_index=topology_index,
            max_lag=lag,
            correlation_window=window,
            min_confidence=confidence,
            gpu_rate=gpu_rate,
        )
        if inc is None:
            continue
        inc.warnings = global_warnings + inc.warnings
        incidents.append(inc)

    # Case 6: cross-service overlapping incidents
    for i, inc1 in enumerate(incidents):
        for inc2 in incidents[i + 1:]:
            s1, e1 = inc1.window_start.timestamp(), inc1.window_end.timestamp()
            s2, e2 = inc2.window_start.timestamp(), inc2.window_end.timestamp()
            if s1 <= e2 and s2 <= e1:
                msg = (
                    f"Incidents for {inc1.affected_services[0]!r} and "
                    f"{inc2.affected_services[0]!r} overlap — may share root cause."
                )
                inc1.warnings.append(msg)
                inc2.warnings.append(msg)

    all_unmapped = globally_unmapped.copy()
    for inc in incidents:
        all_unmapped.update(inc.unmapped_devices)

    all_warnings: list[str] = list(global_warnings)
    for inc in incidents:
        all_warnings.extend(inc.warnings)

    report = c.CorrelationReport(
        generated_at=datetime.now(timezone.utc),
        incidents=incidents,
        total_events_processed=len(events),
        total_incidents_detected=len(incidents),
        total_unmapped_devices=len(all_unmapped),
        total_warnings=len(set(all_warnings)),
    )

    stats["incidents"] = len(incidents)
    return report, _render(c, report, output_format, confidence), stats


def _render(c, report, fmt: str, confidence: float) -> str:
    if fmt == "json":
        return c.render_json(report)
    elif fmt == "markdown":
        return c.render_markdown(report, confidence)
    else:
        return c.render_timeline(report, confidence)


def _emit_output(output_str: str, fmt: str) -> Optional[Path]:
    """
    Prints or writes the correlation output. Returns the output file Path if
    JSON (written to disk), or None if printed to stdout.
    """
    if fmt == "json":
        out_path = Path("powertrace_report.json")
        out_path.write_text(output_str)
        return out_path
    else:
        # Use markup=False so the renderer's text (e.g. ══ separators) is
        # never misinterpreted as Rich markup.
        if _RICH:
            console.print(output_str, markup=False, highlight=False)
        else:
            print(output_str)
        return None


# ── Summary ────────────────────────────────────────────────────────────────────

def print_summary(stats: dict, elapsed: float, output_file: Optional[Path] = None) -> None:
    _print()
    _rule("Summary")

    rows = [
        ("Events collected",   str(stats.get("events_in_file", 0))),
        ("Metric datapoints",  str(stats.get("metrics_in_file", 0))),
        ("Incidents detected", str(stats.get("incidents", 0))),
        ("Time taken",         f"{elapsed:.1f}s"),
    ]
    if output_file:
        rows.append(("Report written", str(output_file)))

    if _RICH:
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column(style="dim", no_wrap=True)
        table.add_column(style="bold white")
        for label, value in rows:
            table.add_row(label, value)
        console.print(table)
    else:
        for label, value in rows:
            print(f"  {label:<22} {value}")

    _print()


# ── Command: run ───────────────────────────────────────────────────────────────

def cmd_run(args) -> None:
    start = time.monotonic()

    events_path   = Path(args.events_out)
    traces_path   = Path(args.traces_out)
    topology_path = Path(args.topology)

    # ── Credential gate ────────────────────────────────────────────────────────
    if not _has_aws_credentials():
        _print("[bold red]⚠  AWS credentials not configured.[/bold red]")
        _print()
        _print("   PowerTrace needs AWS credentials to call Health and CloudWatch APIs.")
        _print("   Configure them with one of:")
        _print("     [bold]aws configure[/bold]")
        _print("     export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...")
        _print("     An attached IAM role (on EC2/ECS/Lambda)")
        _print()
        _print("   To run a full demo without AWS credentials:")
        _print("   [bold cyan]  python main.py simulate[/bold cyan]")
        sys.exit(1)

    n_events_added  = 0
    n_metrics_added = 0

    # ── [1/3] AWS Health ───────────────────────────────────────────────────────
    _print("[dim][[/dim][bold cyan]1/3[/bold cyan][dim]][/dim] Collecting AWS Health events...", end=" ")
    try:
        raw = _aws_health.fetch_aws_health_events(
            region=args.region, lookback_days=7
        )
        health_events = _aws_health.convert_to_infra_events(raw, region=args.region)

        if health_events and not getattr(args, "skip_entities", False):
            arns = [e["id"] for e in health_events]
            enrichment = _aws_health.fetch_affected_instances(arns)
            if enrichment:
                health_events = _aws_health.enrich_device_ids(health_events, enrichment)

        n = _aws_health.merge_events_file(health_events, events_path)
        n_events_added += n
        _print(f"[green]✓[/green] {n} new event(s)")
    except Exception as exc:
        _print(f"[yellow]⚠  skipped ({exc})[/yellow]")

    # ── [2/3] CloudWatch ──────────────────────────────────────────────────────
    _print("[dim][[/dim][bold cyan]2/3[/bold cyan][dim]][/dim] Collecting CloudWatch metrics...", end=" ")
    try:
        metric_data = _cloudwatch.fetch_cloudwatch_metrics(
            instance_id=args.instance,
            region=args.region,
            lookback_minutes=args.lookback,
        )
        cw_events  = _cloudwatch.detect_events_from_metrics(metric_data, args.instance)
        cw_metrics = _cloudwatch.datapoints_to_service_metrics(
            metric_data, args.instance, args.service
        )
        ne = _cloudwatch.merge_events_file(cw_events,  events_path)
        nm = _cloudwatch.merge_traces_file(cw_metrics, traces_path)
        n_events_added  += ne
        n_metrics_added += nm
        _print(f"[green]✓[/green] {nm} metric datapoint(s), {ne} anomaly event(s)")
    except Exception as exc:
        _print(f"[yellow]⚠  skipped ({exc})[/yellow]")

    # ── [3/3] Correlate ───────────────────────────────────────────────────────
    _print("[dim][[/dim][bold cyan]3/3[/bold cyan][dim]][/dim] Running correlation engine...")
    _print()
    try:
        report, output_str, stats = run_correlation_engine(
            events_path=events_path,
            traces_path=traces_path,
            topology_path=topology_path,
            output_format=args.output,
            lag=args.lag,
            confidence=args.confidence,
            gpu_rate=getattr(args, "gpu_rate", None),
        )
    except (FileNotFoundError, ValueError) as exc:
        _print(f"[bold red]Correlation error:[/bold red] {exc}")
        _print()
        _print("Tip: ensure both receivers wrote data before running the correlator.")
        sys.exit(1)

    output_file = _emit_output(output_str, args.output)
    print_summary(stats, time.monotonic() - start, output_file)


# ── Command: simulate ──────────────────────────────────────────────────────────

def cmd_simulate(args) -> None:
    start = time.monotonic()

    _print("[dim]No AWS calls — using sample data for demo.[/dim]")
    _print()

    events_path   = Path(args.events)
    traces_path   = Path(args.traces)
    topology_path = Path(args.topology)

    for label, path in [
        ("Events",   events_path),
        ("Traces",   traces_path),
        ("Topology", topology_path),
    ]:
        if not path.exists():
            _print(f"[bold red]Missing sample file:[/bold red] {path}")
            _print(
                f"Run [bold]git clone https://{GITHUB}[/bold] "
                f"to get the full sample_data directory."
            )
            sys.exit(1)

    try:
        report, output_str, stats = run_correlation_engine(
            events_path=events_path,
            traces_path=traces_path,
            topology_path=topology_path,
            output_format=args.output,
            gpu_rate=getattr(args, "gpu_rate", None),
        )
    except (FileNotFoundError, ValueError) as exc:
        _print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(1)

    output_file = _emit_output(output_str, args.output)
    print_summary(stats, time.monotonic() - start, output_file)


# ── Command: correlate ─────────────────────────────────────────────────────────

def cmd_correlate(args) -> None:
    start = time.monotonic()

    try:
        report, output_str, stats = run_correlation_engine(
            events_path=Path(args.events),
            traces_path=Path(args.traces),
            topology_path=Path(args.topology),
            output_format=args.output,
            window=args.window,
            lag=args.lag,
            baseline=args.baseline,
            confidence=args.confidence,
            gpu_rate=getattr(args, "gpu_rate", None),
        )
    except FileNotFoundError as exc:
        _print(f"[bold red]File not found:[/bold red] {exc}")
        sys.exit(1)
    except ValueError as exc:
        _print(f"[bold red]Input error:[/bold red] {exc}")
        sys.exit(1)

    output_file = _emit_output(output_str, args.output)
    print_summary(stats, time.monotonic() - start, output_file)


# ── Argument parser ────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description=(
            "PowerTrace — correlate AWS infrastructure events with OTel trace "
            "degradations for AI GPU workloads."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
examples:
  # Poll a p4d.24xlarge GPU instance and correlate in real time:
  python main.py run \\
    --instance i-0abc123def456 \\
    --region us-east-1 \\
    --service llama-inference-api \\
    --lookback 120

  # Demo the full pipeline without AWS credentials:
  python main.py simulate

  # Export correlation report as JSON:
  python main.py simulate --output json

  # Correlate your own event and trace files:
  python main.py correlate \\
    --events my_events.json \\
    --traces my_traces.json \\
    --topology sample_data/topology.json \\
    --output markdown

  # Set GPU instance hourly rate for cost estimation:
  python main.py simulate --gpu-rate 32.77   # p4d.24xlarge on-demand

more info: https://{GITHUB}
""",
    )
    parser.add_argument(
        "--version", action="version", version=f"PowerTrace v{VERSION}"
    )

    subs = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="COMMAND",
    )

    # ── run ───────────────────────────────────────────────────────────────────
    run_p = subs.add_parser(
        "run",
        help="Poll AWS Health + CloudWatch for a real EC2 instance, then correlate",
        description=(
            "Collects live AWS Health events and CloudWatch metrics for an EC2\n"
            "GPU instance (e.g. p4d.24xlarge, p3.16xlarge, g5.48xlarge), then\n"
            "runs the correlation engine to detect causally linked incidents.\n\n"
            "Requires AWS credentials. If not configured, use:\n"
            "  python main.py simulate"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "example:\n"
            "  python main.py run \\\n"
            "    --instance i-0abc123def456 \\\n"
            "    --region us-east-1 \\\n"
            "    --service llama-inference-api \\\n"
            "    --lookback 120 \\\n"
            "    --gpu-rate 32.77"
        ),
    )
    run_p.add_argument(
        "--instance", required=True, metavar="INSTANCE_ID",
        help=(
            "EC2 instance ID to poll "
            "(e.g. i-0abc123def456 for a p4d.24xlarge GPU instance)"
        ),
    )
    run_p.add_argument(
        "--region", default="us-east-1",
        help="AWS region of the instance (default: us-east-1)",
    )
    run_p.add_argument(
        "--service", default="unknown-service", dest="service", metavar="SERVICE_NAME",
        help=(
            "Application service name to tag metric rows with — should match "
            "the service_name in your OTel spans "
            "(e.g. llama-inference-api, embedding-service, vllm-server). "
            "Also reads SERVICE_NAME env var. (default: unknown-service)"
        ),
    )
    run_p.add_argument(
        "--lookback", type=int, default=120, metavar="MINUTES",
        help="CloudWatch lookback window in minutes (default: 120)",
    )
    run_p.add_argument(
        "--output", choices=["timeline", "json", "markdown"], default="timeline",
        help="Correlation report format (default: timeline)",
    )
    run_p.add_argument(
        "--events-out", default=str(_SAMPLE_EVENTS), metavar="PATH",
        help=f"Events JSON file to merge into (default: {_SAMPLE_EVENTS})",
    )
    run_p.add_argument(
        "--traces-out", default=str(_SAMPLE_TRACES), metavar="PATH",
        help=f"Traces JSON file to merge into (default: {_SAMPLE_TRACES})",
    )
    run_p.add_argument(
        "--topology", default=str(_SAMPLE_TOPOLOGY), metavar="PATH",
        help=f"Topology mapping file (default: {_SAMPLE_TOPOLOGY})",
    )
    run_p.add_argument(
        "--lag", type=float, default=60.0, metavar="SEC",
        help="Max causal lag in seconds between infra event and trace anomaly (default: 60)",
    )
    run_p.add_argument(
        "--confidence", type=float, default=0.6, metavar="FLOAT",
        help="Min confidence threshold 0–1 for reporting incidents (default: 0.6)",
    )
    run_p.add_argument(
        "--gpu-rate", type=float, default=None, metavar="USD_PER_HR",
        help=(
            "GPU instance on-demand hourly rate for incident cost estimation "
            "(e.g. 32.77 for p4d.24xlarge). Omit to skip cost computation."
        ),
    )
    run_p.add_argument(
        "--skip-entities", action="store_true",
        help=(
            "Skip the health:DescribeAffectedEntities call "
            "(use if your IAM role lacks that permission — device_id falls back to event ARN)"
        ),
    )

    # ── simulate ──────────────────────────────────────────────────────────────
    sim_p = subs.add_parser(
        "simulate",
        help="Run correlation on bundled sample data — no AWS credentials needed",
        description=(
            "Runs the full PowerTrace correlation pipeline against the bundled\n"
            "sample_data files. No AWS credentials required.\n\n"
            "Sample scenario: a PDU voltage sag on rack-14 causes a GPU thermal\n"
            "throttle on server-rack-14-node-3, degrading llama-inference-api\n"
            "P99 latency from 530ms to 2,100ms (+296%) for 107 seconds."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python main.py simulate\n"
            "  python main.py simulate --output json\n"
            "  python main.py simulate --gpu-rate 32.77"
        ),
    )
    sim_p.add_argument(
        "--events", default=str(_SAMPLE_EVENTS), metavar="PATH",
        help=f"Events JSON file (default: {_SAMPLE_EVENTS})",
    )
    sim_p.add_argument(
        "--traces", default=str(_SAMPLE_TRACES), metavar="PATH",
        help=f"Traces JSON file (default: {_SAMPLE_TRACES})",
    )
    sim_p.add_argument(
        "--topology", default=str(_SAMPLE_TOPOLOGY), metavar="PATH",
        help=f"Topology mapping file (default: {_SAMPLE_TOPOLOGY})",
    )
    sim_p.add_argument(
        "--output", choices=["timeline", "json", "markdown"], default="timeline",
        help="Correlation report format (default: timeline)",
    )
    sim_p.add_argument(
        "--gpu-rate", type=float, default=None, metavar="USD_PER_HR",
        help="GPU hourly rate for cost estimation (e.g. 32.77 for p4d.24xlarge)",
    )

    # ── correlate ─────────────────────────────────────────────────────────────
    corr_p = subs.add_parser(
        "correlate",
        help="Run correlation engine on your own event and trace files",
        description=(
            "Runs the PowerTrace correlation engine against explicit file paths.\n"
            "Use this when you have custom event sources or pre-collected data."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "example:\n"
            "  python main.py correlate \\\n"
            "    --events my_events.json \\\n"
            "    --traces my_traces.json \\\n"
            "    --topology sample_data/topology.json \\\n"
            "    --output markdown"
        ),
    )
    corr_p.add_argument(
        "--events",   required=True,
        help="Path to the events JSON file (InfraEvent records)",
    )
    corr_p.add_argument(
        "--traces",   required=True,
        help="Path to the traces JSON file (ServiceMetric records)",
    )
    corr_p.add_argument(
        "--topology", required=True,
        help="Path to the topology JSON file (device-to-service mappings)",
    )
    corr_p.add_argument(
        "--output", choices=["timeline", "json", "markdown"], default="timeline",
        help="Correlation report format (default: timeline)",
    )
    corr_p.add_argument(
        "--window", type=float, default=5.0, metavar="SEC",
        help="Correlation time window in seconds (default: 5)",
    )
    corr_p.add_argument(
        "--lag", type=float, default=60.0, metavar="SEC",
        help="Max causal lag in seconds between infra event and trace anomaly (default: 60)",
    )
    corr_p.add_argument(
        "--baseline", type=float, default=600.0, metavar="SEC",
        help="Baseline lookback window in seconds (default: 600)",
    )
    corr_p.add_argument(
        "--confidence", type=float, default=0.6, metavar="FLOAT",
        help="Min confidence threshold 0–1 for reporting incidents (default: 0.6)",
    )
    corr_p.add_argument(
        "--gpu-rate", type=float, default=None, metavar="USD_PER_HR",
        help="GPU hourly rate for cost estimation (e.g. 32.77 for p4d.24xlarge)",
    )

    return parser


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    # Banner appears for all commands but NOT for --version / --help
    # (those exit inside argparse before we get here)
    print_banner()

    if args.command == "run":
        cmd_run(args)
    elif args.command == "simulate":
        cmd_simulate(args)
    elif args.command == "correlate":
        cmd_correlate(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

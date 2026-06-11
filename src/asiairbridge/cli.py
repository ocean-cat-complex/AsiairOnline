from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .backup import build_jobs, result_to_dict, run_job
from .config import ConfigError, load_config
from .monitor import dashboard_snapshot, scan_source_totals
from .probe import copy_backend_available, net_view, path_exists, ping_host, tcp_open
from .rpc import (
    READONLY_EXTENDED_METHODS,
    READONLY_HARDWARE_METHODS,
    READONLY_STATUS_METHODS,
    build_write_read_plan,
    compact_result,
    run_preview_preflight,
    run_preview_shot,
    run_probe,
    write_probe_report,
)
from .state import RunLock, read_latest_state, write_run_state


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        return args.handler(config, args)
    except (ConfigError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asiairbridge",
        description="Remote ASIAIR backup and operations helper.",
    )
    parser.add_argument("--config", default="config/devices.json", help="Config JSON path.")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Print machine-readable JSON. Put before the subcommand.",
    )

    subcommands = parser.add_subparsers(dest="command", required=True)

    doctor = subcommands.add_parser("doctor", help="Check local tools and device reachability.")
    doctor.add_argument("--device", action="append", help="Limit to one device name. Repeatable.")
    doctor.set_defaults(handler=_cmd_doctor)

    discover = subcommands.add_parser("discover", help="Discover SMB shares for each device.")
    discover.add_argument("--device", action="append", help="Limit to one device name. Repeatable.")
    discover.set_defaults(handler=_cmd_discover)

    plan = subcommands.add_parser("plan", help="Show configured backup jobs.")
    plan.add_argument("--device", action="append", help="Limit to one device name. Repeatable.")
    plan.add_argument(
        "--source-label",
        action="append",
        help="Limit to one configured source label. Repeatable.",
    )
    plan.set_defaults(handler=_cmd_plan)

    backup = subcommands.add_parser("backup", help="Run dry-run or real incremental backup jobs.")
    backup.add_argument("--device", action="append", help="Limit to one device name. Repeatable.")
    backup.add_argument(
        "--source-label",
        action="append",
        help="Limit to one configured source label. Repeatable.",
    )
    dry_group = backup.add_mutually_exclusive_group()
    dry_group.add_argument("--dry-run", action="store_true", help="Preview copy actions without writing files.")
    dry_group.add_argument("--no-dry-run", action="store_true", help="Perform a real backup.")
    backup.add_argument(
        "--force-lock",
        action="store_true",
        help="Remove an existing stale lock before running.",
    )
    backup.set_defaults(handler=_cmd_backup)

    status = subcommands.add_parser("status", help="Show the latest recorded run state.")
    status.set_defaults(handler=_cmd_status)

    monitor = subcommands.add_parser("monitor", help="Show live dashboard status data.")
    monitor.set_defaults(handler=_cmd_monitor)

    rpc_probe = subcommands.add_parser(
        "rpc-probe",
        help="Probe allowlisted ASIAIR JSON-RPC read-only methods.",
    )
    rpc_probe.add_argument("--device", required=True, help="Device name to probe.")
    rpc_probe.add_argument(
        "--profile",
        choices=["status", "hardware", "extended", "all"],
        default="all",
        help="Read-only probe group to run.",
    )
    rpc_probe.add_argument("--port", type=int, default=4700, help="ASIAIR RPC port.")
    rpc_probe.add_argument("--timeout", type=float, default=5.0, help="Per-method timeout seconds.")
    rpc_probe.add_argument("--no-save", action="store_true", help="Do not write state/rpc-probes report.")
    rpc_probe.set_defaults(handler=_cmd_rpc_probe)

    preview_preflight = subcommands.add_parser(
        "rpc-preview-preflight",
        help="Validate preview-related no-op RPC writes without starting exposure.",
    )
    preview_preflight.add_argument("--device", required=True, help="Device name to probe.")
    preview_preflight.add_argument("--port", type=int, default=4700, help="ASIAIR RPC port.")
    preview_preflight.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="Per-method timeout seconds.",
    )
    preview_preflight.add_argument(
        "--no-save",
        action="store_true",
        help="Do not write state/rpc-probes report.",
    )
    preview_preflight.set_defaults(handler=_cmd_rpc_preview_preflight)

    preview_shot = subcommands.add_parser(
        "rpc-preview-shot",
        help="Prepare or run a guarded short preview exposure on the imager port.",
    )
    preview_shot.add_argument("--device", required=True, help="Device name to control.")
    preview_shot.add_argument(
        "--exposure-seconds",
        type=float,
        default=1.0,
        help="Preview exposure length. Converted to ASIAIR microseconds.",
    )
    preview_shot.add_argument("--bin", type=int, default=4, help="Preview binning value.")
    preview_shot.add_argument("--port", type=int, default=4700, help="ASIAIR imager RPC port.")
    preview_shot.add_argument("--timeout", type=float, default=5.0, help="Per-method timeout seconds.")
    preview_shot.add_argument(
        "--wait-timeout",
        type=float,
        default=30.0,
        help="Seconds to poll camera state after starting exposure.",
    )
    preview_shot.add_argument(
        "--execute",
        action="store_true",
        help="Actually send set_page, set_camera_exp_and_bin, and start_exposure.",
    )
    preview_shot.add_argument(
        "--no-autosave",
        action="store_true",
        help="Send keep_autosave_dev=false to start_exposure.",
    )
    preview_shot.add_argument(
        "--skip-image-save",
        action="store_true",
        help="Do not call save_image after the preview exposure completes.",
    )
    preview_shot.add_argument(
        "--no-restore",
        action="store_true",
        help="Do not restore previous page and exposure/bin after execution.",
    )
    preview_shot.add_argument("--no-save", action="store_true", help="Do not write state/rpc-probes report.")
    preview_shot.set_defaults(handler=_cmd_rpc_preview_shot)

    write_read = subcommands.add_parser(
        "rpc-write-read-plan",
        help="Build a dry-run plan for disposable create/write/read RPC tests.",
    )
    write_read.add_argument("--device", required=True, help="Device name to inspect.")
    write_read.add_argument(
        "--test-prefix",
        default="asiairbridge_test",
        help="Name prefix for planned disposable ASIAIR objects.",
    )
    write_read.add_argument(
        "--image-path",
        help="Optional ASIAIR-relative FITS path for planned image metadata write/read.",
    )
    write_read.add_argument("--port", type=int, default=4700, help="ASIAIR imager RPC port.")
    write_read.add_argument("--timeout", type=float, default=5.0, help="Per-method timeout seconds.")
    write_read.add_argument("--no-save", action="store_true", help="Do not write state/rpc-probes report.")
    write_read.set_defaults(handler=_cmd_rpc_write_read_plan)

    scan = subcommands.add_parser("scan-sources", help="Scan source share sizes and cache totals.")
    scan.add_argument("--device", action="append", help="Limit to one device name. Repeatable.")
    scan.add_argument(
        "--source-label",
        action="append",
        help="Limit to one configured source label. Repeatable.",
    )
    scan.set_defaults(handler=_cmd_scan_sources)

    web = subcommands.add_parser("web", help="Run the local web dashboard.")
    web.add_argument("--host", default="127.0.0.1", help="Bind address.")
    web.add_argument("--port", type=int, default=8787, help="Bind port.")
    web.add_argument(
        "--allow-remote-actions",
        action="store_true",
        help="Allow non-loopback clients to trigger scans and backups.",
    )
    web.add_argument(
        "--read-only",
        action="store_true",
        help="Disable scan and backup actions for every client.",
    )
    web.set_defaults(handler=_cmd_web)

    return parser


def _cmd_doctor(config, args: argparse.Namespace) -> int:  # type: ignore[no-untyped-def]
    rows: list[dict[str, Any]] = []
    _add_row(rows, "local", "destination", path_exists(config.project.destination_root))
    _add_row(rows, "local", "copy_backend", copy_backend_available())
    for device in config.get_devices(args.device):
        endpoint_tcp_ok = False
        for endpoint in device.endpoint_candidates():
            scope = f"{device.name}/{endpoint.label}"
            ping = ping_host(endpoint.ip)
            _add_row(rows, scope, f"ping {endpoint.ip}", ping, required=False)
            tcp = tcp_open(endpoint.ip, config.backup.smb_port)
            endpoint_tcp_ok = endpoint_tcp_ok or tcp.ok
            _add_row(rows, scope, f"tcp/{config.backup.smb_port}", tcp)
        if not endpoint_tcp_ok:
            continue
        for source in config.source_roots_for(device):
            if not source.enabled:
                continue
            _add_row(rows, device.name, source.label, path_exists(source.render(device)))

    if args.json_output:
        _print_json(rows)
    else:
        _print_rows(rows, ["scope", "check", "ok", "detail"])
    return 0 if all(row["ok"] or not row["required"] for row in rows) else 1


def _cmd_discover(config, args: argparse.Namespace) -> int:  # type: ignore[no-untyped-def]
    results = []
    for device in config.get_devices(args.device):
        for endpoint in device.endpoint_candidates():
            result = net_view(endpoint.ip)
            results.append(
                {
                    "device": device.name,
                    "endpoint": endpoint.label,
                    "ip": endpoint.ip,
                    "ok": result.ok,
                    "detail": result.detail,
                }
            )

    if args.json_output:
        _print_json(results)
    else:
        for item in results:
            print(f"[{item['device']}/{item['endpoint']}] {item['ip']} ok={item['ok']}")
            print(item["detail"])
            print()
    return 0 if all(item["ok"] for item in results) else 1


def _cmd_plan(config, args: argparse.Namespace) -> int:  # type: ignore[no-untyped-def]
    jobs = build_jobs(config, "plan", args.device, args.source_label)
    rows = [
        {
            "device": job.device.name,
            "source": job.source.label,
            "from": str(job.source_path),
            "to": str(job.destination_path),
        }
        for job in jobs
    ]
    if args.json_output:
        _print_json(rows)
    else:
        _print_rows(rows, ["device", "source", "from", "to"])
    return 0 if jobs else 1


def _cmd_backup(config, args: argparse.Namespace) -> int:  # type: ignore[no-untyped-def]
    dry_run = config.backup.dry_run_default
    if args.dry_run:
        dry_run = True
    if args.no_dry_run:
        dry_run = False

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    jobs = build_jobs(config, run_id, args.device, args.source_label)
    if not jobs:
        print("No enabled backup jobs matched the request.", file=sys.stderr)
        return 1

    started_at = datetime.now().isoformat(timespec="seconds")
    results = []
    lock_metadata = {
        "run_id": run_id,
        "dry_run": dry_run,
        "devices": args.device or [device.name for device in config.get_devices()],
        "source_labels": args.source_label or [],
    }
    with RunLock(config.project.lock_file, force=args.force_lock, metadata=lock_metadata):
        for job in jobs:
            mode = "DRY-RUN" if dry_run else "RUN"
            print(f"{mode} {job.device.name}: {job.source_path} -> {job.destination_path}")
            result = run_job(config, job, dry_run=dry_run)
            results.append(result_to_dict(result))
            print(f"  {result.status}: {result.detail}")

    finished_at = datetime.now().isoformat(timespec="seconds")
    payload = {
        "run_id": run_id,
        "dry_run": dry_run,
        "config_path": str(config.path),
        "started_at": started_at,
        "finished_at": finished_at,
        "results": results,
        "ok": all(item["ok"] for item in results),
    }
    state_path = write_run_state(config.state_path(), run_id, payload)

    if args.json_output:
        _print_json(payload)
    else:
        ok_count = sum(1 for item in results if item["ok"])
        print(f"Recorded state: {state_path}")
        print(f"Summary: {ok_count}/{len(results)} jobs ok")
    return 0 if payload["ok"] else 8


def _cmd_status(config, args: argparse.Namespace) -> int:  # type: ignore[no-untyped-def]
    latest = read_latest_state(config.state_path())
    if latest is None:
        print("No run state recorded yet.")
        return 1
    if args.json_output:
        _print_json(latest)
    else:
        print(f"run_id: {latest.get('run_id')}")
        print(f"dry_run: {latest.get('dry_run')}")
        print(f"ok: {latest.get('ok')}")
        print(f"started_at: {latest.get('started_at')}")
        print(f"finished_at: {latest.get('finished_at')}")
        for item in latest.get("results", []):
            print(
                f"- {item.get('device')} {item.get('source_label')}: "
                f"{item.get('status')} exit={item.get('exit_code')}"
            )
    return 0 if latest.get("ok") else 1


def _cmd_monitor(config, args: argparse.Namespace) -> int:  # type: ignore[no-untyped-def]
    snapshot = dashboard_snapshot(config)
    if args.json_output:
        _print_json(snapshot)
    else:
        lock = snapshot.get("lock", {})
        active = lock.get("active") and lock.get("pid_alive") is not False
        print(f"generated_at: {snapshot.get('generated_at')}")
        print(f"active: {active}")
        print(f"pid: {lock.get('pid', '-')}")
        for job in snapshot.get("jobs", []):
            source = job.get("source_bytes")
            local = (job.get("local") or {}).get("bytes", 0)
            progress = job.get("progress")
            percent = f"{progress * 100:.1f}%" if progress is not None else "-"
            print(
                f"- {job.get('device')} {job.get('source_label')}: "
                f"{local} / {source or '-'} bytes ({percent})"
            )
    return 0


def _cmd_rpc_probe(config, args: argparse.Namespace) -> int:  # type: ignore[no-untyped-def]
    device = config.get_devices([args.device])[0]
    if args.profile == "status":
        methods = READONLY_STATUS_METHODS
    elif args.profile == "hardware":
        methods = READONLY_HARDWARE_METHODS
    elif args.profile == "extended":
        methods = READONLY_EXTENDED_METHODS
    else:
        methods = READONLY_STATUS_METHODS + READONLY_HARDWARE_METHODS + READONLY_EXTENDED_METHODS

    payload = run_probe(device, methods, port=args.port, timeout_seconds=args.timeout)
    if not args.no_save:
        report_path = write_probe_report(config, payload, args.profile)
        payload["report_path"] = str(report_path)

    if args.json_output:
        _print_json(payload)
    else:
        rows = [
            {
                "category": item["category"],
                "method": item["method"],
                "ok": item["ok"],
                "code": item.get("code", "-"),
                "seconds": item["seconds"],
                "result": compact_result(item.get("result") if item.get("ok") else item.get("error")),
            }
            for item in payload["results"]
        ]
        _print_rows(rows, ["category", "method", "ok", "code", "seconds", "result"])
        if payload.get("report_path"):
            print(f"report: {payload['report_path']}")
        print(f"ok: {payload['ok_count']}/{payload['total_count']}")
    return 0 if payload["ok_count"] else 1


def _cmd_rpc_preview_preflight(config, args: argparse.Namespace) -> int:  # type: ignore[no-untyped-def]
    device = config.get_devices([args.device])[0]
    payload = run_preview_preflight(device, port=args.port, timeout_seconds=args.timeout)
    if not args.no_save:
        report_path = write_probe_report(config, payload, "preview-preflight")
        payload["report_path"] = str(report_path)

    if args.json_output:
        _print_json(payload)
    else:
        before = payload["before"]
        after = payload["after"]
        before_page = before["app"].get("page") if isinstance(before["app"], dict) else compact_result(before["app"])
        after_page = after["app"].get("page") if isinstance(after["app"], dict) else compact_result(after["app"])
        rows = [
            {
                "check": "page",
                "before": before_page,
                "after": after_page,
                "unchanged": payload["unchanged"]["page"],
            },
            {
                "check": "camera",
                "before": compact_result(before["camera"]),
                "after": compact_result(after["camera"]),
                "unchanged": payload["unchanged"]["camera_state"],
            },
            {
                "check": "exposure",
                "before": compact_result(before["exposure"]),
                "after": compact_result(after["exposure"]),
                "unchanged": payload["unchanged"]["exposure"],
            },
        ]
        _print_rows(rows, ["check", "before", "after", "unchanged"])
        for item in payload["actions"]:
            print(f"action {item['method']}: ok={item['ok']} code={item.get('code')} seconds={item['seconds']}")
        if payload.get("skipped_reason"):
            print(f"skipped: {payload['skipped_reason']}")
        if payload.get("report_path"):
            print(f"report: {payload['report_path']}")
        print(f"ok: {payload['ok']}")
    return 0 if payload["ok"] else 1


def _cmd_rpc_preview_shot(config, args: argparse.Namespace) -> int:  # type: ignore[no-untyped-def]
    device = config.get_devices([args.device])[0]
    payload = run_preview_shot(
        device,
        exposure_seconds=args.exposure_seconds,
        bin_value=args.bin,
        execute=args.execute,
        keep_autosave_dev=not args.no_autosave,
        save_image=not args.skip_image_save,
        restore_settings=not args.no_restore,
        port=args.port,
        timeout_seconds=args.timeout,
        wait_timeout_seconds=args.wait_timeout,
    )
    if not args.no_save:
        report_path = write_probe_report(config, payload, "preview-shot")
        payload["report_path"] = str(report_path)

    if args.json_output:
        _print_json(payload)
    else:
        rows = [
            {
                "check": item["check"],
                "ok": item["ok"],
                "detail": item["detail"],
            }
            for item in payload["preconditions"]
        ]
        _print_rows(rows, ["check", "ok", "detail"])
        print(f"ready: {payload.get('ready')}")
        print(f"execute: {payload['execute']}")
        for item in payload["planned_actions"]:
            print(f"plan {item['method']}: {compact_result(item['params'])}")
        for item in payload["actions"]:
            print(f"action {item['method']}: ok={item['ok']} code={item.get('code')} seconds={item['seconds']}")
        for item in payload["restore_actions"]:
            print(f"restore {item['method']}: ok={item['ok']} code={item.get('code')} seconds={item['seconds']}")
        if payload.get("polls"):
            last = payload["polls"][-1]
            print(f"polls: {len(payload['polls'])}, last camera={compact_result(last.get('camera'))}")
        if payload.get("report_path"):
            print(f"report: {payload['report_path']}")
        print(f"ok: {payload['ok']}")
    return 0 if payload["ok"] else 1


def _cmd_rpc_write_read_plan(config, args: argparse.Namespace) -> int:  # type: ignore[no-untyped-def]
    device = config.get_devices([args.device])[0]
    payload = build_write_read_plan(
        device,
        test_prefix=args.test_prefix,
        image_path=args.image_path,
        port=args.port,
        timeout_seconds=args.timeout,
    )
    if not args.no_save:
        report_path = write_probe_report(config, payload, "write-read-plan")
        payload["report_path"] = str(report_path)

    if args.json_output:
        _print_json(payload)
    else:
        read_rows = [
            {
                "method": item["method"],
                "ok": item["ok"],
                "code": item.get("code", "-"),
                "result": compact_result(item.get("result") if item.get("ok") else item.get("error")),
            }
            for item in payload["reads"]
        ]
        _print_rows(read_rows, ["method", "ok", "code", "result"])
        print(f"writes_sent: {payload['writes_are_sent']}")
        for workflow in payload["workflows"]:
            print(f"workflow {workflow['name']}: {workflow['status']}")
            for step in workflow.get("steps", []):
                print(f"  {step['phase']} {step['method']}: {compact_result(step.get('params'))}")
        if payload.get("report_path"):
            print(f"report: {payload['report_path']}")
        print(f"ok: {payload['ok']}")
    return 0 if payload["ok"] else 1


def _cmd_scan_sources(config, args: argparse.Namespace) -> int:  # type: ignore[no-untyped-def]
    payload = scan_source_totals(config, args.device, args.source_label)
    if args.json_output:
        _print_json(payload)
    else:
        rows = [
            {
                "device": item["device"],
                "source": item["source_label"],
                "method": item.get("scan_method", "smb_walk"),
                "files": (
                    "-"
                    if item.get("scan_method") == "asiair_jsonrpc_get_disk_volume"
                    else item["stats"]["file_count"]
                ),
                "gb": item["stats"]["gb"],
                "seconds": item["stats"]["scan_seconds"],
            }
            for item in payload["results"]
        ]
        _print_rows(rows, ["device", "source", "method", "files", "gb", "seconds"])
    return 0


def _cmd_web(config, args: argparse.Namespace) -> int:  # type: ignore[no-untyped-def]
    from .web import run_server

    run_server(
        str(config.path),
        host=args.host,
        port=args.port,
        allow_remote_actions=args.allow_remote_actions,
        read_only=args.read_only,
    )
    return 0


def _add_row(
    rows: list[dict[str, Any]],
    scope: str,
    check: str,
    result: Any,
    required: bool = True,
) -> None:
    rows.append(
        {
            "scope": scope,
            "check": check,
            "ok": result.ok,
            "detail": result.detail,
            "required": required,
        }
    )


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _print_rows(rows: list[dict[str, Any]], columns: list[str]) -> None:
    if not rows:
        print("No rows.")
        return
    widths = {
        column: max(len(column), *(len(str(row.get(column, ""))) for row in rows))
        for column in columns
    }
    header = "  ".join(column.ljust(widths[column]) for column in columns)
    print(header)
    print("  ".join("-" * widths[column] for column in columns))
    for row in rows:
        print("  ".join(str(row.get(column, "")).ljust(widths[column]) for column in columns))

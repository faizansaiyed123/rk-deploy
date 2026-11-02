"""rkdeploy command line."""
import argparse
import json
import sys

from . import __version__, ops
from .fleet import FleetError, load_fleet, run_fleet
from .manifest import ManifestError, NAME_RE
from .pack import build_package

# exit codes (docs/SPEC.md)
OK, USAGE, VERIFY, PARTIAL, ROLLED_BACK, FAILED = 0, 2, 4, 5, 6, 7


def _load(args):
    return load_fleet(args.fleet)


def _select(fleet, args):
    return fleet.select(getattr(args, "hosts", None))


def cmd_pack(args):
    out, m = build_package(args.manifest, args.out)
    print(f"packed {m['name']} {m['version']} -> {out}")
    return OK


def cmd_deploy(args):
    meta = ops.read_package_meta(args.package)
    app = meta["name"]
    fleet = _load(args)
    hosts = _select(fleet, args)
    if args.dry_run:
        unit_template = ops.read_package_unit_template(args.package)
        for host in hosts:
            preview = ops.preview_deploy_host(host, args.package, meta, unit_template)
            print(f"{preview['alias']}: dry-run")
            print(f"  Package: {preview['package']}")
            print(f"  Target root: {preview['target_root']}")
            print(f"  Unit file: {preview['unit_name']}")
            print("  Rendered unit:")
            unit_content = preview["unit_content"]
            print(unit_content, end="" if unit_content.endswith("\n") else "\n")
        print("Dry run complete; no SSH commands, uploads, or board-side actions were performed.")
        return OK

    results = run_fleet(hosts, lambda h: ops.deploy_host(h, args.package, app, args.no_auto_rollback),
                        jobs=args.jobs)
    worst = OK
    for h in hosts:
        r = results[h.alias]
        if isinstance(r, Exception):
            r = {"alias": h.alias, "outcome": "error", "log": str(r)}
        print(f"{h.alias}: {r['outcome']}" + (f" version={r.get('version')}" if r.get("version") else ""))
        if r["outcome"] == "ok":
            continue
        if r["outcome"] == "rolled-back":
            worst = max(worst, ROLLED_BACK)
        elif r["outcome"] == "aborted":
            worst = max(worst, VERIFY)
        elif r["outcome"] in ("unreachable", "upload-failed", "error", "unknown"):
            worst = max(worst, PARTIAL)
        else:  # failed / rollback-failed: a board left in a bad state
            worst = FAILED
    return worst


def cmd_rollback(args):
    fleet = _load(args)
    hosts = _select(fleet, args)
    results = run_fleet(hosts, lambda h: ops.rollback_host(h, args.app, args.to), jobs=args.jobs)
    worst = OK
    for h in hosts:
        r = results[h.alias]
        if isinstance(r, Exception):
            r = {"alias": h.alias, "outcome": "error", "log": str(r)}
        print(f"{h.alias}: {r['outcome']}" + (f" version={r.get('version')}" if r.get("version") else ""))
        if r["outcome"] == "ok":
            continue
        worst = FAILED if r["outcome"] != "aborted" else VERIFY
        if len(hosts) > 1 and r["outcome"] in ("error",):
            worst = PARTIAL
    return worst


def cmd_uninstall(args):
    if not NAME_RE.match(args.app):
        print("error: app name must match ^[a-z][a-z0-9-]{1,31}$", file=sys.stderr)
        return USAGE

    fleet = _load(args)
    hosts = _select(fleet, args)
    if not hosts:
        print("error: host selection matched no boards; no changes made", file=sys.stderr)
        return USAGE

    aliases = [host.alias for host in fleet.hosts]
    if len(aliases) != len(set(aliases)):
        print(
            "error: fleet contains duplicate host aliases; "
            "cannot safely inventory or uninstall",
            file=sys.stderr,
        )
        return USAGE

    target_aliases = {host.alias for host in hosts}

    # Always inventory the complete fleet, not just the selected uninstall
    # targets. The override bypasses only the last-copy guard; it does not
    # turn an unreachable board into an assumed-safe inventory result.
    inventory = run_fleet(
        fleet.hosts,
        lambda host: ops.probe_host(host, args.app),
        jobs=args.jobs,
    )
    unreachable = []
    installed = set()
    for host in fleet.hosts:
        record = inventory.get(host.alias)
        if isinstance(record, Exception) or not record or not record.get("reachable"):
            unreachable.append(host.alias)
        elif record.get("installed"):
            installed.add(host.alias)

    if unreachable:
        names = ", ".join(sorted(unreachable))
        print(
            f"error: cannot verify application inventory on: {names}; "
            "no boards were changed",
            file=sys.stderr,
        )
        return VERIFY

    if not args.allow_last_copy:
        selected_installed = installed & target_aliases
        remaining_installed = installed - target_aliases
        if selected_installed and not remaining_installed:
            names = ", ".join(sorted(selected_installed))
            print(
                f"error: uninstall would remove the fleet's last installed copy "
                f"({names}); re-run with --allow-last-copy to confirm",
                file=sys.stderr,
            )
            return VERIFY

    results = run_fleet(
        hosts,
        lambda host: ops.uninstall_host(host, args.app, purge=args.purge),
        jobs=args.jobs,
    )
    outcomes = {}
    for host in hosts:
        result = results.get(host.alias)
        if isinstance(result, Exception):
            result = {"alias": host.alias, "outcome": "error", "log": str(result)}
        if not result:
            result = {"alias": host.alias, "outcome": "error", "log": "no result returned"}
        outcomes[host.alias] = result
        detail = result.get("log", "").strip()
        status = []
        if result.get("shared"):
            status.append(f"shared={result['shared']}")
        if result.get("root_retained") is not None:
            status.append(f"root_retained={result['root_retained']}")
        suffix = (" " + " ".join(status)) if status else ""
        if detail and result["outcome"] not in ("uninstalled", "already-uninstalled"):
            suffix += f" — {detail[-240:]}"
        print(f"{host.alias}: {result['outcome']}{suffix}")

    successes = sum(
        result["outcome"] in ("uninstalled", "already-uninstalled")
        for result in outcomes.values()
    )
    failures = [result for result in outcomes.values()
                if result["outcome"] not in ("uninstalled", "already-uninstalled")]
    if not failures:
        return OK
    if successes:
        return PARTIAL
    if all(result["outcome"] == "aborted" for result in failures):
        return VERIFY
    return FAILED


def cmd_status(args):
    fleet = _load(args)
    hosts = _select(fleet, args)
    results = run_fleet(hosts, lambda h: ops.probe_host(h, args.app), jobs=args.jobs)
    records = []
    for h in hosts:
        r = results[h.alias]
        if isinstance(r, Exception):
            r = {"alias": h.alias, "reachable": False, "verdict": "UNREACHABLE"}
        records.append(r)
    report = ops.build_status_report(records)
    for r in records:
        temps = (r.get("temps_c") or {})
        print(f"{r['alias']}: {r.get('verdict')} version={r.get('version') or '-'} "
              f"service={r.get('service') or '-'} disk_free={r.get('disk_free_pct')}% "
              f"temp_max={temps.get('max')}C" + (" DRIFT" if r.get("version_drift") else ""))
    print(f"fleet verdict: {report['fleet_verdict']}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(f"report written to {args.json}")
    return OK if report["fleet_verdict"] in ("HEALTHY", "ATTENTION") else PARTIAL


def cmd_releases(args):
    fleet = _load(args)
    hosts = _select(fleet, args)
    results = run_fleet(hosts, lambda h: ops.probe_host(h, args.app), jobs=args.jobs)
    for h in hosts:
        r = results[h.alias]
        if isinstance(r, Exception) or not r.get("reachable"):
            print(f"{h.alias}: UNREACHABLE")
            continue
        rel = ", ".join(r.get("releases") or []) or "-"
        print(f"{h.alias}: current={r.get('version') or '-'} previous={r.get('previous') or '-'} releases=[{rel}]")
    return OK


def main(argv=None):
    p = argparse.ArgumentParser(prog="rkdeploy",
                                description="Deploy RKNN apps to Rockchip boards as systemd services.")
    p.add_argument("--version", action="version", version=f"rkdeploy {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("pack", help="pack an app directory into a deployable tarball")
    sp.add_argument("--manifest", default="rkdeploy.yaml")
    sp.add_argument("--out", default="dist")
    sp.set_defaults(fn=cmd_pack)

    for name, fn, helptext in (("deploy", cmd_deploy, "push a package to the fleet"),
                               ("rollback", cmd_rollback, "roll boards back to a previous release")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--fleet", required=True)
        sp.add_argument("--hosts", default=None, help="comma aliases or tag:<tag>")
        sp.add_argument("--jobs", type=int, default=4)
        if name == "deploy":
            sp.add_argument("package")
            sp.add_argument("--no-auto-rollback", action="store_true")
            sp.add_argument("--dry-run", action="store_true",
                            help="preview deployment without contacting boards")
        else:
            sp.add_argument("--app", required=True)
            sp.add_argument("--to", default=None)
        sp.set_defaults(fn=fn)

    sp = sub.add_parser("uninstall", help="remove an app from selected fleet boards")
    sp.add_argument("--fleet", required=True)
    sp.add_argument("--app", required=True)
    sp.add_argument("--hosts", default=None, help="comma aliases or tag:<tag>")
    sp.add_argument("--jobs", type=int, default=4)
    sp.add_argument("--allow-last-copy", action="store_true",
                    help="explicitly allow removing the fleet's final installed copy")
    sp.add_argument("--purge", action="store_true",
                    help="also delete the app's shared/ data (irreversible)")
    sp.set_defaults(fn=cmd_uninstall)

    sp = sub.add_parser("status", help="fleet status report")
    sp.add_argument("--fleet", required=True)
    sp.add_argument("--app", required=True)
    sp.add_argument("--hosts", default=None)
    sp.add_argument("--jobs", type=int, default=4)
    sp.add_argument("--json", default=None)
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("releases", help="list installed releases per board")
    sp.add_argument("--fleet", required=True)
    sp.add_argument("--app", required=True)
    sp.add_argument("--hosts", default=None)
    sp.add_argument("--jobs", type=int, default=4)
    sp.set_defaults(fn=cmd_releases)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (ManifestError, FleetError) as e:
        print(f"error: {e}", file=sys.stderr)
        return USAGE
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return USAGE


if __name__ == "__main__":
    sys.exit(main())

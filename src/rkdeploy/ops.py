"""Controller-side operations: deploy / rollback / uninstall / status / releases."""
import json
import os
import re
import shlex
import tarfile
import tempfile
from importlib import resources

from . import fleet as fl
from .unit import render_unit

RESULT_RE = re.compile(r"RKDEPLOY_RESULT=(\S+)((?:\s+\S+)*)")


def board_script(name):
    return resources.files("rkdeploy.board").joinpath(name).read_text(encoding="utf-8")


def parse_result(output):
    """Extract the RKDEPLOY_RESULT line a board script prints."""
    found = None
    for line in (output or "").splitlines():
        m = RESULT_RE.search(line)
        if m:
            kv = dict(p.split("=", 1) for p in m.group(2).split() if "=" in p)
            found = (m.group(1), kv)
    return found


def read_package_meta(package_path):
    with tarfile.open(package_path, "r:gz") as tf:
        pkg = json.loads(tf.extractfile("package.json").read().decode())
    return pkg


def read_package_unit_template(package_path):
    """Read the unit template from a package without extracting it to disk."""
    with tarfile.open(package_path, "r:gz") as tf:
        template = tf.extractfile("service.template")
        if template is None:
            raise ValueError("package does not contain service.template")
        return template.read().decode("utf-8")


def preview_deploy_host(host, package_path, meta, unit_template):
    """Build the controller-side dry-run preview for one target board."""
    app = meta["name"]
    root = host.root_for(app)
    unit_name = f"rkdeploy-{app}.service"
    unit_content = render_unit(
        unit_template,
        install_root=root,
        app_name=app,
        app_version=meta["version"],
        run_user=meta["service"]["user"],
    )
    return {
        "alias": host.alias,
        "package": os.path.basename(package_path),
        "target_root": root,
        "unit_name": unit_name,
        "unit_content": unit_content,
    }


def deploy_host(host, package_path, app, no_auto_rollback=False):
    root = host.root_for(app)
    inbox = f"/tmp/rkdeploy-inbox-{app}"
    remote_pkg = f"{inbox}/{os.path.basename(package_path)}"
    r = fl.run_ssh(host, f"mkdir -p {inbox}")
    if r.returncode != 0:
        return {"alias": host.alias, "outcome": "unreachable", "detail": r.stderr.strip()[:300]}
    r = fl.scp_to(host, package_path, remote_pkg)
    if r.returncode != 0:
        return {"alias": host.alias, "outcome": "upload-failed", "detail": r.stderr.strip()[:300]}
    env = "RKDEPLOY_NO_AUTO_ROLLBACK=1 " if no_auto_rollback else ""
    cmd = (f"d=$(mktemp -d) && tar -xzf {remote_pkg} -C $d install.sh && "
           f"{env}sh $d/install.sh {remote_pkg} {root}; rc=$?; rm -rf $d; exit $rc")
    r = fl.run_ssh(host, cmd, timeout=900)
    parsed = parse_result(r.stdout)
    outcome = parsed[0] if parsed else ("error" if r.returncode else "unknown")
    return {"alias": host.alias, "outcome": outcome,
            "version": (parsed[1].get("version") if parsed else None),
            "rc": r.returncode, "log": (r.stdout + r.stderr).strip()[-1200:]}


def rollback_host(host, app, to=None):
    root = host.root_for(app)
    script = board_script("rollback.sh")
    args = f"{shlex.quote(root)} {shlex.quote(app)}"
    if to:
        args += f" {shlex.quote(to)}"
    r = fl.run_ssh(host, f"sh -s -- {args}", stdin_data=script, timeout=600)
    parsed = parse_result(r.stdout)
    outcome = parsed[0] if parsed else ("error" if r.returncode else "unknown")
    return {"alias": host.alias, "outcome": outcome,
            "version": (parsed[1].get("version") if parsed else None),
            "rc": r.returncode, "log": (r.stdout + r.stderr).strip()[-1200:]}


def uninstall_host(host, app, purge=False):
    """Uninstall one app from one board using the packaged POSIX shell script."""
    root = host.root_for(app)
    script = board_script("uninstall.sh")
    purge_arg = "1" if purge else "0"
    cmd = f"sh -s -- {shlex.quote(root)} {shlex.quote(app)} {purge_arg}"
    r = fl.run_ssh(host, cmd, stdin_data=script, timeout=600)
    parsed = parse_result(r.stdout)
    outcome = parsed[0] if parsed else ("error" if r.returncode else "unknown")
    details = parsed[1] if parsed else {}
    return {"alias": host.alias, "outcome": outcome,
            "shared": details.get("shared"),
            "root_retained": details.get("root_retained"),
            "rc": r.returncode, "log": (r.stdout + r.stderr).strip()[-1200:]}


def probe_host(host, app):
    root = host.root_for(app)
    script = board_script("probe.sh")
    cmd = f"sh -s -- {shlex.quote(root)} {shlex.quote(app)}"
    r = fl.run_ssh(host, cmd, stdin_data=script, timeout=120)
    kv = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()

    # A marker emitted at the beginning of the script is not proof that the
    # whole inventory completed. Require a successful SSH command and all
    # inventory-defining fields before this board can count as a remaining
    # application copy.
    required = ("PROBE", "INSTALLED", "SERVICE_ACTIVE", "SERVICE_ENABLED")
    complete = all(key in kv for key in required) and kv.get("PROBE") == "1"
    if r.returncode != 0 or not complete:
        record = parse_probe(host.alias, {})
        record["probe_error"] = (
            f"remote probe exited {r.returncode}" if r.returncode != 0
            else "remote probe returned incomplete inventory"
        )
        return record
    return parse_probe(host.alias, kv)


def parse_probe(alias, kv):
    def as_int(v, default=None):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default
    rec = {
        "alias": alias,
        "reachable": kv.get("PROBE") == "1",
        "installed": kv.get("INSTALLED") == "1",
        "version": kv.get("VERSION") or None,
        "previous": kv.get("PREVIOUS") or None,
        "service": kv.get("SERVICE_ACTIVE"),
        "enabled": kv.get("SERVICE_ENABLED"),
        "main_pid": as_int(kv.get("MAIN_PID"), 0),
        "service_since": kv.get("SERVICE_SINCE") or None,
        "releases": [x for x in (kv.get("RELEASES") or "").split(",") if x],
        "disk_free_pct": as_int(kv.get("DISK_FREE_PCT")),
        "temps_c": {"soc": as_int(kv.get("TEMP_SOC_C")), "max": as_int(kv.get("TEMP_MAX_C"))},
        "health": kv.get("HEALTH"),
        "journal_errors_1h": as_int(kv.get("JOURNAL_ERRORS_1H"), -1),
    }
    rec["verdict"] = verdict_for(rec)
    return rec


def verdict_for(rec):
    if not rec.get("reachable"):
        return "UNREACHABLE"
    if not rec.get("installed"):
        return "NOT_INSTALLED"
    if rec.get("service") != "active" or rec.get("health") == "fail":
        return "UNHEALTHY"
    warn = False
    if rec.get("disk_free_pct") is not None:
        if rec["disk_free_pct"] < 5:
            return "UNHEALTHY"
        if rec["disk_free_pct"] < 10:
            warn = True
    tmax = (rec.get("temps_c") or {}).get("max")
    if tmax is not None:
        if tmax >= 85:
            return "UNHEALTHY"
        if tmax >= 75:
            warn = True
    if len(rec.get("releases") or []) < 2:
        warn = True
    return "ATTENTION" if warn else "HEALTHY"


def build_status_report(records):
    """Add fleet-level drift detection and the summary block."""
    versions = [r["version"] for r in records if r.get("reachable") and r.get("version")]
    majority = None
    if versions:
        majority = max(set(versions), key=versions.count)
    for r in records:
        r["version_drift"] = bool(majority and r.get("version") and r["version"] != majority)
        if r["version_drift"] and r.get("verdict") == "HEALTHY":
            r["verdict"] = "ATTENTION"
    order = {"UNHEALTHY": 0, "UNREACHABLE": 0, "ATTENTION": 1, "NOT_INSTALLED": 1, "HEALTHY": 2}
    worst = None
    for r in records:
        v = r.get("verdict", "HEALTHY")
        if worst is None or order.get(v, 2) < order.get(worst, 2):
            worst = v
    from . import __version__
    import datetime
    return {
        "schema": 1,
        "tool_version": __version__,
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "fleet_verdict": worst or "HEALTHY",
        "fleet": records,
    }

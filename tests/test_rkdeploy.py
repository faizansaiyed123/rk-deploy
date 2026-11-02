"""rk-deploy test suite (unittest style; also runs under pytest)."""
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import unittest
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

from rkdeploy import manifest as mf  # noqa: E402
from rkdeploy.cli import main  # noqa: E402
from rkdeploy.fleet import Fleet, load_fleet  # noqa: E402
from rkdeploy.ops import board_script, build_status_report, parse_probe, parse_result, probe_host  # noqa: E402
from rkdeploy.pack import build_package  # noqa: E402
from rkdeploy.unit import render_unit_template  # noqa: E402

DEMO = os.path.join(REPO, "examples", "demo")
FAKE = os.path.join(REPO, "tests", "fake_systemctl.py")


_counter = [0]


def make_app(tmp, version="1.0.0", broken_health=False):
    _counter[0] += 1
    app = os.path.join(tmp, f"app-{version}-{_counter[0]}")
    shutil.copytree(DEMO, app)
    y = os.path.join(app, "rkdeploy.yaml")
    text = open(y).read().replace("version: 1.0.0", f"version: {version}")
    if broken_health:
        text = text.replace("shared/heartbeat.json", "shared/never-written.json")
        text = text.replace("timeout_sec: 15", "timeout_sec: 3")
    open(y, "w").write(text)
    return app


class ManifestTests(unittest.TestCase):
    def test_demo_manifest_loads(self):
        m = mf.load_manifest(os.path.join(DEMO, "rkdeploy.yaml"))
        self.assertEqual(m["name"], "demo")
        self.assertEqual(m["runtime"]["entry"], "app.py")
        self.assertEqual(m["healthcheck"]["type"], "file")

    def _bad(self, mutate):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            app = make_app(td)
            y = os.path.join(app, "rkdeploy.yaml")
            text = open(y).read()
            open(y, "w").write(mutate(text))
            with self.assertRaises(mf.ManifestError):
                mf.load_manifest(y)

    def test_bad_name_rejected(self):
        self._bad(lambda t: t.replace("name: demo", "name: Demo_X"))

    def test_missing_healthcheck_rejected(self):
        self._bad(lambda t: t.split("healthcheck:")[0])

    def test_keep_releases_floor(self):
        self._bad(lambda t: t.replace("keep_releases: 3", "keep_releases: 1"))

    def test_entry_traversal_rejected(self):
        self._bad(lambda t: t.replace("entry: app.py", "entry: ../evil.py"))


class PackTests(unittest.TestCase):
    def test_package_contents_and_reproducibility(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p1, _ = build_package(os.path.join(DEMO, "rkdeploy.yaml"), os.path.join(td, "a"))
            p2, _ = build_package(os.path.join(DEMO, "rkdeploy.yaml"), os.path.join(td, "b"))
            h = lambda p: hashlib.sha256(open(p, "rb").read()).hexdigest()
            self.assertEqual(h(p1), h(p2), "same source must build identical bytes")
            with tarfile.open(p1) as tf:
                names = sorted(tf.getnames())
                pkg = json.loads(tf.extractfile("package.json").read())
                sums = tf.extractfile("SHA256SUMS").read().decode()
            for expected in ("app.py", "models/weights.bin", "package.json",
                             "package.env", "service.template", "install.sh",
                             "DEPENDENCIES.json", "rkdeploy.yaml", "SHA256SUMS"):
                self.assertIn(expected, names)
            self.assertEqual(pkg["name"], "demo")
            self.assertEqual({f["path"] for f in pkg["files"]},
                             {"app.py", "models/weights.bin"})
            self.assertIn("app.py", sums)

    def test_undeclared_files_not_packed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            app = make_app(td)
            open(os.path.join(app, "secret-notes.txt"), "w").write("should not travel")
            p, _ = build_package(os.path.join(app, "rkdeploy.yaml"), os.path.join(td, "out"))
            with tarfile.open(p) as tf:
                self.assertNotIn("secret-notes.txt", tf.getnames())


class UnitTests(unittest.TestCase):
    def test_template_placeholders_and_env(self):
        m = mf.load_manifest(os.path.join(DEMO, "rkdeploy.yaml"))
        t = render_unit_template(m)
        self.assertIn("ExecStart=/usr/bin/python3 @INSTALL_ROOT@/current/app.py", t)
        self.assertIn("User=@RUN_USER@", t)
        self.assertIn("Environment=RKDEPLOY_VERSION=@APP_VERSION@", t)


class FleetTests(unittest.TestCase):
    def test_load_and_select(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "fleet.yaml")
            open(f, "w").write(
                "hosts:\n"
                "  - {alias: a, address: 192.0.2.1, user: root, tags: [pilot]}\n"
                "  - {alias: b, address: 192.0.2.2, user: ops, install_root: /srv/x}\n")
            fleet = load_fleet(f)
            self.assertEqual([h.alias for h in fleet.select("tag:pilot")], ["a"])
            self.assertEqual(fleet.select("b")[0].root_for("demo"), "/srv/x")
            self.assertEqual(fleet.select("a")[0].root_for("demo"), "/opt/demo")
            with self.assertRaises(Exception):
                fleet.select("nope")


class DryRunTests(unittest.TestCase):
    def test_dry_run_previews_each_host_without_remote_operations(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            package, _ = build_package(
                os.path.join(DEMO, "rkdeploy.yaml"),
                os.path.join(td, "dist"),
            )
            fleet_path = os.path.join(td, "fleet.yaml")
            with open(fleet_path, "w", encoding="utf-8") as fleet_file:
                fleet_file.write(
                    "hosts:\n"
                    "  - alias: default\n"
                    "    address: 192.0.2.10\n"
                    "    user: root\n"
                    "  - alias: custom\n"
                    "    address: 192.0.2.11\n"
                    "    user: deploy\n"
                    "    install_root: /srv/apps\n"
                )

            output = io.StringIO()
            with patch("rkdeploy.cli.run_fleet") as run_fleet:
                with patch("rkdeploy.fleet.run_ssh") as run_ssh:
                    with patch("rkdeploy.fleet.scp_to") as scp_to:
                        with redirect_stdout(output):
                            result = main([
                                "deploy", "--fleet", fleet_path, "--dry-run", package,
                            ])

            self.assertEqual(result, 0)
            run_fleet.assert_not_called()
            run_ssh.assert_not_called()
            scp_to.assert_not_called()

            rendered = output.getvalue()
            self.assertIn("default: dry-run", rendered)
            self.assertIn("Package: demo-1.0.0.tar.gz", rendered)
            self.assertIn("Target root: /opt/demo", rendered)
            self.assertIn("Unit file: rkdeploy-demo.service", rendered)
            self.assertIn("ExecStart=/usr/bin/python3 /opt/demo/current/app.py", rendered)
            self.assertIn("User=root", rendered)
            self.assertNotIn("@INSTALL_ROOT@", rendered)
            self.assertNotIn("@RUN_USER@", rendered)

            self.assertIn("custom: dry-run", rendered)
            self.assertIn("Target root: /srv/apps", rendered)
            self.assertIn("ExecStart=/usr/bin/python3 /srv/apps/current/app.py", rendered)
            self.assertIn("Dry run complete; no SSH commands", rendered)


class UninstallCLITests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.td = tempfile.mkdtemp(prefix="rkdeploy-uninstall-cli-")
        self.fleet_path = os.path.join(self.td, "fleet.yaml")
        with open(self.fleet_path, "w", encoding="utf-8") as f:
            f.write(
                "hosts:\n"
                "  - {alias: a, address: 192.0.2.1, user: root}\n"
                "  - {alias: b, address: 192.0.2.2, user: root}\n"
            )

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def _probe(self, alias, installed=True, reachable=True):
        return {"alias": alias, "reachable": reachable, "installed": installed}

    def test_refuses_to_remove_the_last_copy_without_override(self):
        with patch("rkdeploy.cli.ops.probe_host", side_effect=lambda h, app:
                   self._probe(h.alias, installed=(h.alias == "a"))):
            with patch("rkdeploy.cli.ops.uninstall_host") as uninstall:
                result = main([
                    "uninstall", "--fleet", self.fleet_path, "--app", "demo",
                    "--hosts", "a", "--jobs", "1",
                ])
        self.assertEqual(result, 4)
        uninstall.assert_not_called()

    def test_allows_selected_removal_when_another_board_keeps_a_copy(self):
        output = io.StringIO()
        with patch("rkdeploy.cli.ops.probe_host", side_effect=lambda h, app:
                   self._probe(h.alias, installed=True)):
            with patch("rkdeploy.cli.ops.uninstall_host",
                       return_value={"alias": "a", "outcome": "uninstalled",
                                     "shared": "preserved", "root_retained": "1",
                                     "rc": 0, "log": ""}) as uninstall:
                with redirect_stdout(output):
                    result = main([
                        "uninstall", "--fleet", self.fleet_path, "--app", "demo",
                        "--hosts", "a", "--jobs", "1",
                    ])
        self.assertEqual(result, 0)
        self.assertIn("a: uninstalled shared=preserved root_retained=1", output.getvalue())
        uninstall.assert_called_once()
        self.assertEqual(uninstall.call_args.args[1], "demo")
        self.assertFalse(uninstall.call_args.kwargs["purge"])

    def test_unreachable_board_aborts_inventory_before_any_uninstall(self):
        with patch("rkdeploy.cli.ops.probe_host", side_effect=lambda h, app:
                   self._probe(h.alias, installed=(h.alias == "a"),
                               reachable=(h.alias == "a"))):
            with patch("rkdeploy.cli.ops.uninstall_host") as uninstall:
                result = main([
                    "uninstall", "--fleet", self.fleet_path, "--app", "demo",
                    "--hosts", "a", "--jobs", "1",
                ])
        self.assertEqual(result, 4)
        uninstall.assert_not_called()

    def test_allow_last_copy_is_an_explicit_override(self):
        with patch("rkdeploy.cli.ops.probe_host", side_effect=lambda h, app:
                   self._probe(h.alias, installed=(h.alias == "a"))):
            with patch("rkdeploy.cli.ops.uninstall_host",
                       return_value={"alias": "a", "outcome": "uninstalled", "rc": 0, "log": ""}) as uninstall:
                result = main([
                    "uninstall", "--fleet", self.fleet_path, "--app", "demo",
                    "--hosts", "a", "--jobs", "1", "--allow-last-copy",
                ])
        self.assertEqual(result, 0)
        self.assertEqual(uninstall.call_count, 1)

    def test_allow_last_copy_does_not_bypass_unreachable_inventory(self):
        with patch("rkdeploy.cli.ops.probe_host", side_effect=lambda h, app:
                   self._probe(h.alias, installed=(h.alias == "a"),
                               reachable=(h.alias == "a"))):
            with patch("rkdeploy.cli.ops.uninstall_host") as uninstall:
                result = main([
                    "uninstall", "--fleet", self.fleet_path, "--app", "demo",
                    "--hosts", "a", "--jobs", "1", "--allow-last-copy",
                ])
        self.assertEqual(result, 4)
        uninstall.assert_not_called()

    def test_duplicate_fleet_aliases_abort_before_inventory_or_uninstall(self):
        with open(self.fleet_path, "w", encoding="utf-8") as f:
            f.write(
                "hosts:\n"
                "  - {alias: same, address: 192.0.2.1, user: root}\n"
                "  - {alias: same, address: 192.0.2.2, user: root}\n"
            )
        with patch("rkdeploy.cli.ops.probe_host") as probe:
            with patch("rkdeploy.cli.ops.uninstall_host") as uninstall:
                result = main([
                    "uninstall", "--fleet", self.fleet_path, "--app", "demo",
                    "--jobs", "1",
                ])
        self.assertEqual(result, 2)
        probe.assert_not_called()
        uninstall.assert_not_called()

    def test_purge_flag_is_forwarded_to_board_operation(self):
        with patch("rkdeploy.cli.ops.probe_host", side_effect=lambda h, app:
                   self._probe(h.alias, installed=True)):
            with patch("rkdeploy.cli.ops.uninstall_host",
                       return_value={"alias": "a", "outcome": "uninstalled", "rc": 0, "log": ""}) as uninstall:
                result = main([
                    "uninstall", "--fleet", self.fleet_path, "--app", "demo",
                    "--hosts", "a", "--jobs", "1", "--purge",
                ])
        self.assertEqual(result, 0)
        self.assertTrue(uninstall.call_args.kwargs["purge"])


class ProbeTests(unittest.TestCase):
    def test_incomplete_remote_probe_is_not_a_valid_inventory_record(self):
        from types import SimpleNamespace

        host = SimpleNamespace(alias="board-a", root_for=lambda app: "/opt/demo")
        response = SimpleNamespace(returncode=1, stdout="PROBE=1\n", stderr="remote command failed")
        with patch("rkdeploy.ops.fl.run_ssh", return_value=response):
            record = probe_host(host, "demo")

        self.assertFalse(record["reachable"])
        self.assertEqual(record["verdict"], "UNREACHABLE")
        self.assertEqual(record["probe_error"], "remote probe exited 1")

    def test_successful_probe_requires_inventory_fields(self):
        from types import SimpleNamespace

        host = SimpleNamespace(alias="board-a", root_for=lambda app: "/opt/demo")
        response = SimpleNamespace(
            returncode=0,
            stdout="PROBE=1\nINSTALLED=0\nSERVICE_ACTIVE=inactive\nSERVICE_ENABLED=disabled\nVERSION=\nPREVIOUS=\n",
            stderr="",
        )
        with patch("rkdeploy.ops.fl.run_ssh", return_value=response):
            record = probe_host(host, "demo")

        self.assertTrue(record["reachable"])
        self.assertFalse(record["installed"])

    def test_parse_and_verdicts(self):
        kv = {"PROBE": "1", "INSTALLED": "1", "VERSION": "1.0.0", "SERVICE_ACTIVE": "active",
              "SERVICE_ENABLED": "enabled", "MAIN_PID": "42", "RELEASES": "1.0.0,1.1.0",
              "DISK_FREE_PCT": "50", "TEMP_SOC_C": "49", "TEMP_MAX_C": "51",
              "HEALTH": "ok", "JOURNAL_ERRORS_1H": "0"}
        rec = parse_probe("b1", kv)
        self.assertEqual(rec["verdict"], "HEALTHY")
        kv2 = dict(kv, DISK_FREE_PCT="3")
        self.assertEqual(parse_probe("b1", kv2)["verdict"], "UNHEALTHY")
        kv3 = dict(kv, TEMP_MAX_C="80")
        self.assertEqual(parse_probe("b1", kv3)["verdict"], "ATTENTION")

    def test_drift_detection(self):
        base = {"alias": "x", "reachable": True, "installed": True, "version": "1.0.0",
                "service": "active", "health": "ok", "disk_free_pct": 50,
                "temps_c": {"soc": 40, "max": 41}, "releases": ["1.0.0", "0.9.0"],
                "verdict": "HEALTHY"}
        r1 = dict(base, alias="a")
        r2 = dict(base, alias="b", version="1.1.0")
        r3 = dict(base, alias="c", version="1.1.0")
        rep = build_status_report([r1, r2, r3])
        self.assertTrue(rep["fleet"][0]["version_drift"])
        self.assertEqual(rep["fleet"][0]["verdict"], "ATTENTION")

    def test_parse_result_line(self):
        out = "noise\nRKDEPLOY_RESULT=rolled-back version=1.0.0 failed-version=1.1.0\n"
        self.assertEqual(parse_result(out)[0], "rolled-back")
        self.assertEqual(parse_result(out)[1]["failed-version"], "1.1.0")
        self.assertIsNone(parse_result("nothing here"))


class BoardEndToEndTests(unittest.TestCase):
    """Run the real board scripts against a fake systemctl that really
    starts the demo app: install v1 -> install v2 -> rollback -> v1,
    plus the auto-rollback path for a broken release."""

    def setUp(self):
        import tempfile
        self.td = tempfile.mkdtemp(prefix="rkdeploy-e2e-")
        self.root = os.path.join(self.td, "board", "opt", "demo")
        self.sysd = os.path.join(self.td, "systemd")
        self.state = os.path.join(self.td, "fake-systemctl")
        os.makedirs(self.sysd)
        os.makedirs(self.state)
        self.fake = os.path.join(self.td, "systemctl")
        shutil.copy(FAKE, self.fake)
        os.chmod(self.fake, 0o755)
        self.env = dict(os.environ,
                        RKDEPLOY_SYSTEMCTL=self.fake,
                        RKDEPLOY_SYSTEMD_DIR=self.sysd,
                        FAKE_SYSTEMCTL_DIR=self.state)

    def tearDown(self):
        subprocess.run([self.fake, "stop", "rkdeploy-demo.service"],
                       env=self.env, capture_output=True)
        shutil.rmtree(self.td, ignore_errors=True)

    def _installer_from(self, pkg):
        out = os.path.join(self.td, f"install-{os.path.basename(pkg)}.sh")
        with tarfile.open(pkg) as tf:
            data = tf.extractfile("install.sh").read()
        open(out, "wb").write(data)
        return out

    def _board_script(self, name):
        from rkdeploy.ops import board_script
        p = os.path.join(self.td, name)
        open(p, "w").write(board_script(name))
        return p

    def _current(self):
        return os.path.basename(os.readlink(os.path.join(self.root, "current")))

    def _heartbeat_version(self, expected=None):
        hb = os.path.join(self.root, "shared", "heartbeat.json")
        deadline = time.time() + 10
        seen = None
        while time.time() < deadline:
            if os.path.exists(hb):
                try:
                    seen = json.load(open(hb))["version"]
                except Exception:
                    seen = None
                if expected is None or seen == expected:
                    return seen
            time.sleep(0.3)
        return seen

    def _install2(self, version, broken=False):
        app = make_app(self.td, version, broken_health=broken)
        pkg, _ = build_package(os.path.join(app, "rkdeploy.yaml"),
                               os.path.join(self.td, f"dist-{version}"))
        script = self._installer_from(pkg)
        return subprocess.run(["sh", script, pkg, self.root],
                              env=self.env, capture_output=True, text=True, timeout=120)


    def _uninstall(self, purge=False, env=None):
        script = self._board_script("uninstall.sh")
        return subprocess.run(
            ["sh", script, self.root, "demo", "1" if purge else "0"],
            env=env or self.env, capture_output=True, text=True, timeout=60,
        )


# The lifecycle test proper lives here (kept separate so the fake's
# process management is exercised exactly as on a real board).
class LifecycleTests(BoardEndToEndTests):
    def test_upgrade_rollback_and_auto_rollback(self):
        r = self._install2("1.0.0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self._current(), "1.0.0")
        self.assertEqual(self._heartbeat_version("1.0.0"), "1.0.0")
        state = json.load(open(os.path.join(self.root, "state.json")))
        self.assertEqual(state["current"], "1.0.0")
        marker = os.path.join(self.root, "shared", "marker.txt")
        open(marker, "w").write("keep-me")

        r = self._install2("1.0.0")  # same version again: immutable, refuse
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)

        r = self._install2("1.1.0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self._current(), "1.1.0")
        self.assertEqual(self._heartbeat_version("1.1.0"), "1.1.0")
        self.assertEqual(open(marker).read(), "keep-me")

        rb = subprocess.run(["sh", self._board_script("rollback.sh"),
                             self.root, "demo"],
                            env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(rb.returncode, 0, rb.stdout + rb.stderr)
        self.assertEqual(self._current(), "1.0.0")
        self.assertEqual(self._heartbeat_version("1.0.0"), "1.0.0")

        r = self._install2("1.2.0", broken=True)  # health never passes
        self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
        self.assertEqual(self._current(), "1.0.0")
        self.assertEqual(self._heartbeat_version("1.0.0"), "1.0.0")
        state = json.load(open(os.path.join(self.root, "state.json")))
        self.assertEqual(state["current"], "1.0.0")
        self.assertTrue(any(h["result"] == "rolled-back" for h in state["history"]))

    def test_probe_reports(self):
        r = self._install2("1.0.0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = subprocess.run(["sh", self._board_script("probe.sh"), self.root, "demo"],
                             env=self.env, capture_output=True, text=True, timeout=60)
        kv = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
        self.assertEqual(kv["VERSION"], "1.0.0")
        self.assertEqual(kv["SERVICE_ACTIVE"], "active")
        self.assertEqual(kv["HEALTH"], "ok")
        rec = parse_probe("testboard", kv)
        self.assertIn(rec["verdict"], ("HEALTHY", "ATTENTION"))


class UninstallBoardTests(BoardEndToEndTests):
    def test_uninstall_preserves_shared_data_and_status_reports_not_installed(self):
        installed = self._install2("1.0.0")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)

        shared_marker = os.path.join(self.root, "shared", "data", "keep.json")
        os.makedirs(os.path.dirname(shared_marker), exist_ok=True)
        with open(shared_marker, "w", encoding="utf-8") as f:
            f.write('{"keep": true}')

        unit = os.path.join(self.sysd, "rkdeploy-demo.service")
        self.assertTrue(os.path.isfile(unit))
        result = self._uninstall()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("shared=preserved", result.stdout)

        self.assertFalse(os.path.exists(unit))
        self.assertFalse(os.path.exists(os.path.join(self.root, "releases")))
        self.assertFalse(os.path.lexists(os.path.join(self.root, "current")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "state.json")))
        self.assertTrue(os.path.isfile(shared_marker))
        self.assertEqual(open(shared_marker, encoding="utf-8").read(), '{"keep": true}')

        probe = subprocess.run(
            ["sh", self._board_script("probe.sh"), self.root, "demo"],
            env=self.env, capture_output=True, text=True, timeout=30,
        )
        values = dict(line.split("=", 1) for line in probe.stdout.splitlines() if "=" in line)
        self.assertEqual(values["INSTALLED"], "0")
        self.assertEqual(parse_probe("board-a", values)["verdict"], "NOT_INSTALLED")

        repeated = self._uninstall()
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertIn("already-uninstalled", repeated.stdout)
        self.assertTrue(os.path.isfile(shared_marker))

    def test_purge_removes_shared_data_and_empty_install_root(self):
        installed = self._install2("1.0.0")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        marker = os.path.join(self.root, "shared", "data", "remove-me.txt")
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as f:
            f.write("purge me")

        result = self._uninstall(purge=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("shared=purged", result.stdout)
        self.assertFalse(os.path.exists(self.root))

    def test_purge_after_normal_uninstall_can_remove_preserved_data(self):
        installed = self._install2("1.0.0")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        marker = os.path.join(self.root, "shared", "data", "keep-until-purge.txt")
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as f:
            f.write("later")

        first = self._uninstall()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertTrue(os.path.isfile(marker))

        second = self._uninstall(purge=True)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertFalse(os.path.exists(marker))
        self.assertFalse(os.path.exists(self.root))

    def test_refuses_symlink_releases_without_removing_target(self):
        external = os.path.join(self.td, "external-releases")
        os.makedirs(external)
        marker = os.path.join(external, "keep.txt")
        with open(marker, "w", encoding="utf-8") as f:
            f.write("outside")
        os.makedirs(self.root)
        os.symlink(external, os.path.join(self.root, "releases"))

        result = self._uninstall()
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertTrue(os.path.islink(os.path.join(self.root, "releases")))
        self.assertTrue(os.path.isfile(marker))

    def test_shared_only_root_is_not_reported_as_installed(self):
        os.makedirs(os.path.join(self.root, "shared", "data"), exist_ok=True)
        marker = os.path.join(self.root, "shared", "data", "keep.txt")
        with open(marker, "w", encoding="utf-8") as f:
            f.write("persistent")

        probe = subprocess.run(
            ["sh", self._board_script("probe.sh"), self.root, "demo"],
            env=self.env, capture_output=True, text=True, timeout=30,
        )
        values = dict(line.split("=", 1) for line in probe.stdout.splitlines() if "=" in line)
        self.assertEqual(values["INSTALLED"], "0")
        self.assertTrue(os.path.isfile(marker))

    def test_uninstall_refuses_symlinked_parent_of_install_root(self):
        real_parent = os.path.join(self.td, "real-parent")
        linked_parent = os.path.join(self.td, "linked-parent")
        root = os.path.join(real_parent, "demo")
        releases = os.path.join(root, "releases", "1.0.0")
        os.makedirs(releases)
        marker = os.path.join(releases, "install.env")
        with open(marker, "w", encoding="utf-8") as f:
            f.write("APP_NAME=demo\n")
        os.symlink(real_parent, linked_parent)

        script = self._board_script("uninstall.sh")
        result = subprocess.run(
            ["sh", script, os.path.join(linked_parent, "demo"), "demo", "0"],
            env=self.env, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn("install-root-path-contains-symlink", result.stdout)
        self.assertTrue(os.path.isfile(marker))

    def test_purge_refuses_shared_symlink_without_touching_target(self):
        external = os.path.join(self.td, "external-shared")
        os.makedirs(external)
        marker = os.path.join(external, "keep.txt")
        with open(marker, "w", encoding="utf-8") as f:
            f.write("outside")
        os.makedirs(self.root)
        os.symlink(external, os.path.join(self.root, "shared"))

        result = self._uninstall(purge=True)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertTrue(os.path.islink(os.path.join(self.root, "shared")))
        self.assertTrue(os.path.isfile(marker))

    def test_unexpected_install_root_files_are_preserved(self):
        installed = self._install2("1.0.0")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        marker = os.path.join(self.root, "operator-notes.txt")
        with open(marker, "w", encoding="utf-8") as f:
            f.write("do not remove")

        result = self._uninstall()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(os.path.isfile(marker))
        self.assertEqual(open(marker, encoding="utf-8").read(), "do not remove")
        self.assertTrue(os.path.isdir(self.root))

    def test_systemd_state_query_failure_does_not_remove_application_files(self):
        installed = self._install2("1.0.0")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)

        broken_systemctl = os.path.join(self.td, "systemctl-state-query-fails")
        with open(FAKE, encoding="utf-8") as source:
            fake_source = source.read()
        old_active = (
            '    if cmd == "is-active":\n'
            '        pid = read_pid(unit)\n'
            '        if pid and alive(pid):\n'
            '            print("active")\n'
            '            return 0\n'
            '        print("inactive")\n'
            '        return 3'
        )
        self.assertIn(old_active, fake_source)
        with open(broken_systemctl, "w", encoding="utf-8") as f:
            f.write(fake_source.replace(old_active, '    if cmd == "is-active":\n        return 1'))
        os.chmod(broken_systemctl, 0o755)

        env = dict(self.env, RKDEPLOY_SYSTEMCTL=broken_systemctl)
        result = self._uninstall(env=env)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn("cannot-query-service-state", result.stdout)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "current", "install.env")))
        self.assertTrue(os.path.isfile(os.path.join(self.sysd, "rkdeploy-demo.service")))

    def test_stop_failure_does_not_remove_application_files(self):
        installed = self._install2("1.0.0")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)

        broken_systemctl = os.path.join(self.td, "systemctl-stop-fails")
        with open(FAKE, encoding="utf-8") as source:
            fake_source = source.read()
        old_stop = '    if cmd == "stop":\n        stop(unit)\n        return 0'
        new_stop = '    if cmd == "stop":\n        return 1'
        self.assertIn(old_stop, fake_source)
        with open(broken_systemctl, "w", encoding="utf-8") as f:
            f.write(fake_source.replace(old_stop, new_stop))
        os.chmod(broken_systemctl, 0o755)

        env = dict(self.env, RKDEPLOY_SYSTEMCTL=broken_systemctl)
        result = self._uninstall(env=env)
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "current", "install.env")))
        self.assertTrue(os.path.isfile(os.path.join(self.sysd, "rkdeploy-demo.service")))


if __name__ == "__main__":
    unittest.main()

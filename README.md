# rk-deploy

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

**Ship RKNN apps to a fleet of Rockchip boards like you mean it:**
describe the app in one manifest → one command packs it → it lands on every
board as a systemd service → push the next version, roll back in seconds,
uninstall safely, and get a fleet status report you can trust.

rk-deploy is a deployment and small-fleet tool for RKNN applications on
RK3588 / RK3576 / RK356x boards, built for the people who deliver and
maintain them: display vendors, parking and security integrators, and
anyone currently copying tarballs over scp and hand-writing `.service`
files per board.

## Status

**v0.1.0 released (2026-11-02).** The full pipeline is implemented and
validated end to end on a real RK3588 board (Ubuntu 22.04, systemd 249)
using the shipped CLI — not a hand-walked prototype:

| Step | Result |
|---|---|
| deploy demo 1.0.0 | ok, 21 s (three SSH connections through a tunnel dominate) |
| status | ATTENTION — correctly flagged "only one release, no rollback candidate yet" |
| deploy demo 1.1.0 | ok, 19 s |
| status | HEALTHY (SoC 49 °C, disk 13% free) |
| deploy broken 1.2.0 | health check failed → **automatic rollback to 1.1.0**, exit code 6, 26 s |
| rollback to 1.0.0 (explicit) | ok; final status HEALTHY |

A real status report from that run is in
[examples/status-report-rk3588.json](examples/status-report-rk3588.json),
and the earlier by-hand feasibility walk (105 s, seven pitfalls that
shaped the spec) is documented in [docs/SPEC.md](docs/SPEC.md).

Install:

```sh
pip install git+https://github.com/anguoyang/rk-deploy
# or from a clone: pip install .
rkdeploy --version
```

## How it works

```yaml
# rkdeploy.yaml
name: demo
version: 1.1.0
runtime:
  type: python
  entry: app.py
models:
  - path: models/weights.rknn
healthcheck:
  type: file
  path: "@INSTALL_ROOT@/shared/heartbeat.json"
  timeout_sec: 15
```

```sh
rkdeploy pack                                  # → demo-1.1.0.tar.gz (checksummed)
rkdeploy deploy --fleet fleet.yaml demo-1.1.0.tar.gz
rkdeploy deploy --fleet fleet.yaml --dry-run demo-1.1.0.tar.gz  # preview only, no SSH
rkdeploy rollback --fleet fleet.yaml --app demo
rkdeploy status --fleet fleet.yaml             # JSON + per-board summary
rkdeploy uninstall --fleet fleet.yaml --app demo
rkdeploy uninstall --fleet fleet.yaml --app demo --allow-last-copy
rkdeploy uninstall --fleet fleet.yaml --app demo --purge
rkdeploy uninstall --fleet fleet.yaml --app demo --allow-last-copy --purge
```

The uninstall command can also take `--hosts` to select boards. Before it
removes anything, the controller probes every board in the fleet; an
unreachable board aborts the operation. `--allow-last-copy` only overrides
the final-copy safeguard. `--purge` deletes persistent `shared/` data and
should be used only when that data is no longer needed.

Design rules that will not change:

- **Generic packages only.** rk-deploy knows nothing about the app it
  ships. Model files are transported as opaque bytes and never parsed.
- **Immutable releases + atomic switch.** Each version unpacks into its
  own directory; going live (or rolling back) is a single symlink swap.
- **Data outlives versions.** Config and data live in a `shared/`
  directory that upgrades, rollbacks and normal uninstalls never touch.
  `uninstall --purge` is the explicit data-deletion option.
- **Last-copy protection.** Uninstall inventories the complete fleet and
  refuses to remove its final installed copy unless `--allow-last-copy`
  is explicitly supplied. Unreachable boards make the inventory fail closed.
- **Failed health check = automatic rollback** to the previous version,
  with an explicit verdict — never a half-installed board.
- **No agent, no new ports, no credential collection.** Plain SSH/scp
  with your existing keys; no resident agent or custom transport protocol.
- **The board stays dumb.** All parsing happens on the controller; the
  board runs POSIX shell lifecycle scripts and stock systemd.

## Roadmap

- **v0.1** (week of Nov 9, 2026): `pack` / `deploy` / `rollback` /
  `status` / `releases`, fleet push over SSH, automatic rollback.
- **v0.2**: fleet HTML status report, optional resumable transfer for
  large model packages.

## Related

Part of a small family of open Rockchip edge tools by
[Site AI Gateway](https://site-ai-gateway.com):
[rk-edge-doctor](https://github.com/anguoyang/rk-edge-doctor) (board /
model / RTSP diagnostics) ·
[rk-bench](https://github.com/anguoyang/rk-bench) (measured RKNN model
leaderboard) ·
[awesome-rockchip](https://github.com/anguoyang/awesome-rockchip).

## License

Apache-2.0. See [LICENSE](LICENSE).

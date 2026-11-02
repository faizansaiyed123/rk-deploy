# rk-deploy — Specification v0.1 (frozen 2026-11-02)

Deployment and small-fleet tool for RKNN applications on Rockchip boards
(RK3588 / RK3576 / RK356x). One pipeline:

```
manifest → pack → push over SSH → install as systemd service
         → fleet roll-out / version rollback / status report
```

Audience: delivery and operations engineers at display vendors and
parking / security integrators who ship the same app to 5–50 boards.

Relationship to rk-edge-doctor: the doctor is read-only diagnostics
(is the board / model / camera healthy?); rk-deploy is the write side
(get the app onto the boards and manage its versions). They do not
overlap: rk-deploy's status only looks at the apps it deployed.

## Design principles

1. **Generic packages only.** The tool has zero knowledge of the app it
   ships. Model files are moved as opaque bytes — never opened, parsed,
   or content-checked (whole-package hashes only).
2. **Smart controller, dumb board.** All parsing and rendering decisions
   happen on the controller. The board runs POSIX `sh` lifecycle
   scripts plus stock coreutils/systemd: no extra packages, and it
   never parses YAML (board Python/library stacks vary too much across
   BSPs — validated finding #1 below).
3. **Immutable releases, atomic switch.** Every version unpacks into its
   own directory and is never overwritten. Going live is a symlink swap.
4. **Data outlives versions.** Config, data and board-local secrets live
   outside release directories, in `shared/`; upgrades and rollbacks
   never touch them.
5. **Failure rolls back.** If the post-deploy health check fails, the
   previous version is restored by default and the result is reported
   explicitly. No half-installed boards.
6. **No credential collection.** Only the user's existing SSH config,
   keys and agent are used. The tool never asks for or stores passwords.
7. **Offline, no telemetry, no registration, no license checks.**
8. License Apache-2.0; CLI name `rkdeploy`.

## CLI (v0.1)

```
rkdeploy pack     [--manifest rkdeploy.yaml] [--out dist/]
rkdeploy deploy   --fleet fleet.yaml <package.tar.gz> [--hosts tag:x|name,...] [--jobs 4] [--no-auto-rollback]
rkdeploy rollback --fleet fleet.yaml --app <name> [--to <version>] [--hosts ...]
rkdeploy uninstall --fleet fleet.yaml --app <name> [--hosts ...] [--jobs 4] [--allow-last-copy] [--purge]
rkdeploy status   --fleet fleet.yaml [--app <name>] [--json report.json]
rkdeploy releases --fleet fleet.yaml --app <name>
rkdeploy --version
```

Exit codes: 0 all ok · 2 manifest/usage error (no board touched) ·
4 verification/preflight blocked an operation before destructive changes ·
5 partial fleet operation failure · 6 deploy failed but auto-rollback
succeeded · 7 board operation failed or deploy rollback failed (human
review may be required).

## Manifest (`rkdeploy.yaml`, schema 1)

```yaml
name: demo                 # required, ^[a-z][a-z0-9-]{1,31}$; also names install dir + service
version: 1.1.0             # required, semver; same name+version can never be installed twice
description: Generic heartbeat demo
runtime:
  type: python             # python | binary | shell
  entry: app.py            # required, relative path, must exist at pack time
service:                   # all optional, feed the unit-generation rules
  user: root
  restart: on-failure      # no | always | on-failure | on-abnormal
  restart_sec: 2
  env: {}                  # non-secret env only; secrets are board-local (see Layout)
models:                    # optional; transport list only, contents never inspected
  - path: models/weights.rknn
files:                     # optional extra payload, same rules as models
  - path: assets/
depends:                   # optional board preflight list (checked, reported, never auto-installed)
  binaries: [python3]
  apt: [libopencv-core4.5d]
  python: [yaml]
  libs: [librknnrt.so]
  devices: [/dev/video0]   # warn-level when missing
healthcheck:               # required, one of file | command | systemd-only
  type: file
  path: "@INSTALL_ROOT@/shared/heartbeat.json"
  timeout_sec: 15
keep_releases: 3           # optional, default 3
```

At pack time the manifest is **rendered** into a machine-readable
`package.json` inside the package (name/version/entry/env/dependencies/
healthcheck/file list with per-file sha256). The board installer reads
only that file; the YAML snapshot is carried for humans, never parsed on
the board.

## Package format

`<name>-<version>.tar.gz`, entries sorted, relative paths only:

```
rkdeploy.yaml      # manifest snapshot (documentation)
package.json       # rendered machine metadata — the only file the board parses
SHA256SUMS         # per-file sha256, fully verified on the board before anything goes live
service.template   # systemd unit template with @INSTALL_ROOT@/@APP_NAME@/@APP_VERSION@/@RUN_USER@
install.sh         # generic board installer (POSIX sh, ships with rk-deploy, app-agnostic)
DEPENDENCIES.json  # rendered from `depends:` (binaries/apt/python/libs/devices)
<payload...>       # entry point + everything listed in files:/models:
```

Packaging rules:

- Only files the manifest declares are packed — whatever else sits in
  the directory does not travel. This is deliberate: it stops stray
  files (and stray secrets) from hitching a ride.
- Full manifest validation at pack time; any violation exits with
  code 2 and produces no package.
- Absolute paths and `..` are rejected on both sides.
- Dependencies are a **checklist, not an installer**: the board
  preflight verifies each class (`command -v`, ldconfig/common paths,
  import probe, dpkg) and reports what is missing. Hard misses
  (binaries/libs) abort that board's deploy; device misses warn.
  Automatic `apt install` is intentionally out of v0.1 — customer
  boards are often offline, and silently changing system packages on
  an unattended board is worse than a clear report.

## Board layout & systemd unit generation

Default install root `/opt/<name>/` (overridable per fleet host):

```
/opt/<name>/
  releases/<version>/    # immutable release directories
  current -> releases/<version>
  shared/                # config/ (board-local config + .env secrets), data/, app-owned persistence
  state.json             # {current, previous, history:[{version, action, at, result}]}
```

Unit name is always `rkdeploy-<name>.service` (the prefix avoids
colliding with vendor/system units). The unit is **rendered on the
board at install time** from the template — install root and run user
are board-level facts, so writing them into the package at pack time
would couple one package to one board's layout (validated finding #6).

| Manifest field | Unit directive |
|---|---|
| runtime.entry | `ExecStart=<interpreter><INSTALL_ROOT>/current/<entry>` (python → `/usr/bin/python3`, shell → `/bin/sh`, binary → direct) |
| (fixed) | `WorkingDirectory=<INSTALL_ROOT>/current` |
| service.user | `User=` |
| service.restart / restart_sec | `Restart=` / `RestartSec=` |
| service.env + fixed | `Environment=` lines; always injects `RKDEPLOY_SHARED=<INSTALL_ROOT>/shared` and `RKDEPLOY_VERSION=<version>` |
| `shared/config/<name>.env` if present | `EnvironmentFile=-...` — the only source of secrets |
| (fixed) | `After=network.target`, `Type=simple`, `WantedBy=multi-user.target`, `StandardOutput=journal` |

v0.1 supports `Type=simple` only; self-daemonizing apps must ship a
foreground wrapper (the spec says so rather than guessing). Install
order is fixed: render unit → `daemon-reload` → `enable` → `restart`.
Rollback re-renders the unit from the target release's template, so a
version whose unit definition changed rolls its unit back too.

## Fleet transport

`fleet.yaml` lives on the controller only and is never packed:

```yaml
hosts:
  - alias: board-a          # reports use aliases only, never real addresses
    address: 192.0.2.10     # documentation example
    user: root
    tags: [pilot]
  - alias: board-b
    address: 192.0.2.11
    user: root
    install_root: /srv/apps # optional override
    tags: [pilot]
ssh:
  connect_timeout_sec: 15
```

- Transport is plain **SSH/scp**: one tarball plus the installer
  script, one remote execution. No agent process, no new listening
  port, no custom protocol — integrators' existing jump hosts / VPNs /
  LANs just work.
- **One connection per board per operation** (validated finding #2):
  the health-check wait loop runs inside the board-side installer, not
  by polling from the controller. Controllers should also enable SSH
  connection multiplexing (ControlMaster).
- Default concurrency 4 (`--jobs`), `--serial` available; one board's
  failure never blocks the rest — the summary names it and the exit
  code is 5.
- v0.1 ships whole tarballs (typical RKNN packages are well under
  200 MB). A resumable rsync channel is a possible v0.2 addition, as a
  transport accelerator only — the hash verification step is never
  traded away for it.

## Deploy sequence (all inside one board-side run)

1. Upload to the board's inbox (`/tmp/rkdeploy-inbox-<name>/`), never
   directly into the install root.
2. `sha256sum -c SHA256SUMS` — any mismatch aborts (exit 4) with the
   live version untouched.
3. Preflight: free disk (fail if below 3× package size or below 5%),
   dependency checklist, run user exists, systemd available.
4. Unpack into `releases/<version>/`; if that directory already
   exists, refuse (immutability — reinstalling the same version is a
   deliberate manual operation, not a tool shortcut).
5. Render the unit, `daemon-reload`.
6. Record `previous` in state.json, then switch `current` atomically:
   build a fresh symlink and `mv -Tf` it over the old one (never
   `ln -sfn` on a symlink-to-directory — validated finding #5).
7. `systemctl restart`, then wait **on the board** for the health
   check (manifest timeout). For `file` health checks, existence is
   not enough: the file must have been written at or after this
   deploy's restart moment — a heartbeat file kept warm by the
   previous version's still-running process must not pass the new
   version (found by the lifecycle test suite during v0.1
   development, before any real board saw it).
8. Success: update state.json; prune oldest releases beyond
   `keep_releases` (never the current or previous one).
9. Failure: auto-rollback by default — switch back, re-render the old
   unit, restart, re-check — and report `DEPLOY_FAILED_ROLLED_BACK`
   (exit 6).

## Rollback

Rollback is a symlink switch + unit re-render + restart + health
check. Nothing is re-uploaded; the target release is already on the
board. Default target is state.json's `previous`; `--to <version>`
reaches any retained release. Normal deploys refuse packages whose
version is ≤ the running one — downgrades may only happen through the
explicit rollback command, and they are recorded in history.

Rollback never touches `shared/`. If an app changes its on-disk data
format incompatibly, old code reading new data is the app's
responsibility — rk-deploy does no data migration, in either
direction. This boundary is stated here, in the README, and in the
manual, because it is exactly the kind of thing people assume a deploy
tool does for them. It does not.

## Uninstall

`rkdeploy uninstall --fleet fleet.yaml --app <name>` removes the
rk-deploy-managed application from all fleet boards by default.
`--hosts` may narrow the boards changed, but it does not narrow the
safety inventory: before any destructive operation, the controller
probes every board in the fleet. Duplicate host aliases or any
unreachable/unverifiable board abort the operation before changes.

The controller refuses to remove the final installed copy when the
selected targets contain every installed copy. `--allow-last-copy`
explicitly overrides only this final-copy guard; it does not bypass
fleet inventory or duplicate-alias checks.

On each selected board, the uninstall script:

1. Validates the app name, install root and managed paths. It refuses
   symlinks at managed paths rather than following them into unrelated
   data.
2. Stops the `rkdeploy-<name>.service` unit, disables it, removes its
   unit file, and runs `systemctl daemon-reload`. A stop/disable/reload
   failure is reported and prevents the subsequent application-file
   removal.
3. Removes the `current` symlink, `releases/`, `state.json`,
   `history.jsonl` and the temporary state file.
4. Preserves `shared/` by default. `--purge` explicitly removes it.

The install root is removed only if it is empty after managed files are
removed. Unexpected files are left in place. Normal uninstall is
idempotent; a subsequent `--purge` can remove shared data preserved by
an earlier uninstall.

The status probe does not classify a shared-only install root as an
installed application. A deployment marker (state, current release
link, release install metadata, managed unit file, or running service)
is needed for `INSTALLED=1`.

## Uninstall (post-v0.1 addition)

```sh
rkdeploy uninstall --fleet fleet.yaml --app <name> [--hosts ...] [--jobs 4]
                 [--allow-last-copy] [--purge]
```

The controller probes every host in the fleet before starting an uninstall,
even when `--hosts` selects only part of the fleet. An unreachable host or an
incomplete probe aborts before any board is changed. By default, uninstall
refuses an operation that would remove the fleet's final installed copy.
`--allow-last-copy` explicitly overrides that guard; it does not override an
incomplete fleet inventory.

On each selected board, the board-side POSIX shell script stops and disables
the app's `rkdeploy-<name>.service`, removes the unit file, runs
`systemctl daemon-reload`, then removes the managed `current` symlink,
`releases/`, and deployment state/history files. The install root is removed
only when empty; unexpected files are not recursively deleted.

Normal uninstall preserves `shared/` and any other unexpected content.
`--purge` explicitly removes `shared/`; it is irreversible. A shared path
that is a symlink is refused during purge rather than following it. Repeated
uninstall is safe, and a later `--purge` can remove shared data left by a
previous normal uninstall.

Status detection treats a shared-only root as not installed. A systemd unit,
deployment state, current release link, release metadata, or active managed
service is still counted conservatively as installed for the fleet guard.

## Status (fleet inspection)

One connection per board, one composite probe on the board.
A root directory containing only preserved `shared/` data is not
considered installed; deployment metadata, a managed unit or a running
managed service must still identify an application installation.

| Item | Source | Verdict rule |
|---|---|---|
| reachable | SSH result | unreachable → board verdict `UNREACHABLE`, other items empty |
| service | `systemctl is-active/is-enabled/show` for **this app's unit only** | not active → fail. The whole-machine `is-system-running` is never used — a healthy fleet board can sit at `degraded` for unrelated reasons (validated finding #3) |
| version | `current` symlink + state.json | differs from the fleet majority → warn (version drift) |
| releases | `releases/` inventory and size | no rollback candidate left → warn |
| disk | free space on the install-root filesystem | <10% warn, <5% fail |
| thermal | all thermal zones enumerated by type name; reports SoC + maximum | ≥75 °C warn, ≥85 °C fail; zone numbering is never hard-coded (finding #7) |
| health | manifest health check run live + journal error count, last hour | health check fails → fail |

Report: JSON schema 1 (`fleet[]` per-board records + summary) and a
terminal table, one row per board. Reports contain aliases only — no
real addresses, hostnames, or serial numbers. A single-file HTML fleet
report is planned for v0.2.

## Feasibility validation (2026-11-02, real RK3588 board)

Environment: RK3588, Ubuntu 22.04.5, kernel 6.1.118, systemd 249,
Python 3.10.12; root filesystem 87% used (15.9 GB free); machine load
≈16.6 from unrelated pre-existing workloads (deployment actions are
not CPU-bound, so this does not distort the timings).

Method: a controller-side pack script + the packaged POSIX `sh`
installer, using a test install prefix instead of `/opt`. The demo app
was a generic heartbeat program; its "model" file was an opaque
placeholder blob, exercising the transport-without-inspection rule.

Result: **all stages passed**, 105.2 s end to end including cleanup.

| Stage | Result | Time |
|---|---|---|
| pack v1.0.0 (2.4 KB package) | checksums + package.json + unit template rendered | 0.01 s |
| push v1 (scp, 2 files) | arrived in board inbox | 14.0 s — almost all connection setup |
| install v1 | verify → unpack → render unit → daemon-reload → enable → restart → heartbeat confirms 1.0.0 | 19.9 s (incl. controller-side polling, see finding #2) |
| pack + push v1.1.0 | same as above | 0.01 s + 7.3 s |
| install v1.1.0 | `current` flips, service active, heartbeat reports 1.1.0; a marker file written by v1 into `shared/` survives | 20.8 s (incl. polling) |
| rollback to v1.0.0 | no re-upload; symlink flip + restart; heartbeat back to 1.0.0 | 14.0 s (connection + checks dominate; the switch itself is instant) |
| status snapshot | service / enabled / version / disk / 7 thermal zones (SoC 49.0 °C, max 50.8 °C) in one probe | 4.7 s |
| cleanup | test unit removed, `daemon-reload`, test dirs deleted; `systemctl --failed` identical to before the test | included in total |

### Findings (each is already folded into the rules above)

1. **Board-side Python cannot be trusted.** The validation board has
   Python 3.10 (no `tomllib`) and a stray PyYAML 5.4.1; another BSP may
   have neither. Hence: the board never parses the manifest — only
   rendered `package.json` + pure shell.
2. **SSH connection setup dominates everything.** ~5–7 s per
   connection through a tunnel; a 2.4 KB push took 7–14 s, and
   controller-side per-second health polling stretched installs to
   ~20 s. Rules derived: one connection per board per operation,
   health waiting on the board, ControlMaster on the controller. At
   20 boards, chatty per-command SSH turns a status round into pure
   waiting.
3. **Whole-machine systemd state is meaningless for app health.** The
   board reports `degraded` because of an unrelated logrotate unit.
   Status judges only the app's own unit.
4. **Free disk is a real constraint** (87% used) — hence the deploy
   preflight disk gate and `keep_releases` pruning.
5. **The directory-symlink trap is real.** `ln -sfn` on a symlink that
   points to a directory follows it and creates the new link inside
   the target. Atomic switch = new symlink + `mv -Tf`.
6. **Unit files cannot be rendered at pack time** — ExecStart depends
   on install root and run user, which are board-level facts.
7. **Thermal zones must be enumerated by type name.** The board has 7
   zones (soc / bigcore0 / bigcore1 / littlecore / gpu / npu /
   center); layouts differ per BSP.

### Limits of this validation (stated, not hidden)

- Single board: multi-board concurrency and partial-failure summaries
  are not yet exercised; scheduled for the v0.1 development week with
  a second reachable board (or multiple aliases of one board).
- Test prefix instead of `/opt`: production-path permissions are
  first on the list for the v0.1 week.
- The demo app ran no real RKNN inference — by design the deploy layer
  is model-agnostic; inference correctness belongs to diagnostic
  tooling, not deployment tooling.

## v0.1 implementation validation (2026-11-02, shipped CLI, same board)

The released implementation was run against the same RK3588 board
through the real `rkdeploy` CLI (fleet of one, install root under a
test prefix, service name `rkdeploy-demo`):

| Step | Result |
|---|---|
| `deploy` demo 1.0.0 | ok in 21.0 s (mkdir + scp + install = 3 tunnel connections) |
| `status` | ATTENTION — only one release installed, i.e. no rollback candidate; the warning is the spec's own rule firing correctly |
| `deploy` demo 1.1.0 | ok in 19.4 s |
| `status` | HEALTHY — service active, version 1.1.0, disk free 13%, temps SoC 49 °C / max 50 °C |
| `deploy` broken 1.2.0 (health file never appears, 5 s timeout) | health failed → **auto-rollback to 1.1.0**, CLI exit 6, 25.6 s total |
| `rollback --to 1.0.0` | ok; board `state.json` history records all four actions in order |
| `releases` / final `status` | releases [1.0.0, 1.1.0, 1.2.0], current 1.0.0, fleet verdict HEALTHY |

A genuine report from the final status run ships as
`examples/status-report-rk3588.json`. After validation the test
service and directories were removed from the board; its pre-existing
unrelated failed unit was the only entry left in
`systemctl --failed`, exactly as before.

Implementation detail that differs from the letter of the early
draft, deliberately: the package also carries a rendered
`package.env` (flat, shell-quoted KEY=VALUE) next to `package.json`,
so the board installer never needs even a JSON parser — it reads only
`package.env`, and each installed release gains an `install.env` with
board-level values (install root, resolved health-check path) already
substituted.

## Out of scope for v0.1

Resident board agents, central servers and web UIs, automatic package
installation, model conversion, log aggregation, canary strategies
(`--hosts` selection is the v0.1 batching mechanism), non-Linux
boards, app stores / registries.

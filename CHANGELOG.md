# Changelog

## Unreleased

- `deploy --dry-run`: controller-side preview — per-board target root
  and the fully rendered systemd unit, with zero SSH contact.
  Contributed by @faizansaiyed123 in #6 (first external PR; closes #3).

- `rkdeploy uninstall`: fleet-wide preflight before removing an app;
  refuses to remove the final installed copy unless
  `--allow-last-copy` is explicit. Stops/disables the managed service,
  removes release and deployment metadata, and preserves `shared/` unless
  `--purge` is supplied. The status probe no longer treats a shared-only
  install root as an installed application.
- Uninstall coverage for last-copy protection, unreachable inventory,
  shared-data preservation/purge, idempotency, symlink refusal and stop
  failures.

## v0.1.0 — 2026-11-02

First release. Full pipeline implemented and validated end to end on a
real RK3588 board (see the validation section in docs/SPEC.md):

- `rkdeploy pack` — manifest (`rkdeploy.yaml`, schema 1) rendered to
  `package.json` / `package.env` / `DEPENDENCIES.json`, deterministic
  checksummed tarball containing only the files the manifest declares.
- `rkdeploy deploy` — fleet push over SSH (one upload + one execution
  per board), board-side POSIX sh installer: sha256 gate, dependency
  and disk preflight, immutable release directories, systemd unit
  rendered at install time, atomic `current` symlink switch, on-board
  health wait, automatic rollback on health failure (exit codes
  0/2/4/5/6/7 distinguish every outcome).
- `rkdeploy rollback` — symlink flip + unit re-render + health check,
  no re-upload; downgrades are only possible through this command.
- `rkdeploy status` / `releases` — one-probe-per-board fleet report
  (service / version + drift / releases / disk / thermal / health),
  JSON schema 1 plus a terminal table. Reports carry fleet aliases
  only.
- Health checks: file (with a freshness guard — a heartbeat file kept
  warm by the previous version's process cannot pass a new deploy),
  command, or systemd-only.
- 14 tests, including a full board lifecycle run against a fake
  systemctl that really starts the demo app: install → upgrade →
  refusal of a duplicate version → rollback → broken release →
  automatic rollback.

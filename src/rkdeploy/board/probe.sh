#!/bin/sh
# rk-deploy fleet status probe (generic, POSIX sh, read-only).
# Usage: probe.sh <install_root> <app_name>
# Prints KEY=VALUE lines; the controller turns them into the report.
set -u
ROOT="$1"; APP_NAME="$2"
SYSTEMCTL="${RKDEPLOY_SYSTEMCTL:-systemctl}"
SYSTEMD_DIR="${RKDEPLOY_SYSTEMD_DIR:-/etc/systemd/system}"
UNIT="rkdeploy-${APP_NAME}.service"
UNIT_PATH="$SYSTEMD_DIR/$UNIT"

echo "PROBE=1"

# An install root can legitimately remain after uninstall because shared/
# survives. Count deployment metadata, a release, a unit file, or a running
# service instead of treating any directory as an installed application.
ROOT_EXISTS=0
[ -d "$ROOT" ] && ROOT_EXISTS=1
SERVICE_ACTIVE=$("$SYSTEMCTL" is-active "$UNIT" 2>/dev/null || true)
[ -n "$SERVICE_ACTIVE" ] || SERVICE_ACTIVE=unknown
SERVICE_ENABLED=$("$SYSTEMCTL" is-enabled "$UNIT" 2>/dev/null || true)
[ -n "$SERVICE_ENABLED" ] || SERVICE_ENABLED=unknown

INSTALLED=0
if [ -f "$ROOT/state.json" ] || [ -L "$ROOT/current" ] || [ -e "$UNIT_PATH" ] || [ -L "$UNIT_PATH" ]; then
  INSTALLED=1
fi
if [ -d "$ROOT/releases" ] && [ ! -L "$ROOT/releases" ]; then
  for release in "$ROOT"/releases/*; do
    [ -d "$release" ] || continue
    if [ -f "$release/install.env" ]; then
      INSTALLED=1
      break
    fi
  done
fi
case "$SERVICE_ACTIVE" in
  active|activating|reloading|deactivating) INSTALLED=1 ;;
esac

echo "INSTALLED=$INSTALLED"
echo "SERVICE_ACTIVE=$SERVICE_ACTIVE"
echo "SERVICE_ENABLED=$SERVICE_ENABLED"
if [ "$INSTALLED" = "0" ]; then
  echo "VERSION="
  echo "PREVIOUS="
  exit 0
fi

VERSION=""; PREVIOUS=""
if [ "$ROOT_EXISTS" = "1" ] && [ -L "$ROOT/current" ]; then
  VERSION=$(basename "$(readlink "$ROOT/current")")
fi
if [ "$ROOT_EXISTS" = "1" ] && [ -f "$ROOT/state.json" ]; then
  PREVIOUS=$(sed -n 's/.*"previous": "\([^"]*\)".*/\1/p' "$ROOT/state.json" | head -1)
fi
echo "VERSION=$VERSION"
echo "PREVIOUS=$PREVIOUS"

PID=$("$SYSTEMCTL" show -p MainPID "$UNIT" 2>/dev/null | sed -n 's/^MainPID=//p')
echo "MAIN_PID=${PID:-0}"
SINCE=$("$SYSTEMCTL" show -p ExecMainStartTimestamp "$UNIT" 2>/dev/null | sed -n 's/^ExecMainStartTimestamp=//p')
echo "SERVICE_SINCE=$SINCE"

if [ "$ROOT_EXISTS" = "1" ]; then
  REL=$(ls "$ROOT/releases" 2>/dev/null | sort | tr '\n' ',' | sed 's/,$//')
  RELEASE_COUNT=$(ls "$ROOT/releases" 2>/dev/null | wc -l | tr -d ' ')
  DISK_FREE_PCT=$(df -P "$ROOT" 2>/dev/null | awk 'NR==2 {gsub(/%/,"",$5); print 100-$5}')
  DISK_FREE_KB=$(df -Pk "$ROOT" 2>/dev/null | awk 'NR==2 {print $4}')
else
  REL=""
  RELEASE_COUNT=0
  DISK_FREE_PCT=""
  DISK_FREE_KB=""
fi
echo "RELEASES=$REL"
echo "RELEASE_COUNT=$RELEASE_COUNT"
echo "DISK_FREE_PCT=$DISK_FREE_PCT"
echo "DISK_FREE_KB=$DISK_FREE_KB"

SOC=""; MAX=-1
for z in /sys/class/thermal/thermal_zone*; do
  [ -f "$z/type" ] && [ -f "$z/temp" ] || continue
  T=$(cat "$z/type" 2>/dev/null); V=$(cat "$z/temp" 2>/dev/null) || continue
  case "$V" in ''|*[!0-9-]*) continue ;; esac
  C=$((V / 1000))
  [ "$C" -gt "$MAX" ] && MAX=$C
  case "$T" in *soc*) SOC=$C ;; esac
done
echo "TEMP_SOC_C=$SOC"
echo "TEMP_MAX_C=$MAX"

# Live health check, once, from the current release's install.env.
HEALTH=skip
DIR="$ROOT/current"
if [ -f "$DIR/install.env" ]; then
  HT=$(grep '^HEALTH_TYPE=' "$DIR/install.env" | cut -d= -f2-)
  HP=$(grep '^HEALTH_PATH=' "$DIR/install.env" | cut -d= -f2-)
  HC=$(grep '^HEALTH_COMMAND=' "$DIR/install.env" | cut -d= -f2-)
  case "$HT" in
    file) [ -f "$HP" ] && HEALTH=ok || HEALTH=fail ;;
    command) sh -c "$HC" >/dev/null 2>&1 && HEALTH=ok || HEALTH=fail ;;
    systemd-only) [ "$("$SYSTEMCTL" is-active "$UNIT" 2>/dev/null)" = "active" ] && HEALTH=ok || HEALTH=fail ;;
  esac
fi
echo "HEALTH=$HEALTH"

ERRS=-1
if command -v journalctl >/dev/null 2>&1; then
  ERRS=$(journalctl -u "$UNIT" --since "-1h" -p err --no-pager 2>/dev/null | grep -c . || echo -1)
fi
echo "JOURNAL_ERRORS_1H=$ERRS"

#!/bin/sh
# rk-deploy board uninstall (generic, POSIX sh).
# Usage: uninstall.sh <install_root> <app_name> [purge: 0|1]
# Stops/disables the managed service, removes managed releases and metadata,
# and preserves shared/ unless purge=1 is explicitly requested.
set -u

ROOT=${1:-}
APP_NAME=${2:-}
PURGE=${3:-0}
SYSTEMCTL="${RKDEPLOY_SYSTEMCTL:-systemctl}"
SYSTEMD_DIR="${RKDEPLOY_SYSTEMD_DIR:-/etc/systemd/system}"
UNIT="rkdeploy-${APP_NAME}.service"

say() { echo "[rkdeploy] $*"; }
abort() {
  say "ABORT: $*"
  echo "RKDEPLOY_RESULT=aborted reason=$1"
  exit 4
}
fail() {
  say "ERROR: $*"
  echo "RKDEPLOY_RESULT=error reason=$1"
  exit 7
}

[ -n "$ROOT" ] || abort missing-install-root
[ -n "$APP_NAME" ] || abort missing-app-name

# Keep this validation in step with manifest.NAME_RE. It prevents app names
# from changing the unit path or the managed paths below.
case "$APP_NAME" in
  ''|*[!a-z0-9-]*|[!a-z]*) abort invalid-app-name ;;
esac
APP_NAME_LEN=${#APP_NAME}
[ "$APP_NAME_LEN" -ge 2 ] && [ "$APP_NAME_LEN" -le 32 ] || abort invalid-app-name

case "$PURGE" in
  0|1) ;;
  *) abort invalid-purge-option ;;
esac

case "$ROOT" in
  /*) ;;
  *) abort install-root-must-be-absolute ;;
esac
while [ "$ROOT" != "/" ] && [ "${ROOT%/}" != "$ROOT" ]; do
  ROOT=${ROOT%/}
done
case "$ROOT" in
  /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/media|/mnt|/opt|/proc|/root|/run|/sbin|/sys|/tmp|/usr|/var)
    abort install-root-is-a-system-directory
    ;;
esac
case "$ROOT" in
  *"/../"*|*"/./"*|*/..|*/.) abort install-root-contains-dot-segment ;;
  *"//"*) abort install-root-contains-empty-path-component ;;
esac

# Reject a symlink in any component of the install-root path. Checking only
# the final directory is insufficient: a symlinked parent would redirect
# every later rm/rmdir operation to a different tree.
CHECK_PATH="$ROOT"
while [ "$CHECK_PATH" != "/" ]; do
  [ ! -L "$CHECK_PATH" ] || abort install-root-path-contains-symlink
  PARENT=${CHECK_PATH%/*}
  [ -n "$PARENT" ] || PARENT="/"
  [ "$PARENT" != "$CHECK_PATH" ] || break
  CHECK_PATH="$PARENT"
done

# Validate every managed path before making changes. In particular, never
# recurse through a symlink supplied in place of a managed directory/file.
if [ -L "$ROOT" ]; then abort install-root-is-a-symlink; fi
if [ -e "$ROOT" ] && [ ! -d "$ROOT" ]; then abort install-root-is-not-a-directory; fi

RELEASES="$ROOT/releases"
CURRENT="$ROOT/current"
STATE="$ROOT/state.json"
HISTORY="$ROOT/history.jsonl"
STATE_TMP="$ROOT/state.json.tmp"
SHARED="$ROOT/shared"
UNIT_PATH="$SYSTEMD_DIR/$UNIT"

if [ -L "$RELEASES" ]; then abort releases-is-a-symlink; fi
if [ -e "$RELEASES" ] && [ ! -d "$RELEASES" ]; then abort releases-is-not-a-directory; fi

if [ -L "$CURRENT" ]; then
  CURRENT_TARGET=$(readlink "$CURRENT") || abort cannot-read-current-symlink
  case "$CURRENT_TARGET" in
    releases/*) ;;
    *) abort current-symlink-points-outside-releases ;;
  esac
  case "$CURRENT_TARGET" in
    *"/../"*|*"/./"*|*/..|*/.) abort unsafe-current-symlink-target ;;
  esac
elif [ -e "$CURRENT" ]; then
  abort current-is-not-a-symlink
fi

for META_PATH in "$STATE" "$HISTORY" "$STATE_TMP"; do
  if [ -L "$META_PATH" ]; then abort managed-metadata-is-a-symlink; fi
  if [ -e "$META_PATH" ] && [ ! -f "$META_PATH" ]; then abort managed-metadata-is-not-a-file; fi
done

if [ "$PURGE" = "1" ]; then
  if [ -L "$SHARED" ]; then abort shared-is-a-symlink-refusing-purge; fi
  if [ -e "$SHARED" ] && [ ! -d "$SHARED" ]; then abort shared-is-not-a-directory; fi
fi

if [ -L "$UNIT_PATH" ]; then abort systemd-unit-is-a-symlink; fi
if [ -e "$UNIT_PATH" ] && [ ! -f "$UNIT_PATH" ]; then abort systemd-unit-is-not-a-file; fi

# A normal uninstall is idempotent. A later --purge is still allowed to
# remove shared/ after the managed application files have already gone.
UNIT_PRESENT=0
[ -f "$UNIT_PATH" ] && UNIT_PRESENT=1
ACTIVE=$("$SYSTEMCTL" is-active "$UNIT" 2>/dev/null || true)
[ -n "$ACTIVE" ] || abort cannot-query-service-state
case "$ACTIVE" in
  active|activating|reloading|deactivating|maintenance|refreshing|inactive|failed|unknown|not-found)
    ;;
  *) abort unrecognized-service-state ;;
esac

NEED_RELOAD=0
MANAGED_PRESENT=0

[ "$UNIT_PRESENT" = "1" ] && MANAGED_PRESENT=1
[ -f "$STATE" ] && MANAGED_PRESENT=1
[ -f "$HISTORY" ] && MANAGED_PRESENT=1
[ -L "$CURRENT" ] && MANAGED_PRESENT=1
if [ -d "$RELEASES" ]; then
  for RELEASE_DIR in "$RELEASES"/*; do
    [ -d "$RELEASE_DIR" ] || continue
    [ -f "$RELEASE_DIR/install.env" ] && MANAGED_PRESENT=1
  done
fi
case "$ACTIVE" in
  active|activating|reloading|deactivating|maintenance|refreshing) MANAGED_PRESENT=1 ;;
esac

if [ "$UNIT_PRESENT" = "1" ]; then
  # Stop first: disabling a unit does not stop an already running process.
  "$SYSTEMCTL" stop "$UNIT" >/dev/null 2>&1 || fail stop-service
  "$SYSTEMCTL" disable "$UNIT" >/dev/null 2>&1 || fail disable-service
  rm -f "$UNIT_PATH" || fail remove-unit-file
  NEED_RELOAD=1
else
  case "$ACTIVE" in
    active|activating|reloading|deactivating|maintenance|refreshing)
      "$SYSTEMCTL" stop "$UNIT" >/dev/null 2>&1 || fail stop-service
      NEED_RELOAD=1
      ;;
  esac
  ENABLED=$("$SYSTEMCTL" is-enabled "$UNIT" 2>/dev/null || true)
  [ -n "$ENABLED" ] || abort cannot-query-service-enable-state
  case "$ENABLED" in
    enabled|enabled-runtime|linked|linked-runtime|alias)
      "$SYSTEMCTL" disable "$UNIT" >/dev/null 2>&1 || fail disable-service
      NEED_RELOAD=1
      ;;
    disabled|masked|masked-runtime|static|indirect|generated|transient|bad|not-found|unknown)
      ;;
    *) abort unrecognized-service-enable-state ;;
  esac
fi

if [ "$NEED_RELOAD" = "1" ]; then
  "$SYSTEMCTL" daemon-reload >/dev/null 2>&1 || fail systemd-daemon-reload
fi

if [ -d "$ROOT" ]; then
  [ ! -L "$CURRENT" ] || rm -f "$CURRENT" || fail remove-current-link
  [ ! -d "$RELEASES" ] || rm -rf "$RELEASES" || fail remove-releases
  [ ! -f "$STATE" ] || rm -f "$STATE" || fail remove-state
  [ ! -f "$HISTORY" ] || rm -f "$HISTORY" || fail remove-history
  [ ! -f "$STATE_TMP" ] || rm -f "$STATE_TMP" || fail remove-state-temp
  if [ "$PURGE" = "1" ] && [ -d "$SHARED" ]; then
    rm -rf "$SHARED" || fail purge-shared-data
  fi
  # Do not delete unexpected/user-created files. rmdir succeeds only when
  # the root is empty; otherwise leave it and report that fact.
  ROOT_REMAINS=0
  rmdir "$ROOT" 2>/dev/null || ROOT_REMAINS=1
else
  ROOT_REMAINS=0
fi

if [ "$MANAGED_PRESENT" = "0" ] && [ "$PURGE" = "0" ]; then
  say "$APP_NAME is already uninstalled; shared data preserved"
  echo "RKDEPLOY_RESULT=already-uninstalled shared=preserved root_retained=$ROOT_REMAINS"
elif [ "$PURGE" = "1" ]; then
  say "uninstalled $APP_NAME; shared data purged"
  echo "RKDEPLOY_RESULT=uninstalled shared=purged root_retained=$ROOT_REMAINS"
else
  say "uninstalled $APP_NAME; shared data preserved"
  echo "RKDEPLOY_RESULT=uninstalled shared=preserved root_retained=$ROOT_REMAINS"
fi
exit 0

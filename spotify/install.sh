#!/usr/bin/env bash
# Install the host-side Spotify controller as a systemd user service.
#
# The controller needs adb access to the station (persistent ADB on build
# 5000000921) and HTTP access to OpenStation on port 8080. It does not modify
# the device: no APK, no root changes, no /odm files.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="${SCRIPT_DIR}/spotify_control.py"
UNIT_SOURCE="${SCRIPT_DIR}/systemd/station-max-spotify.service.in"

PYTHON="${PYTHON:-$(command -v python3 || true)}"
STATION_URL="${STATION_URL:-http://192.168.10.219:8080}"
ADB_BIN="${ADB_BIN:-$(command -v adb || echo adb)}"
SERIAL="${SERIAL:-192.168.10.219:5555}"
API_PORT="${API_PORT:-8099}"
SETTLE="${SETTLE:-0.8}"
RESUME_WINDOW="${RESUME_WINDOW:-300}"
ACTION="install"

usage() {
    cat <<'EOF'
Usage: ./install.sh [options]

Options:
  --station URL      OpenStation base URL      (default: http://192.168.10.219:8080)
  --adb PATH         adb binary                (default: adb from PATH)
  --serial SERIAL    adb serial                (default: 192.168.10.219:5555)
  --api-port PORT    localhost control API port (default: 8099)
  --settle SECONDS   pause after the answer    (default: 0.8)
  --resume-window S  how long Spotify stays "recent" for resume (default: 300)
  --uninstall        stop and remove the service
  --dry-run          print the generated files without installing
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --station) STATION_URL="$2"; shift 2 ;;
        --adb) ADB_BIN="$2"; shift 2 ;;
        --serial) SERIAL="$2"; shift 2 ;;
        --api-port) API_PORT="$2"; shift 2 ;;
        --settle) SETTLE="$2"; shift 2 ;;
        --resume-window) RESUME_WINDOW="$2"; shift 2 ;;
        --uninstall) ACTION="uninstall"; shift ;;
        --dry-run) ACTION="dry-run"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
    esac
done

if [ -z "${PYTHON}" ]; then
    echo "python3 not found; set PYTHON=/path/to/python3" >&2
    exit 1
fi

DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/station-max-spotify"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}"
UNIT_DIR="${CONFIG_DIR}/systemd/user"
ENV_FILE="${CONFIG_DIR}/station-max-spotify.env"
UNIT_FILE="${UNIT_DIR}/station-max-spotify.service"
UNIT_NAME="station-max-spotify.service"

if [ "${ACTION}" = "uninstall" ]; then
    systemctl --user disable --now "${UNIT_NAME}" 2>/dev/null || true
    rm -f "${UNIT_FILE}" "${ENV_FILE}"
    rm -rf "${DATA_DIR}"
    systemctl --user daemon-reload 2>/dev/null || true
    echo "Removed ${UNIT_NAME} and ${DATA_DIR}"
    exit 0
fi

UNIT_CONTENT="$(sed \
    -e "s|@PYTHON@|${PYTHON}|g" \
    -e "s|@INSTALL_DIR@|${DATA_DIR}|g" \
    -e "s|@ENV_FILE@|${ENV_FILE}|g" \
    "${UNIT_SOURCE}")"

ENV_CONTENT="$(cat <<EOF
STATION_URL=${STATION_URL}
STATION_ADB=${ADB_BIN}
STATION_SERIAL=${SERIAL}
STATION_API_PORT=${API_PORT}
STATION_SETTLE_SECONDS=${SETTLE}
STATION_RESUME_WINDOW=${RESUME_WINDOW}
EOF
)"

if [ "${ACTION}" = "dry-run" ]; then
    echo "== ${ENV_FILE} =="
    echo "${ENV_CONTENT}"
    echo "== ${UNIT_FILE} =="
    echo "${UNIT_CONTENT}"
    exit 0
fi

mkdir -p "${DATA_DIR}" "${UNIT_DIR}"
install -m 0755 "${SOURCE}" "${DATA_DIR}/spotify_control.py"
printf '%s\n' "${ENV_CONTENT}" > "${ENV_FILE}"
chmod 0600 "${ENV_FILE}"
printf '%s\n' "${UNIT_CONTENT}" > "${UNIT_FILE}"
chmod 0644 "${UNIT_FILE}"

systemctl --user daemon-reload
systemctl --user enable --now "${UNIT_NAME}"

echo "Installed ${UNIT_NAME}"
echo "  station: ${STATION_URL}"
echo "  adb:     ${ADB_BIN} -s ${SERIAL}"
echo "  api:     http://127.0.0.1:${API_PORT}/status"
echo "Logs: journalctl --user -u ${UNIT_NAME} -f"

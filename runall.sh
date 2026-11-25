#!/usr/bin/env bash
# Runs all data collection and report scripts (Linux / macOS / OCI Cloud Shell).
# Settings such as report location and upload bucket are in config.json
# (copy config.example.json to config.json first).
#
# Options:
#   Authentication and region options are passed to every get*.py script:
#     -cp PROFILE  -ip  -dt  -st  -i [PROFILE]  -rg REGIONS  -log [FILE]
#   Download options are passed to getFocusReports.py only (default: -update):
#     -year YYYY  -month MM  -day DD  -force  -update
# Examples:
#   ./runall.sh                          # new reports only (Cloud Shell: delegation token)
#   ./runall.sh -cp MYPROFILE -year 2026 -month 10
#
# If ./.venv exists it is activated first; PYTHON=... overrides the interpreter.
set -euo pipefail

cd "$(dirname "$0")"

# Use the project virtual environment when it exists (see README).
if [ -f .venv/bin/activate ]; then
    set +u   # activate scripts may reference unset variables
    # shellcheck disable=SC1091
    source .venv/bin/activate
    set -u
    echo "Using virtual environment: $VIRTUAL_ENV"
else
    echo "No .venv found, using the system Python"
fi

PYTHON="${PYTHON:-python3}"
echo "Python: $(command -v "$PYTHON") ($("$PYTHON" --version 2>&1))"

run() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] === $* ==="
    if ! "$PYTHON" "$@"; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] *** FAILED: $* - stopping" >&2
        exit 1
    fi
}

# Split the command line: download options for getFocusReports.py, the rest for all get*.py.
auth_args=()
download_args=()
while [ $# -gt 0 ]; do
    case "$1" in
        -year|-month|-day)
            [ $# -ge 2 ] || { echo "Error: $1 needs a value" >&2; exit 1; }
            download_args+=("$1" "$2"); shift 2 ;;
        -force|-update)
            download_args+=("$1"); shift ;;
        -cp|-rg)
            [ $# -ge 2 ] || { echo "Error: $1 needs a value" >&2; exit 1; }
            auth_args+=("$1" "$2"); shift 2 ;;
        -i|--interactive|-log)
            # optional value
            if [ $# -ge 2 ] && [ "${2#-}" = "$2" ]; then
                auth_args+=("$1" "$2"); shift 2
            else
                auth_args+=("$1"); shift
            fi ;;
        *)
            auth_args+=("$1"); shift ;;
    esac
done
[ ${#download_args[@]} -gt 0 ] || download_args=(-update)

echo "Running all data..."
run getFocusReports.py ${download_args[@]+"${download_args[@]}"} ${auth_args[@]+"${auth_args[@]}"}

# First run: build the consolidated reports now, so the next steps have data to work with.
if [ -z "$(find FocusReports/ConsolidatedCSV -name "????-??-??.csv" -print -quit 2>/dev/null)" ]; then
    echo "No consolidated reports yet (first run), building them first..."
    run makeDailyReports.py
fi

run getStorageReport.py ${auth_args[@]+"${auth_args[@]}"}
run getResourceNames.py ${auth_args[@]+"${auth_args[@]}"}
run getRelationships.py ${auth_args[@]+"${auth_args[@]}"}

echo "Making all the reports..."
run makeDailyReports.py
run makePerUserReport.py
run makeStorageReport.py
run makeHomeReport.py

# Upload the HTML reports to config.json upload.bucket (skipped when empty)
run uploadReports.py

echo "[$(date '+%Y-%m-%d %H:%M:%S')] All done."

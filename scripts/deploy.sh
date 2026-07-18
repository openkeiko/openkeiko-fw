#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT/scripts/load-env.sh"
load_openkeiko_env "$ROOT/.env"

: "${FW1_MAIN_SERIAL:?Set FW1_MAIN_SERIAL in .env or the environment}"
: "${FW1_DISPLAY_SERIAL:?Set FW1_DISPLAY_SERIAL in .env or the environment}"
MAIN_SERIAL=$FW1_MAIN_SERIAL
DISPLAY_SERIAL=$FW1_DISPLAY_SERIAL

wait_for_device() {
  local serial=$1
  local attempt
  for attempt in $(seq 1 30); do
    if uvx mpremote devs 2>/dev/null | grep -q "$serial"; then
      return 0
    fi
    sleep 0.5
  done
  printf 'Timed out waiting for RP2040 %s\n' "$serial" >&2
  return 1
}

mp_device() {
  local serial=$1
  shift
  local attempt
  for attempt in $(seq 1 10); do
    wait_for_device "$serial"
    if uvx mpremote connect "id:${serial}" resume "$@"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

set_maintenance() {
  local serial=$1
  local enabled=$2
  local value=0
  if [[ $enabled == 1 ]]; then
    value=0x4F574D54
  fi
  # Reset is expected to disconnect USB before mpremote receives its reply.
  uvx mpremote connect "id:${serial}" resume exec \
    "from machine import mem32; mem32[0x4005800c] = ${value}; import machine; machine.reset()" \
    >/dev/null 2>&1 || true
  wait_for_device "$serial"
  sleep 0.5
}

main_maintenance=0
display_maintenance=0
cleanup_maintenance() {
  local status=$?
  trap - EXIT
  if [[ $main_maintenance == 1 ]]; then
    set_maintenance "$MAIN_SERIAL" 0 || true
  fi
  if [[ $display_maintenance == 1 ]]; then
    set_maintenance "$DISPLAY_SERIAL" 0 || true
  fi
  exit "$status"
}
trap cleanup_maintenance EXIT

copy_libraries() {
  local serial=$1
  shift
  mp_device "$serial" exec \
    "import os; 'lib' in os.listdir() or os.mkdir('lib')"
  local source
  for source in "$@"; do
    mp_device "$serial" fs cp "$source" ":lib/$(basename "$source")"
  done
}

shopt -s nullglob
common_libraries=("$ROOT"/common/lib/*.py)
display_libraries=("$ROOT"/display/lib/*.py)
main_libraries=("$ROOT"/main/lib/*.py)

printf 'Putting main watchdog into maintenance mode...\n'
set_maintenance "$MAIN_SERIAL" 1
main_maintenance=1
printf 'Deploying main libraries and application...\n'
copy_libraries "$MAIN_SERIAL" "${common_libraries[@]}" "${main_libraries[@]}"
mp_device "$MAIN_SERIAL" fs cp \
  "$ROOT/main/boot.py" :boot.py
mp_device "$MAIN_SERIAL" fs cp \
  "$ROOT/main/main.py" :main.py

printf 'Leaving main maintenance mode and starting its watchdog...\n'
set_maintenance "$MAIN_SERIAL" 0
main_maintenance=0
wait_for_device "$DISPLAY_SERIAL"

printf 'Putting display watchdog into maintenance mode...\n'
set_maintenance "$DISPLAY_SERIAL" 1
display_maintenance=1
printf 'Deploying display libraries and application...\n'
copy_libraries "$DISPLAY_SERIAL" "${common_libraries[@]}" "${display_libraries[@]}"
mp_device "$DISPLAY_SERIAL" exec \
  "import os; [os.mkdir(name) for name in ('infrared', 'subghz') if name not in os.listdir()]"
mp_device "$DISPLAY_SERIAL" fs cp \
  "$ROOT/display/boot.py" :boot.py
mp_device "$DISPLAY_SERIAL" fs cp \
  "$ROOT/display/main.py" :main.py

printf 'Leaving display maintenance mode and starting its watchdog...\n'
set_maintenance "$DISPLAY_SERIAL" 0
display_maintenance=0
wait_for_device "$MAIN_SERIAL"
wait_for_device "$DISPLAY_SERIAL"
printf 'Deployment complete.\n'

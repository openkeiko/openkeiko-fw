#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT/scripts/load-env.sh"
load_openkeiko_env "$ROOT/.env"

: "${FW1_MAIN_SERIAL:?Set FW1_MAIN_SERIAL in .env or the environment}"
: "${FW1_DISPLAY_SERIAL:?Set FW1_DISPLAY_SERIAL in .env or the environment}"
MAIN_SERIAL=$FW1_MAIN_SERIAL
DISPLAY_SERIAL=$FW1_DISPLAY_SERIAL
IMAGE=${1:-$ROOT/.artifacts/micropython/build/FW1_16MB/firmware.uf2}
MAINTENANCE_MAGIC=0x4F574D54
SCRATCH0=0x4005800c
active_serial=

if [[ ! -f $IMAGE ]]; then
  printf 'UF2 image not found: %s\n' "$IMAGE" >&2
  exit 1
fi

wait_for_device() {
  local serial=$1
  local attempt
  for attempt in $(seq 1 60); do
    if uvx mpremote devs 2>/dev/null | grep -q "$serial"; then
      return 0
    fi
    sleep 0.5
  done
  printf 'Timed out waiting for RP2040 %s\n' "$serial" >&2
  return 1
}

wait_for_bootsel() {
  local attempt
  for attempt in $(seq 1 40); do
    if picotool info >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

enter_bootsel() {
  local serial=$1
  local attempt
  for attempt in $(seq 1 4); do
    if wait_for_device "$serial"; then
      # USB disconnect before command completion is expected.
      timeout 6 uvx mpremote connect "id:${serial}" resume exec \
        "from machine import mem32; mem32[$SCRATCH0] = $MAINTENANCE_MAGIC; import machine; machine.bootloader()" \
        >/dev/null 2>&1 || true
    fi
    if wait_for_bootsel; then
      return 0
    fi
    # A wedged application should recover through its eight-second watchdog.
    sleep 9
  done
  printf 'Could not place RP2040 %s into BOOTSEL\n' "$serial" >&2
  return 1
}

leave_maintenance() {
  local serial=$1
  wait_for_device "$serial"
  timeout 6 uvx mpremote connect "id:${serial}" resume exec \
    "from machine import mem32; mem32[$SCRATCH0] = 0; import machine; machine.reset()" \
    >/dev/null 2>&1 || true
  wait_for_device "$serial"
}

cleanup_flash() {
  local status=$?
  trap - EXIT
  if [[ -n $active_serial ]]; then
    if picotool info >/dev/null 2>&1; then
      # Finish a partially written image before returning to application mode.
      picotool load --update --verify "$IMAGE" >/dev/null 2>&1 || true
      picotool reboot --application >/dev/null 2>&1 || true
    fi
    leave_maintenance "$active_serial" >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup_flash EXIT

flash_one() {
  local label=$1
  local serial=$2
  active_serial=$serial
  printf 'Entering %s BOOTSEL...\n' "$label"
  enter_bootsel "$serial"
  printf 'Loading and verifying %s...\n' "$label"
  picotool load --update --verify "$IMAGE"
  picotool reboot --application
  wait_for_device "$serial"
  printf 'Restarting %s with watchdog supervision...\n' "$label"
  leave_maintenance "$serial"
  active_serial=
}

printf 'UF2: %s\n' "$IMAGE"
printf 'SHA-256: '
shasum -a 256 "$IMAGE" | awk '{print $1}'

# Sequential entry guarantees that descriptor-identical ROM devices are never
# ambiguous. Display-first/main-last also matches the stock UF2 updater order.
flash_one display "$DISPLAY_SERIAL"
flash_one main "$MAIN_SERIAL"

wait_for_device "$MAIN_SERIAL"
wait_for_device "$DISPLAY_SERIAL"
printf 'Both FW1 processors flashed and supervised.\n'

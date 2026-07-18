#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 2 || ! $1 =~ ^(main|display)$ || ! $2 =~ ^(on|off)$ ]]; then
  printf 'Usage: %s <main|display> <on|off>\n' "$0" >&2
  exit 2
fi

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT/scripts/load-env.sh"
load_openkeiko_env "$ROOT/.env"

if [[ $1 == main ]]; then
  : "${FW1_MAIN_SERIAL:?Set FW1_MAIN_SERIAL in .env or the environment}"
  serial=$FW1_MAIN_SERIAL
else
  : "${FW1_DISPLAY_SERIAL:?Set FW1_DISPLAY_SERIAL in .env or the environment}"
  serial=$FW1_DISPLAY_SERIAL
fi
if [[ $2 == on ]]; then
  value=0x4F574D54
else
  value=0
fi

# Disconnect during reset is expected.
uvx mpremote connect "id:${serial}" resume exec \
  "from machine import mem32; mem32[0x4005800c] = ${value}; import machine; machine.reset()" \
  >/dev/null 2>&1 || true

for _ in $(seq 1 60); do
  if uvx mpremote devs 2>/dev/null | grep -q "$serial"; then
    printf '%s maintenance %s (%s)\n' "$1" "$2" "$serial"
    exit 0
  fi
  sleep 0.5
done
printf 'Timed out waiting for %s (%s)\n' "$1" "$serial" >&2
exit 1

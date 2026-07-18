#!/usr/bin/env bash
set -euo pipefail

if ! command -v uvx >/dev/null 2>&1; then
  echo "uvx is required to discover MicroPython devices" >&2
  exit 1
fi

devices=$(uvx mpremote devs)
matches=$(printf '%s\n' "$devices" | awk '$2 != "None" && $3 == "2e8a:0005" {print $2 "\t" $1}')

if [[ -z $matches ]]; then
  echo "No RP2040 MicroPython controllers were detected." >&2
  echo "All serial devices reported by mpremote:" >&2
  printf '%s\n' "$devices" >&2
  exit 1
fi

printf 'Detected RP2040 MicroPython controllers:\n\n'
printf 'SERIAL\tUSB DEVICE\n%s\n\n' "$matches"
printf '%s\n' \
  "The controllers use identical USB descriptors, so their main/display roles" \
  "cannot be inferred safely from enumeration alone. Assign the verified roles" \
  "to FW1_MAIN_SERIAL and FW1_DISPLAY_SERIAL in .env."

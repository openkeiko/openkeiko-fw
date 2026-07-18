"""Hardware-watchdog and reset-scratch helpers for both FW1 RP2040s."""

from machine import WDT, mem32


WATCHDOG_BASE = 0x40058000
SCRATCH0 = WATCHDOG_BASE + 0x0C

MAINTENANCE_MAGIC = 0x4F574D54  # OWMT
WATCHDOG_TIMEOUT_MS = 8_000


def maintenance_enabled():
    return mem32[SCRATCH0] == MAINTENANCE_MAGIC


def set_maintenance(enabled):
    mem32[SCRATCH0] = MAINTENANCE_MAGIC if enabled else 0


def start_watchdog(timeout_ms=WATCHDOG_TIMEOUT_MS):
    if maintenance_enabled():
        return None
    watchdog = WDT(timeout=timeout_ms)
    watchdog.feed()
    return watchdog


def feed_watchdog(watchdog):
    if watchdog is not None:
        watchdog.feed()

"""Early hardware initialization for the OpenKeiko FW1 main CPU."""

from machine import Pin


# GPIO28 is the display RP2040's active-low RUN input. Keep it released during
# normal boot, software reset, watchdog recovery, and UF2 flashing. The display
# now has its own hardware watchdog, so resetting main must never implicitly
# disconnect display USB. Any future RUN pulse must be an explicit, guarded
# recovery operation rather than a boot side effect.
display_run = Pin(28, Pin.OUT, value=1)

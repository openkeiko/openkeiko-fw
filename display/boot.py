"""Early LED-safe initialization for the OpenKeiko FW1 display CPU."""

from ws2812 import WS2812

# Clear the chain before main.py starts. The object stays alive through boot.py,
# and main.py then reinitializes the same PIO state machine.
boot_leds = WS2812(
    pin=7,
    count=7,
    brightness=0.0,
    inverted=True,
    state_machine=0,
)
boot_leds.off()

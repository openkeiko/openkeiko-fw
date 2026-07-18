"""Minimal RP2040 WS2812 driver with optional output inversion."""

import array
from machine import Pin
import rp2
import time


@rp2.asm_pio(
    sideset_init=rp2.PIO.OUT_LOW,
    out_shiftdir=rp2.PIO.SHIFT_LEFT,
    autopull=True,
    pull_thresh=24,
)
def _ws2812():
    label("bitloop")
    out(x, 1).side(0)[2]
    jmp(not_x, "zero").side(1)[1]
    jmp("bitloop").side(1)[4]
    label("zero")
    nop().side(0)[4]


@rp2.asm_pio(
    sideset_init=rp2.PIO.OUT_HIGH,
    out_shiftdir=rp2.PIO.SHIFT_LEFT,
    autopull=True,
    pull_thresh=24,
)
def _ws2812_inverted():
    label("bitloop")
    out(x, 1).side(1)[2]
    jmp(not_x, "zero").side(0)[1]
    jmp("bitloop").side(0)[4]
    label("zero")
    nop().side(1)[4]


class WS2812:
    def __init__(
        self,
        pin,
        count,
        brightness=0.08,
        inverted=False,
        state_machine=0,
    ):
        self.count = count
        self._brightness = 0.0
        self._pixels = [(0, 0, 0)] * count

        idle = 1 if inverted else 0
        self._pin = Pin(pin, Pin.OUT, value=idle)
        program = _ws2812_inverted if inverted else _ws2812
        self._sm = rp2.StateMachine(
            state_machine,
            program,
            freq=8_000_000,
            sideset_base=self._pin,
        )
        self._sm.active(1)
        self.brightness = brightness
        self.off()

    @property
    def brightness(self):
        return self._brightness

    @brightness.setter
    def brightness(self, value):
        if value < 0.0:
            value = 0.0
        elif value > 1.0:
            value = 1.0
        self._brightness = value

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        return self._pixels[index]

    def __setitem__(self, index, color):
        self._pixels[index] = self._validate_color(color)

    @staticmethod
    def _validate_color(color):
        if len(color) != 3:
            raise ValueError("expected an RGB tuple")
        return tuple(max(0, min(255, int(channel))) for channel in color)

    def fill(self, color):
        color = self._validate_color(color)
        self._pixels = [color] * self.count

    def show(self):
        level = self._brightness
        words = array.array("I", [0] * self.count)

        for index, (red, green, blue) in enumerate(self._pixels):
            red = int(red * level)
            green = int(green * level)
            blue = int(blue * level)
            words[index] = (green << 16) | (red << 8) | blue

        self._sm.put(words, 8)
        time.sleep_us(100)

    def off(self):
        self.fill((0, 0, 0))
        self.show()
        self.show()

    @staticmethod
    def wheel(position):
        position &= 0xFF
        if position < 85:
            return 255 - position * 3, position * 3, 0
        if position < 170:
            position -= 85
            return 0, 255 - position * 3, position * 3
        position -= 170
        return position * 3, 0, 255 - position * 3

    def rainbow(self, offset=0):
        for index in range(self.count):
            position = offset + index * 256 // self.count
            self[index] = self.wheel(position)
        self.show()

    def deinit(self):
        self.off()
        self._sm.active(0)
